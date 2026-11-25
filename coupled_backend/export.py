import csv, io, json, math, re, shutil, subprocess, zipfile
from pathlib import Path
from . import config, sexpr as sx
from .kicad_io import count_routable_nets, parse_board, parse_board_outline, render_svg, rotate


def have_kicad() -> bool:
    return shutil.which(config.KICAD_CLI) is not None


def kicad_version() -> tuple[int, ...]:
    if not have_kicad():
        return ()
    try:
        r = subprocess.run([config.KICAD_CLI, "--version"], capture_output=True, text=True, timeout=15)
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", r.stdout or r.stderr or "")
        if m:
            return tuple(int(x) for x in m.groups() if x is not None)
    except Exception:
        pass
    return ()


def _python_drc(board: Path) -> dict:
    """Deterministic format + geometric DRC check when KiCad 8/9 kicad-cli pcb drc is unavailable."""
    text = board.read_text(encoding="utf-8", errors="ignore")
    errors: list[dict] = []
    warnings: list[dict] = []

    try:
        tree, comps, nets, bbox = parse_board(text)
    except Exception as exc:
        return {"engine": "builtin", "passed": False, "error_count": 1, "warning_count": 0,
                "unconnected_count": 0, "violations": [{"type": "parse_error", "severity": "error", "description": str(exc)}]}

    ver = sx.find(tree, "version")
    if not ver or len(ver) < 2:
        errors.append({"type": "schema", "severity": "error", "description": "Missing (version ...) header"})

    bo = parse_board_outline(tree)
    if bo is None or len(bo.outer) < 3:
        errors.append({"type": "edge_cuts", "severity": "error", "description": "Board has no closed Edge.Cuts outline"})

    # Check declared nets vs pad net ids
    declared_nets = {str(n[2]) for n in sx.find_all(tree, "net") if len(n) >= 3 and str(n[2])}
    for nname in nets:
        if nname not in declared_nets:
            errors.append({"type": "net_undeclared", "severity": "error", "description": f"Pad references undeclared net {nname!r}"})

    # Net-cap check (Gap 7)
    routable = count_routable_nets(nets)
    if routable > config.MAX_NETS:
        errors.append({"type": "net_cap", "severity": "error",
                       "description": f"Board has {routable} routable nets (> {config.MAX_NETS} checkpoint cap)"})

    # Courtyard overlap check & copper-to-edge clearance check
    rects: list[tuple[str, tuple[float, float, float, float]]] = []
    for c in comps:
        x0, y0, x1, y1 = c.local_bbox()
        pts = [rotate(a, b, c.rot) for a, b in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        rbox = (c.x + min(p[0] for p in pts), c.y + min(p[1] for p in pts),
                c.x + max(p[0] for p in pts), c.y + max(p[1] for p in pts))
        rects.append((c.ref, rbox))
        if bbox is not None:
            ox0, oy0, ow, oh = bbox
            for p in c.pads:
                dx, dy = rotate(p.x, p.y, c.rot)
                px, py = c.x + dx, c.y + dy
                a = math.radians((p.rot + c.rot) % 360)
                ehw = (p.w / 2.0) * abs(math.cos(a)) + (p.h / 2.0) * abs(math.sin(a))
                ehh = (p.w / 2.0) * abs(math.sin(a)) + (p.h / 2.0) * abs(math.cos(a))
                edge_dist = min((px - ehw) - ox0, (py - ehh) - oy0, (ox0 + ow) - (px + ehw), (oy0 + oh) - (py + ehh))
                if edge_dist < 0.19:
                    errors.append({
                        "type": "copper_edge_clearance",
                        "severity": "error",
                        "description": f"{c.ref} pad {p.num} is {edge_dist:.3f}mm from Edge.Cuts (< 0.2mm)",
                    })

    for i, (ra_ref, ra) in enumerate(rects):
        for rb_ref, rb in rects[i + 1:]:
            if min(ra[2], rb[2]) - max(ra[0], rb[0]) > 1e-3 and min(ra[3], rb[3]) - max(ra[1], rb[1]) > 1e-3:
                errors.append({
                    "type": "courtyards_overlap",
                    "severity": "error",
                    "description": f"Courtyards overlap between {ra_ref} and {rb_ref}",
                })

    return {
        "engine": "builtin",
        "passed": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "unconnected_count": 0,
        "violations": errors + warnings,
    }


def run_drc(board: Path) -> dict:
    """Run KiCad 8/9 `kicad-cli pcb drc --format json` when available, else built-in validator."""
    ver = kicad_version()
    if have_kicad() and ver >= (8, 0):
        rpt = board.with_suffix(".drc.json")
        r = subprocess.run(
            [config.KICAD_CLI, "pcb", "drc", "--format", "json", "--severity-all", "-o", str(rpt), str(board)],
            capture_output=True, text=True, timeout=60,
        )
        if rpt.exists():
            data = json.loads(rpt.read_text(encoding="utf-8"))
            viols = data.get("violations", [])
            unconn = data.get("unconnected_items", [])
            err_cnt = sum(1 for v in viols if v.get("severity") == "error")
            warn_cnt = sum(1 for v in viols if v.get("severity") == "warning")
            has_tracks = "(segment " in board.read_text(encoding="utf-8", errors="ignore")
            return {
                "engine": "kicad-cli",
                "kicad_version": ".".join(map(str, ver)),
                "passed": err_cnt == 0 and (len(unconn) == 0 or not has_tracks),
                "error_count": err_cnt,
                "warning_count": warn_cnt,
                "unconnected_count": len(unconn),
                "violations": viols,
                "unconnected_items": unconn,
            }
        raise RuntimeError(f"kicad-cli pcb drc failed (rc={r.returncode}): {r.stderr or r.stdout}")
    return _python_drc(board)


def to_svg(board: Path) -> bytes:
    if have_kicad() and kicad_version() >= (9, 0):
        out = board.with_suffix(".svg")
        r = subprocess.run([config.KICAD_CLI, "pcb", "export", "svg", "--layers", "F.Cu,B.Cu,F.SilkS,Edge.Cuts", "--page-size-mode", "2",
                            "-o", str(out), str(board)], capture_output=True)
        if r.returncode == 0 and out.exists():
            return out.read_bytes()
    return render_svg(board.read_text()).encode()


def to_gerber_zip(board: Path, bom_csv: str) -> bytes:
    if not have_kicad():
        raise RuntimeError("kicad-cli not found: gerber export runs in the worker image")
    d = board.parent / "gerbers"; d.mkdir(exist_ok=True)
    subprocess.run([config.KICAD_CLI, "pcb", "export", "gerbers", "-o", str(d) + "/", str(board)], check=True)
    subprocess.run([config.KICAD_CLI, "pcb", "export", "drill", "-o", str(d) + "/", str(board)], check=True)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in d.iterdir():
            z.write(f, f.name)
        z.writestr("bom.csv", bom_csv)
    return buf.getvalue()


def bom_csv(netlist_parts: list[dict]) -> str:
    """JLCPCB BOM format; groups identical parts."""
    groups: dict[str, dict] = {}
    for c in netlist_parts:
        g = groups.setdefault(c["mpn"], {"Comment": c.get("value") or c["mpn"], "Designator": [], "Footprint": c.get("footprint_ref", ""), "LCSC Part #": c.get("lcsc_id", "")})
        g["Designator"].append(c["ref"])
    buf = io.StringIO()
    w = csv.writer(buf); w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
    for g in groups.values():
        w.writerow([g["Comment"], ",".join(sorted(g["Designator"])), g["Footprint"], g["LCSC Part #"]])
    return buf.getvalue()
