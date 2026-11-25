"""Octopart component search via Altium Nexar GraphQL API."""
from __future__ import annotations
import logging, re
from typing import Any
import httpx
from . import config, libraries
from .models import Part, Pin

logger = logging.getLogger(__name__)

GRAPHQL_SEARCH_QUERY = """
query SupSearch($q: String!, $limit: Int!, $inStock: Boolean) {
  supSearch(q: $q, country: "US", currency: "USD", limit: $limit, inStockOnly: $inStock) {
    total
    results {
      part {
        mpn
        name
        shortDescription
        manufacturer {
          name
        }
        category {
          name
        }
        totalAvail
        medianPrice1000 {
          price
          currency
        }
        octopartUrl
        specs {
          attribute {
            name
            shortname
          }
          displayValue
        }
      }
    }
  }
}
"""

GRAPHQL_MPN_QUERY = """
query SupSearchMpn($q: String!) {
  supSearchMpn(q: $q, country: "US", currency: "USD", limit: 1) {
    results {
      part {
        mpn
        name
        shortDescription
        manufacturer {
          name
        }
        category {
          name
        }
        totalAvail
        medianPrice1000 {
          price
          currency
        }
        octopartUrl
        specs {
          attribute {
            name
            shortname
          }
          displayValue
        }
      }
    }
  }
}
"""


def available() -> bool:
    return bool(config.NEXAR_ACCESS_TOKEN)


def _infer_footprint_and_pins(category: str, package: str, desc: str) -> tuple[str, list[Pin]]:
    cat_lower = category.lower()
    pkg_upper = package.upper()
    desc_upper = desc.upper()

    # Passives (0603, etc.)
    if any(k in cat_lower for k in ("resistor", "capacitor", "inductor")) or any(k in pkg_upper for k in ("0603", "0402", "0805")):
        prefix = "R" if "resistor" in cat_lower else ("C" if "capacitor" in cat_lower else "L")
        fp = f"{prefix}_0603"
        pins = [Pin(num="1", name="1", type="passive"), Pin(num="2", name="2", type="passive")]
        return fp, pins

    # SOT-23-5 (LDOs, switches, etc.)
    if "SOT-23-5" in pkg_upper or "SOT-23-5" in desc_upper or "SOT23-5" in desc_upper or "SOT23-5" in pkg_upper:
        pins = [
            Pin(num="1", name="VIN", type="power_in"),
            Pin(num="2", name="GND", type="ground"),
            Pin(num="3", name="EN", type="input"),
            Pin(num="4", name="NC", type="nc"),
            Pin(num="5", name="VOUT", type="power_out"),
        ]
        return "SOT-23-5", pins

    # SOT-23 (3-pin)
    if "SOT-23" in pkg_upper or "SOT23" in pkg_upper or "SOT-23" in desc_upper:
        pins = [Pin(num="1", name="1", type="passive"), Pin(num="2", name="2", type="passive"), Pin(num="3", name="3", type="passive")]
        return "SOT-23", pins

    # LGA-8 (sensors, BME280 etc.)
    if "LGA-8" in pkg_upper or "LGA8" in pkg_upper or "LGA-8" in desc_upper or "LGA" in pkg_upper:
        pins = [
            Pin(num="1", name="GND", type="ground"),
            Pin(num="2", name="CSB", type="input"),
            Pin(num="3", name="SDI", type="bidir"),
            Pin(num="4", name="SCK", type="input"),
            Pin(num="5", name="SDO", type="bidir"),
            Pin(num="6", name="VDDIO", type="power_in"),
            Pin(num="7", name="GND", type="ground"),
            Pin(num="8", name="VDD", type="power_in"),
        ]
        return "LGA-8", pins

    # Default fallback
    return "", []


def _parse_nexar_part(item: dict[str, Any], local_db: Any = None) -> Part:
    p = item.get("part") or {}
    mpn = p.get("mpn") or p.get("name") or "UNKNOWN"
    mfr = (p.get("manufacturer") or {}).get("name") or ""
    cat = (p.get("category") or {}).get("name") or ""
    desc = p.get("shortDescription") or p.get("name") or ""
    avail = int(p.get("totalAvail") or 0)
    octopart_url = p.get("octopartUrl") or ""

    price_obj = p.get("medianPrice1000") or {}
    price = float(price_obj.get("price") or 0.0)

    specs = {s["attribute"]["shortname"]: s["displayValue"] for s in p.get("specs", []) if s.get("attribute") and s.get("displayValue")}
    package = specs.get("case_package") or specs.get("package_case") or ""
    val = specs.get("capacitance") or specs.get("resistance") or specs.get("inductance") or specs.get("outputvoltage") or ""

    # Parse voltage max
    voltage_max = None
    v_raw = specs.get("maxinputvoltage") or specs.get("maxsupplyvoltage") or specs.get("voltagerating_dc_") or specs.get("voltagerating")
    if v_raw:
        v_match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(v_raw))
        if v_match:
            voltage_max = float(v_match.group(1))

    # If local_db has this part, enrich with ground-truth pins and footprint
    local_part = local_db.get(mpn) if local_db and hasattr(local_db, "get") else None
    if local_part:
        footprint = local_part.footprint_ref
        pins = local_part.pins
        lcsc_id = local_part.lcsc_id
        if not val:
            val = local_part.value
        if not package:
            package = local_part.package
    else:
        lcsc_id = ""
        sym_pins, sym_fp = libraries.lookup_kicad_symbol(mpn)
        if sym_pins:
            pins = sym_pins
            footprint = sym_fp or libraries.infer_footprint_ref(cat, package, desc)
        else:
            footprint, pins = _infer_footprint_and_pins(cat, package, desc)
            if not footprint:
                footprint = libraries.infer_footprint_ref(cat, package, desc)

    return Part(
        mpn=mpn,
        lcsc_id=lcsc_id,
        category=cat,
        package=package,
        description=desc,
        manufacturer=mfr,
        value=val,
        price=price,
        stock=avail,
        voltage_max=voltage_max,
        footprint_ref=footprint,
        pins=pins,
        octopart_url=octopart_url,
    )


def search_octopart(
    query: str,
    filters: dict[str, Any] | None = None,
    limit: int = 5,
    local_db: Any = None,
    timeout: float = 10.0,
) -> list[Part]:
    """Search components on Octopart via Altium Nexar GraphQL API."""
    if not available():
        logger.warning("Octopart search requested but NEXAR_ACCESS_TOKEN is not configured.")
        return []

    f = filters or {}
    in_stock = bool(f.get("in_stock", False))
    headers = {
        "Authorization": f"Bearer {config.NEXAR_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": GRAPHQL_SEARCH_QUERY,
        "variables": {"q": query or "electronics", "limit": limit, "inStock": in_stock if in_stock else None},
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(config.NEXAR_GRAPHQL_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.error("Nexar GraphQL request returned status %s: %s", resp.status_code, resp.text)
                return []
            data = resp.json()
            if "errors" in data:
                logger.error("Nexar GraphQL returned errors: %s", data["errors"])
                return []
            results = data.get("data", {}).get("supSearch", {}).get("results", [])
            parts = [_parse_nexar_part(r, local_db=local_db) for r in results if r.get("part")]
            return parts
    except Exception as exc:
        logger.error("Error connecting to Nexar/Octopart API: %s", exc)
        return []


def get_octopart_part(mpn: str, local_db: Any = None, timeout: float = 10.0) -> Part | None:
    """Retrieve details for a specific MPN via Octopart/Nexar."""
    if not available() or not mpn:
        return None

    headers = {
        "Authorization": f"Bearer {config.NEXAR_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": GRAPHQL_MPN_QUERY,
        "variables": {"q": mpn},
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(config.NEXAR_GRAPHQL_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("data", {}).get("supSearchMpn", {}).get("results", [])
            if results and results[0].get("part"):
                return _parse_nexar_part(results[0], local_db=local_db)
            return None
    except Exception as exc:
        logger.error("Error looking up MPN on Nexar/Octopart: %s", exc)
        return None
