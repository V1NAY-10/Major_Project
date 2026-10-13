"""HTTP API for CAD sessions and chat."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import cad_service as cad
from . import freecad_client as fc
from . import messages
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
    mode: Literal["feature", "part"] = "feature"


@router.post("/cad/pick/{session_id}")
async def pick(body: PickRequest, session_id: str = Depends(owned)):
    """The feature (or part) under a point clicked in the 3D viewer: {id, feature, part, face, distance}."""
    if len(body.point) != 3:
        raise HTTPException(status_code=400, detail="point must be [x, y, z]")
    return await cad.pick(session_id, body.point, body.mode)


@router.get("/cad/freecad-selection/{session_id}")
async def freecad_selection(session_id: str = Depends(owned)):
    """IDs of the faces / objects currently selected in the FreeCAD window."""
    return await cad.gui_selection(session_id)


@router.post("/cad/show/{session_id}")
async def show(session_id: str = Depends(owned)):
    """Bring FreeCAD and this session's document to the front."""
    return await cad.show_window(session_id)


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
                               operations=result["operations"], version=version)
    return {"message": saved, "state": result["state"], "highlight": result.get("highlight", [])}
