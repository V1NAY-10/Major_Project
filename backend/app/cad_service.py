"""
CAD session service — owns per-session files, versions and the feature-ID registry,
and talks to FreeCAD through freecad_client.

Layout:  data/sessions/<session_id>/
           original.<ext>        uploaded file (never modified)
           work.FCStd            live document FreeCAD edits
           versions/vN.FCStd     snapshot after each committed turn (v0 = upload)
           versions/vN.stl       mesh for the web viewer
           exports/              files generated for download
           meta.json             filename, versions, head, registry, model info, and the session's
                                 settings (display unit, precision, tolerance), locks and datums
"""

import asyncio
import datetime
import json
import os
import re
import shutil
from pathlib import Path

from . import freecad_client as fc
from . import materials as mats
from . import precision, standards, units

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


def _info_path(session_id, v):
    return _dir(session_id) / "versions" / f"v{v}.json"


def _ensure_tree(meta):
    """Versions form a tree (Phase 3): each has a parent. Older sessions were a straight line."""
    for i, ver in enumerate(meta.get("versions") or []):
        ver.setdefault("parent", None if ver["v"] == 0 else meta["versions"][i - 1]["v"])
    return meta


def _version(meta, v):
    for ver in meta["versions"]:
        if ver["v"] == v:
            return ver
    raise CadError(f"There is no version v{v}", 404)


def _children(meta, v):
    return [ver for ver in meta["versions"] if ver.get("parent") == v]


def load(session_id):
    f = _dir(session_id) / "meta.json"
    if not f.exists():
        return None
    return _ensure_tree(json.loads(f.read_text(encoding="utf-8")))


def save(meta):
    d = _dir(meta["session_id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta), encoding="utf-8")
    os.replace(tmp, d / "meta.json")


def _set_info(meta, info):
    """Store new model info, naming standard hole sizes (ISO clearance, tap drill, counterbore…) and the thread
    annotations (from the session, which win, or from threaded hole features in an FCStd file)."""
    meta["info"] = standards.annotate(info)
    annotate_threads(meta)
    return meta["info"]


def annotate_threads(meta):
    info = meta.get("info") or {}
    threads = {t["hole"]: {**t} for t in info.get("threads") or []}
    threads.update({k: {**v, "hole": k} for k, v in (meta.get("threads") or {}).items()})
    for h in (info.get("features") or {}).get("hole", []) or []:
        t = threads.get(h["id"])
        if t:
            h["thread"] = {k: t[k] for k in ("spec", "depth", "source", "note") if t.get(k) is not None}
        else:
            h.pop("thread", None)


def set_thread(meta, hole, size, depth=None):
    """Record (or with size 'none' remove) a thread annotation on a hole. Returns the annotation or None.
    The geometry is not changed: the thread is metadata of the session (STEP has no thread data FreeCAD can write)."""
    hid = str(hole).strip().upper()
    holes = {h["id"]: h for h in ((meta.get("info") or {}).get("features") or {}).get("hole", [])}
    if hid not in holes:
        raise CadError(f"{hole} is not a hole of the current model")
    threads = dict(meta.get("threads") or {})
    if str(size).strip().lower() in ("none", "remove", "no", ""):
        threads.pop(hid, None)
        meta["threads"] = threads
        annotate_threads(meta)
        save(meta)
        return None
    try:
        d, pitch = standards.parse_thread(size)
        tap, pitch, series = standards.tap_drill(d, pitch)
    except ValueError as e:
        raise CadError(str(e))
    h = holes[hid]
    hole_depth = h.get("depth") or 0
    dep = float(depth) if depth is not None else (None if h.get("through") else hole_depth)
    note = None
    if abs(h["diameter"] - tap) > 0.15:
        note = f"{hid} is Ø{h['diameter']:g}; the M{d:g}x{pitch:g} tap drill is Ø{tap:g} (resize it with set_hole_standard type tap)"
    if dep is not None and hole_depth and dep > hole_depth + 1e-6:
        raise CadError(f"A {dep:g} mm thread doesn't fit in {hid}, which is {hole_depth:g} mm deep")
    th = {"spec": f"M{d:g}x{pitch:g}", "depth": dep, "source": "session", **({"note": note} if note else {})}
    threads[hid] = th
    meta["threads"] = threads
    annotate_threads(meta)
    save(meta)
    return {"hole": hid, **th}


def public_state(meta):
    if meta is None:
        return None
    head = meta["head"]
    return {
        "settings": precision.settings_of(meta),
        "locks": meta.get("locks") or [],
        "datums": meta.get("datums") or {},
        "threads": meta.get("threads") or {},
        "session_id": meta["session_id"],
        "filename": meta["filename"],
        "version": head,
        "versions": meta["versions"],
        "can_undo": _version(meta, head).get("parent") is not None,
        "can_redo": bool(_children(meta, head)),
        "materials": meta.get("materials") or {},
        "pending": {k: v for k, v in (meta.get("pending") or {}).items() if k != "records_full"} or None,
        # Alternatives waiting for the user's pick; stale once the model has moved to another version.
        "options": ({"id": meta["options"]["id"], "base_version": meta["options"]["base_version"]}
                    if (meta.get("options") or {}).get("base_version") == head else None),
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
        old = load(session_id) if d.exists() else None
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
            "versions": [{"v": 0, "description": f"Uploaded {filename}", "created_at": _now(), "parent": None}],
            "registry": res["registry"],
            "info": res["info"],
            "dirty": False,
            # display settings carry over to a new upload; locks and datums refer to the old model's IDs
            "settings": (old or {}).get("settings") or {},
            "locks": [],
            "datums": {},
            "threads": {},
        }
        _set_info(meta, res["info"])
        _info_path(session_id, 0).write_text(json.dumps(meta["info"]), encoding="utf-8")
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

def preview_mesh_path(session_id):
    return _dir(session_id) / "preview.stl"


async def apply_op(meta, op, params, preview=False, force=False):
    """Run one engine operation with the session's tolerance, locks, datums and pre-edit checks. Returns the engine
    result; updates meta in place (not saved). preview: a dry run — the result mesh goes to preview.stl and the
    document, registry and info stay as they were."""
    settings = precision.settings_of(meta)
    checks = {"min_wall": settings.get("min_wall"), "force": bool(force)}
    res = await fc.call("apply", session_id=meta["session_id"], work_path=_p(work_path(meta["session_id"])),
                        op=op, params=params, registry=meta["registry"], tolerance=settings["tolerance"],
                        locks=meta.get("locks") or [], datums=meta.get("datums") or None, checks=checks,
                        preview_mesh=_p(preview_mesh_path(meta["session_id"])) if preview else None)
    if preview:
        res.pop("registry", None)
        res["info"] = standards.annotate(res["info"])
        return res
    meta["registry"] = res.pop("registry")
    res["info"] = _set_info(meta, res["info"])
    meta["dirty"] = True
    return res


_DATUM_METHODS = {"model_info", "find_features", "evaluate_locks"}


async def query(meta, method, **params):
    if method in _DATUM_METHODS and meta.get("datums"):
        params.setdefault("datums", meta["datums"])
    res = await fc.call(method, session_id=meta["session_id"], work_path=_p(work_path(meta["session_id"])),
                        registry=meta["registry"], **params)
    meta["registry"] = res.pop("registry", meta["registry"])
    return res


async def refresh_info(meta):
    res = await query(meta, "model_info")
    return _set_info(meta, res["info"])


async def sync_manual_edits(meta):
    """Re-analyse the document; if the user edited it by hand in FreeCAD, record that as a new version.

    Returns True when a manual edit was found. Callers must hold the session lock.
    """
    res = await query(meta, "model_info")
    _set_info(meta, res["info"])
    if res.get("manual_edit"):
        await commit_version(meta, "Manual edit in FreeCAD")
        return True
    return False


async def highlight(session_id, ids, select=True):
    """Meshes of the given features for the web viewer; also selects them in the FreeCAD GUI."""
    async with lock(session_id):
        meta = require(session_id)
        res = await query(meta, "highlight", ids=list(ids or [])[:50], select=bool(select))
        save(meta)
        return {"meshes": res.get("meshes", []), "missing": res.get("missing", [])}


async def pick(session_id, point, mode="feature"):
    """The component (feature or part ID) under a point clicked in the web viewer."""
    async with lock(session_id):
        meta = require(session_id)
        res = await query(meta, "pick", point=[float(x) for x in point][:3], mode=mode)
        save(meta)
        return res


async def wall_thickness(session_id, object=None, threshold=None):
    async with lock(session_id):
        meta = require(session_id)
        params = {k: v for k, v in (("object", object), ("threshold", threshold)) if v is not None}
        res = await query(meta, "wall_thickness", **params)
        save(meta)
        return {"parts": res.get("parts", [])}


async def gui_selection(session_id):
    """IDs of what is selected in the FreeCAD window."""
    async with lock(session_id):
        meta = require(session_id)
        res = await query(meta, "gui_selection")
        save(meta)
        return res


async def show_window(session_id):
    async with lock(session_id):
        require(session_id)
        return await fc.call("show_window", session_id=session_id, work_path=_p(work_path(session_id)))


async def commit_version(meta, description, name=None):
    """Snapshot the working document as a new version, a child of the current one. Editing after an undo starts a
    new branch; the versions that were undone stay in the tree."""
    sid = meta["session_id"]
    new_v = max(v["v"] for v in meta["versions"]) + 1
    fcstd, stl = version_paths(sid, new_v)
    await fc.call("snapshot", session_id=sid, work_path=_p(work_path(sid)), fcstd_path=_p(fcstd), stl_path=_p(stl))
    meta["versions"].append({"v": new_v, "description": description[:300], "created_at": _now(), "parent": meta["head"],
                             **({"name": name[:60]} if name else {})})
    _info_path(sid, new_v).write_text(json.dumps(meta.get("info") or {}), encoding="utf-8")
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
    res = await fc.call("reload", session_id=sid, work_path=_p(work_path(sid)), registry=meta["registry"],
                        datums=meta.get("datums") or None)
    meta["registry"] = res["registry"]
    _set_info(meta, res["info"])
    meta["head"] = v
    meta["dirty"] = False
    save(meta)


async def discard_uncommitted(meta):
    """Roll the working document back to the head version (used when a turn fails midway)."""
    if meta.get("dirty"):
        await restore(meta, meta["head"])


async def undo(session_id):
    """Go back to the parent of the current version."""
    async with lock(session_id):
        meta = require(session_id)
        parent = _version(meta, meta["head"]).get("parent")
        if parent is None:
            raise CadError("Nothing to undo")
        meta.pop("pending", None)
        await restore(meta, parent)
        return public_state(meta)


async def redo(session_id):
    """Go forward to the newest child of the current version (the branch edited last)."""
    async with lock(session_id):
        meta = require(session_id)
        kids = _children(meta, meta["head"])
        if not kids:
            raise CadError("Nothing to redo")
        meta.pop("pending", None)
        await restore(meta, max(kids, key=lambda k: k["v"])["v"])
        return public_state(meta)


async def checkout(session_id, v):
    """Make any version of the tree the current one (the next edit branches from it)."""
    async with lock(session_id):
        meta = require(session_id)
        _version(meta, v)
        meta.pop("pending", None)
        await restore(meta, v)
        return public_state(meta)


def rename_version(meta, v, name):
    ver = _version(meta, v)
    if name:
        ver["name"] = str(name)[:60]
    else:
        ver.pop("name", None)
    save(meta)
    return ver


async def compare(session_id, a, b):
    """Differences from version a to version b: every dimension / feature value that changed, and the material added
    (green) and removed (red) as meshes."""
    async with lock(session_id):
        meta = require(session_id)
        va, vb = _version(meta, a), _version(meta, b)
        infos = []
        for v in (a, b):
            f = _info_path(session_id, v)
            infos.append(json.loads(f.read_text(encoding="utf-8")) if f.exists() else None)
        dims = precision.side_effects(infos[0], infos[1], tolerance=precision.settings_of(meta)["tolerance"]) \
            if all(infos) else None
        geo = await fc.call("compare_versions", path_a=_p(version_paths(session_id, a)[0]),
                            path_b=_p(version_paths(session_id, b)[0]))
        return {"a": va, "b": vb, "dimensions": dims, "geometry": geo}


async def activate(session_id):
    """Bring the session's document to front in FreeCAD (re-opening it if needed)."""
    async with lock(session_id):
        meta = require(session_id)
        if not await sync_manual_edits(meta):
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


# ── Session settings, locks and datums ────────────────────────────────────

async def update_settings(session_id, display_unit=None, precision_digits=None, tolerance_length=None,
                          tolerance_angle=None, min_wall=None):
    """Display unit (mm | in), display precision (decimals), the acceptance tolerance of edits and the minimum wall
    thickness the pre-edit checks warn about (0 = off)."""
    async with lock(session_id):
        meta = require(session_id)
        s = precision.settings_of(meta)
        if display_unit is not None:
            if display_unit not in units.DISPLAY_UNITS:
                raise CadError(f"display_unit must be one of {', '.join(units.DISPLAY_UNITS)}")
            s["display_unit"] = display_unit
        if precision_digits is not None:
            if not 0 <= int(precision_digits) <= 6:
                raise CadError("precision must be 0–6 decimals")
            s["precision"] = int(precision_digits)
        for key, val in (("length", tolerance_length), ("angle", tolerance_angle)):
            if val is not None:
                if not 0 < float(val) <= 10:
                    raise CadError(f"tolerance_{key} must be > 0 and ≤ 10")
                s["tolerance"][key] = float(val)
        if min_wall is not None:
            if not 0 <= float(min_wall) <= 50:
                raise CadError("min_wall must be 0–50 mm (0 turns the check off)")
            s["min_wall"] = float(min_wall)
        meta["settings"] = s
        save(meta)
        return public_state(meta)


def _lock_key(lk):
    return (str(lk["target"]).upper(), lk.get("mode") or "value", (lk.get("axis") or "").upper())


def lock_label(lk, settings):
    if lk.get("mode") == "centered":
        return f"{lk['target']} centred" + (f" along {lk['axis']}" if lk.get("axis") else "")
    v = lk.get("value")
    if isinstance(v, (int, float)):
        return f"{lk['target']} = " + units.fmt_value(v, "angle" if lk.get("unit") == "deg" else "length",
                                                      settings["display_unit"], settings["precision"])
    return f"{lk['target']} (all its sizes and position)"


async def add_lock(meta, target, mode="value", axis=None):
    """Lock a dimension, feature, pattern or part at its current value. Callers hold the session lock."""
    mode = "centered" if str(mode or "value").lower() in ("centered", "centred", "center", "centre") else "value"
    axis = str(axis).strip().upper() if axis else None
    if axis and axis not in ("X", "Y", "Z"):
        raise CadError("axis must be X, Y or Z")
    req = {"target": str(target).strip().upper().replace("<", "∠"), "mode": mode, **({"axis": axis} if axis else {})}
    res = await query(meta, "evaluate_locks", locks=[req])
    lk = res["locks"][0]
    if lk.get("value") is None:
        raise CadError(f"'{target}' is not a dimension, feature, pattern or part of the current model "
                       "(dimension IDs look like P5-P6, H3-P5, P3∠P7, C1.Ø)")
    if mode == "centered" and not lk["value"]:
        raise CadError(f"{target} has no position that can be kept centred")
    lk["label"] = lock_label(lk, precision.settings_of(meta))
    lk["created_at"] = _now()
    locks = [x for x in meta.get("locks") or [] if _lock_key(x) != _lock_key(lk)]
    meta["locks"] = locks + [lk]
    save(meta)
    return lk


def remove_lock(meta, target, mode=None):
    """Remove locks on a target ('all' clears every lock). Returns the removed locks."""
    t = str(target).strip().upper().replace("<", "∠")
    keep, removed = [], []
    for lk in meta.get("locks") or []:
        hit = t == "ALL" or (str(lk["target"]).upper() == t and (mode is None or (lk.get("mode") or "value") == mode))
        (removed if hit else keep).append(lk)
    if not removed:
        raise CadError(f"No lock on '{target}'. Locks: " + (", ".join(lk["label"] for lk in keep) or "none"))
    meta["locks"] = keep
    save(meta)
    return removed


async def set_datums(meta, mapping):
    """Choose datum faces {A, B, C} (plane IDs; null/'default' resets one). Re-analyses to update positions."""
    datums = dict(meta.get("datums") or {})
    feats = {f["id"]: f for f in ((meta.get("info") or {}).get("features") or {}).get("plane", [])}
    for letter, fid in (mapping or {}).items():
        letter = str(letter).strip().upper()
        if letter not in ("A", "B", "C"):
            raise CadError("Datums are A, B and C")
        if fid in (None, "", "default"):
            datums.pop(letter, None)
            continue
        fid = str(fid).strip().upper()
        if fid not in feats or feats[fid].get("is_cap"):
            raise CadError(f"Datum {letter} must be a planar face ID (P#); '{fid}' is not one")
        datums[letter] = fid
    meta["datums"] = datums
    await refresh_info(meta)
    save(meta)
    return meta["info"].get("datums") or {}


async def with_session(session_id, fn, *args, **kwargs):
    """Run fn(meta, …) (sync or async) under the session lock and return the public state."""
    async with lock(session_id):
        meta = require(session_id)
        out = fn(meta, *args, **kwargs)
        if asyncio.iscoroutine(out):
            await out
        return public_state(meta)


# ── materials ─────────────────────────────────────────────────────────────

def set_material(meta, part, material):
    """Assign a material (library name or alias) to a part ID, or to every part with part 'all'."""
    try:
        key = mats.resolve(material)
    except ValueError as e:
        raise CadError(str(e))
    parts = [o.get("id") for o in (meta.get("info") or {}).get("objects", []) if o.get("id")]
    target = str(part or "all").strip().upper()
    if target != "ALL" and target not in parts:
        raise CadError(f"No part {part}. Parts: {', '.join(parts)}")
    m = dict(meta.get("materials") or {})
    for pid in (parts if target == "ALL" else [target]):
        m[pid] = key
    meta["materials"] = m
    save(meta)
    return {pid: mats.MATERIALS[k][0] for pid, k in m.items() if pid in parts}


def densities(meta):
    return {pid: mats.density(k) for pid, k in (meta.get("materials") or {}).items() if k in mats.MATERIALS}
