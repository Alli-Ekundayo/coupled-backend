# coupled-backend: Headless AI PCB Design Backend

Pure headless FastAPI backend: natural-language architecture planning -> parts retrieval -> grounded netlist + ERC -> KiCad 9 schematic -> auto-placement -> 2-layer routing (PCBWorld RL / built-in A* / Freerouting via SSE) -> KiCad 9 DRC & fabrication exports.
Non-commercial only: the `LG-AI-Research/PCBWorld` checkpoints are under the EXAONE NC licence (research/educational use).

## Documentation (Standalone Frontend Integration)
- **[Backend API Reference (`docs/API.md`)](docs/API.md)** — Complete REST & SSE endpoint reference, request/response schemas, Neon Auth JWT/cookie flows, CORS setup, and error codes.
- **[Standalone Frontend Design Guide (`docs/FRONTEND_DESIGN_GUIDE.md`)](docs/FRONTEND_DESIGN_GUIDE.md)** — Decoupled SPA architecture, Dark CAD Studio design tokens, 3-pane workspace blueprint, client-side KiCad 9 `.kicad_pcb` S-expression 2D/3D rendering spec, SSE streaming patterns, and backend edge cases.
- **Interactive OpenAPI Docs** — Available live at `GET /docs` (Swagger UI), `GET /redoc`, and `GET /openapi.json`.

## Run it (laptop, no KiCad, no API key)
    pip install -r requirements.txt
    ROUTER_ENGINE=mock uvicorn coupled_backend.api:app --reload      # :8000
    ./scripts/demo.sh                                         # curl through all 5 stages
    pytest -q tests

Offline, the netlist stage supports one built-in design (USB-C -> 3.3V LDO -> BME280 + I2C header; prompt must mention
BME280/USB-C/sensor). Set `GEMINI_API_KEY` (and optionally `LLM_MODEL`, defaulting to `gemini-2.5-flash`) for arbitrary prompts: the LLM plans parts,
the DB picks verified MPNs/pins, the LLM wires them, `run_erc` gates the result with up to 2 feedback retries.

Set `NEXAR_ACCESS_TOKEN` in `.env` to enable live component search against the Octopart search engine via Altium Nexar GraphQL API. Components can be queried via `/api/v1/parts/search` (`engine: "octopart"` or automatic fallback) and `/api/v1/parts/octopart/search`.

## End-to-End AI PCB Design API
Describe the PCB you need in natural language — the Gemini LLM orchestrates architecture planning, component selection (local DB + live Octopart/Nexar + KiCad symbols/footprints), netlist + ERC, KiCad 9 schematic generation, placement, routing, DRC, and engineering review:

- `POST /api/v1/design` — One-shot full PCB design from `{"prompt": "..."}` returning BOM, netlist, ERC, `.kicad_sch` / schematic SVG, placement, routed `.kicad_pcb` / PCB SVG, DRC report, and LLM review.
- `POST /api/v1/design/stream` — Same orchestrated pipeline streamed live over SSE (`requirements` → `components` → `schematic` → `placement` → `routing_step` → `drc` → `done`).
- `POST /api/v1/projects/{pid}/chat` — Conversational AI assistant to ask engineering questions or iteratively modify an existing board (add/swap parts, resize board, adjust placement, redesign).
- `GET /api/v1/projects` & `GET /api/v1/projects/{pid}` — List and inspect saved projects, artifacts, and chat history.
- `GET /api/v1/projects/{pid}/export?format=kicad_pcb|kicad_sch|schematic_svg|svg|bom|gerber` — Export any generated artifact.

## Real routing (Linux x86_64)
    docker compose up --build        # api :8000 + worker :8001 (shared /data volume)
The worker image installs KiCad 9, clones PCBWorld, downloads checkpoints. `ROUTER_ENGINE=auto` uses PCBWorld if
importable, else the mock router.

## Layout
| file | role |
| --- | --- |
| `designer.py`, `llm.py` | End-to-end Gemini AI PCB designer, conversational refinement (`/chat`), resilient fallback/retry LLM client |
| `schematic.py` | KiCad 9 `.kicad_sch` schematic exporter & engineering blueprint SVG renderer |
| `parts.py`, `seed.py`, `ingest.py`, `octopart.py` | SQL pre-filter + vector rank (SQLite/numpy), live Octopart/Nexar search, verified seed parts, JLC CSV enrichment |
| `libraries.py` | KiCad `.kicad_mod` footprint inliner, `.kicad_sym` pin extractor (with `extends` inheritance), optional `easyeda2kicad` |
| `netlist.py` | part planning, LLM wiring, deterministic ERC (with 64-net checkpoint cap) |
| `placer.py` | layout rules, spring layout seed, simulated annealing on pad-level HPWL, non-rectangular Edge.Cuts polygon & cutout hole legalizer |
| `kicad_io.py` | s-expr parse/strip scraped boards (preserving ground pour zones), KiCad 9 (20241229) writer, `.kicad_pro` net classes, SVG preview |
| `engines.py` | PCBWorld `KiCadRLAgent` adapter (`_load_policy`, `make_decoder_env`, `save_pcb`), mock router, Freerouting fallback |
| `export.py` | KiCad DRC runner (`kicad-cli pcb drc` + built-in validator), SVG/Gerber/BOM exports |
| `api.py`, `auth.py`, `db.py`, `worker_app.py`, `jobs.py` | Headless FastAPI app (CORS, Neon Auth JWT/cookie + `/api/auth/*` proxy, Lakebase Postgres/SQLite persistence, `/design`, `/design/stream`, `/projects/{pid}/chat`, `/drc`, `/parts/octopart/*`, 5-stage endpoints), routing worker, SSE hub |

## Operational notes
- **PCBWorld checkpoints**: `engines._load_policy` and `_pcbworld_route` are wired to the `LGAI-Research/PCBWorld` API (`KiCadRLAgent.from_checkpoint`, `RLEnvConfig.from_checkpoint`, `resolve_eval_env`, `make_decoder_env`, `gather_dist`, `save_pcb`). Live inference requires running inside the Linux x86_64 worker container (`docker compose up --build`) with KiCad 9.0.8 and downloaded HuggingFace checkpoints.
- **KiCad 9 DRC**: `GET /api/v1/projects/{pid}/drc` runs `kicad-cli pcb drc --format json` automatically on KiCad 8/9 installations (such as the worker container) and falls back to the built-in format/courtyard/edge-clearance validator locally.
- **64-net cap**: Enforced via `MAX_NETS` (default `64`) in ERC, `engines.check_board_limits`, and `POST /api/v1/projects/{pid}/route`. Keep demo boards to 5-15 nets on 2 layers.
