"""End-to-end AI PCB Design Orchestrator & Conversational Assistant.

Connects the Gemini LLM across every layer of coupled-backend:
  Stage 0: Natural-language requirements & architecture brief
  Stage 1: Hybrid component selection (Local PartsDB + Octopart/Nexar + KiCad libraries + LLM grounding)
  Stage 2: Grounded netlist synthesis with closed-loop ERC + KiCad 9 schematic (.kicad_sch + SVG)
  Stage 3: LLM-guided placement with real .kicad_mod footprints + self-healing board sizing
  Stage 4: Routing (PCBWorld / Mock) with live SSE streaming
  Stage 5: DRC validation + LLM engineering design review + multi-turn conversational refinement
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Callable
import httpx

from . import config, engines, export, libraries, llm, octopart, schematic
from .kicad_io import Comp, kicad_pro, make_comp, render_svg, write_unrouted_kicad_pcb
from .models import Part, Pin
from .netlist import DEMO_PLAN, generate_and_verify_netlist, is_demo_prompt
from .parts import PartsDB
from .placer import heuristic_rules, solve_coordinates
from .store import Project

logger = logging.getLogger(__name__)

EmitFn = Callable[[dict[str, Any]], None]


def _slugify_project_id(prompt: str) -> str:
    words = re.findall(r"[a-z0-9]+", prompt.lower())[:4]
    base = "_".join(words) if words else "pcb"
    return f"{base}_{uuid.uuid4().hex[:6]}"


def _catalog_summary(db: PartsDB, limit: int = 30) -> list[dict[str, Any]]:
    """Compact summary of netlist-ready parts already in local PartsDB for LLM awareness."""
    rows = db.search("", {"in_stock": True}, k=limit, require_pins=True, engine="local")
    return [
        {
            "mpn": p.mpn,
            "lcsc_id": p.lcsc_id,
            "category": p.category,
            "value": p.value,
            "package": p.package,
            "description": p.description[:90],
        }
        for p in rows
    ]


def _footprint_is_renderable(fp_ref: str, lcsc_id: str = "") -> bool:
    if not fp_ref:
        return False
    try:
        comp = make_comp("TEST", "TEST", fp_ref, lcsc_id=lcsc_id)
        return len(comp.pads) > 0
    except Exception:
        return False


def _ensure_part_pins_via_llm(part: Part) -> Part:
    """If an Octopart part has a valid footprint but no KiCad symbol pin table, ask Gemini for its pinout."""
    if part.pins or not llm.available() or not part.footprint_ref:
        return part
    try:
        comp = make_comp("U1", part.value or part.mpn, part.footprint_ref, mpn=part.mpn, lcsc_id=part.lcsc_id)
    except Exception:
        return part
    pad_nums = sorted({p.num for p in comp.pads if p.kind != "np_thru_hole"})
    if not pad_nums:
        return part
    try:
        res = llm.complete_json(
            "You are an electronics datasheet expert. Provide the pin table for this component matching ONLY the given pad numbers. "
            'Output {"pins":[{"num":"1","name":"VIN","type":"power_in|power_out|ground|passive|input|output|bidir|nc"}]}.',
            json.dumps({
                "mpn": part.mpn,
                "manufacturer": part.manufacturer,
                "category": part.category,
                "package": part.package,
                "description": part.description,
                "pad_numbers": pad_nums,
            }),
        )
        raw_pins = (res or {}).get("pins") or []
        valid_types = {"power_in", "power_out", "ground", "passive", "input", "output", "bidir", "nc"}
        pins = [
            Pin(num=str(x["num"]), name=str(x.get("name") or x["num"]),
                type=x["type"] if x.get("type") in valid_types else "passive")
            for x in raw_pins
            if str(x.get("num")) in pad_nums
        ]
        if pins:
            return part.model_copy(update={"pins": pins})
    except Exception as exc:
        logger.warning("LLM pinout inference failed for %s: %s", part.mpn, exc)
    return part


# ==============================================================================
# Stage 0: LLM Requirements & Architecture Planning
# ==============================================================================

def plan_architecture(
    prompt: str,
    db: PartsDB,
    *,
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
) -> dict[str, Any]:
    """Decompose natural language user need into architecture, board dimensions, and part specs."""
    if not llm.available():
        if not is_demo_prompt(prompt):
            raise ValueError("No GEMINI_API_KEY set: only the built-in USB-C/BME280 demo prompt works offline.")
        return {
            "board_name": "USB-C BME280 Sensor Board",
            "summary": "Offline deterministic demo: USB-C 6P power input -> 3.3V AP2112K LDO -> Bosch BME280 I2C environmental sensor + 1x4 breakout header.",
            "board_width_mm": board_width_mm or 40.0,
            "board_height_mm": board_height_mm or 30.0,
            "power_rails": ["VBUS (5V)", "3V3 (3.3V)", "GND"],
            "interfaces": ["USB-C Power", "I2C (4-pin header)"],
            "placement_notes": "Place USB-C and I2C header on board edges; place decoupling capacitors immediately adjacent to U1 and U2.",
            "part_specs": [{"ref": r, "preferred_mpn": m, "role": m, "query": m, "filters": {}} for r, m in DEMO_PLAN],
            "mode": "offline_demo",
        }

    catalog = _catalog_summary(db)
    sys_prompt = (
        "You are a senior PCB design engineer designing a 2-layer manufacturable PCB from a user's natural language request.\n"
        "Decompose the request into a complete engineering architecture and Bill of Materials.\n"
        "Prefer parts from the verified local catalog when they meet the user's requirements, OR specify real standard MPNs/queries "
        "(which will be looked up live on Octopart and matched to KiCad symbols/footprints).\n"
        "Rules:\n"
        f"- Keep the board practical: 5 to 15 routable nets on 2 layers (<= 15 parts, strictly <= {config.MAX_NETS} nets).\n"
        "- Always include necessary decoupling capacitors (10uF bulk + 100nF bypass) for every IC/regulator, "
        "5.1k CC pull-down resistors for USB-C power receptacles, and current-limiting resistors for LEDs.\n"
        "- IMPORTANT: Each entry in `part_specs` MUST have exactly ONE single reference designator (e.g. separate entries for 'R1' and 'R2', NEVER grouped like 'R1, R2' or 'C1-C3').\n"
        "- Choose reasonable board dimensions in mm (e.g. 40x30mm to 65x50mm).\n"
        "Output JSON:\n"
        '{"board_name":"short title","summary":"2-3 sentence architecture explanation",'
        '"board_width_mm":40.0,"board_height_mm":30.0,'
        '"power_rails":["VBUS (5V)","3V3","GND"],"interfaces":["I2C"],'
        '"placement_notes":"key placement guidelines",'
        '"part_specs":[{"ref":"U1","role":"3.3V LDO regulator","preferred_mpn":"AP2112K-3.3TRG1",'
        '"query":"3.3V low noise LDO regulator SOT-23-5","filters":{"category":"","package":"","v_in_min":0,"value":""}}]}'
    )
    user_msg = json.dumps({
        "user_request": prompt,
        "requested_dimensions_mm": {"width": board_width_mm, "height": board_height_mm},
        "verified_local_catalog": catalog,
        "octopart_live_search_available": octopart.available(),
    })
    brief = llm.complete_json(sys_prompt, user_msg)
    if not isinstance(brief, dict) or not brief.get("part_specs"):
        raise ValueError("LLM failed to produce a valid architecture plan with part_specs")

    brief["board_width_mm"] = float(board_width_mm or brief.get("board_width_mm") or 40.0)
    brief["board_height_mm"] = float(board_height_mm or brief.get("board_height_mm") or 30.0)
    brief["mode"] = "llm"
    return brief


# ==============================================================================
# Stage 1: Hybrid Component Selection (Local DB + Octopart/Nexar + KiCad Libs)
# ==============================================================================

def _pick_single_part(spec: dict[str, Any], db: PartsDB, *, prefer_octopart: bool = False) -> tuple[Part | None, str]:
    """Resolve one part_spec into a grounded, netlist-ready Part with valid pins & footprint."""
    pref_mpn = (spec.get("preferred_mpn") or "").strip()
    query = (spec.get("query") or spec.get("role") or pref_mpn).strip()
    raw_filters = spec.get("filters") or {}
    filters = {k: v for k, v in raw_filters.items() if v not in ("", 0, None)}
    filters["in_stock"] = True

    # 1. Exact preferred MPN in local DB (fastest + verified)
    if pref_mpn and not prefer_octopart:
        p = db.get_exact_ci(pref_mpn)
        if p and p.pins and _footprint_is_renderable(p.footprint_ref, p.lcsc_id):
            return p, "local_exact"

    # 2. Local vector + SQL search
    local_hits = db.search(query, filters, k=3, require_pins=True, engine="local")
    if not local_hits:
        local_hits = db.search(query, {"in_stock": True}, k=3, require_pins=True, engine="local")

    best_local = next(
        (h for h in local_hits if _footprint_is_renderable(h.footprint_ref, h.lcsc_id)),
        None,
    )
    local_score = float(best_local.score or 0.0) if best_local else 0.0

    # Strong local hit (unless Octopart is explicitly preferred or a specific non-local MPN was requested)
    if best_local and not prefer_octopart and not pref_mpn and local_score >= 0.35:
        return best_local, "local_search"

    # 3. Live Octopart / Nexar lookup + KiCad library enrichment
    if octopart.available():
        octo_cands: list[Part] = []
        if pref_mpn:
            direct = octopart.get_octopart_part(pref_mpn, local_db=db)
            if direct:
                octo_cands.append(direct)
        if not octo_cands:
            octo_cands = octopart.search_octopart(query, filters, limit=5, local_db=db)

        for cand in octo_cands:
            enriched = libraries.enrich_part(cand)
            if not enriched.pins and _footprint_is_renderable(enriched.footprint_ref, enriched.lcsc_id):
                enriched = _ensure_part_pins_via_llm(enriched)
            if enriched.pins and _footprint_is_renderable(enriched.footprint_ref, enriched.lcsc_id):
                try:
                    db.upsert([enriched])
                except Exception:
                    pass
                return enriched, "octopart"

    # 4. Fallback to best local hit if available
    if best_local is not None:
        return best_local, "local_fallback"

    return None, "not_found"


def _expand_refs(raw_ref: str) -> list[str]:
    """Expand LLM-grouped designators like 'R1, R2' or 'C1-C3' into individual refs."""
    out: list[str] = []
    for tok in re.split(r"[,;/\s]+", str(raw_ref or "").strip()):
        tok = tok.strip()
        if not tok:
            continue
        m = re.match(r"^([A-Za-z]+)(\d+)-([A-Za-z]*)(\d+)$", tok)
        if m:
            pfx, start_n, pfx2, end_n = m.group(1), int(m.group(2)), m.group(3) or m.group(1), int(m.group(4))
            if pfx == pfx2 and end_n >= start_n and (end_n - start_n) <= 32:
                out.extend(f"{pfx}{i}" for i in range(start_n, end_n + 1))
                continue
        out.append(tok)
    return out


def select_components(
    brief: dict[str, Any],
    db: PartsDB,
    *,
    prefer_octopart: bool = False,
) -> tuple[list[tuple[str, Part]], list[dict[str, Any]]]:
    """Resolve all part_specs in an architecture brief into grounded (ref, Part) tuples + sourcing log."""
    selected: list[tuple[str, Part]] = []
    sourcing_log: list[dict[str, Any]] = []
    seen_refs: set[str] = set()

    for spec in brief.get("part_specs", []):
        refs = [r for r in _expand_refs(spec.get("ref", "")) if r not in seen_refs]
        if not refs:
            continue
        part, source = _pick_single_part(spec, db, prefer_octopart=prefer_octopart)
        for ref in refs:
            if ref in seen_refs:
                continue
            if part is not None:
                seen_refs.add(ref)
                selected.append((ref, part))
                sourcing_log.append({
                    "ref": ref,
                    "role": spec.get("role", ""),
                    "mpn": part.mpn,
                    "lcsc_id": part.lcsc_id,
                    "footprint_ref": part.footprint_ref,
                    "source": source,
                    "price": part.price,
                    "stock": part.stock,
                    "octopart_url": part.octopart_url,
                })
            else:
                sourcing_log.append({
                    "ref": ref,
                    "role": spec.get("role", ""),
                    "mpn": None,
                    "source": "not_found",
                    "query": spec.get("query", ""),
                })

    if not selected:
        raise ValueError("Could not resolve any manufacturable parts for the design prompt.")
    return selected, sourcing_log


# ==============================================================================
# Stage 2, 3, 4, 5 Orchestration (Synchronous & SSE Streaming)
# ==============================================================================

def _place_with_auto_resize(
    comps: list[Comp],
    nets: dict[str, list[tuple[str, str]]],
    rules: dict[str, Any],
    width_mm: float,
    height_mm: float,
    iterations: int,
    seed: int,
) -> tuple[dict[str, tuple[float, float, float]], dict[str, Any], tuple[float, float, float, float]]:
    """Run placement and automatically expand board outline if courtyards still overlap."""
    w, h = float(width_mm), float(height_mm)
    pos, metrics = {}, {}
    outline = (0.0, 0.0, w, h)
    for _attempt in range(3):
        outline = (0.0, 0.0, round(w, 1), round(h, 1))
        pos, metrics = solve_coordinates(comps, nets, rules, outline, iterations, seed)
        if metrics["overlaps"] == 0:
            break
        w *= 1.25
        h *= 1.25
    return pos, metrics, outline


def _generate_design_review(
    prompt: str,
    brief: dict[str, Any],
    plan: list[tuple[str, Part]],
    nl: dict[str, Any],
    erc_status: str,
    erc_warnings: list[str],
    placement_metrics: dict[str, Any],
    routing_result: dict[str, Any] | None,
    drc_report: dict[str, Any],
) -> str:
    """Produce a concise engineering review summarizing the completed PCB design."""
    total_cost = round(sum(p.price for _, p in plan), 4)
    fallback_summary = (
        f"Designed '{brief.get('board_name', 'PCB')}' ({brief.get('board_width_mm')}x{brief.get('board_height_mm')}mm) "
        f"with {len(plan)} verified components (~${total_cost:.2f} unit BOM) and {len(nl.get('nets', {}))} nets. "
        f"ERC: {erc_status}. Placement HPWL: {placement_metrics.get('hpwl_mm')}mm (overlaps: {placement_metrics.get('overlaps')}). "
        f"DRC: {'PASS' if drc_report.get('passed') else 'ISSUES'} ({drc_report.get('error_count', 0)} errors)."
    )
    if not llm.available():
        return fallback_summary
    try:
        review = llm.complete_text(
            "You are a senior PCB engineer reviewing an AI-generated board before fabrication. "
            "Write a clear, concise 4-6 bullet engineering summary covering: architecture & component rationale, "
            "ERC/netlist status, placement quality, routing & DRC status, and any manufacturing notes.",
            json.dumps({
                "prompt": prompt,
                "board_name": brief.get("board_name"),
                "dimensions_mm": [brief.get("board_width_mm"), brief.get("board_height_mm")],
                "components": [{"ref": r, "mpn": p.mpn, "lcsc": p.lcsc_id, "val": p.value, "price": p.price} for r, p in plan],
                "bom_unit_cost_usd": total_cost,
                "nets": list(nl.get("nets", {}).keys()),
                "erc_status": erc_status,
                "erc_warnings": erc_warnings,
                "placement_metrics": placement_metrics,
                "routing_metrics": (routing_result or {}).get("final_metrics"),
                "drc": {"passed": drc_report.get("passed"), "errors": drc_report.get("error_count"), "warnings": drc_report.get("warning_count")},
            }),
        )
        return review or fallback_summary
    except Exception as exc:
        logger.warning("LLM review generation failed: %s", exc)
        return fallback_summary


def run_design_pipeline(
    prompt: str,
    db: PartsDB,
    *,
    project_id: str | None = None,
    owner_user_id: str | None = None,
    board_width_mm: float | None = None,
    board_height_mm: float | None = None,
    auto_route: bool = True,
    prefer_octopart: bool = False,
    iterations: int = 2500,
    checkpoint: str = "d2/ppo",
    max_steps: int = 300,
    seed: int = 0,
    emit: EmitFn | None = None,
    preset_brief: dict[str, Any] | None = None,
    preset_plan: list[tuple[str, Part]] | None = None,
    preset_rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute the complete AI PCB design pipeline from natural language to routed & DRC-checked board."""
    def _emit(ev: dict[str, Any]) -> None:
        if emit is not None:
            emit(ev)

    pid = project_id or _slugify_project_id(prompt)
    prj = Project(pid, owner_user_id=owner_user_id)

    # --- Stage 0: Requirements & Architecture ---
    _emit({"type": "stage", "stage": "requirements", "status": "running", "project_id": pid})
    brief = preset_brief or plan_architecture(prompt, db, board_width_mm=board_width_mm, board_height_mm=board_height_mm)
    if board_width_mm is not None:
        brief["board_width_mm"] = float(board_width_mm)
    if board_height_mm is not None:
        brief["board_height_mm"] = float(board_height_mm)
    prj.save_json("design_brief.json", {"prompt": prompt, **brief})
    _emit({"type": "stage", "stage": "requirements", "status": "done", "brief": brief})

    # --- Stage 1: Component Selection ---
    _emit({"type": "stage", "stage": "components", "status": "running"})
    if preset_plan is not None:
        plan = preset_plan
        sourcing_log = [
            {"ref": r, "mpn": p.mpn, "lcsc_id": p.lcsc_id, "footprint_ref": p.footprint_ref,
             "source": "preset", "price": p.price, "stock": p.stock, "octopart_url": p.octopart_url}
            for r, p in plan
        ]
    else:
        plan, sourcing_log = select_components(brief, db, prefer_octopart=prefer_octopart)
    parts_dict = dict(plan)
    prj.save_json("parts.json", {r: p.model_dump() for r, p in plan})
    prj.save_json("sourcing.json", sourcing_log)
    _emit({"type": "stage", "stage": "components", "status": "done", "count": len(plan), "components": sourcing_log})

    # --- Stage 2: Netlist + ERC + KiCad Schematic ---
    _emit({"type": "stage", "stage": "netlist", "status": "running"})
    nl, erc_status, erc_warnings = generate_and_verify_netlist(prompt, parts_dict, retries=3)
    nl["components"] = [
        {"ref": r, **{k: getattr(p, k) for k in ("mpn", "lcsc_id", "value", "footprint_ref", "package")}}
        for r, p in plan
    ]
    prj.save_json("netlist.json", nl)

    sch_text = schematic.generate_kicad_sch(parts_dict, nl, title=brief.get("board_name") or pid)
    sch_svg = schematic.render_schematic_svg(parts_dict, nl, title=brief.get("board_name") or pid)
    prj.save_text("board.kicad_sch", sch_text)
    prj.save_text("schematic.svg", sch_svg)
    _emit({
        "type": "stage", "stage": "schematic", "status": "done",
        "erc_status": erc_status, "erc_warnings": erc_warnings,
        "net_count": len(nl.get("nets", {})), "schematic_svg": sch_svg,
    })

    # --- Stage 3: LLM-Guided Placement ---
    _emit({"type": "stage", "stage": "placement", "status": "running"})
    comps = [make_comp(r, p.value or p.mpn, p.footprint_ref, mpn=p.mpn, lcsc_id=p.lcsc_id) for r, p in plan]
    nets_tuples = {n: [tuple(x) for x in pins] for n, pins in nl["nets"].items()}
    ptypes = {(r, pn.num): pn.type for r, pt in parts_dict.items() for pn in pt.pins}
    rules = heuristic_rules(comps, nets_tuples, ptypes)
    if preset_rules:
        rules = {
            "edge_components": sorted(set(rules["edge_components"]) | set(preset_rules.get("edge_components", []))),
            "clusters": preset_rules.get("clusters") or rules["clusters"],
        }
    elif llm.available():
        try:
            extra = llm.complete_json(
                'Given this PCB architecture and netlist, return placement rules as '
                '{"edge_components":[refs],"clusters":[[anchor_ic, passive, ...]]}. '
                "Put external connectors/LEDs on edge_components; group decoupling caps & pull-ups with their IC.",
                json.dumps({
                    "placement_notes": brief.get("placement_notes", ""),
                    "nets": nl["nets"],
                    "refs": {r: f"{p.category}:{p.value}" for r, p in plan},
                }),
            )
            if isinstance(extra, dict):
                rules = {
                    "edge_components": sorted(set(rules["edge_components"]) | set(extra.get("edge_components", []))),
                    "clusters": rules["clusters"] or extra.get("clusters", []),
                }
        except Exception as exc:
            logger.warning("LLM placement rules skipped: %s", exc)

    pos, place_metrics, outline = _place_with_auto_resize(
        comps, nets_tuples, rules, brief["board_width_mm"], brief["board_height_mm"], iterations, seed
    )
    brief["board_width_mm"], brief["board_height_mm"] = outline[2], outline[3]
    prj.save_json("design_brief.json", {"prompt": prompt, **brief})

    unrouted_pcb = write_unrouted_kicad_pcb(comps, pos, nets_tuples, outline)
    board_path = prj.save_text("board.kicad_pcb", unrouted_pcb)
    prj.save_text("board.kicad_pro", json.dumps(kicad_pro(nl.get("net_classes", {})), indent=2))
    prj.delete_file("board_routed.kicad_pcb")
    placements = [{"ref": r, "x": round(x, 3), "y": round(y, 3), "rotation": rot} for r, (x, y, rot) in pos.items()]
    prj.save_json("placement.json", placements)
    placed_svg = render_svg(unrouted_pcb)
    _emit({
        "type": "stage", "stage": "placement", "status": "done",
        "board_mm": [outline[2], outline[3]],
        "hpwl_mm": place_metrics["hpwl_mm"], "overlaps": place_metrics["overlaps"],
        "pcb_svg": placed_svg,
    })

    # --- Stage 4: Routing (proxy to worker container when WORKER_URL is set) ---
    routing_result: dict[str, Any] | None = None
    if auto_route:
        _emit({"type": "stage", "stage": "routing", "status": "running"})
        if config.WORKER_URL:
            jid = f"job_{pid}_{uuid.uuid4().hex[:6]}"
            r = httpx.post(
                f"{config.WORKER_URL}/jobs",
                json={"job_id": jid, "board_path": str(board_path), "checkpoint": checkpoint, "max_steps": max_steps},
                timeout=30,
            )
            r.raise_for_status()
            jid = r.json()["job_id"]
            with httpx.Client(timeout=None) as wc:
                with wc.stream("GET", f"{config.WORKER_URL}/jobs/{jid}/stream") as s:
                    for line in s.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        ev = json.loads(line[5:])
                        if ev.get("type") == "done":
                            routing_result = {k: v for k, v in ev.items() if k != "type"}
                        elif ev.get("type") == "error":
                            raise RuntimeError(f"Worker routing failed: {ev.get('error')}")
                        else:
                            _emit({**ev, "type": "routing_step"})
        else:
            routing_result = engines.run_job(
                str(board_path), checkpoint, max_steps,
                lambda ev: _emit({**ev, "type": "routing_step"}),
            )
        prj.sync_from_disk("board_routed.kicad_pcb")
        _emit({"type": "stage", "stage": "routing", "status": "done", "routing": routing_result})

    # --- Stage 5: DRC (via KiCad 9 worker when available) & Engineering Review ---
    _emit({"type": "stage", "stage": "drc", "status": "running"})
    active_board = prj.board() or board_path
    drc_report = None
    if config.WORKER_URL:
        try:
            dr = httpx.post(f"{config.WORKER_URL}/drc", json={"board_path": str(active_board)}, timeout=60)
            if dr.status_code == 200:
                drc_report = dr.json()
        except Exception as exc:
            logger.warning("Worker DRC fallback to local builtin: %s", exc)
    if drc_report is None:
        drc_report = export.run_drc(active_board)
    prj.save_json("drc.json", drc_report)
    pcb_svg = export.to_svg(active_board).decode("utf-8", errors="ignore")
    bom = export.bom_csv([{"ref": r, **p.model_dump()} for r, p in plan])
    _emit({"type": "stage", "stage": "drc", "status": "done", "drc": drc_report})

    review = _generate_design_review(
        prompt, brief, plan, nl, erc_status, erc_warnings, place_metrics, routing_result, drc_report
    )
    prj.save_json("design_review.json", {"review": review})

    # Seed project conversation history
    history = prj.load_json("chat_history.json", [])
    history.append({"role": "user", "content": prompt})
    history.append({"role": "assistant", "content": review})
    prj.save_json("chat_history.json", history)

    result = {
        "project_id": pid,
        "board_name": brief.get("board_name", pid),
        "summary": brief.get("summary", ""),
        "review": review,
        "board_mm": {"width": outline[2], "height": outline[3]},
        "components": sourcing_log,
        "bom_unit_cost_usd": round(sum(p.price for _, p in plan), 4),
        "netlist": nl,
        "erc_status": erc_status,
        "erc_warnings": erc_warnings,
        "placements": placements,
        "placement_metrics": place_metrics,
        "routing": routing_result,
        "drc": drc_report,
        "schematic_svg": sch_svg,
        "pcb_svg": pcb_svg,
        "bom_csv": bom,
        "exports": {
            "kicad_pcb": f"/api/v1/projects/{pid}/export?format=kicad_pcb",
            "kicad_sch": f"/api/v1/projects/{pid}/export?format=kicad_sch",
            "schematic_svg": f"/api/v1/projects/{pid}/export?format=schematic_svg",
            "pcb_svg": f"/api/v1/projects/{pid}/export?format=svg",
            "bom": f"/api/v1/projects/{pid}/export?format=bom",
            "gerber": f"/api/v1/projects/{pid}/export?format=gerber",
            "drc": f"/api/v1/projects/{pid}/drc",
        },
    }
    _emit({"type": "done", "project_id": pid, "result": result})
    return result


# ==============================================================================
# Conversational Iterative Design Assistant (`POST /api/v1/projects/{pid}/chat`)
# ==============================================================================

def chat_with_project(
    pid: str,
    message: str,
    db: PartsDB,
    *,
    owner_user_id: str | None = None,
    auto_route: bool = True,
    iterations: int = 2500,
    max_steps: int = 300,
    checkpoint: str = "d2/ppo",
) -> dict[str, Any]:
    """Conversational AI assistant over an existing PCB project (Q&A + targeted iterative edits)."""
    prj = Project(pid, owner_user_id=owner_user_id)
    loaded = prj.load_json_files([
        "design_brief.json",
        "parts.json",
        "netlist.json",
        "placement.json",
        "drc.json",
        "chat_history.json",
    ])
    brief = loaded.get("design_brief.json")
    raw_parts = loaded.get("parts.json")
    nl = loaded.get("netlist.json")
    placements = loaded.get("placement.json", [])
    drc = loaded.get("drc.json", {})
    history = loaded.get("chat_history.json", [])

    if not raw_parts and not nl:
        # Project does not exist yet: treat this first message as a brand-new design request!
        res = run_design_pipeline(
            message, db, project_id=pid, owner_user_id=owner_user_id, auto_route=auto_route,
            iterations=iterations, checkpoint=checkpoint, max_steps=max_steps,
        )
        return {"project_id": pid, "action": "new_design", "reply": res["review"], "design": res}

    parts = {r: Part(**p) for r, p in raw_parts.items()}

    if not llm.available():
        # Deterministic offline command parsing for resize / re-route / status inspection
        m_size = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)", message.lower())
        if m_size:
            nw, nh = float(m_size.group(1)), float(m_size.group(2))
            res = run_design_pipeline(
                (brief or {}).get("prompt", message), db, project_id=pid, owner_user_id=owner_user_id,
                board_width_mm=nw, board_height_mm=nh, auto_route=auto_route,
                iterations=iterations, checkpoint=checkpoint, max_steps=max_steps,
                preset_brief=brief, preset_plan=list(parts.items()),
            )
            reply = f"Resized board to {nw:g}x{nh:g}mm, re-placed, and re-verified DRC ({res['drc'].get('error_count', 0)} errors)."
            return {"project_id": pid, "action": "update_board_size", "reply": reply, "design": res}
        reply = (
            f"Project '{pid}' currently has {len(parts)} components ({', '.join(sorted(parts))}) and "
            f"{len((nl or {}).get('nets', {}))} nets. Set GEMINI_API_KEY for full natural-language Q&A and iterative edits."
        )
        history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply}])
        prj.save_json("chat_history.json", history)
        return {"project_id": pid, "action": "answer_only", "reply": reply, "design": None}

    # Build rich context for Gemini assistant
    state_ctx = {
        "project_id": pid,
        "design_brief": brief,
        "parts": {r: {"mpn": p.mpn, "lcsc": p.lcsc_id, "value": p.value, "category": p.category, "package": p.package} for r, p in parts.items()},
        "nets": (nl or {}).get("nets", {}),
        "placements": placements,
        "drc": {"passed": drc.get("passed"), "error_count": drc.get("error_count"), "violations": drc.get("violations", [])[:5]},
        "verified_local_catalog": _catalog_summary(db),
    }
    sys_prompt = (
        "You are coupled-backend's AI PCB design assistant helping a user inspect or iteratively modify an existing PCB project.\n"
        "Decide the minimal action needed to fulfill the user's message:\n"
        '- "answer_only": user is asking a question / explanation about the existing design (no board changes)\n'
        '- "update_board_size": change board_width_mm / board_height_mm and re-place/re-route\n'
        '- "re_place": adjust placement_rules (edge_components / clusters) and re-place/re-route\n'
        '- "add_or_swap_parts": add/replace/remove components (provide add_parts and/or remove_refs + revised_prompt) and rebuild schematic/netlist/placement/routing\n'
        '- "full_redesign": major architecture change from scratch (provide revised_prompt)\n'
        "Output JSON:\n"
        '{"action":"answer_only|update_board_size|re_place|add_or_swap_parts|full_redesign",'
        '"reply":"clear engineering response to the user",'
        '"board_width_mm":null,"board_height_mm":null,'
        '"remove_refs":[],"add_parts":[{"ref":"D1","role":"power LED","preferred_mpn":"","query":"green 0603 LED","filters":{}}],'
        '"placement_rules":null,"revised_prompt":null}'
    )
    convo = history[-6:] + [
        {"role": "user", "content": f"Current project state:\n{json.dumps(state_ctx)}\n\nUser message: {message}"}
    ]
    decision = llm.chat_json(sys_prompt, convo) or {"action": "answer_only", "reply": "No changes made."}
    action = decision.get("action", "answer_only")
    reply = decision.get("reply") or "Updated the design."

    updated_design = None
    orig_prompt = (brief or {}).get("prompt", "")
    revised_prompt = decision.get("revised_prompt") or f"{orig_prompt}. Modification: {message}"

    if action == "update_board_size":
        nw = decision.get("board_width_mm") or (brief or {}).get("board_width_mm", 40.0)
        nh = decision.get("board_height_mm") or (brief or {}).get("board_height_mm", 30.0)
        updated_design = run_design_pipeline(
            orig_prompt or message, db, project_id=pid, owner_user_id=owner_user_id,
            board_width_mm=float(nw), board_height_mm=float(nh),
            auto_route=auto_route, iterations=iterations, checkpoint=checkpoint, max_steps=max_steps,
            preset_brief=brief, preset_plan=list(parts.items()),
        )
    elif action == "re_place":
        updated_design = run_design_pipeline(
            orig_prompt or message, db, project_id=pid, owner_user_id=owner_user_id,
            board_width_mm=decision.get("board_width_mm") or (brief or {}).get("board_width_mm"),
            board_height_mm=decision.get("board_height_mm") or (brief or {}).get("board_height_mm"),
            auto_route=auto_route, iterations=iterations, checkpoint=checkpoint, max_steps=max_steps,
            preset_brief=brief, preset_plan=list(parts.items()),
            preset_rules=decision.get("placement_rules"),
        )
    elif action == "add_or_swap_parts":
        updated_parts = {r: p for r, p in parts.items() if r not in set(decision.get("remove_refs") or [])}
        for spec in (decision.get("add_parts") or []):
            ref = str(spec.get("ref") or "").strip()
            if not ref:
                continue
            part, _src = _pick_single_part(spec, db)
            if part is not None:
                updated_parts[ref] = part
        updated_design = run_design_pipeline(
            revised_prompt, db, project_id=pid, owner_user_id=owner_user_id,
            board_width_mm=decision.get("board_width_mm") or (brief or {}).get("board_width_mm"),
            board_height_mm=decision.get("board_height_mm") or (brief or {}).get("board_height_mm"),
            auto_route=auto_route, iterations=iterations, checkpoint=checkpoint, max_steps=max_steps,
            preset_brief=brief, preset_plan=list(updated_parts.items()),
        )
    elif action == "full_redesign":
        updated_design = run_design_pipeline(
            revised_prompt, db, project_id=pid, owner_user_id=owner_user_id,
            board_width_mm=decision.get("board_width_mm"),
            board_height_mm=decision.get("board_height_mm"),
            auto_route=auto_route, iterations=iterations, checkpoint=checkpoint, max_steps=max_steps,
        )

    # Persist chat turn cleanly
    history = prj.load_json("chat_history.json", [])
    if not updated_design:
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})
        prj.save_json("chat_history.json", history)

    return {"project_id": pid, "action": action, "reply": reply, "design": updated_design}
