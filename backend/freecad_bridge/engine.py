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

KIND_PREFIX = {"hole": "H", "boss": "C", "round": "R", "plane": "P", "cone": "K", "sphere": "S"}


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

    for i, f in enumerate(shape.Faces):
        s = f.Surface
        try:
            u0, u1, v0, v1 = f.ParameterRange
            um, vm = (u0 + u1) / 2.0, (v0 + v1) / 2.0
        except Exception:
            continue

        if isinstance(s, Part.Cylinder):
            a = _canon_axis(s.Axis)
            c = V(s.Center)
            r = float(s.Radius)
            p = f.valueAt(um, vm)
            n = f.normalAt(um, vm)
            w = p - c
            radial = w - a * w.dot(a)
            concave = n.dot(radial) < 0
            ts = [vx.Point.dot(a) for vx in f.Vertexes] or [
                V(x, y, z).dot(a) for x in (f.BoundBox.XMin, f.BoundBox.XMax)
                for y in (f.BoundBox.YMin, f.BoundBox.YMax) for z in (f.BoundBox.ZMin, f.BoundBox.ZMax)]
            base = _perp_base(c, a)
            entry = (i, min(ts), max(ts), abs(u1 - u0))
            for g in cyl_groups:
                if (g["concave"] == concave and abs(g["r"] - r) < tol and abs(g["a"].dot(a)) > 1 - 1e-7
                        and (g["base"] - base).Length < tol):
                    g["faces"].append(entry)
                    break
            else:
                cyl_groups.append({"concave": concave, "a": a, "base": base, "r": r, "faces": [entry]})

        elif isinstance(s, Part.Plane):
            n = f.normalAt(um, vm)
            c = f.CenterOfMass
            planes.append({"i": i, "n": n, "c": c, "area": f.Area, "d": n.dot(c), "bb": f.BoundBox})

        elif isinstance(s, Part.Cone):
            a = _canon_axis(s.Axis)
            ts = [vx.Point.dot(a) for vx in f.Vertexes]
            cbase = _perp_base(V(s.Apex), a)
            radii = [(vx.Point - a * vx.Point.dot(a) - cbase).Length for vx in f.Vertexes]
            cones.append({"i": i, "a": a, "base": cbase, "apex": V(s.Apex),
                          "semi": abs(float(s.SemiAngle)), "t0": min(ts) if ts else 0.0,
                          "t1": max(ts) if ts else 0.0, "area": f.Area, "rmax": max(radii) if radii else 0.0})

        elif isinstance(s, Part.Sphere):
            feats.append({"kind": "sphere", "_obj": objname, "_faces": [i], "radius": _r(s.Radius),
                          "center": _vl(V(s.Center)), "_c": V(s.Center)})

        elif isinstance(s, Part.Toroid):
            a = _canon_axis(s.Axis)
            feats.append({"kind": "round", "_obj": objname, "_faces": [i], "radius": _r(s.MinorRadius),
                          "torus_major_radius": _r(s.MajorRadius), "axis": _vl(a), "center": _vl(V(s.Center)),
                          "_a": a, "_base": _perp_base(V(s.Center), a), "_t": (V(s.Center).dot(a),) * 2,
                          "_r": float(s.MinorRadius)})

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
                feats.append(hole)
            else:
                caps = [idx for kind, idx in caps0 + caps1 if kind == "plane"]
                for idx in caps:
                    used_planes.add(idx)
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
    return feats


def _descriptor(f):
    k = f["kind"]
    if k == "plane":
        return {"k": k, "n": _vl(f["_n"], 7), "d": f["_d"], "c": _vl(f["_c"], 5)}
    if k == "sphere":
        return {"k": k, "c": _vl(f["_c"], 5), "r": f["radius"]}
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


def _analyze(doc, registry):
    objs = _solid_objects(doc)
    feats = []
    tol = 1e-3
    for o in objs:
        shape = _gshape(o)
        _, t = _tolerances(shape)
        tol = max(tol, t)
        feats.extend(_features_of_shape(o.Name, shape))
    reg = _assign_ids(feats, registry, tol)
    fmap = {f["id"]: f for f in feats}
    return objs, feats, fmap, reg


def _public(f):
    out = {k: v for k, v in f.items() if not k.startswith("_")}
    out["object"] = f["_obj"]
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


def _model_info(doc, objs, feats):
    gbb = None
    objects = []
    for o in objs:
        s = _gshape(o)
        bb = s.BoundBox
        gbb = bb if gbb is None else gbb.united(bb)
        try:
            parent = o.getParentGeoFeatureGroup()
        except Exception:
            parent = None
        objects.append({"name": o.Name, "label": o.Label, "type": o.TypeId, "volume": _r(s.Volume, 3),
                        **({"parent": parent.Label} if parent is not None else {}),
                        "area": _r(s.Area, 3), "solids": len(s.Solids), "faces": len(s.Faces),
                        "edges": len(s.Edges), "bbox": _bbox_dict(bb),
                        "center_of_mass": _vl(s.Solids[0].CenterOfMass if len(s.Solids) == 1 else bb.Center, 3)})
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
    def __init__(self, doc, fmap, registry):
        self.doc = doc
        self.fmap = fmap
        self.registry = registry
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
    obj = ctx.obj_of(h)
    shape = _gshape(obj)
    diag, _ = _tolerances(shape)
    filled = _fill_hole(shape, h)
    open0, open1 = h["_open"]
    tool = _hole_tool(h["_a"], h["_base"], h["_t"][0], h["_t"][1], d / 2, open0, open1, diag, _tip_semi(shape, h))
    for cone in _countersink_tools(h, h["_base"], d / 2, max(diag * 0.02, 1.0)):
        tool = tool.fuse(cone)
    _set_shape(ctx.doc, obj, filled.cut(tool), f"resize {h['id']} to Ø{d}")
    ctx.registry_updates[h["id"]] = {"r": d / 2}
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
    obj = ctx.obj_of(h)
    shape = _gshape(obj)
    diag, _ = _tolerances(shape)
    filled = _fill_hole(shape, h)
    open0, open1 = h["_open"]
    new_base = base + delta
    tool = _hole_tool(a, new_base, h["_t"][0], h["_t"][1], h["_r"], open0, open1, diag, _tip_semi(shape, h))
    for cone in _countersink_tools(h, new_base, h["_r"], max(diag * 0.02, 1.0)):
        tool = tool.fuse(cone)
    _set_shape(ctx.doc, obj, filled.cut(tool), f"move {h['id']} by {_vl(delta)}")
    ctx.registry_updates[h["id"]] = {"b": _vl(new_base, 5)}
    ctx.verify.append((h["id"], "center"))
    return f"{h['id']} moved by {_vl(delta, 3)} mm"


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
    pts = [_v(x) for x in positions] if positions else [p["_c"]]
    through = depth is None or depth == "through"
    dep = diag * 2 if through else _num(depth, "depth")
    margin = max(diag * 0.02, 1.0)
    tools = []
    for pt in pts:
        q = pt - n * (pt - p["_c"]).dot(n)
        if not fface.isInside(q, tol * 10, True):
            raise EngineError(f"Position {_vl(q, 3)} is not on face {p['id']} (face bbox {p['bbox']})")
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
    obj = ctx.obj_of(p)
    shape = _gshape(obj)
    f = shape.Faces[p["_faces"][0]]
    prism = f.extrude(p["_n"] * dist)
    result = shape.fuse(prism) if dist > 0 else shape.cut(prism)
    _set_shape(ctx.doc, obj, result, f"push/pull {p['id']} {dist:+}")
    ctx.registry_updates[p["id"]] = {"d": p["_d"] + dist, "c": _vl(p["_c"] + p["_n"] * dist, 5)}
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
    pl = obj.Placement
    if rotate:
        axis = _axis_vec(rotate.get("axis", "Z"))
        ang = _num(rotate.get("angle", 0), "angle")
        center = _v(rotate["center"]) if rotate.get("center") is not None else _gshape(obj).BoundBox.Center
        pl = App.Placement(V(), App.Rotation(axis, ang), center).multiply(pl)
    if translate:
        pl = App.Placement(_v(translate), App.Rotation()).multiply(pl)
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


def _measure_field(fmap, fid, field, doc=None):
    if field == "property":
        objname, prop = fid.split(".", 1)
        val = getattr(doc.getObject(objname), prop)
        return {"id": fid, "field": field, "value": _r(val.Value) if hasattr(val, "Value") else val, "status": "ok"}
    if field == "constraint":
        objname, idx = fid.split("#", 1)
        c = doc.getObject(objname).Constraints[int(idx)]
        val = math.degrees(c.Value) if c.Type == "Angle" else c.Value
        return {"id": fid, "field": field, "value": _r(val), "status": "ok"}
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
    _activate(doc, fit=True)
    objs, feats, fmap, reg = _analyze(doc, registry)
    return {"info": _model_info(doc, objs, feats), "registry": reg}


def reload(session_id, work_path, registry=None):
    """Close and re-open the session document from disk (after undo/redo)."""
    _close_session(session_id, work_path)
    doc = _get_doc(session_id, work_path)
    _activate(doc, fit=False)
    objs, feats, fmap, reg = _analyze(doc, registry)
    return {"info": _model_info(doc, objs, feats), "registry": reg}


def model_info(session_id, work_path, registry=None):
    doc = _get_doc(session_id, work_path)
    objs, feats, fmap, reg = _analyze(doc, registry)
    return {"info": _model_info(doc, objs, feats), "registry": reg}


def apply(session_id, work_path, op, params=None, registry=None):
    if op not in OPS:
        raise EngineError(f"Unknown operation '{op}'. Available: {', '.join(OPS)}")
    params = params or {}
    doc = _get_doc(session_id, work_path)
    objs, feats, fmap, reg = _analyze(doc, registry)
    before = {o.Name: _r(_gshape(o).Volume, 3) for o in objs}
    names_before = {o.Name for o in doc.Objects}
    invalid_before = _invalid_objects(doc)
    ctx = _Ctx(doc, fmap, reg)

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
    for fid, patch in ctx.registry_updates.items():
        if fid in reg["items"]:
            reg["items"][fid].update(patch)
    objs2, feats2, fmap2, reg2 = _analyze(doc, reg)
    after = {o.Name: _r(_gshape(o).Volume, 3) for o in objs2}
    verified = [_measure_field(fmap2, fid, field, doc) for fid, field in ctx.verify]
    if ctx.expect_new:
        kind, field, value = ctx.expect_new
        new = [f for fid, f in fmap2.items() if fid not in fmap and f["kind"] == kind
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
        "info": _model_info(doc, objs2, feats2),
        "registry": reg2,
    }


def find_features(session_id, work_path, registry=None, kind=None, diameter_min=None, diameter_max=None,
                  normal=None, near=None, object=None, limit=50):
    doc = _get_doc(session_id, work_path)
    objs, feats, fmap, reg = _analyze(doc, registry)
    out = []
    nvec = _axis_vec(normal) if normal is not None else None
    for f in feats:
        if kind and f["kind"] != kind:
            continue
        if object and f["_obj"] != object:
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
    return {"features": [_public(f) for f in out[: int(limit or 50)]], "total": len(out), "registry": reg}


def measure(session_id, work_path, a, b, registry=None):
    doc = _get_doc(session_id, work_path)
    objs, feats, fmap, reg = _analyze(doc, registry)

    def to_shape(ref):
        if isinstance(ref, (list, tuple)):
            return Part.Vertex(_v(ref)), None
        f = fmap.get(str(ref).upper())
        if f is not None:
            shape = _gshape(doc.getObject(f["_obj"]))
            return Part.makeCompound([shape.Faces[i] for i in f["_faces"]]), f
        return _gshape(_get_obj(doc, ref)), None

    sa, fa = to_shape(a)
    sb, fb = to_shape(b)
    dist, pairs, _ = sa.distToShape(sb)
    out = {"min_distance": _r(dist), "closest_points": [[_vl(p, 3), _vl(q, 3)] for p, q in pairs[:2]], "registry": reg}
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
    objs, feats, fmap, reg = _analyze(doc, registry)
    info = _model_info(doc, objs, [])
    rows = info["objects"]
    if name_contains:
        q = str(name_contains).lower()
        rows = [o for o in rows if q in o["name"].lower() or q in o["label"].lower()
                or q in o.get("parent", "").lower()]
    rows.sort(key=lambda o: -o["volume"])
    out = [{"name": o["name"], "label": o["label"], **({"parent": o["parent"]} if "parent" in o else {}),
            "volume": o["volume"], "bbox": o["bbox"]} for o in rows]
    return {"objects": out, "total": len(out), "overall_bbox": info["bbox"], "registry": reg}


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
    "measure": measure,
    "list_objects": list_objects,
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
