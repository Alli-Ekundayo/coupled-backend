"""Headless FastAPI & AI PCB Design orchestrator. Run: uvicorn coupled_backend.api:app --reload"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse

from . import auth, config, db as pgdb, designer, engines, export, llm, schematic, sexpr as sx
from .ingest import seed_db
from .jobs import JobHub
from .kicad_io import (
    apply_placement_scraped,
    kicad_pro,
    make_comp,
    parse_board,
    parse_board_outline,
    render_svg,
    write_unrouted_kicad_pcb,
)
from .models import ChatReq, DesignReq, NetlistReq, Part, PartSearchReq, RouteReq
from .netlist import generate_and_verify_netlist
from .parts import PartsDB, PostgresPartsDB, create_parts_db, make_embedder
from .placer import heuristic_rules, solve_coordinates
from .store import Project, list_all_projects

hub = JobHub()
db: PartsDB | PostgresPartsDB


@asynccontextmanager
async def lifespan(_):
    global db
    pgdb.init_db()
    db = create_parts_db(make_embedder(config.EMBEDDER))
    if db.count() == 0:
        seed_db(db)
    try:
        yield
    finally:
        pgdb.close_db()


app = FastAPI(
    title="coupled-backend API",
    version="0.1.0",
    description=(
        "Headless AI PCB design backend: natural-language architecture planning, hybrid part sourcing "
        "(Lakebase Postgres / SQLite PartsDB + Octopart/Nexar), grounded netlist + ERC, KiCad 9 schematic "
        "& PCB layout, 2-layer routing (PCBWorld RL / A* / Freerouting) with SSE telemetry, DRC, Neon Auth, "
        "and fabrication exports."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=("*" not in config.CORS_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "set-auth-jwt"],
)

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


def _open_project(pid: str, user: auth.AuthUser | None, *, must_exist: bool = False) -> Project:
    uid = user.id if user else None
    prj = Project(pid, owner_user_id=uid)
    exists, owner = prj.get_access_info()
    if must_exist and not exists:
        raise HTTPException(404, f"project {pid!r} not found")
    if exists and owner is not None and owner != uid:
        raise HTTPException(403, f"forbidden: project {pid!r} belongs to another user")
    prj._cached_owner = owner
    return prj


@app.get("/")
def api_root():
    """Headless API discovery metadata."""
    return {
        "service": "coupled-backend",
        "version": app.version,
        "mode": "headless-api",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
        "health": "/health",
        "auth_config": "/api/auth/config",
    }


# ---- Neon Auth endpoints (config + same-origin proxy + current user)
@app.get("/api/auth/config")
def auth_config():
    return {
        "enabled": auth.auth_enabled(),
        "required": auth.auth_required(),
        "proxy_base_url": "/api/auth",
        "neon_auth_base_url": config.NEON_AUTH_BASE_URL,
    }


@app.api_route(
    "/api/auth/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def auth_proxy(path: str, request: Request):
    return await auth.proxy_auth_request(path, request)


@app.get("/api/v1/me")
def current_user(user: auth.AuthUser | None = Depends(auth.require_user)):
    return {
        "authenticated": user is not None,
        "auth_enabled": auth.auth_enabled(),
        "user": user.model_dump() if user else None,
    }


@app.get("/health")
def health():
    from . import octopart

    worker_info = None
    kicad_ok = export.have_kicad()
    if config.WORKER_URL:
        try:
            r = httpx.get(f"{config.WORKER_URL}/health", timeout=5.0)
            if r.status_code == 200:
                worker_info = r.json()
                kicad_ok = kicad_ok or bool(worker_info.get("kicad_cli"))
        except Exception:
            worker_info = {"ok": False}
    active_router = (
        worker_info.get("active_router")
        if isinstance(worker_info, dict) and worker_info.get("active_router")
        else engines.active_router_name()
    )
    db_health = pgdb.health_check()
    return {
        "ok": bool(db_health.get("connected", True)),
        "parts": db.count(),
        "database": db_health,
        "auth": {
            "enabled": auth.auth_enabled(),
            "required": auth.auth_required(),
            "provider": "neon_auth" if auth.auth_enabled() else None,
            "jwks_configured": bool(auth.jwks_url()),
        },
        "llm": llm.available(),
        "octopart": octopart.available(),
        "worker": config.WORKER_URL or "in-process",
        "worker_health": worker_info,
        "router_engine": config.ROUTER_ENGINE,
        "active_router": active_router,
        "kicad_cli": kicad_ok,
    }


# ---- End-to-End AI PCB Designer (Natural Language -> Parts -> Schematic -> Placement -> Routing -> DRC)
@app.post("/api/v1/design")
def design_pcb(req: DesignReq, user: auth.AuthUser | None = Depends(auth.require_user)):
    """Design a complete PCB from a natural-language specification (one-shot orchestrated pipeline)."""
    if req.project_id:
        _open_project(req.project_id, user, must_exist=False)
    try:
        return designer.run_design_pipeline(
            req.prompt,
            db,
            project_id=req.project_id,
            owner_user_id=user.id if user else None,
            board_width_mm=req.board_width_mm,
            board_height_mm=req.board_height_mm,
            auto_route=req.auto_route,
            prefer_octopart=req.prefer_octopart,
            iterations=req.iterations,
            checkpoint=req.checkpoint,
            max_steps=req.max_steps,
            seed=req.seed,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/v1/design/stream")
async def design_pcb_stream(req: DesignReq, user: auth.AuthUser | None = Depends(auth.require_user)):
    """Stream the full AI PCB design lifecycle via SSE (requirements -> components -> schematic -> placement -> routing -> drc -> review)."""
    if req.project_id:
        _open_project(req.project_id, user, must_exist=False)
    ev_q: queue.Queue[dict | None] = queue.Queue()
    owner_uid = user.id if user else None

    def _worker():
        try:
            designer.run_design_pipeline(
                req.prompt,
                db,
                project_id=req.project_id,
                owner_user_id=owner_uid,
                board_width_mm=req.board_width_mm,
                board_height_mm=req.board_height_mm,
                auto_route=req.auto_route,
                prefer_octopart=req.prefer_octopart,
                iterations=req.iterations,
                checkpoint=req.checkpoint,
                max_steps=req.max_steps,
                seed=req.seed,
                emit=ev_q.put,
            )
        except Exception as exc:
            ev_q.put({"type": "error", "error": str(exc)})
        finally:
            ev_q.put(None)

    threading.Thread(target=_worker, daemon=True).start()

    async def _gen():
        while True:
            item = await asyncio.to_thread(ev_q.get)
            if item is None:
                break
            yield f"event: {item.get('type', 'message')}\ndata: {json.dumps(item)}\n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/v1/projects/{pid}/chat")
def chat_project(pid: str, req: ChatReq, user: auth.AuthUser | None = Depends(auth.require_user)):
    """Conversational AI assistant to inspect, explain, or iteratively refine an existing PCB project."""
    _open_project(pid, user, must_exist=False)
    try:
        return designer.chat_with_project(
            pid,
            req.message,
            db,
            owner_user_id=user.id if user else None,
            auto_route=req.auto_route,
            iterations=req.iterations,
            max_steps=req.max_steps,
            checkpoint=req.checkpoint,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/v1/projects")
def list_projects(user: auth.AuthUser | None = Depends(auth.require_user)):
    """List PCB design projects visible to the authenticated user."""
    return {"projects": list_all_projects(owner_user_id=user.id if user else None)}


@app.get("/api/v1/projects/{pid}")
def get_project(pid: str, user: auth.AuthUser | None = Depends(auth.require_user)):
    """Return full state, artifacts, and conversation history for a PCB project."""
    prj = _open_project(pid, user, must_exist=True)
    files = prj.load_json_files([
        "design_brief.json",
        "sourcing.json",
        "parts.json",
        "netlist.json",
        "placement.json",
        "drc.json",
        "design_review.json",
        "chat_history.json",
    ])
    parts_data = files.get("parts.json")
    return {
        "project_id": pid,
        "owner_user_id": getattr(prj, "_cached_owner", None),
        "design_brief": files.get("design_brief.json"),
        "components": files.get("sourcing.json") or parts_data,
        "parts": parts_data,
        "netlist": files.get("netlist.json"),
        "placements": files.get("placement.json"),
        "drc": files.get("drc.json"),
        "review": (files.get("design_review.json") or {}).get("review"),
        "chat_history": files.get("chat_history.json", []),
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


@app.delete("/api/v1/projects/{pid}")
def delete_project(pid: str, user: auth.AuthUser | None = Depends(auth.require_user)):
    """Delete a PCB project and its stored artifacts."""
    prj = _open_project(pid, user, must_exist=True)
    prj.delete_project()
    return {"deleted": True, "project_id": pid}


# ---- Stage 1 (public component library catalog)
@app.post("/api/v1/parts/search")
def parts_search(req: PartSearchReq):
    from . import octopart

    engine = req.engine or config.PARTS_SEARCH_ENGINE
    if engine in ("octopart", "nexar") and octopart.available():
        parts = octopart.search_octopart(req.query, req.filters, req.k, local_db=db)
        return {"results": [p.model_dump() for p in parts], "engine": "octopart"}

    local_hits = db.search(req.query, req.filters, req.k)
    if not local_hits and octopart.available() and engine == "auto":
        octo_hits = octopart.search_octopart(req.query, req.filters, req.k, local_db=db)
        if octo_hits:
            return {"results": [p.model_dump() for p in octo_hits], "engine": "octopart_fallback"}

    return {"results": [p.model_dump() for p in local_hits], "engine": "local"}


@app.post("/api/v1/parts/octopart/search")
def octopart_search_endpoint(req: PartSearchReq):
    from . import octopart

    if not octopart.available():
        raise HTTPException(503, "Octopart / Nexar token not configured")
    parts = octopart.search_octopart(req.query, req.filters, req.k, local_db=db)
    return {"results": [p.model_dump() for p in parts], "engine": "octopart"}


@app.get("/api/v1/parts/octopart/{mpn}")
def octopart_get_part(mpn: str):
    from . import octopart

    if not octopart.available():
        raise HTTPException(503, "Octopart / Nexar token not configured")
    part = octopart.get_octopart_part(mpn, local_db=db)
    if not part:
        raise HTTPException(404, f"Part {mpn} not found on Octopart")
    return part.model_dump()


# ---- Stage 2
@app.post("/api/v1/projects/{pid}/netlist")
def make_netlist(pid: str, req: NetlistReq, user: auth.AuthUser | None = Depends(auth.require_user)):
    prj = _open_project(pid, user, must_exist=False)
    try:
        if req.parts:
            plan = [(x["ref"], db.get(x["mpn"])) for x in req.parts]
            if any(p is None for _, p in plan):
                raise HTTPException(422, "unknown mpn in parts")
        else:
            brief = designer.plan_architecture(req.prompt, db)
            plan, _ = designer.select_components(brief, db)
            prj.save_json("design_brief.json", {"prompt": req.prompt, **brief})
    except ValueError as e:
        raise HTTPException(422, str(e))
    parts = dict(plan)
    nl, status, warnings = generate_and_verify_netlist(req.prompt, parts)
    nl["components"] = [
        {"ref": r, **{k: getattr(p, k) for k in ("mpn", "lcsc_id", "value", "footprint_ref", "package")}}
        for r, p in plan
    ]
    prj.save_json("parts.json", {r: p.model_dump() for r, p in plan})
    prj.save_json("netlist.json", nl)
    sch_text = schematic.generate_kicad_sch(parts, nl, title=pid)
    sch_svg = schematic.render_schematic_svg(parts, nl, title=pid)
    prj.save_text("board.kicad_sch", sch_text)
    prj.save_text("schematic.svg", sch_svg)
    return {"netlist_id": pid, "netlist": nl, "erc_status": status, "warnings": warnings, "schematic_svg": sch_svg}


# ---- Stage 3 (generated netlist OR scraped board upload)
@app.post("/api/v1/projects/{pid}/place")
async def place(
    pid: str,
    netlist_id: str | None = Form(None),
    board: UploadFile | None = File(None),
    board_width_mm: float = Form(40.0),
    board_height_mm: float = Form(30.0),
    iterations: int = Form(3000),
    seed: int = Form(0),
    keep_zones: bool = Form(True),
    user: auth.AuthUser | None = Depends(auth.require_user),
):
    prj = _open_project(pid, user, must_exist=False)
    zones_preserved = 0
    if board is not None:
        text = (await board.read()).decode()
        try:
            tree, comps, nets, outline = parse_board(text)
            board_outline = parse_board_outline(tree)
        except ValueError as e:
            raise HTTPException(422, f"unparseable .kicad_pcb: {e}")
        if not comps:
            raise HTTPException(422, "board has no footprints")
        if outline is None:
            area = sum(
                (c.local_bbox()[2] - c.local_bbox()[0]) * (c.local_bbox()[3] - c.local_bbox()[1])
                for c in comps
            )
            side = (area * 4) ** 0.5
            outline = (0.0, 0.0, side * 1.2, side)
        rules = heuristic_rules(comps, nets)
        pos, metrics = solve_coordinates(comps, nets, rules, outline, iterations, seed, board_outline=board_outline)
        out = apply_placement_scraped(tree, comps, pos, keep_zones=keep_zones)
        zones_preserved = len(sx.find_all(tree, "zone"))
        mode = "scraped"
    else:
        loaded = prj.load_json_files(["netlist.json", "parts.json"])
        nl, parts = loaded.get("netlist.json"), loaded.get("parts.json")
        if not nl:
            raise HTTPException(409, "no netlist for this project: call /netlist first")
        parts = {r: Part(**p) for r, p in parts.items()}
        comps = [make_comp(r, p.value or p.mpn, p.footprint_ref, mpn=p.mpn, lcsc_id=p.lcsc_id) for r, p in parts.items()]
        nets = {n: [tuple(x) for x in pins] for n, pins in nl["nets"].items()}
        outline = (0.0, 0.0, board_width_mm, board_height_mm)
        ptypes = {(r, pn.num): pn.type for r, pt in parts.items() for pn in pt.pins}
        rules = heuristic_rules(comps, nets, ptypes)
        if llm.available():
            try:
                extra = llm.complete_json(
                    'Return {"edge_components":[refs],"clusters":[[anchor_ic, cap, ...]]} placement anchors for this netlist.',
                    json.dumps({"nets": nl["nets"], "refs": list(parts)}),
                )
                rules = {
                    "edge_components": sorted(set(rules["edge_components"]) | set(extra.get("edge_components", []))),
                    "clusters": rules["clusters"] or extra.get("clusters", []),
                }
            except Exception:
                pass
        pos, metrics = solve_coordinates(comps, nets, rules, outline, iterations, seed)
        out = write_unrouted_kicad_pcb(comps, pos, nets, outline)
        prj.save_text("board.kicad_pro", json.dumps(kicad_pro(nl.get("net_classes", {})), indent=2))
        mode = "netlist"
    prj.save_text("board.kicad_pcb", out)
    prj.delete_file("board_routed.kicad_pcb")
    placements = [{"ref": r, "x": round(x, 3), "y": round(y, 3), "rotation": rot} for r, (x, y, rot) in pos.items()]
    prj.save_json("placement.json", placements)
    return {
        "mode": mode,
        "board_file_url": f"/api/v1/projects/{pid}/export?format=kicad_pcb",
        "placements": placements,
        "hpwl_mm": metrics["hpwl_mm"],
        "overlaps": metrics["overlaps"],
        "layout_rules": metrics["rules"],
        "zones_preserved": zones_preserved,
        "svg": render_svg(out),
    }


# ---- Stage 4
@app.post("/api/v1/projects/{pid}/route", status_code=202)
def route(pid: str, req: RouteReq, user: auth.AuthUser | None = Depends(auth.require_user)):
    prj = _open_project(pid, user, must_exist=False)
    board_name = req.board_file or "board.kicad_pcb"
    board = prj.ensure_local_file(board_name)
    if board is None or not board.exists():
        raise HTTPException(409, "no unrouted board: call /place first")
    try:
        engines.check_board_limits(str(board))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if config.WORKER_URL:
        r = httpx.post(
            f"{config.WORKER_URL}/jobs",
            json={
                "job_id": f"job_{pid}_{abs(hash(str(board))) % 9999}",
                "board_path": str(board),
                "checkpoint": req.checkpoint,
                "max_steps": req.max_steps,
            },
            timeout=30,
        )
        r.raise_for_status()
        jid = r.json()["job_id"]
    else:
        jid = hub.start(
            str(board),
            req.checkpoint,
            req.max_steps,
            on_complete=lambda _res: prj.sync_from_disk("board_routed.kicad_pcb"),
        )
    prj.save_json("last_job.json", {"job_id": jid})
    return {"job_id": jid}


@app.get("/api/v1/jobs/{jid}/stream")
async def stream(jid: str, _user: auth.AuthUser | None = Depends(auth.require_user)):
    if not config.WORKER_URL:
        return StreamingResponse(hub.stream(jid), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def proxy():
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("GET", f"{config.WORKER_URL}/jobs/{jid}/stream") as r:
                async for chunk in r.aiter_text():
                    yield chunk

    return StreamingResponse(proxy(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.post("/api/v1/projects/{pid}/finish")
def finish_remaining(pid: str, user: auth.AuthUser | None = Depends(auth.require_user)):
    """'Finish remaining nets' button: Freerouting on the current board (worker image only)."""
    if not config.WORKER_URL:
        raise HTTPException(501, "set WORKER_URL: Freerouting runs in the worker image")
    prj = _open_project(pid, user, must_exist=True)
    b = prj.board()
    if not b:
        raise HTTPException(409, "no board")
    r = httpx.post(f"{config.WORKER_URL}/finish", json={"board_path": str(b)}, timeout=400)
    r.raise_for_status()
    prj.sync_from_disk("board_routed.kicad_pcb")
    return r.json()


# ---- Stage 5
@app.get("/api/v1/projects/{pid}/export")
def export_project(
    pid: str,
    format: str = Query("kicad_pcb", pattern="^(kicad_pcb|kicad_sch|schematic_svg|svg|gerber|bom)$"),
    user: auth.AuthUser | None = Depends(auth.require_user),
):
    prj = _open_project(pid, user, must_exist=False)
    parts = prj.load_json("parts.json", {})
    if format in ("kicad_sch", "schematic_svg"):
        fname = "board.kicad_sch" if format == "kicad_sch" else "schematic.svg"
        sch_file = prj.ensure_local_file(fname)
        if sch_file is None or not sch_file.exists():
            nl = prj.load_json("netlist.json")
            if not nl or not parts:
                raise HTTPException(404, "no schematic/netlist yet")
            pobjs = {r: Part(**p) for r, p in parts.items()}
            if format == "kicad_sch":
                sch_file = prj.save_text(fname, schematic.generate_kicad_sch(pobjs, nl, title=pid))
            else:
                sch_file = prj.save_text(fname, schematic.render_schematic_svg(pobjs, nl, title=pid))
        if format == "kicad_sch":
            return Response(
                sch_file.read_bytes(),
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{pid}.kicad_sch"'},
            )
        return Response(sch_file.read_bytes(), media_type="image/svg+xml")

    board = prj.board()
    if not board:
        raise HTTPException(404, "no board yet")
    bom = export.bom_csv([{"ref": r, **p} for r, p in parts.items()])
    if format == "kicad_pcb":
        return Response(
            board.read_bytes(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{pid}.kicad_pcb"'},
        )
    if format == "svg":
        if config.WORKER_URL and not export.have_kicad():
            try:
                r = httpx.post(f"{config.WORKER_URL}/export/svg", json={"board_path": str(board)}, timeout=30)
                if r.status_code == 200:
                    return Response(r.content, media_type="image/svg+xml")
            except Exception:
                pass
        return Response(export.to_svg(board), media_type="image/svg+xml")
    if format == "bom":
        return Response(
            bom,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{pid}_bom.csv"'},
        )
    if config.WORKER_URL and not export.have_kicad():
        r = httpx.post(
            f"{config.WORKER_URL}/export/gerber",
            json={"board_path": str(board), "bom_csv": bom},
            timeout=120,
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text)
        return Response(
            r.content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{pid}_fab.zip"'},
        )
    try:
        return Response(
            export.to_gerber_zip(board, bom),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{pid}_fab.zip"'},
        )
    except RuntimeError as e:
        raise HTTPException(501, str(e))


@app.get("/api/v1/projects/{pid}/drc")
def project_drc(pid: str, user: auth.AuthUser | None = Depends(auth.require_user)):
    prj = _open_project(pid, user, must_exist=False)
    board = prj.board()
    if not board:
        raise HTTPException(404, "no board yet")
    result = None
    if config.WORKER_URL:
        try:
            r = httpx.post(f"{config.WORKER_URL}/drc", json={"board_path": str(board)}, timeout=60)
            if r.status_code == 200:
                result = r.json()
        except Exception:
            pass
    if result is None:
        result = export.run_drc(board)
    prj.save_json("drc.json", result)
    return result
