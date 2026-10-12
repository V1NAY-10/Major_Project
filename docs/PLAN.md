# AetherCAD — Rebuild Plan

Goal: upload an existing CAD file (STEP / IGES / BREP / FCStd), it opens in FreeCAD,
then a chat drives **precise** edits and answers questions. Every edit shows up both
in FreeCAD and in the web viewer, with undo.

## 1. What was wrong with the previous pipeline

| Problem | Effect |
|---|---|
| Two LLM calls per message: one writes free-form FreeCAD code, another emits "intents". "Apply" ran **both**. | Edits were applied twice or contradicted each other. |
| Edits were hard-coded hacks: "resize hole" fused a box plug over the area, "width" scaled the whole body. | Geometry was destroyed rather than edited. |
| Three sources of truth (raw STEP in memory, FreeCAD's active doc, MongoDB). The FreeCAD document wasn't tied to the session. | Edits went to the wrong document, or the viewer showed stale meshes. |
| Socket protocol: one `recv(65536)`, reply just `OK`/`ERR`, generated scripts wrapped in `try/except: print`. | Large scripts were truncated, failures were reported as success, and no data came back. |
| The STL came back by polling temp files; if FreeCAD was down, the old mesh was returned as "success". | Silent failures. |
| Parser turned every B-rep face into a "component", used bounding-box overlap as adjacency, and dumped the whole JSON into the prompt. | Token overflow on complex models; the LLM had no reliable references. |
| No verification after an edit. | "Precise" couldn't be checked. |

## 2. New architecture

```
Next.js chat UI ──HTTP──► FastAPI backend ──JSON-RPC/TCP──► FreeCAD (GUI or headless)
  - upload, chat,           - session store (disk +        - freecad_bridge/engine.py
    viewer, undo              Mongo for chat history)        • opens model per session
  - feature panel           - LLM agent with typed tools     • analyses features (stable IDs)
                            - version snapshots               • executes typed operations
                                                              • verifies, saves, meshes STL
```

Principles:
1. **FreeCAD is the only geometry kernel and the source of truth.** One FreeCAD document per
   chat session, saved to `backend/data/sessions/<id>/work.FCStd`.
2. **The LLM never writes geometry code by default.** It calls *typed tools*
   (`resize_hole`, `push_pull_face`, `set_sketch_constraint`, …) with exact numbers.
   Deterministic Python inside FreeCAD runs them. `run_freecad_python` is kept as an
   escape hatch that runs inside a transaction.
3. **Stable feature IDs.** The engine detects holes (`H#`), external cylinders/bosses (`C#`),
   planar faces (`P#`), fillets (`R#`) and cones (`K#`) by geometry, not face index. The IDs
   persist across edits by geometric matching, so "H3" means the same hole after other edits.
4. **Verify every edit.** After each operation the engine re-analyses the shape, checks
   validity and volume, and reports the measured result, e.g. `H3 Ø6.000 → Ø8.000 (verified)`.
   A failed op rolls back (transaction abort) and the error goes back to the LLM to retry or ask.
5. **Versioned.** Each chat turn that changes geometry creates snapshot `vN.FCStd` + `vN.stl`.
   Undo/redo restores snapshots, and FreeCAD reloads them.
6. **Ask instead of guessing.** Ambiguous references ("the hole") get a clarifying question
   listing the candidates.

## 3. Operation set (phase 1)

B-rep (works on STEP/IGES and any solid):
- `resize_hole(hole, diameter)`: defeature the old hole (fallback: exact plug) and cut a new one on the same axis and depth
- `move_hole(hole, offset)`
- `add_holes(face, diameter, positions[], depth | through)`
- `remove_features(ids[])`: defeaturing (holes, fillets, bosses)
- `push_pull_face(face, distance)`: extrude a planar face outward (+) or cut inward (−)
- `set_face_distance(face, reference_face, distance)`: for example "make the length exactly 120"
- `fillet_edges` / `chamfer_edges` with edge selectors (face edges, hole rims, edges parallel to an axis, edge between two faces, all edges)
- `add_primitive(box|cylinder|sphere|cone, …, operation fuse|cut|new)`
- `transform_object`, `scale_object`, `delete_object`

Parametric (FCStd):
- `set_property(object, property, value)`, for example `Pad.Length`
- `set_sketch_constraint(sketch, constraint, value)`
- `set_spreadsheet_cell(sheet, cell/alias, value)`

Query:
- `get_model_info`, `find_features`, `measure(a, b)`

## 4. Phases

- **Phase 1 (this change):** new FreeCAD bridge (RPC server, engine, headless autostart),
  backend agent and tools, session/version store, new endpoints (`/cad/upload`, `/chat`,
  `/cad/undo`, `/cad/redo`, `/cad/model`, `/cad/download`), and a frontend wired to them:
  operation cards, model panel, undo/redo, downloads. Legacy pipeline removed.
- **Phase 2:** click-to-select faces in the 3D viewer (send the face ID with the message),
  highlight the features an answer references, colored GLB meshes instead of STL.
- **Phase 3:** more operations (shell, pattern, mirror, thread annotations, sketch-based pocket
  and pad on a face), assemblies (per-part edits, mates), preview-before-apply mode.
- **Phase 4:** evaluation suite: a set of models plus prompts with expected measured results,
  run in CI against headless FreeCAD to track precision.

## 5. Running

1. FreeCAD GUI: Macro → run `backend/freecad_listener.py` (or paste
   `exec(open(r"<repo>/backend/freecad_listener.py").read())` in the Python console).
   If FreeCAD isn't running, the backend can start a headless `freecadcmd`
   (`FREECAD_AUTOSTART=1`, `FREECAD_CMD=<path to freecadcmd.exe>`).
2. Backend: `cd backend && venv\Scripts\python main.py`
3. Frontend: `cd frontend && npm run dev`

LLM config (any OpenAI-compatible endpoint with tool calling):
`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`. The defaults are Groq and `openai/gpt-oss-120b`
using `GROQ_API_KEY`. A stronger tool-calling model noticeably improves reference resolution.
