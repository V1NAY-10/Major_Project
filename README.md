# AetherCAD: Conversational CAD Editing on FreeCAD

Upload an existing CAD model (STEP, IGES, BREP or FreeCAD `.FCStd`). It opens in FreeCAD, and a chat
interface lets you ask questions about it or request **precise** modifications in plain English.
Every change is applied in FreeCAD, measured, versioned, and shown in the web 3D viewer.

📘 **Full technical documentation:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) covers every module,
the feature-recognition algorithm, the agent loop, the data model, the HTTP API and troubleshooting.
The original rebuild plan is in [docs/PLAN.md](docs/PLAN.md).

---

## How it works

```
Next.js chat UI ──HTTP :8000──► FastAPI backend ──JSON-RPC/TCP :6666──► FreeCAD (GUI or headless)
 (3D viewer, click-to-select)   LLM agent + typed tools                  freecad_bridge/engine.py
                                session/version store
                                MongoDB (sessions + chat)
```

1. **FreeCAD is the geometry kernel and the source of truth.** Each chat session has its own FreeCAD
   document (`backend/data/sessions/<id>/work.FCStd`). With `FREECAD_AUTOSTART=gui` the backend starts FreeCAD
   when it starts, puts its window on the right half of the screen (`FREECAD_WINDOW`) and every edit appears
   there live, side by side with the web app.
2. **Precise parsing with stable IDs.** The engine recognises holes (`H#`: diameter, depth, through/blind,
   drill tip, **countersinks and counterbores**), bosses (`C#`), fillets (`R#`), planar faces (`P#`), cones (`K#`)
   and spheres (`S#`). Faces stored as B-splines (common in STEP files from some exporters) are recognised by
   least-squares fitting. On top of features it extracts:
   - **Hole patterns** (`PAT#`): bolt circles (PCD, count, start angle), rectangular grids and linear rows.
   - **Driving dimensions**, each tied to the IDs that define it: overall sizes and thicknesses (`P1-P2`),
     gaps/slot widths, and hole-to-edge distances (`H3-P5`).
   For `.FCStd` files it also reads parametric properties, sketch constraints and spreadsheet aliases.
   IDs are matched geometrically, so `H3` stays `H3` after other edits.
3. **Edits use the parsed data.** "Make it 120 mm long" → `set_dimension("P5-P6", 120)`; "bolt circle 80" →
   `edit_hole_pattern("PAT1", pcd=80)`. The LLM calls typed tools rather than writing code:
   `set_dimension`, `edit_hole_pattern`, `resize_hole`, `move_hole`, `add_holes`, `remove_features`,
   `push_pull_face`, `set_face_distance`, `fillet_edges`, `chamfer_edges`, `add_primitive`, `transform_object`,
   `scale_object`, `set_property`, `set_sketch_constraint`, `set_spreadsheet_cell`, `delete_object`,
   `set_hole_standard`, `lock_dimension`, `unlock_dimension`, `set_datums`, `edit_slot`, `edit_pocket`, `edit_chamfer`,
   `set_thread`, plus `get_model_info`, `list_objects`, `find_features`, `find_edges`, `list_dimensions`, `list_locks`,
   `measure_wall_thickness` and `measure` for questions.
   Features that open on a moved face move with it (a blind hole keeps its depth below the face).
   `run_freecad_python` is disabled unless `AGENT_ALLOW_PYTHON=1`.
4. **Fast path, no LLM.** Exact commands run instantly without an LLM call: `P5-P6 = 120`,
   `H3-P5 = 12 symmetric`, `H2 Ø8`, `PAT1 pcd 80`, `P5-P6 = 4.5 in`, `P3∠P7 = 60`, `C1 Ø30`, `H3 M8 clearance`,
   `tap H3 for M6`, `lock P1-P2`, `unlock all`, `locks`, `datum A = P3`, `SL1 width 10`, `PK1 depth 6`, `CH1 2`,
   `fillet E12 R2`, `H3 thread M8x1.25`, `wall thickness`, `PAT1 count_u 3`. Clicking a dimension in the panel fills in `P5-P6 = `.
5. **Verified edits.** Each operation runs in a FreeCAD transaction. Invalid results are rolled back, and
   successful ones are re-measured (e.g. `P5-P6 = 120.000 ✓`).
6. **Versions.** Each chat turn that changes geometry creates `vN`. Undo and redo restore snapshots in
   FreeCAD and in the viewer. Edits you make **by hand in the FreeCAD window** are detected and saved as a
   version ("Manual edit in FreeCAD") before the next chat turn. Export to STEP, IGES, STL or FCStd at any time.
7. **Select, inspect, edit.** Every face of every part has an ID: features (H#, P#, C#, R#, K#, S#, F# for
   free-form faces) and whole parts (B#). Click the model in the 3D viewer (Feature or Part mode), click an ID in
   the panel, or take the current selection from the FreeCAD window. The selection is highlighted in both views,
   an inspector shows all its data (sizes, position, related dimensions, pattern) with quick-edit buttons, and
   your next message applies to it: select H3 and type `Ø8`, or "move this 5 mm to the left".
   Features an answer mentions are highlighted automatically.
8. **Assemblies.** Each part of a STEP assembly is analysed separately in global coordinates.
9. **No login.** Single-user local app; every chat session is visible in the sidebar.
10. Ambiguous requests ("make the hole bigger") get a clarifying question listing the candidates.
11. **Precision (Phase 1).**
    - **Units:** type `4.5 in`, `1/4"`, `3 1/2 in`, `12 cm`, `30°`, `Ø8`, `R4`. A settings bar switches the display
      between mm and inch and sets the decimals. The model is always stored in mm.
    - **Tolerance:** every edit reports `requested / achieved / deviation`, shown as ✓ or ⚠. An edit that misses the
      session tolerance (default ±0.001 mm, ±0.01°) is rolled back inside FreeCAD.
    - **Side effects:** a table under each edit lists every other value that changed, with unexpected ones in amber.
    - **Locks:** lock a dimension or feature (lock icon, `lock P1-P2`, `lock H1 centred`). Edits that would change it
      are refused, or done with another anchor that keeps it.
    - **Datums:** A/B/C reference faces (defaults chosen automatically). Hole positions are shown from the datums;
      you can say "15 from datum B, 20 from datum C".
    - **Standard holes:** recognised holes are named (`Ø6.6 = M6 clearance (normal)`), and "make H3 an M8 clearance
      hole", "tap H3 for M6" or "H3 M6 counterbore" use the ISO 273 / tap drill / ISO 4762 / ISO 10642 tables.
    - **More dimensions:** angles `P3∠P7`, boss diameters `C1.Ø`, fillet radii `R2.R`, hole-to-hole `H1-H2` and
      coaxiality `H1◎H2`, all settable with `set_dimension`.
12. **Feature recognition (Phase 2).**
    - **Slots** `SL#` (obround / rectangular, through / blind) and **pockets** `PK#` (blind, planar floor, corner radius)
      are single features: change width, length, depth, corner radius, move or remove them.
    - **Chamfers** `CH#` with size and angle: resize, turn into a fillet, remove.
    - **Edges** `E#`: switch the viewer to *Edge*, click an edge and type `R2` or `C1`, or `fillet E12 R2`.
    - **Threads:** threaded hole features of FCStd files are shown on their holes; add a note with `H3 thread M8x1.25`.
    - **Wall thickness:** `wall thickness`, or the button on a selected part, finds the thinnest wall.
    - Tangent fillet runs are one `R#` chain; ring (toroidal) fillets are marked. Moving a face now carries its edge
      fillets along, pushing a face past a boss no longer leaves a pocket, and grids can change their hole count.

## Technology stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router, Turbopack), React 19, React Three Fiber + drei (STL viewer), TailwindCSS 4, react-markdown |
| Backend | FastAPI + Uvicorn, OpenAI Python SDK (any OpenAI-compatible endpoint; Groq by default), Motor (async MongoDB) |
| Geometry | FreeCAD 1.0+ (OpenCASCADE) running the bridge in `backend/freecad_bridge/` |
| Storage | Files on disk for models/versions, MongoDB (optional) for sessions and chat messages |

## Repository layout

```
backend/
  main.py                  FastAPI app, middleware, CORS, startup
  freecad_listener.py      Run inside the FreeCAD GUI to start the bridge
  app/
    routes.py              /cad/*, /chat, /freecad/status endpoints
    cad_service.py         Session folders, versions, undo/redo, export
    freecad_client.py      TCP client for the bridge + FreeCAD auto-launch
    messages.py            Chat message storage and LLM history
    db.py                  MongoDB connection (falls back to memory)
    units.py               Unit parsing / formatting (mm, cm, in, fractions, degrees, Ø, R)
    standards.py           ISO clearance, tap drill, counterbore, countersink and drill tables
    precision.py           Side-effect report, verification display, session settings
    sessions.py            /sessions endpoints + ownership checks
    agent/
      runner.py            LLM ⇄ tools loop, token budget, rate-limit handling
      prompt.py            System prompt + compact model summary
      tools.py             Tool schemas and execution
      fastpath.py          Exact commands (P1-P2 = 25, H3 Ø8, PAT1 pcd 80) without the LLM
  freecad_bridge/
    server.py              JSON-RPC server inside FreeCAD (GUI or headless)
    engine.py              Feature recognition, IDs, operations, verification, export
    analysis.py            B-spline surface fitting, counterbores, hole patterns, dimensions (incl. angles, radii)
    headless.py            Entry point for freecadcmd
  tests/                   Engine test suite (run with freecadcmd) + fixture parts
  data/sessions/<id>/      Per-session files (created at runtime)
frontend/
  src/app/page.tsx         Main chat + 3D workspace page
  src/components/          Chat, CAD viewer, model panel, sidebar
  src/hooks/useCADSession.ts
  src/lib/api.ts
docs/
  ARCHITECTURE.md          Deep technical documentation
  PLAN.md                  Original rebuild plan and roadmap
```

## Setup

### Prerequisites
- Python 3.10+, Node.js 18+, FreeCAD 1.0+ (tested with 1.1.4)
- An LLM API key for any OpenAI-compatible endpoint with tool calling (a free Groq key works)
- MongoDB (optional; without it, sessions and chat history live in memory and are lost on restart)

### Backend
```bash
cd backend
python -m venv venv
venv\Scripts\activate            # source venv/bin/activate on Linux/macOS
pip install -r requirements.txt
copy .env.example .env           # then fill in the keys
python main.py                   # http://127.0.0.1:8000
```

`backend/.env`:

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` / `LLM_API_KEY` | none | LLM key (`LLM_API_KEY` wins) |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Must support tool calling |
| `LLM_REASONING_EFFORT` | `low` for gpt-oss models | `low` / `medium` / `high`; empty to disable |
| `MONGODB_URI`, `MONGODB_DB_NAME` | none, `freecad_ai` | Optional persistence for sessions and messages |
| `FREECAD_AUTOSTART` | `gui` | `gui`: launch FreeCAD with the listener if it isn't running. `headless`: use `freecadcmd`. `off`: never launch |
| `FREECAD_GUI_EXE`, `FREECAD_CMD_EXE` | auto-detected | Paths to `freecad.exe` / `freecadcmd.exe` |
| `FREECAD_HOST`, `FREECAD_PORT` | `127.0.0.1`, `6666` | Bridge address (the bridge reads `FREECAD_PORT` too) |
| `FREECAD_PRELAUNCH` | `1` | Start FreeCAD when the backend starts (so the first upload doesn't wait) |
| `FREECAD_WINDOW` | `right` | Where the FreeCAD window goes: `right` (browser on the left half), `left`, `max`, `none` |
| `AETHERCAD_DATA` | `backend/data` | Where session files and versions are stored |
| `AGENT_ALLOW_PYTHON` | `0` | `1` lets the agent run arbitrary Python inside FreeCAD (`run_freecad_python`) |

**Token-budget settings** (useful on small free tiers; see [Free-tier LLM limits](#free-tier-llm-limits)):

| Variable | Default | Purpose |
|---|---|---|
| `MODEL_SUMMARY_CHARS` | `4500` | Max size of the model summary in the system prompt |
| `MODEL_SUMMARY_CHARS_FULL` | `9000` | Max size returned by the `get_model_info` tool |
| `LLM_HISTORY_MESSAGES` | `8` | Past chat messages sent with each request |
| `LLM_HISTORY_MESSAGE_CHARS` | `1500` | Each past message is cut to this length |
| `TOOL_RESULT_CHARS` | `4000` | Each tool result is cut to this length |
| `LLM_INPUT_TOKEN_BUDGET` | `5500` | Older tool results are shortened to keep each request under this |
| `LLM_RATE_LIMIT_WAIT` | `30` | Max seconds to wait and retry after a rate-limit (429) error |
| `AGENT_MAX_STEPS` | `12` | Max LLM calls per chat message |

### FreeCAD
**You don't need to do anything by default.** With `FREECAD_AUTOSTART=gui`, the backend launches
FreeCAD with the listener the first time it needs it (you will see `[FreeCAD] launching …` in the
backend log). Leave that window open; if you close it, it is relaunched on the next CAD request.

To start it by hand instead (for example with `FREECAD_AUTOSTART=off`), open FreeCAD and run the listener once:
**Macro ▸ Macros… ▸ `backend/freecad_listener.py` ▸ Execute**, or in the Python console:
```python
exec(open(r"D:/Soham_Jadhav/Major_Project/backend/freecad_listener.py").read())
```
Running the listener again hot-reloads `freecad_bridge/engine.py`.

### Frontend
```bash
cd frontend
npm install
npm run dev                      # http://localhost:3000
```

`frontend/.env.local` (optional):

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_API_URL` | Optional. Backend URL; defaults to `http://<current host>:8000` |

## Using it

1. Attach or drop a CAD file in the chat. It opens in FreeCAD and appears in the 3D workspace.
2. Ask questions: *"How thick is the plate?"*, *"List the holes and their positions"*, *"Distance between H2 and H4?"*
3. Request changes: *"Make the four 6 mm holes 8 mm"*, *"Make it 120 mm long"*,
   *"Round the vertical corners R5"*, *"Add a Ø5 through hole at 70, 30 on the top face"*,
   *"Make the table top round, diameter = its depth"*, *"Make H3 an M8 clearance hole"*,
   *"Make it 4.5 in long but keep H1 centred"*, *"Move H2 to 15 from datum B and 20 from datum C"*,
   *"Make the slot 10 wide"*, *"Round the pocket corners R4"*, *"Is any wall thinner than 3 mm?"*.
   For `.FCStd` files: *"Set the pad length to 25"*.
4. Click the part in the 3D viewer (or an ID in the panel) to select it: it is highlighted here and in FreeCAD,
   and the inspector shows its ID and data. Then just describe the change ("Ø8", "make it 3 mm deeper",
   "move this 5 mm left"). Shift+click adds to the selection; "Part" mode selects whole parts; "From FreeCAD"
   uses what you selected in the FreeCAD window. Use undo/redo in the header, and download from the workspace
   (STEP, FCStd, STL, IGES).
5. **Preview and plans** (Phase 3): *"preview P5-P6 = 130"* shows the result as a ghost with Apply / Cancel;
   compound requests (*"make it 150 long, add two Ø5 holes and round the corners R5"*) and large edits (shell, split,
   mirror part…) become a plan you approve, applied as one version and rolled back entirely if a step fails. Edits are
   checked first (overlapping holes, break-out, interference refused; thin walls warned). New edits: *"mirror H3 about
   y = 20"*, *"pattern H1 3 times 15 mm along X"*, *"add a 6 × 20 slot 4 deep"*, *"add a 20 × 12 pocket R2"*,
   *"counterbore H2 Ø11 × 5"*, *"shell it to 2 mm, open at the top"*, *"draft the side walls 5°"*, *"split at z = 5"*.
   The **History** tab is a version tree: go to, name and compare versions (changed dimensions; added / removed
   material in green / red). Ambiguous requests get clickable choices that highlight in 3D.
6. **Analysis** (Phase 4, Analysis tab or chat): *"material all steel"*, *"mass"*, *"dfm cnc"* / *"dfm print"* /
   *"dfm sheet"* (findings with one-click fixes), *"bom"* (CSV / XLSX download), *"interference"*, *"drawing"*
   (A3 PDF / SVG with views, dimensions and hole callouts), *"cost cnc 10"*. The **Assembly** button shows parts
   separately with visibility toggles and an explode slider.

## Tests

```bash
# Engine: parsing + every edit + analysis, against parts with known ground truth (headless FreeCAD, ~30 s, 383 checks)
"C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe" backend/tests/test_engine.py
# Backend unit tests: fast path, units, standards, side effects, tool units, threads, materials, BOM, plans (56 tests)
cd backend && venv\Scripts\python -m unittest discover -s app -t .
# Frontend
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```

## Free-tier LLM limits

On Groq's free tier, every chat model your account can use (`openai/gpt-oss-120b`, `openai/gpt-oss-20b`,
`qwen/qwen3.8-27b`) allows **8,000 tokens per minute** and 1,000 requests per day. `openai/gpt-oss-120b` is the
best of these at tool calling and is the default.

The backend keeps each request at roughly 4–6k tokens (compact model summary, trimmed history and tool
results, `reasoning_effort=low`). A single chat message can make several LLM calls, so on the free tier a
message that edits the model may **pause for 10–30 seconds** while the backend waits out the per-minute limit
and retries automatically. That is expected. If you see *"The request is larger than the LLM provider allows
per minute"*, lower `MODEL_SUMMARY_CHARS` or `LLM_HISTORY_MESSAGES`, or use a provider with higher limits.
Check your limits at <https://console.groq.com/settings/limits>.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FreeCAD offline` badge | The bridge on port 6666 isn't reachable. It is launched on the next CAD request, or run the listener by hand. |
| `Request too large … tokens per minute` | LLM free-tier limit. See [Free-tier LLM limits](#free-tier-llm-limits). |
| `LLM API key is missing or invalid` | Set `GROQ_API_KEY` or `LLM_API_KEY` in `backend/.env` and restart the backend. |
| Changes to `backend/.env` have no effect | The auto-reloader only watches `.py` files; restart `python main.py`. |
| `MongoDB unreachable` warning | Sessions fall back to memory. Check `MONGODB_URI` and your Atlas IP allow-list. |
| `No solid geometry found in the file` | The file has only meshes or surfaces; only solids can be edited. |

More detail in [docs/ARCHITECTURE.md § Troubleshooting](docs/ARCHITECTURE.md#18-troubleshooting).
