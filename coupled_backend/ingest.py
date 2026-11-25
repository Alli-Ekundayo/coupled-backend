"""Load seed parts and optionally the CDFER/jlcpcb-parts-database CSV.
Enriches JLC rows with KiCad symbol pin tables, KiCad footprints, parsed values/voltages,
and optional easyeda2kicad conversion so ingested parts are immediately netlist-ready."""
from __future__ import annotations

import csv
import re
import sys
from . import libraries
from .models import Part
from .seed import SEED

COLS = {
    "lcsc_id": ["LCSC Part", "lcsc", "LCSC"],
    "mpn": ["MFR.Part", "MFR Part", "mfr"],
    "package": ["Package"],
    "category": ["First Category", "Second Category"],
    "description": ["Description"],
    "manufacturer": ["Manufacturer"],
    "price": ["Price"],
    "stock": ["Stock"],
}

_VAL_RE = re.compile(r"\b(\d+(?:\.\d+)?\s*(?:[pnuµm]?F|[kKmM]?[ΩohmR]+|[pnuµm]?H|V))\b", re.I)
_VOLT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*V\b")


def _pick(row, names):
    for n in names:
        if row.get(n):
            return row[n]
    return ""


def _parse_desc_specs(desc: str) -> tuple[str, float | None]:
    val = ""
    vm = _VAL_RE.search(desc)
    if vm:
        val = vm.group(1).replace(" ", "")
    volt = None
    vs = [float(x) for x in _VOLT_RE.findall(desc)]
    if vs:
        volt = max(vs)
    return val, volt


def load_jlc_csv(path: str, *, enrich: bool = True, use_easyeda: bool = False) -> list[Part]:
    """Load parts from a JLCPCB CSV and enrich with KiCad symbols/footprints so they are netlist-ready."""
    out: list[Part] = []
    with open(path, newline="", encoding="utf-8", errors="ignore") as f:
        for row in csv.DictReader(f):
            d = {k: _pick(row, v) for k, v in COLS.items()}
            if not d["mpn"]:
                continue
            try:
                price = float(re.findall(r"[\d.]+", str(d["price"]))[0])
            except Exception:
                price = 0.0
            val, vmax = _parse_desc_specs(d["description"])
            part = Part(
                mpn=d["mpn"],
                lcsc_id=d["lcsc_id"],
                package=d["package"],
                category=d["category"],
                description=d["description"],
                manufacturer=d["manufacturer"],
                value=val,
                voltage_max=vmax,
                price=price,
                stock=int(float(d["stock"] or 0)),
            )
            if enrich:
                part = libraries.enrich_part(part, use_easyeda=use_easyeda)
            out.append(part)
    return out


def seed_db(db, csv_path: str | None = None, *, use_easyeda: bool = False):
    db.upsert(SEED)
    if csv_path:
        db.upsert(load_jlc_csv(csv_path, enrich=True, use_easyeda=use_easyeda))


if __name__ == "__main__":
    from .config import EMBEDDER
    from .parts import create_parts_db, make_embedder
    d = create_parts_db(make_embedder(EMBEDDER))
    seed_db(d, sys.argv[1] if len(sys.argv) > 1 else None)
    print("parts in db:", d.count())
