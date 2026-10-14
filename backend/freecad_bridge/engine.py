"""
CAD engine — runs INSIDE FreeCAD (GUI or freecadcmd).

Every public function in METHODS is callable over the RPC bridge (see server.py).
The engine is stateless apart from a session -> document map; the feature-ID
registry is owned by the backend and passed in on every call, so IDs survive a
FreeCAD restart.

Design rules
  * Geometry edits are typed operations with exact numbers (no LLM code by default).
  * Each operation runs inside a document transaction: on any failure it is aborted.
  * After each operation the shape is re-analysed and the result is *measured*
    (e.g. the new hole diameter) so the caller gets verified values back.
  * Feature IDs (H#, C#, R#, P#, K#, S#, F#) are matched geometrically across edits.
  * Results are checked against a tolerance and against the session's locks *inside* the transaction, so an
    out-of-tolerance or lock-breaking edit is aborted and the document is left unchanged.
"""

import contextlib
import copy
import io
import math
import os
import re
import traceback

import FreeCAD as App  # type: ignore
import Part  # type: ignore

from freecad_bridge import analysis

GUI = bool(getattr(App, "GuiUp", 0))
if GUI:
    import FreeCADGui as Gui  # type: ignore
else:
    Gui = None

V = App.Vector


class EngineError(Exception):
    """An expected, user-facing failure (bad reference, invalid result, …)."""


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _r(x, n=4):
    return round(float(x), n) + 0.0  # + 0.0 turns -0.0 into 0.0


def _vl(v, n=4):
    return [_r(v.x, n), _r(v.y, n), _r(v.z, n)]


def _v(seq):
    if isinstance(seq, dict):
        return V(float(seq.get("x", 0)), float(seq.get("y", 0)), float(seq.get("z", 0)))
    if seq is None or len(seq) != 3:
        raise EngineError(f"Expected a 3D vector [x, y, z], got {seq!r}")
    return V(float(seq[0]), float(seq[1]), float(seq[2]))


def _axis_vec(spec):
    if isinstance(spec, str):
        s = spec.strip().upper()
        sign = -1.0 if s.startswith("-") else 1.0
        s = s.lstrip("+-")
        if s in ("X", "Y", "Z"):
            return V(*(sign if c == s else 0.0 for c in "XYZ"))
        raise EngineError(f"Unknown axis '{spec}'. Use X, Y, Z, -X, … or a vector.")
    v = _v(spec)
    if v.Length < 1e-12:
        raise EngineError("Direction vector has zero length")
    return v.normalize()


def _canon_axis(a):
    """Return a unit axis with a deterministic sign (first significant component positive)."""
    a = V(a).normalize()
    eps = 1e-9
    if a.z < -eps or (abs(a.z) <= eps and (a.y < -eps or (abs(a.y) <= eps and a.x < 0))):
        a = a.negative()
    return a


def _perp_base(point, axis):
    """Closest point on the axis line to the origin (unique description of the line)."""
    return point - axis * point.dot(axis)


def _num(x, name):
    try:
        return float(x)
    except Exception:
        raise EngineError(f"'{name}' must be a number, got {x!r}")


def _norm_path(p):
    return os.path.normcase(os.path.abspath(p)) if p else ""


def _save(doc):
    """Save and drop the timestamped *.FCBak backups FreeCAD writes next to the file."""
    doc.save()
    if GUI:
        _remember(doc)  # baseline for detecting manual edits made in the FreeCAD window
    try:
        folder, base = os.path.split(doc.FileName)
        stem = os.path.splitext(base)[0]
        for name in os.listdir(folder):
            if name.startswith(stem + ".") and name.endswith(".FCBak"):
                os.remove(os.path.join(folder, name))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Documents / sessions
# ─────────────────────────────────────────────────────────────────────────────

_DOCS = {}  # session_id -> doc.Name


def _find_doc(session_id, work_path=None):
    name = _DOCS.get(session_id)
    docs = App.listDocuments()
    if name and name in docs:
        return docs[name]
    if work_path:
        target = _norm_path(work_path)
        for d in docs.values():
            if d.FileName and _norm_path(d.FileName) == target:
                return d
    return None


def _activate(doc, fit=False):
    App.setActiveDocument(doc.Name)
    if not GUI:
        return
    try:
        gdoc = Gui.getDocument(doc.Name)
        try:
            from PySide import QtWidgets  # type: ignore  (FreeCAD's PySide shim)
        except Exception:
            QtWidgets = None
        if QtWidgets is not None:
            mdi = Gui.getMainWindow().findChild(QtWidgets.QMdiArea)
            if mdi is not None:
                for sw in mdi.subWindowList():
                    if sw.windowTitle().startswith(doc.Label):
                        mdi.setActiveSubWindow(sw)
                        break
        view = gdoc.activeView() if gdoc else None
        if fit and view is not None:
            try:
                view.viewIsometric()
            except Exception:
                pass
            Gui.SendMsgToActiveView("ViewFit")
        Gui.updateGui()
    except Exception as e:  # GUI niceties must never break an edit
        print(f"[AetherCAD] GUI activate warning: {e}")


def _get_doc(session_id, work_path):
    doc = _find_doc(session_id, work_path)
    if doc is None:
        if not work_path or not os.path.exists(work_path):
            raise EngineError("The session's FreeCAD document is not open and no saved copy exists. Re-upload the model.")
        doc = App.openDocument(work_path)
        if GUI:
            _remember(doc)
    _DOCS[session_id] = doc.Name
    try:
        doc.UndoMode = 1
    except Exception:
        pass
    _activate(doc)
    return doc


def _close_session(session_id, work_path=None):
    doc = _find_doc(session_id, work_path)
    if doc is not None:
        App.closeDocument(doc.Name)
    _DOCS.pop(session_id, None)


def _is_visible(obj):
    try:
        if not obj.Visibility:
            return False
    except Exception:
        pass
    parent = None
    try:
        parent = obj.getParentGeoFeatureGroup()
    except Exception:
        pass
    if parent is not None and parent.TypeId == "App::Part":
        return _is_visible(parent)
    return True


# Grouping objects expose the combined shape of their children; analysing them too would
# list every feature twice (once per child, once per container).
CONTAINER_TYPES = {"App::Part", "App::DocumentObjectGroup", "App::Origin", "App::Link", "App::LinkGroup"}


def _solid_objects(doc):
    """Visible objects that carry final solid geometry (one entry per body/part)."""
    out = []
    for o in doc.Objects:
        if o.TypeId.startswith("Sketcher::") or o.TypeId in CONTAINER_TYPES or not hasattr(o, "Shape"):
            continue
        try:
            parent = o.getParentGeoFeatureGroup()
        except Exception:
            parent = None
        if parent is not None and parent.TypeId == "PartDesign::Body":
            continue  # the Body itself represents its features
        if not _is_visible(o):
            continue
        try:
            s = o.Shape
        except Exception:
            continue
        if s.isNull() or not s.Solids:
            continue
        out.append(o)
    return out


def _parent_placement(obj):
    try:
        parent = obj.getParentGeoFeatureGroup()
    except Exception:
        parent = None
    if parent is None:
        return None
    return parent.getGlobalPlacement()


def _gshape(obj):
    """Shape of obj in global coordinates."""
    s = obj.Shape.copy()
    pp = _parent_placement(obj)
    if pp is not None:
        s.Placement = pp.multiply(s.Placement)
    return s


def _get_obj(doc, ref, need_solid=False):
    if ref:
        o = doc.getObject(ref)
        if o is None and str(ref).strip().upper() in _PARTS.get(doc.Name, {}):
            o = doc.getObject(_PARTS[doc.Name][str(ref).strip().upper()])
        if o is None:
            matches = [x for x in doc.Objects if x.Label.lower() == str(ref).lower()]
            o = matches[0] if matches else None
        if o is None:
            raise EngineError(f"No object named '{ref}'. Objects: {', '.join(x.Name for x in doc.Objects[:40])}")
        return o
    solids = _solid_objects(doc)
    if len(solids) == 1:
        return solids[0]
    if not solids:
        raise EngineError("The document has no visible solid object")
    raise EngineError("Several solid objects exist; specify 'object'. Candidates: "
                      + ", ".join(f"{o.Name} ('{o.Label}')" for o in solids[:20]))


# ─────────────────────────────────────────────────────────────────────────────
# Feature analysis
# ─────────────────────────────────────────────────────────────────────────────

# "face" = a free-form face no rule recognises; it still gets an ID so every face of a part is addressable.
KIND_PREFIX = {"hole": "H", "boss": "C", "round": "R", "plane": "P", "cone": "K", "sphere": "S", "face": "F",
               "slot": "SL", "pocket": "PK", "chamfer": "CH"}
EDGE_PREFIX = "E"  # edges get IDs on demand (listing, picking, use), see _edge_table
PART_PREFIX = "B"  # whole solid objects (bodies / parts): B1, B2…


def _tolerances(shape):
    diag = shape.BoundBox.DiagonalLength or 1.0
    return diag, max(1e-3, diag * 1e-5)


def _inside_any(shape, pt, tol):
    for s in shape.Solids:
        try:
            if s.isInside(pt, tol, True):
                return True
        except Exception:
            pass
    return False


def _semantic_position(p, bb):
    labels = []
    for val, lo, hi, names in (
        (p.x, bb.XMin, bb.XMax, ("left(-X)", "right(+X)")),
        (p.y, bb.YMin, bb.YMax, ("front(-Y)", "back(+Y)")),
        (p.z, bb.ZMin, bb.ZMax, ("bottom(-Z)", "top(+Z)")),
    ):
        span = hi - lo
        if span <= 1e-9:
            continue
        f = (val - lo) / span
        if f < 0.25:
            labels.append(names[0])
        elif f > 0.75:
            labels.append(names[1])
    return " ".join(labels) or "central"


def _plane_label(n, c, bb, tol):
    for axis, comp, lo, hi, neg, pos in (
        ("Z", n.z, bb.ZMin, bb.ZMax, "bottom", "top"),
        ("X", n.x, bb.XMin, bb.XMax, "left", "right"),
        ("Y", n.y, bb.YMin, bb.YMax, "front", "back"),
    ):
        if abs(comp) > 0.9999:
            coord = getattr(c, axis.lower())
            if comp > 0 and abs(coord - hi) < tol * 10:
                return f"{pos} face (+{axis}, outermost)"
            if comp < 0 and abs(coord - lo) < tol * 10:
                return f"{neg} face (-{axis}, outermost)"
            return f"faces {'+' if comp > 0 else '-'}{axis} at {axis.lower()}={_r(coord, 3)}"
    return "inclined face"


def _point_normal(face, u, v):
    return face.valueAt(u, v), face.normalAt(u, v)


def _features_of_shape(objname, shape):
    """Detect features on one object's global shape. Returns a list of feature dicts.

    Keys starting with '_' are internal (face indices, vectors) and are stripped
    before anything leaves the engine.
    """
    diag, tol = _tolerances(shape)
    bb = shape.BoundBox
    feats = []
    cyl_groups = []   # [{concave, a, base, r, faces:[(i, t0, t1, span)]}]
    planes, cones = [], []

    unknown = 0
    for i, f in enumerate(shape.Faces):
        try:
            u0, u1, v0, v1 = f.ParameterRange
            um, vm = (u0 + u1) / 2.0, (v0 + v1) / 2.0
        except Exception:
            continue
        si = analysis.surface_info(f, tol)
        kind = si["type"]

        if kind == "cylinder":
            a = _canon_axis(si["axis"])
            c = si["center"]
            r = si["radius"]
            p, n = _point_normal(f, um, vm)
            w = p - c
            radial = w - a * w.dot(a)
            concave = n.dot(radial) < 0
            ts = [vx.Point.dot(a) for vx in f.Vertexes] or [
                V(x, y, z).dot(a) for x in (f.BoundBox.XMin, f.BoundBox.XMax)
                for y in (f.BoundBox.YMin, f.BoundBox.YMax) for z in (f.BoundBox.ZMin, f.BoundBox.ZMax)]
            base = _perp_base(c, a)
            entry = (i, min(ts), max(ts), analysis.angular_span(f, a, c, si))
            for g in cyl_groups:
                if (g["concave"] == concave and abs(g["r"] - r) < tol * (10 if si.get("fitted") else 1)
                        and abs(g["a"].dot(a)) > 1 - 1e-7
                        and (g["base"] - base).Length < tol * (10 if si.get("fitted") else 1)):
                    g["faces"].append(entry)
                    break
            else:
                cyl_groups.append({"concave": concave, "a": a, "base": base, "r": r, "faces": [entry]})

        elif kind == "plane":
            _, n = _point_normal(f, um, vm)
            c = f.CenterOfMass
            planes.append({"i": i, "n": n, "c": c, "area": f.Area, "d": n.dot(c), "bb": f.BoundBox})

        elif kind == "cone":
            a = _canon_axis(si["axis"])
            apex = si["apex"]
            ts = [vx.Point.dot(a) for vx in f.Vertexes]
            cbase = _perp_base(apex, a)
            radii = [(vx.Point - a * vx.Point.dot(a) - cbase).Length for vx in f.Vertexes]
            cones.append({"i": i, "a": a, "base": cbase, "apex": apex,
                          "semi": si["semi"], "t0": min(ts) if ts else 0.0,
                          "t1": max(ts) if ts else 0.0, "area": f.Area, "rmax": max(radii) if radii else 0.0})

        elif kind == "sphere":
            feats.append({"kind": "sphere", "_obj": objname, "_faces": [i], "radius": _r(si["radius"]),
                          "center": _vl(si["center"]), "_c": si["center"]})

        elif kind == "torus":
            a = _canon_axis(si["axis"])
            c = si["center"]
            feats.append({"kind": "round", "_obj": objname, "_faces": [i], "radius": _r(si["minor"]),
                          "torus_major_radius": _r(si["major"]), "axis": _vl(a), "center": _vl(c),
                          "_a": a, "_base": _perp_base(c, a), "_t": (c.dot(a),) * 2,
                          "_r": float(si["minor"])})
        else:
            unknown += 1
            c = f.CenterOfMass
            feats.append({"kind": "face", "_obj": objname, "_faces": [i], "_c": c, "center": _vl(c),
                          "area": _r(f.Area, 3), "surface": si["type"] or type(f.Surface).__name__,
                          "position": _semantic_position(c, bb)})

    used_cones, used_planes = set(), set()

    def _caps_at(t_end, a, base, r):
        """Planar / conical faces closing a cylinder at axial position t_end."""
        caps = []
        for pl in planes:
            if pl["i"] in used_planes or abs(abs(pl["n"].dot(a)) - 1) > 1e-6:
                continue
            if abs(pl["c"].dot(a) - t_end) > tol * 10:
                continue
            off = pl["c"] - a * pl["c"].dot(a) - base
            if off.Length > tol * 10 or pl["bb"].DiagonalLength > 2 * r * math.sqrt(2) + tol * 10:
                continue
            caps.append(("plane", pl["i"]))
        for cn in cones:
            if cn["i"] in used_cones or abs(cn["a"].dot(a)) < 1 - 1e-7 or (cn["base"] - base).Length > tol * 10:
                continue
            # Only a narrowing cone (drill tip) closes the cylinder; a widening one is a countersink/chamfer.
            if cn["rmax"] > r + tol * 10:
                continue
            if abs(cn["t0"] - t_end) < tol * 10 or abs(cn["t1"] - t_end) < tol * 10:
                caps.append(("cone", cn["i"]))
        return caps

    def _countersink_at(t_end, a, base, r):
        for cn in cones:
            if cn["i"] in used_cones or abs(cn["a"].dot(a)) < 1 - 1e-7 or (cn["base"] - base).Length > tol * 10:
                continue
            if cn["rmax"] > r + tol * 10 and (abs(cn["t0"] - t_end) < tol * 10 or abs(cn["t1"] - t_end) < tol * 10):
                return cn
        return None

    holes_here = []
    for g in cyl_groups:
        segs = sorted(g["faces"], key=lambda e: e[1])
        clusters = []
        for e in segs:
            if clusters and e[1] <= clusters[-1]["t1"] + tol * 10:
                cl = clusters[-1]
                cl["faces"].append(e[0])
                cl["t1"] = max(cl["t1"], e[2])
                cl["span"] += e[3]
            else:
                clusters.append({"faces": [e[0]], "t0": e[1], "t1": e[2], "span": e[3]})
        a, base, r = g["a"], g["base"], g["r"]
        for cl in clusters:
            full = cl["span"] >= 2 * math.pi * 0.98
            t0, t1 = cl["t0"], cl["t1"]
            mid = base + a * ((t0 + t1) / 2)
            common = {"_obj": objname, "_faces": list(cl["faces"]), "_a": a, "_base": base,
                      "_t": (t0, t1), "_r": r, "axis": _vl(a)}
            if not full:
                feats.append({"kind": "round", "radius": _r(r), "length": _r(t1 - t0),
                              "concave": g["concave"], "center": _vl(mid), **common})
                continue
            eps = max(tol * 20, 0.01)
            caps0, caps1 = _caps_at(t0, a, base, r), _caps_at(t1, a, base, r)
            if g["concave"]:
                open0 = not caps0 and not _inside_any(shape, base + a * (t0 - eps), tol)
                open1 = not caps1 and not _inside_any(shape, base + a * (t1 + eps), tol)
                through = open0 and open1
                caps = ([] if open0 else caps0) + ([] if open1 else caps1)
                for kind, idx in caps:
                    (used_planes if kind == "plane" else used_cones).add(idx)
                entry_t = t1 if (open1 or not open0) else t0
                if through:
                    direction = a.negative()
                else:
                    direction = a.negative() if entry_t == t1 else a
                tip = any(k == "cone" for k, _ in caps)
                hole = {
                    "kind": "hole", "diameter": _r(2 * r), "radius": _r(r), "depth": _r(t1 - t0),
                    "through": through, "drill_tip": tip,
                    "entry": _vl(base + a * entry_t), "direction": _vl(direction), "center": _vl(mid),
                    "position": _semantic_position(mid, bb),
                    "_caps": [idx for _, idx in caps], "_open": (open0, open1), **common}
                for t_end, is_open in ((t0, open0), (t1, open1)):
                    cs = _countersink_at(t_end, a, base, r) if is_open else None
                    if cs is not None:
                        used_cones.add(cs["i"])
                        outer_t = cs["t1"] if abs(cs["t0"] - t_end) < abs(cs["t1"] - t_end) else cs["t0"]
                        hole.setdefault("countersinks", []).append(
                            {"outer_diameter": _r(2 * cs["rmax"]), "angle_deg": _r(2 * math.degrees(cs["semi"]), 2),
                             "at": _vl(base + a * outer_t)})
                        hole.setdefault("_cs", []).append(
                            {"i": cs["i"], "R": cs["rmax"], "semi": cs["semi"], "outer_t": outer_t,
                             "inward": -1.0 if outer_t >= t1 - tol else 1.0})
                        # The hole starts at the top of its countersink, not where the cylinder wall begins.
                        if abs(entry_t - t_end) < tol * 10:
                            hole["entry"] = _vl(base + a * outer_t)
                if hole.get("_cs"):
                    ends = [t0, t1] + [cs["outer_t"] for cs in hole["_cs"]]
                    hole["depth"] = _r(max(ends) - min(ends))
                holes_here.append(hole)
                feats.append(hole)
            else:
                # A boss's end faces stay ordinary visible planes (unlike hole bottoms): its top face is a
                # real face users refer to, and for a plain cylinder body they are the part's own faces.
                caps = [idx for kind, idx in caps0 + caps1 if kind == "plane"]
                # The free end has air beyond it on the axis; the base end runs into the body.
                eps = max(tol * 20, 0.01)
                free1 = not _inside_any(shape, base + a * (t1 + eps), tol)
                free0 = not _inside_any(shape, base + a * (t0 - eps), tol)
                free_t = t1 if (free1 or not free0) else t0
                base_t = t0 if free_t == t1 else t1
                feats.append({
                    "kind": "boss", "diameter": _r(2 * r), "radius": _r(r), "length": _r(t1 - t0),
                    "center": _vl(mid), "free_end": _vl(base + a * free_t), "base_end": _vl(base + a * base_t),
                    "both_ends_free": free0 and free1,
                    "position": _semantic_position(mid, bb), "_caps": caps, "_free_t": free_t, **common})

    analysis.link_counterbores(holes_here, tol)

    for cn in cones:
        if cn["i"] in used_cones:
            continue
        mid_t = (cn["t0"] + cn["t1"]) / 2
        feats.append({"kind": "cone", "_obj": objname, "_faces": [cn["i"]], "_a": cn["a"], "_base": cn["base"],
                      "_t": (cn["t0"], cn["t1"]), "_r": 0.0, "axis": _vl(cn["a"]),
                      "half_angle_deg": _r(math.degrees(cn["semi"]), 3), "apex": _vl(cn["apex"]),
                      "center": _vl(cn["base"] + cn["a"] * mid_t), "length": _r(cn["t1"] - cn["t0"])})

    for pl in planes:
        n, c = pl["n"], pl["c"]
        feats.append({"kind": "plane", "_obj": objname, "_faces": [pl["i"]], "_n": n, "_c": c, "_d": pl["d"],
                      "normal": _vl(n), "center": _vl(c), "area": _r(pl["area"], 3),
                      "label": _plane_label(n, c, bb, tol), "is_cap": pl["i"] in used_planes,
                      "bbox": {"min": _vl(V(pl["bb"].XMin, pl["bb"].YMin, pl["bb"].ZMin), 3),
                               "max": _vl(V(pl["bb"].XMax, pl["bb"].YMax, pl["bb"].ZMax), 3)}})
    # Composite features take over the faces they are made of (each face keeps exactly one ID).
    adj = analysis.Adjacency(shape)
    feats = analysis.fillet_chains(shape, feats, tol, adj)
    feats = analysis.recesses(shape, feats, tol, adj, lambda p: _semantic_position(p, bb))
    feats = analysis.chamfers(shape, feats, tol, adj)
    for f in feats:
        if f["kind"] == "round" and f.get("torus_major_radius") is not None:
            f["toroidal"] = True
    return feats, unknown


def _descriptor(f):
    k = f["kind"]
    if k == "plane":
        return {"k": k, "n": _vl(f["_n"], 7), "d": f["_d"], "c": _vl(f["_c"], 5)}
    if k == "sphere":
        return {"k": k, "c": _vl(f["_c"], 5), "r": f["radius"]}
    if k == "face":
        return {"k": k, "c": _vl(f["_c"], 5), "area": f["area"]}
    return {"k": k, "a": _vl(f["_a"], 8), "b": _vl(f["_base"], 5), "t": list(f["_t"]), "r": f["_r"]}


def _match_score(desc, f, tol):
    if desc["k"] != f["kind"]:
        return None
    k = f["kind"]
    if k == "plane":
        n = _v(desc["n"])
        if n.dot(f["_n"]) < 1 - 1e-6 or abs(desc["d"] - f["_d"]) > tol * 10:
            return None
        return (_v(desc["c"]) - f["_c"]).Length
    if k == "sphere":
        dist = (_v(desc["c"]) - f["_c"]).Length
        return dist if dist < tol * 10 else None
    if k == "face":
        dist = (_v(desc["c"]) - f["_c"]).Length
        if dist > tol * 10 or abs(desc["area"] - f["area"]) > max(desc["area"] * 1e-3, tol):
            return None
        return dist
    a = _v(desc["a"])
    if abs(a.dot(f["_a"])) < 1 - 1e-6 or (_v(desc["b"]) - f["_base"]).Length > tol * 10:
        return None
    t0, t1 = desc["t"]
    if t1 < f["_t"][0] - tol * 10 or f["_t"][1] < t0 - tol * 10:
        return None
    return abs(desc["r"] - f["_r"]) + abs(t0 - f["_t"][0]) * 1e-3 + abs(t1 - f["_t"][1]) * 1e-3


def _assign_ids(feats, registry, tol):
    registry = registry or {}
    items = dict(registry.get("items", {}))
    nxt = dict(registry.get("next", {}))
    pairs = []
    for fi, f in enumerate(feats):
        for fid, desc in items.items():
            sc = _match_score(desc, f, tol)
            if sc is not None:
                pairs.append((sc, fi, fid))
    pairs.sort(key=lambda p: p[0])
    taken_f, taken_id = set(), set()
    for sc, fi, fid in pairs:
        if fi in taken_f or fid in taken_id:
            continue
        feats[fi]["id"] = fid
        taken_f.add(fi)
        taken_id.add(fid)
    # New features: deterministic order (by kind, then position)
    fresh = [f for i, f in enumerate(feats) if i not in taken_f]
    fresh.sort(key=lambda f: (f["kind"], -f.get("area", 0), f.get("center", [0, 0, 0])[2] * -1,
                              f.get("center", [0, 0, 0])[0], f.get("center", [0, 0, 0])[1]))
    for f in fresh:
        p = KIND_PREFIX[f["kind"]]
        n = int(nxt.get(p, 1))
        f["id"] = f"{p}{n}"
        nxt[p] = n + 1
    new_items = {f["id"]: _descriptor(f) for f in feats}
    return {"items": new_items, "next": nxt}


class _Analysis:
    """Result of analysing a document. Unpacks as (objs, feats, fmap, registry)."""

    def __init__(self, objs, feats, fmap, reg, shapes, tol, unknown):
        self.objs, self.feats, self.fmap, self.reg = objs, feats, fmap, reg
        self.shapes, self.tol, self.unknown = shapes, tol, unknown
        self.patterns = analysis.hole_patterns(feats, tol)
        self.parts = {}  # object name -> part ID
        self._dims = None
        self._edges = None  # edge rows, computed on demand

    def __iter__(self):
        return iter((self.objs, self.feats, self.fmap, self.reg))

    @property
    def dims(self):
        if self._dims is None:
            self._dims = analysis.dimensions(self.shapes, self.feats, self.tol)
        return self._dims

    def pattern(self, pid):
        for p in self.patterns:
            if p["id"].upper() == str(pid).strip().upper():
                return p
        raise EngineError(f"Unknown pattern '{pid}'. Patterns: {', '.join(p['id'] for p in self.patterns) or 'none'}")


def _assign_part_ids(objs, registry, reg):
    """Stable part IDs (B1, B2…) keyed by the FreeCAD object name, which never changes inside a document.
    IDs of deleted objects are not reused."""
    old = dict((registry or {}).get("parts", {}))
    nxt = int(reg["next"].get(PART_PREFIX, 1))
    used = set(old.values())
    parts = {}
    for o in objs:
        pid = old.get(o.Name)
        if pid is None:
            while f"{PART_PREFIX}{nxt}" in used:
                nxt += 1
            pid = f"{PART_PREFIX}{nxt}"
            used.add(pid)
        parts[o.Name] = pid
    reg["parts"] = {**old, **parts}
    reg["next"][PART_PREFIX] = nxt
    return parts


# doc.Name -> {part ID: object name}, from the last analysis (lets ops accept "B2" as an object reference)
_PARTS = {}


DATUM_LETTERS = ("A", "B", "C")


def _resolve_datums(an, explicit=None):
    """Datum faces {A, B, C} → plane features. Explicit choices (plane IDs) win; missing ones default to the
    main part's largest planar face (A) and the largest outermost faces perpendicular to it (B, then C).
    Ties prefer faces on the low side (normal towards -X/-Y/-Z), the usual datum corner."""
    explicit = {str(k).upper(): str(v).upper() for k, v in (explicit or {}).items() if v}
    out, chosen = {}, {}
    for L in DATUM_LETTERS:
        f = an.fmap.get(explicit.get(L, ""))
        if f is not None and f["kind"] == "plane":
            out[L], chosen[L] = f, True
    if not an.objs:
        return out, chosen
    main = out["A"]["_obj"] if "A" in out else max(an.objs, key=lambda o: an.shapes[o.Name].Volume).Name
    pool = [f for f in an.feats if f["kind"] == "plane" and not f.get("is_cap") and f["_obj"] == main]

    def best(cands):
        return min(cands, key=lambda f: ("outermost" not in f["label"], -round(f["area"], 3),
                                         -sum(1 for x in f["_n"] if x < -0.5), _idnum(f)), default=None)

    def perp(f, others):
        return all(abs(f["_n"].dot(o["_n"])) < 1e-6 for o in others)

    if "A" not in out:
        a = min(pool, key=lambda f: (-round(f["area"], 3), "outermost" not in f["label"],
                                     -sum(1 for x in f["_n"] if x < -0.5), _idnum(f)), default=None)
        if a is not None:
            out["A"] = a
    if "B" not in out and "A" in out:
        b = best([f for f in pool if perp(f, [out["A"]])])
        if b is not None:
            out["B"] = b
    if "C" not in out and "A" in out and "B" in out:
        c = best([f for f in pool if perp(f, [out["A"], out["B"]])])
        if c is not None:
            out["C"] = c
    return out, chosen


def _idnum(f):
    return int(re.sub(r"\D", "", f["id"]) or 0)


def _datum_distance(datum, p):
    """Distance of point p from a datum face, measured into the material (positive inside the part)."""
    return (datum["_c"] - p).dot(datum["_n"])


def _add_datum_positions(an):
    """Each hole / boss of the datum part gets datum_position {A, B, C}: the axis distance from datums parallel
    to the axis, and the entry (hole) or free end (boss) distance from a datum perpendicular to it."""
    if not an.datums:
        return
    obj = next(iter(an.datums.values()))["_obj"]
    for f in an.feats:
        if f["kind"] not in ("hole", "boss") or f["_obj"] != obj:
            continue
        pos = {}
        for L, d in an.datums.items():
            if abs(d["_n"].dot(f["_a"])) < 1e-6:
                pos[L] = _r(_datum_distance(d, f["_base"]))
            elif abs(abs(d["_n"].dot(f["_a"])) - 1) < 1e-6:
                end = _v(f["entry"]) if f["kind"] == "hole" else f["_base"] + f["_a"] * f["_free_t"]
                pos[L] = _r(_datum_distance(d, end))
        if pos:
            f["datum_position"] = pos


def _datum_point(an, spec, fixed_n, fixed_d, keep):
    """The point x that lies spec[L] mm (into the material) from each given datum L and on the plane
    fixed_n·x = fixed_d (the face being drilled, or a plane across a hole's axis). Directions the datums don't
    fix keep the coordinates of `keep`. Raises when datums are missing or don't fix a unique point."""
    import numpy as np
    rows, rhs, used = [V(fixed_n)], [float(fixed_d)], []
    for L, dist in (spec or {}).items():
        L = str(L).strip().upper()
        d = an.datums.get(L)
        if d is None:
            raise EngineError(f"Datum {L} is not defined. Datums: "
                              + (", ".join(f"{k}={v['id']}" for k, v in an.datums.items()) or "none"))
        if abs(abs(d["_n"].dot(fixed_n)) - 1) < 1e-6:
            raise EngineError(f"Datum {L} ({d['id']}) is parallel to the {'face' if keep is None else 'plane'} the point "
                              "must lie on, so a distance from it can't place the point; use the other datums")
        rows.append(d["_n"])
        rhs.append(d["_n"].dot(d["_c"]) - _num(dist, f"distance from datum {L}"))
        used.append(L)
    A = np.array([[r.x, r.y, r.z] for r in rows])
    b = np.array(rhs)
    # Directions not fixed yet keep `keep`'s coordinates.
    _, s, vt = np.linalg.svd(A)
    rank = int((s > 1e-9).sum())
    if rank < len(rows):
        raise EngineError("Datums " + ", ".join(used) + " are parallel to each other; give distances from "
                          "perpendicular datums")
    for k in range(rank, 3):
        u = vt[k]
        A = np.vstack([A, u])
        b = np.append(b, float(np.dot(u, [keep.x, keep.y, keep.z])))
    x = np.linalg.solve(A, b)
    return V(*x)


def _analyze(doc, registry, datums=None):
    objs = _solid_objects(doc)
    feats, shapes, unknown = [], {}, {}
    tol = 1e-3
    for o in objs:
        shape = _gshape(o)
        shapes[o.Name] = shape
        _, t = _tolerances(shape)
        tol = max(tol, t)
        found, unknown[o.Name] = _features_of_shape(o.Name, shape)
        feats.extend(found)
    reg = _assign_ids(feats, registry, tol)
    parts = _assign_part_ids(objs, registry, reg)
    _PARTS[doc.Name] = {pid: name for name, pid in parts.items()}
    for f in feats:
        f["_part"] = parts[f["_obj"]]
    analysis.finish_links(feats)
    analysis.finish_features(feats)
    reg["edges"] = dict((registry or {}).get("edges", {}))  # edge IDs are matched lazily (_edge_table)
    fmap = {f["id"]: f for f in feats}
    an = _Analysis(objs, feats, fmap, reg, shapes, tol, unknown)
    an.parts = parts
    an.datums, an.datums_explicit = _resolve_datums(an, datums)
    _add_datum_positions(an)
    return an


def _public(f):
    out = {k: v for k, v in f.items() if not k.startswith("_")}
    out["object"] = f["_obj"]
    out["part"] = f["_part"]
    return out


def _parametric_info(doc):
    params, sketches, sheets = [], [], []
    numeric_types = ("App::PropertyLength", "App::PropertyDistance", "App::PropertyAngle",
                     "App::PropertyFloat", "App::PropertyInteger", "App::PropertyQuantity",
                     "App::PropertyFloatConstraint", "App::PropertyIntegerConstraint",
                     "App::PropertyPercent", "App::PropertyBool", "App::PropertyEnumeration")
    skip = {"Visibility", "Placement", "Label", "Label2", "ExpressionEngine", "Refine", "AttacherType",
            "MapReversed", "MapPathParameter", "AttachmentOffset", "AllowCompound", "_Version",
            "SuppressedShape", "AttacherEngine", "MapMode"}
    for o in doc.Objects:
        tid = o.TypeId
        if tid.startswith("Sketcher::"):
            cons = []
            for i, c in enumerate(o.Constraints):
                if c.Type in ("Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle"):
                    val = math.degrees(c.Value) if c.Type == "Angle" else c.Value
                    cons.append({"index": i, "name": c.Name or "", "type": c.Type, "value": _r(val),
                                 "driving": bool(getattr(c, "Driving", True))})
            sketches.append({"name": o.Name, "label": o.Label, "constraints": cons})
            continue
        if tid == "Spreadsheet::Sheet":
            cells = []
            try:
                for cell in o.getUsedCells():
                    alias = o.getAlias(cell) or ""
                    try:
                        val = o.get(alias or cell)
                        if hasattr(val, "Value"):
                            val = _r(val.Value)
                        elif not isinstance(val, (int, float)):
                            val = str(val)
                    except Exception:
                        val = o.getContents(cell)
                    cells.append({"cell": cell, "alias": alias, "value": val})
            except Exception:
                pass
            sheets.append({"name": o.Name, "label": o.Label, "cells": cells[:200]})
            continue
        if not (tid.startswith("PartDesign::") or tid.startswith("Part::")) or tid == "Part::Feature":
            continue
        props = {}
        for pname in o.PropertiesList:
            if pname in skip or o.getTypeIdOfProperty(pname) not in numeric_types:
                continue
            if o.getGroupOfProperty(pname) in ("Base", "Attachment"):
                continue
            try:
                val = getattr(o, pname)
                if hasattr(val, "Value"):
                    val = _r(val.Value)
                elif isinstance(val, float):
                    val = _r(val)
                props[pname] = val
            except Exception:
                pass
        exprs = {}
        try:
            for path, expr in o.ExpressionEngine:
                exprs[path] = expr
        except Exception:
            pass
        if props:
            entry = {"name": o.Name, "label": o.Label, "type": tid, "properties": props}
            if exprs:
                entry["expressions"] = exprs
            params.append(entry)
    return params, sketches, sheets


def _cad_threads(doc, an):
    """Cosmetic threads of PartDesign Hole features ({hole: H#, spec, depth, source}), matched to the recognised
    holes by their profile circles' centres."""
    out = []
    for o in doc.Objects:
        if o.TypeId != "PartDesign::Hole" or not getattr(o, "Threaded", False):
            continue
        try:
            spec = str(o.ThreadSize)
            depth = float(o.ThreadDepth.Value) if hasattr(o, "ThreadDepth") else float(o.Depth.Value)
            prof = o.Profile[0] if isinstance(o.Profile, (list, tuple)) else o.Profile
            gpl = prof.getGlobalPlacement()
            centres = [gpl.multVec(g.Center) for g in prof.Geometry if isinstance(g, (Part.Circle, Part.ArcOfCircle))]
        except Exception as e:
            print(f"[AetherCAD] thread read warning for {o.Name}: {e}")
            continue
        for c in centres:
            for h in an.feats:
                if h["kind"] != "hole":
                    continue
                off = (c - h["_base"]) - h["_a"] * (c - h["_base"]).dot(h["_a"])
                if off.Length < an.tol * 10:
                    out.append({"hole": h["id"], "spec": spec, "depth": _r(depth), "source": f"FCStd {o.Label}",
                                "modeled": bool(getattr(o, "ModelThread", False))})
                    break
    return out


def _bbox_dict(bb):
    return {"min": _vl(V(bb.XMin, bb.YMin, bb.ZMin), 3), "max": _vl(V(bb.XMax, bb.YMax, bb.ZMax), 3),
            "size": [_r(bb.XLength, 3), _r(bb.YLength, 3), _r(bb.ZLength, 3)]}


def _model_info(doc, an, with_features=True):
    objs = an.objs
    feats = an.feats if with_features else []
    gbb = None
    objects = []
    for o in objs:
        s = an.shapes.get(o.Name) or _gshape(o)
        bb = s.BoundBox
        gbb = bb if gbb is None else gbb.united(bb)
        try:
            parent = o.getParentGeoFeatureGroup()
        except Exception:
            parent = None
        objects.append({"id": an.parts.get(o.Name), "name": o.Name, "label": o.Label, "type": o.TypeId, "volume": _r(s.Volume, 3),
                        **({"parent": parent.Label} if parent is not None else {}),
                        "area": _r(s.Area, 3), "solids": len(s.Solids), "faces": len(s.Faces),
                        "edges": len(s.Edges), "bbox": _bbox_dict(bb),
                        "center_of_mass": _vl(s.Solids[0].CenterOfMass if len(s.Solids) == 1 else bb.Center, 3),
                        **({"unrecognized_faces": an.unknown[o.Name]} if an.unknown.get(o.Name) else {})})
    if not with_features:
        return {"objects": objects, "bbox": _bbox_dict(gbb) if gbb else None}
    params, sketches, sheets = _parametric_info(doc)
    by_kind = {}
    for f in feats:
        by_kind.setdefault(f["kind"], []).append(_public(f))
    for k in by_kind:
        by_kind[k].sort(key=lambda f: int(re.sub(r"\D", "", f["id"]) or 0))
    return {
        "units": "mm",
        "document": doc.Label,
        "objects": objects,
        "bbox": _bbox_dict(gbb) if gbb else None,
        "features": by_kind,
        "counts": {k: len(v) for k, v in by_kind.items()},
        "patterns": an.patterns,
        "dimensions": an.dims,
        "datums": {L: {"id": f["id"], "label": f["label"], "normal": f["normal"], "part": f["_part"],
                       "explicit": bool(an.datums_explicit.get(L))} for L, f in an.datums.items()},
        "parametric": params,
        "sketches": sketches,
        "spreadsheets": sheets,
        "is_parametric": bool(params or sketches),
        "threads": _cad_threads(doc, an),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Shape-edit helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean(shape):
    try:
        cleaned = shape.removeSplitter()
        if not cleaned.isNull() and cleaned.Solids:
            shape = cleaned
    except Exception:
        pass
    if len(shape.Solids) == 1 and shape.ShapeType == "Compound":
        shape = shape.Solids[0]
    return shape


def _validate(shape):
    if shape is None or shape.isNull():
        raise EngineError("The operation produced an empty shape")
    if not shape.Solids:
        raise EngineError("The operation produced no solid")
    if not shape.isValid():
        fixed = shape.copy()
        try:
            fixed.fix(1e-7, 1e-7, 1e-7)
        except Exception:
            pass
        if not fixed.isValid():
            raise EngineError("The resulting shape is geometrically invalid; the change was rolled back")
        shape = fixed
    return shape


def _set_shape(doc, obj, new_shape, note):
    """Write a new global-coordinate shape back to obj (or to a new feature if obj is parametric)."""
    new_shape = _validate(_clean(new_shape))
    target = obj
    if obj.TypeId != "Part::Feature":
        target = doc.addObject("Part::Feature", "AIEdit")
        target.Label = f"{obj.Label} (edited)"
        obj.Visibility = False
        if GUI:
            try:
                target.ViewObject.ShapeColor = obj.ViewObject.ShapeColor
            except Exception:
                pass
    pp = _parent_placement(target)
    if pp is not None:
        local = new_shape.copy()
        local.Placement = pp.inverse().multiply(local.Placement)
        new_shape = local
    target.Shape = new_shape
    try:
        if "AiEdits" not in target.PropertiesList:
            target.addProperty("App::PropertyStringList", "AiEdits", "AetherCAD", "Edits applied by AetherCAD")
        target.AiEdits = list(target.AiEdits) + [note]
    except Exception:
        pass
    return target


def _hole_tool(a, base, t0, t1, r, open0, open1, diag, tip_semi=None):
    margin = max(diag * 0.02, 1.0)
    start = t0 - (margin if open0 else 0.0)
    end = t1 + (margin if open1 else 0.0)
    tool = Part.makeCylinder(r, end - start, base + a * start, a)
    if tip_semi and not (open0 and open1) and tip_semi > 1e-3:
        h = r / math.tan(tip_semi)
        if not open0:
            tool = tool.fuse(Part.makeCone(r, 0, h, base + a * t0, a.negative()))
        elif not open1:
            tool = tool.fuse(Part.makeCone(r, 0, h, base + a * t1, a))
    return tool


def _tip_semi(shape, hole):
    for idx in hole.get("_caps", []):
        s = shape.Faces[idx].Surface
        if isinstance(s, Part.Cone):
            return abs(float(s.SemiAngle))
    return None


def _countersink_tools(hole, base, min_r, margin):
    """Cutting cones that recreate the hole's countersinks on the axis through `base`."""
    tools = []
    a = hole["_a"]
    for cs in hole.get("_cs", []):
        R, semi = cs["R"], cs["semi"]
        if R <= min_r or semi < 1e-3:
            continue
        inward = a * cs["inward"]
        at = base + a * cs["outer_t"]
        tools.append(Part.makeCone(R + margin * math.tan(semi), 0, R / math.tan(semi) + margin,
                                   at - inward * margin, inward))
    return tools


def _fill_hole(shape, hole):
    """Return shape with the hole (and its countersinks) filled back in.

    Defeaturing first; if that fails, fuse an exact plug of the original hole geometry.
    """
    r, a, base = hole["_r"], hole["_a"], hole["_base"]
    t0, t1 = hole["_t"]
    expected = math.pi * r * r * (t1 - t0)
    face_ids = hole["_faces"] + hole.get("_caps", []) + [cs["i"] for cs in hole.get("_cs", [])]
    try:
        filled = shape.defeaturing([shape.Faces[i] for i in face_ids])
        if filled.isValid() and filled.Volume > shape.Volume + 0.5 * expected:
            return filled
    except Exception:
        pass
    plug = _hole_tool(a, base, t0, t1, r, False, False, 0.0, _tip_semi(shape, hole))
    for cs in hole.get("_cs", []):  # exact countersink volume, clipped to the cone's own axial band
        h = (cs["R"] - r) / math.tan(cs["semi"])
        inward = a * cs["inward"]
        plug = plug.fuse(Part.makeCone(cs["R"], r, h, base + a * cs["outer_t"], inward))
    filled = shape.fuse(plug)
    if not filled.isValid():
        raise EngineError(f"Could not fill hole {hole['id']} before re-cutting it")
    return filled


def _plug_holes(shape, holes, original):
    """Fuse exact plugs of the given holes (face indices of `shape` are not used)."""
    filled = shape
    for h in sorted(holes, key=lambda h: -h["_r"]):
        plug = _hole_tool(h["_a"], h["_base"], h["_t"][0], h["_t"][1], h["_r"], False, False, 0.0, _tip_semi(original, h))
        for cs in h.get("_cs", []):
            hh = (cs["R"] - h["_r"]) / math.tan(cs["semi"])
            plug = plug.fuse(Part.makeCone(cs["R"], h["_r"], hh, h["_base"] + h["_a"] * cs["outer_t"], h["_a"] * cs["inward"]))
        filled = filled.fuse(plug)
    if not filled.isValid():
        raise EngineError("Could not fill " + ", ".join(h["id"] for h in holes) + " before re-cutting them")
    return filled


def _fill_holes(shape, holes):
    """Fill several holes at once (defeaturing; falls back to exact plugs)."""
    if len(holes) == 1:
        return _fill_hole(shape, holes[0])
    face_ids, expected = set(), 0.0
    for h in holes:
        face_ids.update(h["_faces"] + h.get("_caps", []) + [cs["i"] for cs in h.get("_cs", [])])
        expected += math.pi * h["_r"] ** 2 * (h["_t"][1] - h["_t"][0])
    try:
        filled = shape.defeaturing([shape.Faces[i] for i in sorted(face_ids)])
        if filled.isValid() and filled.Volume > shape.Volume + 0.5 * expected:
            return filled
    except Exception:
        pass
    filled = shape
    for h in sorted(holes, key=lambda h: -h["_r"]):  # counterbores before the holes inside them
        plug = _hole_tool(h["_a"], h["_base"], h["_t"][0], h["_t"][1], h["_r"], False, False, 0.0, _tip_semi(shape, h))
        for cs in h.get("_cs", []):
            hh = (cs["R"] - h["_r"]) / math.tan(cs["semi"])
            plug = plug.fuse(Part.makeCone(cs["R"], h["_r"], hh, h["_base"] + h["_a"] * cs["outer_t"], h["_a"] * cs["inward"]))
        filled = filled.fuse(plug)
    if not filled.isValid():
        raise EngineError("Could not fill " + ", ".join(h["id"] for h in holes) + " before re-cutting them")
    return filled


def _hole_cutter(shape, h, base, r, diag):
    """Cutting solid that recreates hole h (tip, countersinks) with axis through `base` and radius r."""
    open0, open1 = h["_open"]
    tool = _hole_tool(h["_a"], base, h["_t"][0], h["_t"][1], r, open0, open1, diag, _tip_semi(shape, h))
    for cone in _countersink_tools(h, base, r, max(diag * 0.02, 1.0)):
        tool = tool.fuse(cone)
    return tool


def _with_partner(h):
    """A hole plus its counterbore (or the hole a counterbore belongs to): they move together."""
    partner = h.get("_cb") or h.get("_cb_inner")
    return [h, partner] if partner else [h]


def _recut_holes(ctx, specs, note):
    """Fill holes and cut them again at new positions / sizes in one edit.

    specs: [(hole, delta_vector_or_None, new_radius_or_None)]. Moving a hole moves its counterbore too.
    """
    items, seen = [], set()
    for h, delta, new_r in specs:
        group = _with_partner(h) if delta is not None else [h]
        for g in group:
            if g["id"] not in seen:
                seen.add(g["id"])
                items.append((g, delta, new_r if g is h else None))
    by_obj = {}
    for it in items:
        by_obj.setdefault(it[0]["_obj"], []).append(it)
    for objname, its in by_obj.items():
        obj = ctx.doc.getObject(objname)
        shape = _gshape(obj)
        diag, _ = _tolerances(shape)
        result = _fill_holes(shape, [g for g, _, _ in its])
        for g, delta, new_r in sorted(its, key=lambda it: -(it[2] or it[0]["_r"])):
            base = g["_base"] + (delta if delta is not None else V())
            result = result.cut(_hole_cutter(shape, g, base, new_r or g["_r"], diag))
        _set_shape(ctx.doc, obj, result, note)
    for g, delta, new_r in items:
        patch = {}
        if delta is not None:
            patch["b"] = _vl(g["_base"] + delta, 5)
        if new_r is not None:
            patch["r"] = new_r
        if patch:
            ctx.registry_updates[g["id"]] = patch
    return [g["id"] for g, _, _ in items]


def _push_profile(shape, face, n, d):
    """Profile to extrude when moving planar `face` (outward normal n) by d.

    The profile is the face's outer boundary minus the inner outlines that must stay as they are:
      pulling in (d < 0): outlines with material on the outside (bosses standing on the face) are kept, while
        openings to air (the rim between a countersink and its hole) are cut too;
      pushing out (d > 0): outlines that open into the part (holes, pockets) stay open, while the footprints of
        bosses standing on the face are filled, so a face pushed past a boss doesn't leave a pocket above it.
    """
    if len(face.Wires) < 2:
        return face
    _, tol = _tolerances(shape)
    eps = max(tol * 20, 0.01)
    try:
        profile = Part.Face(face.OuterWire)
        keep = []
        for w in face.Wires:
            if w.isSame(face.OuterWire):
                continue
            wf = Part.Face(w)
            probe = wf.CenterOfMass + (n * eps if d < 0 else n * -eps)
            if _inside_any(shape, probe, tol) == (d < 0):
                keep.append(wf)
        if keep:
            profile = profile.cut(Part.makeCompound(keep))
            profile = profile.Faces[0] if len(profile.Faces) == 1 else profile
        return profile
    except Exception:
        return face


def _opening_centers(face):
    out = []
    for w in face.Wires:
        if w.isSame(face.OuterWire):
            continue
        try:
            out.append(Part.Face(w).CenterOfMass)
        except Exception:
            pass
    return out


def _holes_opening_on(ctx, shape, moves, tol):
    """Holes that open on the moved faces, with the axial shift of each end: {hole id: (hole, [s0, s1])}.

    A blind hole (or counterbore, or a hole inside a counterbore that moves) keeps its depth below
    the face, so both of its ends shift; a through hole only stretches at the moved face.
    """
    holes = [f for f in ctx.an.feats if f["kind"] == "hole" and f["_obj"] == moves[0][0]["_obj"]]
    shifts = {}

    def add(h, end, s):
        entry = shifts.setdefault(h["id"], (h, [0.0, 0.0]))
        ends = (0, 1) if not h["through"] else (end,)
        for e in ends:
            entry[1][e] = s if abs(s) > abs(entry[1][e]) else entry[1][e]

    for p, d in moves:
        n = p["_n"]
        centers = _opening_centers(shape.Faces[p["_faces"][0]])
        for h in holes:
            a = h["_a"]
            if abs(a.dot(n)) < 1 - 1e-6:
                continue
            on_axis = any((c - h["_base"] - a * (c - h["_base"]).dot(a)).Length < tol * 10 for c in centers)
            if not on_axis:
                continue
            for end, t in enumerate(h["_t"]):
                t_open = t
                for cs in h.get("_cs", []):
                    if abs(cs["outer_t"] - t) <= abs(cs["outer_t"] - h["_t"][1 - end]):
                        t_open = cs["outer_t"]
                if abs((h["_base"] + a * t_open - p["_c"]).dot(n)) < tol * 10 and h["_open"][end]:
                    add(h, end, d * n.dot(a))
    # A hole inside a counterbore that moves: its end at the counterbore floor moves with it.
    for hid, (h, s) in list(shifts.items()):
        inner = h.get("_cb_inner")
        if inner is None:
            continue
        cap_end = 0 if not h["_open"][0] else 1
        cap_t = h["_t"][cap_end]
        end = min((0, 1), key=lambda e: abs(inner["_t"][e] - cap_t))
        add(inner, end, s[cap_end])
    return shifts


def _shifted_hole(h, s):
    """Copy of hole h with its axial ends (and countersinks at those ends) shifted by s = [s0, s1]."""
    t0, t1 = h["_t"]
    g = dict(h)
    g["_t"] = (t0 + s[0], t1 + s[1])
    cs_new = []
    for cs in h.get("_cs", []):
        end = 0 if abs(cs["outer_t"] - t0) < abs(cs["outer_t"] - t1) else 1
        cs_new.append(dict(cs, outer_t=cs["outer_t"] + s[end]))
    if cs_new:
        g["_cs"] = cs_new
    return g


def _find_face_like(shape, face, n, tol):
    """The face of `shape` on the same plane as `face` that contains an interior point of it."""
    pts, tris = face.tessellate(max(face.BoundBox.DiagonalLength / 200.0, 0.01))
    probes = [(pts[a] + pts[b] + pts[c]) * (1.0 / 3) for a, b, c in tris[:20]]
    for f in shape.Faces:
        if f.Area < 1e-9:
            continue
        try:
            u0, u1, v0, v1 = f.ParameterRange
            if f.normalAt((u0 + u1) / 2, (v0 + v1) / 2).dot(n) < 1 - 1e-6:
                continue
            if f.findPlane(tol) is None:
                continue
        except Exception:
            continue
        if any(f.isInside(q, tol * 10, True) for q in probes):
            return f
    return None


def _fillets_on(ctx, shape, moves, tol):
    """Cylindrical fillets that run along an edge of a moved face (tangent to it) between two planar faces:
    [(round, moved plane, other plane, distance)]. They are removed before the move and re-made after it, so
    they travel with the face instead of staying behind."""
    adj = analysis.Adjacency(shape)
    planes = {i: f for f in ctx.an.feats if f["kind"] == "plane" and f["_obj"] == moves[0][0]["_obj"] for i in f["_faces"]}
    out = []
    for f in ctx.an.feats:
        if f["kind"] != "round" or f["_obj"] != moves[0][0]["_obj"] or f.get("toroidal") or f.get("_members"):
            continue
        nb = set()
        for i in f["_faces"]:
            nb |= adj.neighbours(i)
        nb_planes = [planes[j] for j in nb if j in planes]
        for p, d in moves:
            if p not in nb_planes or abs(f["_a"].dot(p["_n"])) > 1e-6:
                continue
            others = [q for q in nb_planes if q is not p and abs(f["_a"].dot(q["_n"])) < 1e-6]
            if len(others) == 1:
                out.append((f, p, others[0], d))
    return out


def _corner_edges(shape, point, direction, tol):
    """Straight edges of shape lying on the line (point, direction)."""
    out = []
    for e in shape.Edges:
        if len(e.Vertexes) != 2 or not isinstance(e.Curve, (Part.Line, getattr(Part, "LineSegment", Part.Line))):
            continue
        if all(analysis._dist_to_line(v.Point, point, direction) < tol * 10 for v in e.Vertexes):
            out.append(e)
    return out


def _push_faces(ctx, moves, note):
    """Move planar faces along their outward normals in one edit. moves: [(plane, distance)].

    Holes, countersinks and counterbores that open on a moved face move with it (they are filled
    and re-cut relative to the face's new position), so blind depths are kept and nothing is buried.
    Fillets along the moved face's edges are removed first and re-made at the new position.
    """
    moves = [(p, d) for p, d in moves if abs(d) > 1e-9]
    if not moves:
        return
    objs = {p["_obj"] for p, _ in moves}
    if len(objs) > 1:
        raise EngineError("These faces belong to different objects; change one object at a time")
    obj = ctx.obj_of(moves[0][0])
    shape = _gshape(obj)
    diag, tol = _tolerances(shape)
    shifts = _holes_opening_on(ctx, shape, moves, tol)
    fillets = _fillets_on(ctx, shape, moves, tol)

    base = shape
    faces = [(shape.Faces[p["_faces"][0]], p, d) for p, d in moves]
    if fillets:
        try:
            base = shape.defeaturing([shape.Faces[i] for f, _, _, _ in fillets for i in f["_faces"]])
        except Exception as e:
            raise EngineError(f"Could not lift the fillets along the moved face before moving it: {e}")
        if not base.isValid():
            raise EngineError("Could not lift the fillets along the moved face before moving it")
        if shifts:  # hole faces were indexed on the original shape: plug the holes with their exact geometry
            base = _plug_holes(base, [h for h, _ in shifts.values()], shape)
    elif shifts:
        base = _fill_holes(shape, [h for h, _ in shifts.values()])
    if base is not shape:
        located = []
        for face, p, d in faces:
            f2 = _find_face_like(base, face, p["_n"], tol)
            if f2 is None:
                raise EngineError(f"Could not locate face {p['id']} after closing the holes on it")
            located.append((f2, p, d))
        faces = located

    result = base
    prisms = [(_push_profile(base, f, p["_n"], d).extrude(p["_n"] * d), d) for f, p, d in faces]
    for prism, d in prisms:
        if d > 0:
            result = result.fuse(prism)
    for prism, d in prisms:
        if d < 0:
            result = result.cut(prism)
    for h, s in sorted(shifts.values(), key=lambda it: -it[0]["_r"]):  # counterbores before their holes
        g = _shifted_hole(h, s)
        result = result.cut(_hole_cutter(shape, g, h["_base"], h["_r"], diag))
    for f, p, q, d in fillets:
        corner, cd = analysis._plane_intersection(p["_c"] + p["_n"] * d, p["_n"], q["_c"], q["_n"])
        edges = _corner_edges(result, corner, cd, tol)
        if not edges:
            raise EngineError(f"Could not find the edge to re-make fillet {f['id']} after moving {p['id']}")
        try:
            result = result.makeFillet(f["_r"], edges)
        except Exception as e:
            raise EngineError(f"Could not re-make fillet {f['id']} (R{_r(f['_r'])}) after moving {p['id']}: {e}")
        ctx.registry_updates[f["id"]] = {"b": _vl(f["_base"] + p["_n"] * d - f["_a"] * (p["_n"] * d).dot(f["_a"]), 5)}
    _set_shape(ctx.doc, obj, result, note)
    for p, d in moves:
        ctx.registry_updates[p["id"]] = {"d": p["_d"] + d, "c": _vl(p["_c"] + p["_n"] * d, 5)}
    for hid, (h, s) in shifts.items():
        ctx.registry_updates[hid] = {"t": [h["_t"][0] + s[0], h["_t"][1] + s[1]]}


AXIAL_KINDS = ("hole", "boss", "round", "cone", "slot", "pocket")


_RADIAL_RE = re.compile(r"\s*([A-Za-z]+\d+)\s*\.\s*(Ø|⌀|ø|DIA|DIAMETER|D|R|RAD|RADIUS)\s*", re.I)


def _parse_dim(dimension):
    """(kind, refs) for a dimension ID.

    'P5-P6' / 'H3-P5' / 'H1-H2' → ('pair', …)   'P3∠P7' → ('angle', …)   'H1◎H2' → ('coaxial', …)
    'C1.Ø' / 'C1.dia' → ('diameter', [C1])        'R2.R' / 'R2.radius' → ('radius', [R2])
    """
    if not isinstance(dimension, (list, tuple)):
        m = _RADIAL_RE.fullmatch(str(dimension))
        if m:
            kind = "radius" if m.group(2).upper() in ("R", "RAD", "RADIUS") else "diameter"
            return kind, [m.group(1).upper()]
    s = str(dimension)
    kind = "angle" if ("∠" in s or "<" in s) else "coaxial" if "◎" in s else "pair"
    return kind, _dim_refs(dimension)


def _dim_refs(dimension):
    if isinstance(dimension, (list, tuple)):
        refs = [str(x).strip().upper() for x in dimension]
    else:
        refs = [x.strip().upper() for x in re.split(r"\s*(?:-|↔|∠|<|◎|,|\s)\s*", str(dimension).strip()) if x.strip()]
    if len(refs) != 2:
        raise EngineError(f"A dimension is two feature IDs like 'P1-P2', 'H3-P5' or 'P3∠P7', or one feature with "
                          f".Ø / .R like 'C1.Ø', got {dimension!r}")
    return refs


def _canon_dim_id(kind, refs):
    if kind == "diameter":
        return f"{refs[0]}.Ø"
    if kind == "radius":
        return f"{refs[0]}.R"
    return refs[0] + {"angle": "∠", "coaxial": "◎"}.get(kind, "-") + refs[1]


def _radius_of(f):
    return float(f["_r"]) if f.get("_r") else float(f.get("radius") or 0.0)


def _dim_eval(fmap, dimension):
    """(value, unit) of any dimension ID ('mm' or 'deg'), or None when it can't be measured."""
    try:
        kind, refs = _parse_dim(dimension)
    except EngineError:
        return None
    if kind in ("diameter", "radius"):
        f = fmap.get(refs[0])
        if f is None or f["kind"] not in ("hole", "boss", "round", "sphere"):
            return None
        r = _radius_of(f)
        return (2 * r if kind == "diameter" else r), "mm"
    a, b = fmap.get(refs[0]), fmap.get(refs[1])
    if a is None or b is None:
        return None
    if a["kind"] == "plane" and b["kind"] == "plane" and abs(a["_n"].dot(b["_n"])) < 1 - 1e-6:
        return analysis.included_angle(a["_n"], b["_n"]), "deg"
    if kind == "angle":
        return None
    val = _dim_value(fmap, refs[0], refs[1])
    return None if val is None else (val, "mm")


def _dim_value(fmap, ra, rb):
    """Current value of the dimension between two features (None if not measurable)."""
    a, b = fmap.get(ra), fmap.get(rb)
    if a is None or b is None:
        return None
    if a["kind"] == "plane" and b["kind"] == "plane":
        if abs(a["_n"].dot(b["_n"])) < 1 - 1e-6:
            return None
        return abs((b["_c"] - a["_c"]).dot(a["_n"]))
    if a["kind"] == "plane" and b["kind"] in AXIAL_KINDS:
        a, b = b, a
    if a["kind"] in AXIAL_KINDS and b["kind"] == "plane":
        if abs(a["_a"].dot(b["_n"])) > 1e-6:
            return None
        return abs((a["_base"] - b["_c"]).dot(b["_n"]))
    if a["kind"] in AXIAL_KINDS and b["kind"] in AXIAL_KINDS:
        if abs(a["_a"].dot(b["_a"])) < 1 - 1e-6:
            return None
        w = b["_base"] - a["_base"]
        return (w - a["_a"] * w.dot(a["_a"])).Length
    return None


def _faces_for_feature(shape, f):
    return [shape.Faces[i] for i in f["_faces"] + f.get("_caps", []) + [cs["i"] for cs in f.get("_cs", [])]]


def _is_sharp(shape, edge):
    """False for seam edges and tangent (smooth) edges, which can't be filleted/chamfered."""
    try:
        faces = _dedupe_edges(shape.ancestorsOfType(edge, Part.Face))
    except Exception:
        return True
    if len(faces) < 2:
        return False
    p = edge.valueAt((edge.FirstParameter + edge.LastParameter) / 2.0)
    normals = []
    for f in faces[:2]:
        try:
            u, v = f.Surface.parameter(p)
            normals.append(f.normalAt(u, v))
        except Exception:
            return True
    return normals[0].getAngle(normals[1]) > math.radians(1.0)


def _dedupe_edges(edges):
    out, seen = [], {}
    for e in edges:
        bucket = seen.setdefault(e.hashCode(), [])
        if any(e.isSame(x) for x in bucket):
            continue
        bucket.append(e)
        out.append(e)
    return out


def _select_edges(ctx, shape, selectors):
    if isinstance(selectors, dict):
        selectors = [selectors]
    if not selectors:
        raise EngineError("Provide at least one edge selector")
    edges = []
    for sel in selectors:
        if sel.get("all"):
            edges.extend(shape.Edges)
        if "edge" in sel or "edges" in sel:
            ids = sel.get("edges") or [sel["edge"]]
            for eid in ([ids] if isinstance(ids, str) else ids):
                edges.append(_edge_in_shape(ctx, shape, eid))
        if "face" in sel:
            f = ctx.feature(sel["face"])
            for i in f["_faces"]:
                edges.extend(shape.Faces[i].Edges)
        if "between" in sel:
            fa, fb = (ctx.feature(x) for x in sel["between"])
            ea = [e for i in fa["_faces"] for e in shape.Faces[i].Edges]
            eb = [e for i in fb["_faces"] for e in shape.Faces[i].Edges]
            common = [e for e in ea if any(e.isSame(x) for x in eb)]
            if not common:
                raise EngineError(f"{sel['between'][0]} and {sel['between'][1]} share no edge")
            edges.extend(common)
        if "hole" in sel or "boss" in sel:
            f = ctx.feature(sel.get("hole") or sel.get("boss"), ("hole", "boss"))
            a, (t0, t1) = f["_a"], f["_t"]
            end = sel.get("end", "both")
            if f["kind"] == "hole":
                near_t = t1 if f["_open"][1] or not f["_open"][0] else t0  # entry
                names = {"entry": "near", "exit": "far", "both": "both"}
            else:
                near_t = f["_free_t"]  # free end (tip) of a boss
                names = {"free": "near", "tip": "near", "top": "near", "base": "far", "both": "both"}
            far_t = t0 if near_t == t1 else t1
            key = names.get(end)
            if key is None:
                raise EngineError(f"'end' for a {f['kind']} must be one of: {', '.join(names)}")
            wanted = {"near": [near_t], "far": [far_t], "both": [t0, t1]}[key]
            _, tol = _tolerances(shape)
            for i in f["_faces"]:
                for e in shape.Faces[i].Edges:
                    if isinstance(e.Curve, Part.Circle) and any(abs(V(e.Curve.Center).dot(a) - t) < tol * 10 for t in wanted):
                        edges.append(e)
        if "parallel_to" in sel:
            axis = _axis_vec(sel["parallel_to"])
            pool = shape.Edges
            if "on_face" in sel:
                f = ctx.feature(sel["on_face"])
                pool = [e for i in f["_faces"] for e in shape.Faces[i].Edges]
            for e in pool:
                if len(e.Vertexes) == 2 and isinstance(e.Curve, (Part.Line, getattr(Part, "LineSegment", Part.Line))):
                    d = e.Vertexes[1].Point - e.Vertexes[0].Point
                    if d.Length > 1e-9 and abs(d.normalize().dot(axis)) > 1 - 1e-6:
                        edges.append(e)
    edges = [e for e in _dedupe_edges(edges) if _is_sharp(shape, e)]
    if not edges:
        raise EngineError(f"No sharp edges matched selector(s) {selectors}")
    return edges


# ─────────────────────────────────────────────────────────────────────────────
# Operation context + operations
# ─────────────────────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self, doc, an):
        self.doc = doc
        self.an = an
        self.fmap = an.fmap
        self.registry = an.reg
        self.registry_updates = {}  # id -> descriptor patch (expected geometry after the op)
        self.verify = []            # [(id, field)] values to measure after the op
        self.expect_new = None      # (kind, field, value, count): new features that should appear
        self.expect_counts = None   # {kind: n}: this many more features of a kind should exist afterwards
        self.allow_removed = set()  # feature IDs the op may make disappear ("*" = any: the op restructures the part)

    def feature(self, fid, kinds=None):
        f = self.fmap.get(str(fid).strip().upper())
        if f is None:
            raise EngineError(f"Unknown feature id '{fid}'. Use get_model_info / find_features for current IDs.")
        if kinds and f["kind"] not in kinds:
            raise EngineError(f"{fid} is a {f['kind']}, expected {' or '.join(kinds)}")
        return f

    def obj_of(self, f):
        return self.doc.getObject(f["_obj"])


def op_resize_hole(ctx, hole, diameter):
    d = _num(diameter, "diameter")
    if d <= 0:
        raise EngineError("diameter must be > 0")
    h = ctx.feature(hole, ("hole",))
    if h.get("_cb") and d >= 2 * h["_cb"]["_r"]:
        raise EngineError(f"{h['id']} sits in counterbore {h['_cb']['id']} (Ø{h['_cb']['diameter']}); "
                          "the hole must stay smaller than its counterbore. Resize the counterbore first.")
    _recut_holes(ctx, [(h, None, d / 2)], f"resize {h['id']} to Ø{d}")
    ctx.verify.append((h["id"], "diameter", d))
    return f"{h['id']}: Ø{h['diameter']} → Ø{_r(d)} mm"


def op_move_hole(ctx, hole, offset=None, to=None, to_datum=None):
    """Move a hole sideways: by an offset, to a point, or to distances from datums ({"B": 15, "C": 20})."""
    h = ctx.feature(hole, ("hole",))
    a, base = h["_a"], h["_base"]
    if to_datum:
        target = _datum_point(ctx.an, to_datum, a, a.dot(base), base)
        delta = _perp_base(target, a) - base
        for L, dist in to_datum.items():
            ctx.verify.append((h["id"], f"datum:{str(L).upper()}", _num(dist, "datum distance")))
    elif to is not None:
        target = _v(to)
        delta = _perp_base(target, a) - base
    elif offset is not None:
        off = _v(offset)
        delta = off - a * off.dot(a)
    else:
        raise EngineError("Provide 'offset' [dx,dy,dz] or 'to' [x,y,z]")
    _check_no_overlap(ctx, h["_obj"], base + delta, a, h["_r"], ignore={g["id"] for g in _with_partner(h)})
    moved = _recut_holes(ctx, [(h, delta, None)], f"move {h['id']} by {_vl(delta)}")
    ctx.verify.append((h["id"], "center", _vl(base + delta + a * ((h["_t"][0] + h["_t"][1]) / 2), 9)))
    extra = [m for m in moved if m != h["id"]]
    return f"{h['id']} moved by {_vl(delta, 3)} mm" + (f" (with counterbore {', '.join(extra)})" if extra else "")


def op_set_hole_profile(ctx, hole, diameter=None, counterbore_diameter=None, counterbore_depth=None,
                        countersink_diameter=None, countersink_angle=None, remove_counterbore=False,
                        remove_countersink=False, note=None):
    """Re-cut a hole with a new diameter and/or counterbore / countersink at its entry, in one edit.

    Unspecified parts are kept (an existing counterbore keeps its size). Used for standard sizes
    ("M8 clearance", "M6 socket head counterbore"). The axis, the far end (through / blind depth / drill tip)
    and the entry face stay where they are."""
    h = ctx.feature(hole, ("hole",))
    if h.get("_cb_inner") is not None:  # the user named the counterbore: edit the hole it belongs to
        h = h["_cb_inner"]
    cb = h.get("_cb")
    a, base, r = h["_a"], h["_base"], h["_r"]
    t0, t1 = h["_t"]
    obj = ctx.obj_of(h)
    shape = _gshape(obj)
    diag, tol = _tolerances(shape)
    m = max(diag * 0.02, 1.0)

    # Entry of the whole stack (counterbore / countersink top) and the far end of the hole itself.
    if cb is not None:
        c0, c1 = cb["_t"]
        top_t = c1 if cb["_open"][1] else c0
    else:
        open0, open1 = h["_open"]
        top_t = t1 if (open1 or not open0) else t0
        for cs in h.get("_cs", []):
            if abs(cs["outer_t"] - top_t) <= abs(cs["outer_t"] - (t0 + t1 - top_t)):
                top_t = cs["outer_t"]
    sign = -1.0 if abs(top_t - max(t0, t1, top_t)) < 1e-9 else 1.0  # along a, into the material
    far_end = 0 if abs(t0 - top_t) > abs(t1 - top_t) else 1
    far_t, far_open = h["_t"][far_end], h["_open"][far_end]
    d_in = a * sign

    new_r = _num(diameter, "diameter") / 2 if diameter is not None else r
    top_cs = [cs for cs in h.get("_cs", []) if abs(cs["outer_t"] - top_t) < tol * 10]
    cbd = _num(counterbore_diameter, "counterbore_diameter") if counterbore_diameter is not None else \
        (2 * cb["_r"] if cb is not None and not remove_counterbore else None)
    cbdep = _num(counterbore_depth, "counterbore_depth") if counterbore_depth is not None else \
        (abs(cb["_t"][1] - cb["_t"][0]) if cb is not None else None)
    csd = _num(countersink_diameter, "countersink_diameter") if countersink_diameter is not None else \
        (2 * top_cs[0]["R"] if top_cs and not remove_countersink else None)
    csa = _num(countersink_angle, "countersink_angle") if countersink_angle is not None else \
        (math.degrees(2 * top_cs[0]["semi"]) if top_cs else 90.0)
    if new_r <= 0:
        raise EngineError("diameter must be > 0")
    if cbd is not None and cbdep is None:
        raise EngineError("A new counterbore needs counterbore_depth")
    if cbd is not None and csd is not None:
        raise EngineError(f"{h['id']} can't have both a counterbore and a countersink at its entry; remove one")
    if cbd is not None and (cbd <= 2 * new_r or cbdep <= 0):
        raise EngineError(f"The counterbore (Ø{_r(cbd)}) must be larger than the hole (Ø{_r(2 * new_r)}) and > 0 deep")
    if csd is not None and (csd <= 2 * new_r or not 0 < csa < 180):
        raise EngineError(f"The countersink (Ø{_r(csd)}) must be larger than the hole (Ø{_r(2 * new_r)}), angle 0–180°")
    depth_avail = abs(far_t - top_t)
    if cbd is not None and cbdep >= depth_avail - tol:
        raise EngineError(f"A counterbore {_r(cbdep)} deep doesn't fit in {h['id']}, which is {_r(depth_avail)} deep")

    filled = _fill_holes(shape, _with_partner(h))
    if cb is not None and cbd is None:
        ctx.allow_removed.add(cb["id"])
    far_x = far_t + (d_in.dot(a) * m if far_open else 0.0)
    top_x = top_t - d_in.dot(a) * m
    lo, hi = sorted((far_x, top_x))
    tool = Part.makeCylinder(new_r, hi - lo, base + a * lo, a)
    semi_tip = _tip_semi(shape, h)
    if not far_open and semi_tip:
        tool = tool.fuse(Part.makeCone(new_r, 0, new_r / math.tan(semi_tip), base + a * far_t, d_in))
    if cbd is not None:
        tool = tool.fuse(Part.makeCylinder(cbd / 2, cbdep + m, base + a * top_t - d_in * m, d_in))
    if csd is not None:
        semi = math.radians(csa / 2)
        R = csd / 2
        tool = tool.fuse(Part.makeCone(R + m * math.tan(semi), 0, R / math.tan(semi) + m, base + a * top_t - d_in * m, d_in))
    _set_shape(ctx.doc, obj, filled.cut(tool), note or f"hole profile {h['id']}")

    ctx.registry_updates[h["id"]] = {"r": new_r}
    if cb is not None and cbd is not None:
        ctx.registry_updates[cb["id"]] = {"r": cbd / 2, "t": sorted([top_t, top_t + sign * cbdep])}
    ctx.verify.append((h["id"], "diameter", 2 * new_r))
    parts = [f"Ø{_r(2 * new_r)}"]
    if cbd is not None:
        ctx.verify += [(h["id"], "counterbore_diameter", cbd), (h["id"], "counterbore_depth", cbdep)]
        parts.append(f"counterbore Ø{_r(cbd)} × {_r(cbdep)}")
    if csd is not None:
        ctx.verify += [(h["id"], "countersink_diameter", csd), (h["id"], "countersink_angle", csa)]
        parts.append(f"countersink Ø{_r(csd)} × {_r(csa)}°")
    return f"{h['id']}: " + ", ".join(parts) + (f" ({note})" if note else "")


def op_set_dimension(ctx, dimension, value, anchor="start"):
    """Set a dimension between two features to an exact value.

    plane–plane : the faces move apart/together (anchor: start = first ID stays, end = second ID stays,
                  symmetric = both move half).
    hole–plane  : the hole moves (with its counterbore) so its axis is `value` from the face.
    hole–hole   : the holes move along the line joining them (anchor as above).
    """
    target = _num(value, "value")
    anchor = str(anchor or "start").lower()
    if anchor not in ("start", "end", "symmetric"):
        raise EngineError("anchor must be start, end or symmetric")
    kind, refs = _parse_dim(dimension)
    if kind in ("diameter", "radius"):
        return _set_radial(ctx, ctx.feature(refs[0]), kind, target)
    a, b = ctx.feature(refs[0]), ctx.feature(refs[1])
    if a["kind"] == "plane" and b["kind"] == "plane" and abs(a["_n"].dot(b["_n"])) < 1 - 1e-6:
        return _set_angle(ctx, a, b, target, anchor)
    if kind == "angle":
        raise EngineError(f"{a['id']}∠{b['id']}: an angle dimension needs two non-parallel planar faces")
    coaxial = target == 0 and a["kind"] == "hole" and b["kind"] == "hole"
    if target < 0 or (target == 0 and not coaxial):
        raise EngineError("A dimension must be > 0 (0 is only allowed between two holes: make them coaxial)")
    current = _dim_value(ctx.fmap, a["id"], b["id"])
    if current is None:
        raise EngineError(f"{a['id']} and {b['id']} don't define a measurable dimension (need parallel faces, "
                          "a hole and a face parallel to its axis, two parallel holes, or two non-parallel faces "
                          "for an angle)")
    did = _canon_dim_id("coaxial" if kind == "coaxial" else "pair", [a["id"], b["id"]])
    if abs(target - current) < 1e-7:
        ctx.verify.append((did, "dimension", target))
        return f"{did} is already {_r(target)} mm"
    delta = target - current
    share = {"start": (0.0, 1.0), "end": (1.0, 0.0), "symmetric": (0.5, 0.5)}[anchor]

    if a["kind"] == "plane" and b["kind"] == "plane":
        sep = (b["_c"] - a["_c"]).dot(a["_n"])
        u = a["_n"] * (1.0 if sep >= 0 else -1.0)  # direction from a towards b
        _push_faces(ctx, [(b, delta * share[1] * u.dot(b["_n"])), (a, -delta * share[0] * u.dot(a["_n"]))],
                    f"set {did} = {target}")
    elif "plane" in (a["kind"], b["kind"]):
        h, p = (a, b) if b["kind"] == "plane" else (b, a)
        if h["kind"] != "hole":
            raise EngineError(f"Only holes can be moved to set a distance to a face ({h['id']} is a {h['kind']})")
        n = p["_n"]
        d_now = (h["_base"] - p["_c"]).dot(n)
        want = target if d_now >= 0 else -target
        _recut_holes(ctx, [(h, n * (want - d_now), None)], f"set {did} = {target}")
    else:
        if a["kind"] != "hole" or b["kind"] != "hole":
            raise EngineError("Only holes can be moved to set an axis-to-axis distance")
        w = b["_base"] - a["_base"]
        w = w - a["_a"] * w.dot(a["_a"])
        if w.Length < 1e-9:
            raise EngineError(f"{a['id']} and {b['id']} are coaxial, so there is no direction to move them apart; "
                              "use move_hole with an offset")
        u = V(w).normalize()
        specs = []
        if share[1]:
            specs.append((b, u * (delta * share[1]), None))
        if share[0]:
            specs.append((a, u * (-delta * share[0]), None))
        _recut_holes(ctx, specs, f"set {did} = {target}")
    ctx.verify.append((did, "dimension", target))
    return f"{did}: {_r(current)} → {_r(target)} mm ({anchor})"


def _set_radial(ctx, f, kind, target):
    """Set a diameter (.Ø) or radius (.R): holes are re-cut, bosses grown/shrunk, fillets re-made."""
    if target <= 0:
        raise EngineError(f"A {kind} must be > 0")
    new_r = target / 2 if kind == "diameter" else target
    did = _canon_dim_id(kind, [f["id"]])
    if f["kind"] == "hole":
        return op_resize_hole(ctx, f["id"], 2 * new_r)
    if f["kind"] == "boss":
        msg = _resize_boss(ctx, f, new_r)
    elif f["kind"] == "round":
        msg = _refillet(ctx, f, new_r)
    else:
        raise EngineError(f"Changing the {kind} of a {f['kind']} ({f['id']}) isn't supported")
    ctx.verify.append((did, "dimension", target))
    return msg


def _resize_boss(ctx, f, new_r):
    """Grow (fuse a tube outside it) or shrink (cut a tube off it) a cylindrical boss, keeping its axis, its
    length and the body it stands on."""
    obj = ctx.obj_of(f)
    shape = _gshape(obj)
    diag, tol = _tolerances(shape)
    a, base, r = f["_a"], f["_base"], f["_r"]
    t0, t1 = f["_t"]
    eps = max(tol * 20, 0.01)
    free0 = not _inside_any(shape, base + a * (t0 - eps), tol)
    free1 = not _inside_any(shape, base + a * (t1 + eps), tol)
    skin = max(tol * 100, 0.01)  # overlap so booleans don't leave slivers
    if abs(new_r - r) < 1e-9:
        return f"{f['id']} is already Ø{_r(2 * new_r)} mm"
    if new_r < r:
        m = max(diag * 0.02, 1.0)
        lo, hi = t0 - (m if free0 else 0.0), t1 + (m if free1 else 0.0)
        tube = Part.makeCylinder(r + skin, hi - lo, base + a * lo, a).cut(Part.makeCylinder(new_r, hi - lo, base + a * lo, a))
        result = shape.cut(tube)
    else:
        tube = Part.makeCylinder(new_r, t1 - t0, base + a * t0, a).cut(
            Part.makeCylinder(max(r - skin, r * 0.5), t1 - t0, base + a * t0, a))
        result = shape.fuse(tube)
    _set_shape(ctx.doc, obj, result, f"{f['id']} Ø{_r(2 * r)} → Ø{_r(2 * new_r)}")
    ctx.registry_updates[f["id"]] = {"r": new_r}
    return f"{f['id']}: Ø{_r(2 * r)} → Ø{_r(2 * new_r)} mm"


def _refillet(ctx, f, new_r):
    """Change a fillet's radius: remove it (defeaturing restores the sharp edge), then fillet that edge again."""
    obj = ctx.obj_of(f)
    shape = _gshape(obj)
    _, tol = _tolerances(shape)
    a, base, r = f["_a"], f["_base"], f["_r"]
    t0, t1 = f["_t"]
    if f.get("torus_major_radius") is not None:
        raise EngineError(f"{f['id']} is a toroidal (ring) fillet; changing its radius isn't supported yet. "
                          "Remove it and fillet the edge again")
    try:
        sharp = shape.defeaturing([shape.Faces[i] for i in f["_faces"]])
    except Exception as e:
        raise EngineError(f"Could not remove fillet {f['id']} to re-make it: {e}")
    if not sharp.isValid() or abs(sharp.Volume - shape.Volume) < 1e-9:
        raise EngineError(f"Could not remove fillet {f['id']} to re-make it")
    # The restored sharp edge runs along the fillet axis, near it (within a few radii), over its length.
    edges = []
    for e in sharp.Edges:
        if len(e.Vertexes) != 2 or not isinstance(e.Curve, (Part.Line, getattr(Part, "LineSegment", Part.Line))):
            continue
        p, q = e.Vertexes[0].Point, e.Vertexes[1].Point
        d = q - p
        if d.Length < 1e-9 or abs(d.normalize().dot(a)) < 1 - 1e-6:
            continue
        mid = (p + q) * 0.5
        off = (mid - base) - a * (mid - base).dot(a)
        lo, hi = sorted((p.dot(a), q.dot(a)))
        if off.Length < 3 * r and hi > t0 + tol and lo < t1 - tol:
            edges.append(e)
    edges = [e for e in _dedupe_edges(edges) if _is_sharp(sharp, e)]
    if not edges:
        raise EngineError(f"Could not find the edge under fillet {f['id']} after removing it")
    try:
        result = sharp.makeFillet(new_r, edges)
    except Exception as e:
        raise EngineError(f"Fillet R{_r(new_r)} failed on the edge of {f['id']}: {e}. Try a smaller radius.")
    corner = _perp_base(edges[0].Vertexes[0].Point, a)
    # The fillet axis sits on the corner's bisector at a distance proportional to the radius.
    ctx.registry_updates[f["id"]] = {"r": new_r, "b": _vl(corner + (base - corner) * (new_r / r), 5)}
    _set_shape(ctx.doc, obj, result, f"{f['id']} R{_r(r)} → R{_r(new_r)}")
    return f"{f['id']}: R{_r(r)} → R{_r(new_r)} mm"


def _half_space(p, n, size):
    """A big box filling the side of the plane through p that the normal n points away from."""
    box = Part.makeBox(size, size, size, V(-size / 2, -size / 2, -size))
    box.Placement = App.Placement(p, App.Rotation(V(0, 0, 1), n))
    return box


def _set_angle(ctx, a, b, target, anchor):
    """Set the included angle between two planar faces by rotating one of them about the edge they share.

    anchor start: the first face stays and the second rotates; end: the other way round. The swept wedge is
    bounded by the rotating face's flat neighbours, then added (face swings out) or removed (swings in)."""
    if not 0 < target < 180:
        raise EngineError("An angle between faces must be between 0 and 180°")
    if anchor == "symmetric":
        raise EngineError("anchor 'symmetric' isn't supported for angles; use start or end")
    fixed, mov = (a, b) if anchor == "start" else (b, a)
    if fixed["_obj"] != mov["_obj"]:
        raise EngineError("Both faces must belong to the same part")
    did = f"{a['id']}∠{b['id']}"
    obj = ctx.obj_of(mov)
    shape = _gshape(obj)
    diag, tol = _tolerances(shape)
    fm, ff = shape.Faces[mov["_faces"][0]], shape.Faces[fixed["_faces"][0]]
    shared = analysis.shared_edges(fm, ff)
    lines = [e for e in shared if len(e.Vertexes) == 2 and isinstance(e.Curve, (Part.Line, getattr(Part, "LineSegment", Part.Line)))]
    if not lines:
        raise EngineError(f"{a['id']} and {b['id']} don't share a straight edge. An angle edit rotates one face about "
                          "the edge it shares with the other")
    p0 = lines[0].Vertexes[0].Point
    hinge = (lines[0].Vertexes[1].Point - p0).normalize()
    nf, nm = fixed["_n"], mov["_n"]
    current = analysis.included_angle(nf, nm)
    if abs(current - target) < 1e-9:
        ctx.verify.append((did, "dimension", target))
        return f"{did} is already {_r(target)}°"
    delta = target - current
    nm2 = None
    for sgn in (1.0, -1.0):
        cand = App.Rotation(hinge, sgn * delta).multVec(nm)
        if abs(analysis.included_angle(nf, cand) - target) < 1e-7:
            nm2, rot = cand, App.Rotation(hinge, sgn * delta)
            break
    if nm2 is None:
        raise EngineError(f"Could not find a rotation of {mov['id']} giving {target}°")
    c_new = p0 + rot.multVec(mov["_c"] - p0)
    outward = (c_new - mov["_c"]).dot(nm) > 0
    # Flat neighbours of the rotating face bound the wedge; a curved or concave neighbour can't be handled.
    neighbours = []
    for g in ctx.an.feats:
        if g["_obj"] != mov["_obj"] or g is mov:
            continue
        for i in g["_faces"]:
            if not any(e.isSame(x) for e in fm.OuterWire.Edges for x in shape.Faces[i].Edges):
                continue  # not a neighbour, or it meets the face only at an inner opening (a hole rim)
            if g["kind"] != "plane":
                raise EngineError(f"{mov['id']} borders {g['id']} ({g['kind']}), which is not flat; angle edits need "
                                  "flat neighbouring faces")
            if (g["_c"] - mov["_c"]).dot(nm) > tol * 10:
                raise EngineError(f"{mov['id']} meets {g['id']} at a concave (inside) edge; angle edits need convex "
                                  "outer edges")
            neighbours.append(g)
    L = diag * 4
    wedge = _half_space(p0, nm2 if outward else nm, L).common(_half_space(p0, (nm if outward else nm2).negative(), L))
    for g in neighbours:
        wedge = wedge.common(_half_space(g["_c"], g["_n"], L))
    if wedge.isNull() or wedge.Volume < 1e-12:
        raise EngineError("The angle change produced no material change")
    result = shape.fuse(wedge) if outward else shape.cut(wedge)
    _set_shape(ctx.doc, obj, result, f"set {did} = {target}°")
    ctx.registry_updates[mov["id"]] = {"n": _vl(nm2, 9), "d": nm2.dot(p0), "c": _vl(c_new, 5)}
    ctx.verify.append((did, "dimension", target))
    return f"{did}: {_r(current)}° → {_r(target)}° ({mov['id']} rotated about its edge with {fixed['id']})"


def op_edit_hole_pattern(ctx, pattern, pcd=None, count=None, spacing=None, spacing_u=None, spacing_v=None,
                         start_angle=None, anchor="center", count_u=None, count_v=None):
    """Change a hole pattern: bolt-circle diameter / count / start angle, linear spacing / count,
    or grid spacings / counts (count_u × count_v, about the same centre). Holes keep their size, depth,
    countersinks and counterbores."""
    pat = ctx.an.pattern(pattern)
    holes = [ctx.feature(h) for h in pat["holes"]]
    if count is not None:
        count = int(_num(count, "count"))
        if count < 1:
            raise EngineError("count must be >= 1")
        if pat["type"] == "grid":
            raise EngineError("A grid has two counts: give count_u (along dir_u) and/or count_v (along dir_v)")
    for name, val in (("count_u", count_u), ("count_v", count_v)):
        if val is not None:
            if pat["type"] != "grid":
                raise EngineError(f"{name} is only for grid patterns; use count")
            if int(_num(val, name)) < 1:
                raise EngineError(f"{name} must be >= 1")
    for name, val in (("pcd", pcd), ("spacing", spacing), ("spacing_u", spacing_u), ("spacing_v", spacing_v)):
        if val is not None and _num(val, name) <= 0:
            raise EngineError(f"{name} must be > 0")
    pts = analysis.pattern_positions(pat, pcd=pcd, count=count, spacing=spacing, spacing_u=spacing_u,
                                     spacing_v=spacing_v, start_angle=start_angle, anchor=anchor,
                                     count_u=count_u, count_v=count_v)
    a = holes[0]["_a"]
    ctx.allow_removed.update(g["id"] for h in holes for g in _with_partner(h))  # fewer holes when the count drops
    # Every new hole must start on the part: probe just inside the material under its entry point.
    shape0 = _gshape(ctx.obj_of(holes[0]))
    _, tol0 = _tolerances(shape0)
    solid = _fill_holes(shape0, [g for h in holes for g in _with_partner(h)])
    into = _v(holes[0]["direction"])
    off = [p for p in pts if not _inside_any(solid, p + into * max(tol0 * 20, 0.01), tol0)]
    if off:
        raise EngineError(f"{len(off)} of the {len(pts)} new hole positions are off the part (e.g. {_vl(off[0], 3)}); "
                          "change the spacing / PCD so the pattern fits")
    note = f"edit {pat['id']}"
    if len(pts) == len(holes):
        _recut_holes(ctx, [(h, _perp_base(p, a) - h["_base"], None) for h, p in zip(holes, pts)], note)
    else:
        obj = ctx.obj_of(holes[0])
        shape = _gshape(obj)
        diag, _ = _tolerances(shape)
        old = [g for h in holes for g in _with_partner(h)]
        result = _fill_holes(shape, old)
        template = sorted(_with_partner(holes[0]), key=lambda g: -g["_r"])
        for p in pts:
            delta = _perp_base(p, a) - holes[0]["_base"]
            for g in template:
                result = result.cut(_hole_cutter(shape, g, g["_base"] + delta, g["_r"], diag))
        _set_shape(ctx.doc, obj, result, note)
        for h, p in zip(holes, pts):
            delta = _perp_base(p, a) - h["_base"]
            for g in _with_partner(h):
                ctx.registry_updates[g["id"]] = {"b": _vl(g["_base"] + delta, 5)}
    req = {k: float(v) for k, v in (("pcd", pcd), ("spacing", spacing), ("spacing_u", spacing_u),
                                    ("spacing_v", spacing_v), ("start_angle_deg", start_angle)) if v is not None}
    if count is not None:
        req["count"] = count
    if count_u is not None or count_v is not None:
        req["count"] = int(count_u or pat["count_u"]) * int(count_v or pat["count_v"])
    ctx.verify.append((holes[0]["id"], "pattern", req or None))
    changes = ", ".join(f"{k} {v}" for k, v in (("PCD", pcd), ("count", count), ("count_u", count_u),
                                                ("count_v", count_v), ("spacing", spacing),
                                                ("spacing_u", spacing_u), ("spacing_v", spacing_v),
                                                ("start angle", start_angle)) if v is not None)
    return f"{pat['id']} ({pat['type']}, {pat['count']} × Ø{pat['hole_diameter']}): {changes or 'unchanged'}"


def _face_point(p, x):
    """A point on planar face feature p: [x,y,z], or two numbers = the global coordinates along the face's two
    in-plane axes (in X, Y, Z order) for a face normal to X, Y or Z; the third comes from the face."""
    if isinstance(x, (list, tuple)) and len(x) == 2:
        n = p["_n"]
        axis = max(range(3), key=lambda i: abs(n[i]))
        if abs(abs(n[axis]) - 1) > 1e-6:
            raise EngineError(f"Face {p['id']} is not normal to X, Y or Z: give positions as full [x, y, z]")
        u, v = (float(x[0]), float(x[1]))
        c = [None, None, None]
        c[axis] = p["_c"][axis]
        rest = [i for i in range(3) if i != axis]
        c[rest[0]], c[rest[1]] = u, v
        return V(*c)
    return _v(x)


def _check_no_overlap(ctx, objname, point, axis, r, ignore=()):
    """A hole of radius r on the axis through `point` must not cut into another hole (pre-edit check)."""
    if (getattr(ctx, "checks", None) or {}).get("force"):
        return
    for h in ctx.an.feats:
        if h["kind"] != "hole" or h["_obj"] != objname or h["id"] in ignore or abs(h["_a"].dot(axis)) < 1 - 1e-7:
            continue
        w = point - h["_base"]
        off = (w - h["_a"] * w.dot(h["_a"])).Length
        if off < min(r, h["_r"]):
            continue  # coaxial (a counterbore / the same hole)
        if off < r + h["_r"] - 1e-6:
            raise EngineError(f"Pre-edit check: this hole would overlap {h['id']} (Ø{h['diameter']}, axes "
                              f"{_r(off, 3)} mm apart, needs ≥ {_r(r + h['_r'], 3)}). Move it, or pass force to do it anyway.")


def op_add_holes(ctx, face, diameter, positions=None, depth=None):
    d = _num(diameter, "diameter")
    if d <= 0:
        raise EngineError("diameter must be > 0")
    p = ctx.feature(face, ("plane", "pocket", "slot"))
    if p["kind"] != "plane":  # drill into a pocket's / blind slot's floor
        if p.get("_floor") is None:
            raise EngineError(f"{p['id']} has no floor to drill into")
        fl = _gshape(ctx.obj_of(p)).Faces[p["_floor"]]
        p = {"id": p["id"], "kind": "plane", "_obj": p["_obj"], "_faces": [p["_floor"]], "_n": p["_n"],
             "_c": fl.CenterOfMass, "_d": p["_n"].dot(fl.CenterOfMass), "bbox": _bbox_dict(fl.BoundBox)}
    obj = ctx.obj_of(p)
    shape = _gshape(obj)
    diag, tol = _tolerances(shape)
    fface = shape.Faces[p["_faces"][0]]
    n = p["_n"]
    pts = [(_datum_point(ctx.an, x, p["_n"], p["_n"].dot(p["_c"]), p["_c"]) if isinstance(x, dict)
            else _face_point(p, x)) for x in positions] if positions else [p["_c"]]
    through = depth is None or depth == "through"
    dep = diag * 2 if through else _num(depth, "depth")
    margin = max(diag * 0.02, 1.0)
    tools = []
    for pt in pts:
        q = pt - n * (pt - p["_c"]).dot(n)
        if not fface.isInside(q, tol * 10, True):
            # Say why, so the user (and the LLM) can pick a valid point instead of retrying the same one.
            inside = [h for h in ctx.an.feats if h["kind"] == "hole" and h["_obj"] == p["_obj"]
                      and ((q - h["_base"]) - h["_a"] * (q - h["_base"]).dot(h["_a"])).Length < h["_r"] + d / 2]
            if inside:
                h = inside[0]
                raise EngineError(f"Position {_vl(q, 3)} is not possible: a Ø{_r(d)} hole there would cut into hole "
                                  f"{h['id']} (Ø{h['diameter']}, axis through {_vl(h['_base'] + h['_a'] * h['_t'][1], 3)}). "
                                  f"Choose a point at least {_r(h['_r'] + d / 2)} mm from that axis. Do not retry "
                                  f"the same point; tell the user.")
            raise EngineError(f"Position {_vl(q, 3)} is not on face {p['id']}: it lies outside the face's material "
                              f"(face bbox {p['bbox']}; the face may have openings or an irregular outline). "
                              f"Do not retry the same point; tell the user.")
        _check_no_overlap(ctx, p["_obj"], q, n, d / 2)
        tools.append(Part.makeCylinder(d / 2, dep + margin, q + n * margin, n.negative()))
    tool = tools[0] if len(tools) == 1 else Part.makeCompound(tools)
    _set_shape(ctx.doc, obj, shape.cut(tool), f"add {len(pts)} hole(s) Ø{d} on {p['id']}")
    ctx.expect_new = ("hole", "diameter", d, len(pts))
    return f"Added {len(pts)} Ø{_r(d)} mm {'through' if through else f'{_r(dep)} mm deep'} hole(s) on {p['id']}"


def op_remove_features(ctx, features):
    if isinstance(features, str):
        features = [features]
    feats = [ctx.feature(x, ("hole", "boss", "round", "cone", "sphere", "plane", "slot", "pocket", "chamfer"))
             for x in features]
    objs = {f["_obj"] for f in feats}
    msgs = []
    for objname in objs:
        obj = ctx.doc.getObject(objname)
        shape = _gshape(obj)
        group = [f for f in feats if f["_obj"] == objname]
        faces = [face for f in group for face in _faces_for_feature(shape, f)]
        try:
            result = shape.defeaturing(faces)
        except Exception as e:
            raise EngineError(f"Defeaturing failed for {', '.join(f['id'] for f in group)}: {e}")
        unchanged = abs(result.Volume - shape.Volume) < 1e-9 and len(result.Faces) == len(shape.Faces)
        if unchanged or not result.isValid():
            if len(group) == 1 and group[0]["kind"] == "hole":
                result = _fill_hole(shape, group[0])  # a single hole can always be plugged exactly
            elif len(group) == 1 and group[0]["kind"] in ("slot", "pocket"):
                result = shape.fuse(_recess_plug(shape, group[0]))  # an exact plug of the recess
            else:
                raise EngineError(f"FreeCAD could not remove {', '.join(f['id'] for f in group)} together; "
                                  "try removing them one at a time")
        _set_shape(ctx.doc, obj, result, "remove " + ", ".join(f["id"] for f in group))
        ctx.allow_removed.update(f["id"] for f in group)
        ctx.allow_removed.update(g["id"] for f in group if f["kind"] == "hole" for g in _with_partner(f))
        msgs.append(", ".join(f["id"] for f in group))
        ctx.verify.extend((f["id"], "removed") for f in group)
    return "Removed " + "; ".join(msgs)


def _frame(origin, x, z):
    """Placement whose local X, Y, Z axes are x, z × x, z at origin."""
    x, z = V(x).normalize(), V(z).normalize()
    y = z.cross(x)
    m = App.Matrix(x.x, y.x, z.x, origin.x, x.y, y.y, z.y, origin.y, x.z, y.z, z.z, origin.z, 0, 0, 0, 1)
    return App.Placement(m)


def _slot_solid(centre, n_out, direction, width, length, depth, outline, above=0.0, below=0.0):
    """Solid filling a slot whose opening is centred at `centre` on a face with outward normal n_out:
    obround or rectangular, `depth` deep, extended `above` the opening and `below` the bottom."""
    h = depth + above + below
    if outline == "obround":
        straight = length - width
        solid = Part.makeCylinder(width / 2, h, V(-straight / 2, 0, -depth - below))
        solid = solid.fuse(Part.makeCylinder(width / 2, h, V(straight / 2, 0, -depth - below)))
        if straight > 1e-9:
            solid = solid.fuse(Part.makeBox(straight, width, h, V(-straight / 2, -width / 2, -depth - below)))
        solid = solid.removeSplitter()
    else:
        solid = Part.makeBox(length, width, h, V(-length / 2, -width / 2, -depth - below))
    solid.Placement = _frame(centre, direction, n_out)
    return solid


def _breaks_through(shape, f, new_depth):
    """True when a blind recess made new_depth deep would leave no material under its floor (probed under
    several points of the floor itself, so holes in the floor don't count)."""
    _, tol = _tolerances(shape)
    floor = shape.Faces[f["_floor"]]
    pts, tris = floor.tessellate(max(floor.BoundBox.DiagonalLength / 50.0, 0.05))
    probes = [(pts[a] + pts[b] + pts[c]) * (1.0 / 3) for a, b, c in tris[:: max(1, len(tris) // 6)][:6]]
    drop = new_depth - f["_depth"] + max(tol * 20, 0.01)
    return any(not _inside_any(shape, q - f["_n"] * drop, tol) for q in probes) if drop > 0 else False


def _recess_plug(shape, f):
    """Exact solid filling a slot or pocket (its outline extruded from the floor / exit up to the opening)."""
    if f["kind"] == "slot":
        return _slot_solid(f["_c"], f["_n"], f["_dir"], f["_width"], f["_length"], f["_depth"], f["shape"])
    floor = shape.Faces[f["_floor"]]
    return Part.Face(floor.OuterWire).extrude(f["_n"] * f["_depth"])


def op_edit_slot(ctx, slot, width=None, length=None, depth=None, offset=None, to=None, to_datum=None):
    """Change a slot's width, length or depth, or move it (offset / to / to_datum, sideways only). The slot is
    filled with its exact shape and cut again; it stays centred on the same point unless moved."""
    f = ctx.feature(slot, ("slot",))
    obj = ctx.obj_of(f)
    shape = _gshape(obj)
    diag, tol = _tolerances(shape)
    n, c = f["_n"], f["_c"]
    w = _num(width, "width") if width is not None else f["_width"]
    L = _num(length, "length") if length is not None else f["_length"]
    dep = _num(depth, "depth") if depth is not None else f["_depth"]
    if w <= 0 or L <= 0 or dep <= 0:
        raise EngineError("width, length and depth must be > 0")
    if f["shape"] == "obround" and L <= w:
        raise EngineError(f"An obround slot must be longer than it is wide (length {_r(L)} ≤ width {_r(w)})")
    if f["through"] and depth is not None:
        raise EngineError(f"{f['id']} goes through the part; its depth is the part's thickness")
    delta = V()
    if to_datum:
        target = _datum_point(ctx.an, to_datum, n, n.dot(c), c)
        delta = target - c
        for L_, dist in to_datum.items():
            ctx.verify.append((f["id"], f"datum:{str(L_).upper()}", _num(dist, "datum distance")))
    elif to is not None:
        delta = _v(to) - c
    elif offset is not None:
        delta = _v(offset)
    delta = delta - n * delta.dot(n)
    if not f["through"] and _breaks_through(shape, f, dep):
        raise EngineError(f"A {_r(dep)} mm deep slot would break through the part; the floor needs material under it")
    m = max(diag * 0.02, 1.0)
    filled = shape.fuse(_recess_plug(shape, f))
    cutter = _slot_solid(c + delta, n, f["_dir"], w, L, dep, f["shape"], above=m, below=m if f["through"] else 0.0)
    _set_shape(ctx.doc, obj, filled.cut(cutter), f"edit slot {f['id']}")
    nc = c + delta
    a = f["_a"]
    ctx.registry_updates[f["id"]] = {"b": _vl(_perp_base(nc, a), 5), "r": w / 2,
                                     "t": sorted([nc.dot(a), (nc - n * dep).dot(a)])}
    for key, val, given in (("width", w, width), ("length", L, length), ("depth", dep, depth)):
        if given is not None:
            ctx.verify.append((f["id"], key, val))
    if delta.Length > 1e-9:
        ctx.verify.append((f["id"], "center", _vl(nc, 9)))
    changes = [f"{k} {_r(v)}" for k, v, g in (("width", w, width), ("length", L, length), ("depth", dep, depth)) if g is not None]
    if delta.Length > 1e-9:
        changes.append(f"moved by {_vl(delta, 3)}")
    return f"{f['id']} ({f['shape']} slot): " + (", ".join(changes) or "unchanged")


def op_edit_pocket(ctx, pocket, depth=None, corner_radius=None):
    """Change a pocket's depth (its floor moves; holes in the floor keep their depth below it) or the radius
    of its vertical corners (0 = sharp)."""
    f = ctx.feature(pocket, ("pocket",))
    if depth is None and corner_radius is None:
        raise EngineError("Give depth and/or corner_radius")
    msgs = []
    if depth is not None:
        dep = _num(depth, "depth")
        if dep <= 0:
            raise EngineError("depth must be > 0")
        shape = _gshape(ctx.obj_of(f))
        if _breaks_through(shape, f, dep):
            raise EngineError(f"A {_r(dep)} mm deep pocket would break through the part")
        fl = shape.Faces[f["_floor"]]
        floor = {"id": f["id"], "kind": "plane", "_obj": f["_obj"], "_faces": [f["_floor"]], "_n": f["_n"],
                 "_c": fl.CenterOfMass, "_d": f["_n"].dot(fl.CenterOfMass)}
        _push_faces(ctx, [(floor, f["_depth"] - dep)], f"pocket {f['id']} depth {dep}")
        a = f["_a"]
        ctx.registry_updates[f["id"]] = {"t": sorted([f["_c"].dot(a), (f["_c"] - f["_n"] * dep).dot(a)])}
        ctx.verify.append((f["id"], "depth", dep))
        msgs.append(f"depth {_r(f['_depth'])} → {_r(dep)} mm")
    if corner_radius is not None:
        if depth is not None:
            raise EngineError("Change the depth and the corner radius in two separate edits")
        r = _num(corner_radius, "corner_radius")
        if r < 0:
            raise EngineError("corner_radius must be ≥ 0")
        obj = ctx.obj_of(f)
        shape = _gshape(obj)
        _, tol = _tolerances(shape)
        walls = [w for w in f["_walls"] if w not in f.get("_corners", [])]
        wall_planes = [(shape.Faces[j].CenterOfMass, analysis.normal_at(shape.Faces[j], shape.Faces[j].CenterOfMass))
                       for j in walls]
        base = shape
        if f.get("_corners"):
            try:
                base = shape.defeaturing([shape.Faces[i] for i in f["_corners"]])
            except Exception as e:
                raise EngineError(f"Could not remove the corner rounds of {f['id']}: {e}")
            if not base.isValid():
                raise EngineError(f"Could not remove the corner rounds of {f['id']}")
        # the vertical corner edges between two wall planes
        edges = []
        for e in base.Edges:
            if len(e.Vertexes) != 2 or not isinstance(e.Curve, (Part.Line, getattr(Part, "LineSegment", Part.Line))):
                continue
            dvec = e.Vertexes[1].Point - e.Vertexes[0].Point
            if dvec.Length < 1e-9 or abs(abs(dvec.normalize().dot(f["_n"])) - 1) > 1e-6:
                continue
            on = [k for k, (c, nn) in enumerate(wall_planes) if all(abs((v.Point - c).dot(nn)) < tol * 10 for v in e.Vertexes)]
            if len(on) == 2:
                edges.append(e)
        if not edges:
            raise EngineError(f"Could not find the corner edges of {f['id']}")
        result = base
        if r > 0:
            try:
                result = base.makeFillet(r, edges)
            except Exception as e:
                raise EngineError(f"Corner radius {_r(r)} failed for {f['id']}: {e}. It must be smaller than half "
                                  "the pocket's width")
        _set_shape(ctx.doc, obj, result, f"pocket {f['id']} corners R{r}")
        ctx.registry_updates[f["id"]] = {"r": r}
        ctx.verify.append((f["id"], "corner_radius", r))
        msgs.append(f"corner radius {_r(f['_corner_radius'])} → {_r(r)} mm")
    return f"{f['id']}: " + ", ".join(msgs)


def op_edit_chamfer(ctx, chamfer, size=None, to_fillet=None):
    """Re-make a chamfer with a new (equal-distance) size, or turn it into a fillet of radius to_fillet:
    the chamfer face is removed (restoring the sharp edge) and the edge is chamfered / filleted again."""
    f = ctx.feature(chamfer, ("chamfer",))
    if (size is None) == (to_fillet is None):
        raise EngineError("Give either size (new chamfer) or to_fillet (radius)")
    val = _num(size if size is not None else to_fillet, "size" if size is not None else "to_fillet")
    if val <= 0:
        raise EngineError("The size / radius must be > 0")
    obj = ctx.obj_of(f)
    shape = _gshape(obj)
    _, tol = _tolerances(shape)
    try:
        sharp = shape.defeaturing([shape.Faces[i] for i in f["_faces"]])
    except Exception as e:
        raise EngineError(f"Could not remove chamfer {f['id']}: {e}")
    if not sharp.isValid() or abs(sharp.Volume - shape.Volume) < 1e-9:
        raise EngineError(f"Could not remove chamfer {f['id']}")
    edges = _corner_edges(sharp, f["_corner"], f["_a"], tol)
    if not edges:
        raise EngineError(f"Could not find the edge under chamfer {f['id']}")
    try:
        result = sharp.makeChamfer(val, edges) if size is not None else sharp.makeFillet(val, edges)
    except Exception as e:
        raise EngineError(f"{'Chamfer' if size is not None else 'Fillet'} {_r(val)} failed on the edge of {f['id']}: {e}")
    _set_shape(ctx.doc, obj, result, f"chamfer {f['id']} → {'size' if size is not None else 'fillet R'}{val}")
    ctx.allow_removed.add(f["id"])
    if size is not None:
        ctx.registry_updates[f["id"]] = {"r": val}
        ctx.verify.append((f["id"], "size", val))
        return f"{f['id']}: chamfer {_r(f['_r'])} → {_r(val)} mm"
    ctx.expect_new = ("round", "radius", val, 1)
    return f"{f['id']}: chamfer {_r(f['_r'])} mm → fillet R{_r(val)}"


def op_push_pull_face(ctx, face, distance):
    dist = _num(distance, "distance")
    if dist > 0:
        ctx.allow_removed.update(f["id"] for f in ctx.an.feats if f["kind"] == "boss")  # bosses can be buried
    if abs(dist) < 1e-9:
        return "No change (distance 0)"
    p = ctx.feature(face, ("plane",))
    _push_faces(ctx, [(p, dist)], f"push/pull {p['id']} {dist:+}")
    ctx.verify.append((p["id"], "offset", p["_d"] + dist))
    return f"{p['id']} moved {dist:+.4f} mm along its normal {p['normal']}"


def op_set_face_distance(ctx, face, reference_face, distance):
    target = _num(distance, "distance")
    p = ctx.feature(face, ("plane",))
    ref = ctx.feature(reference_face, ("plane",))
    if abs(p["_n"].dot(ref["_n"])) < 1 - 1e-6:
        raise EngineError(f"{p['id']} and {ref['id']} are not parallel")
    s = (p["_c"] - ref["_c"]).dot(p["_n"])
    delta = (target - s) if s >= 0 else (-target - s)
    msg = op_push_pull_face(ctx, p["id"], delta)
    ctx.verify[:] = [v for v in ctx.verify if v[:2] != (p["id"], "offset")]
    ctx.verify.append((p["id"], f"distance_to:{ref['id']}", target))
    return f"Distance {p['id']}↔{ref['id']}: {_r(abs(s))} → {_r(target)} mm ({msg})"


def op_fillet_edges(ctx, radius, edges, object=None):
    rad = _num(radius, "radius")
    obj = _get_obj(ctx.doc, object) if object else _obj_from_selectors(ctx, edges)
    shape = _gshape(obj)
    sel = _select_edges(ctx, shape, edges)
    try:
        result = shape.makeFillet(rad, sel)
    except Exception as e:
        raise EngineError(f"Fillet R{rad} failed on {len(sel)} edge(s): {e}. Try a smaller radius.")
    _set_shape(ctx.doc, obj, result, f"fillet R{rad} on {len(sel)} edges")
    ctx.expect_new = ("round", "radius", rad, None)
    return f"Filleted {len(sel)} edge(s) with R{_r(rad)} mm"


def op_chamfer_edges(ctx, size, edges, object=None):
    sz = _num(size, "size")
    obj = _get_obj(ctx.doc, object) if object else _obj_from_selectors(ctx, edges)
    shape = _gshape(obj)
    sel = _select_edges(ctx, shape, edges)
    try:
        result = shape.makeChamfer(sz, sel)
    except Exception as e:
        raise EngineError(f"Chamfer {sz} failed on {len(sel)} edge(s): {e}. Try a smaller size.")
    _set_shape(ctx.doc, obj, result, f"chamfer {sz} on {len(sel)} edges")
    return f"Chamfered {len(sel)} edge(s) by {_r(sz)} mm"


def _obj_from_selectors(ctx, selectors):
    if isinstance(selectors, dict):
        selectors = [selectors]
    for sel in selectors or []:
        if "edge" in sel or "edges" in sel:
            ids = sel.get("edges") or [sel["edge"]]
            eid = ids if isinstance(ids, str) else ids[0]
            return ctx.doc.getObject(_edge_row(ctx.an, eid)["_obj"])
        for key in ("face", "hole", "boss"):
            if key in sel:
                return ctx.obj_of(ctx.feature(sel[key]))
        if "between" in sel:
            return ctx.obj_of(ctx.feature(sel["between"][0]))
        if "on_face" in sel:
            return ctx.obj_of(ctx.feature(sel["on_face"]))
    return _get_obj(ctx.doc, None)


def _make_primitive(kind, dims, position, direction):
    dims = dims or {}
    pos = _v(position) if position is not None else V(0, 0, 0)
    dirv = _axis_vec(direction) if direction is not None else V(0, 0, 1)
    kind = kind.lower()

    def g(name, *alts):
        for k in (name,) + alts:
            if k in dims:
                return _num(dims[k], k)
        raise EngineError(f"{kind} needs dimension '{name}'")

    if kind == "box":
        L, W, H = g("length", "x"), g("width", "y"), g("height", "z")
        shape = Part.makeBox(L, W, H, V(-L / 2, -W / 2, -H / 2))
        if (dirv - V(0, 0, 1)).Length > 1e-9:
            shape.rotate(V(0, 0, 0), *_rotation_to(dirv))
        shape.translate(pos)
        return shape
    if kind == "cylinder":
        return Part.makeCylinder(g("radius") if "radius" in dims else g("diameter") / 2, g("height", "length"), pos, dirv)
    if kind == "cone":
        return Part.makeCone(g("radius1", "bottom_radius"), g("radius2", "top_radius"), g("height", "length"), pos, dirv)
    if kind == "sphere":
        return Part.makeSphere(g("radius") if "radius" in dims else g("diameter") / 2, pos)
    if kind == "torus":
        return Part.makeTorus(g("major_radius"), g("minor_radius"), pos, dirv)
    raise EngineError(f"Unknown primitive '{kind}'. Use box, cylinder, cone, sphere, torus.")


def _rotation_to(dirv):
    z = V(0, 0, 1)
    axis = z.cross(dirv)
    if axis.Length < 1e-12:
        return (V(1, 0, 0), 180.0)
    return (axis.normalize(), math.degrees(z.getAngle(dirv)))


def op_add_primitive(ctx, shape, dimensions, position, direction=None, operation="fuse", object=None, name=None):
    tool = _make_primitive(shape, dimensions, position, direction)
    operation = (operation or "fuse").lower()
    if operation in ("new", "new_object"):
        o = ctx.doc.addObject("Part::Feature", name or shape.capitalize())
        o.Shape = tool
        return f"Created new object {o.Name} ({shape}, volume {_r(tool.Volume, 3)} mm³)"
    obj = _get_obj(ctx.doc, object)
    base = _gshape(obj)
    if operation == "replace":
        ctx.allow_removed.add("*")
        _set_shape(ctx.doc, obj, tool, f"replace with {shape}")
        return (f"Replaced the shape of {obj.Label} with a {shape}: volume {_r(base.Volume, 3)} → "
                f"{_r(tool.Volume, 3)} mm³, bbox {_bbox_dict(tool.BoundBox)['size']}")
    ctx.allow_removed.add("*")  # a primitive can cover or cut through anything
    if operation == "fuse":
        result = base.fuse(tool)
    elif operation == "cut":
        result = base.cut(tool)
    elif operation in ("common", "intersect"):
        result = base.common(tool)
    else:
        raise EngineError("operation must be fuse, cut, common, replace or new")
    _set_shape(ctx.doc, obj, result, f"{operation} {shape} at {_vl(_v(position) if position else V(), 3)}")
    return f"{operation} {shape} with {obj.Label}: volume {_r(base.Volume, 3)} → {_r(result.Volume, 3)} mm³"


def op_transform_object(ctx, object=None, translate=None, rotate=None):
    obj = _get_obj(ctx.doc, object)
    if translate and not rotate and obj.Name in ctx.an.shapes:
        bb = ctx.an.shapes[obj.Name].BoundBox
        ctx.verify.append((obj.Name, "bbox_min", _vl(V(bb.XMin, bb.YMin, bb.ZMin) + _v(translate), 9)))
    # Compose in global coordinates (what the user sees), then convert back to the parent's frame.
    parent = _parent_placement(obj)
    gpl = parent.multiply(obj.Placement) if parent is not None else obj.Placement
    if rotate:
        axis = _axis_vec(rotate.get("axis", "Z"))
        ang = _num(rotate.get("angle", 0), "angle")
        center = _v(rotate["center"]) if rotate.get("center") is not None else _gshape(obj).BoundBox.Center
        gpl = App.Placement(V(), App.Rotation(axis, ang), center).multiply(gpl)
    if translate:
        gpl = App.Placement(_v(translate), App.Rotation()).multiply(gpl)
    pl = parent.inverse().multiply(gpl) if parent is not None else gpl
    obj.Placement = pl
    return f"{obj.Label} placement → base {_vl(pl.Base, 3)}, rotation {_r(pl.Rotation.Angle * 180 / math.pi, 3)}° about {_vl(pl.Rotation.Axis, 3)}"


def op_scale_object(ctx, factor, object=None, center=None):
    ctx.allow_removed.add("*")
    obj = _get_obj(ctx.doc, object)
    shape = _gshape(obj)
    c = _v(center) if center is not None else shape.BoundBox.Center
    if isinstance(factor, (int, float, str)):
        f = _num(factor, "factor")
        new = shape.copy()
        new.scale(f, c)
        desc = f"×{f}"
    else:
        sx, sy, sz = (_num(x, "factor") for x in factor)
        m = App.Matrix()
        m.move(c.negative())
        m2 = App.Matrix()
        m2.scale(sx, sy, sz)
        m = m2.multiply(m)
        m3 = App.Matrix()
        m3.move(c)
        m = m3.multiply(m)
        new = shape.transformGeometry(m)
        desc = f"×[{sx}, {sy}, {sz}]"
    _set_shape(ctx.doc, obj, new, f"scale {desc}")
    bb = shape.BoundBox
    fx = [f] * 3 if isinstance(factor, (int, float, str)) else [sx, sy, sz]
    ctx.verify.append((obj.Name, "bbox_size", [bb.XLength * abs(fx[0]), bb.YLength * abs(fx[1]), bb.ZLength * abs(fx[2])]))
    return f"Scaled {obj.Label} {desc} about {_vl(c, 3)}; bbox now {_bbox_dict(new.BoundBox)['size']}"


def op_set_property(ctx, object, property, value):
    obj = _get_obj(ctx.doc, object)
    if property not in obj.PropertiesList:
        raise EngineError(f"{obj.Name} has no property '{property}'")
    try:
        for path, expr in obj.ExpressionEngine:
            if path == property:
                raise EngineError(f"{obj.Name}.{property} is driven by the expression '{expr}'. Change its source instead.")
    except EngineError:
        raise
    except Exception:
        pass
    old = getattr(obj, property)
    old_s = _r(old.Value) if hasattr(old, "Value") else old
    setattr(obj, property, value)
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    ctx.verify.append((f"{obj.Name}.{property}", "property", float(value) if numeric else None))
    return f"{obj.Label}.{property}: {old_s} → {value}"


def op_set_sketch_constraint(ctx, sketch, constraint, value):
    sk = _get_obj(ctx.doc, sketch)
    if not sk.TypeId.startswith("Sketcher::"):
        raise EngineError(f"{sk.Name} is not a sketch")
    idx = None
    if isinstance(constraint, int) or (isinstance(constraint, str) and constraint.isdigit()):
        idx = int(constraint)
    else:
        for i, c in enumerate(sk.Constraints):
            if c.Name == constraint:
                idx = i
                break
    if idx is None or idx >= len(sk.Constraints):
        raise EngineError(f"Constraint '{constraint}' not found in {sk.Name}")
    c = sk.Constraints[idx]
    if c.Type not in ("Distance", "DistanceX", "DistanceY", "Radius", "Diameter", "Angle"):
        raise EngineError(f"Constraint {idx} is a {c.Type} constraint, not a dimension")
    if not getattr(c, "Driving", True):
        raise EngineError(f"Constraint {idx} is a reference (non-driving) constraint")
    unit = "deg" if c.Type == "Angle" else "mm"
    old = math.degrees(c.Value) if c.Type == "Angle" else c.Value
    sk.setDatum(idx, App.Units.Quantity(f"{_num(value, 'value')} {unit}"))
    ctx.verify.append((f"{sk.Name}#{idx}", "constraint", _num(value, "value")))
    return f"{sk.Label} constraint {idx}{' ('+c.Name+')' if c.Name else ''} {c.Type}: {_r(old)} → {value} {unit}"


def op_set_spreadsheet_cell(ctx, sheet, cell, value):
    sh = _get_obj(ctx.doc, sheet)
    if sh.TypeId != "Spreadsheet::Sheet":
        raise EngineError(f"{sh.Name} is not a spreadsheet")
    addr = cell
    try:
        found = sh.getCellFromAlias(cell)
        if found:
            addr = found
    except Exception:
        pass
    old = sh.getContents(addr)
    sh.set(addr, str(value))
    return f"{sh.Label}[{addr}]: {old} → {value}"


def op_delete_object(ctx, object):
    ctx.allow_removed.add("*")
    obj = _get_obj(ctx.doc, object)
    # Containers list their children in Group; that is membership, not a dependency.
    users = [o.Name for o in obj.InList
             if not (o.TypeId in CONTAINER_TYPES or o.TypeId == "PartDesign::Body")
             or obj not in (getattr(o, "Group", None) or [])]
    if users:
        raise EngineError(f"{obj.Name} is used by {', '.join(users)}; delete or edit those first")
    label = obj.Label
    ctx.doc.removeObject(obj.Name)
    return f"Deleted {label}"


def op_run_python(ctx, code, description=""):
    ctx.allow_removed.add("*")
    ns = {"App": App, "FreeCAD": App, "Part": Part, "doc": ctx.doc, "V": V, "Vector": V}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<aethercad>", "exec"), ns)
    out = buf.getvalue().strip()
    return (description or "Ran custom FreeCAD script") + (f"\nstdout:\n{out[-2000:]}" if out else "")


OPS = {
    "resize_hole": op_resize_hole,
    "move_hole": op_move_hole,
    "add_holes": op_add_holes,
    "remove_features": op_remove_features,
    "push_pull_face": op_push_pull_face,
    "set_face_distance": op_set_face_distance,
    "set_dimension": op_set_dimension,
    "set_hole_profile": op_set_hole_profile,
    "edit_hole_pattern": op_edit_hole_pattern,
    "edit_slot": op_edit_slot,
    "edit_pocket": op_edit_pocket,
    "edit_chamfer": op_edit_chamfer,
    "fillet_edges": op_fillet_edges,
    "chamfer_edges": op_chamfer_edges,
    "add_primitive": op_add_primitive,
    "transform_object": op_transform_object,
    "scale_object": op_scale_object,
    "set_property": op_set_property,
    "set_sketch_constraint": op_set_sketch_constraint,
    "set_spreadsheet_cell": op_set_spreadsheet_cell,
    "delete_object": op_delete_object,
    "run_python": op_run_python,
}


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────

def _invalid_objects(doc):
    bad = set()
    for o in doc.Objects:
        st = o.State
        if "Invalid" in st or "Error" in st:
            bad.add(o.Name)
    return bad


DEFAULT_TOLERANCE = {"length": 1e-3, "angle": 1e-2}  # mm, degrees
ANGLE_FIELDS = {"countersink_angle", "start_angle_deg", "angle_step_deg", "half_angle_deg", "angle_deg"}


def _tolerance(tolerance=None):
    t = dict(DEFAULT_TOLERANCE)
    for k in t:
        if tolerance and tolerance.get(k) is not None:
            t[k] = abs(float(tolerance[k]))
    return t


def _missing(fid, field, note):
    return {"id": fid, "field": field, "status": "missing", "note": note}


def _measure_field(an, fid, field, doc=None):
    """Measure a value after an edit, at full precision. Returns {id, field, value, unit?, status} where unit is
    'mm' or 'deg' for measurable quantities."""
    fmap = an.fmap
    if field == "property":
        objname, prop = fid.split(".", 1)
        o = doc.getObject(objname)
        val = getattr(o, prop)
        unit = "deg" if o.getTypeIdOfProperty(prop) == "App::PropertyAngle" else "mm"
        return {"id": fid, "field": field, "value": val.Value if hasattr(val, "Value") else val, "unit": unit,
                "status": "ok"}
    if field == "constraint":
        objname, idx = fid.split("#", 1)
        c = doc.getObject(objname).Constraints[int(idx)]
        val = math.degrees(c.Value) if c.Type == "Angle" else c.Value
        return {"id": fid, "field": field, "value": val, "unit": "deg" if c.Type == "Angle" else "mm", "status": "ok"}
    if field == "dimension":
        res = _dim_eval(fmap, fid)
        if res is None:
            return _missing(fid, field, "one of the faces/holes was not found after the edit")
        return {"id": fid, "field": field, "value": res[0], "unit": res[1], "status": "ok"}
    if field == "pattern":
        for p in an.patterns:
            if fid in p["holes"]:
                keys = ("pcd", "count", "angle_step_deg", "start_angle_deg") if p["type"] == "circular" else \
                    ("spacing", "count") if p["type"] == "linear" else ("spacing_u", "spacing_v", "count")
                return {"id": p["id"], "field": field, "value": {k: p[k] for k in keys}, "status": "ok",
                        "_pattern": p}
        return _missing(fid, field, "no pattern found containing this hole")
    if field in ("bbox_min", "bbox_size"):
        s = an.shapes.get(fid)
        if s is None:
            return _missing(fid, field, "object not found after the edit")
        bb = s.BoundBox
        val = [bb.XMin, bb.YMin, bb.ZMin] if field == "bbox_min" else [bb.XLength, bb.YLength, bb.ZLength]
        return {"id": an.parts.get(fid, fid), "field": field, "value": val, "unit": "mm", "status": "ok"}
    f = fmap.get(fid)
    if field == "removed":
        return ({"id": fid, "field": field, "value": True, "status": "ok"} if f is None
                else _missing(fid, field, "the feature is still there after the edit"))
    if f is None:
        return _missing(fid, field, "feature not found after the edit (it may have merged or been consumed)")
    out = {"id": fid, "field": field, "status": "ok", "unit": "mm"}
    if field.startswith("distance_to:"):
        ref = fmap.get(field.split(":", 1)[1])
        if ref is None:
            return _missing(fid, field, "the reference face was not found after the edit")
        return {**out, "value": abs((f["_c"] - ref["_c"]).dot(f["_n"]))}
    if field.startswith("datum:"):
        d = an.datums.get(field.split(":", 1)[1])
        if d is None:
            return _missing(fid, field, "datum not defined after the edit")
        return {**out, "value": _datum_distance(d, f["_base"])}
    if field == "diameter":
        return {**out, "value": 2 * _radius_of(f)}
    if field == "offset":
        return {**out, "value": f["_d"]}
    if field == "center" and f["kind"] in ("slot", "pocket"):  # the centre of the opening
        return {**out, "value": _vl(f["_c"], 9)}
    if field == "center" and f.get("_a") is not None:  # the axis midpoint (the feature's centre), at full precision
        return {**out, "value": _vl(f["_base"] + f["_a"] * ((f["_t"][0] + f["_t"][1]) / 2), 9)}
    if field in ("counterbore_diameter", "counterbore_depth"):
        cb = f.get("_cb")
        if cb is None:
            return _missing(fid, field, "no counterbore found after the edit")
        return {**out, "value": 2 * cb["_r"] if field == "counterbore_diameter" else abs(cb["_t"][1] - cb["_t"][0])}
    if field in ("countersink_diameter", "countersink_angle"):
        cs = (f.get("_cs") or [None])[0]
        if cs is None:
            return _missing(fid, field, "no countersink found after the edit")
        if field == "countersink_diameter":
            return {**out, "value": 2 * cs["R"]}
        return {**out, "value": math.degrees(2 * cs["semi"]), "unit": "deg"}
    return {**out, "value": f.get("_" + field, f.get(field)), "unit": "deg" if field in ANGLE_FIELDS else "mm"}


def _deviation(achieved, requested):
    if isinstance(requested, (list, tuple)):
        return math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(achieved, requested)))
    return abs(float(achieved) - float(requested))


def _rounded(v, n=6):
    if isinstance(v, float):
        return _r(v, n)
    if isinstance(v, (list, tuple)):
        return [_rounded(x, n) for x in v]
    if isinstance(v, dict):
        return {k: _rounded(x, n) for k, x in v.items()}
    return v


def _verify_item(an, item, doc, tol):
    """One verification entry: the measured value and, when a target was requested,
    {requested, achieved, deviation, within_tolerance}."""
    fid, field = item[0], item[1]
    requested = item[2] if len(item) > 2 else None
    m = _measure_field(an, fid, field, doc)
    pattern = m.pop("_pattern", None)
    if m["status"] == "ok" and requested is not None:
        val = m["value"]
        if isinstance(requested, dict):  # pattern: compare each requested key with its own tolerance
            devs, ok = {}, True
            for k, want in requested.items():
                got = val.get(k)
                if got is None:
                    continue
                dev = abs(float(got) - float(want))
                if k == "start_angle_deg" and pattern and pattern.get("full_circle"):
                    step = pattern["angle_step_deg"]  # a full circle has no first hole: compare modulo the step
                    dev = abs((float(got) - float(want) + step / 2) % step - step / 2)
                limit = 0.5 if k == "count" else tol["angle" if k in ANGLE_FIELDS else "length"]
                devs[k] = dev
                ok = ok and dev <= limit + 1e-12
            m.update(requested=requested, achieved=val, deviation=max(devs.values()) if devs else 0.0,
                     within_tolerance=ok)
        elif isinstance(val, (int, float, list, tuple)) and not isinstance(val, bool):
            dev = _deviation(val, requested)
            limit = tol["angle" if m.get("unit") == "deg" else "length"]
            m.update(requested=requested, achieved=val, deviation=dev, within_tolerance=dev <= limit + 1e-12)
    return _rounded(m)


# ── Locks (design intent) ───────────────────────────────────────────────────

def _feature_params(an, f):
    """The values a feature lock keeps fixed."""
    k = f["kind"]
    if k == "plane":
        return {"offset": f["_d"], "nx": f["_n"].x, "ny": f["_n"].y, "nz": f["_n"].z}
    out = {}
    if k in ("hole", "boss", "round", "cone", "slot", "pocket", "chamfer"):
        out.update(x=f["_base"].x, y=f["_base"].y, z=f["_base"].z, radius=f["_r"])
    if k == "hole":
        out["depth"] = f["depth"]
    if k == "boss":
        out["length"] = f["length"]
    if k in ("slot", "pocket"):
        out.update({key: f[key] for key in ("width", "length", "depth", "corner_radius") if f.get(key) is not None})
    if k == "chamfer":
        out.update(size=f["_r"], angle=f["angle_deg"])
    if k in ("sphere", "face"):
        c = f["_c"]
        out.update(x=c.x, y=c.y, z=c.z)
    return out


def _lock_target(an, target):
    """('dimension' | 'feature' | 'pattern' | 'part', thing) for a lock target, or (None, None) if absent."""
    t = str(target).strip().upper()
    if t in an.fmap:
        return "feature", an.fmap[t]
    if t.startswith("PAT"):
        p = next((p for p in an.patterns if p["id"] == t), None)
        return ("pattern", p) if p else (None, None)
    obj = _part_object(an, t)
    if obj:
        return "part", obj
    return ("dimension", t) if _dim_eval(an.fmap, t) is not None else (None, None)


def _lock_value(an, lock):
    """Current value of a lock's target: a number (dimension) or a dict of numbers. None if it is gone."""
    mode = lock.get("mode") or "value"
    kind, thing = _lock_target(an, lock["target"])
    if kind is None:
        return None
    if mode == "centered":
        f = thing if kind == "feature" else None
        if f is None:
            return None
        bb = an.shapes[f["_obj"]].BoundBox
        c = f.get("_base") if f.get("_base") is not None else f.get("_c")
        if c is None:
            return None
        axes = [lock["axis"].upper()] if lock.get("axis") else \
            [ax for ax, u in (("X", V(1, 0, 0)), ("Y", V(0, 1, 0)), ("Z", V(0, 0, 1)))
             if f.get("_a") is None or abs(f["_a"].dot(u)) < 1e-6]
        mids = {"X": (c.x, bb.Center.x), "Y": (c.y, bb.Center.y), "Z": (c.z, bb.Center.z)}
        return {ax: mids[ax][0] - mids[ax][1] for ax in axes if ax in mids}
    if kind == "dimension":
        return _dim_eval(an.fmap, thing)[0]
    if kind == "feature":
        return _feature_params(an, thing)
    if kind == "pattern":
        return {k: thing[k] for k in ("count", "pcd", "spacing", "spacing_u", "spacing_v") if k in thing}
    s = an.shapes[thing]
    bb = s.BoundBox
    return {"volume": s.Volume, "xmin": bb.XMin, "ymin": bb.YMin, "zmin": bb.ZMin,
            "xmax": bb.XMax, "ymax": bb.YMax, "zmax": bb.ZMax}


def _lock_unit(an, lock):
    return "deg" if lock.get("mode", "value") == "value" and (_dim_eval(an.fmap, lock["target"]) or (0, "mm"))[1] == "deg" else "mm"


def _lock_violations(an, locks, tol):
    """Locks whose value moved by more than the tolerance (or whose target disappeared)."""
    out = []
    for lock in locks or []:
        want = lock.get("value")
        got = _lock_value(an, lock)
        name = lock["target"] + (" centred" if lock.get("mode") == "centered" else "")
        if got is None:
            out.append(f"{name} would be removed")
            continue
        if want is None:
            continue
        limit = tol["angle" if _lock_unit(an, lock) == "deg" else "length"] + 1e-9
        if isinstance(want, dict):
            bad = [k for k, w in want.items() if k in got and abs(float(got[k]) - float(w)) > (
                max(abs(float(w)) * 1e-7, 0.01) if k == "volume" else limit)]
            if bad:
                out.append(f"{name} would change ({', '.join(f'{k} {_r(want[k])} → {_r(got[k])}' for k in bad)})")
        elif abs(float(got) - float(want)) > limit:
            out.append(f"{name} would change from {_r(want)} to {_r(got)}")
    return out


class _LockViolation(EngineError):
    pass


def _pre_checks(ctx, an, an2, checks):
    """Problems the edit would introduce, as [(severity, message)]; errors refuse the edit unless checks.force.

    error   a hole / slot / pocket the edit didn't mean to remove has disappeared (it merged with another or broke out
            of the part), or two parts that didn't overlap now do;
    warning a wall got thinner than checks.min_wall."""
    out = []
    if "*" not in ctx.allow_removed:
        for fid, f in an.fmap.items():
            if f["kind"] not in ("hole", "slot", "pocket") or fid in an2.fmap or fid in ctx.allow_removed:
                continue
            near = [h["id"] for h in an.feats if h["kind"] == "hole" and h is not f and h["_obj"] == f["_obj"]
                    and f.get("_a") is not None and abs(h["_a"].dot(f["_a"])) > 1 - 1e-7
                    and analysis.axis_offset(f, h) < f["_r"] + h["_r"] + 1.0]
            out.append(("error", f"{fid} would be destroyed" + (f" (it would merge with {', '.join(near)})" if near
                                                                else " (it would break out of the part)")))
    # parts the edit changed: new ones, a different volume / position, or a feature moved, added or removed
    touched_objs = {an.fmap[i]["_obj"] for i in ctx.registry_updates if i in an.fmap}
    touched_objs |= {an2.fmap[i]["_obj"] for i in set(an2.fmap) ^ set(an.fmap) if i in an2.fmap}
    touched_objs |= {an.fmap[i]["_obj"] for i in set(an2.fmap) ^ set(an.fmap) if i in an.fmap}
    changed = [o for o in an2.objs if o.Name not in an.shapes or o.Name in touched_objs
               or abs(an2.shapes[o.Name].Volume - an.shapes[o.Name].Volume) > 1e-6
               or (an2.shapes[o.Name].BoundBox.Center - an.shapes[o.Name].BoundBox.Center).Length > 1e-6]
    for o in changed:
        for other in an2.objs:
            if other is o or other in changed and other.Name < o.Name:
                continue
            a2, b2 = an2.shapes[o.Name], an2.shapes[other.Name]
            if not a2.BoundBox.intersect(b2.BoundBox):
                continue
            c2 = a2.common(b2)
            v2 = c2.Volume if not c2.isNull() and c2.Solids else 0.0
            if v2 <= 1e-6:
                continue
            v1 = 0.0
            if o.Name in an.shapes and other.Name in an.shapes:
                c1 = an.shapes[o.Name].common(an.shapes[other.Name])
                v1 = c1.Volume if not c1.isNull() and c1.Solids else 0.0
            if v2 > v1 + 1e-6:
                out.append(("error", f"{an2.parts.get(o.Name)} ({o.Label}) would interfere with "
                                     f"{an2.parts.get(other.Name)} ({other.Label}): {_r(v2, 3)} mm³ overlap"))
    mw = checks.get("min_wall")
    if mw:
        for o in changed:
            if len(an2.shapes[o.Name].Faces) > 400:
                continue
            w2 = _wall_thickness_of(an2, o, per_face=4)
            if w2 and w2["min_thickness"] < float(mw):
                w1 = _wall_thickness_of(an, o, per_face=4) if o.Name in an.shapes else None
                if not w1 or w2["min_thickness"] < w1["min_thickness"] - 1e-3:
                    out.append(("error" if checks.get("strict") else "warning",
                                f"{an2.parts.get(o.Name)}: the thinnest wall would be {w2['min_thickness']} mm between "
                                f"{' and '.join(x for x in w2['faces'] if x)} (minimum {float(mw):g} mm)"))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Mesh / export
# ─────────────────────────────────────────────────────────────────────────────

def _export_stl(doc, path):
    import Mesh  # type: ignore
    import MeshPart  # type: ignore
    objs = _solid_objects(doc)
    if not objs:
        raise EngineError("Nothing to mesh")
    mesh = Mesh.Mesh()
    for o in objs:
        s = _gshape(o)
        lin = max(s.BoundBox.DiagonalLength / 3000.0, 0.002)
        mesh.addMesh(MeshPart.meshFromShape(Shape=s, LinearDeflection=lin,
                                            AngularDeflection=math.radians(12), Relative=False))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mesh.write(path)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# FreeCAD GUI: window layout, manual edits, selection
# ─────────────────────────────────────────────────────────────────────────────

def _arrange_window():
    """Put the FreeCAD window beside the browser, once per FreeCAD session.

    FREECAD_WINDOW = right (default) | left | max | none.
    """
    mode = os.environ.get("FREECAD_WINDOW", "right").strip().lower()
    if not GUI or mode in ("", "none", "off", "0"):
        return
    try:
        from PySide import QtWidgets  # type: ignore
        mw = Gui.getMainWindow()
        if not mw.property("aethercadArranged"):
            mw.setProperty("aethercadArranged", True)
            scr = mw.screen() if hasattr(mw, "screen") else QtWidgets.QApplication.primaryScreen()
            g = scr.availableGeometry()
            if mode == "max":
                mw.showMaximized()
            else:
                mw.showNormal()
                frame = mw.frameGeometry().size() - mw.geometry().size()
                half = g.width() // 2
                mw.move(g.x() + (half if mode == "right" else 0), g.y())
                mw.resize(half - frame.width(), g.height() - frame.height())
        if mw.isMinimized():
            mw.showNormal()
        mw.raise_()
        mw.activateWindow()
    except Exception as e:
        print(f"[AetherCAD] window layout warning: {e}")


_SIGS = {}  # doc.Name -> geometry signature when AetherCAD last saved / loaded it


def _doc_signature(doc):
    """Cheap fingerprint of the visible solid geometry (FreeCAD's GUI 'Modified' flag is set by
    view changes and isn't cleared reliably, so it can't tell real manual edits apart)."""
    sig = []
    for o in _solid_objects(doc):
        try:
            s = _gshape(o)
            bb = s.BoundBox
            sig.append((o.Name, round(s.Volume, 6), round(s.Area, 6), len(s.Faces),
                        tuple(round(x, 6) for x in (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax))))
        except Exception:
            sig.append((o.Name, None))
    return tuple(sig)


def _remember(doc):
    _SIGS[doc.Name] = _doc_signature(doc)


def _gui_modified(doc):
    """True when the geometry changed since AetherCAD last saved/loaded it, i.e. the user edited it by
    hand in FreeCAD. Unknown (e.g. FreeCAD was restarted) counts as unchanged."""
    if not GUI:
        return False
    old = _SIGS.get(doc.Name)
    return old is not None and old != _doc_signature(doc)


def _sel_target(obj):
    """(root object name, subname prefix) for selecting obj's sub-elements, through App::Part containers."""
    chain = []
    try:
        p = obj.getParentGeoFeatureGroup()
        while p is not None:
            chain.insert(0, p)
            p = p.getParentGeoFeatureGroup()
    except Exception:
        chain = []
    if not chain:
        return obj.Name, ""
    return chain[0].Name, ".".join(x.Name for x in chain[1:] + [obj]) + "."


def _feature_faces(f):
    return f["_faces"] + f.get("_caps", []) + [cs["i"] for cs in f.get("_cs", [])]


def _edge_table(an):
    """Every edge of every part (rows from analysis.edge_rows), with IDs matched against the registry's edges.
    Matched rows get their E# ID; others have id None until _edge_ids assigns one. Unmatched registry entries
    (edges that no longer exist) are dropped."""
    if an._edges is not None:
        return an._edges
    owners = {}
    for (obj, i), f in _face_owners(an).items():
        owners.setdefault(obj, {})[i] = f["id"]
    rows = []
    for o in an.objs:
        shape = an.shapes[o.Name]
        _, tol = _tolerances(shape)
        for r in analysis.edge_rows(shape, o.Name, owners.get(o.Name, {}), tol):
            r["part"] = an.parts.get(o.Name)
            r["_tol"] = tol
            rows.append(r)
    reg = an.reg.setdefault("edges", {})
    pairs = []
    for ri, r in enumerate(rows):
        for eid, d in reg.items():
            if d.get("o") != r["_obj"] or d.get("k") != r["kind"]:
                continue
            dist = (_v(d["m"]) - r["_mid"]).Length
            if dist < r["_tol"] * 10 and abs(d["l"] - r["_length"]) < max(r["_tol"] * 10, r["_length"] * 1e-4):
                pairs.append((dist, ri, eid))
    pairs.sort(key=lambda x: x[0])
    taken_r, taken_e = set(), set()
    for _, ri, eid in pairs:
        if ri in taken_r or eid in taken_e:
            continue
        rows[ri]["id"] = eid
        taken_r.add(ri)
        taken_e.add(eid)
    for eid in [e for e in reg if e not in taken_e]:
        del reg[eid]
    for r in rows:
        r.setdefault("id", None)
    an._edges = rows
    return rows


def _edge_ids(an, rows):
    """Give E# IDs to rows that have none yet (deterministic order) and record them in the registry."""
    reg = an.reg.setdefault("edges", {})
    nxt = int(an.reg["next"].get(EDGE_PREFIX, 1))
    for r in sorted([r for r in rows if not r["id"]], key=lambda r: (r["_obj"], -r["_mid"].z, r["_mid"].x, r["_mid"].y)):
        r["id"] = f"{EDGE_PREFIX}{nxt}"
        reg[r["id"]] = {"o": r["_obj"], "k": r["kind"], "m": _vl(r["_mid"], 5), "l": r["_length"]}
        nxt += 1
    an.reg["next"][EDGE_PREFIX] = nxt
    return rows


def _edge_public(r):
    return {k: v for k, v in r.items() if not k.startswith("_")}


def _edge_row(an, eid):
    key = str(eid).strip().upper()
    for r in _edge_table(an):
        if r["id"] == key:
            return r
    raise EngineError(f"Unknown edge '{eid}' (it may have changed in an edit). Use find_edges, or pick it in the viewer.")


def _edge_in_shape(ctx, shape, eid):
    """The edge of `shape` (a fresh global copy of its object) that edge ID eid refers to."""
    r = _edge_row(ctx.an, eid)
    _, tol = _tolerances(shape)
    for e in shape.Edges:
        try:
            mid = e.valueAt((e.FirstParameter + e.LastParameter) / 2.0)
        except Exception:
            continue
        if (mid - r["_mid"]).Length < tol * 10 and abs(e.Length - r["_length"]) < max(tol * 10, e.Length * 1e-4):
            return e
    raise EngineError(f"Edge {eid} was not found on its part")


def _part_object(an, key):
    """Object name for a part ID (B1), or None."""
    return next((name for name, pid in an.parts.items() if pid == key), None)


def _resolve_refs(an, ids):
    """Features for a list of IDs; dimension IDs (P1-P2) and pattern IDs (PAT1) expand to their members.
    Part IDs (B1) are returned separately as object names: (features, missing, part object names)."""
    out, missing, parts = [], [], []
    for raw in ids or []:
        key = str(raw).strip().upper()
        if re.fullmatch(rf"{EDGE_PREFIX}\d+", key):
            continue  # edges are drawn separately (highlight)
        if _part_object(an, key):
            parts.append(_part_object(an, key))
        elif key in an.fmap:
            out.append(an.fmap[key])
        elif key.startswith("PAT"):
            try:
                out.extend(an.fmap[h] for h in an.pattern(key)["holes"] if h in an.fmap)
            except EngineError:
                missing.append(raw)
        elif any(c in key for c in "-∠<◎."):
            try:
                refs = _parse_dim(key)[1]
                out.extend(an.fmap[r] for r in refs if r in an.fmap)
            except EngineError:
                missing.append(raw)
        else:
            missing.append(raw)
    seen, uniq = set(), []
    for f in out:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    return uniq, missing, parts


# ─────────────────────────────────────────────────────────────────────────────
# Public RPC methods
# ─────────────────────────────────────────────────────────────────────────────

def ping():
    return {"pong": True, "gui": GUI, "version": ".".join(App.Version()[:3])}


def open_model(session_id, source_path, work_path, registry=None):
    _close_session(session_id, work_path)
    ext = os.path.splitext(source_path)[1].lower()
    if ext == ".fcstd":
        doc = App.openDocument(source_path)
    else:
        doc = App.newDocument("AI_" + re.sub(r"\W", "", session_id)[-8:])
        if ext in (".step", ".stp", ".iges", ".igs"):
            if GUI:
                import ImportGui  # type: ignore
                ImportGui.insert(source_path, doc.Name)
            else:
                import Import  # type: ignore
                Import.insert(source_path, doc.Name)
        elif ext in (".brep", ".brp"):
            o = doc.addObject("Part::Feature", "Model")
            o.Shape = Part.read(source_path)
        else:
            App.closeDocument(doc.Name)
            raise EngineError(f"Unsupported file type '{ext}'")
    doc.recompute()
    if not _solid_objects(doc):
        App.closeDocument(doc.Name)
        raise EngineError("No solid geometry found in the file (meshes and pure surfaces cannot be edited)")
    os.makedirs(os.path.dirname(work_path), exist_ok=True)
    doc.saveAs(work_path)
    doc.Label = os.path.splitext(os.path.basename(source_path))[0]
    _save(doc)
    _DOCS[session_id] = doc.Name
    try:
        doc.UndoMode = 1
    except Exception:
        pass
    _arrange_window()
    _activate(doc, fit=True)
    an = _analyze(doc, registry)
    return {"info": _model_info(doc, an), "registry": an.reg}


def reload(session_id, work_path, registry=None, datums=None):
    """Close and re-open the session document from disk (after undo/redo)."""
    _close_session(session_id, work_path)
    doc = _get_doc(session_id, work_path)
    _activate(doc, fit=False)
    an = _analyze(doc, registry, datums)
    return {"info": _model_info(doc, an), "registry": an.reg}


def model_info(session_id, work_path, registry=None, datums=None):
    """Re-analyse the document. `manual_edit` is true when the user edited it by hand in FreeCAD
    (the change is saved so the backend can record it as a version)."""
    doc = _get_doc(session_id, work_path)
    manual = _gui_modified(doc)
    if manual:
        doc.recompute()
        _save(doc)
    an = _analyze(doc, registry, datums)
    return {"info": _model_info(doc, an), "registry": an.reg, "manual_edit": manual}


def _alternatives(op, params):
    """Other ways to do the same edit when the requested one would break a lock (design intent):
    keep the other face fixed, or move both faces half way."""
    if op == "set_dimension" and not params.get("anchor"):
        return [(dict(params, anchor=a), f"anchor '{a}'") for a in ("end", "symmetric")]
    if op == "set_face_distance" and params.get("face") and params.get("reference_face"):
        return [(dict(params, face=params["reference_face"], reference_face=params["face"]),
                 f"moved {params['reference_face']} instead of {params['face']}")]
    return []


def apply(session_id, work_path, op, params=None, registry=None, tolerance=None, locks=None, datums=None,
          preview_mesh=None, checks=None):
    """Run one edit transactionally. The result is measured inside the transaction: a value outside the
    tolerance or a broken lock aborts it (document unchanged). When a lock blocks the requested way of doing
    the edit, the alternatives from _alternatives are tried before refusing."""
    if op not in OPS:
        raise EngineError(f"Unknown operation '{op}'. Available: {', '.join(OPS)}")
    params = dict(params or {})
    tol = _tolerance(tolerance)
    doc = _get_doc(session_id, work_path)
    attempts = [(params, None)] + (_alternatives(op, params) if locks else [])
    first_violation = None
    for p, how in attempts:
        an = _analyze(doc, registry, datums)
        try:
            res = _apply_once(doc, an, op, p, tol, locks, datums, preview_mesh, checks or {})
        except _LockViolation as e:
            first_violation = first_violation or e
            continue
        if how:
            res["message"] += f"\nLock: {first_violation.args[0]} with the requested anchor, so the edit {how}."
            res["lock_resolution"] = how
        return res
    raise EngineError(f"Refused: locked {first_violation.args[0]}. Nothing was changed. "
                      + ("No other anchor keeps the locks either. " if len(attempts) > 1 else "")
                      + "Unlock it first (unlock_dimension) or choose another edit.")


def _apply_once(doc, an, op, params, tol, locks, datums, preview_mesh=None, checks=None):
    before = {o.Name: _r(an.shapes[o.Name].Volume, 3) for o in an.objs}
    names_before = {o.Name for o in doc.Objects}
    invalid_before = _invalid_objects(doc)
    ctx = _Ctx(doc, an)
    ctx.checks = checks or {}

    doc.openTransaction(f"AetherCAD: {op}")
    try:
        message = OPS[op](ctx, **params)
        doc.recompute()
        newly_bad = _invalid_objects(doc) - invalid_before
        if newly_bad:
            raise EngineError(f"Recompute failed for: {', '.join(sorted(newly_bad))}")
        if not _solid_objects(doc) and op != "delete_object":
            raise EngineError("The edit left no visible solid geometry")
        reg = copy.deepcopy(an.reg)
        for fid, patch in ctx.registry_updates.items():
            if fid in reg["items"]:
                reg["items"][fid].update(patch)
        an2 = _analyze(doc, reg, datums)
        verified = [_verify_item(an2, item, doc, tol) for item in ctx.verify]
        # A requested value that can't be measured after the edit means the edit didn't produce it: roll back.
        unmeasured = [v for item, v in zip(ctx.verify, verified)
                      if len(item) > 2 and item[2] is not None and v.get("status") != "ok"]
        if ctx.expect_new:
            kind, field, value, count = ctx.expect_new
            new = [f for fid, f in an2.fmap.items() if fid not in an.fmap and f["kind"] == kind
                   and abs(float(f.get(field, -1)) - value) < max(tol["length"] * 10, 1e-3)]
            for f in new:
                m = _verify_item(an2, (f["id"], field, value), doc, tol)
                verified.append(m)
            if not new or (count and len(new) != count):
                miss = _missing(kind, field, f"expected {count or 'a'} new {kind}(s) with {field} {_r(value)}, "
                                             f"found {len(new)}")
                verified.append(miss)
                unmeasured.append(miss)
        if ctx.expect_counts:
            for kind, n in ctx.expect_counts.items():
                got = sum(1 for f in an2.feats if f["kind"] == kind) - sum(1 for f in ctx.an.feats if f["kind"] == kind)
                entry = {"id": kind, "field": "count", "requested": n, "achieved": got, "deviation": abs(got - n),
                         "within_tolerance": got == n, "status": "ok", "value": got}
                verified.append(entry)
                if got != n:
                    unmeasured.append({**entry, "note": f"expected {n} more {kind}(s), found {got}"})
        if unmeasured:
            raise EngineError("The result could not be verified, so the edit was rolled back: " + "; ".join(
                f"{v['id']} {v['field']}: {v.get('note', 'not found')}" for v in unmeasured))
        problems = _pre_checks(ctx, an, an2, ctx.checks)
        errors = [m for sev, m in problems if sev == "error"]
        if errors and not ctx.checks.get("force"):
            raise EngineError("Pre-edit check failed, so nothing was changed: " + "; ".join(errors)
                              + " (pass force to do it anyway)")
        warnings = [m for _, m in problems]
        bad = [v for v in verified if v.get("within_tolerance") is False]
        if bad:
            raise EngineError("Outside tolerance, so the edit was rolled back: " + "; ".join(
                f"{v['id']} {v['field']} requested {v['requested']}, achieved {v['achieved']} "
                f"(deviation {v['deviation']} > ±{tol['angle' if v.get('unit') == 'deg' else 'length']:g} "
                f"{'°' if v.get('unit') == 'deg' else 'mm'})" for v in bad))
        violations = _lock_violations(an2, locks, tol)
        if violations:
            raise _LockViolation("; ".join(violations))
        if preview_mesh:
            # Dry run: keep the result's mesh and measurements, then throw the edit away.
            _export_stl(doc, preview_mesh)
            info2 = _model_info(doc, an2)
            doc.abortTransaction()
            doc.recompute()
            return {"preview": True, "message": message, "verified": verified, "warnings": warnings,
                    "volume_before": before, "volume_after": {o.Name: _r(an2.shapes[o.Name].Volume, 3) for o in an2.objs},
                    "info": info2, "registry": an.reg, "touched": sorted(set(ctx.registry_updates)), "mesh": preview_mesh}
        doc.commitTransaction()
    except TypeError as e:
        doc.abortTransaction()
        doc.recompute()
        raise EngineError(f"Bad parameters for {op}: {e}")
    except Exception:
        doc.abortTransaction()
        doc.recompute()
        raise

    converted = [o for o in doc.Objects if o.Name not in names_before and o.Name.startswith("AIEdit")]
    if converted:
        message += ("\nNote: " +", ".join(o.Label for o in converted) + " replaces a parametric object with a "
                    "plain solid; that object's parameters no longer drive the shown geometry.")
    after = {o.Name: _r(an2.shapes[o.Name].Volume, 3) for o in an2.objs}
    touched = set(ctx.registry_updates) | {v["id"] for v in verified if v.get("status") == "ok"}
    for item in ctx.verify:
        if item[1] == "dimension":
            try:
                touched.update(_parse_dim(item[0])[1])
            except EngineError:
                pass
    touched |= set(an2.fmap) ^ set(an.fmap)  # new and removed features
    _save(doc)
    _activate(doc)
    return {
        "message": message,
        "verified": verified,
        "warnings": warnings,
        "touched": sorted(touched),
        "volume_before": before,
        "volume_after": after,
        "info": _model_info(doc, an2),
        "registry": an2.reg,
    }


def evaluate_locks(session_id, work_path, locks, registry=None, datums=None):
    """Current value of each lock target ({target, mode?, axis?}); value None when the target doesn't exist."""
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry, datums)
    out = []
    for lock in locks or []:
        val = _lock_value(an, lock)
        out.append({**lock, "value": _rounded(val, 9) if val is not None else None,
                    "unit": _lock_unit(an, lock) if val is not None else None})
    return {"locks": out, "registry": an.reg}


def find_features(session_id, work_path, registry=None, kind=None, diameter_min=None, diameter_max=None,
                  normal=None, near=None, object=None, limit=50, datums=None):
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry, datums)
    objnames = None
    if object:
        q = str(object).lower()
        objnames = {o.Name for o in an.objs if o.Name.lower() == q or o.Label.lower() == q
                    or an.parts.get(o.Name, "").lower() == q}
    out = []
    nvec = _axis_vec(normal) if normal is not None else None
    for f in an.feats:
        if kind and f["kind"] != kind:
            continue
        if objnames is not None and f["_obj"] not in objnames:
            continue
        dia = f.get("diameter")
        if diameter_min is not None and (dia is None or dia < float(diameter_min) - 1e-6):
            continue
        if diameter_max is not None and (dia is None or dia > float(diameter_max) + 1e-6):
            continue
        if nvec is not None and (f["kind"] != "plane" or f["_n"].dot(nvec) < 1 - 1e-4):
            continue
        out.append(f)
    if near is not None:
        p = _v(near)
        out.sort(key=lambda f: (_v(f["center"]) - p).Length)
    else:
        out.sort(key=lambda f: (f["kind"], int(re.sub(r"\D", "", f["id"]) or 0)))
    return {"features": [_public(f) for f in out[: int(limit or 50)]], "total": len(out), "registry": an.reg}


def dimensions(session_id, work_path, registry=None, object=None, feature=None, type=None, limit=60):
    """Driving dimensions, optionally filtered by object (name/label), a feature ID they involve, or type."""
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    rows = an.dims
    if object:
        q = str(object).lower()
        names = {o.Name for o in an.objs if o.Name.lower() == q or o.Label.lower() == q
                    or an.parts.get(o.Name, "").lower() == q}
        rows = [d for d in rows if d["object"] in names]
    if feature:
        fid = str(feature).strip().upper()
        rows = [d for d in rows if fid in d["refs"]]
    if type:
        rows = [d for d in rows if d["type"] == type]
    return {"dimensions": rows[: int(limit or 60)], "total": len(rows), "patterns": an.patterns, "registry": an.reg}


def measure(session_id, work_path, a, b, registry=None):
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    fmap = an.fmap

    def to_shape(ref):
        if isinstance(ref, (list, tuple)):
            return Part.Vertex(_v(ref)), None
        f = fmap.get(str(ref).upper())
        if f is not None:
            shape = an.shapes[f["_obj"]]
            return Part.makeCompound([shape.Faces[i] for i in f["_faces"]]), f
        return _gshape(_get_obj(doc, ref)), None

    sa, fa = to_shape(a)
    sb, fb = to_shape(b)
    dist, pairs, _ = sa.distToShape(sb)
    out = {"min_distance": _r(dist), "closest_points": [[_vl(p, 3), _vl(q, 3)] for p, q in pairs[:2]], "registry": an.reg}
    if fa and fb:
        if fa.get("_a") is not None and fb.get("_a") is not None and abs(fa["_a"].dot(fb["_a"])) > 1 - 1e-6:
            d = fb["_base"] - fa["_base"]
            out["axis_to_axis_distance"] = _r((d - fa["_a"] * d.dot(fa["_a"])).Length)
        if fa["kind"] == "plane" and fb["kind"] == "plane" and abs(fa["_n"].dot(fb["_n"])) > 1 - 1e-6:
            out["plane_to_plane_distance"] = _r(abs((fb["_c"] - fa["_c"]).dot(fa["_n"])))
        ca, cb = _v(fa["center"]), _v(fb["center"])
        out["center_delta"] = _vl(cb - ca, 4)
        out["center_distance"] = _r((cb - ca).Length)
    return out


def find_edges(session_id, work_path, registry=None, feature=None, object=None, convexity=None, kind=None,
               near=None, min_length=None, limit=60):
    """Edges with IDs (E#), filtered by an adjacent feature, part, convexity (convex / concave / tangent), curve kind
    (line / circle / arc …), length; sorted by distance to `near` if given. Returned edges get IDs."""
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    rows = _edge_table(an)
    if feature:
        fid = str(feature).strip().upper()
        rows = [r for r in rows if fid in r["faces"]]
    if object:
        q = str(object).lower()
        names = {o.Name for o in an.objs if q in (o.Name.lower(), o.Label.lower(), an.parts.get(o.Name, "").lower())}
        rows = [r for r in rows if r["_obj"] in names]
    if convexity:
        rows = [r for r in rows if r["convexity"] == convexity]
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    if min_length is not None:
        rows = [r for r in rows if r["_length"] >= float(min_length)]
    if near is not None:
        p = _v(near)
        rows = sorted(rows, key=lambda r: (r["_mid"] - p).Length)
    total = len(rows)
    rows = rows[: int(limit or 60)]
    _edge_ids(an, rows)
    if near is None:
        rows = sorted(rows, key=lambda r: int(r["id"][1:]))
    return {"edges": [_edge_public(r) for r in rows], "total": total, "registry": an.reg}


def wall_thickness(session_id, work_path, registry=None, object=None, threshold=None, samples=6, max_rays=3000):
    """Minimum wall thickness per part by ray casting: from sample points on every face a ray goes into the
    material; the length until it leaves the solid is the local wall thickness. The thinnest ray is then refined
    with the exact distance between the two faces it joins (when they don't touch).
    Returns {parts: [{part, object, min_thickness, at, faces, thin}]}; `thin` lists faces under `threshold`."""
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    objs = an.objs
    if object:
        q = str(object).lower()
        objs = [o for o in objs if q in (o.Name.lower(), o.Label.lower(), an.parts.get(o.Name, "").lower())]
        if not objs:
            raise EngineError(f"No part '{object}'")
    nfaces = sum(len(an.shapes[o.Name].Faces) for o in objs) or 1
    per_face = max(1, min(int(samples), max_rays // nfaces))
    out = [r for r in (_wall_thickness_of(an, o, threshold, per_face) for o in objs) if r]
    return {"parts": out, "registry": an.reg}


def _wall_thickness_of(an, o, threshold=None, per_face=6):
    """Thinnest wall of one analysed part (see wall_thickness), or None."""
    owners = _face_owners(an)
    shape = an.shapes[o.Name]
    diag, tol = _tolerances(shape)
    adj = analysis.Adjacency(shape)
    best, face_min = None, {}
    for i, face in enumerate(shape.Faces):
        try:
            pts, tris = face.tessellate(max(diag / 200.0, 0.05))
        except Exception:
            continue
        if not tris:
            continue
        step = max(1, len(tris) // per_face)
        for t in tris[::step][:per_face]:
            p = (pts[t[0]] + pts[t[1]] + pts[t[2]]) * (1.0 / 3)
            n = analysis.normal_at(face, p)
            start = p - n * (tol * 2)
            ray = Part.LineSegment(start, p - n * (diag * 2)).toShape()
            try:
                inside = shape.common(ray)
            except Exception:
                continue
            segs = [e for e in inside.Edges if min((v.Point - start).Length for v in e.Vertexes) < max(tol * 50, 0.05)]
            if not segs:
                continue
            seg = segs[0]
            q = max((v.Point for v in seg.Vertexes), key=lambda v: (v - start).Length)
            th = (q - p).Length
            if th < face_min.get(i, (math.inf,))[0]:
                face_min[i] = (th, p, q)
            if best is None or th < best[0]:
                best = (th, i, p, q)
    if best is None:
        return None
    th, i, p, q = best
    hit = min(range(len(shape.Faces)), key=lambda j: shape.Faces[j].distToShape(Part.Vertex(q))[0] if j != i else math.inf)
    if hit not in adj.neighbours(i):
        exact, pairs, _ = shape.Faces[i].distToShape(shape.Faces[hit])
        if exact > tol and exact <= th + tol:
            th, (p, q) = exact, pairs[0]
    fid = lambda j: (owners.get((o.Name, j)) or {}).get("id")  # noqa: E731
    thin = []
    if threshold is not None:
        thin = sorted(({"face": fid(j), "thickness": _r(v[0]), "at": _vl(v[1], 3)} for j, v in face_min.items()
                       if v[0] < float(threshold)), key=lambda x: x["thickness"])[:30]
    return {"part": an.parts.get(o.Name), "object": o.Name, "label": o.Label, "min_thickness": _r(th),
            "at": [_vl(p, 3), _vl(q, 3)], "faces": [fid(i), fid(hit)], "thin": thin}


def list_objects(session_id, work_path, registry=None, name_contains=None):
    """Every solid object (largest first) with its container, size and position."""
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    info = _model_info(doc, an, with_features=False)
    rows = info["objects"]
    if name_contains:
        q = str(name_contains).lower()
        rows = [o for o in rows if q in o["name"].lower() or q in o["label"].lower()
                or q in o.get("parent", "").lower()]
    rows.sort(key=lambda o: -o["volume"])
    out = [{"name": o["name"], "label": o["label"], **({"parent": o["parent"]} if "parent" in o else {}),
            "volume": o["volume"], "bbox": o["bbox"]} for o in rows]
    return {"objects": out, "total": len(out), "overall_bbox": info["bbox"], "registry": an.reg}


def highlight(session_id, work_path, ids, registry=None, select=True, zoom=False, max_triangles=60000):
    """Triangle meshes of the given features (global mm) for the web viewer, and — in GUI mode —
    select them in FreeCAD so both views show the same thing. IDs may be features, dimensions or patterns."""
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    feats, missing, part_objs = _resolve_refs(an, ids)
    # (id, kind, object name, face indices); a part is all of its object's faces
    items = [(an.parts[o], "part", o, range(len(an.shapes[o].Faces))) for o in part_objs]
    items += [(f["id"], f["kind"], f["_obj"], _feature_faces(f)) for f in feats]
    meshes, total = [], 0
    edge_ids = [str(x).strip().upper() for x in ids or [] if re.fullmatch(rf"{EDGE_PREFIX}\d+", str(x).strip().upper())]
    for eid in edge_ids:
        try:
            r = _edge_row(an, eid)
        except EngineError:
            missing.append(eid)
            continue
        pts = r["_edge"].discretize(Deflection=max(an.shapes[r["_obj"]].BoundBox.DiagonalLength / 600.0, 0.01))
        lines = []
        for a, b in zip(pts, pts[1:]):
            lines += [_r(a.x), _r(a.y), _r(a.z), _r(b.x), _r(b.y), _r(b.z)]
        meshes.append({"id": eid, "kind": "edge", "positions": [], "indices": [], "lines": lines,
                       "edge": _edge_public(r)})
    for fid, kind, objname, faces in items:
        shape = an.shapes[objname]
        defl = max(shape.BoundBox.DiagonalLength / 600.0, 0.01)
        pos, idx = [], []
        for i in faces:
            try:
                pts, tris = shape.Faces[i].tessellate(defl)
            except Exception:
                continue
            off = len(pos) // 3
            for p in pts:
                pos += [_r(p.x, 4), _r(p.y, 4), _r(p.z, 4)]
            for t in tris:
                idx += [t[0] + off, t[1] + off, t[2] + off]
        total += len(idx) // 3
        if total > max_triangles:
            break
        meshes.append({"id": fid, "kind": kind, "positions": pos, "indices": idx})
    if GUI and select:
        try:
            Gui.Selection.clearSelection()
            for o in part_objs:
                root, prefix = _sel_target(doc.getObject(o))
                if prefix:
                    Gui.Selection.addSelection(doc.Name, root, prefix)
                else:
                    Gui.Selection.addSelection(doc.getObject(o))
            for f in feats:
                root, prefix = _sel_target(doc.getObject(f["_obj"]))
                for i in _feature_faces(f):
                    Gui.Selection.addSelection(doc.Name, root, f"{prefix}Face{i + 1}")
            for eid in edge_ids:
                r = next((r for r in _edge_table(an) if r["id"] == eid), None)
                if r is None:
                    continue
                shape = an.shapes[r["_obj"]]
                for k, e in enumerate(shape.Edges):
                    if e.isSame(r["_edge"]):
                        root, prefix = _sel_target(doc.getObject(r["_obj"]))
                        Gui.Selection.addSelection(doc.Name, root, f"{prefix}Edge{k + 1}")
                        break
            if zoom and (feats or part_objs):
                Gui.SendMsgToActiveView("ViewSelection")
            Gui.updateGui()
        except Exception as e:
            print(f"[AetherCAD] selection warning: {e}")
    return {"meshes": meshes, "missing": missing, "registry": an.reg}


def _face_owners(an):
    """(object name, face index) -> the feature a user means when pointing at that face. A face can belong to
    two features (a boss's end face is also a plane); the ordinary visible plane wins over the boss, while a
    hole's floor (a cap plane) goes to the hole."""
    owners = {}
    # later wins: cap planes < other features (holes, bosses…) < ordinary planes
    for f in sorted(an.feats, key=lambda f: (0 if f.get("is_cap") else 2) if f["kind"] == "plane" else 1):
        for i in _feature_faces(f):
            owners[(f["_obj"], i)] = f
    return owners


def pick(session_id, work_path, point, registry=None, mode="feature"):
    """The component under a point clicked in the web viewer (a point on the STL, global mm): the feature
    whose face is nearest, or with mode="part" the part. Returns {id, feature, part, face, distance}."""
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    p = _v(point)
    vx = Part.Vertex(p)
    best = None
    for o in an.objs:
        shape = an.shapes[o.Name]
        # The STL is a tessellation, so the clicked point can be off the true surface by the mesh deflection.
        margin = max(shape.BoundBox.DiagonalLength * 0.01, 0.2)
        bb = App.BoundBox(shape.BoundBox)
        bb.enlarge(margin)
        if not bb.isInside(p):
            continue
        for i, face in enumerate(shape.Faces):
            fb = App.BoundBox(face.BoundBox)
            fb.enlarge(margin)
            if not fb.isInside(p):
                continue
            d = face.distToShape(vx)[0]
            if d <= margin and (best is None or d < best[0]):
                best = (d, o.Name, i)
    if mode == "edge":
        best_e = None
        for r in _edge_table(an):
            margin = max(an.shapes[r["_obj"]].BoundBox.DiagonalLength * 0.02, 0.5)
            bb = App.BoundBox(r["_edge"].BoundBox)
            bb.enlarge(margin)
            if not bb.isInside(p):
                continue
            d = r["_edge"].distToShape(vx)[0]
            if d <= margin and (best_e is None or d < best_e[0]):
                best_e = (d, r)
        if best_e is None:
            return {"id": None, "feature": None, "part": None, "registry": an.reg}
        d, r = best_e
        _edge_ids(an, [r])
        return {"id": r["id"], "feature": None, "part": r["part"], "edge": _edge_public(r), "distance": _r(d),
                "registry": an.reg}
    if best is None:
        return {"id": None, "feature": None, "part": None, "registry": an.reg}
    d, objname, i = best
    f = _face_owners(an).get((objname, i))
    part = an.parts[objname]
    return {"id": part if mode == "part" or f is None else f["id"], "feature": f["id"] if f else None,
            "part": part, "face": i + 1, "distance": _r(d), "registry": an.reg}


def gui_selection(session_id, work_path, registry=None):
    """IDs of what the user has selected in the FreeCAD window: faces map to their features, whole objects
    to their part IDs."""
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    if not GUI:
        return {"ids": [], "gui": False, "registry": an.reg}
    owners = _face_owners(an)
    ids = []

    def solid_of(obj):
        # A face picked on a PartDesign Body resolves to its tip feature; walk up to the analysed object.
        while obj is not None and obj.Name not in an.parts:
            try:
                obj = obj.getParentGeoFeatureGroup()
            except Exception:
                return None
        return obj

    for sel in Gui.Selection.getSelectionEx(doc.Name):
        obj = solid_of(sel.Object)
        if obj is None:
            continue
        faces = [n.split(".")[-1] for n in sel.SubElementNames if n.split(".")[-1].startswith("Face")]
        found = [owners.get((obj.Name, int(n[4:]) - 1)) for n in faces]
        for fid in [f["id"] for f in found if f] or [an.parts[obj.Name]]:
            if fid not in ids:
                ids.append(fid)
    return {"ids": ids, "gui": True, "registry": an.reg}


def show_window(session_id=None, work_path=None):
    """Bring FreeCAD (and the session's document) to the front."""
    if session_id:
        doc = _get_doc(session_id, work_path)
        _activate(doc, fit=False)
    if GUI:
        try:
            mw = Gui.getMainWindow()
            if mw.isMinimized():
                mw.showNormal()
            mw.raise_()
            mw.activateWindow()
        except Exception as e:
            print(f"[AetherCAD] show window warning: {e}")
    return {"shown": GUI}


def snapshot(session_id, work_path, fcstd_path, stl_path):
    doc = _get_doc(session_id, work_path)
    _save(doc)
    os.makedirs(os.path.dirname(fcstd_path), exist_ok=True)
    doc.saveCopy(fcstd_path)
    _export_stl(doc, stl_path)
    return {"fcstd": fcstd_path, "stl": stl_path}


def export(session_id, work_path, fmt, path):
    doc = _get_doc(session_id, work_path)
    fmt = fmt.lower()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if fmt == "fcstd":
        doc.saveCopy(path)
    elif fmt == "stl":
        _export_stl(doc, path)
    elif fmt in ("step", "stp", "iges", "igs", "brep"):
        objs = _solid_objects(doc)
        if fmt == "brep":
            Part.makeCompound([_gshape(o) for o in objs]).exportBrep(path)
        else:
            import Import  # type: ignore
            Import.export(objs, path)
    else:
        raise EngineError(f"Unsupported export format '{fmt}'")
    return {"path": path}


def close_session(session_id, work_path=None):
    _close_session(session_id, work_path)
    return {"closed": True}


METHODS = {
    "ping": ping,
    "open_model": open_model,
    "reload": reload,
    "model_info": model_info,
    "apply": apply,
    "find_features": find_features,
    "dimensions": dimensions,
    "measure": measure,
    "list_objects": list_objects,
    "highlight": highlight,
    "pick": pick,
    "gui_selection": gui_selection,
    "show_window": show_window,
    "snapshot": snapshot,
    "export": export,
    "close_session": close_session,
    "evaluate_locks": evaluate_locks,
    "find_edges": find_edges,
    "wall_thickness": wall_thickness,
}


def _register_modules():
    """Phase 3 (editing.py) and Phase 4 (manufacturing.py) add their ops and methods here; reloading the engine
    reloads them too."""
    import importlib
    import sys
    from freecad_bridge import editing, manufacturing
    for m in (editing, manufacturing):
        importlib.reload(m)
        m.register(sys.modules[__name__])


_register_modules()


def dispatch(method, params):
    """Run an RPC method; returns a JSON-able response dict."""
    fn = METHODS.get(method)
    if fn is None:
        return {"ok": False, "error": f"Unknown method '{method}'"}
    try:
        return {"ok": True, "result": fn(**(params or {}))}
    except EngineError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        tb = traceback.format_exc()
        print(tb)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb[-4000:]}
