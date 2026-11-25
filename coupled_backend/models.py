from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field


class Pin(BaseModel):
    num: str
    name: str
    type: str = "passive"   # power_in | power_out | ground | passive | input | output | bidir | nc


class Part(BaseModel):
    mpn: str
    lcsc_id: str = ""
    category: str = ""
    package: str = ""
    description: str = ""
    manufacturer: str = ""
    value: str = ""
    price: float = 0.0
    stock: int = 0
    voltage_max: float | None = None
    footprint_ref: str = ""
    pins: list[Pin] = Field(default_factory=list)
    score: float | None = None
    octopart_url: str = ""


class PartSearchReq(BaseModel):
    query: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)
    k: int = 5
    engine: str = "auto"   # auto | local | octopart


class NetlistReq(BaseModel):
    prompt: str
    parts: list[dict[str, Any]] | None = None   # optional [{ref, mpn}] to skip planning


class PlaceReq(BaseModel):
    netlist_id: str | None = None
    board_width_mm: float = 40.0
    board_height_mm: float = 30.0
    iterations: int = 3000
    seed: int = 0


class RouteReq(BaseModel):
    board_file: str | None = None
    checkpoint: str = "d2/ppo"
    max_steps: int = 300


class DesignReq(BaseModel):
    prompt: str
    project_id: str | None = None
    board_width_mm: float | None = None
    board_height_mm: float | None = None
    auto_route: bool = True
    prefer_octopart: bool = False
    iterations: int = 2500
    checkpoint: str = "d2/ppo"
    max_steps: int = 300
    seed: int = 0


class ChatReq(BaseModel):
    message: str
    auto_route: bool = True
    iterations: int = 2500
    max_steps: int = 300
    checkpoint: str = "d2/ppo"

