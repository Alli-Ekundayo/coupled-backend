# coupled-backend Standalone Frontend — Architecture & Design Guide

This guide specifies how to build a **standalone, decoupled frontend** for the headless `coupled-backend` backend API ([API.md](./API.md)). It preserves the Flux-inspired AI PCB CAD studio UX while eliminating monolithic static serving from the Python backend.

---

## 1. Recommended Stack & Repository Architecture

### Suggested Tech Stack
| Concern | Recommended Choice | Rationale |
| :--- | :--- | :--- |
| **Framework** | React 19 + TypeScript + Vite (or Next.js App Router) | Fast builds, strict typing for CAD geometry & API schemas, easy static/CDN deployment |
| **Server State** | TanStack Query (`@tanstack/react-query`) | Caching for `/health`, `/api/v1/projects`, `/api/v1/projects/{pid}`, parts search, and automatic invalidation after mutations/SSE completion |
| **Client CAD State** | Zustand | Zero-rerender-overhead store for high-frequency 60fps camera pan/zoom, layer toggles, selection, net highlighting, and SSE telemetry |
| **Styling** | Tailwind CSS v4 + CSS custom properties (tokens below) | Dense CAD dark-mode UI with crisp borders, monospace metrics, and zero layout shift |
| **2D PCB Viewport** | HTML5 `<canvas>` (DPR-scaled) | Smooth 60fps pan/zoom/hit-testing across hundreds of pads, segments, vias, courtyards, and polygons |
| **3D PCB Viewport** | Three.js / React Three Fiber *(or lightweight Canvas2D isometric projector)* | Real PBR FR-4 + ENIG gold pads + extruded SMD/THT package bodies + orbit controls |
| **Auth Client** | `@neon/auth` / `better-auth/react` *(when Neon Auth enabled)* | Supports email/password, OAuth, session cookies, and EdDSA Bearer JWTs |

### Recommended Folder Structure
```text
coupled-web/
├── .env.example                  # VITE_API_BASE_URL=http://localhost:8000
├── src/
│   ├── api/
│   │   ├── client.ts             # Typed fetch wrapper, auth headers, error normalization
│   │   ├── sse.ts                # Reusable POST/GET SSE ReadableStream parser
│   │   ├── types.ts              # Shared TypeScript interfaces matching docs/API.md
│   │   └── endpoints.ts          # Typed methods for every coupled-backend route
│   ├── auth/
│   │   ├── AuthProvider.tsx      # Bootstraps from GET /api/auth/config, manages JWT/session
│   │   └── AuthGate.tsx          # Sign-in / sign-up modal or route when auth.required === true
│   ├── cad/
│   │   ├── sexpr.ts              # KiCad 9 S-expression tokenizer & AST parser
│   │   ├── parseKicadPcb.ts      # Extracts footprints, pads, segments, vias, Edge.Cuts, zones
│   │   ├── math.ts               # KiCad Y-down CCW rotation, world↔screen transforms, hit-testing
│   │   ├── PcbCanvas2D.tsx       # Interactive 2D PCB canvas
│   │   ├── PcbCanvas3D.tsx       # Interactive 3D PCB board preview
│   │   └── SchematicViewer.tsx   # XSS-safe SVG schematic viewer with pan/zoom/fit
│   ├── store/
│   │   └── useStudioStore.ts     # Active project, view, selection, layers, camera, API trace log
│   ├── components/
│   │   ├── Topbar.tsx            # Brand, project switcher, view tabs, health pills, Export & User menu
│   │   ├── left-sidebar/
│   │   │   ├── LibraryTab.tsx    # Local/Octopart part search + filters
│   │   │   ├── ObjectsTab.tsx    # Components list + Nets tree (click-to-highlight)
│   │   │   └── PipelineTab.tsx   # Granular Stage 2/3/4 controls + scraped .kicad_pcb uploader + 64-net meter
│   │   ├── overlays/
│   │   │   ├── BoardBadge.tsx    # Top-left live board dimensions / part / segment / via stats
│   │   │   ├── CanvasHud.tsx     # Bottom-left 2D/3D toggle, layer pills, zoom, cursor mm readout
│   │   │   └── AiCopilotDrawer.tsx # Collapsible top-right Design From Scratch + Chat & Refine drawer
│   │   ├── right-sidebar/
│   │   │   ├── InspectorPanel.tsx # Selected part hero, properties, Octopart pricing/stock sync, pinout table
│   │   │   ├── DrcPanel.tsx      # KiCad 9 DRC metrics & violation list
│   │   │   └── BomSummaryCard.tsx # Unit BOM estimate
│   │   ├── CodeInspectorView.tsx # Tabbed viewer: API log, netlist.json, .kicad_pcb, .kicad_sch, drc.json, brief
│   │   └── ExportModal.tsx       # Accessible <dialog> for KiCad, Gerber ZIP, BOM CSV, SVGs
│   └── App.tsx
```

---

## 2. Backend Integration & Auth Patterns

### Environment Configuration
Configure a single API base URL in the frontend:
```env
VITE_API_BASE_URL=http://localhost:8000
```
On the `coupled-backend` backend, set `CORS_ORIGINS` to your frontend origin(s) whenever using cookie auth or deploying to production:
```env
CORS_ORIGINS=http://localhost:5173,https://studio.coupled-backend.dev
```

### Authentication Flow (Capability-Driven)
On app mount, call `GET /health` and `GET /api/auth/config` in parallel:
1. **If `auth.enabled === false` or `auth.required === false`:** Enter the CAD workspace immediately without forcing login (show optional Sign In button if `auth.enabled === true`).
2. **If `auth.required === true`:**
   - **Recommended Decoupled Pattern (Bearer JWT):**
     - Authenticate the user against `config.neon_auth_base_url` (or via `/api/auth/*` proxy).
     - Extract the EdDSA JWT from the `set-auth-jwt` response header (exposed via CORS) or Better Auth's JWT/token endpoint.
     - Attach `Authorization: Bearer <jwt>` to all `/api/v1/*` requests (including `fetch`-based SSE streams and file downloads).
     - Verify session on boot via `GET /api/v1/me`. On `401`, refresh token or prompt sign-in.
   - **Alternative Cookie Pattern (Explicit `CORS_ORIGINS` required):**
     - Authenticate via `{API_BASE}/api/auth/*` with `credentials: "include"`.
     - Pass `credentials: "include"` on all `fetch()` calls (and `{ withCredentials: true }` on `EventSource`).
     - Using `@neon/auth` in your standalone frontend:
       ```ts
       import { createAuthClient } from "@neon/auth";
       import { BetterAuthReactAdapter } from "@neon/auth/react/adapters"; // or BetterAuthVanillaAdapter

       export const authClient = createAuthClient(`${import.meta.env.VITE_API_BASE_URL}/api/auth`, {
         adapter: BetterAuthReactAdapter({ fetchOptions: { credentials: "include" } }),
       });
       ```

### Unified SSE Streaming Helper (Works for BOTH `POST` and `GET` + Bearer Auth)
Do **not** rely on browser `EventSource` for authenticated streams, because `EventSource` cannot send `POST` bodies or `Authorization: Bearer` headers. Use one `fetch()` + `ReadableStream` parser for both `POST /api/v1/design/stream` and `GET /api/v1/jobs/{jid}/stream`:

```ts
export async function streamSse<T = Record<string, unknown>>(
  url: string,
  init: RequestInit,
  onEvent: (eventName: string, data: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, { ...init, signal });
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    throw new Error(`SSE ${init.method ?? "GET"} ${url} (${res.status}): ${text}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";

    for (const block of blocks) {
      let eventName = "message";
      const dataLines: string[] = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (!dataLines.length) continue;
      onEvent(eventName, JSON.parse(dataLines.join("\n")) as T);
    }
  }
}
```

### Authenticated File Downloads (`.kicad_pcb`, `.kicad_sch`, `bom.csv`, `_fab.zip`)
When `auth.required === true` and you use Bearer JWTs, plain `<a href="/api/v1/projects/.../export">` tags will fail with `401` because anchor navigations do not attach `Authorization` headers. Instead, trigger downloads via `fetch()` + `Blob` and read the filename from the CORS-exposed `Content-Disposition` header:

```ts
export async function downloadExport(url: string, token?: string, fallbackName = "export.bin") {
  const res = await fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    credentials: token ? "omit" : "include",
  });
  if (!res.ok) throw new Error(`Export failed (${res.status}): ${await res.text()}`);
  const disp = res.headers.get("Content-Disposition") ?? "";
  const match = disp.match(/filename="([^"]+)"/);
  const filename = match?.[1] ?? fallbackName;

  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(objectUrl);
}
```

---

## 3. Visual Design System (Dark CAD Studio)

The UI follows a high-density, low-glare **Dark PCB Engineering Studio** aesthetic inspired by Flux.ai and modern KiCad EDA workflows. High contrast is reserved for active copper layers, selected footprints, and DRC error states.

### Color Tokens
```css
:root {
  color-scheme: dark;

  /* Surfaces (elevated dark graphite hierarchy) */
  --bg-canvas:    #111214;   /* Infinite CAD viewport backdrop */
  --bg-panel:     #1b1c20;   /* Topbar & left/right sidebars */
  --bg-panel-alt: #22242a;   /* Cards, section boxes, pills */
  --bg-card:      #262830;   /* Buttons & hovered cards */
  --bg-input:     #15161a;   /* Inputs, textareas, code blocks */
  --border:       #2e313a;   /* 1px hairlines */
  --border-strong:#3f4350;   /* Elevated panels, floating overlays */

  /* Typography */
  --text:         #eceef2;
  --text-muted:   #989dab;
  --text-dim:     #6c7080;

  /* Brand & AI Accents */
  --accent:       #00e676;   /* Active selection, primary CTA, DRC pass */
  --accent-dim:   rgba(0, 230, 118, 0.14);
  --accent-text:  #062113;   /* Text on --accent buttons */
  --ai:           #a78bfa;   /* AI Copilot highlights & running stage pulses */
  --ai-grad:      linear-gradient(135deg, #7c3aed, #2563eb);

  /* Semantic EDA / PCB Layer Colors */
  --substrate-2d: #081b15;   /* Dark solder-mask PCB fill in 2D */
  --substrate-3d: #166534;   /* Classic green FR-4 top in 3D */
  --edge-cuts:    #e8c85a;   /* Board perimeter outline */
  --copper-f:     #ef4444;   /* Front copper (F.Cu) & power rails */
  --copper-b:     #3b82f6;   /* Back copper (B.Cu) & signal nets */
  --pad-gold:     #e0b44b;   /* ENIG gold SMD/THT pads */
  --via-silver:   #d8dee9;   /* Plated through-hole via ring */
  --silk:         rgba(241, 245, 249, 0.78);
  --courtyard:    rgba(148, 163, 184, 0.32);

  /* Diagnostics */
  --ok:           #00e676;
  --warn:         #f59e0b;
  --err:          #f43f5e;
}
```

### Typography & Density
- **UI Sans:** `Inter, ui-sans-serif, system-ui, sans-serif` — compact CAD base `12.5px` (`line-height: 1.4`), uppercase micro-headers `10.5px` (`letter-spacing: 0.06em`, `font-weight: 700`).
- **Engineering Mono:** `"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace` — mandatory for reference designators (`U1`, `R2`), MPNs, LCSC IDs (`C51118`), mm coordinates, DRC counts, net names, and code views.
- **Viewport Grid Backdrop:** Dual-scale subtle millimeter grid over `--bg-canvas` (`20px` minor lines at `rgba(255,255,255,0.03)`, `100px` major lines at `rgba(255,255,255,0.06)`).
- **Focus & Motion:** `2px solid var(--accent)` `:focus-visible` ring; crisp `120–150ms` transitions; gentle opacity pulse on actively running SSE pipeline stages.

---

## 4. Workspace Layout & Information Architecture

Design the primary desktop studio as a full-viewport (`100dvh`, `overflow: hidden`) **3-Pane CAD Shell** so the canvas never scrolls away and controls stay reachable without modal fatigue.

```text
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ TOPBAR (46px)                                                                            │
│ [C] coupled-backend  [Project Select ▾][+][↻]  │ [Schematic][Code/API][PCB][3D] │ ●LLM ●Octo ●DRC [EXPORT] [User] │
├────────────────────┬───────────────────────────────────────────────┬─────────────────────┤
│ LEFT SIDEBAR       │ CENTER INTERACTIVE VIEWPORT (1fr)             │ RIGHT INSPECTOR     │
│ (300px)            │                                               │ (310px)             │
│ [Library|Obj|Pipe] │  ┌─ BoardBadge (top-left) ──┐  ┌─ Copilot ──┐ │ ┌─ Selected Part ─┐ │
│                    │  │ demo · 40×30mm · 9 parts │  │ ✦ AI [▼]   │ │ │ Icon + MPN/Ref  │ │
│ • Library Search   │  └──────────────────────────┘  │ (drawer    │ │ │ Properties KV   │ │
│   - Query + Engine │                                │  expands   │ │ └─────────────────┘ │
│   - V_in min/Stock │         2D / 3D PCB            │  downward) │ │ ┌─ Pricing/LCSC ──┐ │
│   - Part Cards     │       or Schematic SVG         └────────────┘ │ │ Stock|Price|LCSC│ │
│                    │      or Code/API Log                          │ │ [Octopart ↻]    │ │
│ • Objects Tree     │                                               │ └─────────────────┘ │
│   - Components     │                                               │ ┌─ Pinout & Nets ─┐ │
│   - Nets (x / 64)  │                                               │ │ Pin|Name|Typ|Net│ │
│                    │  ┌─ Floating Canvas HUD (bottom-left) ──────┐ │ └─────────────────┘ │
│ • Pipeline & Rules │  │ 2D|3D │ F.Cu B.Cu Silk CrtYd │ - Fit + │mm│ │ ┌─ KiCad 9 DRC ───┐ │
│   - Stage 2/3/4    │  └──────────────────────────────────────────┘ │ │ Err | Warn | Unc│ │
│   - Scraped Upload │                                               │ │ Violation List  │ │
│   - 64-Net Meter   │                                               │ └─────────────────┘ │
│                    │                                               │ ┌─ BOM Summary ───┐ │
└────────────────────┴───────────────────────────────────────────────┴─────────────────────┘
```

### Key Layout Rules
1. **Non-Colliding Overlays:**
   - Pin the **Board Badge** to `top: 12px; left: 14px` (`pointer-events: none`), visible only in `PCB` and `3D` views.
   - Pin the **Canvas HUD** to `bottom: 14px; left: 14px`. Show layer toggles (`2D`, `3D`, `F.Cu`, `B.Cu`, `Silk`, `CrtYd`) and `mm` cursor readout in `PCB`/`3D` views; show only zoom controls (`-`, `Fit`, `+`) in `Schematic` view; hide in `Code / API` view.
   - Pin the **AI Copilot Drawer** to `top: 12px; right: 14px` (`width: 365px`, `max-height: calc(100% - 76px)`). Start **collapsed** when an existing project loads so the board is unobstructed; auto-expand when creating a new/empty project.
2. **Capability-Driven Health Pills (Topbar Right):**
   - `Gemini` (`ok` if `health.llm`, else `warn` `"Offline"`)
   - `Octopart` (`ok` if `health.octopart`, else `warn` `"No Octo"`)
   - `Router` (`PCBWorld RL` / `A* 2L` / `Mock` from `health.active_router`)
   - `KiCad` (`KiCad <version>` if `health.kicad_cli`, else `warn` `"No KiCad"`)
   - `DRC` (`DRC PASS` green when `passed && error_count === 0 && unconnected_count === 0`, else red error/unconnected count)
   - Disable or tooltip-explain worker-only controls (`Finish` Freerouting button, `Gerbers + Drill ZIP` export) when `health.worker === "in-process"` and `!health.kicad_cli`, and disable Octopart buttons when `!health.octopart`.
3. **Responsive Adaptation:**
   - Below `1100px`, collapse Left & Right sidebars into slide-over drawers toggled from the topbar so the CAD canvas remains usable on smaller laptops/tablets.

---

## 5. Client-Side CAD Rendering Specification

To deliver a smooth interactive editor instead of static raster/SVG previews, parse the raw `.kicad_pcb` file fetched from `GET /api/v1/projects/{pid}/export?format=kicad_pcb` directly in the browser.

### 5.1 Coordinate System & Rotation Math
KiCad `.kicad_pcb` files use **millimeters** with **X right, Y down**, and **positive angles counter-clockwise (CCW) on screen**:

```ts
/** Rotate local offset (px, py) by footprint rotation angle (deg, KiCad CCW in Y-down frame). */
export function rotatePt(px: number, py: number, rotDeg: number): [number, number] {
  const rad = (rotDeg * Math.PI) / 180;
  const c = Math.cos(rad);
  const s = Math.sin(rad);
  return [px * c + py * s, -px * s + py * c];
}
```
- **World → Screen (2D):** `sx = (wx - cam.cx) * cam.scale + W / 2`, `sy = (wy - cam.cy) * cam.scale + H / 2`.
- **Hit-Testing Footprints on Click:** Convert click `(sx, sy)` to world `(wx, wy)`, transform into footprint local space via `rotatePt(wx - fp.x, wy - fp.y, -fp.rot)`, and test against `[bx0 - 0.5, by0 - 0.5, bx1 + 0.5, by1 + 0.5]`.

### 5.2 Parsing `.kicad_pcb` S-Expressions
Parse the S-expression tree with a lightweight stack tokenizer (`/\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+/g`) and extract:
1. **`(net <id> "<name>")`** → lookup map `netNames[id] = name`.
2. **`(footprint "<lib>" ...)`**:
   - Reference & Value from `(property "Reference" ...)` / `(property "Value" ...)` (fallback to legacy `(fp_text reference ...)`).
   - Placement `(at x y [rot])`.
   - Pads `(pad "<num>" <smd|thru_hole|np_thru_hole> <rect|roundrect|circle|oval> (at px py [ang]) (size w h) [(drill d [dy])] [(net id name)])`.
     - If pad `(at ...)` omits angle, default `ang = fp.rot`.
   - Graphics `(fp_line ...)` and `(fp_rect ...)` (expand `fp_rect` to 4 line segments) on `F.SilkS` and `F.CrtYd`.
   - Footprint local `bbox`: prefer extents of `F.CrtYd` lines if present, else pad extents padded by `0.3 mm`.
3. **`(segment (start x1 y1) (end x2 y2) (width w) (layer "F.Cu"|"B.Cu") (net id))`** → copper traces.
4. **`(via (at x y) (size d) (drill dr) (net id))`** → plated through-hole vias.
5. **`(gr_line ... (layer "Edge.Cuts"))`** (and optional `gr_rect`/`gr_poly`) → chain endpoint-adjacent segments (tolerance `0.08 mm`) into closed outer polygon + interior cutout holes (for scraped non-rectangular/notched boards).
6. **`(zone (net_name ...) (layer ...) (polygon ...))`** → optional translucent copper pour outline preview on scraped boards.

### 5.3 2D PCB Layer Render Order (Back to Front)
Always scale canvas backing store by `window.devicePixelRatio` (`ctx.setTransform(dpr, 0, 0, dpr, 0, 0)`):
1. **Board Substrate Polygon** (`#081b15` fill + soft drop shadow) + **Cutout Holes** (`#111214`) + **`Edge.Cuts` Perimeter** (`#e8c85a`).
2. **Back Copper (`B.Cu`) Segments** (`#3b82f6`, `lineCap = "round"`).
3. **Front Copper (`F.Cu`) Segments** (`#ef4444`, `lineCap = "round"`).
   - *Net Highlighting:* When `highlightedNet` is active, draw matching segments/vias/pads in `#00e676` (`1.4×` stroke width, `alpha = 1.0`) and dim non-matching traces to `alpha = 0.28`.
4. **Plated Vias** (outer ring `#d8dee9`, inner drill hole `#111214`).
5. **Footprint Courtyards (`F.CrtYd`) & Silkscreen (`F.SilkS`)** rotated by `fp.rot`.
6. **Selected Footprint Highlight Box** (`rgba(0,230,118,0.12)` fill + `1.8px` `#00e676` stroke).
7. **Pads** (rotated by `-pd.ang`, `#eab308` THT / `#d9a940` SMD, rounded corners for `roundrect`/`circle`/`oval`, dark drill center if `drill > 0`).
8. **Reference Labels** (`U1`, `R1`, etc.) centered at `(fp.x, fp.y)` when `layers.silk && cam.scale >= 4.5`.

### 5.4 3D Board Preview Spec
Whether implemented in **Three.js / React Three Fiber** (recommended for a standalone app) or a lightweight **Canvas2D painter's-algorithm isometric renderer**:
- Extrude the `Edge.Cuts` polygon (minus cutout holes) to standard **1.6 mm FR-4 thickness** (`z = -1.6` to `0.0`).
- Render `B.Cu` traces on bottom face (`z = -1.62` or translucent under-substrate), `F.Cu` traces at `z = +0.04`, ENIG gold pads and vias at `z = +0.06`.
- Classify 3D package heights & materials by `ref`, `lib`, and `value`:
  - USB-C receptacle (`usb`): `3.2 mm` brushed metallic shell (`#cbd5e1` / `#94a3b8`)
  - Pin headers (`header`/`hdr` or `J*`): `2.6 mm` dark plastic housing + `5.0 mm` vertical gold pins on every pad
  - LEDs (`led` or `D*`): `0.9 mm` translucent emerald/amber lens
  - Ceramic caps (`C*`): `0.85 mm` tan ceramic body (`#b4834c`) + silver end caps
  - Resistors (`R*`): `0.65 mm` matte black body (`#1f2430`) + silver end caps
  - Metal-lid LGA sensors (`lga`/`bme`): `1.0 mm` satin silver lid (`#d1d5db`)
  - Standard ICs (`U*`): `1.35 mm` epoxy black body (`#222631`)
- Support orbit drag (`yaw`, clamped `pitch`), scroll zoom, click-to-select footprint, and floating silkscreen labels.

### 5.5 Schematic SVG Viewer
- Fetch SVG from `GET /api/v1/projects/{pid}/export?format=schematic_svg` (or take `schematic_svg` directly from SSE `stage: "schematic"`).
- **XSS Hardening:** Always parse via `DOMParser().parseFromString(svgText, "image/svg+xml")`, strip any `<script>` tags and inline `on*` event attributes, and import safely into the DOM (never raw `dangerouslySetInnerHTML` on unverified strings).
- Wrap inside an overflow-hidden pan/zoom stage with CSS `transform: translate(x, y) scale(s)` and a `Fit` calculation reading `svg.viewBox.baseVal`.

---

## 6. Core User Flows & State Synchronization

### Flow A: App Bootstrap
1. Parallel fetch `GET /health` + `GET /api/auth/config`.
2. If `auth.required === true`, verify session via `GET /api/v1/me`; if `401`, present Sign In / Sign Up UI.
3. Parallel load `GET /api/v1/projects` and initial catalog browse `POST /api/v1/parts/search` (`{ query: "", filters: { in_stock: true }, k: 12, engine: "auto" }`).
4. Auto-select the most recent project (`projects[0]` — Postgres returns `updated_at DESC`) and run **Project Hydration**.

### Flow B: Canonical Project Hydration (`loadProject(pid)`)
1. Fetch `GET /api/v1/projects/{pid}`.
2. Normalize parts map:
   - Prefer `detail.parts` (`Record<string, Part>`).
   - Fallback to `detail.components` (handle both object-map and `SourcingEntry[]` array shapes).
   - If any part lacks `pins` (e.g., scraped board), extract pin numbers from parsed footprint pads.
3. Fetch CAD artifacts in parallel (`Promise.allSettled` — some stages may not exist yet):
   - `GET /api/v1/projects/{pid}/export?format=kicad_pcb` → `parseKicadPcb()` → render 2D + 3D + fit camera.
   - `GET /api/v1/projects/{pid}/export?format=kicad_sch` → populate Code view.
   - `GET /api/v1/projects/{pid}/export?format=schematic_svg` → render Schematic view.
4. Populate Objects tree, Net Cap meter (`Object.keys(netlist.nets).length / 64`), DRC panel, Chat history, and Export links.

### Flow C: Live End-to-End AI Design (SSE)
1. Display a 6-pill progress stepper: `Reqs` → `Parts` → `Sch` → `Place` → `Route` → `DRC`.
2. Submit `POST /api/v1/design/stream` using `streamSse()`.
3. React to live frames:
   - Map `stage: "netlist"` (`running`) onto the `Sch` pill; mark `Sch` `done` on `stage: "schematic"` and immediately preview `ev.schematic_svg`!
   - On `stage: "placement"` (`done`), show board size & HPWL metrics in status bar.
   - On `routing_step`, update live routing progress (`Step X · connected/total nets · vias`) and optionally preview `ev.svg_snapshot`.
   - On `stage: "drc"` (`done`), populate DRC badges immediately.
   - On `done`, invalidate project list query and run `loadProject(ev.project_id)` to hydrate the interactive 2D/3D `.kicad_pcb` canvas.
   - Support an **AbortController** Cancel button for long-running LLM/routing requests.

### Flow D: Conversational Refinement (`/chat`)
1. Append user message optimistically to local chat feed.
2. Call `POST /api/v1/projects/{pid}/chat`.
3. Inspect `res.action`:
   - If `res.design !== null` (`update_board_size`, `re_place`, `add_or_swap_parts`, `full_redesign`, `new_design`): show action toast and re-hydrate the project (`loadProject(res.project_id)`) so 2D, 3D, Schematic, Objects, BOM, and DRC stay in lockstep.
   - If `res.action === "answer_only"`: append `res.reply` without reloading CAD files.

### Flow E: Parts Library & Live Octopart Sync
1. Search via `POST /api/v1/parts/search` (or `POST /api/v1/parts/octopart/search` when user explicitly picks `Octopart Live`).
2. Clicking a library part opens it in the Right Inspector with badge `LIB` (distinct from board designators like `U1`).
3. Clicking `Octopart ↻` in the Inspector calls `GET /api/v1/parts/octopart/{encodeURIComponent(mpn)}` and merges fresh `stock`, `price`, and `octopart_url` while preserving verified `pins`.

### Flow F: Granular Stage Controls & Scraped Board Upload
Expose an advanced **Pipeline & Rules** tab for power users & debugging:
- **Stage 2:** `POST /api/v1/projects/{pid}/netlist` (JSON prompt + optional explicit `parts` override).
- **Stage 3 (Netlist):** `POST /api/v1/projects/{pid}/place` (`FormData`: `netlist_id`, `board_width_mm`, `board_height_mm`, `iterations`, `seed`).
- **Stage 3 (Scraped `.kicad_pcb`):** Drag-and-drop file input sending `FormData` with `board` file + `keep_zones` checkbox (`"true"`/`"false"`). Display `zones_preserved` count on completion.
- **Stage 4:** `POST /api/v1/projects/{pid}/route` (`202` → `job_id`) followed immediately by `streamSse("GET", "/api/v1/jobs/{jid}/stream")`. On `done`, re-hydrate project and automatically trigger `GET /api/v1/projects/{pid}/drc`.
- **Stage 4 Finish:** `POST /api/v1/projects/{pid}/finish` (only enabled when `health.worker !== "in-process"`).
- **Delete Project:** Confirmation dialog calling `DELETE /api/v1/projects/{pid}`, clearing active workspace state and invalidating project list.

---

## 7. Backend Gotchas & Edge-Case Checklist

Handle these verified backend behaviors explicitly in your standalone frontend:

- [ ] **`POST /api/v1/projects/{pid}/place` requires `multipart/form-data`:** Never send `application/json` to `/place` — FastAPI expects `Form(...)` / `File(...)` fields.
- [ ] **SSE Error Key Difference:** `/api/v1/design/stream` emits `{ type: "error", error: "..." }`, whereas `/api/v1/jobs/{jid}/stream` emits `{ type: "error", message: "...", trace?: "..." }`. Read `ev.error ?? ev.message` in your shared handler.
- [ ] **SSE Stage Name Normalization:** In `/api/v1/design/stream`, Stage 2 emits `stage: "netlist"` on `running` and `stage: "schematic"` on `done`. Map both to the same UI step.
- [ ] **SSE Stream HTTP 200 on Failure:** Once streaming begins, exceptions are emitted as an `event: error` frame inside an HTTP `200` stream — always listen for `event: error` rather than relying solely on `res.ok`.
- [ ] **`GET /api/v1/projects/{pid}` `parts` vs `components`:** Always read `detail.parts` for the full `Record<ref, Part>` dictionary with pinouts; `detail.components` may be a `SourcingEntry[]` array (missing `pins`, `category`, `package`, `manufacturer`).
- [ ] **64-Net Hard Cap:** Warn visually in the Objects/Constraints meter as net count approaches `64`, and handle `422` cleanly on `/route` if a scraped board exceeds `MAX_NETS`.
- [ ] **Offline Mode (`health.llm === false`):** Surface an `"Offline Demo Mode"` banner explaining that arbitrary prompts return `422` without `GEMINI_API_KEY`, and offer quick-prompt chips matching the built-in USB-C/BME280 demo & `<W>x<H>` chat resize.
- [ ] **Worker-Gated Capabilities (`501` / `503`):**
  - `501` on `POST /finish` and `GET /export?format=gerber` when running without KiCad CLI / worker container.
  - `503` on `/api/v1/parts/octopart/*` when `NEXAR_ACCESS_TOKEN` is unset.
- [ ] **Multi-Tenant `403` Handling:** If a user navigates via deep link to a `project_id` owned by another user, display a clear "Access Denied" empty state rather than crashing the canvas.
- [ ] **API Trace Log (Developer Mode):** Record `{ timestamp, method, path, status, durationMs, preview }` inside your `apiClient` wrapper and surface it in the `Code / API` view tab — invaluable for debugging LLM/router latency and inspecting generated `.kicad_pcb`, `.kicad_sch`, `netlist.json`, and `drc.json`.
