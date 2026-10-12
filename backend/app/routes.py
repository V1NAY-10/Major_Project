"""HTTP API for CAD sessions and chat."""

from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import cad_service as cad
from . import freecad_client as fc
from . import messages
from .agent import runner
from .auth import current_user
from .sessions import require_owned

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

async def owned(session_id: str, user: str = Depends(current_user)) -> str:
    """Dependency for routes with a {session_id} path parameter: 404 unless the user owns it."""
    await require_owned(session_id, user)
    return session_id


@router.post("/cad/upload")
async def upload(file: UploadFile = File(...), session_id: str = Form(...), user: str = Depends(current_user)):
    await require_owned(session_id, user)
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


@router.post("/chat")
async def chat(body: ChatRequest, user: str = Depends(current_user)):
    await require_owned(body.session_id, user)
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")
    history = await messages.history_for_llm(body.session_id)
    await messages.add(body.session_id, "user", text)
    try:
        result = await runner.run_turn(body.session_id, text, history)
    except Exception as e:
        if error_status(e) is not None:
            await messages.add(body.session_id, "assistant", f"⚠️ {e}", kind="error")
        raise
    version = result["state"]["version"] if result["state"] else None
    saved = await messages.add(body.session_id, "assistant", result["content"],
                               operations=result["operations"], version=version)
    return {"message": saved, "state": result["state"]}
