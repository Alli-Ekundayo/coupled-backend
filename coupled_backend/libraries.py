"""KiCad footprint (.kicad_mod), KiCad symbol (.kicad_sym), and EasyEDA library loader.

Resolves real footprints and datasheet pin tables from installed KiCad libraries
(KICAD_FOOTPRINTS_DIR / KICAD_SYMBOLS_DIR), cached local libraries (DATA_DIR/footprints),
optional `easyeda2kicad`, and deterministic rules for standard passives/discretes.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import config, sexpr as sx
from .models import Part, Pin
from .sexpr import Sym

logger = logging.getLogger(__name__)


# Canonical short aliases -> KiCad standard Library:Footprint names
FOOTPRINT_ALIASES: dict[str, str] = {
    "R_0402": "Resistor_SMD:R_0402_1005Metric",
    "R_0603": "Resistor_SMD:R_0603_1608Metric",
    "R_0805": "Resistor_SMD:R_0805_2012Metric",
    "R_1206": "Resistor_SMD:R_1206_3216Metric",
    "C_0402": "Capacitor_SMD:C_0402_1005Metric",
    "C_0603": "Capacitor_SMD:C_0603_1608Metric",
    "C_0805": "Capacitor_SMD:C_0805_2012Metric",
    "C_1206": "Capacitor_SMD:C_1206_3216Metric",
    "L_0603": "Inductor_SMD:L_0603_1608Metric",
    "L_0805": "Inductor_SMD:L_0805_2012Metric",
    "LED_0603": "LED_SMD:LED_0603_1608Metric",
    "LED_0805": "LED_SMD:LED_0805_2012Metric",
    "D_SOD-123": "Diode_SMD:D_SOD-123",
    "D_SOD-323": "Diode_SMD:D_SOD-323",
    "SOT-23": "Package_TO_SOT_SMD:SOT-23",
    "SOT-23-3": "Package_TO_SOT_SMD:SOT-23-3",
    "SOT-23-5": "Package_TO_SOT_SMD:SOT-23-5",
    "SOT-23-6": "Package_TO_SOT_SMD:SOT-23-6",
    "SOT-223": "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    "SOIC-8": "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
    "SOIC-16": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
    "TSSOP-16": "Package_SO:TSSOP-16_4.4x5mm_P0.65mm",
    "QFN-16": "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm",
    "QFN-24": "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm",
    "QFN-32": "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm",
    "LGA-8": "Package_LGA:Bosch_LGA-8_2.5x2.5mm_P0.65mm_ClockwisePinNumbering",
    "USBC-6P": "Connector_USB:USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal",
    "USBC-16P": "Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
    "HDR-1x02": "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
    "HDR-1x03": "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
    "HDR-1x04": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "HDR-1x06": "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
    "HDR-1x08": "Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical",
}

_GND_NAMES = frozenset({"GND", "AGND", "DGND", "PGND", "VSS", "VSSA", "VSSD", "GNDA", "GNDD", "EP", "SHIELD", "GND1", "GND2"})
_GRAPHIC_HEADS = frozenset({"fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"})
_KICAD_PIN_TYPE_MAP = {
    "power_in": "power_in",
    "power_out": "power_out",
    "bidirectional": "bidir",
    "input": "input",
    "output": "output",
    "tri_state": "output",
    "open_collector": "output",
    "open_emitter": "output",
    "no_connect": "nc",
    "passive": "passive",
    "free": "passive",
    "unspecified": "passive",
}


@dataclass
class FootprintDef:
    """Parsed KiCad .kicad_mod footprint definition."""
    lib_ref: str
    pads: list[dict[str, Any]]
    graphics: list[list] = field(default_factory=list)
    courtyard_bbox: tuple[float, float, float, float] | None = None
    attr: str = "smd"
    source_path: str = ""


def _upgrade_graphic_v9(node: list) -> list:
    """Normalize a .kicad_mod graphic primitive (KiCad 5/6/7/8) to KiCad 9 (20241229) syntax."""
    head = sx.head(node)
    out: list[Any] = [Sym(head)]
    width: float | None = None
    has_stroke = False
    has_fill = False
    for child in node[1:]:
        if not isinstance(child, list):
            out.append(child)
            continue
        ch = sx.head(child)
        if ch in ("tstamp", "uuid"):
            continue  # replaced with fresh uuid on write
        if ch == "width":
            nums = sx.nums(child)
            if nums:
                width = nums[0]
            continue
        if ch == "stroke":
            has_stroke = True
            # Ensure stroke has valid type token
            st = [Sym("stroke")]
            has_type = False
            for sc in child[1:]:
                if isinstance(sc, list) and sx.head(sc) == "type":
                    has_type = True
                st.append(sc)
            if not has_type:
                st.append([Sym("type"), Sym("solid")])
            out.append(st)
            continue
        if ch == "layer" and len(child) >= 2:
            out.append([Sym("layer"), str(child[1])])
            continue
        if ch == "fill":
            has_fill = True
            val = str(child[1]) if len(child) > 1 else "none"
            if val == "no":
                val = "none"
            elif val == "yes":
                val = "solid"
            out.append([Sym("fill"), Sym(val)])
            continue
        out.append(child)
    if not has_stroke:
        out.append([Sym("stroke"), [Sym("width"), width if width is not None else 0.12], [Sym("type"), Sym("solid")]])
    if head in ("fp_rect", "fp_circle", "fp_poly") and not has_fill:
        out.append([Sym("fill"), Sym("none")])
    return out


def _extract_courtyard_bbox(graphics: list[list]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for g in graphics:
        ly = sx.find(g, "layer")
        if not ly or str(ly[1]) not in ("F.CrtYd", "B.CrtYd"):
            continue
        for key in ("start", "end", "center", "mid"):
            pt = sx.find(g, key)
            if pt:
                nums = sx.nums(pt)
                if len(nums) >= 2:
                    xs.append(nums[0])
                    ys.append(nums[1])
        pts = sx.find(g, "pts")
        if pts:
            for q in pts[1:]:
                if isinstance(q, list) and len(q) >= 3:
                    try:
                        xs.append(float(q[1]))
                        ys.append(float(q[2]))
                    except (TypeError, ValueError):
                        pass
    if len(xs) >= 2 and len(ys) >= 2:
        return (min(xs), min(ys), max(xs), max(ys))
    return None


def parse_kicad_mod(text: str, lib_ref: str = "", source_path: str = "") -> FootprintDef:
    """Parse a KiCad .kicad_mod S-expression (KiCad 5/6/7/8/9) into FootprintDef."""
    tree = sx.parse(text)
    head = sx.head(tree)
    if head not in ("footprint", "module"):
        raise ValueError(f"not a KiCad footprint (.kicad_mod): root is {head!r}")

    name = lib_ref or (str(tree[1]) if len(tree) > 1 else "unknown")
    pads: list[dict[str, Any]] = []
    graphics: list[list] = []
    attr = "smd"

    for child in tree[1:]:
        if not isinstance(child, list):
            continue
        ch = sx.head(child)
        if ch == "attr":
            tokens = [str(x) for x in child[1:]]
            if "through_hole" in tokens:
                attr = "through_hole"
            elif "smd" in tokens:
                attr = "smd"
        elif ch in _GRAPHIC_HEADS:
            graphics.append(_upgrade_graphic_v9(child))
        elif ch == "pad" and len(child) >= 4:
            pnum = str(child[1])
            pkind = str(child[2])
            pshape = str(child[3])
            at_n = sx.find(child, "at")
            at_v = (sx.nums(at_n) + [0.0, 0.0, 0.0])[:3] if at_n else [0.0, 0.0, 0.0]
            sz_n = sx.find(child, "size")
            sz_v = (sx.nums(sz_n) + [1.0, 1.0])[:2] if sz_n else [1.0, 1.0]
            dr_n = sx.find(child, "drill")
            drill_x = 0.0
            drill_y = 0.0
            if dr_n:
                d_nums = sx.nums(dr_n)
                if d_nums:
                    drill_x = d_nums[0]
                    if len(d_nums) >= 2 and any(str(x) == "oval" for x in dr_n[1:]):
                        drill_y = d_nums[1]
            rr_n = sx.find(child, "roundrect_rratio")
            rr = sx.nums(rr_n)[0] if rr_n and sx.nums(rr_n) else 0.25
            ly_n = sx.find(child, "layers")
            layers = tuple(str(x) for x in ly_n[1:]) if ly_n else None
            pads.append({
                "num": pnum,
                "x": at_v[0],
                "y": at_v[1],
                "rot": at_v[2],
                "w": sz_v[0],
                "h": sz_v[1],
                "kind": pkind,
                "shape": pshape,
                "drill": drill_x,
                "drill_y": drill_y,
                "roundrect_rratio": rr,
                "layers": layers,
            })

    if any(p["kind"] == "thru_hole" for p in pads):
        attr = "through_hole"
    crtyd = _extract_courtyard_bbox(graphics)
    return FootprintDef(lib_ref=name, pads=pads, graphics=graphics, courtyard_bbox=crtyd, attr=attr, source_path=source_path)


@lru_cache(maxsize=4)
def _footprint_dir_index(root_str: str) -> dict[str, Path]:
    """Build stem -> Path map across *.pretty directories under root."""
    root = Path(root_str)
    idx: dict[str, Path] = {}
    if not root.is_dir():
        return idx
    for pretty in root.glob("*.pretty"):
        if not pretty.is_dir():
            continue
        lib = pretty.stem
        for mod in pretty.glob("*.kicad_mod"):
            idx[f"{lib}:{mod.stem}"] = mod
            idx.setdefault(mod.stem, mod)
            idx.setdefault(mod.stem.lower(), mod)
    return idx


def _try_easyeda2kicad(lcsc_id: str) -> tuple[Path | None, Path | None]:
    """Run easyeda2kicad CLI if available to fetch symbol & footprint for an LCSC ID."""
    if not lcsc_id or not shutil.which("easyeda2kicad"):
        return None, None
    out_dir = config.DATA_DIR / "easyeda"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_base = out_dir / lcsc_id
    fp_dir = out_dir / f"{lcsc_id}.pretty"
    sym_file = out_dir / f"{lcsc_id}.kicad_sym"
    if not (fp_dir.is_dir() and any(fp_dir.glob("*.kicad_mod"))) and not sym_file.exists():
        try:
            subprocess.run(
                ["easyeda2kicad", "--full", f"--lcsc_id={lcsc_id}", "--output", str(out_base)],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except Exception as exc:
            logger.debug("easyeda2kicad failed for %s: %s", lcsc_id, exc)
            return None, None
    fp = next(fp_dir.glob("*.kicad_mod"), None) if fp_dir.is_dir() else None
    sym = sym_file if sym_file.exists() else None
    return fp, sym


def resolve_kicad_mod(ref: str, lcsc_id: str = "") -> Path | None:
    """Resolve a footprint reference (alias, Lib:Name, stem, or path) to a .kicad_mod file."""
    if not ref and not lcsc_id:
        return None

    # 1. Direct path
    if ref:
        p = Path(ref)
        if p.suffix == ".kicad_mod" and p.is_file():
            return p

    # 2. Local project footprints cache (DATA_DIR / "footprints" or "easyeda")
    for cache_dir in (config.DATA_DIR / "footprints", config.DATA_DIR / "easyeda"):
        if cache_dir.is_dir():
            cidx = _footprint_dir_index(str(cache_dir))
            for cand in (ref, FOOTPRINT_ALIASES.get(ref, ""), lcsc_id):
                if cand and cand in cidx:
                    return cidx[cand]
            if lcsc_id:
                easy_pretty = cache_dir / f"{lcsc_id}.pretty"
                if easy_pretty.is_dir():
                    m = next(easy_pretty.glob("*.kicad_mod"), None)
                    if m:
                        return m

    # 3. Installed KiCad footprint libraries
    target = FOOTPRINT_ALIASES.get(ref, ref)
    fp_root = config.KICAD_FOOTPRINTS_DIR
    if target and fp_root.is_dir():
        if ":" in target:
            lib, name = target.split(":", 1)
            direct = fp_root / f"{lib}.pretty" / f"{name}.kicad_mod"
            if direct.is_file():
                return direct
        idx = _footprint_dir_index(str(fp_root))
        for cand in (target, target.split(":")[-1], target.split(":")[-1].lower()):
            if cand in idx:
                return idx[cand]

    # 4. Optional easyeda2kicad fetch by LCSC ID
    if lcsc_id:
        fp, _ = _try_easyeda2kicad(lcsc_id)
        if fp is not None:
            return fp

    return None


@lru_cache(maxsize=256)
def load_footprint(ref: str, lcsc_id: str = "") -> FootprintDef | None:
    """Load and cache a FootprintDef from KiCad library or EasyEDA cache, else None."""
    path = resolve_kicad_mod(ref, lcsc_id=lcsc_id)
    if path is None:
        return None
    try:
        lib_ref = FOOTPRINT_ALIASES.get(ref, ref) or path.stem
        if ":" not in lib_ref and path.parent.suffix == ".pretty":
            lib_ref = f"{path.parent.stem}:{path.stem}"
        return parse_kicad_mod(path.read_text(encoding="utf-8", errors="ignore"), lib_ref=lib_ref, source_path=str(path))
    except Exception as exc:
        logger.warning("Failed to parse .kicad_mod %s: %s", path, exc)
        return None


def clone_graphics_with_uuids(graphics: list[list]) -> list[str]:
    """Serialize footprint graphics as KiCad 9 S-expressions with fresh UUIDs."""
    lines: list[str] = []
    for g in graphics:
        node = list(g) + [[Sym("uuid"), str(uuid.uuid4())]]
        lines.append("    " + sx.dump(node, level=2))
    return lines


# ==============================================================================
# KiCad .kicad_sym symbol index & pin extraction (Gap 3)
# ==============================================================================

_SYM_HEADER_RE = re.compile(r'^\s{2}\(symbol\s+"([^"]+)"', re.MULTILINE)


@lru_cache(maxsize=4)
def _symbol_file_index(root_str: str) -> dict[str, tuple[Path, str]]:
    """Fast scan of *.kicad_sym files -> {normalized_name: (path, exact_symbol_name)}."""
    root = Path(root_str)
    idx: dict[str, tuple[Path, str]] = {}
    if not root.is_dir():
        return idx
    for sym_path in sorted(root.glob("*.kicad_sym")):
        try:
            text = sym_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _SYM_HEADER_RE.finditer(text):
            name = m.group(1)
            # Skip sub-unit symbols like "BME280_1_1"
            if re.search(r"_\d+_\d+$", name):
                continue
            key = name.upper()
            idx.setdefault(key, (sym_path, name))
    return idx


@lru_cache(maxsize=64)
def _parse_symbol_file(path_str: str) -> dict[str, list]:
    """Parse one .kicad_sym file into {symbol_name: sexpr_node}."""
    tree = sx.parse(Path(path_str).read_text(encoding="utf-8", errors="ignore"))
    out: dict[str, list] = {}
    for child in tree[1:]:
        if isinstance(child, list) and sx.head(child) == "symbol" and len(child) >= 2:
            out[str(child[1])] = child
    return out


def _normalize_pin_type(kicad_type: str, pin_name: str) -> str:
    clean_name = pin_name.strip().upper().lstrip("~")
    if clean_name in _GND_NAMES or clean_name.startswith(("GND", "VSS", "PGND", "AGND", "DGND")):
        return "ground"
    return _KICAD_PIN_TYPE_MAP.get(kicad_type, "passive")


def _extract_symbol_data(sym_map: dict[str, list], name: str, _seen: set[str] | None = None) -> tuple[list[Pin], str]:
    """Extract (pins, footprint_ref) from a symbol, resolving (extends ...) inheritance."""
    seen = _seen or set()
    if name in seen or name not in sym_map:
        return [], ""
    seen.add(name)
    node = sym_map[name]

    # Footprint property on this symbol
    fp_ref = ""
    for prop in sx.find_all(node, "property"):
        if len(prop) >= 3 and str(prop[1]) == "Footprint" and str(prop[2]).strip():
            fp_ref = str(prop[2]).strip()
            break

    # Resolve parent if (extends "PARENT")
    pins_by_num: dict[str, Pin] = {}
    ext = sx.find(node, "extends")
    if ext and len(ext) >= 2:
        parent_pins, parent_fp = _extract_symbol_data(sym_map, str(ext[1]), seen)
        for p in parent_pins:
            pins_by_num[p.num] = p
        if not fp_ref:
            fp_ref = parent_fp

    # Collect pins from direct children and unit sub-symbols (e.g. NAME_1_1)
    containers = [node] + sx.find_all(node, "symbol")
    for cont in containers:
        for pin_node in sx.find_all(cont, "pin"):
            if len(pin_node) < 3:
                continue
            ktype = str(pin_node[1])
            nm_node = sx.find(pin_node, "name")
            num_node = sx.find(pin_node, "number")
            if not num_node or len(num_node) < 2:
                continue
            pnum = str(num_node[1]).strip()
            pname = str(nm_node[1]).strip() if nm_node and len(nm_node) >= 2 else pnum
            if pname in ("~", ""):
                pname = pnum
            pins_by_num[pnum] = Pin(num=pnum, name=pname, type=_normalize_pin_type(ktype, pname))

    # Sort numerically when possible
    def _sort_key(p: Pin):
        return (0, int(p.num), "") if p.num.isdigit() else (1, 0, p.num)

    return sorted(pins_by_num.values(), key=_sort_key), fp_ref


def _mpn_candidates(mpn: str) -> list[str]:
    """Generate normalized symbol lookup candidates from an MPN (stripping packaging suffixes)."""
    s = mpn.strip().upper()
    if not s:
        return []
    cands = [s]
    # Strip common reel/packaging suffixes (Diodes TRG1, TI R/DR/DBVR/DGKR, ON LT1G/T1G, etc.)
    for pat in (
        r"(TRG\d+|TR\d*|T\d+G|LT\d+G|RKG|RGTR|DBVR|DGKR|DR|NSR|PBF|G4|-ND|-TR|-T\d*)$",
        r"[-_](REEL|TAPE|CT|TR)$",
    ):
        trimmed = re.sub(pat, "", s)
        if trimmed and trimmed not in cands:
            cands.append(trimmed)
    # Strip trailing 1-3 alpha package/grade suffix letters (e.g., AP2112K-3.3TRG1 -> AP2112K-3.3)
    m = re.match(r"^([A-Z0-9]+-[0-9.]+)[A-Z0-9]+$", s)
    if m and m.group(1) not in cands:
        cands.append(m.group(1))
    # Also try base before last hyphen if long
    if "-" in s:
        base = s.rsplit("-", 1)[0]
        if len(base) >= 4 and base not in cands:
            cands.append(base)
    return cands


def lookup_kicad_symbol(mpn: str) -> tuple[list[Pin], str]:
    """Look up (pins, footprint_ref) for an MPN in installed KiCad .kicad_sym libraries."""
    idx = _symbol_file_index(str(config.KICAD_SYMBOLS_DIR))
    if not idx or not mpn:
        return [], ""
    for cand in _mpn_candidates(mpn):
        hit = idx.get(cand)
        if hit is not None:
            sym_path, exact_name = hit
            sym_map = _parse_symbol_file(str(sym_path))
            pins, fp = _extract_symbol_data(sym_map, exact_name)
            if pins:
                return pins, fp
    return [], ""


def infer_footprint_ref(category: str = "", package: str = "", description: str = "") -> str:
    """Map JLC/Octopart package & category strings to a canonical KiCad Library:Footprint ref."""
    cat = (category or "").lower()
    pkg = (package or "").upper().strip()
    desc = (description or "").upper()
    blob = f"{pkg} {desc}"

    # Direct alias hit
    if pkg in FOOTPRINT_ALIASES:
        return FOOTPRINT_ALIASES[pkg]

    # SMD passives / LEDs
    for size in ("0402", "0603", "0805", "1206"):
        if re.search(rf"\b{size}\b", blob) or pkg.endswith(size):
            if "led" in cat or "LED" in desc:
                return FOOTPRINT_ALIASES.get(f"LED_{size}", FOOTPRINT_ALIASES["LED_0603"])
            if "resistor" in cat or "OHM" in desc or "Ω" in description:
                return FOOTPRINT_ALIASES[f"R_{size}"]
            if "capacitor" in cat or "MLCC" in desc or "UF" in desc or "NF" in desc or "PF" in desc:
                return FOOTPRINT_ALIASES[f"C_{size}"]
            if "inductor" in cat or "ferrite" in cat or "BEAD" in desc:
                return FOOTPRINT_ALIASES.get(f"L_{size}", FOOTPRINT_ALIASES["L_0603"])

    # Diodes
    if "SOD-123" in blob or "SOD123" in blob:
        return FOOTPRINT_ALIASES["D_SOD-123"]
    if "SOD-323" in blob or "SOD323" in blob:
        return FOOTPRINT_ALIASES["D_SOD-323"]

    # USB-C connectors
    if "USB" in blob and ("TYPE-C" in blob or "USB-C" in blob or "USB C" in blob):
        if "16P" in blob or "16 POS" in blob or "16PIN" in blob or "31-M-12" in blob:
            return FOOTPRINT_ALIASES["USBC-16P"]
        return FOOTPRINT_ALIASES["USBC-6P"]

    # Pin headers
    if "header" in cat or "PIN HEADER" in desc or "HEADER" in desc:
        m = re.search(r"1\s*[X×]\s*(\d+)|(\d+)\s*(?:P|POS|POSITION)\b", blob)
        if m:
            cnt = int(m.group(1) or m.group(2))
            key = f"HDR-1x{cnt:02d}"
            if key in FOOTPRINT_ALIASES:
                return FOOTPRINT_ALIASES[key]
            return f"Connector_PinHeader_2.54mm:PinHeader_1x{cnt:02d}_P2.54mm_Vertical"

    # SOT packages (note LCSC uses SOT-25 / SOT-25-5 for SOT-23-5)
    if any(t in blob for t in ("SOT-23-5", "SOT23-5", "SOT-25", "SC-70-5")):
        return FOOTPRINT_ALIASES["SOT-23-5"]
    if any(t in blob for t in ("SOT-23-6", "SOT23-6", "SOT-26")):
        return FOOTPRINT_ALIASES["SOT-23-6"]
    if "SOT-223" in blob:
        return FOOTPRINT_ALIASES["SOT-223"]
    if any(t in blob for t in ("SOT-23", "SOT23")):
        return FOOTPRINT_ALIASES["SOT-23"]

    # IC packages
    if "LGA-8" in blob or "LGA8" in blob:
        return FOOTPRINT_ALIASES["LGA-8"]
    if any(t in blob for t in ("SOIC-8", "SOP-8", "SO-8")):
        return FOOTPRINT_ALIASES["SOIC-8"]
    if any(t in blob for t in ("SOIC-16", "SOP-16", "SO-16")):
        return FOOTPRINT_ALIASES["SOIC-16"]
    if "TSSOP-16" in blob:
        return FOOTPRINT_ALIASES["TSSOP-16"]
    if "QFN-16" in blob:
        return FOOTPRINT_ALIASES["QFN-16"]
    if "QFN-24" in blob:
        return FOOTPRINT_ALIASES["QFN-24"]
    if "QFN-32" in blob:
        return FOOTPRINT_ALIASES["QFN-32"]

    return ""


def _infer_standard_pins(category: str, package: str, description: str, footprint_ref: str) -> list[Pin]:
    """Deterministic pin tables for standard passives, LEDs, diodes, headers, and MOSFETs."""
    cat = (category or "").lower()
    desc = (description or "").upper()
    fp = (footprint_ref or "").upper()

    # 2-pin passives
    if any(k in cat for k in ("resistor", "capacitor", "inductor", "ferrite", "fuse")) or any(
        fp.startswith(p) for p in ("RESISTOR_", "CAPACITOR_", "INDUCTOR_", "R_", "C_", "L_")
    ):
        return [Pin(num="1", name="1", type="passive"), Pin(num="2", name="2", type="passive")]

    # LEDs & Diodes (KiCad convention: pin 1 = K cathode, pin 2 = A anode)
    if any(k in cat for k in ("led", "diode")) or fp.startswith(("LED_", "DIODE_")):
        return [Pin(num="1", name="K", type="passive"), Pin(num="2", name="A", type="passive")]

    # USB-C 6-pin power-only
    if "USB4125" in fp or "USBC-6P" in fp or ("USB" in desc and ("6P" in desc or "6 POS" in desc)):
        return [
            Pin(num="A5", name="CC1", type="bidir"),
            Pin(num="B5", name="CC2", type="bidir"),
            Pin(num="A9", name="VBUS", type="power_out"),
            Pin(num="B9", name="VBUS", type="power_out"),
            Pin(num="A12", name="GND", type="ground"),
            Pin(num="B12", name="GND", type="ground"),
            Pin(num="S1", name="SHIELD", type="ground"),
        ]

    # Pin headers 1xN
    m = re.search(r"PINHEADER_1X(\d+)|HDR-1X(\d+)", fp)
    if m:
        cnt = int(m.group(1) or m.group(2))
        return [Pin(num=str(i), name=str(i), type="passive") for i in range(1, cnt + 1)]

    # SOT-23 MOSFET
    if ("mosfet" in cat or "MOSFET" in desc) and "SOT-23" in fp:
        return [Pin(num="1", name="G", type="input"), Pin(num="2", name="S", type="passive"), Pin(num="3", name="D", type="passive")]

    # Generic SOT-23-5 LDO (standard pinout: 1=VIN, 2=GND, 3=EN, 4=NC/BP, 5=VOUT)
    if ("regulator" in cat or "ldo" in cat or "LDO" in desc) and "SOT-23-5" in fp:
        return [
            Pin(num="1", name="VIN", type="power_in"),
            Pin(num="2", name="GND", type="ground"),
            Pin(num="3", name="EN", type="input"),
            Pin(num="4", name="NC", type="nc"),
            Pin(num="5", name="VOUT", type="power_out"),
        ]

    return []


def enrich_part(part: Part, *, use_easyeda: bool = False) -> Part:
    """Attach verified pins and KiCad footprint_ref from KiCad libraries, EasyEDA, or standard rules."""
    pins = list(part.pins)
    fp_ref = part.footprint_ref or ""

    # 1. KiCad symbol library lookup (gives real datasheet pins + canonical Footprint)
    if not pins:
        sym_pins, sym_fp = lookup_kicad_symbol(part.mpn)
        if sym_pins:
            pins = sym_pins
        if not fp_ref and sym_fp:
            fp_ref = sym_fp

    # 2. Optional easyeda2kicad lookup by LCSC ID
    if (not pins or not fp_ref) and use_easyeda and part.lcsc_id:
        fp_path, sym_path = _try_easyeda2kicad(part.lcsc_id)
        if not fp_ref and fp_path is not None:
            fp_ref = str(fp_path)
        if not pins and sym_path is not None:
            try:
                sym_map = _parse_symbol_file(str(sym_path))
                if sym_map:
                    first_name = next(iter(sym_map))
                    easy_pins, easy_fp = _extract_symbol_data(sym_map, first_name)
                    if easy_pins:
                        pins = easy_pins
                    if not fp_ref and easy_fp:
                        fp_ref = easy_fp
            except Exception as exc:
                logger.debug("Failed to parse easyeda symbol %s: %s", sym_path, exc)

    # 3. Footprint inference from package/category/description
    if not fp_ref:
        fp_ref = infer_footprint_ref(part.category, part.package, part.description)

    # 4. Deterministic pin inference for passives, LEDs, headers, standard discretes
    if not pins:
        pins = _infer_standard_pins(part.category, part.package, part.description, fp_ref)

    if pins != part.pins or fp_ref != part.footprint_ref:
        return part.model_copy(update={"pins": pins, "footprint_ref": fp_ref})
    return part
