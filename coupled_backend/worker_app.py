"""PCBWorld routing & KiCad 9 worker (Linux x86_64 container).
The API service proxies routing, KiCad 9 DRC, Gerber/Drill export, and SVG export here when WORKER_URL is set."""
from __future__ import annotations

from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from . import config, engines, export
from .engines import freerouting_finish
from .jobs import JobHub

app, hub = FastAPI(title="pcbworld-worker"), JobHub()


class Job(BaseModel):
    job_id: str
    board_path: str
    checkpoint: str = "d2/ppo"
    max_steps: int = 300


class BoardPathReq(BaseModel):
    board_path: str
    bom_csv: str = ""


@app.post("/jobs", status_code=202)
def submit(j: Job):
    return {"job_id": hub.start(j.board_path, j.checkpoint, j.max_steps, j.job_id)}


@app.get("/jobs/{jid}/stream")
async def stream(jid: str):
    return StreamingResponse(hub.stream(jid), media_type="text/event-stream")


@app.post("/finish")
def finish(body: dict):
    return {"output_path": freerouting_finish(body["board_path"])}


@app.post("/drc")
def drc(req: BoardPathReq):
    p = Path(req.board_path)
    if not p.exists():
        raise HTTPException(404, f"board not found: {p}")
    return export.run_drc(p)


@app.post("/export/gerber")
def export_gerber(req: BoardPathReq):
    p = Path(req.board_path)
    if not p.exists():
        raise HTTPException(404, f"board not found: {p}")
    try:
        data = export.to_gerber_zip(p, req.bom_csv)
    except RuntimeError as e:
        raise HTTPException(501, str(e))
    return Response(data, media_type="application/zip")


@app.post("/export/svg")
def export_svg(req: BoardPathReq):
    p = Path(req.board_path)
    if not p.exists():
        raise HTTPException(404, f"board not found: {p}")
    return Response(export.to_svg(p), media_type="image/svg+xml")


_HEALTH_CACHE: dict | None = None


def _build_health() -> dict:
    ckpts = []
    if config.CHECKPOINT_DIR.is_dir():
        ckpts = sorted(str(p.relative_to(config.CHECKPOINT_DIR)) for p in config.CHECKPOINT_DIR.rglob("*.pt"))
    has_cli = export.have_kicad()
    kver = ".".join(map(str, export.kicad_version())) if has_cli else ""
    return {
        "ok": True,
        "kicad_cli": has_cli,
        "kicad_version": kver,
        "pcbworld": engines.pcbworld_status(),
        "active_router": engines.active_router_name(),
        "checkpoints": len(ckpts),
    }


@app.get("/health")
def health():
    global _HEALTH_CACHE
    if _HEALTH_CACHE is None or (_HEALTH_CACHE.get("kicad_cli") and not _HEALTH_CACHE.get("kicad_version")):
        _HEALTH_CACHE = _build_health()
    return _HEALTH_CACHE
