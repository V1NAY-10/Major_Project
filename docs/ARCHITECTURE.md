# AetherCAD: Technical Documentation

This document explains how AetherCAD works from end to end: every process, module, algorithm, data
structure and HTTP endpoint, and how a chat message turns into a verified geometry edit inside FreeCAD.
It describes the code as it exists in this repository. For setup instructions see the
[README](../README.md); for the original design rationale see [PLAN.md](PLAN.md).

## Contents

1. [What AetherCAD does](#1-what-aethercad-does)
2. [System architecture](#2-system-architecture)
3. [Repository map](#3-repository-map)
4. [Startup and process lifecycle](#4-startup-and-process-lifecycle)
5. [The FreeCAD bridge (RPC layer)](#5-the-freecad-bridge-rpc-layer)
6. [The engine: documents and objects](#6-the-engine-documents-and-objects)
7. [Feature recognition](#7-feature-recognition)
8. [Stable feature IDs](#8-stable-feature-ids)
9. [Edit operations](#9-edit-operations)
10. [Transactions, validation and verification](#10-transactions-validation-and-verification)
11. [Backend services](#11-backend-services)
12. [The AI agent](#12-the-ai-agent)
13. [HTTP API reference](#13-http-api-reference)
14. [Frontend](#14-frontend)
15. [End-to-end walkthroughs](#15-end-to-end-walkthroughs)
16. [Data model reference](#16-data-model-reference)
17. [Configuration reference](#17-configuration-reference)
18. [Troubleshooting](#18-troubleshooting)
19. [Known limitations and technical debt](#19-known-limitations-and-technical-debt)
20. [Extending AetherCAD](#20-extending-aethercad)
21. [Glossary](#21-glossary)

---

## 1. What AetherCAD does

AetherCAD is a conversational CAD editor. A user uploads an existing solid model (STEP, IGES, BREP or
FreeCAD `.FCStd`) and then talks to it:

- **Questions**: "How thick is the plate?", "List the holes", "Distance between H2 and H4?"
- **Edits**: "Make the 6 mm holes 8 mm", "Make it 120 mm long", "Fillet the vertical edges R5",
  "Set the pad length to 25".

The core design decisions:

| Decision | Why |
|---|---|
| **FreeCAD is the only geometry kernel and the source of truth.** | All geometry lives in a real FreeCAD document per session. The web app never computes geometry itself. |
| **The LLM calls typed tools with exact numbers, not free-form code.** | Deterministic Python inside FreeCAD performs the edit, so results are reproducible and checkable. |
| **Features get stable IDs (`H3`, `P1`…) matched by geometry.** | The LLM and the user can refer to "H3" and it means the same hole across edits. |
| **Every edit is transactional and verified.** | Failed or invalid edits are rolled back; successful ones are re-measured and reported. |
| **Every turn that changes geometry is a version.** | Undo/redo restores exact snapshots in FreeCAD and the viewer. |

---

## 2. System architecture

```mermaid
flowchart LR
    subgraph Browser
        UI["Next.js app<br/>chat · 3D viewer · model panel"]
    end
    subgraph Backend["FastAPI backend (python main.py, :8000)"]
        R["routes.py / main.py<br/>HTTP endpoints"]
        A["agent/runner.py<br/>LLM ⇄ tools loop"]
        S["cad_service.py<br/>sessions · versions"]
        C["freecad_client.py<br/>TCP client + autostart"]
        M["messages.py / db.py"]
    end
    subgraph FreeCAD["FreeCAD process (GUI or freecadcmd)"]
        SV["freecad_bridge/server.py<br/>JSON-RPC :6666"]
        E["freecad_bridge/engine.py<br/>analysis · operations · export"]
    end
    LLM[("LLM provider<br/>Groq / any OpenAI-compatible")]
    DB[("MongoDB<br/>sessions · messages")]
    FS[("backend/data/sessions/&lt;id&gt;/<br/>FCStd · STL · meta.json")]
    CL[("Clerk<br/>auth")]

    UI -- HTTP/JSON --> R
    UI -. sign-in, session token .-> CL
    R -. verify JWT via JWKS .-> CL
    R --> A
    R --> S
    A --> S
    A -- chat.completions --> LLM
    S --> C
    C -- "4-byte length + JSON" --> SV
    SV --> E
    E <--> FS
    S <--> FS
    R --> M
    M <--> DB
```

### Processes and ports

| Process | Started by | Port | Role |
|---|---|---|---|
| Next.js dev server | `npm run dev` in `frontend/` | 3000 | Serves the UI; Clerk middleware guards pages |
| FastAPI (Uvicorn) | `python main.py` in `backend/` | 8000 | HTTP API, LLM agent, session store |
| FreeCAD + bridge | Auto-launched by the backend, or by running `freecad_listener.py` | 6666 (localhost only) | Geometry kernel; executes engine methods |
| MongoDB | External (e.g. Atlas) | — | Optional persistence of sessions and chat |
| LLM provider | External | — | Groq by default (`openai/gpt-oss-120b`) |

The browser only talks to the backend, and every request carries the user's Clerk session token. The backend
is the only client of FreeCAD and of the LLM.

---

## 3. Repository map

```
Major_Project/
├── README.md                     Quick start
├── docs/
│   ├── ARCHITECTURE.md           This document
│   └── PLAN.md                   Original rebuild plan and roadmap
├── backend/
│   ├── main.py                   FastAPI app object, lifespan, middleware, CORS, entry point
│   ├── freecad_listener.py       Macro to run inside the FreeCAD GUI; starts the bridge
│   ├── requirements.txt          fastapi, uvicorn, openai, pydantic, python-dotenv, motor, pymongo…
│   ├── .env.example              Configuration template (copy to .env)
│   ├── app/
│   │   ├── auth.py               Clerk session-token verification (FastAPI dependency)
│   │   ├── sessions.py           /sessions endpoints + session ownership checks
│   │   ├── routes.py             /freecad/status, /cad/*, /chat endpoints + error mapping
│   │   ├── cad_service.py        Per-session folders, meta.json, locks, versions, undo/redo, export
│   │   ├── freecad_client.py     JSON-RPC client, FreeCAD executable discovery and auto-launch
│   │   ├── messages.py           Chat message persistence + history for the LLM
│   │   ├── db.py                 MongoDB connection with in-memory fallback
│   │   └── agent/
│   │       ├── runner.py         One chat turn: LLM ⇄ tool loop, budget, retries, commit
│   │       ├── prompt.py         System prompt text and compact model summary
│   │       └── tools.py          Tool JSON schemas and the tool executor
│   ├── freecad_bridge/           Code that runs INSIDE FreeCAD's Python
│   │   ├── server.py             Socket server, framing, GUI-thread marshalling
│   │   ├── engine.py             Feature recognition, ID registry, operations, verification, export
│   │   └── headless.py           Entry point for `freecadcmd`
│   └── data/sessions/<id>/       Runtime data (created on upload; not in git)
└── frontend/
    ├── next.config.ts            Dev origins, server-action body limit
    └── src/
        ├── middleware.ts         Clerk: every route except /sign-in and /sign-up requires auth
        ├── app/
        │   ├── layout.tsx        ClerkProvider + ThemeProvider
        │   ├── page.tsx          Main page: chat, upload, undo/redo, 3D workspace
        │   ├── sign-in/…         Clerk sign-in page
        │   └── sign-up/…         Clerk sign-up page
        ├── components/
        │   ├── chat/ChatMessage.tsx   Message bubble + operation cards with verified values
        │   ├── chat/ChatInput.tsx     Textarea, attach button, suggestions
        │   ├── cad/CADViewer.tsx      React Three Fiber STL viewer (Z-up)
        │   ├── cad/ModelPanel.tsx     Size, features, parameters, version history
        │   └── ui/Sidebar.tsx         Session list, rename, new chat
        ├── hooks/useCADSession.ts     CAD state, upload, undo/redo, FreeCAD status polling
        ├── lib/api.ts                 Backend URL, authenticated fetch helpers (token attached)
        └── types/                     cad.ts, chat.ts: TypeScript mirrors of backend JSON
```

---

## 4. Startup and process lifecycle

### 4.1 Backend startup (`backend/main.py`)

1. `load_dotenv()` runs **before** any `app.*` import, because those modules read environment variables
   at import time (`LLM_MODEL`, `FREECAD_AUTOSTART`, budgets…).
2. The FastAPI app is created. Middleware order matters:
   - `_json_errors` (HTTP middleware) catches any unexpected exception, prints the traceback and returns
     `500 {"detail": "Server error: <Type>: <msg>"}`. It is registered **before** CORS so that it sits
     *inside* CORS, so even crash responses carry CORS headers and the browser shows the real message
     instead of "Failed to fetch".
   - `CORSMiddleware` allows `http://localhost:3000`, `http://127.0.0.1:3000` and `http://192.168.x.x:3000`.
3. `app.include_router(sessions.router)` and `app.include_router(router)` add the session and CAD/chat routes;
   `install_error_handlers(app)` maps domain errors to HTTP statuses (see [§13.4](#134-error-mapping)).
   Importing `app.auth` prints a warning if `CLERK_SECRET_KEY` is missing or `AUTH_DISABLED=1`.
4. In the `lifespan` handler (FastAPI's replacement for `on_event("startup")`), `init_db()` connects to MongoDB (5 s timeout + ping). On failure it prints a
   warning and the app keeps running with in-memory storage.
5. `uvicorn.run("main:app", reload=True, reload_excludes=["data/*", "freecad_bridge/*"])`: the auto-reloader
   restarts on `.py` changes but **not** when session data is written, which would otherwise kill requests
   mid-flight. It also does not watch `.env`, so restart manually after changing configuration.

### 4.2 FreeCAD startup (`backend/app/freecad_client.py`)

FreeCAD is started lazily: nothing happens until the first call that needs geometry.

```mermaid
sequenceDiagram
    participant B as Backend (fc.call)
    participant F as FreeCAD bridge :6666
    B->>F: ping (10 s timeout)
    alt bridge answers
        F-->>B: {pong, gui, version}
    else connection refused
        Note over B: FREECAD_AUTOSTART == "off" → raise FreeCADUnavailable (503)
        B->>B: acquire _start_lock, ping again (another thread may have launched it)
        B->>B: _launch(): subprocess.Popen([freecad.exe, freecad_listener.py])
        loop every 1.5 s, up to 90 s
            B->>F: ping
        end
        F-->>B: pong
    end
    B->>F: actual method (e.g. open_model)
```

- **Executable discovery** (`_find_exe`): an explicit env var (`FREECAD_GUI_EXE` / `FREECAD_CMD_EXE`) wins;
  otherwise it searches `%ProgramFiles%\FreeCAD*\bin\` and `%LOCALAPPDATA%\FreeCAD*\bin\` (newest folder
  first), then `PATH` (Linux/macOS).
- **Modes**:
  - `gui` (default): launches `freecad.exe backend/freecad_listener.py`. You see the FreeCAD window and every
    edit happens visibly in it.
  - `headless`: launches `freecadcmd backend/freecad_bridge/headless.py`. No window.
  - `off`: never launches; you must start FreeCAD and run the listener yourself.
- `status()` (used by `/freecad/status`) only pings. It never launches FreeCAD.

### 4.3 Frontend startup

`npm run dev` starts Next.js. `src/middleware.ts` runs Clerk's `clerkMiddleware`: any route other than
`/sign-in(.*)` and `/sign-up(.*)` calls `auth.protect()`, so unauthenticated users are redirected to sign-in.
(Next.js 16 warns that the `middleware` file convention is deprecated in favour of `proxy`; it still works.)

---

## 5. The FreeCAD bridge (RPC layer)

### 5.1 Wire protocol

Both directions use the same framing: a **4-byte big-endian unsigned length**, then that many bytes of
**UTF-8 JSON**.

```
request : {"id": 7, "method": "apply", "params": {"session_id": "...", "op": "resize_hole", ...}}
response: {"id": 7, "ok": true,  "result": {...}}
        | {"id": 7, "ok": false, "error": "H9 is a plane, expected hole", "traceback": "..."}
```

- One TCP connection per call (`freecad_client.call_sync`): connect with 5 s timeout, send, read exactly
  4 bytes, then exactly *length* bytes (`_recv_exact` loops until complete, so large payloads are never
  truncated), close.
- Default per-call read timeout is 300 s; a timeout becomes `FreeCADError("did not answer within …")`.
- `ok: false` is raised as `FreeCADError(message, traceback)`. Connection failures raise
  `FreeCADUnavailable`.
- The async wrapper `call()` runs `ensure_running` and `call_sync` in Starlette's thread pool so the event
  loop is never blocked by socket I/O.

### 5.2 Server inside FreeCAD (`freecad_bridge/server.py`)

FreeCAD's document API is **not thread-safe** and must be used from the Qt main thread in GUI mode.

**GUI mode** (`start_gui()`, called by `freecad_listener.py`):

```mermaid
sequenceDiagram
    participant Sock as Socket thread (per connection)
    participant Q as queue.Queue
    participant Main as Qt main thread (QTimer every 50 ms)
    participant Eng as engine.dispatch
    Sock->>Sock: read_msg(conn)
    Sock->>Q: put((request, Event, holder))
    Sock->>Sock: Event.wait(600 s)
    Main->>Q: get_nowait()
    Main->>Eng: handle(request)
    Eng-->>Main: response dict
    Main->>Sock: holder["resp"] = response; Event.set()
    Sock->>Sock: write_msg(conn, response)
```

- An accept loop runs on a daemon thread; each connection gets its own thread.
- A `QTimer` on the main thread drains the queue every 50 ms and runs requests **sequentially**, so all
  FreeCAD calls happen on the main thread, one at a time.
- If the main thread doesn't answer within 600 s the socket thread replies with an error.
- The timer object is stored in `_state` so Python doesn't garbage-collect it.
- PySide import falls back `PySide` → `PySide6` → `PySide2`.

**Headless mode** (`serve_forever()`, via `headless.py`): a plain sequential accept → read → handle →
write loop on the calling thread.

**Hot reload**: running `freecad_listener.py` again while the bridge is up calls
`importlib.reload(engine)`, so you can edit `engine.py` without restarting FreeCAD. The RPC method
`reload_engine` does the same remotely.

`handle()` dispatches to `engine.dispatch(method, params)`, which looks up `METHODS` and converts
`EngineError` to `{"ok": false, "error": msg}` (expected, user-facing) and any other exception to an error
with a traceback (unexpected; also printed to FreeCAD's report view).

### 5.3 RPC methods (`engine.METHODS`)

| Method | Params | Returns | Purpose |
|---|---|---|---|
| `ping` | — | `{pong, gui, version}` | Health check |
| `open_model` | `session_id, source_path, work_path, registry?` | `{info, registry}` | Import the uploaded file into a new document, save as `work.FCStd`, analyse |
| `reload` | `session_id, work_path, registry?` | `{info, registry}` | Close and re-open `work.FCStd` (after undo/redo) |
| `model_info` | `session_id, work_path, registry?` | `{info, registry}` | Re-analyse the current document |
| `apply` | `session_id, work_path, op, params, registry` | `{message, verified, volume_before, volume_after, info, registry}` | Run one edit operation transactionally |
| `find_features` | `session_id, work_path, registry, kind?, diameter_min?, diameter_max?, normal?, near?, object?, limit?` | `{features, total, registry}` | Filtered/sorted feature query |
| `measure` | `session_id, work_path, a, b, registry` | distances (see [§9.3](#93-query-methods)) | Measure between features, objects or points |
| `list_objects` | `session_id, work_path, registry, name_contains?` | `{objects, total, overall_bbox, registry}` | Every solid object, largest first, with parent assembly and bbox |
| `snapshot` | `session_id, work_path, fcstd_path, stl_path` | paths | Save, copy to `vN.FCStd`, mesh to `vN.stl` |
| `export` | `session_id, work_path, fmt, path` | `{path}` | Export STEP/IGES/BREP/STL/FCStd |
| `close_session` | `session_id, work_path?` | `{closed}` | Close the session's document |

---

## 6. The engine: documents and objects

`engine.py` runs inside FreeCAD's Python. It is **stateless apart from one map** `_DOCS: session_id → doc.Name`.
The feature-ID registry is owned by the backend and passed in on every call, so IDs survive a FreeCAD
restart.

### 6.1 Session documents

- `_find_doc(session_id, work_path)`: looks up `_DOCS`; if FreeCAD was restarted (map empty), it searches
  open documents whose `FileName` equals `work_path`.
- `_get_doc()`: if not open, re-opens `work.FCStd` from disk. Sets `UndoMode = 1` and activates the
  document. If neither the document nor the file exists, it raises *"Re-upload the model."*
- `_activate(doc, fit)`: in GUI mode, brings the document's MDI sub-window to the front and optionally sets
  an isometric view and fits it. GUI errors are caught and printed; they never break an edit.
- `_save(doc)`: saves and deletes the `*.FCBak` backup files FreeCAD writes next to the document.

### 6.2 Which objects count (`_solid_objects`)

An object is analysed and edited only if:

1. It is not a sketch (`Sketcher::*`), not a **container** (`App::Part`, `App::DocumentObjectGroup`,
   `App::Origin`, `App::Link`, `App::LinkGroup`), and has a `Shape`. In FreeCAD 1.0 an `App::Part` exposes the
   combined shape of its children; analysing it as well would report every feature twice.
2. It is not a child of a `PartDesign::Body` (the Body itself represents its features' final solid).
3. It is visible (and, if inside an `App::Part`, the part is visible too).
4. Its shape is non-null and contains at least one solid.

STEP assemblies imported with `ImportGui` usually produce one `Part::Feature` per part inside `App::Part`
containers; each part is analysed on its own and reports its container as `parent`. Containers can still be
targeted by name (e.g. `transform_object` on a whole sub-assembly). Mesh-only or surface-only files have no
solids and are rejected at upload.

### 6.3 Global coordinates (`_gshape`)

Objects inside an `App::Part` have placements relative to their parent. `_gshape(obj)` copies the shape and
pre-multiplies the parent's **global placement**, so every analysis, measurement and edit happens in one
global coordinate system (the one the user sees). When an edited shape is written back
(`_set_shape`), the inverse parent placement is applied so the object stays correctly placed in its
container.

### 6.4 Coordinate conventions

Millimetres and degrees. Axes: **X** left(−)/right(+), **Y** front(−)/back(+), **Z** bottom(−)/top(+).
The web viewer uses the same Z-up frame (see [§14.5](#145-3d-viewer-cadviewertsx)).

---

## 7. Feature recognition

`_features_of_shape(objname, shape)` turns a B-rep solid into a list of semantic features. It runs on
every analysis (upload, before and after each edit, queries).

### 7.1 Tolerances

```
diag = shape bounding-box diagonal
tol  = max(1e-3, diag × 1e-5)      # e.g. 1 m part → 0.01 mm
```

Most comparisons use `tol × 10`. Axis parallelism uses `|a·b| > 1 − 1e-7` (cylinders) or `1 − 1e-6`.

### 7.2 Face classification (pass 1)

Each face's underlying surface type decides what happens:

| Surface | Handling |
|---|---|
| `Part.Cylinder` | Collected into **cylinder groups** (see below) |
| `Part.Plane` | Collected as a plane candidate: normal at mid-parameter, centre of mass, area, offset `d = n·c`, bbox |
| `Part.Cone` | Collected as a cone candidate: canonical axis, axial range, max radius, semi-angle, apex |
| `Part.Sphere` | Immediately a **sphere** feature `S#` (radius, centre) |
| `Part.Toroid` | Immediately a **round** feature `R#` (minor radius = fillet radius; torus-shaped fillets) |
| anything else (B-spline etc.) | Ignored |

**Axis canonicalisation** (`_canon_axis`): an axis and its negation describe the same line, so the axis is
flipped until its first significant component is positive (z, then y, then x). Each axis line is
described by `base = point − axis·(point·axis)`, the point on the line closest to the origin.

**Cylinder grouping**: for each cylindrical face the engine computes:

- `concave`: whether the face normal points **towards** the axis (`n · radial < 0`). Concave = material
  outside the cylinder = **hole** wall. Convex = **boss**/shaft.
- the axial range `[t0, t1]` from the face's vertices projected on the axis,
- the angular span `|u1 − u0|`.

Faces with the same concavity, radius (±tol), parallel axis and same axis line are grouped (a full cylinder
is often split into two half-faces).

### 7.3 Holes, bosses and fillets (pass 2)

Inside each group, faces are sorted by `t0` and **clustered** along the axis when they touch or overlap.
For each cluster:

- **Partial cylinder** (total angular span < 98 % of 2π) → a **round** `R#` (a fillet or rounded edge), with
  radius, length, concavity, centre and axis.
- **Full concave cylinder** → a **hole** `H#`:
  1. **Caps** at each end (`_caps_at`): a perpendicular planar face whose centre is on the axis at that end
     and whose bbox is no wider than the hole (the flat bottom of a blind hole), or a *narrowing* cone on the
     same axis touching that end (a **drill tip**).
  2. **Open end**: no cap **and** a probe point just beyond the end (`eps = max(20·tol, 0.01)`) is *not*
     inside the solid.
  3. `through = open0 and open1`.
  4. **Entry**: the open end (the top end `t1` is preferred). `direction` points from the entry into the
     material.
  5. **Countersinks** (`_countersink_at`): a *widening* cone on the same axis at an open end. It records the
     outer diameter, included angle (2 × semi-angle) and position.
  6. Output: `diameter, radius, depth (t1−t0), through, drill_tip, entry, direction, center, position,
     countersinks`. Internal fields keep face indices, caps, open flags and countersink geometry for edits.
- **Full convex cylinder** → a **boss** `C#`: planar caps at its ends are marked used. The **free end** is
  the end with air beyond it (probe point outside the solid); the other is the **base end** where it joins
  the body.

Planes and cones consumed as caps or countersinks are marked used so they aren't reported twice.

### 7.4 Cones and planes (pass 3)

- Remaining cones become **cone** features `K#` (half-angle, apex, axis, centre, length). These are typically
  chamfers or tapers.
- Every plane becomes a **plane** feature `P#` with `normal, center, area, bbox` and a **label**
  (`_plane_label`):
  - Normal along ±Z/±X/±Y and at the object's bounding-box extreme → `"top face (+Z, outermost)"`,
    `"left face (-X, outermost)"`, etc.
  - Axis-aligned but not at the extreme → `"faces +Z at z=12.5"`.
  - Otherwise → `"inclined face"`.
  - Planes that are hole bottoms carry `is_cap: true`. The UI and LLM summary hide them.

### 7.5 Semantic positions

Holes and bosses get a human-readable `position` from their centre's location within the object's bounding
box: below 25 % of the span on an axis → `left(-X)`/`front(-Y)`/`bottom(-Z)`; above 75 % → the opposite;
otherwise nothing. Example: `"right(+X) back(+Y)"`, or `"central"`.

### 7.6 Parametric information (`_parametric_info`)

For `.FCStd` files the engine also reports:

- **Parametric objects** (`PartDesign::*` and `Part::*` except plain `Part::Feature`): numeric, boolean and
  enum properties (lengths, angles, counts…), skipping placement/attachment/base groups, plus any
  **expressions** (`ExpressionEngine`) that drive properties.
- **Sketches**: dimensional constraints (`Distance`, `DistanceX`, `DistanceY`, `Radius`, `Diameter`,
  `Angle`), with index, name, value (angles in degrees) and whether they are driving.
- **Spreadsheets**: used cells with alias and value (up to 200).

`is_parametric` is true when there are parametric objects or sketches.

### 7.7 Model info structure (`_model_info`)

```json
{
  "units": "mm",
  "document": "bracket",
  "objects": [{"name": "Part__Feature", "label": "Bracket", "type": "Part::Feature", "parent": "Assembly",
               "volume": 41250.0, "area": 9800.0, "solids": 1, "faces": 22, "edges": 60,
               "bbox": {"min": [...], "max": [...], "size": [...]}, "center_of_mass": [...]}],
  "bbox": {"min": [...], "max": [...], "size": [100, 60, 10]},
  "features": {"hole": [...], "boss": [...], "round": [...], "plane": [...], "cone": [...], "sphere": [...]},
  "counts": {"hole": 4, "plane": 10, "round": 4},
  "parametric": [...], "sketches": [...], "spreadsheets": [...],
  "is_parametric": false
}
```

`parent` (label of the containing `App::Part`) is present only for parts inside an assembly.
Each feature in `features` is the public form (internal `_` keys stripped) plus `object` (owner name).
Lists are sorted by ID number.

---

## 8. Stable feature IDs

The geometry changes after every edit and FreeCAD's face indices are renumbered, so IDs cannot be face
indices. Instead the backend keeps a **registry** of geometric descriptors and the engine re-matches
features against it on every analysis.

### 8.1 Registry format

```json
{
  "items": {
    "H1": {"k": "hole",  "a": [0,0,1], "b": [20,15,0], "t": [0,10], "r": 3.0},
    "P1": {"k": "plane", "n": [0,0,1], "d": 10.0, "c": [50,30,10]},
    "S1": {"k": "sphere", "c": [0,0,5], "r": 4.0}
  },
  "next": {"H": 5, "P": 11, "R": 5}
}
```

- Axial features (hole, boss, round, cone): axis `a`, axis-line base point `b`, axial range `t`, radius `r`.
- Planes: normal `n`, offset `d = n·c`, centre `c`.
- Spheres: centre `c`, radius `r`.
- `next` holds the next number per prefix. **Numbers are never reused**, so a removed `H2` stays gone and a
  new hole becomes `H5`, not `H2`.

The registry is stored in `meta.json` by the backend and sent with every RPC call.

### 8.2 Matching (`_match_score`, `_assign_ids`)

For every (new feature, registry entry) pair of the same kind, a score is computed (lower is better;
`None` = incompatible):

| Kind | Compatible when | Score |
|---|---|---|
| plane | same normal direction (`n·n' > 1−1e-6`) and same offset (±10·tol) | distance between centres |
| sphere | centres within 10·tol | centre distance |
| axial | parallel axes, same axis line (±10·tol), overlapping axial ranges | `|Δr| + 0.001·(|Δt0| + |Δt1|)` |

All compatible pairs are sorted by score and assigned **greedily** (each feature and each ID used at most
once). Unmatched features get fresh IDs in a deterministic order: by kind, then larger area first, then
higher Z, then X, then Y.

### 8.3 Expected-geometry patches

Some operations change exactly the properties used for matching, so before re-analysis the operation
writes a patch into the registry describing the *expected* new geometry:

| Operation | Patch | Effect |
|---|---|---|
| `resize_hole` | `r = new_radius` | The resized hole keeps its ID |
| `move_hole` | `b = new axis base` | The moved hole keeps its ID |
| `push_pull_face` / `set_face_distance` | `d` and `c` shifted by the distance | The moved face keeps its ID |

---

## 9. Edit operations

All operations live in `engine.OPS` and receive a context `_Ctx` with the document, the current feature map
and the registry. `ctx.feature(id, kinds)` resolves an ID (case-insensitive) and type-checks it; unknown IDs
produce *"Unknown feature id … Use get_model_info / find_features"* so the LLM can recover.

### 9.1 How B-rep edits are written back (`_set_shape`)

1. **Clean**: `removeSplitter()` merges coplanar faces left by booleans; a compound with one solid is
   unwrapped.
2. **Validate**: empty shape or no solids → error. Invalid shape → try `fix()`; still invalid → error
   ("rolled back").
3. **Target**: if the object is a plain `Part::Feature`, its `Shape` is replaced in place. If it is
   parametric (e.g. a PartDesign Body), a new `Part::Feature` named `AIEdit` (label `"<label> (edited)"`) is
   created with the result, the original is hidden, and its colour is copied. `apply()` detects this and adds
   a note to the operation message (*"…replaces a parametric object with a plain solid; that object's
   parameters no longer drive the shown geometry"*), so the LLM tells the user.
4. **Placement**: converts the global-coordinate result back into the parent container's local frame.
5. **Audit log**: appends a note to the object's `AiEdits` string-list property (group "AetherCAD"), so the
   FCStd records what AetherCAD did.

### 9.2 Operation reference

| Operation | Parameters | Algorithm |
|---|---|---|
| `resize_hole` | `hole`, `diameter` | **Fill** the old hole (`_fill_hole`), then **cut** a new cylinder of the new diameter on the same axis and axial range. Open ends are extended by a margin (`max(2 % of diag, 1 mm)`) so the cut fully clears. A blind hole with a drill tip gets a new tip cone with the same angle; countersinks are recreated with the same outer diameter and angle. |
| `move_hole` | `hole`, `offset` or `to` | Only the component **perpendicular** to the hole axis is used (a hole can't move along its own axis). Fill the old hole, cut an identical one (tip, countersinks) on the shifted axis. |
| `add_holes` | `face` (plane), `diameter`, `positions?`, `depth?` | Each position is projected onto the face plane and must lie inside the face. A cylinder starts a margin above the face and goes along −normal: through (depth = 2 × diag) or blind (given depth). No positions → face centre. Multiple holes are cut in one boolean. |
| `remove_features` | `features[]` | Groups by object, then calls OpenCASCADE **defeaturing** on all the feature's faces (walls, caps, countersink cones). If defeaturing changes nothing or yields an invalid shape: a single hole is plugged exactly; otherwise it errors with "try removing them one at a time". |
| `push_pull_face` | `face`, `distance` | Extrudes the planar face along its outward normal by `distance` into a prism; **fuse** if positive (adds material), **cut** if negative (removes). |
| `set_face_distance` | `face`, `reference_face`, `distance` | Faces must be parallel. Computes the current signed distance along `face`'s normal and push/pulls `face` by the difference, so the distance becomes exact. The reference stays fixed. Used for "make it 120 mm long". |
| `fillet_edges` | `radius`, `edges[]`, `object?` | Resolves edges via selectors (below), keeps only **sharp** edges, calls `makeFillet`. Verification expects a new round with that radius. |
| `chamfer_edges` | `size`, `edges[]`, `object?` | Same selection; `makeChamfer` with equal distance. |
| `add_primitive` | `shape`, `dimensions`, `position`, `direction?`, `operation?`, `object?` | Builds a box (centred on `position`, L×W×H along X/Y/Z, optionally rotated so its Z aligns with `direction`), cylinder (base centre at `position`, along `direction`), cone, sphere (centre) or torus. Then `fuse` / `cut` / `common` with the target object, `replace` to swap the target object's whole shape for the primitive (the object keeps its name, label, colour and assembly membership; used for "make the rectangular top round"), or `new` to create a separate object. |
| `transform_object` | `object?`, `translate?`, `rotate?{axis, angle, center?}` | Composes a new `Placement`: rotation about `center` (default: bbox centre), then translation. |
| `scale_object` | `factor` (number or `[sx,sy,sz]`), `object?`, `center?` | Uniform scale via `shape.scale`, or non-uniform via a matrix and `transformGeometry`, about `center` (default: bbox centre). |
| `set_property` | `object`, `property`, `value` | Refuses if the property is driven by an expression (*"change its source instead"*). Sets it and verifies the new value. |
| `set_sketch_constraint` | `sketch`, `constraint` (name or index), `value` | Only driving dimensional constraints. Sets with units (`mm` or `deg`) via `setDatum`; the solver and recompute update the model. |
| `set_spreadsheet_cell` | `sheet`, `cell` (address or alias), `value` | Resolves aliases to addresses and sets the cell. |
| `delete_object` | `object` | Refuses if other objects depend on it (`InList`). Membership in a container's `Group` (an `App::Part`, group or Body) is not counted as a dependency, so parts inside assemblies can be deleted. |
| `run_python` (tool: `run_freecad_python`) | `code`, `description` | **Disabled unless `AGENT_ALLOW_PYTHON=1`** (the tool is then not even offered to the LLM). Executes code with `doc`, `App`, `Part`, `V` in scope; stdout (last 2,000 chars) is returned. Still runs inside the transaction, so errors roll back. |

**Edge selectors** (`_select_edges`), combinable in one object and passed as a list:

| Selector | Meaning |
|---|---|
| `{face: "P3"}` | All edges of face P3 |
| `{between: ["P1","P5"]}` | The edge(s) shared by two faces |
| `{hole: "H2", end: "entry"\|"exit"\|"both"}` | Circular rim edges of a hole (entry = open side it's drilled from) |
| `{boss: "C1", end: "free"\|"base"\|"both"}` | Rim edges of a boss (free = tip, base = junction with body) |
| `{parallel_to: "Z", on_face?: "P2"}` | Straight edges parallel to an axis, optionally only those on a face |
| `{all: true}` | Every edge |

Afterwards edges are de-duplicated (`isSame`) and filtered with `_is_sharp`: seam edges and tangent edges
(normals of the two adjacent faces within 1°) can't be filleted and are dropped.

When `object` isn't given, the target object is inferred from the first selector's feature; if that's not
possible and the document has exactly one solid it is used, otherwise the engine asks for `object` and lists
candidates.

### 9.3 Query methods

- **`find_features`** filters by kind, diameter range, plane normal (`+Z`, `-X`…) and object; sorts by
  distance to `near` if given, else by ID; returns at most `limit` (default 50) plus the total count.
- **`measure(a, b)`**: each reference may be a feature ID (its faces), an object name/label (its whole
  shape) or an `[x,y,z]` point. Returns:
  - `min_distance` and up to 2 pairs of closest points (OpenCASCADE `distToShape`);
  - for two parallel axial features: `axis_to_axis_distance`;
  - for two parallel planes: `plane_to_plane_distance`;
  - for two features: `center_delta` and `center_distance`.

---

## 10. Transactions, validation and verification

`engine.apply()` wraps every edit:

```mermaid
flowchart TD
    A[analyse document with registry] --> B[record volumes and already-invalid objects]
    B --> C[doc.openTransaction]
    C --> D[run operation]
    D --> E[doc.recompute]
    E --> F{new invalid objects?<br/>or no solids left?}
    F -- yes --> X[abortTransaction + recompute<br/>raise EngineError]
    D -- exception --> X
    F -- no --> G[commitTransaction]
    G --> H[apply registry patches]
    H --> I[re-analyse → new IDs, info]
    I --> J[measure verify list<br/>+ expected new features]
    J --> K[save work.FCStd, activate]
    K --> L[return message, verified, volumes, info, registry]
```

- A `TypeError` from wrong arguments becomes *"Bad parameters for <op>: …"* so the LLM can correct the call.
- **Verification** (`_measure_field`) re-measures, after re-analysis, the values the operation promised:

| Operation | Verified |
|---|---|
| `resize_hole` | the hole's new `diameter` |
| `move_hole` | the hole's new `center` |
| `set_face_distance` | the actual plane-to-plane distance |
| `set_property` | the property value read back |
| `set_sketch_constraint` | the constraint value read back |
| `fillet_edges` | at least one *new* round with that radius exists |

  A verification entry has `status: "ok"` with the measured `value`, or `status: "missing"` with a note (for
  example the feature merged into another). The UI shows ✓ chips and ⚠ warnings accordingly, and the system
  prompt tells the LLM to call a value "verified" only when it appears here.
- `volume_before` / `volume_after` per object are returned for every edit, so the effect of any change
  (including custom scripts) is visible.

---

## 11. Backend services

### 11.1 Session storage on disk (`app/cad_service.py`)

```
backend/data/sessions/<session_id>/
├── original.<ext>         uploaded file, never modified
├── work.FCStd             live document that FreeCAD edits
├── versions/
│   ├── v0.FCStd  v0.stl   state right after upload
│   ├── v1.FCStd  v1.stl   after the first turn that changed geometry
│   └── …
├── exports/               files generated for download (<stem>_v<N>.<ext>)
└── meta.json              see §16.1
```

- Session IDs are validated with `^[A-Za-z0-9_-]{1,64}$` before touching the filesystem (prevents path
  traversal).
- `meta.json` is written atomically (`meta.json.tmp` + `os.replace`).
- **Per-session `asyncio.Lock`**: upload, chat turns, undo/redo, activate and export for a session are
  serialised, so two requests can never edit the same document at once. (The locks live in process memory;
  run a single backend process.)

**Key functions**

| Function | What it does |
|---|---|
| `upload(session_id, filename, bytes)` | Validates extension and non-empty content. If the session already had a model, closes it in FreeCAD and **deletes the folder** (uploading replaces the model). Writes `original.*`, calls `open_model`, then `snapshot` → v0, writes `meta.json`. FreeCAD import errors become HTTP 422. |
| `apply_op(meta, op, params)` | RPC `apply`; updates `meta.registry`, `meta.info` and sets `dirty = True` (not yet saved). |
| `query(meta, method, …)` | RPC for read-only methods; keeps the registry in sync. |
| `refresh_info(meta)` | RPC `model_info`. |
| `commit_version(meta, description)` | Deletes any redo branch (versions after `head`), snapshots `v(head+1)`, appends to `versions`, sets `head`, clears `dirty`, saves meta. |
| `restore(meta, v)` | Closes the document, copies `vN.FCStd` over `work.FCStd`, RPC `reload`, updates info/registry/head. |
| `discard_uncommitted(meta)` | If a turn failed midway with edits applied, restores `head`, so a failed turn leaves no partial edits. |
| `undo` / `redo` | `restore(head ∓ 1)` under the lock. |
| `activate(session_id)` | Re-analyses and brings the document to front (called when switching chats). |
| `mesh_path(session_id, v)` | Path to `vN.stl` (default `head`). |
| `export(session_id, fmt)` | RPC `export` into `exports/`. Formats: `step`/`stp`, `iges`/`igs`, `stl`, `fcstd`, `brep`. |
| `public_state(meta)` | The `CadState` JSON sent to the browser (§16.3). |

### 11.2 FreeCAD client (`app/freecad_client.py`)

Covered in [§4.2](#42-freecad-startup-backendappfreecad_clientpy) and [§5.1](#51-wire-protocol).
Exceptions: `FreeCADUnavailable` (→ 503) and `FreeCADError` (→ 500, or 422 during upload).

### 11.3 Database (`app/db.py`)

`init_db()` connects with Motor (async MongoDB driver), pings, and stores the database handle; `get_db()`
returns it or `None`. **Every caller handles `None`** by using in-memory dictionaries instead, so the app
works without MongoDB (data is lost on restart).

### 11.4 Sessions (`app/sessions.py`)

Sessions are chat threads owned by a Clerk user ID. The user always comes from the verified token
([§11.6](#116-authentication-appauthpy)), never from the request body or query.

- `GET /sessions` lists the user's sessions, newest first (max 200).
- `POST /sessions` creates one (`title` is the first message or `CAD: <filename>`; stored up to 120 chars).
- `PATCH /sessions/{id}` renames (owner only).
- `GET /sessions/{id}` and `GET /sessions/{id}/messages` (owner only).
- `require_owned(session_id, user)` returns the session or raises **404** (not 403, so the existence of other
  users' sessions isn't revealed). The CAD and chat routes use it too.

`_session_out` returns a **whitelist** of fields (`id, title, user_id, created_at, cad_filename`). Session
documents created by older versions of the app contain binary fields that are not JSON-serialisable;
whitelisting keeps them out of responses. `cad_filename` comes from the session's `meta.json`.

The session ID is the MongoDB `ObjectId` as a 24-char hex string (or a UUID hex in memory mode); the same ID
names the folder under `data/sessions/`.

### 11.5 Messages (`app/messages.py`)

`add(session_id, role, content, operations?, version?, kind?)` stores a message with a UTC timestamp.
`kind` is one of `upload`, `undo`, `redo`, `error`, or absent for normal chat.

`history_for_llm(session_id)` builds the conversation history sent to the LLM:

- the last `LLM_HISTORY_MESSAGES` (default 8) stored messages,
- skipping `error` messages (failed turns shouldn't steer the model),
- each content cut to `LLM_HISTORY_MESSAGE_CHARS` (default 1,500),
- assistant messages get a suffix listing the edits they applied and the model version
  (`[Edits applied: H2: Ø6 → Ø8 mm]`, `[Model version 3]`), so the LLM knows what already happened even if the
  text reply was short.

### 11.6 Authentication (`app/auth.py`)

```mermaid
sequenceDiagram
    participant FE as Frontend (Clerk)
    participant BE as Backend
    participant CK as Clerk API
    FE->>FE: getToken() → short-lived session JWT (RS256)
    FE->>BE: request + Authorization: Bearer <JWT>
    BE->>CK: GET /v1/jwks (first time, then cached 1 h; Bearer CLERK_SECRET_KEY)
    CK-->>BE: public signing keys
    BE->>BE: verify signature, exp (10 s leeway), require sub
    BE->>BE: user = sub → ownership check on the session
```

- `current_user` is a FastAPI dependency used by every `/sessions`, `/cad/*` and `/chat` endpoint.
  `/` and `/freecad/status` are public.
- Missing header → 401 *"Not signed in"*; expired → 401 *"Session expired"*; bad token → 401; Clerk
  unreachable → 503; `CLERK_SECRET_KEY` unset → 500 *"Server authentication is not configured"*.
- The JWKS request sends a `User-Agent` header (Clerk's edge rejects Python's default one with 403).
- **`AUTH_DISABLED=1`** turns verification off for local testing; the user is then `?user_id=` or `"local"`.
- Binary endpoints (`/cad/model`, `/cad/download`) are protected too, so the frontend fetches them with the
  header and turns the response into a blob ([§14.2](#142-backend-url-and-requests-libapits)).

---

## 12. The AI agent

### 12.1 One chat turn (`agent/runner.py → run_turn`)

```mermaid
flowchart TD
    S([POST /chat]) --> H[history_for_llm, then store user message]
    H --> K{API key set?}
    K -- no --> E1[LLMError 500]
    K -- yes --> M{session has a model?}
    M -- no --> N[single LLM call with NO_MODEL_PROMPT,<br/>no tools] --> OUT
    M -- yes --> L[acquire session lock, reload meta]
    L --> P[messages = system prompt incl. model summary<br/>+ history + user message]
    P --> T[tool_defs = tools_for meta]
    T --> LOOP{step ≤ AGENT_MAX_STEPS}
    LOOP --> C[_complete: fit budget, call LLM]
    C --> Q{tool calls?}
    Q -- no --> D[final text]
    Q -- yes --> X[append assistant msg;<br/>for each call: parse JSON args,<br/>tools.execute, append tool result]
    X --> LOOP
    LOOP -- exhausted --> D2["'I stopped after too many steps…'"]
    D --> V{meta.dirty?}
    D2 --> V
    V -- yes --> CV[commit_version → vN+1]
    V -- no --> SV[save meta]
    CV --> OUT([return content, operations, state])
    SV --> OUT
    C -- exception --> RB[discard_uncommitted → restore head] --> RE[re-raise]
```

Important properties:

- **One turn = at most one version.** Several edit tool calls in one turn are committed together as one
  version whose description joins their messages (`_describe`, ≤ 300 chars).
- **A failing turn leaves nothing half-applied**: any exception restores the head version.
- **Tool failures do not abort the turn.** An edit that fails in FreeCAD is rolled back by the engine and
  returned to the LLM as `{"ok": false, "error": …}`; the prompt tells it to fix the call and retry at most
  twice, or explain.
- If the LLM ends with no text but made edits, the reply is generated from the edit records.

### 12.2 System prompt (`agent/prompt.py`)

`build_system(meta)` = `SYSTEM_PROMPT` + `CURRENT MODEL (<filename>, version N):` + compact JSON summary.

The prompt's rules:

- **Coordinates & units**: mm/degrees, axis meanings, ID prefixes, plane labels. Face labels use *global*
  axes, and imported models are often **Y-up** (SolidWorks STEP), so for furniture/assemblies the LLM must
  decide what "top" means from part labels and sizes, not label words alone.
- **Assemblies**: each part is an object with a label and parent; find parts with `list_objects`; pass
  `object` to edit tools when several exist.
- **Answering**: use only model data and query tools; never invent dimensions; **never ask the user for a
  value that can be looked up or measured**; quote IDs and exact values.
- **Editing**: exact absolute numbers (the LLM converts "increase by 2 mm", "double", "10 % longer"
  itself); "make it 120 mm long" → `set_face_distance` between the opposite outermost faces; one call per
  feature for "all 6 mm holes"; replacing a whole part's shape → `add_primitive` with `operation: "replace"`,
  sized and positioned from the part's bbox; **ask** a short question listing candidates when a reference is ambiguous;
  refuse physically impossible edits; prefer parametric tools (`set_property`, `set_sketch_constraint`,
  `set_spreadsheet_cell`) for parametric models and edit expression sources rather than driven values;
  `run_freecad_python` only as a last resort; on `ok: false`, fix and retry at most twice.
- **Reply style**: 1–4 short lines after edits, quoting verified values; no code or JSON.

`NO_MODEL_PROMPT` is used when no model is loaded: answer general CAD questions and ask the user to attach a
file.

### 12.3 Model summary and its size budget

The raw model info can be huge (an assembly with 44 parts and hundreds of faces is ~23 k characters), and
LLM rate limits are counted in tokens, so `summarize_model(info, full)` builds a compact view that must fit
a character budget:

- `MODEL_SUMMARY_CHARS` (default 4,500 ≈ 1.4 k tokens) for the system prompt,
- `MODEL_SUMMARY_CHARS_FULL` (default 9,000) for the `get_model_info` tool.

What it contains:

| Field | Content |
|---|---|
| `objects` | **sorted by volume, largest first** (so truncation drops small hardware, not the main bodies): name, label (only if different from name), parent assembly, type, volume, bbox min/max, solid count (only if > 1) |
| `overall_bbox`, `feature_counts` | from the model info |
| `hole_groups` | holes grouped by (diameter, through, depth) with counts and IDs, so "the four 6 mm holes" is easy to resolve |
| `features` | per kind, only the fields the LLM needs (holes: diameter, depth, through, drill_tip, entry, direction, position, countersinks; planes: label, normal, centre, area; …). `object` only appears when there are several objects. Planes are sorted outermost-first, then by area; cap planes are dropped. |
| `parametric_objects`, `sketches`, `spreadsheets` | up to 60 / 30 / aliased cells |
| `truncated_counts`, `note` | how many items were cut per kind (including objects), and a hint to use `list_objects` / `find_features` |

**Budget loop**: start with per-kind limits (objects 40, holes 60, bosses 30, rounds 30, planes 40, cones 20,
spheres 20; ×3 for `full`), round all floats to 2 decimals, serialise; while the JSON exceeds the budget,
halve every limit (minimum 3) and try again (up to 6 rounds). Nothing is lost permanently: the LLM can always
query the rest with `find_features`.

### 12.4 Tools (`agent/tools.py`)

Tools are OpenAI-format function schemas. The descriptions are written for the LLM (when to use each tool,
units, conventions).

| Group | Tools |
|---|---|
| Query | `get_model_info`, `list_objects`, `find_features` (filters: kind, diameter range, normal, near, object), `measure` |
| B-rep edits | `resize_hole`, `move_hole`, `add_holes`, `remove_features`, `push_pull_face`, `set_face_distance`, `fillet_edges`, `chamfer_edges`, `add_primitive`, `transform_object`, `scale_object` |
| Parametric edits | `set_property`, `set_sketch_constraint`, `set_spreadsheet_cell`, `delete_object` |
| Escape hatch | `run_freecad_python` (mapped to the engine op `run_python`); only offered when `AGENT_ALLOW_PYTHON=1` |

`tools_for(meta)` omits `set_property`, `set_sketch_constraint` and `set_spreadsheet_cell` when the model has
no parametric data (STEP/IGES/BREP), and `run_freecad_python` unless enabled. That saves tokens and stops the
LLM from trying them; `execute` also refuses `run_freecad_python` when disabled, in case a model calls it anyway.

`execute(meta, name, args)` returns `(result_for_llm, record_for_ui)`:

- Query tools → result only (no UI record). `get_model_info` refreshes info and returns the *full* summary.
- Edit tools → `cad.apply_op(...)`; the result includes `message`, `verified`, `new_feature_ids`,
  `removed_feature_ids` (by comparing ID sets before/after), volumes and the new overall size. The **record**
  (shown as an operation card in the chat) holds the tool name, args, ok, message, verified values, volumes
  and new/removed IDs.
- `FreeCADError` / `CadError` → `{"ok": false, "error": …}` for the LLM plus a failed record for edits.
- `scale_object` accepts `factors` (per-axis) as an alias for `factor`.

### 12.5 Token budget and provider limits

Free LLM tiers limit **tokens per minute** (Groq free tier: 8,000 TPM for every chat model), and a turn makes
several calls that each resend the whole conversation. The runner keeps requests small:

| Mechanism | Where | Default |
|---|---|---|
| Compact model summary with a char budget | `prompt.py` | 4,500 chars |
| History limited and truncated | `messages.py` | 8 messages × 1,500 chars |
| Only relevant tools sent | `tools.tools_for` | parametric tools dropped for B-rep models |
| Each tool result cut | `runner.TOOL_RESULT_CHARS` | 4,000 chars |
| Request budget: before every call, older tool results (oldest first) are shortened to 300 chars until the estimated size (chars ÷ 3, conservative) fits | `runner._fit_budget` | `LLM_INPUT_TOKEN_BUDGET` = 5,500 |
| Low reasoning effort for gpt-oss models (fewer output tokens) | `runner.REASONING_EFFORT` | `low` |

Typical first request for the 42-part coffee-table assembly: system ≈ 1.9 k + tools ≈ 2.8 k + history ≈ 0.1 k
tokens.

### 12.6 LLM call and error handling (`_complete`)

Up to 3 attempts per call:

| Error | Handling |
|---|---|
| `429 RateLimitError` | Reads the wait time from the `Retry-After` header or the message (`"try again in 7.5s"`, `"1m2.5s"`, `"350ms"`). If ≤ `LLM_RATE_LIMIT_WAIT` (30 s), sleeps and retries (at most twice); otherwise `LLMError(429)` "rate limit reached, wait a minute". |
| `413` (request larger than the per-minute limit) | `LLMError(413)` explaining the free-tier limit and which settings to lower. Not retried (it would fail again). |
| `401 AuthenticationError` | `LLMError(500)` "API key missing or invalid". |
| `400 BadRequestError` | Often a malformed tool call (`tool_use_failed`); retried with an extra system message asking for valid JSON. |
| Other API errors | Retried; after 3 attempts `LLMError(502)`. |

`LLMError` carries an HTTP status that `routes.py` returns to the browser; the chat endpoint also stores it as
an `error` message so the transcript shows what happened.

### 12.7 Choosing a model

Any OpenAI-compatible endpoint with function calling works (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`).
Tool-calling quality matters most: the model must pick the right tool, resolve IDs from the summary and send
exact numbers. On Groq's free tier the available chat models (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`,
`qwen/qwen3.8-27b`) all share the 8 k TPM / 1 k requests-per-day limit; `openai/gpt-oss-120b` is the default
and the most reliable. Check current limits at <https://console.groq.com/settings/limits>, because providers
change them.

---

## 13. HTTP API reference

Base URL: `http://127.0.0.1:8000`. All bodies are JSON unless noted. Errors are `{"detail": "<message>"}`.
Every endpoint except `GET /` and `GET /freecad/status` requires `Authorization: Bearer <Clerk session token>`
([§11.6](#116-authentication-appauthpy)); session-specific endpoints return 404 unless the caller owns the session.

### 13.1 Sessions (`main.py`)

| Method & path | Body / query | Response |
|---|---|---|
| `GET /` | — | `{"message": "AetherCAD backend is running"}` |
| `GET /sessions` | — | `[Session]` of the signed-in user, newest first |
| `POST /sessions` | `{title}` | `Session` owned by the signed-in user |
| `PATCH /sessions/{id}` | `{title}` | `{id, title}` |
| `GET /sessions/{id}` | — | `Session` |
| `GET /sessions/{id}/messages` | — | `[Message]` oldest first |

`Session = {id, title, user_id, created_at, cad_filename}`

### 13.2 CAD (`routes.py`)

| Method & path | Body / query | Response |
|---|---|---|
| `GET /freecad/status` | — | `{connected: true, pong, gui, version}` or `{connected: false, error}` |
| `POST /cad/upload` | multipart: `file`, `session_id` | `CadState`; also stores an `upload` message with a summary |
| `GET /cad/state/{session_id}` | — | `CadState`, or 404 if no model |
| `POST /cad/activate/{session_id}` | — | `CadState` (re-analysed; document brought to front in FreeCAD) |
| `POST /cad/undo/{session_id}` | — | `CadState`; stores an `undo` message |
| `POST /cad/redo/{session_id}` | — | `CadState`; stores a `redo` message |
| `GET /cad/model/{session_id}` | `?v=` (default head) | STL file (`model/stl`, `Cache-Control: no-store`) |
| `GET /cad/download/{session_id}` | `?format=step\|iges\|stl\|fcstd\|brep` | File attachment `<stem>_v<N>.<ext>` |

### 13.3 Chat

`POST /chat` with `{session_id, message}` → `{message: Message, state: CadState | null}`.

Order of work: build history → store the user message → `run_turn` → store the assistant message (with
`operations` and `version`) → return it with the new state. On a known error the backend stores
`⚠️ <error>` as an `error` message and returns the error status; the frontend then reloads the transcript.

### 13.4 Error mapping

| Exception | HTTP status |
|---|---|
| `CadError` | its own status (400 default; 404 no model / missing version; 422 FreeCAD import failure) |
| `LLMError` | its own status (429, 413, 500, 502) |
| `FreeCADUnavailable` | 503 |
| Auth (`HTTPException` from `current_user`) | 401 / 500 / 503 (see §11.6) |
| `FreeCADError` | 500 (`"FreeCAD error: …"`) |
| anything else | 500 via `_json_errors` (`"Server error: …"`) |

---

## 14. Frontend

### 14.1 Authentication

`ClerkProvider` wraps the app (`layout.tsx`); `middleware.ts` protects every page except sign-in/sign-up.
`page.tsx` registers Clerk's `getToken` with `setAuthTokenGetter`, so every backend request carries a fresh
session token; the backend derives the user from it.

### 14.2 Backend URL and requests (`lib/api.ts`)

`getApiUrl()`: `NEXT_PUBLIC_API_URL` if set; else, in the browser, `http://127.0.0.1:8000` for localhost
(forces IPv4 to avoid IPv6 "connection refused"), or `http://<current hostname>:8000` when opened from
another device on the LAN.

| Helper | Purpose |
|---|---|
| `setAuthTokenGetter(fn)` | Registers the token source (Clerk `getToken`) |
| `authFetch(path, init)` | `fetch` with `Authorization: Bearer <token>`; network failure → *"Cannot reach the backend…"* |
| `apiFetch<T>(path, init)` | `authFetch` + JSON; non-2xx → `ApiError(detail, status)` |
| `postJson<T>(path, body)` | JSON POST via `apiFetch` |
| `apiBlob(path)` | `authFetch` for binaries; returns the blob and the filename from `Content-Disposition` (exposed by CORS) |

### 14.3 Main page (`app/page.tsx`)

State: current session ID, messages, prompt text, loading, error, panel visibility, drag state; CAD state
comes from `useCADSession()`, FreeCAD status from `useFreeCADStatus()`.

| Action | Flow |
|---|---|
| **Select a session** | Effect loads `GET /cad/state` (+ fire-and-forget `POST /cad/activate`) and `GET /sessions/{id}/messages`. |
| **New chat** | Clears all local state. A session is created lazily by `ensureSession()` on first upload or message (title = file name or message). The `createdHere` ref prevents the select-effect from refetching a session this page just created. |
| **Upload** (button or drag-and-drop) | `ensureSession` → `POST /cad/upload` → set CAD state → reload messages (to show the upload summary). |
| **Send** | Optimistically shows the user message → `POST /chat` → append the assistant message, update CAD state. On error: show the banner, restore the prompt, reload the transcript from the backend. |
| **Undo / Redo** | `POST /cad/undo|redo` → new state → reload messages. Buttons are disabled when `can_undo`/`can_redo` is false or a request is running. |
| **Click a feature ID** | `insertRef` appends it to the prompt. |
| **Download** | `download(format)` fetches `/cad/download/{id}?format=…` with the token via `apiBlob`, then saves it through a temporary object-URL link (a plain `<a href>` can't send the auth header). |

The header shows file name and version, undo/redo, and a FreeCAD badge (green "FreeCAD" with version and
GUI/headless on hover, or amber "FreeCAD offline").

### 14.4 Hooks (`hooks/useCADSession.ts`)

- `useCADSession()` mirrors backend CAD state: `cad`, `upload`, `undo`, `redo`, `loadSessionCad`,
  `modelPath` (the state's `mesh_url`, which contains `?v=<version>` so a new version forces a reload),
  `download(format)`.
- `useFreeCADStatus(10000)` polls `/freecad/status` every 10 s.

### 14.5 3D viewer (`CADViewer.tsx`)

- Fetches the STL with the auth header (`apiBlob`), parses it with three-stdlib's `STLLoader.parse`, computes
  vertex normals, renders a single
  `MeshStandardMaterial` mesh with an `Edges` overlay (edges whose angle exceeds 25°).
- **Z-up camera** (`up: [0,0,1]`), matching FreeCAD, so the part appears as it does in FreeCAD; a gizmo
  shows X (red), Y (green), Z (blue).
- `Bounds fit` frames the model. The `<Canvas key={fitKey}>` uses `session_id:filename`, so the camera
  re-frames for a new file but **keeps your view** when only the version changes.
- `OrbitControls` for rotate/pan/zoom.

### 14.6 Model panel (`ModelPanel.tsx`)

- Overall X/Y/Z size tiles, object count, total volume.
- **Features** tab: holes (Ø, through/depth, countersink, entry point, position), bosses, fillets, planar
  faces (largest first, caps hidden, up to 60), cones. Every ID is a clickable chip that inserts it into the
  prompt.
- **Parameters** tab: parametric objects and their properties (ƒ marks expression-driven ones), sketch
  constraints and spreadsheet aliases, all clickable. For B-rep models it explains there is no parametric
  data.
- **History** tab: versions newest first with time and description; current version highlighted.

### 14.7 Chat components

- `ChatMessage` renders assistant text as Markdown, a version badge (`v3`), a copy button, and an
  **operation card** per edit: green/red status, tool label, first line of the message or the error, ✓ chips
  for verified values (`✓ H2 diameter = 8.000`), ⚠ warnings for unverifiable values, and new feature IDs.
- `ChatInput`: textarea (Enter sends, Shift+Enter newline), attach button limited to
  `.step,.stp,.iges,.igs,.brep,.brp,.fcstd`, and suggestion chips while the conversation is new.
- `Sidebar`: lists sessions (`GET /sessions`), inline rename (`PATCH`), new chat. Switching chats only
  changes the selected ID; the page then calls `/cad/state` and `/cad/activate`.

---

## 15. End-to-end walkthroughs

### 15.1 Uploading a file

```mermaid
sequenceDiagram
    actor U as User
    participant FE as Frontend
    participant BE as Backend
    participant FC as FreeCAD engine
    U->>FE: drop bracket.step
    FE->>BE: POST /sessions {title:"CAD: bracket.step"} + Bearer token
    BE-->>FE: {id: "6ac95…"}
    FE->>BE: POST /cad/upload (file, session_id)
    BE->>BE: validate ext, write original.step (lock held)
    BE->>FC: open_model(source, work.FCStd)
    FC->>FC: newDocument, ImportGui.insert, recompute, check solids
    FC->>FC: saveAs work.FCStd, fit view, analyse → features + IDs
    FC-->>BE: {info, registry}
    BE->>FC: snapshot → versions/v0.FCStd + v0.stl
    BE->>BE: write meta.json (head 0), store "Loaded bracket.step…" message
    BE-->>FE: CadState
    FE->>BE: GET /cad/model/6ac95…?v=0
    BE-->>FE: v0.stl → viewer renders
```

### 15.2 An edit: "Make the 6 mm holes 8 mm"

1. `POST /chat`. History is collected and the user message stored.
2. The runner builds the prompt. The summary contains
   `hole_groups: [{diameter: 6, through: true, count: 4, ids: ["H1","H2","H3","H4"]}]`.
3. LLM call 1 returns four tool calls: `resize_hole({hole:"H1", diameter:8})` … `H4`.
4. For each, `tools.execute` → `cad.apply_op` → RPC `apply`:
   - the engine fills H1 (defeaturing), cuts an Ø8 cylinder on the same axis through the part, commits,
   - patches the registry (`H1.r = 4`) so the new hole keeps the ID `H1`,
   - re-analyses and verifies `H1.diameter = 8.0`,
   - saves `work.FCStd`; returns the message, verified list, volumes, new info and registry.
5. The four results go back to the LLM (call 2), which replies:
   *"Resized H1–H4 Ø6 → Ø8 mm (verified 8.000 each)."*
6. `meta.dirty` is true → `commit_version` → `v1.FCStd` + `v1.stl`, description
   `"H1: Ø6 → Ø8 mm; H2: …"`.
7. The assistant message (content + 4 operation records + `version: 1`) is stored and returned with the new
   `CadState`. The viewer loads `?v=1`; the panel shows the new diameters; FreeCAD shows the edited part.

On Groq's free tier, the 8 k TPM limit may be reached between call 1 and call 2; the runner waits for the time
Groq indicates and continues.

### 15.3 A failed edit

If, say, a fillet radius is too large, `makeFillet` throws → the engine aborts the transaction (document
unchanged) and returns *"Fillet R20 failed on 4 edge(s): … Try a smaller radius."* → the LLM receives
`ok: false`, may retry with a smaller radius or explain. If no edit in the turn succeeded, no version is
created. If the turn itself crashes (LLM error, FreeCAD down), `discard_uncommitted` restores the head version.

### 15.4 Undo / redo

`POST /cad/undo/{id}` → `restore(head-1)`: close the doc, copy `v(head-1).FCStd` to `work.FCStd`, reload,
re-analyse with the current registry (IDs stay consistent), save meta. The frontend gets a `CadState` whose
`mesh_url` points to the older STL. A new edit after undo **discards the redo branch** (versions after head are
deleted).

### 15.5 Switching chats

Selecting a session calls `GET /cad/state` and `POST /cad/activate`, which re-opens that session's
`work.FCStd` in FreeCAD if needed and brings it to the front, so FreeCAD always shows the chat you're in.

---

## 16. Data model reference

### 16.1 `meta.json`

```json
{
  "session_id": "6ac9512a50ef3c1f3a36763e",
  "filename": "COFFEE TABLE.step",
  "ext": "step",
  "created_at": "2026-10-10T08:12:44.120000+00:00",
  "head": 2,
  "versions": [
    {"v": 0, "description": "Uploaded COFFEE TABLE.step", "created_at": "..."},
    {"v": 1, "description": "P1 moved +5.0000 mm along its normal [0.0, 1.0, 0.0]", "created_at": "..."},
    {"v": 2, "description": "Filleted 4 edge(s) with R3 mm", "created_at": "..."}
  ],
  "registry": {"items": {"P1": {"k": "plane", "n": [0,-1,0], "d": -10.0, "c": [0,10,0]}}, "next": {"P": 41}},
  "info": { "...": "model info, §7.7" },
  "dirty": false
}
```

`head` is the version currently loaded; `versions[head+1:]` is the redo branch. `dirty` is true only while a
turn has applied edits that are not yet committed.

### 16.2 MongoDB collections (`MONGODB_DB_NAME`, default `freecad_ai`)

**`sessions`**

```json
{"_id": ObjectId("6ac9512a50ef3c1f3a36763e"), "title": "CAD: table.step", "user_id": "user_3Asb…",
 "created_at": ISODate("2026-10-10T08:12:40Z")}
```

**`messages`**

```json
{"_id": ObjectId("…"), "session_id": "6ac9512a50ef3c1f3a36763e", "role": "assistant",
 "content": "Resized H1–H4 Ø6 → Ø8 mm (verified 8.000 each).",
 "operations": [{"tool": "resize_hole", "args": {"hole": "H1", "diameter": 8}, "ok": true,
                 "message": "H1: Ø6.0 → Ø8.0 mm",
                 "verified": [{"id": "H1", "field": "diameter", "value": 8.0, "status": "ok"}],
                 "volume_before": {"Part__Feature": 41250.0}, "volume_after": {"Part__Feature": 40810.2},
                 "new_features": [], "removed_features": []}],
 "version": 1, "created_at": ISODate("…")}
```

`kind` is set for `upload`, `undo`, `redo` and `error` messages.

### 16.3 `CadState` (sent to the browser)

```ts
{
  session_id: string; filename: string;
  version: number;                 // head
  versions: VersionEntry[];        // up to and including head
  can_undo: boolean; can_redo: boolean; dirty: boolean;
  info: ModelInfo;                 // §7.7
  mesh_url: string;                // "/cad/model/<id>?v=<head>"
}
```

TypeScript mirrors are in `frontend/src/types/cad.ts` and `chat.ts`.

---

## 17. Configuration reference

### 17.1 `backend/.env`

| Variable | Default | Read in | Meaning |
|---|---|---|---|
| `CLERK_SECRET_KEY` | — | auth | Clerk secret (same as frontend) used to fetch the JWKS; required unless `AUTH_DISABLED=1` |
| `CLERK_JWKS_URL` | `https://api.clerk.com/v1/jwks` | auth | JWKS endpoint |
| `AUTH_DISABLED` | `0` | auth | `1` = no token verification (local testing only) |
| `AGENT_ALLOW_PYTHON` | `0` | tools | `1` = offer and allow `run_freecad_python` |
| `GROQ_API_KEY` | — | runner | LLM key (fallback) |
| `LLM_API_KEY` | — | runner | LLM key (wins over `GROQ_API_KEY`) |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | runner | OpenAI-compatible endpoint |
| `LLM_MODEL` | `openai/gpt-oss-120b` | runner | Model with tool calling |
| `LLM_REASONING_EFFORT` | `low` if model name contains `gpt-oss`, else empty | runner | Sent as `reasoning_effort`; empty disables |
| `AGENT_MAX_STEPS` | `12` | runner | Max LLM calls per turn |
| `TOOL_RESULT_CHARS` | `4000` | runner | Max chars of each tool result |
| `LLM_INPUT_TOKEN_BUDGET` | `5500` | runner | Estimated input tokens per request |
| `LLM_RATE_LIMIT_WAIT` | `30` | runner | Max seconds to wait on a 429 |
| `MODEL_SUMMARY_CHARS` | `4500` | prompt | Summary budget (system prompt) |
| `MODEL_SUMMARY_CHARS_FULL` | `9000` | prompt | Summary budget (`get_model_info`) |
| `LLM_HISTORY_MESSAGES` | `8` | messages | Past messages sent to the LLM |
| `LLM_HISTORY_MESSAGE_CHARS` | `1500` | messages | Truncation per past message |
| `MONGODB_URI` | — | db | Unset → in-memory |
| `MONGODB_DB_NAME` | `freecad_ai` | db | Database name |
| `FREECAD_AUTOSTART` | `gui` | freecad_client | `gui` / `headless` / `off` |
| `FREECAD_GUI_EXE` / `FREECAD_CMD_EXE` | auto | freecad_client | Executable paths |
| `FREECAD_HOST` / `FREECAD_PORT` | `127.0.0.1` / `6666` | freecad_client, server.py | Bridge address; the bridge reads `FREECAD_PORT` from FreeCAD's environment (the auto-launched FreeCAD inherits the backend's) |
| `AETHERCAD_DATA` | `backend/data` | cad_service | Data root (`sessions/` inside it) |
| `AETHERCAD_BACKEND` | `C:\VINAY_MAIN\Major_Project\backend` | freecad_listener | Backend folder when the listener is `exec`'d from the console (no `__file__`) |

### 17.2 `frontend/.env.local`

| Variable | Meaning |
|---|---|
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` | Clerk keys (the secret is also needed in `backend/.env`) |
| `NEXT_PUBLIC_CLERK_SIGN_IN_URL`, `NEXT_PUBLIC_CLERK_SIGN_UP_URL` | Sign-in/up routes |
| `NEXT_PUBLIC_API_URL` | Optional backend URL override |

`next.config.ts` lists `allowedDevOrigins` (LAN IP for dev access from other devices) and a 20 MB server-action
body limit.

---

## 18. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Not signed in` / `Invalid session token` (401) | Missing/invalid Clerk token | Sign in again; reload the page. |
| `Server authentication is not configured` (500) | `CLERK_SECRET_KEY` missing in `backend/.env` | Copy it from `frontend/.env.local`, restart. |
| `Could not reach Clerk to verify the session` (503) | No internet / Clerk down | Retry; for offline work set `AUTH_DISABLED=1`. |
| Session list empty after enabling auth | Sessions are stored per Clerk user id | Sessions created with another user id (or `local`) belong to that user. |
| AI picks the wrong face as "top" | Model is Y-up | Name the part ("the table top") or the face ID; the agent identifies parts by label and size. |
| Badge says **FreeCAD offline** | Nothing listening on 6666 | Normal before first use; it launches on the next CAD request. Or run `freecad_listener.py` in FreeCAD. |
| `FreeCAD is not running and its executable was not found` | Auto-discovery failed | Set `FREECAD_GUI_EXE` (or `FREECAD_CMD_EXE` for headless). |
| `FreeCAD was launched but the bridge did not come up in time` | FreeCAD took > 90 s or the macro failed | Check FreeCAD's report view; run the listener manually. |
| `cannot bind 127.0.0.1:6666` in FreeCAD | Another listener/freecadcmd is running | Close the other FreeCAD instance. |
| `The session's FreeCAD document is not open and no saved copy exists` | `data/sessions/<id>` was deleted | Re-upload the model. |
| `No solid geometry found in the file` | Mesh/surface-only file | Use a solid STEP/IGES/BREP/FCStd. |
| `Request too large … tokens per minute (TPM)` / `The request is larger than the LLM provider allows per minute` | Free-tier TPM limit | Lower `MODEL_SUMMARY_CHARS`, `LLM_HISTORY_MESSAGES`, `LLM_INPUT_TOKEN_BUDGET`; or use a provider/tier with higher limits. |
| Messages pause 10–30 s | Waiting out the per-minute limit (429 → retry) | Expected on free tiers. |
| `The LLM provider's rate limit was reached` | Wait required > 30 s, or daily limit hit | Wait, raise `LLM_RATE_LIMIT_WAIT`, or switch provider. |
| `LLM API key is missing or invalid` | Key unset/wrong | Fix `backend/.env`, restart. |
| `model … does not exist` / 404 from the LLM | Model not available to your account | Use a model listed by `GET https://api.groq.com/openai/v1/models`. |
| `.env` change has no effect | Reloader watches only `.py` | Restart `python main.py`. |
| `MongoDB unreachable` at startup | URI wrong or IP not allow-listed in Atlas | Fix, or run without MongoDB (in-memory). |
| `GET /sessions` 500 with `UnicodeDecodeError` | Old session docs with binary fields (before the whitelist fix) | Fixed in `_session_out`; update the backend. |
| "Cannot reach the backend" in the UI | Backend not running / wrong host | Start the backend; set `NEXT_PUBLIC_API_URL` if needed. |
| Viewer shows "Failed to load 3D model" | STL missing for that version | Check `data/sessions/<id>/versions/`; re-upload if missing. |

---

## 19. Known limitations and technical debt

### Resolved

| Former issue | Fix |
|---|---|
| No auth on `/cad/*` and `/chat`; anyone with a session ID could read or edit it | Clerk JWT verification on every endpoint + ownership checks (§11.6) |
| `PATCH /sessions/{id}` didn't check ownership | Owner-only via `require_owned` |
| `user_id` taken from the query string (spoofable) | User comes only from the verified token |
| Sidebar called non-existent `/sessions/{id}/sync` and `/sessions/new/sync` | Calls removed |
| Assembly containers (`App::Part`) analysed as solids, duplicating ~2/3 of all features | Containers skipped (§6.2) |
| Large parts dropped from the LLM summary of big assemblies | Objects sorted by volume; `list_objects` tool |
| LLM asked the user for dimensions it could measure | Prompt rule + `list_objects` |
| No way to turn a part into another shape ("make the top round") | `add_primitive` `operation: "replace"` |
| Parts inside assemblies couldn't be deleted (container counted as a dependency) | Group membership ignored |
| Silent loss of parametric control when a B-rep tool edits a parametric object | Operation message now says so explicitly |
| `run_freecad_python` always available | Off unless `AGENT_ALLOW_PYTHON=1` |
| Bridge port hard-coded on the FreeCAD side | `server.py` reads `FREECAD_PORT` |
| `/sessions` 500 (`UnicodeDecodeError`) on old session documents | Field whitelist in `_session_out` |
| Deprecated `on_event("startup")`, default "Create Next App" metadata, unused `@supabase/supabase-js`, committed `get-pip.py` | Lifespan handler, real metadata, dependency and file removed |

### Remaining (by design or future work)

- **Editing a parametric object with a B-rep tool** still produces a plain `AIEdit` solid (FreeCAD can't write
  arbitrary B-rep edits back into a feature tree). The prompt steers the LLM to parametric tools first and the
  user is told when it happens.
- **Recognition covers analytic surfaces only** (planes, cylinders, cones, spheres, tori). B-spline faces aren't
  classified; slots, pockets and patterns are not single features (a slot appears as planes + partial
  cylinders). Planned in phase 3 of [PLAN.md](PLAN.md).
- **Orientation**: face labels use global axes. Y-up models (common from SolidWorks) get "top" labels on the
  wrong faces; the agent compensates using part labels and sizes, but naming the part helps.
- **Single process / single FreeCAD**: per-session locks and memory-mode storage live in one backend process,
  and one FreeCAD instance serves all sessions sequentially. Run one backend worker; long edits queue others.
- **Viewer**: single-colour STL without face picking or highlighting (phase 2 of the plan).
- **Free-tier LLM limits**: 8 k tokens/minute on Groq free; multi-step turns pause while waiting (§12.5).

## 20. Extending AetherCAD

### Adding a new edit operation

1. **Engine** (`freecad_bridge/engine.py`): write `op_<name>(ctx, …)`. Resolve features with
   `ctx.feature(id, kinds)`, work on `_gshape(obj)`, write back with `_set_shape(ctx.doc, obj, result, note)`.
   Raise `EngineError` for user-facing failures. Optionally append to `ctx.verify`, set
   `ctx.registry_updates[id]` to keep IDs stable, or `ctx.expect_new`. Return a one-line message.
2. Register it in `OPS`.
3. **Tool schema** (`backend/app/agent/tools.py`): add a `_fn(...)` entry to `TOOLS` with a precise description
   (units, conventions, when to use) and add the name to `EDIT_TOOLS` (and to `OP_NAME` if the tool name
   differs from the op name).
4. **Prompt** (`agent/prompt.py`): add a rule if the LLM needs guidance on when to choose the new tool.
5. **UI label** (`frontend/src/components/chat/ChatMessage.tsx`): add it to `TOOL_LABELS`.
6. Hot-reload the engine (run `freecad_listener.py` again); the backend reloads itself.

### Adding a new recognised feature kind

Add detection in `_features_of_shape`, a prefix in `KIND_PREFIX`, a descriptor in `_descriptor` and a
score in `_match_score`, the LLM fields in `prompt._KEEP` and a limit in `prompt.LIMITS`, the TypeScript kind in
`types/cad.ts`, and a section in `ModelPanel.tsx`.

### Using another LLM provider

Set `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` to any OpenAI-compatible endpoint that supports tool calling.
If the model rejects `reasoning_effort`, set `LLM_REASONING_EFFORT=` (empty). With higher rate limits you can
raise `MODEL_SUMMARY_CHARS`, `LLM_HISTORY_MESSAGES` and `LLM_INPUT_TOKEN_BUDGET` for more context.

---

## 21. Glossary

| Term | Meaning |
|---|---|
| **B-rep** | Boundary representation: a solid described by its faces, edges and vertices (what STEP/IGES contain). |
| **Bridge** | The JSON-RPC server running inside FreeCAD (`freecad_bridge/server.py`). |
| **Engine** | The code inside FreeCAD that analyses and edits geometry (`freecad_bridge/engine.py`). |
| **Feature** | A recognised geometric element with an ID: hole `H`, boss `C`, round/fillet `R`, plane `P`, cone `K`, sphere `S`. |
| **Registry** | The map of feature IDs to geometric descriptors used to keep IDs stable across edits. |
| **Cap** | A planar face that closes the bottom of a blind hole (hidden from lists). |
| **Defeaturing** | OpenCASCADE operation that removes faces and heals the solid as if the feature had never been modelled. |
| **Head** | The version currently loaded in the working document. |
| **Turn** | One user message and everything the agent does in response (≤ 1 new version). |
| **TPM / RPD** | Tokens per minute / requests per day: LLM provider rate limits. |
| **Verified value** | A dimension re-measured from the geometry after an edit, not just the requested value. |
