"""KiCad 9 .kicad_pcb I/O: .kicad_mod footprint inlining, scraped-board ingest with zone/outline
preservation, KiCad 9 (format 20241229) board & .kicad_pro writer, and SVG preview."""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from . import libraries, sexpr as sx
from .sexpr import Sym

CLEAR = 0.25  # courtyard margin around pad extents (used when no F.CrtYd is in the footprint)


@dataclass
class Pad:
    num: str
    x: float
    y: float
    w: float
    h: float
    kind: str = "smd"      # smd | thru_hole | np_thru_hole
    shape: str = "rect"
    drill: float = 0.0
    net: str | None = None
    rot: float = 0.0       # local pad rotation in degrees
    drill_y: float = 0.0   # oval drill second axis (>0 => oval drill)
    roundrect_rratio: float = 0.25
    layers: tuple[str, ...] | None = None


@dataclass
class Comp:
    ref: str
    value: str
    lib: str
    pads: list[Pad]
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0
    node: list | None = None      # original sexpr (scraped boards)
    mpn: str = ""
    graphics: list[list] = field(default_factory=list)
    courtyard_bbox: tuple[float, float, float, float] | None = None
    attr: str = ""

    def local_bbox(self) -> tuple[float, float, float, float]:
        xs: list[float] = []
        ys: list[float] = []
        for p in self.pads:
            hw, hh = p.w / 2.0, p.h / 2.0
            if p.rot % 360:
                a = math.radians(p.rot)
                ca, sa = abs(math.cos(a)), abs(math.sin(a))
                ehw = hw * ca + hh * sa
                ehh = hw * sa + hh * ca
            else:
                ehw, ehh = hw, hh
            xs.extend((p.x - ehw - CLEAR, p.x + ehw + CLEAR))
            ys.extend((p.y - ehh - CLEAR, p.y + ehh + CLEAR))
        if self.courtyard_bbox is not None:
            cx0, cy0, cx1, cy1 = self.courtyard_bbox
            xs.extend((cx0, cx1))
            ys.extend((cy0, cy1))
        if not xs:
            return (-1.0 - CLEAR, -1.0 - CLEAR, 1.0 + CLEAR, 1.0 + CLEAR)
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class BoardOutline:
    """Exact 2D board outline extracted from Edge.Cuts (supports non-rectangular boards & holes)."""
    bbox: tuple[float, float, float, float]       # (x0, y0, w, h)
    outer: list[tuple[float, float]]              # closed CCW/CW outer polygon vertices
    holes: list[list[tuple[float, float]]] = field(default_factory=list)


def rotate(px: float, py: float, rot: float) -> tuple[float, float]:
    """KiCad frame (y down), positive angle = counter-clockwise on screen."""
    a = math.radians(rot)
    return px * math.cos(a) + py * math.sin(a), -px * math.sin(a) + py * math.cos(a)


# ---------- footprints (.kicad_mod library / EasyEDA first, procedural fallback) ----------
def _two(w=0.8, h=0.95, dx=0.825) -> list[Pad]:
    return [Pad("1", -dx, 0, w, h), Pad("2", dx, 0, w, h)]


def _procedural_pads(ref: str) -> list[Pad]:
    """Fallback procedural footprints when KiCad footprint libraries are not installed."""
    key = ref.split(":")[-1]
    if ref in ("R_0402", "C_0402") or "0402" in key:
        return _two(w=0.56, h=0.62, dx=0.48)
    if ref in ("R_0603", "C_0603", "LED_0603", "L_0603") or "0603" in key:
        return _two(w=0.8, h=0.95, dx=0.825)
    if ref in ("R_0805", "C_0805", "LED_0805", "L_0805") or "0805" in key:
        return _two(w=1.0, h=1.35, dx=0.95)
    if ref in ("R_1206", "C_1206") or "1206" in key:
        return _two(w=1.15, h=1.8, dx=1.475)
    if ref == "SOT-23-5" or "SOT-23-5" in key:
        return [
            Pad("1", -1.1375, -0.95, 1.325, 0.6, shape="roundrect"),
            Pad("2", -1.1375, 0.0, 1.325, 0.6, shape="roundrect"),
            Pad("3", -1.1375, 0.95, 1.325, 0.6, shape="roundrect"),
            Pad("4", 1.1375, 0.95, 1.325, 0.6, shape="roundrect"),
            Pad("5", 1.1375, -0.95, 1.325, 0.6, shape="roundrect"),
        ]
    if ref == "SOT-23-6" or "SOT-23-6" in key:
        return [
            Pad("1", -1.1375, -0.95, 1.325, 0.6, shape="roundrect"),
            Pad("2", -1.1375, 0.0, 1.325, 0.6, shape="roundrect"),
            Pad("3", -1.1375, 0.95, 1.325, 0.6, shape="roundrect"),
            Pad("4", 1.1375, 0.95, 1.325, 0.6, shape="roundrect"),
            Pad("5", 1.1375, 0.0, 1.325, 0.6, shape="roundrect"),
            Pad("6", 1.1375, -0.95, 1.325, 0.6, shape="roundrect"),
        ]
    if ref in ("SOT-23", "SOT-23-3") or key in ("SOT-23", "SOT-23-3"):
        return [
            Pad("1", -0.9375, -0.95, 1.475, 0.6, shape="roundrect"),
            Pad("2", -0.9375, 0.95, 1.475, 0.6, shape="roundrect"),
            Pad("3", 0.9375, 0.0, 1.475, 0.6, shape="roundrect"),
        ]
    if ref == "LGA-8" or "LGA-8" in key:
        top = [Pad(str(i + 1), -0.975 + 0.65 * i, -1.025, 0.5, 0.35, rot=90.0) for i in range(4)]
        bot = [Pad(str(5 + i), 0.975 - 0.65 * i, 1.025, 0.5, 0.35, rot=90.0) for i in range(4)]
        return top + bot
    if ref == "SOIC-8" or "SOIC-8" in key:
        left = [Pad(str(i + 1), -2.475, -1.905 + 1.27 * i, 1.95, 0.6, shape="roundrect") for i in range(4)]
        right = [Pad(str(5 + i), 2.475, 1.905 - 1.27 * i, 1.95, 0.6, shape="roundrect") for i in range(4)]
        return left + right
    if ref == "USBC-4P":
        return [Pad(str(i + 1), -3 + 2 * i, 0, 1.0, 2.0) for i in range(4)]
    if ref == "USBC-6P" or "USB4125" in key:
        return [
            Pad("A5", -0.5, -3.08, 0.7, 1.2, shape="roundrect"),
            Pad("B5", 0.5, -3.08, 0.7, 1.2, shape="roundrect", rot=180.0),
            Pad("A9", 1.52, -3.08, 0.76, 1.2, shape="roundrect", rot=180.0),
            Pad("B9", -1.52, -3.08, 0.76, 1.2, shape="roundrect"),
            Pad("A12", 2.75, -3.08, 0.8, 1.2, shape="roundrect", rot=180.0),
            Pad("B12", -2.75, -3.08, 0.8, 1.2, shape="roundrect"),
            Pad("S1", -4.32, -3.0, 1.1, 1.7, "thru_hole", "oval", 0.6, drill_y=1.2),
            Pad("S1", -4.32, 0.8, 1.1, 1.7, "thru_hole", "oval", 0.6, drill_y=1.2),
            Pad("S1", 4.32, -3.0, 1.1, 1.7, "thru_hole", "oval", 0.6, drill_y=1.2),
            Pad("S1", 4.32, 0.8, 1.1, 1.7, "thru_hole", "oval", 0.6, drill_y=1.2),
        ]
    if ref.startswith("HDR-1x") or "PinHeader_1x" in key:
        import re as _re
        m = _re.search(r"1x(\d+)", key, _re.I)
        n = int(m.group(1)) if m else 4
        return [Pad(str(i + 1), 0.0, 2.54 * i, 1.7, 1.7, "thru_hole", "rect" if i == 0 else "oval", 1.0) for i in range(n)]
    raise KeyError(f"no footprint for {ref!r}: add a .kicad_mod to KICAD_FOOTPRINTS_DIR or FOOTPRINT_ALIASES")


def footprint_pads(ref: str, lcsc_id: str = "") -> list[Pad]:
    """Return Pad list for `ref`: inlines real .kicad_mod from KiCad/EasyEDA library when available."""
    fp_def = libraries.load_footprint(ref, lcsc_id=lcsc_id)
    if fp_def is not None and fp_def.pads:
        return [
            Pad(
                num=p["num"], x=p["x"], y=p["y"], w=p["w"], h=p["h"],
                kind=p["kind"], shape=p["shape"], drill=p["drill"],
                rot=p["rot"], drill_y=p["drill_y"],
                roundrect_rratio=p["roundrect_rratio"], layers=p["layers"],
            )
            for p in fp_def.pads
        ]
    return _procedural_pads(ref)


def make_comp(ref: str, value: str, footprint_ref: str, mpn: str = "", lcsc_id: str = "") -> Comp:
    """Build a Comp with real inlined .kicad_mod pads, silkscreen/fab/courtyard graphics, and bbox."""
    fp_def = libraries.load_footprint(footprint_ref, lcsc_id=lcsc_id)
    if fp_def is not None and fp_def.pads:
        pads = [
            Pad(
                num=p["num"], x=p["x"], y=p["y"], w=p["w"], h=p["h"],
                kind=p["kind"], shape=p["shape"], drill=p["drill"],
                rot=p["rot"], drill_y=p["drill_y"],
                roundrect_rratio=p["roundrect_rratio"], layers=p["layers"],
            )
            for p in fp_def.pads
        ]
        return Comp(
            ref=ref, value=value, lib=fp_def.lib_ref, pads=pads, mpn=mpn,
            graphics=fp_def.graphics, courtyard_bbox=fp_def.courtyard_bbox, attr=fp_def.attr,
        )
    pads = _procedural_pads(footprint_ref)
    return Comp(ref=ref, value=value, lib=footprint_ref, pads=pads, mpn=mpn)


# ---------- scraped board ingest & Edge.Cuts polygon extraction ----------
def _xy(n) -> tuple[float, float, float]:
    v = sx.nums(n) if n else []
    return tuple((v + [0.0, 0.0, 0.0])[:3])


def _discretize_arc(start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float], segs: int = 12) -> list[tuple[float, float]]:
    """Sample points along circular arc defined by KiCad (start, mid, end)."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [start, mid, end]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)

    def _norm(ang: float) -> float:
        return ang % (2.0 * math.pi)

    # Choose sweep direction (CCW vs CW) that passes through `am`
    ccw_total = _norm(a1 - a0)
    ccw_mid = _norm(am - a0)
    sweep = ccw_total if ccw_mid <= ccw_total else ccw_total - 2.0 * math.pi
    return [(ux + r * math.cos(a0 + sweep * i / segs), uy + r * math.sin(a0 + sweep * i / segs)) for i in range(segs + 1)]


def _polygon_area(poly: list[tuple[float, float]]) -> float:
    if len(poly) < 3:
        return 0.0
    s = 0.0
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _chain_segments(chains: list[list[tuple[float, float]]], tol: float = 0.05) -> list[list[tuple[float, float]]]:
    """Assemble open polylines/segments on Edge.Cuts into closed polygon loops."""
    remaining = [list(c) for c in chains if len(c) >= 2]
    loops: list[list[tuple[float, float]]] = []
    while remaining:
        cur = remaining.pop(0)
        changed = True
        while changed and math.dist(cur[0], cur[-1]) > tol:
            changed = False
            for idx, seg in enumerate(remaining):
                if math.dist(cur[-1], seg[0]) <= tol:
                    cur.extend(seg[1:])
                    remaining.pop(idx)
                    changed = True
                    break
                if math.dist(cur[-1], seg[-1]) <= tol:
                    cur.extend(reversed(seg[:-1]))
                    remaining.pop(idx)
                    changed = True
                    break
                if math.dist(cur[0], seg[-1]) <= tol:
                    cur = seg[:-1] + cur
                    remaining.pop(idx)
                    changed = True
                    break
                if math.dist(cur[0], seg[0]) <= tol:
                    cur = list(reversed(seg[1:])) + cur
                    remaining.pop(idx)
                    changed = True
                    break
        if len(cur) >= 3:
            if math.dist(cur[0], cur[-1]) <= tol:
                cur = cur[:-1]
            if len(cur) >= 3:
                loops.append(cur)
    return loops


def parse_board_outline(tree: list) -> BoardOutline | None:
    """Reconstruct exact 2D board outline (outer polygon + cutout holes + bbox) from Edge.Cuts."""
    closed_polys: list[list[tuple[float, float]]] = []
    open_chains: list[list[tuple[float, float]]] = []
    xs: list[float] = []
    ys: list[float] = []

    for n in tree:
        if not (isinstance(n, list) and sx.head(n).startswith("gr_")):
            continue
        ly = sx.find(n, "layer")
        if not (ly and str(ly[1]) == "Edge.Cuts"):
            continue
        kind = sx.head(n)
        if kind == "gr_line":
            s, e = sx.find(n, "start"), sx.find(n, "end")
            if s and e:
                p0, p1 = _xy(s)[:2], _xy(e)[:2]
                open_chains.append([p0, p1])
                xs.extend((p0[0], p1[0])); ys.extend((p0[1], p1[1]))
        elif kind == "gr_arc":
            s, m, e = sx.find(n, "start"), sx.find(n, "mid"), sx.find(n, "end")
            if s and m and e:
                pts = _discretize_arc(_xy(s)[:2], _xy(m)[:2], _xy(e)[:2])
                open_chains.append(pts)
                for px, py in pts:
                    xs.append(px); ys.append(py)
        elif kind == "gr_rect":
            s, e = sx.find(n, "start"), sx.find(n, "end")
            if s and e:
                x0, y0 = _xy(s)[:2]
                x1, y1 = _xy(e)[:2]
                poly = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
                closed_polys.append(poly)
                xs.extend((x0, x1)); ys.extend((y0, y1))
        elif kind == "gr_circle":
            c_n, e_n = sx.find(n, "center"), sx.find(n, "end")
            if c_n and e_n:
                cx, cy = _xy(c_n)[:2]
                ex, ey = _xy(e_n)[:2]
                r = math.hypot(ex - cx, ey - cy)
                pts = [(cx + r * math.cos(2 * math.pi * i / 32), cy + r * math.sin(2 * math.pi * i / 32)) for i in range(32)]
                closed_polys.append(pts)
                xs.extend((cx - r, cx + r)); ys.extend((cy - r, cy + r))
        elif kind == "gr_poly":
            pts_n = sx.find(n, "pts")
            poly = []
            for q in (pts_n or [])[1:]:
                if isinstance(q, list) and len(q) >= 3:
                    px, py = float(q[1]), float(q[2])
                    poly.append((px, py))
                    xs.append(px); ys.append(py)
            if len(poly) >= 3:
                closed_polys.append(poly)

    if len(xs) < 2 or len(ys) < 2:
        return None
    bbox = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    loops = closed_polys + _chain_segments(open_chains)
    if not loops:
        x0, y0, w, h = bbox
        return BoardOutline(bbox=bbox, outer=[(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)], holes=[])
    loops.sort(key=_polygon_area, reverse=True)
    return BoardOutline(bbox=bbox, outer=loops[0], holes=loops[1:])


def count_copper_layers(tree: list) -> int:
    """Count declared copper layers (signal / power / mixed) in a parsed .kicad_pcb tree."""
    ly_block = sx.find(tree, "layers")
    if not ly_block:
        return 2
    cu = 0
    for item in ly_block[1:]:
        if isinstance(item, list) and len(item) >= 3:
            name = str(item[1])
            kind = str(item[2])
            if name.endswith(".Cu") or kind in ("signal", "power", "mixed"):
                cu += 1
    return cu or 2


def count_routable_nets(nets: dict[str, list]) -> int:
    """Count routable nets (nets with >= 2 connected pads)."""
    return sum(1 for pins in nets.values() if len(pins) >= 2)


def parse_board(text: str):
    """-> (tree, comps, nets{net:[(ref,pin)]}, outline_bbox(x0,y0,w,h)|None)"""
    tree = sx.parse(text)
    netnames = {}
    for n in sx.find_all(tree, "net"):
        if len(n) >= 3:
            netnames[str(n[1])] = n[2]
    comps, nets = [], {}
    for fp in sx.find_all(tree, "footprint"):
        ref = value = ""
        for p in sx.find_all(fp, "property"):
            if p[1] == "Reference":
                ref = p[2]
            elif p[1] == "Value":
                value = p[2]
        for t in sx.find_all(fp, "fp_text"):  # older files
            if t[1] == "reference":
                ref = t[2]
            elif t[1] == "value":
                value = t[2]
        if not ref:
            continue
        x, y, rot = _xy(sx.find(fp, "at"))
        pads = []
        for pd in sx.find_all(fp, "pad"):
            at, size, dr, net = sx.find(pd, "at"), sx.find(pd, "size"), sx.find(pd, "drill"), sx.find(pd, "net")
            px, py, pang = _xy(at)
            local_rot = (pang - rot) % 360.0
            w, h = (sx.nums(size) + [1, 1])[:2] if size else (1, 1)
            nname = None
            if net:
                nname = net[2] if len(net) >= 3 else (netnames.get(str(net[1]), str(net[1])) if len(net) == 2 else None)
            pads.append(Pad(
                str(pd[1]), px, py, w, h, str(pd[2]), str(pd[3]),
                (sx.nums(dr) or [0])[0] if dr else 0.0, nname or None, rot=local_rot,
            ))
            if nname:
                nets.setdefault(nname, []).append((ref, str(pd[1])))
        crtyd = libraries._extract_courtyard_bbox([c for c in fp[1:] if isinstance(c, list) and sx.head(c).startswith("fp_")])
        comps.append(Comp(ref, value, str(fp[1]), pads, x, y, rot, node=fp, courtyard_bbox=crtyd))
    ol = parse_board_outline(tree)
    outline = ol.bbox if ol else None
    return tree, comps, nets, outline


def apply_placement_scraped(
    tree,
    comps: list[Comp],
    placements: dict[str, tuple[float, float, float]],
    *,
    keep_zones: bool = True,
) -> str:
    """Strip routed copper (segments/vias/arcs), preserve zone/pour definitions (stripping only stale
    filled_polygon pours when keep_zones=True), and rewrite footprint positions."""
    new_tree = []
    for n in tree:
        if isinstance(n, list):
            hd = sx.head(n)
            if hd in ("segment", "via", "arc"):
                continue
            if hd == "zone":
                if not keep_zones:
                    continue
                # Preserve the zone (net, layer, polygon outline, keepout/clearance/thermal settings)
                # and drop only stale poured copper geometry so KiCad/router refills cleanly.
                cleaned_zone = [
                    child for child in n
                    if not (isinstance(child, list) and sx.head(child) in ("filled_polygon", "fill_segments"))
                ]
                new_tree.append(cleaned_zone)
                continue
        new_tree.append(n)
    tree[:] = new_tree

    by_ref = {c.ref: c for c in comps}
    for ref, (x, y, rot) in placements.items():
        c = by_ref[ref]
        fp = c.node
        old = c.rot
        at = sx.find(fp, "at")
        at[:] = [Sym("at"), float(x), float(y)] + ([float(rot)] if rot else [])
        for pd in sx.find_all(fp, "pad"):
            pat = sx.find(pd, "at")
            if pat is None:
                continue
            nums = sx.nums(pat)
            ang = ((nums[2] if len(nums) > 2 else old) - old + rot) % 360
            pat[:] = [Sym("at"), float(nums[0]), float(nums[1])] + ([float(ang)] if ang else [])
    return sx.dump(tree) + "\n"


# ---------- KiCad 9 (format 20241229) unrouted board writer ----------
# Layer IDs match KiCad 9.0 include/layer_ids.h (PCB_LAYER_ID)
LAYERS = '''(layers
    (0 "F.Cu" signal)
    (2 "B.Cu" signal)
    (9 "F.Adhes" user "F.Adhesive")
    (11 "B.Adhes" user "B.Adhesive")
    (13 "F.Paste" user)
    (15 "B.Paste" user)
    (5 "F.SilkS" user "F.Silkscreen")
    (7 "B.SilkS" user "B.Silkscreen")
    (1 "F.Mask" user)
    (3 "B.Mask" user)
    (17 "Dwgs.User" user "User.Drawings")
    (19 "Cmts.User" user "User.Comments")
    (25 "Edge.Cuts" user)
    (27 "Margin" user)
    (31 "F.CrtYd" user "F.Courtyard")
    (29 "B.CrtYd" user "B.Courtyard")
    (35 "F.Fab" user "F.Fabrication")
    (33 "B.Fab" user "B.Fabrication")
  )'''


def _u() -> str:
    return str(uuid.uuid4())


def write_unrouted_kicad_pcb(comps: list[Comp], placements, nets: dict[str, list[tuple[str, str]]], outline) -> str:
    """Write a KiCad 9.0 (version 20241229) unrouted .kicad_pcb file.
    comps carry pads (from .kicad_mod or procedural); nets maps net->[(ref,pin)]. outline=(x0,y0,w,h) in mm."""
    ids = {n: i + 1 for i, n in enumerate(sorted(nets))}
    pin_net = {(r, p): n for n, pins in nets.items() for r, p in pins}
    x0, y0, w, h = outline
    out = [
        '(kicad_pcb',
        '  (version 20241229)',
        '  (generator "coupled-backend")',
        '  (generator_version "0.1")',
        '  (general (thickness 1.6) (legacy_teardrops no))',
        '  (paper "A4")',
        '  ' + LAYERS,
        '  (setup (pad_to_mask_clearance 0) (allow_soldermask_bridges_in_footprints no)'
        ' (pcbplotparams (layerselection 0x00010fc_ffffffff) (plot_on_all_layers_selection 0x0000000_00000000)'
        ' (disableapertmacros no) (usegerberextensions no) (usegerberattributes yes) (usegerberadvancedattributes yes)'
        ' (creategerberjobfile yes) (svgprecision 4) (outputdirectory "")))',
        '  (net 0 "")',
    ]
    out += [f'  (net {i} "{n}")' for n, i in ids.items()]
    for c in comps:
        x, y, rot = placements[c.ref]
        rot = rot % 360
        r = f' {rot:g}' if rot else ""
        fp_name = c.lib if ":" in c.lib else f"coupled-backend:{c.lib}"
        bx0, by0, bx1, by1 = c.local_bbox()
        # Keep silkscreen Reference inside Edge.Cuts (flip to opposite side of footprint if too close to board edge)
        ref_ly = by0 - 0.8
        rdx, rdy = rotate(0.0, ref_ly, rot)
        if not (x0 + 1.2 <= x + rdx <= x0 + w - 1.2 and y0 + 1.0 <= y + rdy <= y0 + h - 1.0):
            alt_ly = by1 + 0.8
            adx, ady = rotate(0.0, alt_ly, rot)
            if x0 + 1.2 <= x + adx <= x0 + w - 1.2 and y0 + 1.0 <= y + ady <= y0 + h - 1.0:
                ref_ly = alt_ly
        attr = c.attr or ("through_hole" if any(p.kind == "thru_hole" for p in c.pads) else "smd")
        out.append(f'  (footprint "{fp_name}" (layer "F.Cu") (uuid "{_u()}") (at {x:.4f} {y:.4f}{r})')
        out.append(f'    (property "Reference" "{c.ref}" (at 0 {ref_ly:.3f} {rot:g}) (layer "F.SilkS") (uuid "{_u()}") (effects (font (size 0.7 0.7) (thickness 0.11))))')
        out.append(f'    (property "Value" "{c.value}" (at 0 {by1 + 0.8:.3f} {rot:g}) (layer "F.Fab") (uuid "{_u()}") (effects (font (size 0.8 0.8) (thickness 0.12))))')
        out.append(f'    (property "Footprint" "{fp_name}" (at 0 0 0) (unlocked yes) (layer "F.Fab") (hide yes) (uuid "{_u()}") (effects (font (size 1 1) (thickness 0.15))))')
        out.append(f'    (property "Datasheet" "" (at 0 0 0) (unlocked yes) (layer "F.Fab") (hide yes) (uuid "{_u()}") (effects (font (size 1 1) (thickness 0.15))))')
        out.append(f'    (attr {attr})')
        if c.graphics:
            out.extend(libraries.clone_graphics_with_uuids(c.graphics))
            if c.courtyard_bbox is None:
                out.append(f'    (fp_rect (start {bx0:.3f} {by0:.3f}) (end {bx1:.3f} {by1:.3f}) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd") (uuid "{_u()}"))')
        else:
            out.append(f'    (fp_rect (start {bx0:.3f} {by0:.3f}) (end {bx1:.3f} {by1:.3f}) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd") (uuid "{_u()}"))')
        for p in c.pads:
            net = None if p.kind == "np_thru_hole" else pin_net.get((c.ref, p.num))
            nstr = f' (net {ids[net]} "{net}")' if net else ""
            pad_ang = (p.rot + rot) % 360
            ang = f" {pad_ang:g}" if pad_ang else ""
            rr = f" (roundrect_rratio {p.roundrect_rratio:g})" if p.shape == "roundrect" else ""
            if p.kind in ("thru_hole", "np_thru_hole"):
                if p.drill_y > 0:
                    dr = f" (drill oval {p.drill:g} {p.drill_y:g})"
                elif p.drill > 0:
                    dr = f" (drill {p.drill:g})"
                else:
                    dr = ""
                default_layers = ("*.Cu", "*.Mask") if p.kind == "thru_hole" else ("*.Cu", "*.Mask")
                ly_str = " ".join(f'"{ly}"' for ly in (p.layers or default_layers))
                out.append(f'    (pad "{p.num}" {p.kind} {p.shape} (at {p.x:.4f} {p.y:.4f}{ang}) (size {p.w:g} {p.h:g}){dr} (layers {ly_str}){rr}{nstr} (uuid "{_u()}"))')
            else:
                ly_str = " ".join(f'"{ly}"' for ly in (p.layers or ("F.Cu", "F.Paste", "F.Mask")))
                out.append(f'    (pad "{p.num}" smd {p.shape} (at {p.x:.4f} {p.y:.4f}{ang}) (size {p.w:g} {p.h:g}) (layers {ly_str}){rr}{nstr} (uuid "{_u()}"))')
        out.append('    (embedded_fonts no)')
        out.append('  )')
    for a, b in (((x0, y0), (x0 + w, y0)), ((x0 + w, y0), (x0 + w, y0 + h)), ((x0 + w, y0 + h), (x0, y0 + h)), ((x0, y0 + h), (x0, y0))):
        out.append(f'  (gr_line (start {a[0]:.3f} {a[1]:.3f}) (end {b[0]:.3f} {b[1]:.3f}) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid "{_u()}"))')
    out.append('  (embedded_fonts no)')
    out.append(')')
    return "\n".join(out) + "\n"


def kicad_pro(net_classes: dict[str, str], filename: str = "board.kicad_pro") -> dict:
    """KiCad 9 .kicad_pro project file with design rules and net classes (Default, power, signal)."""
    rules = {"Default": (0.2, 0.25), "power": (0.2, 0.5), "signal": (0.2, 0.25)}
    classes = [
        {
            "bus_width": 12,
            "clearance": v[0],
            "diff_pair_gap": 0.25,
            "diff_pair_via_gap": 0.25,
            "diff_pair_width": v[1],
            "line_style": 0,
            "microvia_diameter": 0.3,
            "microvia_drill": 0.1,
            "name": k,
            "pcb_color": "rgba(0, 0, 0, 0.000)",
            "priority": -1 if k == "Default" else 0,
            "schematic_color": "rgba(0, 0, 0, 0.000)",
            "track_width": v[1],
            "via_diameter": 0.6,
            "via_drill": 0.3,
            "wire_width": 6,
        }
        for k, v in rules.items()
    ]
    assignments = {n: c for n, c in net_classes.items() if c in rules and c != "Default"}
    return {
        "board": {
            "design_settings": {
                "meta": {"version": 2},
                "rules": {
                    "max_error": 0.005,
                    "min_clearance": 0.15,
                    "min_connection": 0.0,
                    "min_copper_edge_clearance": 0.2,
                    "min_groove_width": 0.0,
                    "min_hole_clearance": 0.2,
                    "min_hole_to_hole": 0.2,
                    "min_microvia_diameter": 0.2,
                    "min_microvia_drill": 0.1,
                    "min_resolved_spokes": 2,
                    "min_silk_clearance": 0.0,
                    "min_text_height": 0.8,
                    "min_text_thickness": 0.08,
                    "min_through_hole_diameter": 0.3,
                    "min_track_width": 0.15,
                    "min_via_annular_width": 0.1,
                    "min_via_diameter": 0.5,
                    "solder_mask_to_copper_clearance": 0.0,
                },
            }
        },
        "meta": {"filename": filename, "version": 3},
        "net_settings": {
            "classes": classes,
            "meta": {"version": 4},
            "net_colors": None,
            "netclass_assignments": assignments or None,
            "netclass_patterns": [],
        },
    }


# ---------- pad world positions / SVG preview ----------
def board_geometry(text: str):
    tree = sx.parse(text)
    pads, segs, vias = [], [], []
    for fp in sx.find_all(tree, "footprint"):
        x, y, rot = _xy(sx.find(fp, "at"))
        for pd in sx.find_all(fp, "pad"):
            pat, size = sx.find(pd, "at"), sx.find(pd, "size")
            nums = sx.nums(pat)
            lx, ly = nums[0], nums[1]
            ang = nums[2] if len(nums) > 2 else rot
            dx, dy = rotate(lx, ly, rot)
            w, h = (sx.nums(size) + [1, 1])[:2]
            if round(ang) % 180 == 90:
                w, h = h, w
            net = sx.find(pd, "net")
            pads.append((x + dx, y + dy, w, h, str(net[2]) if net and len(net) >= 3 else ""))
    for s in sx.find_all(tree, "segment"):
        a, b, wd = _xy(sx.find(s, "start")), _xy(sx.find(s, "end")), (sx.nums(sx.find(s, "width")) or [0.25])[0]
        ly_n = sx.find(s, "layer")
        ly = str(ly_n[1]) if ly_n and len(ly_n) >= 2 else "F.Cu"
        segs.append((a[0], a[1], b[0], b[1], wd, ly))
    for v in sx.find_all(tree, "via"):
        at = _xy(sx.find(v, "at"))
        sz = (sx.nums(sx.find(v, "size")) or [0.6])[0]
        dr = (sx.nums(sx.find(v, "drill")) or [0.3])[0]
        vias.append((at[0], at[1], sz, dr))
    return tree, pads, segs, vias


def render_svg(text: str, scale: float = 12.0) -> str:
    tree, pads, segs, vias = board_geometry(text)
    xs = [p[0] for p in pads] + [s[0] for s in segs] + [s[2] for s in segs] or [0]
    ys = [p[1] for p in pads] + [s[1] for s in segs] + [s[3] for s in segs] or [0]
    bo = parse_board_outline(tree)
    x0, y0, w, h = bo.bbox if bo else (min(xs) - 3, min(ys) - 3, max(xs) - min(xs) + 6, max(ys) - min(ys) + 6)
    el = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0:.2f} {y0:.2f} {w:.2f} {h:.2f}" width="{w * scale:.0f}" height="{h * scale:.0f}">']
    if bo and len(bo.outer) >= 3:
        pts_str = " ".join(f"{px:.3f},{py:.3f}" for px, py in bo.outer)
        el.append(f'<polygon points="{pts_str}" fill="#0b3d2e" stroke="#e8d44d" stroke-width="0.15"/>')
        for hole in bo.holes:
            h_str = " ".join(f"{px:.3f},{py:.3f}" for px, py in hole)
            el.append(f'<polygon points="{h_str}" fill="#1a1a1a" stroke="#e8d44d" stroke-width="0.15"/>')
    else:
        el.append(f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="#0b3d2e" stroke="#e8d44d" stroke-width="0.15"/>')
    # Back copper first, then front copper, then vias, then pads
    for ly_name, col in (("B.Cu", "#3b82f6"), ("F.Cu", "#ef4444")):
        for a, b, c, d, wd, ly in segs:
            if ly == ly_name:
                el.append(f'<line x1="{a:.3f}" y1="{b:.3f}" x2="{c:.3f}" y2="{d:.3f}" stroke="{col}" stroke-width="{wd}" stroke-linecap="round" opacity="0.92"/>')
    for vx, vy, vsz, vdr in vias:
        el.append(f'<circle cx="{vx:.3f}" cy="{vy:.3f}" r="{vsz / 2:.3f}" fill="#cbd5e1"/>')
        el.append(f'<circle cx="{vx:.3f}" cy="{vy:.3f}" r="{vdr / 2:.3f}" fill="#111214"/>')
    for px, py, pw, ph, _n in pads:
        el.append(f'<rect x="{px - pw / 2:.3f}" y="{py - ph / 2:.3f}" width="{pw:.3f}" height="{ph:.3f}" fill="#d9b44a"/>')
    return "".join(el) + "</svg>"
