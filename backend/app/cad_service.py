"""
CAD session service — owns per-session files, versions and the feature-ID registry,
and talks to FreeCAD through freecad_client.

Layout:  data/sessions/<session_id>/
           original.<ext>        uploaded file (never modified)
           work.FCStd            live document FreeCAD edits
           versions/vN.FCStd     snapshot after each committed turn (v0 = upload)
           versions/vN.stl       mesh for the web viewer
           exports/              files generated for download
           meta.json             filename, versions, head, registry, model info
"""

import asyncio
import datetime
import json
import os
import re
import shutil
from pathlib import Path

from . import freecad_client as fc

DATA_DIR = Path(os.getenv("AETHERCAD_DATA", Path(__file__).resolve().parent.parent / "data")) / "sessions"
ALLOWED_EXTENSIONS = {"step", "stp", "iges", "igs", "brep", "brp", "fcstd"}
EXPORT_FORMATS = {"step": "step", "stp": "step", "iges": "igs", "igs": "igs", "stl": "stl",
                  "fcstd": "FCStd", "brep": "brep"}

_locks: dict[str, asyncio.Lock] = {}


class CadError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def lock(session_id: str) -> asyncio.Lock:
    return _locks.setdefault(session_id, asyncio.Lock())


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _dir(session_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id or ""):
        raise CadError("Invalid session id")
    return DATA_DIR / session_id


def _p(path: Path) -> str:
    return str(path).replace("\\", "/")


def work_path(session_id):
    return _dir(session_id) / "work.FCStd"


def version_paths(session_id, v):
    d = _dir(session_id) / "versions"
    return d / f"v{v}.FCStd", d / f"v{v}.stl"


def load(session_id):
    f = _dir(session_id) / "meta.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def save(meta):
    d = _dir(meta["session_id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, d / "meta.json")


def public_state(meta):
    if meta is None:
        return None
    head = meta["head"]
    return {
        "session_id": meta["session_id"],
        "filename": meta["filename"],
        "version": head,
        "versions": [v for v in meta["versions"] if v["v"] <= head],
        "can_undo": head > 0,
        "can_redo": head < len(meta["versions"]) - 1,
        "dirty": meta.get("dirty", False),
        "info": meta["info"],
        "mesh_url": f"/cad/model/{meta['session_id']}?v={head}",
    }


# ── Upload ──────────────────────────────────────────────────────────────────

async def upload(session_id: str, filename: str, content: bytes):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise CadError(f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    if not content:
        raise CadError("Uploaded file is empty")
    d = _dir(session_id)
    async with lock(session_id):
        if d.exists():
            await _safe_close(session_id)
            shutil.rmtree(d, ignore_errors=True)
        (d / "versions").mkdir(parents=True, exist_ok=True)
        src = d / f"original.{'FCStd' if ext == 'fcstd' else ext}"
        src.write_bytes(content)
        try:
            res = await fc.call("open_model", session_id=session_id, source_path=_p(src),
                                work_path=_p(work_path(session_id)))
        except fc.FreeCADError as e:
            raise CadError(f"FreeCAD could not open the file: {e}", 422)
        fcstd, stl = version_paths(session_id, 0)
        await fc.call("snapshot", session_id=session_id, work_path=_p(work_path(session_id)),
                      fcstd_path=_p(fcstd), stl_path=_p(stl))
        meta = {
            "session_id": session_id,
            "filename": filename,
            "ext": ext,
            "created_at": _now(),
            "head": 0,
            "versions": [{"v": 0, "description": f"Uploaded {filename}", "created_at": _now()}],
            "registry": res["registry"],
            "info": res["info"],
            "dirty": False,
        }
        save(meta)
        return public_state(meta)


async def _safe_close(session_id):
    try:
        await fc.call("close_session", session_id=session_id, work_path=_p(work_path(session_id)))
    except Exception:
        pass


def require(session_id):
    meta = load(session_id)
    if meta is None:
        raise CadError("No CAD model in this session. Upload a file first.", 404)
    return meta


# ── Engine calls (callers must hold the session lock) ─────────────────────

async def apply_op(meta, op, params):
    """Run one engine operation. Returns the engine result; updates meta in place (not saved)."""
    res = await fc.call("apply", session_id=meta["session_id"], work_path=_p(work_path(meta["session_id"])),
                        op=op, params=params, registry=meta["registry"])
    meta["registry"] = res.pop("registry")
    meta["info"] = res["info"]
    meta["dirty"] = True
    return res


async def query(meta, method, **params):
    res = await fc.call(method, session_id=meta["session_id"], work_path=_p(work_path(meta["session_id"])),
                        registry=meta["registry"], **params)
    meta["registry"] = res.pop("registry", meta["registry"])
    return res


async def refresh_info(meta):
    res = await query(meta, "model_info")
    meta["info"] = res["info"]
    return res["info"]


async def commit_version(meta, description):
    """Snapshot the working document as a new version (drops any redo history)."""
    sid = meta["session_id"]
    new_v = meta["head"] + 1
    for old in meta["versions"][new_v:]:
        for p in version_paths(sid, old["v"]):
            p.unlink(missing_ok=True)
    meta["versions"] = meta["versions"][:new_v]
    fcstd, stl = version_paths(sid, new_v)
    await fc.call("snapshot", session_id=sid, work_path=_p(work_path(sid)), fcstd_path=_p(fcstd), stl_path=_p(stl))
    meta["versions"].append({"v": new_v, "description": description[:300], "created_at": _now()})
    meta["head"] = new_v
    meta["dirty"] = False
    save(meta)
    return new_v


async def restore(meta, v):
    """Restore version v into the working document and reload it in FreeCAD."""
    sid = meta["session_id"]
    fcstd, _ = version_paths(sid, v)
    if not fcstd.exists():
        raise CadError(f"Version {v} is missing on disk", 404)
    await _safe_close(sid)
    shutil.copyfile(fcstd, work_path(sid))
    res = await fc.call("reload", session_id=sid, work_path=_p(work_path(sid)), registry=meta["registry"])
    meta["registry"] = res["registry"]
    meta["info"] = res["info"]
    meta["head"] = v
    meta["dirty"] = False
    save(meta)


async def discard_uncommitted(meta):
    """Roll the working document back to the head version (used when a turn fails midway)."""
    if meta.get("dirty"):
        await restore(meta, meta["head"])


async def undo(session_id):
    async with lock(session_id):
        meta = require(session_id)
        if meta["head"] == 0:
            raise CadError("Nothing to undo")
        await restore(meta, meta["head"] - 1)
        return public_state(meta)


async def redo(session_id):
    async with lock(session_id):
        meta = require(session_id)
        if meta["head"] >= len(meta["versions"]) - 1:
            raise CadError("Nothing to redo")
        await restore(meta, meta["head"] + 1)
        return public_state(meta)


async def activate(session_id):
    """Bring the session's document to front in FreeCAD (re-opening it if needed)."""
    async with lock(session_id):
        meta = require(session_id)
        await refresh_info(meta)
        save(meta)
        return public_state(meta)


def mesh_path(session_id, v=None):
    meta = require(session_id)
    v = meta["head"] if v is None else v
    _, stl = version_paths(session_id, v)
    if not stl.exists():
        raise CadError("Mesh not found for that version", 404)
    return stl


async def export(session_id, fmt):
    fmt = fmt.lower()
    if fmt not in EXPORT_FORMATS:
        raise CadError(f"Unsupported format '{fmt}'. Use one of: step, iges, stl, fcstd, brep")
    async with lock(session_id):
        meta = require(session_id)
        stem = Path(meta["filename"]).stem
        out = _dir(session_id) / "exports" / f"{stem}_v{meta['head']}.{EXPORT_FORMATS[fmt]}"
        await fc.call("export", session_id=session_id, work_path=_p(work_path(session_id)),
                      fmt=fmt, path=_p(out))
        return out
