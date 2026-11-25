"""Schematic generator: exports valid KiCad 9 (.kicad_sch) schematics and clean engineering SVG schematics."""
from __future__ import annotations

import html
import math
import uuid
from typing import Any

from .models import Part, Pin

_PIN_TYPE_TO_KICAD = {
    "power_in": "power_in",
    "power_out": "power_out",
    "ground": "power_in",
    "bidir": "bidirectional",
    "input": "input",
    "output": "output",
    "passive": "passive",
    "nc": "no_connect",
}

_GRID = 2.54  # standard KiCad 100 mil schematic grid (mm)
_PIN_PITCH = 5.08  # 200 mil vertical pin spacing (keeps pin names and net pills crisp and non-overlapping)


def _u() -> str:
    return str(uuid.uuid4())


def _snap(v: float) -> float:
    return round(v / _GRID) * _GRID


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _split_pins(pins: list[Pin]) -> tuple[list[Pin], list[Pin]]:
    """Split pins into left (inputs/power_in/ground) and right (outputs/bidir/remaining) sides."""
    if len(pins) <= 2:
        return pins[:1], pins[1:]
    left: list[Pin] = []
    right: list[Pin] = []
    for p in pins:
        if p.type in ("input", "power_in", "ground"):
            left.append(p)
        elif p.type in ("output", "power_out", "bidir"):
            right.append(p)
        else:
            (left if len(left) <= len(right) else right).append(p)
    # Rebalance if extremely lopsided
    while len(left) > len(right) + 2 and len(left) > 2:
        right.append(left.pop())
    while len(right) > len(left) + 2 and len(right) > 2:
        left.append(right.pop())
    return left, right


def _classify_column(ref: str, part: Part) -> int:
    """Assign logical left-to-right schematic flow column (0=input, 1=power, 2=IC/sensor, 3=output/misc)."""
    desc = f"{part.category} {part.description} {part.mpn}".lower()
    if ref.startswith("J"):
        return 0 if any(k in desc for k in ("usb", "power", "barrel", "battery", "vin")) or ref == "J1" else 3
    if ref.startswith("U"):
        if any(p.type == "power_out" for p in part.pins) or any(k in desc for k in ("ldo", "regulator", "buck", "boost")):
            return 1
        return 2
    if ref.startswith("C"):
        return 1 if "uf" in (part.value or "").lower() else 2
    if ref.startswith("R"):
        return 0 if "cc" in desc else 2
    return 3


def _symbol_geom(part: Part) -> tuple[list[Pin], list[Pin], float, float]:
    """Return (left_pins, right_pins, half_w_mm, half_h_mm) on KiCad 2.54mm grid."""
    left, right = _split_pins(part.pins)
    rows = max(len(left), len(right), 1)
    half_h = _snap(max(3.81, ((rows - 1) * _PIN_PITCH) / 2.0 + 3.81))
    if len(part.pins) > 2:
        max_l = max((len(p.name) for p in left), default=1)
        max_r = max((len(p.name) for p in right), default=1)
        # Ensure generous horizontal space inside box so left/right pin names never collide
        needed_w = (max_l + max_r) * 1.9 + 5.5
        half_w = _snap(max(10.16, needed_w / 2.0))
    else:
        half_w = _snap(5.08)
    return left, right, half_w, half_h


def _pin_local_positions(left: list[Pin], right: list[Pin], half_w: float, half_h: float, stub: float = 5.08):
    """Return dict[pin.num -> (pin_end_x, pin_end_y, body_x, side, angle_deg)] in KiCad symbol coords (y up)."""
    out: dict[str, tuple[float, float, float, str, int]] = {}
    for side, plist in (("L", left), ("R", right)):
        n = len(plist)
        for i, p in enumerate(plist):
            y = _snap(((n - 1) / 2.0 - i) * _PIN_PITCH)
            if side == "L":
                out[p.num] = (-(half_w + stub), y, -half_w, "L", 0)
            else:
                out[p.num] = (half_w + stub, y, half_w, "R", 180)
    return out


def _layout_schematic(parts: dict[str, Part]) -> dict[str, tuple[float, float]]:
    """Place parts in clean left-to-right schematic columns with generous label clearance (mm, y down)."""
    cols: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
    for r in sorted(parts):
        cols[_classify_column(r, parts[r])].append(r)

    col_x = {0: 66.04, 1: 152.40, 2: 241.30, 3: 325.12}
    pos: dict[str, tuple[float, float]] = {}
    for c_idx, refs in cols.items():
        y = 38.10
        for r in refs:
            _, _, _, half_h = _symbol_geom(parts[r])
            cy = _snap(y + half_h + 5.08)
            pos[r] = (col_x[c_idx], cy)
            y = cy + half_h + 17.78
    return pos


def generate_kicad_sch(parts: dict[str, Part], netlist: dict, title: str = "coupled-backend") -> str:
    """Generate a valid KiCad 9 .kicad_sch file from grounded parts and netlist."""
    pin_net = {(r, p): n for n, pins in netlist.get("nets", {}).items() for r, p in pins}
    pos = _layout_schematic(parts)

    # Deduplicate lib_symbols by MPN
    unique_mpns: dict[str, Part] = {}
    for pt in parts.values():
        unique_mpns.setdefault(pt.mpn, pt)

    out = [
        f'(kicad_sch (version 20250114) (generator "coupled-backend") (generator_version "0.1")',
        f'  (uuid "{_u()}")',
        '  (paper "A4")',
        f'  (title_block (title "{_esc(title)}") (comment 1 "Generated by coupled-backend AI PCB designer"))',
        '  (lib_symbols',
    ]

    sym_pin_maps: dict[str, dict[str, tuple[float, float, float, str, int]]] = {}
    for mpn, pt in unique_mpns.items():
        left, right, hw, hh = _symbol_geom(pt)
        pmap = _pin_local_positions(left, right, hw, hh, stub=5.08)
        sym_pin_maps[mpn] = pmap
        sym_id = f"coupled-backend:{_esc(mpn)}"
        prefix = "U"
        for r, p0 in parts.items():
            if p0.mpn == mpn:
                prefix = "".join(ch for ch in r if ch.isalpha()) or "U"
                break
        out.append(f'    (symbol "{sym_id}" (in_bom yes) (on_board yes)')
        out.append(f'      (property "Reference" "{prefix}" (at 0 {hh + 2.54:.2f} 0) (effects (font (size 1.27 1.27))))')
        out.append(f'      (property "Value" "{_esc(pt.value or pt.mpn)}" (at 0 {-(hh + 2.54):.2f} 0) (effects (font (size 1.27 1.27))))')
        out.append(f'      (property "Footprint" "{_esc(pt.footprint_ref)}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))')
        out.append(f'      (property "Datasheet" "{_esc(pt.octopart_url)}" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))')
        out.append(f'      (symbol "{_esc(mpn)}_0_1"')
        out.append(f'        (rectangle (start {-hw:.2f} {hh:.2f}) (end {hw:.2f} {-hh:.2f}) (stroke (width 0.254) (type default)) (fill (type background)))')
        out.append('      )')
        out.append(f'      (symbol "{_esc(mpn)}_1_1"')
        for p in pt.pins:
            px, py, _bx, _side, ang = pmap[p.num]
            ktype = _PIN_TYPE_TO_KICAD.get(p.type, "passive")
            out.append(
                f'        (pin {ktype} line (at {px:.2f} {py:.2f} {ang}) (length 5.08) '
                f'(name "{_esc(p.name)}" (effects (font (size 1.016 1.016)))) '
                f'(number "{_esc(p.num)}" (effects (font (size 1.016 1.016)))))'
            )
        out.append('      )')
        out.append('    )')
    out.append('  )')

    # Place symbol instances + wires + global labels
    wires_and_labels: list[str] = []
    for ref in sorted(parts):
        pt = parts[ref]
        sx_pos, sy_pos = pos[ref]
        _, _, hw, hh = _symbol_geom(pt)
        pmap = sym_pin_maps[pt.mpn]
        sym_id = f"coupled-backend:{_esc(pt.mpn)}"
        out.append(f'  (symbol (lib_id "{sym_id}") (at {sx_pos:.2f} {sy_pos:.2f} 0) (unit 1) (in_bom yes) (on_board yes) (dnp no) (uuid "{_u()}")')
        out.append(f'    (property "Reference" "{_esc(ref)}" (at {sx_pos:.2f} {sy_pos - hh - 2.54:.2f} 0) (effects (font (size 1.27 1.27))))')
        out.append(f'    (property "Value" "{_esc(pt.value or pt.mpn)}" (at {sx_pos:.2f} {sy_pos + hh + 2.54:.2f} 0) (effects (font (size 1.27 1.27))))')
        out.append(f'    (property "Footprint" "{_esc(pt.footprint_ref)}" (at {sx_pos:.2f} {sy_pos:.2f} 0) (effects (font (size 1.27 1.27)) hide))')
        out.append(f'    (property "Datasheet" "{_esc(pt.octopart_url)}" (at {sx_pos:.2f} {sy_pos:.2f} 0) (effects (font (size 1.27 1.27)) hide))')
        if pt.lcsc_id:
            out.append(f'    (property "LCSC" "{_esc(pt.lcsc_id)}" (at {sx_pos:.2f} {sy_pos:.2f} 0) (effects (font (size 1.27 1.27)) hide))')
        for p in pt.pins:
            out.append(f'    (pin "{_esc(p.num)}" (uuid "{_u()}"))')
        out.append('  )')

        for p in pt.pins:
            px, py, _bx, side, _ang = pmap[p.num]
            # In KiCad schematic coordinates, y increases DOWNWARD, whereas symbol local y is UPWARD
            wx = sx_pos + px
            wy = sy_pos - py
            net = pin_net.get((ref, p.num))
            if net:
                stub_dx = -5.08 if side == "L" else 5.08
                lx = wx + stub_dx
                rot = 180 if side == "L" else 0
                just = "right" if side == "L" else "left"
                wires_and_labels.append(
                    f'  (wire (pts (xy {wx:.2f} {wy:.2f}) (xy {lx:.2f} {wy:.2f})) (stroke (width 0) (type default)) (uuid "{_u()}"))'
                )
                wires_and_labels.append(
                    f'  (global_label "{_esc(net)}" (shape bidirectional) (at {lx:.2f} {wy:.2f} {rot}) '
                    f'(effects (font (size 1.1 1.1)) (justify {just})) (uuid "{_u()}"))'
                )
            else:
                wires_and_labels.append(f'  (no_connect (at {wx:.2f} {wy:.2f}) (uuid "{_u()}"))')

    out.extend(wires_and_labels)
    out.append(')')
    return "\n".join(out) + "\n"


def _net_color(net_name: str, net_classes: dict[str, str]) -> str:
    up = net_name.upper()
    if up in ("GND", "AGND", "DGND", "PGND", "VSS"):
        return "#374151"
    if net_classes.get(net_name) == "power" or any(k in up for k in ("VBUS", "3V3", "5V", "VDD", "VCC", "VIN", "VOUT")):
        return "#dc2626"
    return "#2563eb"


def render_schematic_svg(parts: dict[str, Part], netlist: dict, title: str = "coupled-backend") -> str:
    """Render a crisp, self-contained engineering SVG schematic with labeled pins and color-coded nets."""
    pin_net = {(r, p): n for n, pins in netlist.get("nets", {}).items() for r, p in pins}
    net_classes = netlist.get("net_classes", {})
    pos = _layout_schematic(parts)

    scale = 3.6  # mm -> px (18.3px per 5.08mm pin pitch -> clean separation for 14px net pills)
    max_x = max((x + _symbol_geom(parts[r])[2] for r, (x, _) in pos.items()), default=260.0) + 58.0
    max_y = max((y + _symbol_geom(parts[r])[3] for r, (_, y) in pos.items()), default=160.0) + 28.0
    W, H = int(max_x * scale), int(max_y * scale)

    el = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="ui-monospace, SFMono-Regular, Menlo, monospace">',
        f'<rect width="{W}" height="{H}" fill="#f8fafc"/>',
        f'<rect x="10" y="10" width="{W - 20}" height="{H - 20}" fill="none" stroke="#94a3b8" stroke-width="1.5"/>',
        f'<text x="24" y="34" font-size="16" font-weight="700" fill="#0f172a">{html.escape(title)} — Schematic</text>',
        f'<text x="24" y="52" font-size="11" fill="#475569">{len(parts)} components · {len(netlist.get("nets", {}))} nets</text>',
    ]

    for ref in sorted(parts):
        pt = parts[ref]
        sx_pos, sy_pos = pos[ref]
        left, right, hw, hh = _symbol_geom(pt)
        pmap = _pin_local_positions(left, right, hw, hh, stub=5.08)

        cx, cy = sx_pos * scale, sy_pos * scale
        bw, bh = hw * 2 * scale, hh * 2 * scale
        bx, by = cx - hw * scale, cy - hh * scale

        # Component body (KiCad classic cream body + maroon outline)
        el.append(f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="3" fill="#fef9c3" stroke="#991b1b" stroke-width="1.8"/>')
        el.append(f'<text x="{cx:.1f}" y="{by - 6:.1f}" text-anchor="middle" font-size="12" font-weight="700" fill="#0f172a">{html.escape(ref)}</text>')
        subtitle = f"{pt.value or pt.mpn}" + (f" ({pt.lcsc_id})" if pt.lcsc_id else "")
        el.append(f'<text x="{cx:.1f}" y="{by + bh + 14:.1f}" text-anchor="middle" font-size="10" fill="#334155">{html.escape(subtitle)}</text>')

        for p in pt.pins:
            px, py, body_x, side, _ = pmap[p.num]
            x_body = (sx_pos + body_x) * scale
            x_end = (sx_pos + px) * scale
            y_pin = (sy_pos - py) * scale
            net = pin_net.get((ref, p.num))
            col = _net_color(net, net_classes) if net else "#94a3b8"

            # Pin lead line
            el.append(f'<line x1="{x_body:.1f}" y1="{y_pin:.1f}" x2="{x_end:.1f}" y2="{y_pin:.1f}" stroke="{col}" stroke-width="1.6"/>')
            # Pin number (outside) and pin name (inside body)
            if side == "L":
                el.append(f'<text x="{x_body - 4:.1f}" y="{y_pin - 3:.1f}" text-anchor="end" font-size="8.5" fill="#475569">{html.escape(p.num)}</text>')
                el.append(f'<text x="{x_body + 4:.1f}" y="{y_pin + 3:.1f}" text-anchor="start" font-size="9" fill="#0f172a">{html.escape(p.name)}</text>')
            else:
                el.append(f'<text x="{x_body + 4:.1f}" y="{y_pin - 3:.1f}" text-anchor="start" font-size="8.5" fill="#475569">{html.escape(p.num)}</text>')
                el.append(f'<text x="{x_body - 4:.1f}" y="{y_pin + 3:.1f}" text-anchor="end" font-size="9" fill="#0f172a">{html.escape(p.name)}</text>')

            if net:
                # Net flag pill
                tag_w = max(28, len(net) * 6.8 + 10)
                if side == "L":
                    tx = x_end - tag_w - 4
                    el.append(f'<line x1="{x_end:.1f}" y1="{y_pin:.1f}" x2="{x_end - 4:.1f}" y2="{y_pin:.1f}" stroke="{col}" stroke-width="1.6"/>')
                    el.append(f'<rect x="{tx:.1f}" y="{y_pin - 7:.1f}" width="{tag_w:.1f}" height="14" rx="3" fill="{col}"/>')
                    el.append(f'<text x="{tx + tag_w / 2:.1f}" y="{y_pin + 3.2:.1f}" text-anchor="middle" font-size="9" font-weight="600" fill="#ffffff">{html.escape(net)}</text>')
                else:
                    tx = x_end + 4
                    el.append(f'<line x1="{x_end:.1f}" y1="{y_pin:.1f}" x2="{tx:.1f}" y2="{y_pin:.1f}" stroke="{col}" stroke-width="1.6"/>')
                    el.append(f'<rect x="{tx:.1f}" y="{y_pin - 7:.1f}" width="{tag_w:.1f}" height="14" rx="3" fill="{col}"/>')
                    el.append(f'<text x="{tx + tag_w / 2:.1f}" y="{y_pin + 3.2:.1f}" text-anchor="middle" font-size="9" font-weight="600" fill="#ffffff">{html.escape(net)}</text>')
            else:
                # X mark for no-connect
                el.append(f'<line x1="{x_end - 3:.1f}" y1="{y_pin - 3:.1f}" x2="{x_end + 3:.1f}" y2="{y_pin + 3:.1f}" stroke="#94a3b8" stroke-width="1.2"/>')
                el.append(f'<line x1="{x_end - 3:.1f}" y1="{y_pin + 3:.1f}" x2="{x_end + 3:.1f}" y2="{y_pin - 3:.1f}" stroke="#94a3b8" stroke-width="1.2"/>')

    el.append("</svg>")
    return "".join(el)
