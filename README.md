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
 (Clerk auth, 3D viewer)        LLM agent + typed tools                  freecad_bridge/engine.py
                                session/version store
                                MongoDB (sessions + chat)
```

1. **FreeCAD is the geometry kernel and the source of truth.** Each chat session has its own FreeCAD
   document (`backend/data/sessions/<id>/work.FCStd`).
2. **Feature recognition with stable IDs.** The engine finds holes (`H#`, with diameter, depth, through or blind,
   and countersinks), bosses (`C#`), fillets (`R#`), planar faces (`P#`, labelled top, bottom, left and so on),
   cones (`K#`) and spheres (`S#`). For `.FCStd` files it also reads parametric properties, sketch constraints
   and spreadsheet aliases. IDs are matched geometrically, so `H3` stays `H3` after other edits.
3. **The LLM calls typed tools rather than writing code:** `resize_hole`, `move_hole`, `add_holes`,
   `remove_features`, `push_pull_face`, `set_face_distance`, `fillet_edges`, `chamfer_edges`,
   `add_primitive`, `transform_object`, `scale_object`, `set_property`, `set_sketch_constraint`,
   `set_spreadsheet_cell`, `delete_object`, plus `get_model_info`, `list_objects`, `find_features` and
   `measure` for questions. `add_primitive` can also **replace** a whole part's shape (e.g. turn a rectangular
   table top into a round one). `run_freecad_python` is disabled unless `AGENT_ALLOW_PYTHON=1`.
7. **Assemblies.** Each part of a STEP assembly is analysed separately (container groups are skipped, so
   nothing is counted twice), and the agent can find parts by name or size.
8. **Authenticated API.** Every backend request carries the user's Clerk session token; the backend verifies
   it and only lets users see and edit their own sessions.
4. **Verified edits.** Each operation runs in a FreeCAD transaction. Invalid results are rolled back, and
   successful ones are re-measured (e.g. `H2 Ø6 → Ø8, verified 8.000`).
5. **Versions.** Each chat turn that changes geometry creates `vN`. Undo and redo restore snapshots in
   FreeCAD and in the viewer. Export to STEP, IGES, STL or FCStd at any time.
6. Ambiguous requests ("make the hole bigger") get a clarifying question listing the candidates.

## Technology stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router, Turbopack), React 19, React Three Fiber + drei (STL viewer), TailwindCSS 4, Clerk auth, react-markdown |
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
    auth.py                Clerk session-token verification
    sessions.py            /sessions endpoints + ownership checks
    agent/
      runner.py            LLM ⇄ tools loop, token budget, rate-limit handling
      prompt.py            System prompt + compact model summary
      tools.py             Tool schemas and execution
  freecad_bridge/
    server.py              JSON-RPC server inside FreeCAD (GUI or headless)
    engine.py              Feature recognition, operations, verification, export
    headless.py            Entry point for freecadcmd
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
- Python 3.10+, Node.js 18+, FreeCAD 1.0+
- An LLM API key for any OpenAI-compatible endpoint with tool calling (a free Groq key works)
- A Clerk account (free) for frontend sign-in
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
| `CLERK_SECRET_KEY` | none | **Required.** Same value as in `frontend/.env.local`; used to verify session tokens |
| `AUTH_DISABLED` | `0` | `1` skips token verification (local testing only; user comes from `?user_id=`) |
| `GROQ_API_KEY` / `LLM_API_KEY` | none | LLM key (`LLM_API_KEY` wins) |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Must support tool calling |
| `LLM_REASONING_EFFORT` | `low` for gpt-oss models | `low` / `medium` / `high`; empty to disable |
| `MONGODB_URI`, `MONGODB_DB_NAME` | none, `freecad_ai` | Optional persistence for sessions and messages |
| `FREECAD_AUTOSTART` | `gui` | `gui`: launch FreeCAD with the listener if it isn't running. `headless`: use `freecadcmd`. `off`: never launch |
| `FREECAD_GUI_EXE`, `FREECAD_CMD_EXE` | auto-detected | Paths to `freecad.exe` / `freecadcmd.exe` |
| `FREECAD_HOST`, `FREECAD_PORT` | `127.0.0.1`, `6666` | Bridge address (the bridge reads `FREECAD_PORT` too) |
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
exec(open(r"C:/VINAY_MAIN/Major_Project/backend/freecad_listener.py").read())
```
Running the listener again hot-reloads `freecad_bridge/engine.py`.

### Frontend
```bash
cd frontend
npm install
npm run dev                      # http://localhost:3000
```

`frontend/.env.local`:

| Variable | Purpose |
|---|---|
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` | Clerk auth keys (copy `CLERK_SECRET_KEY` to `backend/.env` too) |
| `NEXT_PUBLIC_CLERK_SIGN_IN_URL`, `NEXT_PUBLIC_CLERK_SIGN_UP_URL` | Usually `/sign-in` and `/sign-up` |
| `NEXT_PUBLIC_API_URL` | Optional. Backend URL; defaults to `http://<current host>:8000` |

## Using it

1. Sign in, then attach or drop a CAD file in the chat. It opens in FreeCAD and appears in the 3D workspace.
2. Ask questions: *"How thick is the plate?"*, *"List the holes and their positions"*, *"Distance between H2 and H4?"*
3. Request changes: *"Make the four 6 mm holes 8 mm"*, *"Make it 120 mm long"*,
   *"Round the vertical corners R5"*, *"Add a Ø5 through hole at 70, 30 on the top face"*,
   *"Make the table top round, diameter = its depth"*.
   For `.FCStd` files: *"Set the pad length to 25"*.
4. Click a feature ID in the panel to insert it into your message. Use undo/redo in the header, and download
   from the workspace (STEP, FCStd, STL, IGES).

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
| `Not signed in` / `Invalid session token` (401) | Sign in again; the frontend sends the Clerk token automatically. |
| `Server authentication is not configured` | Set `CLERK_SECRET_KEY` in `backend/.env` and restart. |
| `FreeCAD offline` badge | The bridge on port 6666 isn't reachable. It is launched on the next CAD request, or run the listener by hand. |
| `Request too large … tokens per minute` | LLM free-tier limit. See [Free-tier LLM limits](#free-tier-llm-limits). |
| `LLM API key is missing or invalid` | Set `GROQ_API_KEY` or `LLM_API_KEY` in `backend/.env` and restart the backend. |
| Changes to `backend/.env` have no effect | The auto-reloader only watches `.py` files; restart `python main.py`. |
| `MongoDB unreachable` warning | Sessions fall back to memory. Check `MONGODB_URI` and your Atlas IP allow-list. |
| `No solid geometry found in the file` | The file has only meshes or surfaces; only solids can be edited. |

More detail in [docs/ARCHITECTURE.md § Troubleshooting](docs/ARCHITECTURE.md#18-troubleshooting).
