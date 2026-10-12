"""Chat sessions (one CAD model + conversation each), owned by a Clerk user."""

import datetime
import uuid

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import cad_service as cad
from . import messages
from .auth import current_user
from .db import get_db

router = APIRouter()

_memory_sessions: dict[str, dict] = {}


class SessionCreate(BaseModel):
    title: str


class SessionUpdate(BaseModel):
    title: str


def _session_out(doc):
    # Whitelist fields: older session docs carry binary fields that aren't JSON-serializable.
    sid = str(doc["_id"])
    meta = cad.load(sid)
    created = doc.get("created_at")
    return {
        "id": sid,
        "title": doc.get("title", ""),
        "user_id": doc.get("user_id"),
        "created_at": created.isoformat() if isinstance(created, datetime.datetime) else created,
        "cad_filename": meta["filename"] if meta else None,
    }


async def find_owned(session_id: str, user_id: str):
    """The session document if it exists and belongs to user_id, else None."""
    db = get_db()
    if db is None:
        s = _memory_sessions.get(session_id)
        return s if s and s["user_id"] == user_id else None
    try:
        return await db.sessions.find_one({"_id": ObjectId(session_id), "user_id": user_id})
    except Exception:
        return None


async def require_owned(session_id: str, user_id: str):
    s = await find_owned(session_id, user_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    return s


@router.get("/sessions")
async def list_sessions(user: str = Depends(current_user)):
    db = get_db()
    if db is None:
        rows = [s for s in _memory_sessions.values() if s["user_id"] == user]
        rows.sort(key=lambda s: s["created_at"], reverse=True)
    else:
        rows = await db.sessions.find({"user_id": user}).sort("created_at", -1).to_list(length=200)
    return [_session_out(s) for s in rows]


@router.post("/sessions")
async def create_session(session: SessionCreate, user: str = Depends(current_user)):
    db = get_db()
    doc = {"title": session.title[:120], "user_id": user,
           "created_at": datetime.datetime.now(datetime.timezone.utc)}
    if db is None:
        doc["_id"] = uuid.uuid4().hex
        _memory_sessions[doc["_id"]] = doc
    else:
        doc["_id"] = (await db.sessions.insert_one(doc)).inserted_id
    return _session_out(doc)


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: str, session: SessionUpdate, user: str = Depends(current_user)):
    s = await require_owned(session_id, user)
    title = session.title[:120]
    db = get_db()
    if db is None:
        s["title"] = title
    else:
        await db.sessions.update_one({"_id": s["_id"]}, {"$set": {"title": title}})
    return {"id": session_id, "title": title}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user: str = Depends(current_user)):
    return _session_out(await require_owned(session_id, user))


@router.get("/sessions/{session_id}/messages")
async def get_messages(session_id: str, user: str = Depends(current_user)):
    await require_owned(session_id, user)
    return await messages.list_messages(session_id)
