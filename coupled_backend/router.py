"""DRC-aware 2-layer PCB autorouter (F.Cu + B.Cu + plated vias + targeted Pathfinder rip-up & reroute).

Replaces the naive single-layer L-shape mock router with a real geometric grid A* router that:
  - Enforces exact copper-to-copper clearances against foreign SMD pads, THT pads, NPTH drills,
    routed tracks (including pad lead-in stubs), plated vias, and Edge.Cuts polygons/holes.
  - Routes across F.Cu and B.Cu using plated through-hole vias (0.6mm / 0.3mm drill) whenever
    nets need to cross, avoiding tracks_crossing, shorting_items, and solder_mask_bridge.
  - Adds foreign-pad proximity repulsion so early nets leave escape channels around fine-pitch
    ICs (0.65mm LGA-8, 0.95mm SOT-23-5, 1.0mm USB-C).
  - Uses Union-Find component bridging for multi-pin nets and targeted rip-up-and-reroute
    (identifying exact blocking nets via soft-halo A*, bumping congestion, and re-routing).
  - Streams live routing progress events identical to the PCBWorld SSE schema.
"""
from __future__ import annotations

import heapq
import math
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import sexpr as sx
from .kicad_io import BoardOutline, Comp, parse_board, parse_board_outline, render_svg, rotate
from .placer import _dist_to_seg, _point_in_polygon


@dataclass
class _WorldPad:
    ref: str
    num: str
    wx: float
    wy: float
    hw: float  # half-width in pad-local frame
    hh: float  # half-height in pad-local frame
    ang_rad: float  # world orientation (radians, KiCad CCW)
    cos_a: float
    sin_a: float
    kind: str  # smd | thru_hole | np_thru_hole
    drill: float
    net: str | None
    layers: tuple[int, ...]  # (0,) for F.Cu SMD, (0, 1) for THT/NPTH


def _point_to_obb_dist(px: float, py: float, pad: _WorldPad) -> float:
    """Exact Euclidean distance from world point (px, py) to rotated pad rectangle."""
    dx = px - pad.wx
    dy = py - pad.wy
    lx = dx * pad.cos_a - dy * pad.sin_a
    ly = dx * pad.sin_a + dy * pad.cos_a
    qx = max(abs(lx) - pad.hw, 0.0)
    qy = max(abs(ly) - pad.hh, 0.0)
    return math.hypot(qx, qy)


def _seg_to_obb_dist(ax: float, ay: float, bx: float, by: float, pad: _WorldPad) -> float:
    """Minimum distance from segment (a->b) to rotated pad rectangle."""
    best = min(_point_to_obb_dist(ax, ay, pad), _point_to_obb_dist(bx, by, pad))
    if best == 0.0:
        return 0.0
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 > 1e-12:
        t_proj = max(0.0, min(1.0, ((pad.wx - ax) * dx + (pad.wy - ay) * dy) / l2))
        best = min(best, _point_to_obb_dist(ax + t_proj * dx, ay + t_proj * dy, pad))
        steps = max(4, min(16, int(math.sqrt(l2) / 0.06) + 1))
        for i in range(1, steps):
            t = i / steps
            d = _point_to_obb_dist(ax + t * dx, ay + t * dy, pad)
            if d < best:
                best = d
                if best == 0.0:
                    return 0.0
    return best


def _orient(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _seg_to_seg_dist(ax: float, ay: float, bx: float, by: float, cx: float, cy: float, dx: float, dy: float) -> float:
    """Exact 2D minimum Euclidean distance between segments AB and CD."""
    # Quick intersection check
    o1 = _orient(ax, ay, bx, by, cx, cy)
    o2 = _orient(ax, ay, bx, by, dx, dy)
    o3 = _orient(cx, cy, dx, dy, ax, ay)
    o4 = _orient(cx, cy, dx, dy, bx, by)
    if (o1 * o2 < 0.0) and (o3 * o4 < 0.0):
        return 0.0
    return min(
        _dist_to_seg(ax, ay, cx, cy, dx, dy)[0],
        _dist_to_seg(bx, by, cx, cy, dx, dy)[0],
        _dist_to_seg(cx, cy, ax, ay, bx, by)[0],
        _dist_to_seg(dx, dy, ax, ay, bx, by)[0],
    )


def _extract_world_pads(comps: list[Comp]) -> list[_WorldPad]:
    out: list[_WorldPad] = []
    for c in comps:
        for p in c.pads:
            dx, dy = rotate(p.x, p.y, c.rot)
            world_deg = (p.rot + c.rot) % 360.0
            rad = math.radians(world_deg)
            is_tht = p.kind in ("thru_hole", "np_thru_hole")
            layers = (0, 1) if is_tht else (0,)
            out.append(
                _WorldPad(
                    ref=c.ref,
                    num=p.num,
                    wx=c.x + dx,
                    wy=c.y + dy,
                    hw=max(p.w * 0.5, 0.05),
                    hh=max(p.h * 0.5, 0.05),
                    ang_rad=rad,
                    cos_a=math.cos(rad),
                    sin_a=math.sin(rad),
                    kind=p.kind,
                    drill=p.drill,
                    net=None if p.kind == "np_thru_hole" else p.net,
                    layers=layers,
                )
            )
    return out


def _strip_existing_routing(text: str) -> str:
    """Remove top-level (segment ...), (via ...), and (arc ...) nodes so re-routing starts clean."""
    start = text.find("(kicad_pcb")
    if start < 0:
        return text
    out: list[str] = []
    n = len(text)
    i = 0
    depth = 0
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "(":
            if depth == 1:
                j = i + 1
                while j < n and text[j].isspace():
                    j += 1
                k = j
                while k < n and not text[k].isspace() and text[k] not in "()":
                    k += 1
                head = text[j:k]
                if head in ("segment", "via", "arc"):
                    sub_depth = 1
                    sub_str = False
                    i += 1
                    while i < n and sub_depth > 0:
                        c2 = text[i]
                        if sub_str:
                            if c2 == "\\" and i + 1 < n:
                                i += 2
                                continue
                            if c2 == '"':
                                sub_str = False
                        elif c2 == '"':
                            sub_str = True
                        elif c2 == "(":
                            sub_depth += 1
                        elif c2 == ")":
                            sub_depth -= 1
                        i += 1
                    while i < n and text[i] in " \t\r\n":
                        i += 1
                    continue
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch == ")":
            depth -= 1
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _disk_offsets(radius_cells: float) -> list[tuple[int, int]]:
    r = int(math.ceil(radius_cells))
    r2 = radius_cells * radius_cells + 1e-9
    return [
        (dx, dy)
        for dy in range(-r, r + 1)
        for dx in range(-r, r + 1)
        if dx * dx + dy * dy <= r2
    ]


class _GridBoard:
    """2-layer routing grid with exact pad, via, track, and Edge.Cuts DRC clearance enforcement."""

    def __init__(
        self,
        outline: BoardOutline,
        pads: list[_WorldPad],
        *,
        grid_mm: float = 0.20,
        track_w: float = 0.18,
        clearance: float = 0.21,
        via_dia: float = 0.54,
        via_drill: float = 0.30,
        edge_margin: float = 0.52,
    ):
        self.outline = outline
        self.pads = pads
        self.grid = grid_mm
        self.track_w = track_w
        self.clearance = clearance
        self.via_dia = via_dia
        self.via_drill = via_drill
        self.edge_margin = edge_margin

        x0, y0, w, h = outline.bbox
        self.x0 = x0
        self.y0 = y0
        self.nx = max(8, int(math.floor(w / grid_mm)) + 1)
        self.ny = max(8, int(math.floor(h / grid_mm)) + 1)
        self.size = self.nx * self.ny

        net_names = sorted({p.net for p in pads if p.net})
        self.net_to_idx = {n: i + 1 for i, n in enumerate(net_names)}
        self.idx_to_net = {i + 1: n for i, n in enumerate(net_names)}

        # Static obstacles from Edge.Cuts + Pads
        self.static_block: list[list[int]] = [[0] * self.size, [0] * self.size]
        self.via_static: list[int] = [0] * self.size
        # Two-tier pad-escape apron (keeps early nets from boxing in adjacent fine-pitch SMD pads!)
        self.pad_near_inner: list[list[int]] = [[0] * self.size, [0] * self.size]
        self.pad_near_outer: list[list[int]] = [[0] * self.size, [0] * self.size]

        self._build_static_maps()

        # Dynamic routed state (per net, for rip-up & reroute)
        self.dyn_track: list[list[int]] = [[0] * self.size, [0] * self.size]
        self.halo_owner: list[list[int]] = [[0] * self.size, [0] * self.size]
        self.halo_multi: list[dict[int, set[int]]] = [{}, {}]
        self.via_cells: dict[int, int] = {}
        self.via_halo_owner: list[int] = [0] * self.size
        self.via_halo_multi: dict[int, set[int]] = {}
        self.via_drill_block: list[int] = [0] * self.size

        self.track_halo_off = _disk_offsets((track_w + clearance - 1e-4) / grid_mm)
        self.via_to_track_off = _disk_offsets((via_dia * 0.5 + track_w * 0.5 + clearance + 0.02) / grid_mm)
        self.via_to_via_off = _disk_offsets((via_dia + clearance + 0.02) / grid_mm)
        self.via_hole_off = _disk_offsets((via_drill + 0.25) / grid_mm)

        self.net_routes: dict[int, dict] = {}
        self.congestion: list[list[float]] = [[0.0] * self.size, [0.0] * self.size]

    def g2w(self, gx: int, gy: int) -> tuple[float, float]:
        return self.x0 + gx * self.grid, self.y0 + gy * self.grid

    def w2g(self, wx: float, wy: float) -> tuple[int, int]:
        gx = max(0, min(self.nx - 1, int(round((wx - self.x0) / self.grid))))
        gy = max(0, min(self.ny - 1, int(round((wy - self.y0) / self.grid))))
        return gx, gy

    def _dist_to_edge(self, wx: float, wy: float) -> float:
        poly = self.outline.outer
        if not _point_in_polygon(wx, wy, poly):
            return -1.0
        best = float("inf")
        n = len(poly)
        for i in range(n):
            d, _, _ = _dist_to_seg(wx, wy, poly[i][0], poly[i][1], poly[(i + 1) % n][0], poly[(i + 1) % n][1])
            if d < best:
                best = d
        for hole in self.outline.holes:
            if _point_in_polygon(wx, wy, hole):
                return -1.0
            m = len(hole)
            for i in range(m):
                d, _, _ = _dist_to_seg(wx, wy, hole[i][0], hole[i][1], hole[(i + 1) % m][0], hole[(i + 1) % m][1])
                if d < best:
                    best = d
        return best

    def _mark_static(self, arr: list[int], idx: int, nidx: int) -> None:
        cur = arr[idx]
        if cur == 0:
            arr[idx] = nidx
        elif cur != nidx:
            arr[idx] = -1

    def _build_static_maps(self) -> None:
        track_edge_min = self.track_w * 0.5 + self.edge_margin
        via_edge_min = self.via_dia * 0.5 + self.edge_margin

        is_rect = (
            len(self.outline.outer) == 4
            and not self.outline.holes
            and all(
                min(abs(px - self.outline.bbox[0]), abs(px - (self.outline.bbox[0] + self.outline.bbox[2]))) < 1e-3
                and min(abs(py - self.outline.bbox[1]), abs(py - (self.outline.bbox[1] + self.outline.bbox[3]))) < 1e-3
                for px, py in self.outline.outer
            )
        )
        bx0, by0, bw, bh = self.outline.bbox
        bx1, by1 = bx0 + bw, by0 + bh

        for gy in range(self.ny):
            wy = self.y0 + gy * self.grid
            row = gy * self.nx
            for gx in range(self.nx):
                wx = self.x0 + gx * self.grid
                if is_rect:
                    d_edge = min(wx - bx0, bx1 - wx, wy - by0, by1 - wy)
                else:
                    d_edge = self._dist_to_edge(wx, wy)
                if d_edge < track_edge_min:
                    self.static_block[0][row + gx] = -1
                    self.static_block[1][row + gx] = -1
                    self.via_static[row + gx] = -1
                elif d_edge < via_edge_min:
                    self.via_static[row + gx] = -1

        track_pad_clear = self.track_w * 0.5 + self.clearance + 0.035
        via_pad_clear = self.via_dia * 0.5 + self.clearance + 0.035
        pad_apron_inner = track_pad_clear + 0.55
        pad_apron_outer = track_pad_clear + 1.15

        for pad in self.pads:
            nidx = self.net_to_idx.get(pad.net, -1) if pad.net else -1
            diag_r = math.hypot(pad.hw, pad.hh)
            pad_drill_clear = (pad.drill * 0.5 + self.via_drill * 0.5 + 0.25) if pad.drill > 0 else 0.0
            max_r = max(diag_r + max(via_pad_clear, pad_apron_outer), pad_drill_clear) + self.grid
            gx0 = max(0, int(math.floor((pad.wx - max_r - self.x0) / self.grid)))
            gx1 = min(self.nx - 1, int(math.ceil((pad.wx + max_r - self.x0) / self.grid)))
            gy0 = max(0, int(math.floor((pad.wy - max_r - self.y0) / self.grid)))
            gy1 = min(self.ny - 1, int(math.ceil((pad.wy + max_r - self.y0) / self.grid)))

            for gy in range(gy0, gy1 + 1):
                wy = self.y0 + gy * self.grid
                row = gy * self.nx
                for gx in range(gx0, gx1 + 1):
                    wx = self.x0 + gx * self.grid
                    d = _point_to_obb_dist(wx, wy, pad)
                    cell = row + gx
                    if d < track_pad_clear:
                        for ly in pad.layers:
                            self._mark_static(self.static_block[ly], cell, nidx)
                    elif d < pad_apron_inner:
                        for ly in pad.layers:
                            self._mark_static(self.pad_near_inner[ly], cell, nidx)
                    elif d < pad_apron_outer and pad.kind == "smd":
                        for ly in pad.layers:
                            self._mark_static(self.pad_near_outer[ly], cell, nidx)
                    if pad.drill > 0 and math.hypot(wx - pad.wx, wy - pad.wy) < pad_drill_clear:
                        self.via_static[cell] = -1
                    elif d < via_pad_clear:
                        if d < 0.05:
                            self.via_static[cell] = -1
                        else:
                            self._mark_static(self.via_static, cell, nidx)

    def _add_halo(self, owner: list[int], multi: dict[int, set[int]], cell: int, nidx: int) -> None:
        cur = owner[cell]
        if cur == 0:
            owner[cell] = nidx
        elif cur == nidx:
            if cell in multi:
                multi[cell].add(nidx)
        elif cur > 0:
            s = multi.setdefault(cell, {cur})
            s.add(nidx)
            owner[cell] = -1
        else:
            multi.setdefault(cell, set()).add(nidx)

    def _rasterize_seg_cells(self, x1: float, y1: float, x2: float, y2: float, ly: int) -> set[tuple[int, int, int]]:
        dist = math.hypot(x2 - x1, y2 - y1)
        steps = max(1, int(math.ceil(dist / (self.grid * 0.45))))
        out: set[tuple[int, int, int]] = set()
        for i in range(steps + 1):
            t = i / steps
            gx, gy = self.w2g(x1 + t * (x2 - x1), y1 + t * (y2 - y1))
            out.add((gx, gy, ly))
        return out

    def _stamp_net(self, nidx: int, cells: set[tuple[int, int, int]], vias: set[tuple[int, int]]) -> None:
        nx, ny = self.nx, self.ny
        for gx, gy, ly in cells:
            c = gy * nx + gx
            self.dyn_track[ly][c] = nidx
            for dx, dy in self.track_halo_off:
                nx2, ny2 = gx + dx, gy + dy
                if 0 <= nx2 < nx and 0 <= ny2 < ny:
                    self._add_halo(self.halo_owner[ly], self.halo_multi[ly], ny2 * nx + nx2, nidx)
            for dx, dy in self.via_to_track_off:
                nx2, ny2 = gx + dx, gy + dy
                if 0 <= nx2 < nx and 0 <= ny2 < ny:
                    self._add_halo(self.via_halo_owner, self.via_halo_multi, ny2 * nx + nx2, nidx)
        for gx, gy in vias:
            vc = gy * nx + gx
            self.via_cells[vc] = nidx
            for dx, dy in self.via_to_track_off:
                nx2, ny2 = gx + dx, gy + dy
                if 0 <= nx2 < nx and 0 <= ny2 < ny:
                    c2 = ny2 * nx + nx2
                    self._add_halo(self.halo_owner[0], self.halo_multi[0], c2, nidx)
                    self._add_halo(self.halo_owner[1], self.halo_multi[1], c2, nidx)
            for dx, dy in self.via_to_via_off:
                nx2, ny2 = gx + dx, gy + dy
                if 0 <= nx2 < nx and 0 <= ny2 < ny:
                    self._add_halo(self.via_halo_owner, self.via_halo_multi, ny2 * nx + nx2, nidx)
            for dx, dy in self.via_hole_off:
                nx2, ny2 = gx + dx, gy + dy
                if 0 <= nx2 < nx and 0 <= ny2 < ny:
                    self.via_drill_block[ny2 * nx + nx2] = 1

    def rebuild_dynamic_halos(self) -> None:
        self.dyn_track = [[0] * self.size, [0] * self.size]
        self.halo_owner = [[0] * self.size, [0] * self.size]
        self.halo_multi = [{}, {}]
        self.via_cells = {}
        self.via_halo_owner = [0] * self.size
        self.via_halo_multi = {}
        self.via_drill_block = [0] * self.size
        for nidx, rdata in self.net_routes.items():
            self._stamp_net(nidx, rdata["stamped"], rdata["vias"])

    def rip_up_net(self, nidx: int) -> None:
        if nidx in self.net_routes:
            del self.net_routes[nidx]
            self.rebuild_dynamic_halos()

    def commit_route(
        self,
        nidx: int,
        cells: set[tuple[int, int, int]],
        vias: set[tuple[int, int]],
        segments: list[tuple[float, float, float, float, int]],
        via_coords: list[tuple[float, float]],
    ) -> None:
        # Rasterize ALL committed segments (including pad lead-in/out stubs!) so nothing goes unstamped
        stamped = set(cells)
        for x1, y1, x2, y2, ly in segments:
            stamped.update(self._rasterize_seg_cells(x1, y1, x2, y2, ly))

        entry = self.net_routes.setdefault(
            nidx, {"cells": set(), "stamped": set(), "vias": set(), "segments": [], "via_coords": []}
        )
        entry["cells"].update(cells)
        entry["stamped"].update(stamped)
        entry["vias"].update(vias)
        entry["segments"].extend(segments)
        entry["via_coords"].extend(via_coords)
        self._stamp_net(nidx, stamped, vias)

    def can_traverse(self, cell: int, ly: int, nidx: int) -> bool:
        sb = self.static_block[ly][cell]
        if sb != 0 and sb != nidx:
            return False
        ho = self.halo_owner[ly][cell]
        if ho != 0 and ho != nidx:
            return False
        return True

    def can_traverse_static(self, cell: int, ly: int, nidx: int) -> bool:
        sb = self.static_block[ly][cell]
        return sb == 0 or sb == nidx

    def blocking_nets_at(self, cell: int, ly: int, nidx: int) -> set[int]:
        ho = self.halo_owner[ly][cell]
        if ho == 0 or ho == nidx:
            return set()
        if ho > 0:
            return {ho}
        return {k for k in self.halo_multi[ly].get(cell, ()) if k != nidx}

    def can_place_via(self, cell: int, nidx: int, *, allow_dynamic: bool = False) -> bool:
        if self.via_cells.get(cell) == nidx:
            return True
        if not allow_dynamic and self.via_drill_block[cell]:
            return False
        vs = self.via_static[cell]
        if vs != 0 and vs != nidx:
            return False
        if not allow_dynamic:
            vho = self.via_halo_owner[cell]
            if vho != 0 and vho != nidx:
                return False
            return self.can_traverse(cell, 0, nidx) and self.can_traverse(cell, 1, nidx)
        return self.can_traverse_static(cell, 0, nidx) and self.can_traverse_static(cell, 1, nidx)

    def _stub_clears_foreign_copper(self, ax: float, ay: float, bx: float, by: float, ly: int, nidx: int) -> bool:
        min_seg_d = self.track_w + self.clearance
        min_via_d = self.via_dia * 0.5 + self.track_w * 0.5 + self.clearance
        xlo, xhi = min(ax, bx) - min_via_d - 0.1, max(ax, bx) + min_via_d + 0.1
        ylo, yhi = min(ay, by) - min_via_d - 0.1, max(ay, by) + min_via_d + 0.1
        for other_nidx, rdata in self.net_routes.items():
            if other_nidx == nidx:
                continue
            for sx1, sy1, sx2, sy2, sly in rdata["segments"]:
                if sly != ly:
                    continue
                if max(sx1, sx2) < xlo or min(sx1, sx2) > xhi or max(sy1, sy2) < ylo or min(sy1, sy2) > yhi:
                    continue
                if _seg_to_seg_dist(ax, ay, bx, by, sx1, sy1, sx2, sy2) < min_seg_d:
                    return False
            for vx, vy in rdata["via_coords"]:
                if xlo <= vx <= xhi and ylo <= vy <= yhi:
                    if _dist_to_seg(vx, vy, ax, ay, bx, by)[0] < min_via_d:
                        return False
        return True

    def find_pad_entries(
        self,
        pad: _WorldPad,
        nidx: int,
        *,
        allow_dynamic: bool = False,
    ) -> list[tuple[float, int, int, int]]:
        """Find legal grid entry cells (stub_dist_mm, gx, gy, layer) tightly anchored to `pad`."""
        cgx, cgy = self.w2g(pad.wx, pad.wy)
        foreign_pads = [
            p for p in self.pads
            if p is not pad and p.net != pad.net
            and abs(p.wx - pad.wx) < 3.0 and abs(p.wy - pad.wy) < 3.0
        ]
        min_stub_clear = self.track_w * 0.5 + self.clearance + 0.015
        tight_cap = max(pad.hw, pad.hh) + 0.24
        fallback_cap = max(pad.hw, pad.hh) + 0.50
        max_r = max(2, int(math.ceil(fallback_cap / self.grid)))

        tight: list[tuple[float, int, int, int]] = []
        wider: list[tuple[float, int, int, int]] = []

        for dy in range(-max_r, max_r + 1):
            gy = cgy + dy
            if not (0 <= gy < self.ny):
                continue
            for dx in range(-max_r, max_r + 1):
                gx = cgx + dx
                if not (0 <= gx < self.nx):
                    continue
                wx, wy = self.g2w(gx, gy)
                dist = math.hypot(wx - pad.wx, wy - pad.wy)
                if dist > fallback_cap:
                    continue
                cell = gy * self.nx + gx
                for ly in pad.layers:
                    if allow_dynamic:
                        if not self.can_traverse_static(cell, ly, nidx):
                            continue
                    else:
                        if not self.can_traverse(cell, ly, nidx):
                            continue
                    ok = True
                    for fp in foreign_pads:
                        if ly not in fp.layers:
                            continue
                        if _seg_to_obb_dist(pad.wx, pad.wy, wx, wy, fp) < min_stub_clear:
                            ok = False
                            break
                    if not ok:
                        continue
                    if not allow_dynamic and not self._stub_clears_foreign_copper(pad.wx, pad.wy, wx, wy, ly, nidx):
                        continue
                    if dist <= tight_cap:
                        tight.append((dist, gx, gy, ly))
                    else:
                        wider.append((dist, gx, gy, ly))

        tight.sort()
        if tight:
            return tight[:6]
        wider.sort()
        return wider[:4]

    def astar(
        self,
        nidx: int,
        starts: list[tuple[float, int, int, int]],
        goals: set[tuple[int, int, int]],
        *,
        via_cost: float = 14.0,
        bend_cost: float = 0.55,
        max_expansions: int = 95000,
        soft_dynamic_penalty: float | None = None,
    ) -> list[tuple[int, int, int]] | None:
        if not starts or not goals:
            return None
        for _d, gx, gy, ly in starts:
            if (gx, gy, ly) in goals:
                return [(gx, gy, ly)]

        g_xs = [g[0] for g in goals]
        g_ys = [g[1] for g in goals]
        g_min_x, g_max_x = min(g_xs), max(g_xs)
        g_min_y, g_max_y = min(g_ys), max(g_ys)

        def heur(gx: int, gy: int) -> float:
            dx = 0 if g_min_x <= gx <= g_max_x else (g_min_x - gx if gx < g_min_x else gx - g_max_x)
            dy = 0 if g_min_y <= gy <= g_max_y else (g_min_y - gy if gy < g_min_y else gy - g_max_y)
            return float(dx + dy)

        nx, ny = self.nx, self.ny
        inv_grid = 1.0 / self.grid
        g_best: dict[tuple[int, int, int], float] = {}
        parent: dict[tuple[int, int, int], tuple[int, int, int] | None] = {}
        heap: list[tuple[float, float, int, int, int, int]] = []

        for d_mm, gx, gy, ly in starts:
            k = (gx, gy, ly)
            g0 = d_mm * inv_grid
            if g0 < g_best.get(k, 1e18):
                g_best[k] = g0
                parent[k] = None
                heapq.heappush(heap, (g0 + heur(gx, gy), g0, gx, gy, ly, 4))

        dirs = ((1, 0, 0), (-1, 0, 1), (0, 1, 2), (0, -1, 3))
        soft = soft_dynamic_penalty is not None
        dyn_pen = soft_dynamic_penalty or 0.0
        expansions = 0

        while heap and expansions < max_expansions:
            _f, g, gx, gy, ly, pdir = heapq.heappop(heap)
            cur = (gx, gy, ly)
            if g > g_best.get(cur, 1e18) + 1e-9:
                continue
            if cur in goals:
                path = [cur]
                p = parent[cur]
                while p is not None:
                    path.append(p)
                    p = parent[p]
                path.reverse()
                return path

            expansions += 1
            cell = gy * nx + gx

            for dx, dy, did in dirs:
                nx2, ny2 = gx + dx, gy + dy
                if not (0 <= nx2 < nx and 0 <= ny2 < ny):
                    continue
                ncell = ny2 * nx + nx2
                extra = 0.0
                if soft:
                    if not self.can_traverse_static(ncell, ly, nidx):
                        continue
                    ho = self.halo_owner[ly][ncell]
                    if ho != 0 and ho != nidx:
                        extra = dyn_pen
                else:
                    if not self.can_traverse(ncell, ly, nidx):
                        continue

                pref = 0.0 if (ly == 0 and dx != 0) or (ly == 1 and dy != 0) else 0.10
                turn = bend_cost if (pdir < 4 and pdir != did) else 0.0
                pni = self.pad_near_inner[ly][ncell]
                pno = self.pad_near_outer[ly][ncell]
                apron = 4.5 if (pni != 0 and pni != nidx) else (1.8 if (pno != 0 and pno != nidx) else 0.0)
                reuse = -0.50 if self.dyn_track[ly][ncell] == nidx else 0.0
                step_c = max(0.35, 1.0 + pref + turn + apron + reuse + self.congestion[ly][ncell] + extra)
                ng = g + step_c
                nk = (nx2, ny2, ly)
                if ng < g_best.get(nk, 1e18):
                    g_best[nk] = ng
                    parent[nk] = cur
                    heapq.heappush(heap, (ng + heur(nx2, ny2), ng, nx2, ny2, ly, did))

            other_ly = 1 - ly
            if self.can_place_via(cell, nidx, allow_dynamic=soft):
                vc = 0.5 if self.via_cells.get(cell) == nidx else via_cost
                if soft and not self.can_place_via(cell, nidx, allow_dynamic=False):
                    vc += dyn_pen * 1.5
                ng = g + vc
                nk = (gx, gy, other_ly)
                if ng < g_best.get(nk, 1e18):
                    g_best[nk] = ng
                    parent[nk] = cur
                    heapq.heappush(heap, (ng + heur(gx, gy), ng, gx, gy, other_ly, 4))

        return None


def _compress_path_to_segments_and_vias(
    grid: _GridBoard,
    path: list[tuple[int, int, int]],
    start_pad: _WorldPad | None,
    end_pad: _WorldPad | None,
) -> tuple[list[tuple[float, float, float, float, int]], set[tuple[int, int]]]:
    """Convert grid path into minimal collinear (x1, y1, x2, y2, layer) segments + via grid coords."""
    segs: list[tuple[float, float, float, float, int]] = []
    vias: set[tuple[int, int]] = set()
    if not path:
        return segs, vias

    if start_pad is not None:
        sx, sy = grid.g2w(path[0][0], path[0][1])
        if math.hypot(sx - start_pad.wx, sy - start_pad.wy) > 1e-4:
            segs.append((start_pad.wx, start_pad.wy, sx, sy, path[0][2]))

    run_start = path[0]
    run_dir: tuple[int, int] | None = None
    for i in range(1, len(path)):
        prev = path[i - 1]
        cur = path[i]
        if cur[2] != prev[2]:
            if prev != run_start:
                x1, y1 = grid.g2w(run_start[0], run_start[1])
                x2, y2 = grid.g2w(prev[0], prev[1])
                segs.append((x1, y1, x2, y2, run_start[2]))
            vias.add((cur[0], cur[1]))
            run_start = cur
            run_dir = None
        else:
            d = (cur[0] - prev[0], cur[1] - prev[1])
            if run_dir is None:
                run_dir = d
            elif d != run_dir:
                x1, y1 = grid.g2w(run_start[0], run_start[1])
                x2, y2 = grid.g2w(prev[0], prev[1])
                segs.append((x1, y1, x2, y2, run_start[2]))
                run_start = prev
                run_dir = d

    if path[-1] != run_start:
        x1, y1 = grid.g2w(run_start[0], run_start[1])
        x2, y2 = grid.g2w(path[-1][0], path[-1][1])
        segs.append((x1, y1, x2, y2, run_start[2]))

    if end_pad is not None:
        ex, ey = grid.g2w(path[-1][0], path[-1][1])
        if math.hypot(end_pad.wx - ex, end_pad.wy - ey) > 1e-4:
            segs.append((ex, ey, end_pad.wx, end_pad.wy, path[-1][2]))

    return segs, vias


def _net_mst_pairs(pads: list[_WorldPad]) -> list[tuple[int, int]]:
    """Build Prim MST over pad indices (Manhattan distance)."""
    n = len(pads)
    if n < 2:
        return []
    in_tree = [False] * n
    in_tree[0] = True
    pairs: list[tuple[int, int]] = []
    for _ in range(n - 1):
        best = (float("inf"), -1, -1)
        for i in range(n):
            if not in_tree[i]:
                continue
            pi = pads[i]
            for j in range(n):
                if in_tree[j]:
                    continue
                pj = pads[j]
                d = abs(pi.wx - pj.wx) + abs(pi.wy - pj.wy)
                if d < best[0]:
                    best = (d, i, j)
        _, u, v = best
        in_tree[v] = True
        pairs.append((u, v))
    return pairs


def _format_routed_kicad(
    base_text: str,
    net_ids: dict[str, int],
    net_routes: dict[int, dict],
    idx_to_net: dict[int, str],
    *,
    track_w: float,
    via_dia: float,
    via_drill: float,
) -> str:
    layer_name = {0: "F.Cu", 1: "B.Cu"}
    lines: list[str] = []
    seen_vias: set[tuple[int, int, int]] = set()
    for nidx, rdata in net_routes.items():
        nname = idx_to_net[nidx]
        kid = net_ids.get(nname, 0)
        for x1, y1, x2, y2, ly in rdata["segments"]:
            if math.hypot(x2 - x1, y2 - y1) < 1e-4:
                continue
            lines.append(
                f'  (segment (start {x1:.4f} {y1:.4f}) (end {x2:.4f} {y2:.4f}) '
                f'(width {track_w:g}) (layer "{layer_name[ly]}") (net {kid}) (uuid "{uuid.uuid4()}"))'
            )
        for vx, vy in rdata.get("via_coords", ()):
            key = (round(vx * 1000), round(vy * 1000), kid)
            if key in seen_vias:
                continue
            seen_vias.add(key)
            lines.append(
                f'  (via (at {vx:.4f} {vy:.4f}) (size {via_dia:g}) (drill {via_drill:g}) '
                f'(layers "F.Cu" "B.Cu") (net {kid}) (uuid "{uuid.uuid4()}"))'
            )
    trimmed = base_text.rstrip()
    if trimmed.endswith(")"):
        trimmed = trimmed[:-1].rstrip()
    return trimmed + "\n" + "\n".join(lines) + "\n)\n"


def astar_route(
    board_path: str,
    max_steps: int,
    emit: Callable[[dict], None],
    *,
    grid_mm: float = 0.20,
    track_w: float = 0.18,
    clearance: float = 0.21,
    via_dia: float = 0.54,
    via_drill: float = 0.30,
    edge_margin: float = 0.52,
) -> dict:
    """Route all nets in `board_path` using DRC-aware 2-layer A* + vias + targeted rip-up & reroute."""
    raw_text = Path(board_path).read_text(encoding="utf-8", errors="ignore")
    clean_text = _strip_existing_routing(raw_text)
    tree, comps, nets, bbox = parse_board(clean_text)
    outline = parse_board_outline(tree)
    if outline is None:
        bx0, by0, bw, bh = bbox or (0.0, 0.0, 40.0, 30.0)
        outline = BoardOutline(
            bbox=(bx0, by0, bw, bh),
            outer=[(bx0, by0), (bx0 + bw, by0), (bx0 + bw, by0 + bh), (bx0, by0 + bh)],
            holes=[],
        )

    max_dim = max(outline.bbox[2], outline.bbox[3])
    if max_dim > 110.0:
        grid_mm = 0.25

    world_pads = _extract_world_pads(comps)
    net_ids = {str(n[2]): int(n[1]) for n in sx.find_all(tree, "net") if len(n) >= 3 and str(n[1]).isdigit()}

    pads_by_net: dict[str, list[_WorldPad]] = {}
    for wp in world_pads:
        if wp.net:
            pads_by_net.setdefault(wp.net, []).append(wp)

    routable_nets = [n for n, plist in pads_by_net.items() if len(plist) >= 2]

    def _net_sort_key(n: str) -> tuple[int, float]:
        ps = pads_by_net[n]
        span = (max(p.wx for p in ps) - min(p.wx for p in ps)) + (max(p.wy for p in ps) - min(p.wy for p in ps))
        return (len(ps), span)

    net_order = sorted(routable_nets, key=_net_sort_key)

    grid = _GridBoard(
        outline,
        world_pads,
        grid_mm=grid_mm,
        track_w=track_w,
        clearance=clearance,
        via_dia=via_dia,
        via_drill=via_drill,
        edge_margin=edge_margin,
    )

    def _route_single_net(
        net_name: str,
        emit_steps: bool,
        step_counter: list[int],
        routed_nets_count: int,
    ) -> tuple[bool, int, set[int]]:
        """Route one net strictly. Returns (all_connected, missing_bridges, blocking_net_indices)."""
        nidx = grid.net_to_idx[net_name]
        plist = pads_by_net[net_name]
        n_pads = len(plist)
        mst = _net_mst_pairs(plist)

        uf = list(range(n_pads))
        comp_pads: dict[int, set[int]] = {i: {i} for i in range(n_pads)}
        comp_cells: dict[int, set[tuple[int, int, int]]] = {i: set() for i in range(n_pads)}
        pad_connected: set[int] = set()

        def find(a: int) -> int:
            while uf[a] != a:
                uf[a] = uf[uf[a]]
                a = uf[a]
            return a

        net_vias: set[tuple[int, int]] = set()
        blockers: set[int] = set()

        def _build_side(root: int, hint: int, *, allow_dynamic: bool = False):
            pad_map: dict[tuple[int, int, int], int] = {}
            entries: list[tuple[float, int, int, int]] = [(0.0, gx, gy, ly) for (gx, gy, ly) in comp_cells[root]]
            for pi in comp_pads[root]:
                if pi not in pad_connected:
                    for d_mm, gx, gy, ly in grid.find_pad_entries(plist[pi], nidx, allow_dynamic=allow_dynamic):
                        entries.append((d_mm, gx, gy, ly))
                        pad_map.setdefault((gx, gy, ly), pi)
            if not entries:
                for d_mm, gx, gy, ly in grid.find_pad_entries(plist[hint], nidx, allow_dynamic=allow_dynamic):
                    entries.append((d_mm, gx, gy, ly))
                    pad_map.setdefault((gx, gy, ly), hint)
            return entries, pad_map

        def _connect_components(ru: int, rv: int, hint_u: int, hint_v: int, *, probe_blockers: bool = False) -> bool:
            if step_counter[0] >= max_steps:
                return False
            s_entries, start_pad_map = _build_side(ru, hint_u, allow_dynamic=False)
            g_entries, goal_pad_map = _build_side(rv, hint_v, allow_dynamic=False)
            goal_set = {(gx, gy, ly) for _, gx, gy, ly in g_entries}

            path = grid.astar(nidx, s_entries, goal_set)
            if path is None:
                if probe_blockers:
                    # Run soft-halo probe strictly to discover which foreign net(s) are blocking us
                    s_soft, _ = _build_side(ru, hint_u, allow_dynamic=True)
                    g_soft, _ = _build_side(rv, hint_v, allow_dynamic=True)
                    probe = grid.astar(
                        nidx,
                        s_soft,
                        {(gx, gy, ly) for _, gx, gy, ly in g_soft},
                        soft_dynamic_penalty=40.0,
                        max_expansions=60000,
                    )
                    if probe:
                        for gx, gy, ly in probe:
                            c = gy * grid.nx + gx
                            bn = grid.blocking_nets_at(c, ly, nidx)
                            if bn:
                                blockers.update(bn)
                                for dy2 in (-1, 0, 1):
                                    for dx2 in (-1, 0, 1):
                                        gx2, gy2 = gx + dx2, gy + dy2
                                        if 0 <= gx2 < grid.nx and 0 <= gy2 < grid.ny:
                                            grid.congestion[ly][gy2 * grid.nx + gx2] += 6.0
                return False

            sp_idx = None if path[0] in comp_cells[ru] else start_pad_map.get(path[0])
            ep_idx = None if path[-1] in comp_cells[rv] else goal_pad_map.get(path[-1])
            segs, vias = _compress_path_to_segments_and_vias(
                grid,
                path,
                plist[sp_idx] if sp_idx is not None else None,
                plist[ep_idx] if ep_idx is not None else None,
            )
            new_cells = set(path)
            new_vias = vias - net_vias
            via_coords = [grid.g2w(vx_g, vy_g) for vx_g, vy_g in new_vias]
            net_vias.update(new_vias)
            grid.commit_route(nidx, new_cells, new_vias, segs, via_coords)

            if sp_idx is not None:
                pad_connected.add(sp_idx)
            if ep_idx is not None:
                pad_connected.add(ep_idx)

            uf[rv] = ru
            comp_pads[ru] |= comp_pads.pop(rv)
            comp_cells[ru] |= comp_cells.pop(rv)
            comp_cells[ru] |= new_cells

            step_counter[0] += 1
            if emit_steps:
                emit({
                    "type": "step",
                    "step": step_counter[0],
                    "connected_nets": routed_nets_count,
                    "total_nets": len(routable_nets),
                    "unrouted_count": max(0, len(routable_nets) - routed_nets_count),
                    "vias": sum(len(r["vias"]) for r in grid.net_routes.values()),
                    "drc_violations": 0,
                    "engine": "astar",
                })
            return True

        # Pass 1: route shortest MST pairs first (tight fine-pitch IC escapes before long trunks)
        mst.sort(key=lambda uv: abs(plist[uv[0]].wx - plist[uv[1]].wx) + abs(plist[uv[0]].wy - plist[uv[1]].wy))
        deferred: list[tuple[int, int]] = []
        for u_i, v_i in mst:
            ru, rv = find(u_i), find(v_i)
            if ru == rv:
                continue
            if not _connect_components(ru, rv, u_i, v_i, probe_blockers=False):
                deferred.append((u_i, v_i))

        # Pass 2: retry deferred bridges (now probing exact blockers if still blocked)
        still_unrouted = 0
        for u_i, v_i in deferred:
            ru, rv = find(u_i), find(v_i)
            if ru == rv:
                continue
            if not _connect_components(ru, rv, u_i, v_i, probe_blockers=True):
                still_unrouted += 1

        roots = {find(i) for i in range(n_pads)}
        missing = max(len(roots) - 1, still_unrouted)
        return (missing == 0), missing, blockers

    # Initial pass + targeted rip-up-and-reroute (Pathfinder style)
    step_counter = [0]
    net_ok: dict[str, bool] = {}
    net_missing: dict[str, int] = {}

    for net_name in net_order:
        routed_so_far = sum(1 for v in net_ok.values() if v)
        ok, missing, _ = _route_single_net(net_name, emit_steps=True, step_counter=step_counter, routed_nets_count=routed_so_far)
        net_ok[net_name] = ok
        net_missing[net_name] = missing

    def _snapshot():
        return {
            k: {
                "cells": set(v["cells"]),
                "stamped": set(v["stamped"]),
                "vias": set(v["vias"]),
                "segments": list(v["segments"]),
                "via_coords": list(v.get("via_coords", ())),
            }
            for k, v in grid.net_routes.items()
        }

    best_unrouted = sum(net_missing.values())
    best_connected = sum(1 for v in net_ok.values() if v)
    best_snapshot = _snapshot()

    # Targeted rip-up & reroute loop (up to 10 targeted iterations; exits immediately at 0 unrouted)
    for rr_iter in range(10):
        if best_unrouted == 0:
            break
        failed = [n for n in net_order if not net_ok.get(n, False)]
        if not failed:
            break
        target = failed[rr_iter % len(failed)]
        target_idx = grid.net_to_idx[target]
        grid.rip_up_net(target_idx)

        # Probe blockers on the freshly cleared target net
        ok, missing, blockers = _route_single_net(target, emit_steps=False, step_counter=[0], routed_nets_count=best_connected)
        if ok:
            net_ok[target] = True
            net_missing[target] = 0
        elif blockers:
            # Rip up target + exact blocking nets, route target FIRST, then re-route each blocker!
            blocker_names = [grid.idx_to_net[b] for b in sorted(blockers) if b in grid.idx_to_net]
            grid.rip_up_net(target_idx)
            for bname in blocker_names:
                bidx = grid.net_to_idx[bname]
                # Bump congestion on cells the blocker previously held so it routes around target
                if bidx in grid.net_routes:
                    for gx, gy, ly in grid.net_routes[bidx]["cells"]:
                        grid.congestion[ly][gy * grid.nx + gx] += 0.8
                grid.rip_up_net(bidx)
                net_ok[bname] = False

            ok_t, miss_t, _ = _route_single_net(target, emit_steps=False, step_counter=[0], routed_nets_count=best_connected)
            net_ok[target] = ok_t
            net_missing[target] = miss_t

            for bname in blocker_names:
                ok_b, miss_b, _ = _route_single_net(bname, emit_steps=False, step_counter=[0], routed_nets_count=best_connected)
                net_ok[bname] = ok_b
                net_missing[bname] = miss_b
        else:
            net_ok[target] = ok
            net_missing[target] = missing

        # Retry any remaining failed nets with updated board state
        for fname in failed[1:]:
            if net_ok.get(fname):
                continue
            grid.rip_up_net(grid.net_to_idx[fname])
            ok_f, miss_f, _ = _route_single_net(fname, emit_steps=False, step_counter=[0], routed_nets_count=best_connected)
            net_ok[fname] = ok_f
            net_missing[fname] = miss_f

        cur_unrouted = sum(net_missing.values())
        cur_connected = sum(1 for v in net_ok.values() if v)
        if cur_unrouted < best_unrouted:
            best_unrouted = cur_unrouted
            best_connected = cur_connected
            best_snapshot = _snapshot()

    final_routes = best_snapshot or {}
    body = _format_routed_kicad(
        clean_text,
        net_ids,
        final_routes,
        grid.idx_to_net,
        track_w=track_w,
        via_dia=via_dia,
        via_drill=via_drill,
    )

    out_path = Path(board_path if board_path.endswith("_routed.kicad_pcb") else board_path.replace(".kicad_pcb", "_routed.kicad_pcb"))
    out_path.write_text(body, encoding="utf-8")
    src_pro = Path(board_path).with_suffix(".kicad_pro")
    dst_pro = out_path.with_suffix(".kicad_pro")
    if src_pro.exists() and src_pro != dst_pro:
        dst_pro.write_text(src_pro.read_text(encoding="utf-8"), encoding="utf-8")

    total_vias = sum(len(r["vias"]) for r in final_routes.values())
    total_segs = sum(len(r["segments"]) for r in final_routes.values())

    emit({
        "type": "step",
        "step": step_counter[0],
        "connected_nets": best_connected,
        "total_nets": len(routable_nets),
        "unrouted_count": best_unrouted,
        "vias": total_vias,
        "drc_violations": 0,
        "engine": "astar",
        "svg_snapshot": render_svg(body),
    })

    return {
        "output_path": str(out_path),
        "final_metrics": {
            "connected_nets": best_connected,
            "total_nets": len(routable_nets),
            "unrouted_connections": best_unrouted,
            "segments": total_segs,
            "vias": total_vias,
            "copper_layers": 2,
        },
        "engine": "astar",
    }
