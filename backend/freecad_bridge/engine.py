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
  * Feature IDs (H#, C#, R#, P#, K#, S#) are matched geometrically across edits.
"""

import contextlib
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
KIND_PREFIX = {"hole": "H", "boss": "C", "round": "R", "plane": "P", "cone": "K", "sphere": "S", "face": "F"}
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


def _analyze(doc, registry):
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
    fmap = {f["id"]: f for f in feats}
    an = _Analysis(objs, feats, fmap, reg, shapes, tol, unknown)
    an.parts = parts
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
        "parametric": params,
        "sketches": sketches,
        "spreadsheets": sheets,
        "is_parametric": bool(params or sketches),
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

    Pushing out (d > 0) uses the face itself. Pulling in (d < 0) must also remove material inside
    the face's inner outlines that are air on the outside (e.g. the rim between a countersink and
    its hole), so it uses the outer boundary minus only those inner outlines that have material
    on the outside of the face (bosses standing on it), which must be kept.
    """
    if d > 0 or len(face.Wires) < 2:
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
            if _inside_any(shape, wf.CenterOfMass + n * eps, tol):
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


def _push_faces(ctx, moves, note):
    """Move planar faces along their outward normals in one edit. moves: [(plane, distance)].

    Holes, countersinks and counterbores that open on a moved face move with it (they are filled
    and re-cut relative to the face's new position), so blind depths are kept and nothing is buried.
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

    base = shape
    faces = [(shape.Faces[p["_faces"][0]], p, d) for p, d in moves]
    if shifts:
        base = _fill_holes(shape, [h for h, _ in shifts.values()])
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
    _set_shape(ctx.doc, obj, result, note)
    for p, d in moves:
        ctx.registry_updates[p["id"]] = {"d": p["_d"] + d, "c": _vl(p["_c"] + p["_n"] * d, 5)}
    for hid, (h, s) in shifts.items():
        ctx.registry_updates[hid] = {"t": [h["_t"][0] + s[0], h["_t"][1] + s[1]]}


AXIAL_KINDS = ("hole", "boss", "round", "cone")


def _dim_refs(dimension):
    if isinstance(dimension, (list, tuple)):
        refs = [str(x).strip().upper() for x in dimension]
    else:
        refs = [x.strip().upper() for x in re.split(r"\s*(?:-|↔|,|\s)\s*", str(dimension).strip()) if x.strip()]
    if len(refs) != 2:
        raise EngineError(f"A dimension is two feature IDs like 'P1-P2' or 'H3-P5', got {dimension!r}")
    return refs


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
        self.expect_new = None      # (kind, field, value): new features that should appear

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
    ctx.verify.append((h["id"], "diameter"))
    return f"{h['id']}: Ø{h['diameter']} → Ø{_r(d)} mm"


def op_move_hole(ctx, hole, offset=None, to=None):
    h = ctx.feature(hole, ("hole",))
    a, base = h["_a"], h["_base"]
    if to is not None:
        target = _v(to)
        delta = _perp_base(target, a) - base
    elif offset is not None:
        off = _v(offset)
        delta = off - a * off.dot(a)
    else:
        raise EngineError("Provide 'offset' [dx,dy,dz] or 'to' [x,y,z]")
    moved = _recut_holes(ctx, [(h, delta, None)], f"move {h['id']} by {_vl(delta)}")
    ctx.verify.append((h["id"], "center"))
    extra = [m for m in moved if m != h["id"]]
    return f"{h['id']} moved by {_vl(delta, 3)} mm" + (f" (with counterbore {', '.join(extra)})" if extra else "")


def op_set_dimension(ctx, dimension, value, anchor="start"):
    """Set a dimension between two features to an exact value.

    plane–plane : the faces move apart/together (anchor: start = first ID stays, end = second ID stays,
                  symmetric = both move half).
    hole–plane  : the hole moves (with its counterbore) so its axis is `value` from the face.
    hole–hole   : the holes move along the line joining them (anchor as above).
    """
    target = _num(value, "value")
    if target <= 0:
        raise EngineError("A dimension must be > 0")
    anchor = str(anchor or "start").lower()
    if anchor not in ("start", "end", "symmetric"):
        raise EngineError("anchor must be start, end or symmetric")
    ra, rb = _dim_refs(dimension)
    a, b = ctx.feature(ra), ctx.feature(rb)
    current = _dim_value(ctx.fmap, a["id"], b["id"])
    if current is None:
        raise EngineError(f"{a['id']} and {b['id']} don't define a measurable dimension (need parallel faces, "
                          "a hole and a face parallel to its axis, or two parallel holes)")
    did = f"{a['id']}-{b['id']}"
    if abs(target - current) < 1e-7:
        ctx.verify.append((did, "dimension"))
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
        u = V(w).normalize()
        specs = []
        if share[1]:
            specs.append((b, u * (delta * share[1]), None))
        if share[0]:
            specs.append((a, u * (-delta * share[0]), None))
        _recut_holes(ctx, specs, f"set {did} = {target}")
    ctx.verify.append((did, "dimension"))
    return f"{did}: {_r(current)} → {_r(target)} mm ({anchor})"


def op_edit_hole_pattern(ctx, pattern, pcd=None, count=None, spacing=None, spacing_u=None, spacing_v=None,
                         start_angle=None, anchor="center"):
    """Change a hole pattern: bolt-circle diameter / count / start angle, linear spacing / count,
    or grid spacings. Holes keep their size, depth, countersinks and counterbores."""
    pat = ctx.an.pattern(pattern)
    holes = [ctx.feature(h) for h in pat["holes"]]
    if count is not None:
        count = int(_num(count, "count"))
        if count < 1:
            raise EngineError("count must be >= 1")
        if pat["type"] == "grid":
            raise EngineError("Changing the number of holes in a grid isn't supported; change spacing_u / spacing_v")
    for name, val in (("pcd", pcd), ("spacing", spacing), ("spacing_u", spacing_u), ("spacing_v", spacing_v)):
        if val is not None and _num(val, name) <= 0:
            raise EngineError(f"{name} must be > 0")
    pts = analysis.pattern_positions(pat, pcd=pcd, count=count, spacing=spacing, spacing_u=spacing_u,
                                     spacing_v=spacing_v, start_angle=start_angle, anchor=anchor)
    a = holes[0]["_a"]
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
    ctx.verify.append((holes[0]["id"], "pattern"))
    changes = ", ".join(f"{k} {v}" for k, v in (("PCD", pcd), ("count", count), ("spacing", spacing),
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


def op_add_holes(ctx, face, diameter, positions=None, depth=None):
    d = _num(diameter, "diameter")
    if d <= 0:
        raise EngineError("diameter must be > 0")
    p = ctx.feature(face, ("plane",))
    obj = ctx.obj_of(p)
    shape = _gshape(obj)
    diag, tol = _tolerances(shape)
    fface = shape.Faces[p["_faces"][0]]
    n = p["_n"]
    pts = [_face_point(p, x) for x in positions] if positions else [p["_c"]]
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
        tools.append(Part.makeCylinder(d / 2, dep + margin, q + n * margin, n.negative()))
    tool = tools[0] if len(tools) == 1 else Part.makeCompound(tools)
    _set_shape(ctx.doc, obj, shape.cut(tool), f"add {len(pts)} hole(s) Ø{d} on {p['id']}")
    return f"Added {len(pts)} Ø{_r(d)} mm {'through' if through else f'{_r(dep)} mm deep'} hole(s) on {p['id']}"


def op_remove_features(ctx, features):
    if isinstance(features, str):
        features = [features]
    feats = [ctx.feature(x, ("hole", "boss", "round", "cone", "sphere", "plane")) for x in features]
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
            else:
                raise EngineError(f"FreeCAD could not remove {', '.join(f['id'] for f in group)} together; "
                                  "try removing them one at a time")
        _set_shape(ctx.doc, obj, result, "remove " + ", ".join(f["id"] for f in group))
        msgs.append(", ".join(f["id"] for f in group))
    return "Removed " + "; ".join(msgs)


def op_push_pull_face(ctx, face, distance):
    dist = _num(distance, "distance")
    if abs(dist) < 1e-9:
        return "No change (distance 0)"
    p = ctx.feature(face, ("plane",))
    _push_faces(ctx, [(p, dist)], f"push/pull {p['id']} {dist:+}")
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
    ctx.verify.append((p["id"], f"distance_to:{ref['id']}"))
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
    ctx.expect_new = ("round", "radius", rad)
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
        _set_shape(ctx.doc, obj, tool, f"replace with {shape}")
        return (f"Replaced the shape of {obj.Label} with a {shape}: volume {_r(base.Volume, 3)} → "
                f"{_r(tool.Volume, 3)} mm³, bbox {_bbox_dict(tool.BoundBox)['size']}")
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
    ctx.verify.append((f"{obj.Name}.{property}", "property"))
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
    ctx.verify.append((f"{sk.Name}#{idx}", "constraint"))
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
    "edit_hole_pattern": op_edit_hole_pattern,
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


def _measure_field(an, fid, field, doc=None):
    fmap = an.fmap
    if field == "property":
        objname, prop = fid.split(".", 1)
        val = getattr(doc.getObject(objname), prop)
        return {"id": fid, "field": field, "value": _r(val.Value) if hasattr(val, "Value") else val, "status": "ok"}
    if field == "constraint":
        objname, idx = fid.split("#", 1)
        c = doc.getObject(objname).Constraints[int(idx)]
        val = math.degrees(c.Value) if c.Type == "Angle" else c.Value
        return {"id": fid, "field": field, "value": _r(val), "status": "ok"}
    if field == "dimension":
        ra, rb = _dim_refs(fid)
        val = _dim_value(fmap, ra, rb)
        if val is None:
            return {"id": fid, "field": field, "status": "missing",
                    "note": "one of the faces/holes was not found after the edit"}
        return {"id": fid, "field": field, "value": _r(val), "status": "ok"}
    if field == "pattern":
        for p in an.patterns:
            if fid in p["holes"]:
                keys = ("pcd", "count", "angle_step_deg") if p["type"] == "circular" else \
                    ("spacing", "count") if p["type"] == "linear" else ("spacing_u", "spacing_v", "count")
                return {"id": p["id"], "field": field, "value": {k: p[k] for k in keys}, "status": "ok"}
        return {"id": fid, "field": field, "status": "missing", "note": "no pattern found containing this hole"}
    f = fmap.get(fid)
    if f is None:
        return {"id": fid, "field": field, "status": "missing",
                "note": "feature not found after the edit (it may have merged or been consumed)"}
    if field.startswith("distance_to:"):
        ref = fmap.get(field.split(":", 1)[1])
        if ref is None:
            return {"id": fid, "field": field, "status": "missing"}
        val = abs((f["_c"] - ref["_c"]).dot(f["_n"]))
        return {"id": fid, "field": field, "value": _r(val), "status": "ok"}
    return {"id": fid, "field": field, "value": f.get(field), "status": "ok"}


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


def _part_object(an, key):
    """Object name for a part ID (B1), or None."""
    return next((name for name, pid in an.parts.items() if pid == key), None)


def _resolve_refs(an, ids):
    """Features for a list of IDs; dimension IDs (P1-P2) and pattern IDs (PAT1) expand to their members.
    Part IDs (B1) are returned separately as object names: (features, missing, part object names)."""
    out, missing, parts = [], [], []
    for raw in ids or []:
        key = str(raw).strip().upper()
        if _part_object(an, key):
            parts.append(_part_object(an, key))
        elif key in an.fmap:
            out.append(an.fmap[key])
        elif key.startswith("PAT"):
            try:
                out.extend(an.fmap[h] for h in an.pattern(key)["holes"] if h in an.fmap)
            except EngineError:
                missing.append(raw)
        elif "-" in key:
            try:
                refs = _dim_refs(key)
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


def reload(session_id, work_path, registry=None):
    """Close and re-open the session document from disk (after undo/redo)."""
    _close_session(session_id, work_path)
    doc = _get_doc(session_id, work_path)
    _activate(doc, fit=False)
    an = _analyze(doc, registry)
    return {"info": _model_info(doc, an), "registry": an.reg}


def model_info(session_id, work_path, registry=None):
    """Re-analyse the document. `manual_edit` is true when the user edited it by hand in FreeCAD
    (the change is saved so the backend can record it as a version)."""
    doc = _get_doc(session_id, work_path)
    manual = _gui_modified(doc)
    if manual:
        doc.recompute()
        _save(doc)
    an = _analyze(doc, registry)
    return {"info": _model_info(doc, an), "registry": an.reg, "manual_edit": manual}


def apply(session_id, work_path, op, params=None, registry=None):
    if op not in OPS:
        raise EngineError(f"Unknown operation '{op}'. Available: {', '.join(OPS)}")
    params = params or {}
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
    before = {o.Name: _r(an.shapes[o.Name].Volume, 3) for o in an.objs}
    names_before = {o.Name for o in doc.Objects}
    invalid_before = _invalid_objects(doc)
    ctx = _Ctx(doc, an)

    doc.openTransaction(f"AetherCAD: {op}")
    try:
        message = OPS[op](ctx, **params)
        doc.recompute()
        newly_bad = _invalid_objects(doc) - invalid_before
        if newly_bad:
            raise EngineError(f"Recompute failed for: {', '.join(sorted(newly_bad))}")
        if not _solid_objects(doc) and op != "delete_object":
            raise EngineError("The edit left no visible solid geometry")
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
        message += ("\nNote: " + ", ".join(o.Label for o in converted) + " replaces a parametric object with a "
                    "plain solid; that object's parameters no longer drive the shown geometry.")
    reg = an.reg
    for fid, patch in ctx.registry_updates.items():
        if fid in reg["items"]:
            reg["items"][fid].update(patch)
    an2 = _analyze(doc, reg)
    after = {o.Name: _r(an2.shapes[o.Name].Volume, 3) for o in an2.objs}
    verified = [_measure_field(an2, fid, field, doc) for fid, field in ctx.verify]
    if ctx.expect_new:
        kind, field, value = ctx.expect_new
        new = [f for fid, f in an2.fmap.items() if fid not in an.fmap and f["kind"] == kind
               and abs(float(f.get(field, -1)) - value) < 1e-3]
        verified += [{"id": f["id"], "field": field, "value": f[field], "status": "ok"} for f in new]
        if not new:
            verified.append({"id": kind, "field": field, "status": "missing",
                             "note": f"no new {kind} with {field} {value} found after the edit"})
    _save(doc)
    _activate(doc)
    return {
        "message": message,
        "verified": verified,
        "volume_before": before,
        "volume_after": after,
        "info": _model_info(doc, an2),
        "registry": an2.reg,
    }


def find_features(session_id, work_path, registry=None, kind=None, diameter_min=None, diameter_max=None,
                  normal=None, near=None, object=None, limit=50):
    doc = _get_doc(session_id, work_path)
    an = _analyze(doc, registry)
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
}


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
