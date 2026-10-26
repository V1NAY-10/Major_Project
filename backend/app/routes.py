"""HTTP API for CAD sessions and chat."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from fastapi.responses import Response

from . import analysis_service as analysis
from . import cad_service as cad
from . import freecad_client as fc
from . import materials as mats
from . import messages
from . import reports
from .agent import runner
from .sessions import require_session

router = APIRouter()


def error_status(e: Exception):
    """HTTP status for known domain errors, or None."""
    if isinstance(e, (cad.CadError, runner.LLMError)):
        return e.status
    if isinstance(e, fc.FreeCADUnavailable):
        return 503
    if isinstance(e, fc.FreeCADError):
        return 500
    return None


def install_error_handlers(app: FastAPI):
    async def handler(request: Request, e: Exception):
        detail = f"FreeCAD error: {e}" if isinstance(e, fc.FreeCADError) else str(e)
        return JSONResponse(status_code=error_status(e), content={"detail": detail})

    for exc in (cad.CadError, runner.LLMError, fc.FreeCADUnavailable, fc.FreeCADError):
        app.add_exception_handler(exc, handler)


# ── FreeCAD ─────────────────────────────────────────────────────────────────

@router.get("/freecad/status")
async def freecad_status():
    return await fc.status()


# ── CAD session ─────────────────────────────────────────────────────────────

async def owned(session_id: str) -> str:
    """Dependency for routes with a {session_id} path parameter: 404 unless the session exists."""
    await require_session(session_id)
    return session_id


@router.post("/cad/upload")
async def upload(file: UploadFile = File(...), session_id: str = Form(...)):
    await require_session(session_id)
    state = await cad.upload(session_id, file.filename or "model", await file.read())
    await messages.add(session_id, "assistant",
                       f"Loaded **{state['filename']}** in FreeCAD. {_upload_summary(state['info'])}\n\n"
                       "Ask me about it or tell me what to change.",
                       version=0, kind="upload")
    return state


def _upload_summary(info):
    info = info or {}
    parts = []
    size = (info.get("bbox") or {}).get("size")
    if size:
        parts.append(f"Overall size {size[0]} × {size[1]} × {size[2]} mm")
    counts = info.get("counts") or {}
    names = {"hole": "holes", "boss": "bosses", "round": "fillets/rounds", "plane": "planar faces"}
    found = [f"{counts[k]} {v}" for k, v in names.items() if counts.get(k)]
    if found:
        parts.append("detected " + ", ".join(found))
    pats = info.get("patterns") or []
    if pats:
        names = {"circular": "bolt circle", "grid": "grid", "linear": "row"}
        parts.append(f"{len(pats)} hole pattern{'s' if len(pats) > 1 else ''} ("
                     + ", ".join(f"{p['id']}: {p['count']}× Ø{p.get('hole_diameter')} "
                                 f"{names.get(p['type'], p['type'])}" for p in pats[:4]) + ")")
    dims = info.get("dimensions") or []
    if dims:
        parts.append(f"{len(dims)} editable dimensions")
    unknown = sum(o.get("unrecognized_faces", 0) for o in info.get("objects") or [])
    if unknown:
        parts.append(f"{unknown} free-form faces not classified")
    if info.get("is_parametric"):
        parts.append("parametric features available")
    return ("; ".join(parts) + ".") if parts else ""


@router.get("/cad/state/{session_id}")
async def get_state(session_id: str = Depends(owned)):
    return cad.public_state(cad.require(session_id))


@router.post("/cad/activate/{session_id}")
async def activate(session_id: str = Depends(owned)):
    return await cad.activate(session_id)


@router.post("/cad/undo/{session_id}")
async def undo(session_id: str = Depends(owned)):
    state = await cad.undo(session_id)
    await messages.add(session_id, "assistant", f"↶ Undone — back to version {state['version']}.",
                       version=state["version"], kind="undo")
    return state


@router.post("/cad/redo/{session_id}")
async def redo(session_id: str = Depends(owned)):
    state = await cad.redo(session_id)
    await messages.add(session_id, "assistant", f"↷ Redone — now at version {state['version']}.",
                       version=state["version"], kind="redo")
    return state


class HighlightRequest(BaseModel):
    ids: list[str] = []
    select: bool = True


@router.post("/cad/highlight/{session_id}")
async def highlight(body: HighlightRequest, session_id: str = Depends(owned)):
    """Feature meshes for the web viewer; also selects the features in FreeCAD."""
    return await cad.highlight(session_id, body.ids, body.select)


class PickRequest(BaseModel):
    point: list[float]
    mode: Literal["feature", "part", "edge"] = "feature"


@router.post("/cad/pick/{session_id}")
async def pick(body: PickRequest, session_id: str = Depends(owned)):
    """The feature (or part) under a point clicked in the 3D viewer: {id, feature, part, face, distance}."""
    if len(body.point) != 3:
        raise HTTPException(status_code=400, detail="point must be [x, y, z]")
    return await cad.pick(session_id, body.point, body.mode)


@router.get("/cad/wall-thickness/{session_id}")
async def wall_thickness(session_id: str = Depends(owned), object: Optional[str] = None,
                         threshold: Optional[float] = None):
    """Minimum wall thickness per part (ray casting), and the faces thinner than `threshold`."""
    return await cad.wall_thickness(session_id, object, threshold)


@router.get("/cad/freecad-selection/{session_id}")
async def freecad_selection(session_id: str = Depends(owned)):
    """IDs of the faces / objects currently selected in the FreeCAD window."""
    return await cad.gui_selection(session_id)


@router.post("/cad/show/{session_id}")
async def show(session_id: str = Depends(owned)):
    """Bring FreeCAD and this session's document to the front."""
    return await cad.show_window(session_id)


class SettingsRequest(BaseModel):
    display_unit: Optional[Literal["mm", "in"]] = None
    precision: Optional[int] = None
    tolerance_length: Optional[float] = None
    tolerance_angle: Optional[float] = None
    min_wall: Optional[float] = None


@router.put("/cad/settings/{session_id}")
async def settings(body: SettingsRequest, session_id: str = Depends(owned)):
    """Display unit and precision, and the acceptance tolerance of edits (internal storage stays mm)."""
    return await cad.update_settings(session_id, body.display_unit, body.precision, body.tolerance_length,
                                     body.tolerance_angle, body.min_wall)


class LockRequest(BaseModel):
    target: str
    mode: Literal["value", "centered"] = "value"
    axis: Optional[Literal["X", "Y", "Z"]] = None


@router.post("/cad/locks/{session_id}")
async def add_lock(body: LockRequest, session_id: str = Depends(owned)):
    """Lock a dimension / feature / pattern / part at its current value."""
    return await cad.with_session(session_id, cad.add_lock, body.target, body.mode, body.axis)


@router.post("/cad/locks/{session_id}/remove")
async def remove_lock(body: LockRequest, session_id: str = Depends(owned)):
    return await cad.with_session(session_id, cad.remove_lock, body.target, body.mode)


class DatumsRequest(BaseModel):
    A: Optional[str] = None
    B: Optional[str] = None
    C: Optional[str] = None


@router.put("/cad/datums/{session_id}")
async def datums(body: DatumsRequest, session_id: str = Depends(owned)):
    """Set datum faces (plane IDs); 'default' resets one. Fields left out are unchanged."""
    return await cad.with_session(session_id, cad.set_datums, body.model_dump(exclude_unset=True))


@router.get("/cad/model/{session_id}")
async def model_mesh(session_id: str = Depends(owned), v: Optional[int] = None):
    return FileResponse(cad.mesh_path(session_id, v), media_type="model/stl",
                        headers={"Cache-Control": "no-store"})


@router.get("/cad/download/{session_id}")
async def download(session_id: str = Depends(owned), format: str = "step"):
    path = await cad.export(session_id, format)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


# ── Chat ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str
    # IDs the user selected in the viewer (features H3, parts B2, patterns PAT1, dimensions P1-P2):
    # the request applies to them unless it says otherwise.
    selection: list[str] = []


@router.post("/chat")
async def chat(body: ChatRequest):
    await require_session(body.session_id)
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")
    history = await messages.history_for_llm(body.session_id)
    selection = [x.strip().upper() for x in body.selection if x and x.strip()][:20]
    await messages.add(body.session_id, "user", text, **({"selection": selection} if selection else {}))
    try:
        result = await runner.run_turn(body.session_id, text, history, selection)
    except Exception as e:
        if error_status(e) is not None:
            await messages.add(body.session_id, "assistant", f"⚠️ {e}", kind="error")
        raise
    version = result["state"]["version"] if result["state"] else None
    saved = await messages.add(body.session_id, "assistant", result["content"],
                               operations=result["operations"], version=version, plan=result.get("plan"),
                               choices=result.get("choices"), options=result.get("options"))
    return {"message": saved, "state": result["state"], "highlight": result.get("highlight", [])}


# ── Phase 3: previews and plans, version tree ───────────────────────────────

class PreviewRequest(BaseModel):
    tool: Optional[str] = None
    args: dict = {}
    steps: list[dict] = []
    summary: str = ""


@router.post("/cad/preview/{session_id}")
async def preview(body: PreviewRequest, session_id: str = Depends(owned)):
    """Dry-run one edit (tool + args) or several (steps); the result waits as the pending plan."""
    steps = body.steps or ([{"tool": body.tool, "args": body.args}] if body.tool else [])
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        pending = await runner.plans.preview(meta, steps, body.summary, source="api")
        text = runner.plans.describe(pending)
        msg = await messages.add(session_id, "assistant", text, plan=pending, version=meta["head"])
        return {"message": msg, "state": cad.public_state(meta)}


@router.post("/cad/plan/{session_id}/apply")
async def plan_apply(session_id: str = Depends(owned), plan_id: Optional[str] = None):
    """Apply the pending plan as one version (all steps or nothing)."""
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        content, records, ok = await runner.plans.apply(meta, plan_id)
        msg = await messages.add(session_id, "assistant", content, operations=records, version=meta["head"],
                                 **({} if ok else {"kind": "error"}))
        return {"message": msg, "state": cad.public_state(meta), "ok": ok}


@router.post("/cad/plan/{session_id}/cancel")
async def plan_cancel(session_id: str = Depends(owned)):
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        runner.plans.cancel(meta)
        msg = await messages.add(session_id, "assistant", "Cancelled the plan; nothing was changed.")
        return {"message": msg, "state": cad.public_state(meta)}


class OptionRequest(BaseModel):
    key: int
    set_id: Optional[str] = None


@router.post("/cad/options/{session_id}/apply")
async def option_apply(body: OptionRequest, session_id: str = Depends(owned)):
    """Apply the alternative the user picked (all its steps as one version, or nothing)."""
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        content, records, ok = await runner.plans.apply_option(meta, body.key, body.set_id)
        msg = await messages.add(session_id, "assistant", content, operations=records, version=meta["head"],
                                 **({} if ok else {"kind": "error"}))
        return {"message": msg, "state": cad.public_state(meta), "ok": ok}


@router.get("/cad/preview-mesh/{session_id}")
async def preview_mesh(session_id: str = Depends(owned), opt: Optional[int] = None):
    path = runner.plans.option_mesh_path(session_id, opt) if opt else cad.preview_mesh_path(session_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No preview")
    return FileResponse(path, media_type="model/stl", headers={"Cache-Control": "no-store"})


@router.post("/cad/checkout/{session_id}/{v}")
async def checkout(v: int, session_id: str = Depends(owned)):
    state = await cad.checkout(session_id, v)
    await messages.add(session_id, "assistant", f"Switched to version {v}. The next edit starts a branch from it.",
                       version=v, kind="redo")
    return state


class RenameRequest(BaseModel):
    name: str = ""


@router.patch("/cad/versions/{session_id}/{v}")
async def rename_version(v: int, body: RenameRequest, session_id: str = Depends(owned)):
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        cad.rename_version(meta, v, body.name.strip())
        return cad.public_state(meta)


@router.get("/cad/compare/{session_id}")
async def compare(a: int, b: int, session_id: str = Depends(owned)):
    """Version a → b: changed dimensions and the material added / removed (meshes)."""
    return await cad.compare(session_id, a, b)


# ── Phase 4: materials, mass, DFM, BOM, interference, drawings, cost ────────

@router.get("/cad/materials")
async def material_library():
    return mats.library()


class MaterialRequest(BaseModel):
    part: str = "all"
    material: str


@router.put("/cad/materials/{session_id}")
async def set_material(body: MaterialRequest, session_id: str = Depends(owned)):
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        cad.set_material(meta, body.part, body.material)
        return cad.public_state(meta)


async def _locked(session_id, fn, *args, **kwargs):
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        out = await fn(meta, *args, **kwargs)
        cad.save(meta)
        return out


@router.get("/cad/mass/{session_id}")
async def mass(session_id: str = Depends(owned)):
    return await _locked(session_id, analysis.mass)


@router.get("/cad/dfm/{session_id}")
async def dfm(session_id: str = Depends(owned), process: str = "cnc"):
    return await _locked(session_id, analysis.dfm, process)


class ToolRequest(BaseModel):
    tool: str
    args: dict = {}
    label: Optional[str] = None


@router.post("/cad/run-tool/{session_id}")
async def run_tool(body: ToolRequest, session_id: str = Depends(owned)):
    """Apply one tool call as its own version (one-click DFM fixes, quick actions)."""
    result = await runner.run_tool(session_id, body.tool, body.args, body.label)
    msg = await messages.add(session_id, "assistant", (f"**{body.label}**\n" if body.label else "") + result["content"],
                             operations=result["operations"], version=result["state"]["version"])
    return {"message": msg, "state": result["state"], "highlight": result["highlight"]}


@router.get("/cad/bom/{session_id}")
async def bom(session_id: str = Depends(owned), format: Literal["json", "csv", "xlsx"] = "json"):
    data = await _locked(session_id, analysis.bom)
    if format == "json":
        return data
    stem = (cad.load(session_id) or {}).get("filename", "model").rsplit(".", 1)[0]
    if format == "csv":
        return Response(reports.bom_csv(data["rows"]), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{stem}_BOM.csv"'})
    return Response(reports.bom_xlsx(data["rows"]),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{stem}_BOM.xlsx"'})


@router.get("/cad/interference/{session_id}")
async def interference(session_id: str = Depends(owned), clearance: float = 1.0):
    return await _locked(session_id, analysis.interference, clearance)


@router.get("/cad/part-meshes/{session_id}")
async def part_meshes(session_id: str = Depends(owned)):
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        res = await cad.query(meta, "part_meshes")
        cad.save(meta)
        return {"parts": res["parts"]}


@router.post("/cad/drawing/{session_id}")
async def make_drawing(session_id: str = Depends(owned), part: Optional[str] = None):
    return await _locked(session_id, analysis.drawing, part)


@router.get("/cad/drawing/{session_id}")
async def get_drawing(session_id: str = Depends(owned), format: Literal["svg", "pdf"] = "pdf"):
    async with cad.lock(session_id):
        meta = cad.require(session_id)
        svg, pdf = analysis.drawing_paths(meta)
        if not (svg.exists() and pdf.exists()):
            await analysis.drawing(meta)
        path = pdf if format == "pdf" else svg
        return FileResponse(path, filename=path.name, media_type="application/pdf" if format == "pdf" else "image/svg+xml")


@router.get("/cad/cost/{session_id}")
async def cost(session_id: str = Depends(owned), process: Literal["cnc", "print3d"] = "cnc", quantity: int = 1):
    return await _locked(session_id, analysis.cost, process, quantity)
