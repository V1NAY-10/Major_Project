# AetherCAD — Project Status

_Last updated: 2026-10-13_

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
| Automated tests | ✅ 181/181 engine checks pass; 9/9 fast-path tests pass |
| Documentation | ✅ README.md and docs/ARCHITECTURE.md updated |
| Git commit | ⏳ Not committed yet |

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
| Engine test suite: `freecadcmd backend/tests/test_engine.py` | **181 passed, 0 failed** (~9 s) |
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
4. **Commit the work** once you're happy with it (nothing is committed yet).

---

## 5. Known limitations

- Moving a face leaves a fillet along its edge in its old position.
- Pushing a face past the top of a boss standing on it leaves a pocket above the boss.
- Grid hole patterns can't change their hole count; changing their spacing works.
- Free-form (non-analytic) faces stay unclassified and are reported as `unrecognized_faces`.
- Slots and pockets aren't single features, but their widths and depths appear as `gap` and `thickness` dimensions.
- Single backend process and single FreeCAD instance: sessions are served one at a time.

## 6. Possible next steps

- Preview an edit before applying it.
- More operations: shell, mirror, slot and pocket edits, and changing a grid's hole count.
- Run the engine test suite in CI on headless FreeCAD.
