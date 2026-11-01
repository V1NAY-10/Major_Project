# AetherCAD — Project Status

_Last updated: 2026-11-01_

## Phases

| Phase | Scope | Status |
|---|---|---|
| Rebuild (2026-10-13) | Architecture review, precise parsing, dimension layer, FreeCAD side by side, component IDs, click-to-select | ✅ Done (sections 1–6 below) |
| Phase 1 — Precision foundations (2026-10-14) | Units, tolerance-checked edits, side-effect report, locks, datums, standard hole sizes, angle/radial/hole-to-hole dimensions | ✅ Done and approved ([section 7](#7-phase-1--precision-foundations-2026-10-14)) |
| Phase 2 — Better feature recognition (2026-10-15) | Slots, pockets, chamfers, edge IDs, threads, wall thickness, fillet chains, the three known limitations of §5 | ✅ Done and approved ([section 8](#8-phase-2--better-feature-recognition-2026-10-15)) |
| Phase 3 — Safer, smarter editing (2026-10-15) | Preview, multi-step plans, pre-edit checks, 10 new ops, version tree + compare, clickable ambiguity choices | ✅ Done, committed in `0e6eb44` ([section 9](#9-phase-3--safer-smarter-editing-2026-10-15)) |
| Phase 4 — Analysis and manufacturing (2026-10-15) | Materials + mass properties, DFM (CNC / 3D print / sheet metal) with fixes, BOM CSV/XLSX, interference, exploded view, 2D drawing PDF/SVG, cost estimate | ✅ Done, committed in `0e6eb44` ([section 10](#10-phase-4--analysis-and-manufacturing-2026-10-15)) |
| **Alternatives for open-ended requests** (after Phase 4) | "Make the nose smoother" → 2–4 dry-run alternatives with 3D ghosts; pick one to apply | ✅ Done, **not committed yet** ([section 12](#12-alternatives-for-open-ended-requests-after-phase-4)) |
| **Mesh import + launch-vehicle demo** (2026-10-29) | STL / OBJ / PLY rebuilt as exact solids; end-to-end simulation on a nose-cone STL; 9 bugs it found fixed | ✅ Done, **not committed yet** ([section 14](#14-stl-mesh-import-and-the-nose-cone-demo-2026-10-29)) |
| **Rocket-nozzle demo: forced changes** (2026-11-01) | Second launch-vehicle STL (nozzle + thrust-mount ring); 11 structural / performance edits checked against hand calculations; 7 bugs it found fixed | ✅ Done, **not committed yet** ([section 15](#15-rocket-nozzle-stl-and-forced-design-changes-2026-11-01)) |
| Phase 5 — Viewer and UX | glTF picking, sections, measurement, streaming chat, shortcuts | ⏳ Not started |
| Phase 6 — Robustness, evaluation, deployment | Eval suite, concurrency, security, logging, CI, Docker | ⏳ Not started |

## Summary

| Area | Status |
|---|---|
| Architecture review | ✅ Done. The existing architecture is correct and was kept and extended, not replaced |
| Precise CAD parsing (features, patterns, dimensions) | ✅ Done and tested |
| Dimension / parameter editing from parsed data | ✅ Done and tested |
| FreeCAD side by side (auto-start, window layout, highlight, manual-edit sync) | ✅ Done and tested in the real FreeCAD 1.1.4 GUI |
| Backend AI agent (new tools, fast path, prompt) | ✅ Done and tested end to end |
| Frontend (Dimensions tab, patterns, 3D highlight, Show in FreeCAD) | ✅ Done; typecheck, lint and build pass |
| Component IDs for every face and part; click-to-select inspector | ✅ Done and tested (API + real FreeCAD GUI) |
| Clerk login | ✅ Removed (single-user local app, no sign-in) |
| Precision foundations (Phase 1) | ✅ Done and tested (engine, backend API end to end against headless FreeCAD, frontend build) |
| Feature recognition (Phase 2) | ✅ Done and tested (engine, backend API end to end against headless FreeCAD, frontend build) |
| Safer, smarter editing (Phase 3) | ✅ Done and tested (engine, API, simulation; frontend build) |
| Analysis and manufacturing (Phase 4) | ✅ Done and tested (engine, API, simulation; frontend build) |
| Automated tests | ✅ 417/417 engine checks and 62/62 backend unit tests (2026-11-01); nozzle STL simulation 14/14 deterministic steps, 2 LLM steps failed (§15); nose-cone STL simulation 14/14 deterministic steps (§14); earlier simulations: Phases 1–2 43/43 (§8.7), Phases 3–4 38/38 steps that ran, 11 LLM steps skipped for the Groq quota (§11) |
| Documentation | ✅ README.md, docs/ARCHITECTURE.md, docs/PLAN.md and this file updated through the Alternatives feature |
| Git commit | Rebuild in `9a71faa`, Phases 1–4 in `0e6eb44`. ⏳ The Alternatives feature, `agent/test_options.py` and the `demo/5_payload_adapter` files are not committed yet, nor the STL import (§14), `demo/nosecone/`, the nozzle fixes (§15) and `demo/nozzle/` |

---

## 1. Architecture verdict

The existing design is correct:

```
Next.js chat UI ──HTTP──► FastAPI backend (LLM agent + typed tools) ──JSON-RPC──► FreeCAD (GUI)
```

The key design choices:

- FreeCAD is the only geometry engine and the source of truth.
- The AI calls fixed tools with exact numbers and never writes geometry code.
- Every feature gets a stable ID (H3, P1…).
- Every edit runs in a transaction and is measured afterwards.
- Each chat turn that changes the model creates a version.

**What was missing** was a *dimension layer*. Parsing stopped at faces and holes and never produced the dimensions
a user actually asks to change. So for "make it 120 mm long" the AI had to work out the geometry itself,
which is unreliable. That layer has now been added on top of the existing design.

---

## 2. What was done

### 2.1 Parsing precision (`backend/freecad_bridge/analysis.py`, new; `engine.py`)

| Capability | Before | Now |
|---|---|---|
| Faces stored as B-splines (common from some exporters) | Not recognised at all (whole part parsed to nothing) | Fitted to plane / cylinder / cone / sphere by least squares |
| Countersunk hole depth | Wrong (7.5 instead of 10) | Correct; entry is on the countersink top |
| Counterbored hole | Reported as two unrelated holes | Linked: `counterbore {diameter, depth}`; they move together |
| Hole patterns | Not recognised | Bolt circle (PCD, count, angle), rectangular grid, linear row (`PAT#`) |
| Dimensions | None | Overall sizes, thicknesses, gaps/slot widths, hole-to-edge distances, each named by stable IDs (`P5-P6`, `H3-P5`) |
| Unrecognised faces | Silently ignored | Counted per part and shown in the UI |

### 2.2 Editing using the parsed data

- **`set_dimension`**: sets any dimension exactly. Works for face-to-face, hole-to-face and hole-to-hole, with an anchor of start, end or symmetric.
- **`edit_hole_pattern`**: changes the bolt-circle diameter, hole count or start angle, or the spacing of a linear row or grid.
- **Moving a face keeps its features:** blind holes, counterbores and countersinks on the face keep their depth below it.
- **Fast path, no AI needed:** `P5-P6 = 120`, `H3-P5 = 12 symmetric`, `H2 Ø8`, `PAT1 pcd 80` run instantly and for free.
- **Every edit is still verified:** each change is re-measured, e.g. `P5-P6 = 120.000 ✓`, and a failed edit is rolled back.

### 2.3 FreeCAD side by side

- FreeCAD starts automatically when the backend starts (`FREECAD_PRELAUNCH`).
- The FreeCAD window snaps to the right half of the screen (`FREECAD_WINDOW=right|left|max|none`).
- Clicking a feature, dimension or pattern ID highlights it in both the web 3D viewer and FreeCAD.
- Edits made by hand in the FreeCAD window are detected and saved as a version ("Manual edit in FreeCAD").
- A "Show in FreeCAD" button brings the FreeCAD window to the front.

### 2.4 Backend (`backend/app/`)

- **New AI tools:** `set_dimension`, `edit_hole_pattern`, `list_dimensions`.
- **AI context:** the model summary now includes dimensions and patterns, with new prompt rules for using them.
- **Fast path:** `agent/fastpath.py` runs exact commands without calling the AI.
- **Highlighting:** new endpoints `/cad/highlight/{id}` and `/cad/show/{id}`, and chat replies return a `highlight` list.
- **Environment:** `backend/venv` is created and the requirements are installed.

### 2.5 Frontend (`frontend/src/`)

- **New Dimensions tab:** clicking a dimension fills in `P5-P6 = ` so you only type the new value.
- **Features tab:** shows hole patterns and counterbore details.
- **3D viewer:** draws amber highlights on top of the model.
- **Show in FreeCAD:** new button in the workspace header.
- **Lint:** ESLint upgraded to 9, which fixes a lint setup that was already broken, and all lint errors are fixed.

### 2.6 Bugs fixed

- FreeCAD's "modified" flag gave false manual-edit detections on every first message. Detection now uses a geometry signature.
- Moving a part inside an assembly used the wrong coordinate frame.
- The flange's top and bottom faces were hidden, so it had no thickness dimension.
- A hole stored as a B-spline was classified as a fillet.
- Pushing a face in left thin slivers next to countersinks.
- The FreeCAD listener had a hard-coded path from another machine (`C:\VINAY_MAIN\...`).

### 2.7 Component IDs and selection (2026-10-13)

- **Every face has an ID.** Faces no rule recognises are now `F#` features instead of being ignored. The tests
  check, on every fixture, that each face belongs to exactly one ID and that IDs are unique.
- **Part IDs** `B1, B2…` for each solid (stable across edits; accepted anywhere an object name is).
- **Click to select** in the 3D viewer: "Feature" mode picks the hole/face/fillet you clicked, "Part" mode picks
  the whole part, Shift+click adds. "From FreeCAD" takes what you selected in the FreeCAD window. IDs in the
  panel select too.
- **Inspector**: the selected component is highlighted in both views and its card shows its ID and all data
  (diameter, depth, countersink/counterbore, normal, entry, centre, area, part, pattern), its dimensions
  (click to set) and quick-edit buttons.
- **Edit the selection**: the message box shows "Editing H3 …" chips and your next message applies to them.
  `Ø10` with a hole selected or `120` with a dimension selected runs instantly (no AI); otherwise the AI gets the
  selection with its data ("move this 5 mm towards the front" moved the selected hole: verified).
- **Clerk removed** from frontend and backend; all chat sessions are listed (no per-user filter).

---

## 3. Verification

| Check | Result |
|---|---|
| Engine test suite: `freecadcmd backend/tests/test_engine.py` | **181 passed, 0 failed** (~9 s) at the time (184 when Phase 1 started; 242 after Phase 1, see §7.4) |
| Fast-path unit tests | **9/9 OK** |
| Selection end to end (live backend + FreeCAD GUI) | pick hole wall → H1 ✓; part mode → B1 ✓; highlight → selected in FreeCAD → read back `["H1"]` ✓; `Ø10` with H1 selected → 10.000 ✓; "move this 5 mm towards the front edge" → H1 moved to Y 25 ✓ |
| Frontend `tsc --noEmit`, `npm run lint`, `next build` | All pass |
| GUI test inside real FreeCAD 1.1.4 | Window placement, selection, manual-edit detection and edits all correct |
| Full stack (backend auto-launches FreeCAD GUI) | Upload → `PAT1 pcd 80` ✓ → highlight ✓ → undo ✓ → show ✓ |
| Natural-language test via LLM | "Put the bolt holes on a 76 mm bolt circle and make the flange 15 mm thick": the AI chose `edit_hole_pattern` and `set_dimension` by itself; verified **PCD 76.000** and **15.000 mm** |
| Performance | 30-part assembly: open 0.8 s, analysis 0.22 s, dimensions 0.24 s |

---

## 4. What you need to do

1. **Run it:**
   - Backend: `cd backend && venv\Scripts\python main.py`. FreeCAD opens automatically.
   - Frontend: `cd frontend && npm run dev`, then open http://localhost:3000.
2. **Optional:** use a stronger or higher-limit LLM. The Groq free tier (8k tokens per minute) works but pauses between steps. Set `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` in `backend/.env`.
3. **Optional:** delete the unused `CLERK_SECRET_KEY` line from `backend/.env`.
4. **Commit the Alternatives work** once you're happy with it (Phases 1–4 are committed). Everything else still open is in [§13](#13-whats-remaining).

---

## 5. Known limitations

_Fixed in Phase 2 (regression tests in `test_phase2_regressions`):_

- ~~Moving a face leaves a fillet along its edge in its old position.~~ The fillet is lifted and re-made at the new position.
- ~~Pushing a face past the top of a boss standing on it leaves a pocket above the boss.~~ The boss footprint is filled.
- ~~Grid hole patterns can't change their hole count.~~ `count_u` / `count_v`.
- ~~Slots and pockets aren't single features.~~ `SL#` / `PK#` (open-ended grooves still appear as `gap` dimensions).

_Still open:_

- Free-form (non-analytic) faces stay unclassified and are reported as `unrecognized_faces`.
- Single backend process and single FreeCAD instance: sessions are served one at a time.
- Phase 1 limitations are listed in [§7.6](#76-known-limitations-of-phase-1), Phase 2 limitations in [§8.6](#86-known-limitations-of-phase-2).

## 6. Possible next steps

Superseded: preview, shell, mirror, slot / pocket edits and grid hole counts were done in Phases 2–3. The current
list of open work is in [§13](#13-whats-remaining).

---

## 7. Phase 1 — Precision foundations (2026-10-14)

Goal: every number the system shows or produces is exact, unit-correct and explicitly verified.
The architecture is unchanged (Next.js → FastAPI → FreeCAD); everything is new typed tools, engine ops and checks.

### 7.1 What was built

| Requirement | Implementation |
|---|---|
| **Units and number parsing** | `backend/app/units.py` (shared by the fast path and the tool-argument layer): mm, cm, m, µm, in / `"`, ft / `'`, fractions (`1/4"`, `3 1/2 in`), degrees / rad, and `Ø` / `⌀` / `dia` / `D` / `R` / `r` / `radius`. Tool arguments may be numbers (mm) or strings with a unit; they are converted exactly before reaching FreeCAD. |
| Display unit and precision | Session setting (mm or inch, 2–4 decimals) in the workspace settings bar; stored in `meta.json`; internal storage stays mm. A bare number typed in the chat uses the display unit. |
| **Tolerance-aware verification** | Session tolerance (default ±0.001 mm, ±0.01°). Every verified value now has `requested / achieved / deviation / within_tolerance`. The check runs **inside the FreeCAD transaction**: an edit outside tolerance is aborted (document unchanged) with the reason. Chat shows ✓ (green) or ⚠ (amber) chips with Δ. |
| **Side-effect report** | `backend/app/precision.py` compares every dimension, feature parameter and pattern value before and after an edit and lists everything that changed except the target. Changes involving a feature the edit touched are "expected"; others are flagged amber. Collapsible table under each operation card; a short version goes to the LLM. |
| **Locks / design intent** | `lock_dimension`, `unlock_dimension`, `list_locks` tools; lock icons in the Dimensions tab and the inspector; "Keep centred" for holes and bosses. Locks are per session. The engine checks them inside the transaction; a violating edit is refused, or, when the user didn't fix the anchor, retried with the other anchors (`end`, `symmetric`) or the opposite face, and the reply says which lock decided it. |
| **Datums** | Default A = largest planar face, B and C = largest outermost faces perpendicular to it (prefers the low side). Set them with `set_datums`, `datum A = P3`, or the inspector's A/B/C buttons. Holes and bosses report `datum_position`; `move_hole(to_datum={B,C})` and `add_holes(positions=[{B,C}])` accept datum coordinates, verified per datum. |
| **Standard sizes** | `backend/app/standards.py`: ISO 273 clearance (fine/normal/coarse, M1.6–M36), tap drills M2–M24 coarse and common fine pitches, ISO 4762 counterbores (DIN 974-1 diameters), ISO 10642 countersinks, metric and fractional drill series. Recognised holes are named (`Ø6.6 = M6 clearance (normal)`); `set_hole_standard` ("make H3 an M8 clearance hole", "tap H3 for M6", "M6 counterbore") resolves to exact sizes and re-cuts the hole with the new engine op `set_hole_profile` (diameter + counterbore/countersink in one verified edit). |
| **Angular and radial dimensions** | New dimension types: `angle` (`P3∠P7`, included angle between adjacent non-square planar faces), `diameter` (`C1.Ø`) and `radius` (`R2.R`), `center_distance` (`H1-H2`, nearest parallel neighbours, ties kept), `coaxial` (`H1◎H2`). All editable with `set_dimension`: angles rotate a face about its shared edge, boss diameters grow/shrink the boss, fillet radii re-make the fillet, coaxiality 0 moves a hole onto the other's axis. |

### 7.2 Files changed

| File | Change |
|---|---|
| `backend/app/units.py` (new) | Unit parsing and display formatting |
| `backend/app/standards.py` (new) | Standard-size tables, `resolve()` for requests, `identify()` / `annotate()` for recognised holes |
| `backend/app/precision.py` (new) | Session settings defaults, side-effect diff, verification display strings |
| `backend/freecad_bridge/analysis.py` | Angle, radial, centre-distance and coaxial dimensions; deterministic tie-break for hole edge distances |
| `backend/freecad_bridge/engine.py` | Tolerance and lock checks inside the transaction (`_apply_once`), lock alternatives, `evaluate_locks` RPC, datums (`_resolve_datums`, `_datum_point`), dimension parser for `∠ ◎ .Ø .R`, `set_dimension` for angles / radii / coaxiality, `set_hole_profile` op, requested values on every verifiable op |
| `backend/app/cad_service.py` | Settings, locks and datums in `meta.json`; standard names on every info update; tolerance/locks/datums passed to the engine |
| `backend/app/agent/tools.py` | New tools (`set_hole_standard`, `lock_dimension`, `unlock_dimension`, `list_locks`, `set_datums`), unit-aware argument layer, side effects in edit results |
| `backend/app/agent/fastpath.py` | Units, angles, radial sizes, standards, locks and datums commands |
| `backend/app/agent/prompt.py`, `runner.py` | Short precision rules, a SESSION line (unit, tolerance, locks), datums and standard names in the summary; new ID forms highlighted |
| `backend/app/routes.py` | `PUT /cad/settings`, `POST /cad/locks`, `POST /cad/locks/remove`, `PUT /cad/datums` |
| `backend/tests/fixtures.py`, `test_engine.py` | `chamfer_block`, `coaxial_block` fixtures; 58 new checks |
| `backend/app/test_units.py`, `test_standards.py`, `test_precision.py` (new), `agent/test_fastpath.py` | 28 new unit tests |
| `frontend/src/lib/units.tsx` (new), `components/cad/SettingsBar.tsx` (new) | Display-unit context and the unit / decimals / tolerance bar |
| `frontend/src/components/chat/ChatMessage.tsx` | ✓/⚠ chips with deviation, side-effect table, lock note |
| `frontend/src/components/cad/ModelPanel.tsx`, `SelectionInspector.tsx` | Values in the display unit, standard names, datum positions, new dimension groups, lock icons, datums and locks strip, datum A/B/C and "Keep centred" buttons |
| `frontend/src/types/*.ts`, `hooks/useCADSession.ts`, `app/page.tsx` | Types and calls for settings, locks and datums |

### 7.3 How to use it

- Units: `P5-P6 = 4.5 in`, `H2 Ø1/4"`, `PAT1 pcd 3 1/2 in`, or switch the display to inch and type `P5-P6 = 4`.
- Angles and radii: `P3∠P7 = 60` (degrees), `C1 Ø30`, `R2 R3`, `H1◎H2 = 0`.
- Standards: `H3 M8 clearance`, `tap H3 for M6`, `H3 M6 counterbore`, or select a hole and type `M8 clearance`.
- Locks: click a lock icon, or `lock P1-P2`, `lock H1 centred X`, `unlock all`, `locks`.
- Datums: `datum A = P3`, inspector buttons A/B/C; "move H3 to 15 from datum B and 20 from datum C".
- Tolerance: the settings bar above the viewer; an edit that misses it is rolled back.

### 7.4 Verification

| Check | Result |
|---|---|
| Engine suite `freecadcmd backend/tests/test_engine.py` | **242 passed, 0 failed** (184 before Phase 1; 58 new), ~13 s |
| Backend unit tests `python -m unittest discover -s app -t .` | **37 passed** (12 fast-path incl. 3 new, 7 units, 6 standards, 12 side-effect / display / argument layer) |
| API end to end (FastAPI TestClient + headless FreeCAD bridge, fast-path commands) | 19/19: inch input → 114.300 mm verified; inch display; lock refuses a thickness change; lock/unlock/list; `M8 clearance` on a selected hole → Ø9 named "M8 clearance (normal)"; `M8 counterbore` → Ø9 + Ø15 × 8.6; datum set/reset; bad lock targets rejected |
| Frontend `npx tsc --noEmit`, `npm run lint`, `npm run build` | All pass |
| Visual check of the new UI in a browser | **Not done** (no browser available in this session); please look at the settings bar, ✓/⚠ chips, side-effect table and lock icons |
| LLM path with a real model | **Not run** (no LLM calls made); the fast path and tool layer were tested directly |

Acceptance criteria: unit-parsing and tolerance tests ✓; fixtures report angles (chamfer block: 135°) and hole standards
(demo plate: M6 counterbore, M10 clearance, M6 tap drill) ✓; an edit that disturbs a locked dimension is refused ✓;
the side-effect table is produced on a real edit ✓.

### 7.5 Token budget

Tool schemas grew from 11,986 to 13,806 characters and the system prompt from 4,328 to 5,208 characters
(≈ +700 tokens per LLM request). Datum positions are only in `get_model_info` / `find_features`, not in the
prompt summary; edit results carry a compact side-effect summary only.

### 7.6 Known limitations of Phase 1

- Angle edits rotate a face about the straight edge it shares with the other face, and need flat, convex
  neighbours; `symmetric` is not supported for angles. Holes in a rotated face are not re-cut.
- Fillet radius edits support cylindrical fillets only (not toroidal ring fillets).
- Ops without a numeric target (`remove_features` is checked for removal only; `add_primitive`, `chamfer_edges`,
  `delete_object`, rotations, `run_freecad_python`) report no requested/achieved values.
- A part lock compares volume and bounding box only; a feature lock compares size and axis position.
- Counterbore depths in the ISO 4762 table are typical design values (head height + clearance); clearance holes for
  counterbores and countersinks use the normal fit. Numbered and letter drills are not in the drill table.
- Bare numbers use the display unit: with the display set to inch, `P5-P6 = 120` means 120 in. Replies always show the unit.
- The angle shown is the included angle (90° = square corner); a "30° slope" must be converted by the user or the LLM.

---

## 8. Phase 2 — Better feature recognition (2026-10-15)

Goal: recognise the features machinists and designers talk about as single, editable features, give every edge an
ID, and fix the three known limitations of face moves and grids. Every face still has exactly one ID.

### 8.1 What was built

| Requirement | Implementation |
|---|---|
| **Slots `SL#`** | Closed recesses that open through an inner outline of a planar face, with vertical walls: obround (two parallel walls + two half-cylinder ends) or rectangular (sharp corners, length ≥ 2 × width); through (exit through the opposite face, entered from the upward face like holes) or blind (planar floor). Reported: shape, width, length, depth, through, centre (of the opening), direction (length axis), face it opens on, edge distances to the part's faces. **Edits:** `edit_slot` (width, length, depth, move by offset / to / datums) re-fills the slot with its exact shape and cuts the new one; `remove_features`. |
| **Pockets `PK#`** | Any other closed blind recess with a planar floor: length, width, depth, corner radius (0 = sharp), outline, floor area, face. **Edits:** `edit_pocket` depth (the floor moves; holes in the floor keep their depth below it) and corner_radius (corner rounds re-made or added); `remove_features`. `add_holes(face=PK1)` drills into the floor. |
| **Chamfers `CH#`** | Narrow planar strips (≤ ¼ of the neighbouring faces' width) bevelling a convex edge between two planar faces: size (or both distances when unequal), angle to the first face, length, the two faces. **Edits:** `edit_chamfer` size, `to_fillet` (becomes a fillet), `remove_features` (sharp edge restored). |
| **Edge IDs `E#`** | Every edge of every part has a type (line / circle / arc / …), length, adjacent feature IDs and convexity (convex / concave / tangent / seam). IDs are given on demand (listing with `find_edges`, picking in the new **Edge** mode of the viewer, use) and matched geometrically across edits. `fillet_edges` / `chamfer_edges` accept `{edge: "E12"}`; fast path `fillet E12 R2`, or select an edge and type `R2` / `C1`. Picked edges are drawn as amber lines in the viewer and selected in FreeCAD. |
| **Threads** | Threaded `PartDesign::Hole` features in FCStd files are read (size, depth, modelled or cosmetic) and attached to the recognised holes. `set_thread` (or `H3 thread M8x1.25 depth 12`) records a thread annotation on any hole in the session; holes whose diameter isn't the tap drill get a note. Shown in the panel and inspector. |
| **Wall thickness** | `measure_wall_thickness` (tool, fast path `wall thickness`, inspector button on parts, `GET /cad/wall-thickness`): rays from sample points on every face into the material; the thinnest is refined with the exact distance between the two faces. Returns the minimum, where it is, the two faces, and faces under a threshold. |
| **Toroidal fillets and fillet chains** | Torus fillets are marked `toroidal`. Fillet faces that meet tangentially with the same radius (cylinders, tori, spherical corner blends) are merged into one `R#` with `chain_faces`. |
| **Known limitations fixed** | (1) A fillet along an edge of a moved face is lifted (defeaturing) and re-made at the new corner, keeping its ID. (2) Pushing a face out fills the footprints of bosses standing on it, so a face pushed past a boss buries it instead of leaving a pocket. (3) Grids change their hole count with `count_u` / `count_v` (IDs of the surviving holes kept). |

### 8.2 Files changed

| File | Change |
|---|---|
| `backend/freecad_bridge/analysis.py` | `Adjacency` (faces per edge), `fillet_chains`, `recesses` (slots, pockets), `chamfers`, `edge_rows` + convexity, grid counts in `pattern_positions`, slot/pocket edge distances |
| `backend/freecad_bridge/engine.py` | New kinds `SL`/`PK`/`CH`, recognition passes in `_features_of_shape`; ops `edit_slot`, `edit_pocket`, `edit_chamfer`; slot / pocket / chamfer removal; edge table with lazy IDs (`_edge_table`, `_edge_ids`), edge selectors, edge picking and highlighting; `find_edges` and `wall_thickness` RPCs; threads from `PartDesign::Hole`; fillets travel with moved faces; boss footprints filled on push-out; grid `count_u` / `count_v`; drilling into pocket floors |
| `backend/app/agent/tools.py` | Tools `edit_slot`, `edit_pocket`, `edit_chamfer`, `find_edges`, `measure_wall_thickness`, `set_thread`; `{edge}` selector; grid counts; editing tools only offered when the model has such features |
| `backend/app/cad_service.py`, `routes.py` | Thread annotations in the session, `GET /cad/wall-thickness`, pick mode `edge` |
| `backend/app/agent/fastpath.py`, `prompt.py`, `runner.py`, `precision.py` | Commands for slots, pockets, chamfers, edges, threads, wall thickness, grid counts; summary fields; new IDs highlighted; side-effect fields for slots / pockets / chamfers |
| `backend/tests/fixtures.py`, `test_engine.py` | Fixtures `slot_plate`, `pocket_block`, `chamfer_box`, `fillet_chain_box`, `boss_rim_plate`, `thin_wall_plate`, `write_threaded_fcstd`; 84 new checks incl. "no face owned by two features" and the three regression tests |
| `backend/app/agent/test_fastpath.py`, `app/test_precision.py` | 5 new unit tests |
| `frontend/src/types/cad.ts`, `components/cad/CADViewer.tsx`, `ModelPanel.tsx`, `SelectionInspector.tsx`, `chat/ChatMessage.tsx`, `app/page.tsx` | New kinds and edge type; edge lines in the viewer; Edge pick mode; Slots / Pockets / Chamfers sections; thread and chain info; inspector cards for slots, pockets, chamfers and edges; wall-thickness button on parts |

### 8.3 How to use it

- Upload a part with slots / pockets / chamfers: they appear as `SL#`, `PK#`, `CH#` in the Features tab.
- `SL1 width 10`, `SL1 length 40`, `SL1 depth 6`, "move SL1 5 mm towards the back".
- `PK1 depth 10`, `PK1 corner R3`; "drill a Ø4 hole in the floor of PK1 at 30, 30".
- `CH1 3`, `CH1 to fillet R2`, "remove CH1".
- Switch the viewer to **Edge**, click an edge, type `R2` (fillet) or `C1` (chamfer); or `fillet E12 R2`.
- `wall thickness`, or the "Wall thickness" button on a selected part.
- `H3 thread M8x1.25 depth 12`; threaded holes of FCStd files show their thread automatically.
- `PAT1 count_u 3` changes a grid's hole count.

### 8.4 Verification

| Check | Result |
|---|---|
| Engine suite | **326 passed, 0 failed** (242 before Phase 2; 84 new), ~20 s |
| Backend unit tests | **42 passed** (5 new) |
| API end to end (TestClient + headless FreeCAD, fast-path commands) | 13/13: slots recognised on upload, `SL1 width 10` verified, slot selected + `width 12`, `wall thickness` (5.000 mm under the blind slot), wall-thickness endpoint, edge pick → E#, edge highlight → lines, edge selected + `R2` → fillet verified, `CH1 3`, `PK2 depth 10`, thread annotation set / removed |
| Demo STEP files | No false positives: plate, flange, L-bracket and bar parse exactly as before |
| Frontend `tsc --noEmit`, `npm run lint`, `npm run build` | All pass |
| Browser check of the new UI / LLM path with a real model | **Not done** in this session (no browser; no LLM calls) |

Acceptance: a fixture with exact ground truth for each new feature ✓ (slot widths/lengths/depths and edit volumes,
pocket floor areas and corner volumes, chamfer sizes and volumes, 20-face fillet chain, toroidal rim, 2.0 mm wall,
FCStd threads); every face has exactly one ID ✓ (coverage + "no face owned by two features" on every new fixture);
the three known limitations have regression tests ✓.

### 8.5 Token budget

Tool schemas: 15,408 characters for a part without slots / pockets / chamfers (13,806 after Phase 1), 16,987 with
all of them; the slot / pocket / chamfer / thread tools are only sent when the model has such features. System prompt
5,561 characters (5,208).

### 8.6 Known limitations of Phase 2

- Slots and pockets need vertical walls (perpendicular to the face they open on) that open through an *inner*
  outline: open-ended grooves, slots breaking out of an edge, drafted walls and pockets with filleted floors stay
  planes / rounds (their widths and depths are still `gap` / `thickness` dimensions). Through cutouts that are neither
  obround nor sharp rectangular slots stay as faces.
- Pocket length / width are reported but not editable (only depth and corner radius); changing depth and corner
  radius needs two edits.
- Chamfers are planar strips only (no conical chamfers on bosses; a chamfer on a hole entry is reported as a
  countersink). Very wide bevels (> ¼ of the neighbouring faces) stay inclined planes.
- Edge IDs are given when edges are listed, picked or used; they follow an edge as long as its midpoint and length stay
  the same (an edge that an edit lengthens gets a new ID).
- Thread annotations can't be written into STEP files (FreeCAD's exporter has no thread/PMI support): they live in
  the session and are read back from threaded `PartDesign::Hole` features of FCStd files.
- Wall thickness is sampled (≈ 6 rays per face, capped at 3,000 per request) and then refined for the thinnest spot,
  so a very small thin region between sample points can be missed.
- Moving a face re-makes cylindrical fillets running along its edges between two planar faces; fillet chains and ring
  fillets on a moved face are not re-made.
- Analysis of the 30-part benchmark assembly takes ~0.5 s (0.25 s before Phase 2) because of the new passes.

### 8.7 End-to-end simulation of Phases 1 and 2 (2026-10-15)

A scripted user session drove the real stack (backend routes → fast path / LLM agent with `openai/gpt-oss-120b` on
Groq → headless FreeCAD) through 43 steps covering every Phase 1 and Phase 2 deliverable, each checked against known
geometry: **43/43 pass**, including 6 natural-language requests (datum move, "make the remaining 6 mm holes M6
clearance" → 3 × `set_hole_standard`, slot removal by description, drilling into a pocket floor, two wall-thickness
questions). The interactive report with before/after 3D views is published as a private artifact.

The first run found two problems, both fixed:

| Found | Fix |
|---|---|
| `PAT1 count_u 3` with the old 80 mm spacing put two holes outside the plate and was **accepted** with only a "no pattern found" warning | `edit_hole_pattern` refuses positions that are off the part; and any edit whose requested value cannot be measured afterwards is now rolled back (`_apply_once`). Regression test added (engine checks 326 → 327). |
| Asked to drill into a pocket floor, the LLM picked the top face (refused) and asked for a face ID | The `add_holes` description now says `face` may be a pocket / blind slot ID; on the re-run the LLM called `add_holes(face=PK2)` |

---

## 9. Phase 3 — Safer, smarter editing (2026-10-15)

### 9.1 What was built

| Requirement | Implementation |
|---|---|
| **Preview before apply** | Every edit can run as a dry run. One edit: the engine applies it inside its transaction, measures it, writes the result mesh (`preview.stl`) and aborts (`apply(..., preview_mesh=)`). Several edits: they are applied, meshed, and the head version is restored from its snapshot. Either way the document is unchanged (engine test: geometry signature identical). The viewer shows the result as a cyan ghost over the model with **Apply / Cancel**. `preview <command>` in the chat, `POST /cad/preview`, and the agent's `propose_plan` all create one. Large edits (shell, split, scale, mirror part, draft, ≥ 3 edits, replace, removing ≥ 3 features, patterns ≥ 6) are previewed automatically instead of applied. |
| **Multi-step plans** | `propose_plan` (agent) lists the tool calls; the user sees a numbered plan with each step's verified preview values and approves it (button, or *apply* / *yes*). Applying runs the steps as one version; if step N fails, everything goes back to the version the plan started from. |
| **Pre-edit checks** | Inside the engine transaction, before commit: a hole that would overlap another or break out of the part, any hole / slot / pocket an edit would destroy, and a part moved into another (new interference) are refused with the reason (overridable with `force`); a wall made thinner than the session's *min wall* (default 1 mm, settings bar) gives a warning shown on the operation card. |
| **New operations** | `mirror_object` (copy / replace / fuse-symmetric), `mirror_features`, `pattern_feature` (linear / circular, any hole / slot / pocket / boss; copies landing on an identical feature are skipped), `add_slot`, `add_pocket`, `add_counterbore`, `add_countersink`, `draft_faces` (rotates walls about their edge with a neutral face), `split_object`, `shell_object`. All transactional, verified (expected new-feature counts, sizes, angles) and covered by engine tests. |
| **Version tree** | Versions have a parent: an edit after undo starts a branch and the undone versions stay. Undo = parent, redo = newest child, **go to** any version, **name** versions. **Compare** two versions: every dimension / feature value that changed, and the material added (green) / removed (red) drawn in the viewer. |
| **Ambiguity** | The agent's `ask_user` tool returns clickable choices; hovering a choice highlights the feature in 3D, clicking answers with it selected. |

Supporting change: the ~17 new tools are in on-demand groups (`load_tools` modeling / analysis / more), pre-loaded by keywords in the message, so a request carries no more tool text than before (Groq free tier: 8k tokens/minute).

### 9.2 Files

| File | Change |
|---|---|
| `backend/freecad_bridge/editing.py` (new) | Phase 3 ops, `feature_tool`, `compare_versions`; planes also as `y = 20` (found by the simulation: "mirror about y = 20" was sent as `YZ`) |
| `backend/freecad_bridge/engine.py` | `_apply_once`: preview mesh + abort, expected feature counts, `_pre_checks` (destroyed features, interference, thin walls), refusals of overlapping holes in `add_holes` / `move_hole`, `allow_removed` per op, `_wall_thickness_of`, module registration |
| `backend/app/cad_service.py` | Version tree (parents, branches, per-version info snapshots, checkout, rename, compare), engine preview pass-through, pre-edit check settings |
| `backend/app/agent/plans.py` (new) | Preview / apply / cancel of pending plans, large-edit rule, plan text |
| `backend/app/agent/runner.py` | Plans (`propose_plan`, auto-preview, *apply* / *cancel* / *preview …*), `ask_user` choices, tool groups, `run_tool`, tool-call logging; found by the simulation: **compound requests are collected into one plan** (`is_compound`), **a failed plan preview goes back to the model once** keeping the steps that worked (`_plan_retry`), and **a reply that claims edits no tool made is withheld** (`_guard_unverified_claims`) |
| `backend/app/agent/tools.py`, `prompt.py`, `messages.py`, `routes.py` | New tools and groups, prompt rules, plan / choices in messages, endpoints: preview, plan apply / cancel, preview mesh, checkout, rename, compare |
| `frontend/src/components/cad/VersionTree.tsx` (new), `CADViewer.tsx`, `chat/ChatMessage.tsx`, `SettingsBar.tsx`, `app/page.tsx`, `hooks/useCADSession.ts`, `types/*` | Version tree tab with compare, ghost / diff overlays, plan Apply / Cancel, choice buttons with hover highlight, warnings, min-wall setting |

### 9.3 Known limitations of Phase 3

- Plan steps are fixed when previewed; a step that refers to a feature created by an earlier step of the same plan must use the ID the preview showed.
- Draft is a rotation about the edge shared with the neutral face (flat, convex neighbours only); there is no OpenCASCADE draft-angle operation in FreeCAD's Python API.
- Mirror / pattern work on holes, slots, pockets and bosses (not on fillets or chamfers).
- The thin-wall pre-check ray-casts parts with ≤ 400 faces (skipped above that for speed).
- Version compare needs the per-version info snapshots written since Phase 3 for the dimension table (the geometry diff works for any version).

---

## 10. Phase 4 — Analysis and manufacturing (2026-10-15)

### 10.1 What was built

| Requirement | Implementation |
|---|---|
| **Mass properties** | Material library (17 materials: steels, stainless, aluminium 6061 / 7075, brass, copper, titanium, cast iron, ABS, PLA, PETG, nylon, POM, PC, oak) with densities; material per part (`set_material`, Analysis tab, `material all steel`); mass, centre of mass, inertia tensor about the centre of mass and principal moments (kg·mm²), volume, area per part and in total. Engine test: steel box mass and inertia within 0.1 % of the hand calculation. |
| **DFM checks** | CNC: hole depth / diameter (> 5 warning, > 10 error), pocket internal corners vs cutter radius (sharp = error), non-standard hole sizes, thin walls, deep narrow slots, sharp blind rectangular slots. 3D printing: overhang area per face (> 45° from vertical, not on the bed), small holes / pins, walls under 2 × nozzle. Sheet metal (detected constant-thickness plates): hole-to-edge (< 2t) and hole-to-bend (< 2.5t + R). Each finding names its features; where possible it carries a one-click fix (`edit_pocket` corner radius, `resize_hole` to the nearest standard drill, `set_dimension` moving a hole away from the edge) applied as a new version. |
| **Assembly tools** | Bill of materials grouping identical parts (geometry signature) with quantity, material, unit / total mass, size — CSV and XLSX download (XLSX written directly, no extra dependency). Interference (overlap volume) and clearance check between parts. Assembly view in the viewer: per-part meshes, visibility toggles and an explode slider (picking still works). |
| **2D drawing** | A3 first-angle sheet from FreeCAD TechDraw projections (`TechDraw.projectEx`, visible + dashed hidden lines): front, top, left and isometric views, automatic standard scale, overall dimensions with arrows, hole callouts with centre marks (`4× Ø6 THRU`, counterbore / countersink notes), first-angle symbol and title block (part, material, mass, scale, date). Written as SVG and PDF (vector, Helvetica), downloadable from the Analysis tab or the chat (`drawing`). |
| **Cost estimate** | Rough per-part cost for CNC (stock with allowance, material price, removal rate by material, per-feature time, setup amortised over the quantity) or FDM printing (printed volume, filament price, print time), clearly labelled as an estimate (USD). |

### 10.2 Files

| File | Change |
|---|---|
| `backend/freecad_bridge/manufacturing.py` (new) | `mass_properties`, `dfm`, `interference`, `bom_groups`, `part_meshes`, `drawing` (projection, layout, SVG + PDF writers) |
| `backend/app/materials.py`, `reports.py`, `analysis_service.py` (new) | Material library and cost model; BOM rows, CSV, XLSX; session-level analysis used by tools and routes |
| `backend/app/routes.py`, `agent/tools.py`, `agent/fastpath.py` | Endpoints (materials, mass, DFM, run-tool, BOM, interference, part meshes, drawing, cost), analysis tools, fast-path commands (`mass`, `dfm cnc`, `bom`, `interference`, `drawing`, `cost cnc 10`, `material all steel`) |
| `frontend/src/components/cad/AnalysisPanel.tsx` (new), `ModelPanel.tsx`, `page.tsx` | Analysis tab (materials, mass, DFM with fix buttons, BOM + downloads, interference, drawing preview + downloads, cost), Assembly view controls |

### 10.3 Known limitations of Phase 4

- Material prices, removal rates and print speeds are illustrative defaults; the cost is a rough estimate, not a quote.
- The drawing has overall dimensions and hole callouts only (no feature-to-feature dimensions, tolerances or section views), and is generated for the largest part of an assembly unless a part is given. It is drawn from TechDraw projections, not saved as a TechDraw page in the FCStd.
- DFM sheet-metal rules apply only to parts detected as constant-thickness plates; bends are found as coaxial concave / convex rounds whose radii differ by the thickness.
- BOM groups parts by geometry (volume, area, size); mirrored copies count as identical.
- Overhang detection works on the part's current orientation (Z up = build direction).

---

## 11. End-to-end simulation of Phases 3 and 4 (2026-10-15)

A scripted user session drove the real stack: the FastAPI routes in-process, the agent (fast-path commands and LLM
requests) and headless FreeCAD 1.1.4. Each step was checked automatically against the known geometry of the test
fixtures. Interactive report (3D before/after per step, preview ghosts, compare diff, the generated drawing and BOM):
https://claude.ai/artifact/75zrUBbRoWHbVCwJ5GMzDP

| Result | Count |
|---|---|
| Steps that ran (fast path, API, analysis) | **38/38 passed** |
| LLM steps skipped (Groq free tier: daily token quota used up for both gpt-oss-120b and gpt-oss-20b) | 11 (9 requests + 2 follow-ups) |

Covered and passing: preview leaves the model unchanged and applies as one version; a plan failing at step 3, both
at preview and at apply time, rolls back to the start version; overlapping holes, break-out and interference are
refused and a thin wall is warned; pattern, pocket, counterbore, draft, mirror (fuse) and split; undo then edit
makes a branch, a version is named, and two branches are compared (8 changed values, 12,219.9 mm³ removed); steel
box mass 472.20 g and Ixx 145.595 kg·mm², both matching the hand calculation; the CNC check flags the bad part and
the one-click fix removes the corner finding; print overhang and sheet-metal edge/bend findings; BOM 1 top /
2 rails / 4 legs as CSV and XLSX; 7 part meshes; interference 4000 mm³; drawing PDF/SVG with
`4× Ø6 THRU`; CNC cost estimate.

Found by the LLM runs earlier the same day, and fixed:
- replies claiming edits that no tool made are now withheld;
- compound requests become one plan;
- a failed plan preview gets one corrected retry, keeping the steps that worked;
- mirror planes can be written as `y = 20` (new engine check).

After the fix, gpt-oss-20b mirrored the hole correctly and also added the slot. The skipped LLM steps should be
re-run once the quota resets: run `simulate.py` from the scratchpad, or use the chat.

---

## 12. Alternatives for open-ended requests (after Phase 4)

Goal: a qualitative request with no numbers ("make the nose end smoother", "make it lighter", "what are my options
to …") should not make the agent silently pick one edit. It now offers 2–4 genuinely different approaches, each
dry-run in FreeCAD, and the user picks one.

### 12.1 What was built

| Piece | Implementation |
|---|---|
| Detection | `runner.is_open_ended`: qualitative words (smoother, lighter, stronger, nicer, improve, options, other ways …) **and** no digits in the message. In that mode the model gets a system note to call `propose_options`, and any direct edit-tool call is refused with "offer this as one alternative" |
| `propose_options` tool | Control tool, always sent (like `propose_plan` / `ask_user`): `question` + 2–4 options, each `{title, description, steps[{tool, args}]}` |
| Dry runs | `plans.propose_options` runs each option through the same `_dry_run` as plans (refactored out of `plans.preview`); a workable option's mesh is kept as `preview_opt<N>.stl`. An option that fails stays in the list as "not possible" with the reason. Stored in `meta["options"]` with its base version |
| Retry | If every option fails its dry run, the errors go back to the model once to try other features / edges / tools |
| Picking | Option card **Preview** (ghost in the 3D view) / **Apply** in the chat, or type `2`, `option 2`, `use 1`, `#2`. `plans.apply_option` turns it into a pending plan and applies it as **one** version (all steps or nothing). `cancel` drops the options. Options go stale when the head version changes |
| Robustness | `_unsent_tools`: if the model calls a known tool that wasn't sent (e.g. `draft_faces` before `load_tools`), the retry includes it. `_gave_up`: when tools were withdrawn after repeated failures and the model still won't answer, the reply lists what was tried, why it failed, and that nothing was modified |
| Prompt | New rule for open-ended requests; free-form `F#` faces can't be drafted or pushed, so work on their edges, neighbouring planar faces or the whole part |
| API | `POST /cad/options/{sid}/apply {key, set_id}`; `GET /cad/preview-mesh/{sid}?opt=N`; `public_state` exposes `options {id, base_version}` while current; chat messages store `options`, and the history sent to the LLM notes which options were offered |
| Frontend | `DesignOption` / `OptionSet` types, `OptionCard` in `ChatMessage.tsx` (title, trade-off, verified values, warnings, Preview / Apply); the first workable option is shown as the ghost automatically (`page.tsx`) |

### 12.2 Files

`backend/app/agent/plans.py`, `runner.py`, `tools.py`, `prompt.py`, `backend/app/cad_service.py`, `messages.py`,
`routes.py`, `backend/app/agent/test_options.py` (new, 6 tests), `frontend/src/types/cad.ts`, `types/chat.ts`,
`components/chat/ChatMessage.tsx`, `app/page.tsx`, `README.md`. New demo part: `demo/5_payload_adapter.step` / `.FCStd`.

### 12.3 Verification

| Check | Result |
|---|---|
| Backend unit tests | **62 passed** (6 new in `test_options.py`: open-ended detection, option-pick parsing, unsent tools, tool always offered, reply text, give-up text) |
| Engine suite | Not re-run for this change (no engine code changed); 383/383 at the end of Phase 4 |
| Frontend typecheck / lint / build | Not re-run on 2026-10-26: run `npx tsc --noEmit && npm run lint && npm run build` before committing |
| LLM end to end | Not recorded; try "make the nose end smoother" on `demo/5_payload_adapter.step` |

### 12.4 Known limitations

- Detection is a keyword regex: an open-ended request phrased without those words is handled as a normal edit, and
  any digit in the message (e.g. "make the R5 end smoother") turns options mode off.
- At most 4 options, dry-run one after another, so a turn with 4 multi-step options takes noticeably longer.
- Options are only valid at the version they were previewed on; after any other edit the user must ask again.

---

## 13. What's remaining

**Housekeeping**

1. Run the frontend checks, then commit the Alternatives feature, `test_options.py` and the payload-adapter demo files.
2. Re-run the 11 LLM steps of the Phases 3–4 simulation that were skipped for the Groq quota (§11).
3. Check in a browser the UI added since Phase 1 (settings bar, ✓/⚠ chips, side-effect table, locks, option cards);
   it has never been checked visually.
4. Optional: remove the unused `CLERK_SECRET_KEY` from `backend/.env`; use a higher-limit LLM than the Groq free tier.

**Phase 5 — Viewer and UX** (not started): exact face picking (glTF or face map instead of STL), section plane,
measuring tool, 3D annotations, streaming chat replies, command palette and keyboard shortcuts.

**Phase 6 — Robustness, evaluation, deployment** (not started): evaluation suite (≥ 50 model + prompt cases with
expected measured results, LLM comparison), job queue / worker pool (today: one backend process and one FreeCAD,
sessions served one at a time), request limits and security, structured logging, CI running the engine suite on
headless FreeCAD, Docker Compose.

**Open limitations worth tackling** (details in §5, §7.6, §8.6, §9.3, §10.3, §12.4): free-form faces stay
unclassified; slots / pockets need vertical walls; pocket length / width aren't editable; no conical chamfers; fillet
chains aren't re-made on face moves; draft is a rotation, not a true draft op; mirror / pattern don't handle fillets or
chamfers; the drawing has only overall dimensions and hole callouts; the cost model uses illustrative prices.

---

## 14. STL mesh import and the nose-cone demo (2026-10-29)

Goal: demo the whole project on a launch-vehicle part supplied as an STL. STL / OBJ / PLY uploads were refused
("meshes cannot be edited"), so they are now rebuilt as exact, editable solids first.

### 14.1 What was built

| Piece | Implementation |
|---|---|
| `freecad_bridge/mesh_import.py` (new) | Each separate body of the mesh → exact solid. **Turned parts**: axis from the triangle normals (line-complex fit), half cross-section cut through the axis, split into lines and arcs, each refitted to the mesh vertices (exact on the surface), revolved; off-axis holes fitted as cylinders, snapped radial / axial, cut blind or through. **Other parts**: coplanar triangles merged into exact planes, cylindrical patches filled then cut / fused as exact cylinders. Report per body: exact faces, max deviation from the mesh, volume vs mesh |
| `engine.open_model` | `.stl / .obj / .ply` go through `mesh_import.rebuild`; result carries `mesh_import` |
| Backend | `cad_service`: STL/OBJ/PLY allowed, report stored in meta and `public_state.mesh_import`; `routes._mesh_summary` adds the rebuild summary to the upload message |
| Frontend | File picker and drop zone accept `.stl,.obj,.ply`; upload hints mention STL |
| Demo part | `demo/nosecone/`: `nosecone_assembly.stl` (nose cone + bulkhead, 34,238 triangles), single-part STLs, STEP / FCStd originals and `make_nosecone.py` |

Result on the demo STL: 2 bodies → 12 exact faces each; tangent ogive arcs R 925.000 / R 923.000; all profile
corners at the design values; max deviation 0.005 mm; nose volume 194,801.63 mm³ vs 194,801.97 mm³ in the original
CAD; 4 radial Ø4 holes and 4 blind Ø3.3 tap-drill holes found, opposite holes exactly coaxial.

### 14.2 Bugs found by the simulation and fixed

| Found | Fix |
|---|---|
| Shrinking the hollow shoulder (`C1 Ø95.8`) cut an 8 mm groove into the ogive: the boss end was tested for air on the axis, which is inside the bore | `engine._boss_end_free`: sampled just inside the boss's radius |
| Lengthening the shoulder re-cut the Ø92 bore 8 mm past its end into the narrowing ogive cavity (wall 2 → 1.44 mm) | `engine._clear_beyond`: a through hole is overshot only where the space beyond is clear at its radius |
| An "alternative" for *make the tip blunter* replaced the whole nose cone with a cylinder | `plans._drastic`: alternatives that replace / delete a part or remove > 40 % of its volume are not offered |
| A cut that missed the part was recorded as a version that changed nothing | `MATERIAL_OPS` edits with identical geometry and no verified target are refused |
| LLM extra argument (`object=` on push_pull_face) crashed the op | `engine._unknown_params`: ignored and reported |
| LLM edge selector `["E_tip"]` crashed (`'str' has no attribute 'get'`); `operation: "add"` refused | `_edge_selectors` accepts bare IDs, clear error otherwise; add / subtract → fuse / cut |
| Requests over Groq's 8,000 TPM (8,049–8,381 tokens) failed the turn | `_fit_budget` also shortens older chat messages; a 413 is retried with only the tools in use; one more attempt |
| Bounding boxes of curved parts read from the display mesh (99.969 instead of 100) | `engine._exact_bbox` uses the exact geometry |
| Hole axes fitted 1e-6 off 45° made the analysis drop one hole of each coaxial pair | `mesh_import._snap_to_turned` |

### 14.3 Verification

| Check | Result |
|---|---|
| Engine suite | **401 / 401** (new: STL nose cone and fin import, shoulder Ø / length edits on the rebuilt nose cone, no-op refusal, unknown arguments, bare edge IDs, `add` operation) |
| Backend unit tests | **62 / 62** |
| Frontend | `tsc --noEmit`, `npm run lint`, `npm run build` pass |
| Simulation, deterministic steps 1–14 (upload, features, walls, mass/CG, interference, preview + plan, dimension edit, DFM, cost, BOM, drawing, version branch + compare, STEP export) | **14 / 14 pass**, every check against the design values |
| Simulation, LLM steps 15–18 | `gpt-oss-120b` (before its daily quota ran out): wall/shoulder question correct, the 4-hole edit passed (all at 15.000 mm), alternatives offered (exposed the replace bug). Final run on `gpt-oss-20b`: it moved holes the wrong way, missed H10 and misread the shoulder; the app executed and verified what it asked. Re-run on 120b when the quota resets |

Interactive report (3D view of every version, step log, drawing, fixes): https://claude.ai/artifact/LegT22aXz1DwK9tEiuFGBw

### 14.4 Known limitations

- Turned parts need ≥ 85 % of their area to be one surface of revolution; off-axis features other than cylinders
  (flats, slots on a tube) are not rebuilt on turned parts. The prismatic path rebuilds planes and cylinders only;
  fillets, cones and free-form patches stay as small triangular faces there.
- Mesh volume is inside the true surface, so the report's "volume vs mesh" is +0.0–0.2 % on curved parts.
- On Groq's free tier a multi-step LLM edit on this model takes 3 minutes (one ~8k-token request per minute) and
  the daily quota covers roughly one full simulation.

---

## 15. Rocket-nozzle STL and forced design changes (2026-11-01)

Goal: show the project works on another launch-vehicle part and that structural and other changes forced on it
really change the geometry. New demo part `demo/nozzle/` (`make_nozzle.py`): a conical 15° convergent-divergent
nozzle (chamber Ø100, 30° converging cone, throat Ø40 between tangent arcs R30 / R10, exit Ø120, area ratio 9,
4 mm wall, flange Ø160 × 12 with 8 × Ø9 bolt holes on PCD 140, 2 × Ø3 radial pressure taps) and the aluminium
thrust-mount ring it bolts to (8 matching holes, 6 × Ø11 attachment holes on PCD 180). `nozzle_assembly.stl` has
39,672 triangles.

### 15.1 Forced changes (simulation steps 5–12, each checked against a hand calculation)

| Change | Result |
|---|---|
| Flange 12 → 16 mm | +41,745.5 mm³ = (π(80²−54²) − 8·π·4.5²) × 4; bolt holes stay through, now 16 deep |
| M8 → M10 bolts, 16 holes on both parts (one previewed plan) | all Ø11 (ISO 273 normal), −4,021.2 / −2,513.3 mm³, holes still aligned |
| 8 → 12 bolts on both parts | 30° pitch on PCD 140, nozzle and ring holes coincide, attachment circle untouched |
| Flange OD 160 → 170 | +41,469.0 mm³ = π(85²−80²) × 16; edge distance 4.5 → 9.5 mm |
| Nozzle 20 mm longer | exit Ø 120 → 130.72, area ratio 9.00 → 10.68; added volume = the 15° walls continued |
| Pressure taps Ø3 → Ø4 | both still through |
| Chamber wall 4 → 6 mm (boss Ø108 → Ø112) | taps re-opened through the thicker wall (6.0 deep) |
| Throat radius R30 → R40 | refused (toroidal face); model unchanged |
| Mass / CG after all changes | masses follow the new volumes × density; still no interference |

### 15.2 Bugs found by the simulation and fixed

| Found | Fix |
|---|---|
| Throat rebuilt wrong from the mesh: R10 read as a line, R30 as R28.9 + a stray cone, tangent points rounded (92.947 → 93); max deviation 0.055 mm | `mesh_import._vertex_profile`: the profile is split on the mesh vertices (on the true surface) with a tight tolerance; `_tangent_point` computes tangent junctions exactly and leaves them unrounded. Now 0.008 mm, volume −0.009 % |
| Exit plane rounded 240.958 → 241 | with exact corners only rounding noise is snapped. The nose-cone tip now reads the true Z 295.588 (was 295.6; tests updated) |
| 45° bolt holes snapped to X/Y 49.5 instead of 49.497 (PCD 140.0036) | `mesh_import._snap_to_turned`: axial holes snapped by radius and angle about the part axis |
| Lengthening the nozzle added a straight Ø120 tube; shortening would leave a sliver of wall | `engine._tapered_push`: a face bounded by cone rims continues the cone walls, built on the neighbours' own surfaces and merged by `_unify_same_domain` (one face per cone) |
| Growing the chamber boss buried the radial taps (through → 3.67 mm blind) | `engine._resize_boss`: holes opening on the boss are filled and re-cut whole through the new wall |
| Equal holes on two concentric bolt circles (ring after M10: Ø11 bolt and attachment holes) gave no pattern | `analysis.hole_patterns`: a group that fits no pattern is split into rings around its centre |
| A later hole edit silently corrupted the solid (−2,930 mm³, self-intersecting) | `engine._clean`: a `removeSplitter` result is kept only if valid with an unchanged volume; root cause fixed in `_resize_boss` above |

### 15.3 Verification

| Check | Result |
|---|---|
| Engine suite | **417 / 417** (new: nozzle rebuild arcs / tangent points / exit plane / polar bolt snap, nozzle 20 mm longer and shorter, chamber boss with taps, BOP-clean later edit, two concentric equal-hole bolt circles) |
| Backend unit tests | **62 / 62** |
| Simulation, deterministic steps 1–14 | **14 / 14 pass** (upload, features, mass/CG, wall + interference, 7 forced changes, refused throat edit, mass after changes, DFM / cost / BOM / drawing / STEP) |
| Simulation, LLM steps 15–16 (`gpt-oss-120b`) | **fail**: asked for throat and exit Ø it guessed from hole / boss sizes (no bore-profile measurement); the M12 request ran into the 8k TPM cap and ended as options with a wrong size. The first run's LLM edit exposed the silent-corruption bug above |

Interactive report (3D view of every version, change summary, step log, drawing): https://claude.ai/artifact/U8tzFyaeCZXc4VMKgHCzNb

### 15.4 Known limitations

- No structural analysis (stress, pressure load, bolt preload, buckling, thermal). "Structural" changes here are
  geometric, with mass / CG / wall thickness / edge distance / interference as the checks.
- No profile-level edits for turned parts: throat diameter, the torus blends (R30 / R10) and cone half-angles can't
  be changed. A boss diameter edit is local (it leaves a step at the next blend instead of offsetting the wall).
- No bore-profile measurement (throat / exit diameter), so the LLM can't answer those questions correctly.
- Groq free tier: models with many holes push requests past 8,000 TPM, and the LLM then degrades.
