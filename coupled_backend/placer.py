"""Stage 3: layout rules + force-directed seed + simulated annealing minimising pad-level HPWL.
Supports exact non-rectangular Edge.Cuts outlines (polygons + cutout holes) and copper-edge margins."""
from __future__ import annotations

import math
import random
import networkx as nx
from .kicad_io import BoardOutline, Comp, rotate

CLUSTER_GAP = 3.0
EDGE_MARGIN = 0.35  # keeps pads/courtyards safely inside Edge.Cuts (>= KiCad 9 0.2mm copper_edge_clearance)


def _point_in_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > py) != (y2 > py)) and (px < (x2 - x1) * (py - y1) / ((y2 - y1) or 1e-12) + x1):
            inside = not inside
    return inside


def _dist_to_seg(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> tuple[float, float, float]:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-12:
        return math.hypot(px - ax, py - ay), ax, ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), cx, cy


def _closest_on_polygon(px: float, py: float, poly: list[tuple[float, float]]) -> tuple[float, float, float]:
    best = (float("inf"), px, py)
    n = len(poly)
    for i in range(n):
        d, cx, cy = _dist_to_seg(px, py, poly[i][0], poly[i][1], poly[(i + 1) % n][0], poly[(i + 1) % n][1])
        if d < best[0]:
            best = (d, cx, cy)
    return best


def _polygon_centroid(poly: list[tuple[float, float]]) -> tuple[float, float]:
    if not poly:
        return (0.0, 0.0)
    return (sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))


def _is_axis_aligned_rect(poly: list[tuple[float, float]], bbox: tuple[float, float, float, float], tol: float = 0.05) -> bool:
    if len(poly) != 4:
        return False
    x0, y0, w, h = bbox
    corners = {(round(x0, 2), round(y0, 2)), (round(x0 + w, 2), round(y0, 2)),
               (round(x0 + w, 2), round(y0 + h, 2)), (round(x0, 2), round(y0 + h, 2))}
    for px, py in poly:
        if not any(math.hypot(px - cx, py - cy) <= tol for cx, cy in corners):
            return False
    return True


def heuristic_rules(comps: list[Comp], nets: dict[str, list[tuple[str, str]]], pin_types: dict | None = None) -> dict:
    """Connectors on the edge; each capacitor clusters with the IC it decouples.
    Bulk caps (uF) prefer the IC sourcing the rail (power_out), small caps (nF) the IC consuming it (power_in)."""
    pin_types = pin_types or {}
    c_by = {c.ref: c for c in comps}
    edge = [c.ref for c in comps if c.ref.startswith("J")]
    clusters: dict[str, list] = {}
    for cap in (c for c in comps if c.ref.startswith("C")):
        cap_nets = [n for n, pins in nets.items() if any(r == cap.ref for r, _ in pins)]
        best = None
        for n in cap_nets:
            pins = nets[n]
            if any(pin_types.get((r, p)) == "ground" for r, p in pins) or n.upper() in ("GND", "AGND", "DGND"):
                continue
            ics = [(r, p) for r, p in pins if r.startswith("U") and r in c_by]
            if not ics:
                continue
            bulk = "uf" in cap.value.lower()
            want = "power_out" if bulk else "power_in"
            pick = next((r for r, p in ics if pin_types.get((r, p)) == want), None) or next(
                (r for r, p in ics if pin_types.get((r, p)) in ("power_in", "power_out")), ics[0][0]
            )
            best = pick
            break
        if best:
            clusters.setdefault(best, []).append(cap.ref)
    return {"edge_components": edge, "clusters": [[a] + b for a, b in clusters.items()]}


def _sanitize(rules: dict, refs: set) -> dict:
    return {
        "edge_components": [r for r in rules.get("edge_components", []) if r in refs],
        "clusters": [[r for r in cl if r in refs] for cl in rules.get("clusters", []) if len([r for r in cl if r in refs]) > 1],
    }


class _State:
    def __init__(self, comps, nets, rules, outline, board_outline: BoardOutline | None = None, margin: float = EDGE_MARGIN):
        self.c = {c.ref: c for c in comps}
        self.refs = list(self.c)
        self.nets = [[(r, p) for r, p in pins if r in self.c] for pins in nets.values()]
        self.nets = [n for n in self.nets if len(n) > 1]
        self.rules, self.ol = rules, outline
        self.margin = margin
        self.pad = {(c.ref, p.num): (p.x, p.y) for c in comps for p in c.pads}
        self.bb = {c.ref: c.local_bbox() for c in comps}
        self.outer_poly: list[tuple[float, float]] | None = None
        self.holes: list[list[tuple[float, float]]] = []
        if board_outline is not None and len(board_outline.outer) >= 3:
            if board_outline.holes or not _is_axis_aligned_rect(board_outline.outer, outline):
                self.outer_poly = board_outline.outer
                self.holes = board_outline.holes

    def rect(self, ref, x, y, rot):
        x0, y0, x1, y1 = self.bb[ref]
        pts = [rotate(a, b, rot) for a, b in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        return (x + min(p[0] for p in pts), y + min(p[1] for p in pts), x + max(p[0] for p in pts), y + max(p[1] for p in pts))

    def hpwl(self, pos):
        tot = 0.0
        for net in self.nets:
            xs, ys = [], []
            for r, p in net:
                if (r, p) not in self.pad:
                    continue
                x, y, rot = pos[r]
                dx, dy = rotate(*self.pad[(r, p)], rot)
                xs.append(x + dx); ys.append(y + dy)
            if xs:
                tot += (max(xs) - min(xs)) + (max(ys) - min(ys))
        return tot

    def _poly_violation(self, rbox: tuple[float, float, float, float]) -> float:
        """Penalty for corners/center outside a non-rectangular outer polygon or inside cutout holes."""
        if self.outer_poly is None:
            return 0.0
        ax0, ay0, ax1, ay1 = rbox
        pts = ((ax0, ay0), (ax1, ay0), (ax1, ay1), (ax0, ay1), ((ax0 + ax1) * 0.5, (ay0 + ay1) * 0.5))
        viol = 0.0
        for px, py in pts:
            if not _point_in_polygon(px, py, self.outer_poly):
                d, _, _ = _closest_on_polygon(px, py, self.outer_poly)
                viol += 120.0 * (d + 0.5)
            else:
                d, _, _ = _closest_on_polygon(px, py, self.outer_poly)
                if d < self.margin:
                    viol += 60.0 * (self.margin - d)
            for hole in self.holes:
                if _point_in_polygon(px, py, hole):
                    d, _, _ = _closest_on_polygon(px, py, hole)
                    viol += 120.0 * (d + 0.5)
        return viol

    def penalty(self, pos):
        R = {r: self.rect(r, *pos[r]) for r in self.refs}
        pen, ov = 0.0, 0
        ox0, oy0, ow, oh = self.ol
        m = self.margin
        for i, a in enumerate(self.refs):
            ax0, ay0, ax1, ay1 = R[a]
            pen += 100 * (
                max(0.0, (ox0 + m) - ax0)
                + max(0.0, (oy0 + m) - ay0)
                + max(0.0, ax1 - (ox0 + ow - m))
                + max(0.0, ay1 - (oy0 + oh - m))
            )
            if self.outer_poly is not None:
                pen += self._poly_violation(R[a])
            for b in self.refs[i + 1:]:
                bx0, by0, bx1, by1 = R[b]
                dx, dy = min(ax1, bx1) - max(ax0, bx0), min(ay1, by1) - max(ay0, by0)
                if dx > 0 and dy > 0:
                    ov += 1
                    pen += 50 * dx * dy + 30 * min(dx, dy) + 5
        for ref in self.rules["edge_components"]:
            x0, y0, x1, y1 = R[ref]
            if self.outer_poly is not None:
                cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
                d_edge, _, _ = _closest_on_polygon(cx, cy, self.outer_poly)
                pen += 20 * max(0.0, d_edge - max(x1 - x0, y1 - y0) * 0.5)
            else:
                pen += 20 * min(max(0.0, x0 - (ox0 + m)), max(0.0, y0 - (oy0 + m)),
                                max(0.0, (ox0 + ow - m) - x1), max(0.0, (oy0 + oh - m) - y1))
        for cl in self.rules["clusters"]:
            a = R[cl[0]]
            for b in cl[1:]:
                bb = R[b]
                gap = math.hypot(max(0.0, bb[0] - a[2], a[0] - bb[2]), max(0.0, bb[1] - a[3], a[1] - bb[3]))
                pen += 8 * max(0.0, gap - 0.5) if gap > CLUSTER_GAP else 1.0 * gap
        return pen, ov

    def cost(self, pos):
        return self.hpwl(pos) + self.penalty(pos)[0]


def solve_coordinates(comps, nets, rules, outline, iters=3000, seed=0, board_outline: BoardOutline | None = None):
    rnd = random.Random(seed)
    rules = _sanitize(rules, {c.ref for c in comps})
    st = _State(comps, nets, rules, outline, board_outline=board_outline)
    x0, y0, w, h = outline
    g = nx.Graph(); g.add_nodes_from(st.refs)
    for net in st.nets:
        for (a, _), (b, _) in zip(net, net[1:]):
            if a != b:
                g.add_edge(a, b, weight=g.get_edge_data(a, b, {"weight": 0})["weight"] + 1)
    lay = nx.spring_layout(g, seed=seed, weight="weight", k=0.8)
    mx = max(abs(v[0]) for v in lay.values()) or 1; my = max(abs(v[1]) for v in lay.values()) or 1
    cx_init, cy_init = _polygon_centroid(st.outer_poly) if st.outer_poly else (x0 + w / 2, y0 + h / 2)
    pos = {r: (float(cx_init + lay[r][0] / mx * w * 0.35), float(cy_init + lay[r][1] / my * h * 0.35), 0.0) for r in st.refs}
    cur = st.cost(pos); best, best_c = dict(pos), cur
    T0 = max(cur, 1.0) * 0.05
    for i in range(iters):
        T = T0 * (0.002 ** (i / max(iters - 1, 1)))
        ref = rnd.choice(st.refs); old = pos[ref]; m = rnd.random(); swap = None
        if m < 0.6:
            s = max(0.2, 4.0 * T / T0)
            pos[ref] = (old[0] + rnd.gauss(0, s), old[1] + rnd.gauss(0, s), old[2])
        elif m < 0.8:
            pos[ref] = (old[0], old[1], (old[2] + 90) % 360)
        elif len(st.refs) > 1:
            other = rnd.choice([r for r in st.refs if r != ref]); swap = (other, pos[other])
            pos[ref] = (pos[other][0], pos[other][1], old[2]); pos[other] = (old[0], old[1], pos[other][2])
        new = st.cost(pos)
        if new <= cur or rnd.random() < math.exp((cur - new) / max(T, 1e-9)):
            cur = new
            if cur < best_c:
                best, best_c = dict(pos), cur
        else:
            pos[ref] = old
            if swap:
                pos[swap[0]] = swap[1]
    pos = _legalize(st, best)
    pen, ov = st.penalty(pos)
    return pos, {"hpwl_mm": round(st.hpwl(pos), 2), "overlaps": ov, "cost": round(st.cost(pos), 2), "rules": rules}


def _legalize(st: _State, pos, rounds=300):
    pos = dict(pos); ox0, oy0, ow, oh = st.ol
    m = st.margin
    poly_c = _polygon_centroid(st.outer_poly) if st.outer_poly else None
    for _ in range(rounds):
        moved = False
        for i, a in enumerate(st.refs):
            for b in st.refs[i + 1:]:
                ra, rb = st.rect(a, *pos[a]), st.rect(b, *pos[b])
                dx, dy = min(ra[2], rb[2]) - max(ra[0], rb[0]), min(ra[3], rb[3]) - max(ra[1], rb[1])
                if dx > 0 and dy > 0:
                    moved = True
                    ax, ay, ar = pos[a]; bx, by, br = pos[b]
                    if dx < dy:
                        s = 1 if ax >= bx else -1; pos[a] = (ax + s * (dx / 2 + 0.01), ay, ar); pos[b] = (bx - s * (dx / 2 + 0.01), by, br)
                    else:
                        s = 1 if ay >= by else -1; pos[a] = (ax, ay + s * (dy / 2 + 0.01), ar); pos[b] = (bx, by - s * (dy / 2 + 0.01), br)
        for r in st.refs:
            x, y, rot = pos[r]; x0, y0, x1, y1 = st.rect(r, x, y, rot)
            nx_val = x + max(0.0, (ox0 + m) - x0) - max(0.0, x1 - (ox0 + ow - m))
            ny_val = y + max(0.0, (oy0 + m) - y0) - max(0.0, y1 - (oy0 + oh - m))
            if st.outer_poly is not None:
                rx0, ry0, rx1, ry1 = st.rect(r, nx_val, ny_val, rot)
                corners = ((rx0, ry0), (rx1, ry0), (rx1, ry1), (rx0, ry1))
                for px, py in corners:
                    if not _point_in_polygon(px, py, st.outer_poly):
                        d, cx, cy = _closest_on_polygon(px, py, st.outer_poly)
                        # Push corner inside toward closest edge point + small inward bias toward centroid
                        vx, vy = cx - px, cy - py
                        vlen = math.hypot(vx, vy) or 1.0
                        nx_val += vx + (vx / vlen) * (m + 0.05)
                        ny_val += vy + (vy / vlen) * (m + 0.05)
                        moved = True
                        break
                    for hole in st.holes:
                        if _point_in_polygon(px, py, hole):
                            d, cx, cy = _closest_on_polygon(px, py, hole)
                            vx, vy = cx - px, cy - py
                            vlen = math.hypot(vx, vy) or 1.0
                            nx_val += vx + (vx / vlen) * (m + 0.05)
                            ny_val += vy + (vy / vlen) * (m + 0.05)
                            moved = True
                            break
                if poly_c and not _point_in_polygon(nx_val, ny_val, st.outer_poly):
                    nx_val = nx_val * 0.8 + poly_c[0] * 0.2
                    ny_val = ny_val * 0.8 + poly_c[1] * 0.2
                    moved = True
            pos[r] = (nx_val, ny_val, rot)
        if not moved:
            break
    return pos
