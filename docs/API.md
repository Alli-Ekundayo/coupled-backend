# coupled-backend Backend API Reference

`coupled-backend` is a **pure headless FastAPI backend** for AI-assisted 2-layer PCB design. It orchestrates natural-language architecture planning, component sourcing (Lakebase Postgres / SQLite PartsDB + live Octopart/Nexar + KiCad libraries), grounded netlist generation with closed-loop ERC, KiCad 9 schematic export (`.kicad_sch` + SVG), simulated-annealing placement, 2-layer routing (`PCBWorld` RL / built-in DRC-aware `A*` / `Freerouting`) with live Server-Sent Events (SSE), KiCad 9 DRC validation, and fabrication exports (Gerber/Drill ZIP, JLCPCB BOM CSV, KiCad files, SVGs).

- **Default Dev Base URL:** `http://localhost:8000`
- **Interactive OpenAPI Docs:** `GET /docs` (Swagger UI), `GET /redoc` (ReDoc), `GET /openapi.json`
- **API Version:** `0.1.0`

---

## 1. Architecture, CORS & Authentication

### Service Topology
```text
Standalone Frontend (Vite / Next.js / SPA / Mobile / CLI)
        │  HTTPS / JSON / multipart / SSE
        ▼
coupled-backend API (FastAPI :8000)
   ├── Neon Auth (Managed Better Auth — EdDSA JWT / session cookie)
   ├── Lakebase Postgres (psycopg 3 pooled) or local SQLite fallback
   ├── Gemini LLM (google-genai with model fallback chain)
   ├── Octopart / Altium Nexar GraphQL API
   └── Optional Worker Service (WORKER_URL :8001 — Linux x86_64)
         ├── KiCad 9 CLI (kicad-cli pcb drc / export gerbers / drill / svg)
         ├── PCBWorld RL Router (KiCadRLAgent)
         └── Freerouting (freerouting.jar + pcbnew)
```

### CORS Configuration
Configured via `CORS_ORIGINS` (comma-separated origins, default `*`):
- When `CORS_ORIGINS=*`: `allow_origins=["*"]`, `allow_credentials=False`. Suitable for Bearer JWT auth and local testing.
- When explicit origins are set (e.g., `CORS_ORIGINS=http://localhost:5173,https://studio.example.com`): `allow_credentials=True` is automatically enabled so cross-origin cookie auth and credentialed `EventSource` work.
- Exposed response headers: `Content-Disposition` (for download filenames) and `set-auth-jwt` (for Neon Auth JWT extraction).

### Authentication (Neon Auth / Managed Better Auth)
Authentication is controlled by `NEON_AUTH_BASE_URL` and `AUTH_REQUIRED` (default `true` when `NEON_AUTH_BASE_URL` is configured).

1. **Discover auth status on startup:** Call `GET /api/auth/config` or inspect `auth` in `GET /health`.
2. **Supported credentials on protected routes:**
   - **Bearer JWT (recommended for decoupled SPAs):**
     ```http
     Authorization: Bearer <ed25519_jwt>
     ```
     Verified against `NEON_AUTH_JWKS_URL` (cached 300s, EdDSA/Ed25519 with automatic JWKS rotation retry).
   - **Session Cookie:** Better Auth / Neon Auth cookie (`neon-auth*` or `better-auth*`) verified via `NEON_AUTH_BASE_URL/get-session`.
3. **Standalone Auth Strategies:**
   - **Direct Neon Auth:** Point your frontend Better Auth / `@neon/auth` client directly at `neon_auth_base_url` (from `/api/auth/config`), obtain a JWT (`set-auth-jwt` header or session token endpoint), and attach `Authorization: Bearer <jwt>` on every API call.
   - **Same-Origin Proxy:** Route frontend auth calls through `ANY /api/auth/{path}` on the coupled-backend API, which forwards to `NEON_AUTH_BASE_URL` and rewrites `Set-Cookie` for host-only delivery.
4. **Access Control & Ownership:**
   - **Public routes:** `GET /`, `GET /health`, `GET /api/auth/config`, `ANY /api/auth/{path}`, `POST /api/v1/parts/search`, `POST /api/v1/parts/octopart/search`, `GET /api/v1/parts/octopart/{mpn}`.
   - **Protected routes (when `auth.required === true`):** `/api/v1/me`, `/api/v1/design*`, `/api/v1/projects*`, `/api/v1/jobs/*/stream`. Missing/invalid credentials return `401 Unauthorized` with `WWW-Authenticate: Bearer`.
   - **Multi-tenant ownership:** Projects created by an authenticated user store `owner_user_id`. Reading, modifying, exporting, or deleting another user's project returns `403 Forbidden`. Unowned/legacy projects (`owner_user_id: null`) remain readable by all users.

---

## 2. Endpoint Index

| Group | Method | Path | Auth | Content-Type (Req → Res) | Purpose |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Meta** | `GET` | `/` | Public | — → `application/json` | Headless API discovery links |
| **Meta** | `GET` | `/health` | Public | — → `application/json` | Live DB, auth, LLM, Octopart, router & KiCad 9 status |
| **Auth** | `GET` | `/api/auth/config` | Public | — → `application/json` | Neon Auth enablement & URLs |
| **Auth** | `ANY` | `/api/auth/{path}` | Public | passthrough | Reverse proxy to `NEON_AUTH_BASE_URL` |
| **Auth** | `GET` | `/api/v1/me` | Required* | — → `application/json` | Current authenticated user profile |
| **AI Design** | `POST` | `/api/v1/design` | Required* | `application/json` → `application/json` | One-shot full PCB design pipeline |
| **AI Design** | `POST` | `/api/v1/design/stream` | Required* | `application/json` → `text/event-stream` | Live SSE-streamed full PCB design pipeline |
| **AI Chat** | `POST` | `/api/v1/projects/{pid}/chat` | Required* | `application/json` → `application/json` | Conversational Q&A and iterative PCB edits |
| **Projects** | `GET` | `/api/v1/projects` | Required* | — → `application/json` | List projects visible to current user |
| **Projects** | `GET` | `/api/v1/projects/{pid}` | Required* | — → `application/json` | Fetch full project artifacts, metadata & chat history |
| **Projects** | `DELETE` | `/api/v1/projects/{pid}` | Required* | — → `application/json` | Delete a project and its files |
| **Parts** | `POST` | `/api/v1/parts/search` | Public | `application/json` → `application/json` | Hybrid SQL + vector search (+ Octopart fallback) |
| **Parts** | `POST` | `/api/v1/parts/octopart/search` | Public | `application/json` → `application/json` | Direct Octopart / Nexar GraphQL search |
| **Parts** | `GET` | `/api/v1/parts/octopart/{mpn}` | Public | — → `application/json` | Live Octopart part lookup by MPN |
| **Stage 2** | `POST` | `/api/v1/projects/{pid}/netlist` | Required* | `application/json` → `application/json` | Grounded netlist + ERC + KiCad 9 schematic |
| **Stage 3** | `POST` | `/api/v1/projects/{pid}/place` | Required* | `multipart/form-data` → `application/json` | Auto-placement (project netlist or uploaded `.kicad_pcb`) |
| **Stage 4** | `POST` | `/api/v1/projects/{pid}/route` | Required* | `application/json` → `application/json` (`202`) | Start async 2-layer routing job |
| **Stage 4** | `GET` | `/api/v1/jobs/{jid}/stream` | Required* | — → `text/event-stream` | Live SSE routing step telemetry |
| **Stage 4** | `POST` | `/api/v1/projects/{pid}/finish` | Required* | — → `application/json` | Freerouting fallback (worker image only) |
| **Stage 5** | `GET` | `/api/v1/projects/{pid}/export` | Required* | query → file / SVG / ZIP / CSV | Download KiCad files, SVGs, BOM, or Gerber ZIP |
| **Stage 5** | `GET` | `/api/v1/projects/{pid}/drc` | Required* | — → `application/json` | Run KiCad 9 CLI DRC (or built-in DRC) |

*\*Required only when `auth.required === true` (`NEON_AUTH_BASE_URL` set and `AUTH_REQUIRED=true`).*

---

## 3. Core Data Schemas (TypeScript Reference)

```ts
export type PinType =
  | "power_in"
  | "power_out"
  | "ground"
  | "passive"
  | "input"
  | "output"
  | "bidir"
  | "nc";

export interface Pin {
  num: string;
  name: string;
  type: PinType;
}

export interface Part {
  mpn: string;
  lcsc_id: string;
  category: string;
  package: string;
  description: string;
  manufacturer: string;
  value: string;
  price: number;
  stock: number;
  voltage_max: number | null;
  footprint_ref: string;      // e.g. "Package_TO_SOT_SMD:SOT-23-5"
  pins: Pin[];
  score: number | null;       // cosine similarity score on vector queries
  octopart_url: string;
}

export interface SourcingEntry {
  ref: string;
  role?: string;
  mpn: string | null;
  lcsc_id?: string;
  footprint_ref?: string;
  source: "local_exact" | "local_search" | "octopart" | "local_fallback" | "preset" | "not_found";
  price?: number;
  stock?: number;
  octopart_url?: string;
  query?: string;             // present when source === "not_found"
}

export interface NetlistComponentRef {
  ref: string;
  mpn: string;
  lcsc_id: string;
  value: string;
  footprint_ref: string;
  package: string;
}

export interface Netlist {
  nets: Record<string, [ref: string, pinNum: string][]>;
  net_classes: Record<string, "power" | "signal" | "diff_pair" | string>;
  components?: NetlistComponentRef[];
}

export interface Placement {
  ref: string;
  x: number;                  // mm (KiCad Y-down coordinate frame)
  y: number;                  // mm
  rotation: number;           // degrees CCW (0, 90, 180, 270)
}

export interface LayoutRules {
  edge_components: string[];
  clusters: string[][];
}

export interface PlacementMetrics {
  hpwl_mm: number;
  overlaps: number;
  cost: number;
  rules: LayoutRules;
}

export interface DrcViolation {
  type: string;
  severity: "error" | "warning" | string;
  description: string;
  items?: unknown[];          // present in kicad-cli JSON output
}

export interface DrcReport {
  engine: "kicad-cli" | "builtin";
  kicad_version?: string;
  passed: boolean;
  error_count: number;
  warning_count: number;
  unconnected_count: number;
  violations: DrcViolation[];
  unconnected_items?: unknown[];
}

export interface RoutingResult {
  output_path: string;
  engine: "pcbworld" | "astar" | "mock";
  final_metrics: Record<string, number | string | boolean>;
}

export interface ExportsMap {
  kicad_pcb: string;
  kicad_sch: string;
  schematic_svg: string;
  pcb_svg: string;
  bom: string;
  gerber: string;
  drc: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}
```

---

## 4. Detailed Endpoint Specifications

### 4.1 Discovery & Health

#### `GET /`
Returns API service metadata and discovery links.

**Response `200 OK`:**
```json
{
  "service": "coupled-backend",
  "version": "0.1.0",
  "mode": "headless-api",
  "docs": "/docs",
  "redoc": "/redoc",
  "openapi": "/openapi.json",
  "health": "/health",
  "auth_config": "/api/auth/config"
}
```

#### `GET /health`
Inspects all backend capabilities so the frontend can enable/disable feature UI (e.g., Octopart search, Gerber export, Freerouting, auth gating).

**Response `200 OK`:**
```json
{
  "ok": true,
  "parts": 11,
  "database": {
    "backend": "postgres",
    "connected": true,
    "pooled": true,
    "branch": "main",
    "branch_id": "br-...",
    "database": "neondb",
    "postgres_version": "PostgreSQL 17..."
  },
  "auth": {
    "enabled": true,
    "required": true,
    "provider": "neon_auth",
    "jwks_configured": true
  },
  "llm": true,
  "octopart": true,
  "worker": "http://worker:8001",
  "worker_health": {
    "ok": true,
    "kicad_cli": true,
    "kicad_version": "9.0.8",
    "pcbworld": {
      "pcb_world": true,
      "gymnasium": true,
      "kicad_rl_router": true
    },
    "active_router": "pcbworld",
    "checkpoints": 4
  },
  "router_engine": "auto",
  "active_router": "pcbworld",
  "kicad_cli": true
}
```
> When running locally without Postgres or a worker container: `database.backend` is `"sqlite"`, `worker` is `"in-process"`, `worker_health` is `null`, and `active_router` is `"astar"` (or `"mock"` when `ROUTER_ENGINE=mock`).

---

### 4.2 Authentication

#### `GET /api/auth/config`
Returns Neon Auth configuration for bootstrapping frontend sign-in/sign-up flows.

**Response `200 OK`:**
```json
{
  "enabled": true,
  "required": true,
  "proxy_base_url": "/api/auth",
  "neon_auth_base_url": "https://ep-....neonauth.../neondb/auth"
}
```

#### `ANY /api/auth/{path}`
Transparent reverse proxy forwarding `GET`, `POST`, `PUT`, `PATCH`, `DELETE` requests to `{NEON_AUTH_BASE_URL}/{path}` (e.g., `/api/auth/sign-in/email`, `/api/auth/sign-up/email`, `/api/auth/get-session`, `/api/auth/sign-out`). Rewrites `Set-Cookie` to drop upstream `Domain=` / `Partitioned` and convert `SameSite=None` to `SameSite=Lax`.
- **Errors:** `503 Service Unavailable` if `NEON_AUTH_BASE_URL` is not configured.

#### `GET /api/v1/me`
Returns the authenticated user decoded from the `Authorization: Bearer` JWT or session cookie.

**Response `200 OK`:**
```json
{
  "authenticated": true,
  "auth_enabled": true,
  "user": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "email": "engineer@example.com",
    "name": "Ada Lovelace",
    "role": "authenticated",
    "email_verified": true
  }
}
```
- **Errors:** `401 Unauthorized` when `auth.required === true` and credentials are missing/invalid.

---

### 4.3 End-to-End AI PCB Designer

#### `POST /api/v1/design`
Runs the entire 6-stage PCB design pipeline synchronously and returns all generated artifacts and download links.

**Request Body (`application/json` — `DesignReq`):**
| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `prompt` | `string` | *(required)* | Natural-language PCB specification |
| `project_id` | `string \| null` | `null` | Custom slug/ID; auto-generated (`<words>_<hex6>`) if omitted |
| `board_width_mm` | `number \| null` | `null` | Override board width in mm (LLM picks default, typically 40–65mm; auto-expands up to 3× by 1.25× if courtyards overlap) |
| `board_height_mm` | `number \| null` | `null` | Override board height in mm |
| `auto_route` | `boolean` | `true` | Run Stage 4 routing automatically |
| `prefer_octopart` | `boolean` | `false` | Prefer live Octopart lookup over local PartsDB hits |
| `iterations` | `integer` | `2500` | Simulated annealing placement iterations |
| `checkpoint` | `string` | `"d2/ppo"` | PCBWorld checkpoint (`d2/ppo`, `d2/grpo`, `d2/ppo_terminal`, `d1/grid100`) |
| `max_steps` | `integer` | `300` | Maximum routing steps |
| `seed` | `integer` | `0` | Deterministic RNG seed for placement |

**Response `200 OK`:**
```json
{
  "project_id": "usb_c_powered_3_a1b2c3",
  "board_name": "USB-C BME280 Sensor Board",
  "summary": "USB-C 5V regulated to 3.3V via AP2112K feeding BME280 and I2C header.",
  "review": "- Architecture uses AP2112K-3.3TRG1...\n- DRC passed with 0 errors.",
  "board_mm": { "width": 40.0, "height": 30.0 },
  "components": [
    {
      "ref": "J1",
      "role": "USB-C power",
      "mpn": "TYPE-C-31-M-17",
      "lcsc_id": "C283540",
      "footprint_ref": "Connector_USB:USB_C_Receptacle_GCT_USB4125-xx-x_6P_TopMnt_Horizontal",
      "source": "local_exact",
      "price": 0.2,
      "stock": 22625,
      "octopart_url": ""
    }
  ],
  "bom_unit_cost_usd": 3.4784,
  "netlist": {
    "nets": { "VBUS": [["J1", "A9"], ["U1", "1"]] },
    "net_classes": { "VBUS": "power", "GND": "power", "3V3": "power" },
    "components": []
  },
  "erc_status": "PASS",
  "erc_warnings": [],
  "placements": [{ "ref": "J1", "x": 4.21, "y": 15.0, "rotation": 0.0 }],
  "placement_metrics": {
    "hpwl_mm": 124.6,
    "overlaps": 0,
    "cost": 128.4,
    "rules": { "edge_components": ["J1", "J2"], "clusters": [["U1", "C1", "C2"]] }
  },
  "routing": {
    "output_path": "/data/projects/.../board_routed.kicad_pcb",
    "engine": "astar",
    "final_metrics": {
      "connected_nets": 7,
      "total_nets": 7,
      "unrouted_connections": 0,
      "segments": 48,
      "vias": 4,
      "copper_layers": 2
    }
  },
  "drc": {
    "engine": "kicad-cli",
    "kicad_version": "9.0.8",
    "passed": true,
    "error_count": 0,
    "warning_count": 0,
    "unconnected_count": 0,
    "violations": [],
    "unconnected_items": []
  },
  "schematic_svg": "<svg ...>...</svg>",
  "pcb_svg": "<svg ...>...</svg>",
  "bom_csv": "Comment,Designator,Footprint,LCSC Part #\n...",
  "exports": {
    "kicad_pcb": "/api/v1/projects/usb_c_powered_3_a1b2c3/export?format=kicad_pcb",
    "kicad_sch": "/api/v1/projects/usb_c_powered_3_a1b2c3/export?format=kicad_sch",
    "schematic_svg": "/api/v1/projects/usb_c_powered_3_a1b2c3/export?format=schematic_svg",
    "pcb_svg": "/api/v1/projects/usb_c_powered_3_a1b2c3/export?format=svg",
    "bom": "/api/v1/projects/usb_c_powered_3_a1b2c3/export?format=bom",
    "gerber": "/api/v1/projects/usb_c_powered_3_a1b2c3/export?format=gerber",
    "drc": "/api/v1/projects/usb_c_powered_3_a1b2c3/drc"
  }
}
```
- **Errors:**
  - `401` — Authentication required.
  - `403` — `project_id` exists and belongs to another user.
  - `422` — Offline mode with non-demo prompt, LLM failure to produce `part_specs`, or no resolvable manufacturable parts.

---

#### `POST /api/v1/design/stream`
Same request body (`DesignReq`) as `POST /api/v1/design`, returning `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

> **Client Implementation Note:** Because this endpoint uses `POST` with a JSON body (and optionally an `Authorization` header), **do not use browser `EventSource`**. Use `fetch()` + `response.body.getReader()` to parse `\n\n`-delimited SSE frames.

**SSE Event Sequence:**

| SSE `event:` | JSON `data:` Payload | Notes |
| :--- | :--- | :--- |
| `stage` | `{"type":"stage","stage":"requirements","status":"running","project_id":"..."}` | First event; announces resolved `project_id` |
| `stage` | `{"type":"stage","stage":"requirements","status":"done","brief":{...}}` | Contains `board_name`, `summary`, `power_rails`, `interfaces`, `part_specs` |
| `stage` | `{"type":"stage","stage":"components","status":"running"}` | — |
| `stage` | `{"type":"stage","stage":"components","status":"done","count":9,"components":[...SourcingEntry]}` | Resolved BOM sourcing log |
| `stage` | `{"type":"stage","stage":"netlist","status":"running"}` | **Note:** running event uses `"netlist"` |
| `stage` | `{"type":"stage","stage":"schematic","status":"done","erc_status":"PASS","erc_warnings":[],"net_count":7,"schematic_svg":"<svg..."}` | **Note:** completion event uses `"schematic"` and includes full SVG |
| `stage` | `{"type":"stage","stage":"placement","status":"running"}` | — |
| `stage` | `{"type":"stage","stage":"placement","status":"done","board_mm":[40,30],"hpwl_mm":124.6,"overlaps":0,"pcb_svg":"<svg..."}` | Unrouted placed board SVG |
| `stage` | `{"type":"stage","stage":"routing","status":"running"}` | Emitted only if `auto_route: true` |
| `routing_step` | `{"type":"routing_step","step":12,"connected_nets":5,"total_nets":7,"unrouted_count":2,"vias":2,"drc_violations":0,"engine":"astar","svg_snapshot":"<svg..."}` | Repeated during routing (`vias`, `ratsnest_reduction`, `svg_snapshot` vary by engine) |
| `stage` | `{"type":"stage","stage":"routing","status":"done","routing":{...RoutingResult}}` | Emitted only if `auto_route: true` |
| `stage` | `{"type":"stage","stage":"drc","status":"running"}` | — |
| `stage` | `{"type":"stage","stage":"drc","status":"done","drc":{...DrcReport}}` | Final DRC report |
| `done` | `{"type":"done","project_id":"...","result":{...same shape as POST /api/v1/design}}` | Terminal success frame |
| `error` | `{"type":"error","error":"..."}` | Terminal failure frame (HTTP status is still `200` once stream starts) |

---

### 4.4 Conversational Refinement

#### `POST /api/v1/projects/{pid}/chat`
Asks engineering questions about an existing project OR triggers targeted rebuilds (resize, re-place, add/swap/remove parts, or full redesign).

> **Special behavior:** If project `{pid}` has no `parts.json` and no `netlist.json` yet, this endpoint automatically treats `message` as a brand-new design prompt and runs the full design pipeline (`action: "new_design"`).

**Request Body (`application/json` — `ChatReq`):**
```json
{
  "message": "Add a green 0603 power indicator LED with a 4.7k resistor on 3V3",
  "auto_route": true,
  "iterations": 2500,
  "max_steps": 300,
  "checkpoint": "d2/ppo"
}
```

**Response `200 OK`:**
```json
{
  "project_id": "ai_bme280",
  "action": "add_or_swap_parts",
  "reply": "Added green power LED D1 (KT-0603G) with 4.7k resistor R3 on 3V3 and rebuilt layout.",
  "design": { "...full DesignResponse object, or null when action === 'answer_only'..." }
}
```
Possible `action` values:
- `"answer_only"` — Q&A only; `design` is `null`.
- `"update_board_size"` — Re-placed & re-routed at new board dimensions (also works offline with regex `<W>x<H>` or `<W>×<H>`).
- `"re_place"` — Updated edge/cluster placement rules and re-placed/re-routed.
- `"add_or_swap_parts"` — Added/removed/replaced components and rebuilt schematic → routing → DRC.
- `"full_redesign"` — Fresh full pipeline from revised prompt.
- `"new_design"` — Initial creation when `{pid}` had no prior parts/netlist.

- **Errors:** `401`, `403`, `422`.

---

### 4.5 Project Management

#### `GET /api/v1/projects`
Lists projects visible to the authenticated user (their own projects + unowned shared/seeded projects), ordered by `updated_at DESC`.

**Response `200 OK`:**
```json
{
  "projects": [
    {
      "project_id": "ai_bme280",
      "owner_user_id": "550e8400-e29b-41d4-a716-446655440000",
      "board_name": "USB-C BME280 Sensor Board",
      "prompt": "USB-C powered 3.3V BME280 sensor board",
      "component_count": 9,
      "has_board": true,
      "has_schematic": true,
      "drc_passed": true,
      "updated_at": "2026-11-25T12:00:00+00:00"
    }
  ]
}
```

#### `GET /api/v1/projects/{pid}`
Returns all persisted JSON artifacts, conversation history, and export URLs for `{pid}`.

**Response `200 OK`:**
```json
{
  "project_id": "ai_bme280",
  "owner_user_id": "550e8400-e29b-41d4-a716-446655440000",
  "design_brief": { "prompt": "...", "board_name": "...", "board_width_mm": 40.0, "board_height_mm": 30.0 },
  "components": [ "...SourcingEntry[] if created via /design, OR Record<ref, Part> if created via /netlist..." ],
  "parts": { "U1": { "...full Part object with pins..." } },
  "netlist": { "nets": {}, "net_classes": {}, "components": [] },
  "placements": [{ "ref": "U1", "x": 18.2, "y": 14.5, "rotation": 0 }],
  "drc": { "...DrcReport or null..." },
  "review": "Engineering summary text or null",
  "chat_history": [{ "role": "user", "content": "..." }, { "role": "assistant", "content": "..." }],
  "exports": { "...ExportsMap..." }
}
```
> **Important Schema Note:** Always prefer `detail.parts` (`Record<ref, Part>`) for full component metadata and pin tables. `detail.components` returns `sourcing.json` (an array of `SourcingEntry`) when present, and falls back to `parts.json` (an object map) otherwise.
- **Errors:** `401`, `403`, `404` if project does not exist.

#### `DELETE /api/v1/projects/{pid}`
Deletes the project row, stored `project_files`, and local disk directory.

**Response `200 OK`:**
```json
{ "deleted": true, "project_id": "ai_bme280" }
```
- **Errors:** `401`, `403`, `404`.

---

### 4.6 Component Library & Octopart Search (Stage 1)

#### `POST /api/v1/parts/search`
Public hybrid component catalog search combining SQL hard filters with 512-dim cosine similarity ranking (`HashEmbedder` or SentenceTransformers) and optional Octopart/Nexar fallback.

**Request Body (`application/json` — `PartSearchReq`):**
```json
{
  "query": "3.3V low noise LDO",
  "filters": {
    "in_stock": true,
    "v_in_min": 5.0,
    "package": "SOT-23-5",
    "category": "Power",
    "value": "3.3V"
  },
  "k": 10,
  "engine": "auto"
}
```
- `filters` keys (all optional):
  - `in_stock` (`boolean`): requires `stock >= 5`.
  - `v_in_min` (`number`): requires `voltage_max >= v_in_min`.
  - `package` (`string | string[]`): exact match on package name(s).
  - `category` (`string`): case-insensitive substring match (`ILIKE %...%`).
  - `value` (`string`): case-insensitive exact value match.
- `engine`: `"auto"` (local first, Octopart fallback if 0 local hits), `"local"`, or `"octopart"` / `"nexar"`.
- Passing `query: ""` browses catalog parts sorted by `stock DESC` (omits embedding blob column on Postgres to minimize egress).

**Response `200 OK`:**
```json
{
  "results": [ { "...Part..." } ],
  "engine": "local"
}
```
`engine` is `"local"`, `"octopart"`, or `"octopart_fallback"`.

#### `POST /api/v1/parts/octopart/search`
Forces a live Octopart / Altium Nexar GraphQL query (`supSearch`) enriched with local DB or KiCad library symbols/footprints.
- **Request Body:** Same as `PartSearchReq`.
- **Response `200 OK`:** `{ "results": Part[], "engine": "octopart" }`.
- **Errors:** `503 Service Unavailable` if `NEXAR_ACCESS_TOKEN` is unset.

#### `GET /api/v1/parts/octopart/{mpn}`
Looks up a single MPN live on Octopart/Nexar (`supSearchMpn`) for real-time pricing, availability, and Octopart URL.
- **Response `200 OK`:** Single `Part` object.
- **Errors:** `503` if `NEXAR_ACCESS_TOKEN` unset; `404` if MPN not found.

---

### 4.7 Granular 5-Stage Pipeline Endpoints

Use these when building an advanced manual/step-by-step CAD workflow or uploading an existing `.kicad_pcb` board for re-placement and re-routing.

#### Stage 2: `POST /api/v1/projects/{pid}/netlist`
Plans parts (or uses explicit `parts`), generates a grounded netlist with ERC retry loop, and saves `parts.json`, `netlist.json`, `board.kicad_sch`, and `schematic.svg`.

**Request Body (`application/json` — `NetlistReq`):**
```json
{
  "prompt": "USB-C to 3.3V BME280 sensor board",
  "parts": [
    { "ref": "U1", "mpn": "AP2112K-3.3TRG1" }
  ]
}
```
- `parts` is optional (`null` to auto-plan from `prompt`). If provided, every `mpn` must resolve in `PartsDB` (or Octopart).

**Response `200 OK`:**
```json
{
  "netlist_id": "demo",
  "netlist": { "...Netlist..." },
  "erc_status": "PASS",
  "warnings": [],
  "schematic_svg": "<svg ...>...</svg>"
}
```
- **Errors:** `401`, `403`, `422` (unknown MPN or offline non-demo prompt).

---

#### Stage 3: `POST /api/v1/projects/{pid}/place`
Runs force-directed seed + simulated annealing placement + overlap/polygon legalizer.

> **Content-Type:** Strictly **`multipart/form-data`** (NOT JSON), because it supports uploading an existing `.kicad_pcb` file.

**Form Fields:**
| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `netlist_id` | `string` (optional) | `null` | Netlist reference when placing from project's `netlist.json` |
| `board` | `File` (optional) | `null` | Upload a scraped `.kicad_pcb` file (strips existing tracks/vias & stale `filled_polygon`, preserves `Edge.Cuts` polygons/holes and copper pour `zone` definitions) |
| `board_width_mm` | `float` | `40.0` | Board width in mm (netlist mode only) |
| `board_height_mm` | `float` | `30.0` | Board height in mm (netlist mode only) |
| `iterations` | `int` | `3000` | Simulated annealing iterations |
| `seed` | `int` | `0` | Deterministic RNG seed |
| `keep_zones` | `bool` | `true` | Preserve copper pour `(zone ...)` blocks in scraped boards |

**Response `200 OK`:**
```json
{
  "mode": "netlist",
  "board_file_url": "/api/v1/projects/demo/export?format=kicad_pcb",
  "placements": [{ "ref": "J1", "x": 4.2, "y": 15.0, "rotation": 0.0 }],
  "hpwl_mm": 124.6,
  "overlaps": 0,
  "layout_rules": { "edge_components": ["J1", "J2"], "clusters": [["U1", "C1", "C2"]] },
  "zones_preserved": 0,
  "svg": "<svg ...>...</svg>"
}
```
- `mode` is `"netlist"` or `"scraped"`.
- Deletes any stale `board_routed.kicad_pcb` and persists `board.kicad_pcb`, `board.kicad_pro`, and `placement.json`.
- **Errors:** `401`, `403`, `409` (netlist mode without prior `/netlist`), `422` (unparseable `.kicad_pcb` or 0 footprints).

---

#### Stage 4a: `POST /api/v1/projects/{pid}/route`
Enqueues an asynchronous 2-layer routing job (via `WORKER_URL` or in-process `JobHub`). Enforces the `MAX_NETS` (default `64`) net cap before starting.

**Request Body (`application/json` — `RouteReq`):**
```json
{
  "board_file": null,
  "checkpoint": "d2/ppo",
  "max_steps": 300
}
```
- `board_file`: defaults to `"board.kicad_pcb"`.

**Response `202 Accepted`:**
```json
{ "job_id": "job_a1b2c3" }
```
- **Errors:** `401`, `403`, `409` (no unrouted board; call `/place` first), `422` (exceeds `MAX_NETS` 64-net cap).

---

#### Stage 4b: `GET /api/v1/jobs/{jid}/stream`
Streams routing progress as Server-Sent Events (`text/event-stream`).

> **Auth Note for Standalone Frontends:** Standard browser `new EventSource(url)` cannot attach `Authorization: Bearer` headers. If using Bearer JWT auth, stream this endpoint with `fetch(url, { headers: { Authorization: "Bearer ..." } })` and parse SSE blocks manually, OR use session cookies with `new EventSource(url, { withCredentials: true })` and explicit `CORS_ORIGINS`.

**SSE Events:**
- `event: step`
  - `astar` engine: `{"type":"step","step":14,"connected_nets":6,"total_nets":7,"unrouted_count":1,"vias":3,"drc_violations":0,"engine":"astar","svg_snapshot":"<svg..."}` (`svg_snapshot` included on final step)
  - `pcbworld` engine: `{"type":"step","step":14,"connected_nets":6,"total_nets":7,"unrouted_count":1,"drc_violations":0,"ratsnest_reduction":0.86,"engine":"pcbworld"}`
  - `mock` engine: `{"type":"step","step":9,"connected_nets":4,"total_nets":7,"drc_violations":0,"engine":"mock","svg_snapshot":"<svg..."}` (`svg_snapshot` every 3 steps + final)
- `event: done`
  ```json
  {
    "type": "done",
    "output_path": "/data/projects/demo/board_routed.kicad_pcb",
    "final_metrics": { "connected_nets": 7, "total_nets": 7, "unrouted_connections": 0, "segments": 44, "vias": 4, "copper_layers": 2 },
    "engine": "astar"
  }
  ```
- `event: error`
  ```json
  { "type": "error", "message": "unknown job", "trace": "..." }
  ```
  *(Note: `/jobs/{jid}/stream` uses key `"message"`, whereas `/design/stream` uses key `"error"`.)*

---

#### Stage 4c: `POST /api/v1/projects/{pid}/finish`
Runs Freerouting (`freerouting.jar`) on the worker container to complete any remaining unrouted nets.
- **Request Body:** None.
- **Response `200 OK`:** `{ "output_path": "/data/projects/{pid}/board_routed_fr.kicad_pcb" }`.
- **Errors:** `401`, `403`, `404`, `409` (no board), `501 Not Implemented` if `WORKER_URL` is not configured.

---

#### Stage 5a: `GET /api/v1/projects/{pid}/export?format=<fmt>`
Exports CAD source files, rendered SVGs, JLCPCB BOM, or a full fabrication ZIP.

| `format` | Media-Type | Download Header | Description |
| :--- | :--- | :--- | :--- |
| `kicad_pcb` *(default)* | `application/octet-stream` | `attachment; filename="{pid}.kicad_pcb"` | KiCad 9 PCB (`20241229`); serves `board_routed.kicad_pcb` if routed, else `board.kicad_pcb` |
| `kicad_sch` | `application/octet-stream` | `attachment; filename="{pid}.kicad_sch"` | KiCad 9 Schematic (`20250114`); lazily generated from `netlist.json` if absent |
| `schematic_svg` | `image/svg+xml` | *(inline)* | Engineering SVG schematic with pin names & color-coded net pills |
| `svg` | `image/svg+xml` | *(inline)* | PCB board preview SVG (KiCad 9 CLI if available, else built-in SVG renderer) |
| `bom` | `text/csv` | `attachment; filename="{pid}_bom.csv"` | JLCPCB BOM (`Comment,Designator,Footprint,LCSC Part #`) grouped by MPN |
| `gerber` | `application/zip` | `attachment; filename="{pid}_fab.zip"` | KiCad Gerbers + Excellon drill files + `bom.csv` (requires `kicad-cli` locally or on worker) |

- **Errors:** `401`, `403`, `404` (no board or no schematic/netlist yet), `422` (invalid `format`), `501` (`gerber` requested without `kicad-cli` or worker).

---

#### Stage 5b: `GET /api/v1/projects/{pid}/drc`
Runs KiCad 8/9 CLI DRC (`kicad-cli pcb drc --format json --severity-all`) via worker or locally, falling back to the built-in format/courtyard/copper-edge-clearance validator when KiCad CLI is unavailable. Persists the result to `drc.json`.

**Response `200 OK`:** `DrcReport` object (see Section 3).
- **Errors:** `401`, `403`, `404` (no board yet).

---

## 5. Environment Variables & Operational Limits

| Variable | Default | Description |
| :--- | :--- | :--- |
| `CORS_ORIGINS` | `*` | Comma-separated allowed frontend origins; set explicit origins to enable credentialed CORS |
| `GEMINI_API_KEY` | `""` | Enables Gemini LLM; when unset, only built-in USB-C/BME280 demo prompt & regex resize work |
| `LLM_MODEL` | `gemini-2.5-flash` | Primary Gemini model |
| `LLM_FALLBACK_MODELS` | `gemini-2.5-flash-lite,...` | Automatic retry fallback models on transient `429`/`503` |
| `NEXAR_ACCESS_TOKEN` | `""` | Enables live Octopart/Nexar GraphQL component search & MPN lookup |
| `PARTS_SEARCH_ENGINE` | `auto` | Default part search mode (`auto`, `local`, `octopart`) |
| `DATABASE_URL` | `""` | Neon Lakebase Postgres pooled URL (`-pooler`); falls back to local SQLite if empty |
| `DATABASE_URL_UNPOOLED` | `""` | Direct Postgres URL used for startup SQL migrations |
| `NEON_AUTH_BASE_URL` | `""` | Managed Better Auth URL; enables JWT + cookie verification and `/api/auth/*` proxy |
| `NEON_AUTH_JWKS_URL` | `<base>/.well-known/jwks.json` | Override JWKS endpoint for EdDSA JWT verification |
| `AUTH_REQUIRED` | `true` | Enforce authentication on protected routes when `NEON_AUTH_BASE_URL` is configured |
| `WORKER_URL` | `""` | Remote Linux x86_64 KiCad 9 + PCBWorld worker URL (`http://worker:8001` in Docker Compose) |
| `ROUTER_ENGINE` | `auto` | `auto` (PCBWorld → built-in 2-layer A*), `pcbworld`, or `mock` |
| `MAX_NETS` | `64` | Hard cap on routable nets (enforced in ERC, DRC, and `/route`) |
| `DATA_DIR` | `./data` | Local working directory for SQLite and KiCad scratch files |

### Hard Technical Limits
- **2 Copper Layers:** `F.Cu` (front) and `B.Cu` (back) with plated through-hole vias (`0.54–0.60 mm` outer / `0.30 mm` drill).
- **64-Net Cap:** Boards with `> 64` routable nets fail ERC and return `422` on `/route`. Best demo/production results are 5–15 nets.
- **Board Design Rules (`.kicad_pro`):**
  - `Default` class: `0.25 mm` width, `0.20 mm` clearance
  - `power` class: `0.50 mm` width, `0.20 mm` clearance
  - `signal` class: `0.25 mm` width, `0.20 mm` clearance
  - Minimum copper-to-edge clearance: `0.20 mm` (`EDGE_MARGIN = 0.35 mm` during placement, `0.52 mm` during A* routing).
