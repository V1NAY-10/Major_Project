"""
Phase 3 editing operations and version comparison — runs INSIDE FreeCAD, registered into engine.OPS / METHODS.

  mirror_object      mirror a whole part about a plane (copy / replace / fuse into a symmetric part)
  mirror_features    mirror holes, slots, pockets or bosses about a plane
  pattern_feature    linear or circular pattern of a hole, slot, pocket or boss
  add_slot           cut a new obround / rectangular slot into a planar face
  add_pocket         cut a new rectangular pocket (optional corner radius)
  add_counterbore    add a counterbore to an existing hole        (→ set_hole_profile)
  add_countersink    add a countersink to an existing hole        (→ set_hole_profile)
  draft_faces        tilt faces by a draft angle about their edge with a neutral face
  split_object       split a part with a plane (keep both halves, or one)
  shell_object       hollow a part to a wall thickness, optionally opening faces
  compare_versions   geometry difference between two saved versions (added / removed material)

Every op follows the engine's rules: it works on global shapes, writes back with _set_shape, records what it promised
to produce in ctx.verify / ctx.expect_new / ctx.expect_counts, and raises EngineError for user-facing failures; the
engine's transaction, verification, pre-edit checks and rollback apply unchanged.
"""

import re
import math

import FreeCAD as App  # type: ignore
import Part  # type: ignore

V = App.Vector
E = None  # the engine module, set by register()


def register(engine):
    global E
    E = engine
    engine.OPS.update({
        "mirror_object": op_mirror_object, "mirror_features": op_mirror_features, "pattern_feature": op_pattern_feature,
        "add_slot": op_add_slot, "add_pocket": op_add_pocket, "add_counterbore": op_add_counterbore,
        "add_countersink": op_add_countersink, "draft_faces": op_draft_faces, "split_object": op_split_object,
        "shell_object": op_shell_object,
    })
    engine.METHODS.update({"compare_versions": compare_versions})
    for _name, _fn in list(engine.METHODS.items()):
        if not hasattr(engine, _name):
            setattr(engine, _name, _fn)  # callable as engine.<method> like the built-in ones


# ── planes ───────────────────────────────────────────────────────────────────

def _plane(ctx, plane, at=None, shape=None):
    """(point, unit normal) of a plane given as a planar face ID (P3), an axis plane name ('YZ', 'XZ', 'XY' — through
    the part's centre, or at the coordinate `at` along the plane's normal), 'y=20' (= XZ at 20), or
    {point: [..], normal: [..]}."""
    if isinstance(plane, dict):
        return E._v(plane.get("point", [0, 0, 0])), E._axis_vec(plane.get("normal", "Z"))
    s = str(plane or "").strip().upper().replace(" ", "")
    m = re.fullmatch(r"([XYZ])(?:=(-?[\d.]+))?", s)  # "y=20" / "Y": the plane where that coordinate is constant
    if m:
        s, at = {"X": "YZ", "Y": "XZ", "Z": "XY"}[m.group(1)], (float(m.group(2)) if m.group(2) else at)
    names = {"YZ": V(1, 0, 0), "ZY": V(1, 0, 0), "XZ": V(0, 1, 0), "ZX": V(0, 1, 0), "XY": V(0, 0, 1), "YX": V(0, 0, 1)}
    if s in names:
        n = names[s]
        c = shape.BoundBox.Center if shape is not None else V()
        if at is not None:
            c = c + n * (E._num(at, "at") - c.dot(n))
        return c, n
    f = ctx.feature(s, ("plane",))
    return f["_c"], f["_n"]


# ── feature tools (the solid a feature adds or removes) ─────────────────────

TOOL_KINDS = ("hole", "slot", "pocket", "boss")


def feature_tool(shape, f):
    """(solid, 'cut' | 'fuse') that recreates feature f at its own place."""
    diag, tol = E._tolerances(shape)
    m = max(diag * 0.02, 1.0)
    k = f["kind"]
    if k == "hole":
        tool = None
        for g in sorted(E._with_partner(f), key=lambda g: -g["_r"]):
            c = E._hole_cutter(shape, g, g["_base"], g["_r"], diag)
            tool = c if tool is None else tool.fuse(c)
        return tool, "cut"
    if k == "slot":
        return E._slot_solid(f["_c"], f["_n"], f["_dir"], f["_width"], f["_length"], f["_depth"], f["shape"],
                             above=m, below=m if f["through"] else 0.0), "cut"
    if k == "pocket":
        floor = shape.Faces[f["_floor"]]
        prism = Part.Face(floor.OuterWire).extrude(f["_n"] * (f["_depth"] + m))
        return prism, "cut"
    if k == "boss":
        a, base = f["_a"], f["_base"]
        t0, t1 = f["_t"]
        free_t = f["_free_t"]
        lo, hi = (t0, t1)
        # reach slightly into the body at the base end so the copy fuses cleanly
        if free_t == t1:
            lo -= tol * 10
        else:
            hi += tol * 10
        return Part.makeCylinder(f["_r"], hi - lo, base + a * lo, a), "fuse"
    raise E.EngineError(f"{f['id']} is a {k}; only holes, slots, pockets and bosses can be mirrored or patterned")


def _ref_point(f):
    """A point that places a feature: the opening centre of a slot / pocket, the axis midpoint of a hole / boss."""
    if f["kind"] in ("slot", "pocket"):
        return f["_c"]
    return f["_base"] + f["_a"] * ((f["_t"][0] + f["_t"][1]) / 2)


def _apply_tools(base, tools):
    """Fuse the additive tools, then cut the subtractive ones."""
    result = base
    for t, how in tools:
        if how == "fuse":
            result = result.fuse(t)
    cuts = [t for t, how in tools if how == "cut"]
    if cuts:
        result = result.cut(cuts[0] if len(cuts) == 1 else Part.makeCompound(cuts))
    return result


def _expect_more(ctx, kind, n):
    counts = getattr(ctx, "expect_counts", None) or {}
    counts[kind] = counts.get(kind, 0) + n
    ctx.expect_counts = counts


# ── mirror ───────────────────────────────────────────────────────────────────

def op_mirror_object(ctx, object=None, plane="YZ", at=None, mode="copy"):
    """Mirror a whole part. mode: copy (a new mirrored part), replace (the part becomes its mirror image),
    fuse (part + mirror image: a symmetric part; the plane should touch the part)."""
    obj = E._get_obj(ctx.doc, object)
    shape = E._gshape(obj)
    p, n = _plane(ctx, plane, at, shape)
    mir = shape.mirror(p, n)
    mode = str(mode or "copy").lower()
    if mode == "copy":
        new = ctx.doc.addObject("Part::Feature", "Mirror")
        new.Label = f"{obj.Label} (mirror)"
        new.Shape = mir
        bb = mir.BoundBox
        ctx.verify.append((new.Name, "bbox_min", [bb.XMin, bb.YMin, bb.ZMin]))
        return f"Mirrored {obj.Label} about the plane through {E._vl(p, 3)} ⟂ {E._vl(n, 3)} into a new part ({new.Label})"
    if mode == "replace":
        E._set_shape(ctx.doc, obj, mir, f"mirror about {E._vl(n, 3)}")
        bb = mir.BoundBox
        ctx.verify.append((obj.Name, "bbox_min", [bb.XMin, bb.YMin, bb.ZMin]))
        return f"{obj.Label} replaced by its mirror image"
    if mode == "fuse":
        res = shape.fuse(mir)
        if len(E._clean(res).Solids) > 1:
            raise E.EngineError("The mirror image doesn't touch the part (the result would be two separate solids); "
                                "put the plane on a face of the part or use mode 'copy'")
        E._set_shape(ctx.doc, obj, res, f"mirror-fuse about {E._vl(n, 3)}")
        bb = res.BoundBox
        ctx.verify.append((obj.Name, "bbox_size", [bb.XLength, bb.YLength, bb.ZLength]))
        return f"{obj.Label} made symmetric about the plane through {E._vl(p, 3)}"
    raise E.EngineError("mode must be copy, replace or fuse")


def op_mirror_features(ctx, features, plane="YZ", at=None):
    """Mirror holes, slots, pockets and bosses of one part about a plane (the originals stay)."""
    if isinstance(features, str):
        features = [features]
    feats = [ctx.feature(x, TOOL_KINDS) for x in features]
    if len({f["_obj"] for f in feats}) != 1:
        raise E.EngineError("Mirror features of one part at a time")
    obj = ctx.obj_of(feats[0])
    shape = E._gshape(obj)
    p, n = _plane(ctx, plane, at, shape)
    tools = []
    for f in feats:
        t, how = feature_tool(shape, f)
        tools.append((t.mirror(p, n), how))
        _expect_more(ctx, f["kind"], 1)
    E._set_shape(ctx.doc, obj, _apply_tools(shape, tools), "mirror " + ", ".join(f["id"] for f in feats))
    return f"Mirrored {', '.join(f['id'] for f in feats)} about the plane through {E._vl(p, 3)} ⟂ {E._vl(n, 3)}"


# ── patterns of any feature ──────────────────────────────────────────────────

def op_pattern_feature(ctx, feature, type="linear", count=2, spacing=None, direction="X", axis="Z", center=None,
                       angle=360.0):
    """Copy a hole, slot, pocket or boss: linear (count instances `spacing` apart along `direction`, the original
    being the first) or circular (count instances over `angle` degrees about `axis` through `center`; 360 = full
    circle). count includes the original."""
    f = ctx.feature(feature, TOOL_KINDS)
    count = int(E._num(count, "count"))
    if count < 2:
        raise E.EngineError("count must be at least 2 (it includes the original)")
    obj = ctx.obj_of(f)
    shape = E._gshape(obj)
    tool, how = feature_tool(shape, f)
    ref = _ref_point(f)
    moves = []  # functions placing a copy (applied to the tool and to the reference point)
    if str(type).lower() == "linear":
        if spacing is None:
            raise E.EngineError("A linear pattern needs spacing")
        d = E._axis_vec(direction)
        s_ = E._num(spacing, "spacing")
        for k in range(1, count):
            moves.append(("translate", d * (s_ * k)))
        desc = f"{count} × along {E._vl(d, 3)} every {E._r(s_)} mm"
    elif str(type).lower() == "circular":
        a = E._axis_vec(axis)
        c = E._v(center) if center is not None else shape.BoundBox.Center
        total = E._num(angle, "angle")
        step = total / count if abs(abs(total) - 360) < 1e-9 else total / (count - 1)
        for k in range(1, count):
            moves.append(("rotate", (c, a, step * k)))
        desc = f"{count} × every {E._r(step)}° about {E._vl(a, 3)} through {E._vl(c, 3)}"
    else:
        raise E.EngineError("type must be linear or circular")
    _, tol = E._tolerances(shape)
    same = [g for g in ctx.an.feats if g["kind"] == f["kind"] and g["_obj"] == f["_obj"]
            and abs(g.get("_r", 0) - f.get("_r", 0)) < tol * 10]
    copies, skipped = [], 0
    for kind_, arg in moves:
        t = tool.copy()
        p = V(ref)
        if kind_ == "translate":
            t.translate(arg)
            p = p + arg
        else:
            t.rotate(*arg)
            p = App.Rotation(arg[1], arg[2]).multVec(p - arg[0]) + arg[0]
        if any((_ref_point(g) - p).Length < tol * 10 for g in same):
            skipped += 1  # an identical feature is already there
            continue
        if not shape.BoundBox.intersect(t.BoundBox):
            raise E.EngineError("A copy of the pattern lies completely outside the part; reduce the spacing / count")
        copies.append((t, how))
    if not copies:
        raise E.EngineError(f"Every position of this pattern already has a {f['kind']} like {f['id']}")
    _expect_more(ctx, f["kind"], len(copies))
    E._set_shape(ctx.doc, obj, _apply_tools(shape, copies), f"pattern {f['id']} {desc}")
    if skipped:
        desc += f" ({skipped} position(s) already had one)"
    return f"{f['id']} patterned: {desc}"


# ── new slots, pockets, counterbores, countersinks ───────────────────────────

def _centre_on_face(ctx, p, center):
    if center is None:
        return p["_c"]
    if isinstance(center, dict):
        return E._datum_point(ctx.an, center, p["_n"], p["_n"].dot(p["_c"]), p["_c"])
    return E._face_point(p, center)


def _in_plane_dir(direction, n):
    if isinstance(direction, (int, float)):  # an angle (degrees) from the face's first in-plane axis
        from freecad_bridge import analysis
        e1, e2 = analysis.basis(n)
        a = math.radians(float(direction))
        return e1 * math.cos(a) + e2 * math.sin(a)
    d = E._axis_vec(direction)
    d = d - n * d.dot(n)
    if d.Length < 1e-9:
        raise E.EngineError("The slot direction is perpendicular to the face")
    return d.normalize()


def _outline_on_face(fface, centre, d, n, length, width, tol):
    """The slot / pocket outline's corners and end points must all lie on the face."""
    w = n.cross(d)
    pts = [centre + d * (sx * length / 2) + w * (sy * width / 2) for sx in (-1, 1) for sy in (-1, 1)]
    pts += [centre + d * (sx * length / 2) for sx in (-1, 1)] + [centre]
    bad = [q for q in pts if not fface.isInside(q, tol * 10, True)]
    if bad:
        raise E.EngineError(f"The outline runs off face (e.g. {E._vl(bad[0], 3)}); move it or make it smaller")


def op_add_slot(ctx, face, width, length, depth=None, center=None, direction="X", shape="obround"):
    """Cut a slot into a planar face: obround or rectangular, `depth` deep (through when omitted), centred at
    `center` ([x, y] / [x, y, z] / datum distances {B: .., C: ..}; default the face centre), its length along
    `direction` (an axis, a vector, or an angle in degrees)."""
    p = ctx.feature(face, ("plane",))
    obj = ctx.obj_of(p)
    sh = E._gshape(obj)
    diag, tol = E._tolerances(sh)
    w, L = E._num(width, "width"), E._num(length, "length")
    if w <= 0 or L <= 0:
        raise E.EngineError("width and length must be > 0")
    outline = str(shape or "obround").lower()
    if outline == "obround" and L <= w:
        raise E.EngineError("An obround slot must be longer than it is wide")
    n = p["_n"]
    c = _centre_on_face(ctx, p, center)
    c = c - n * (c - p["_c"]).dot(n)
    d = _in_plane_dir(direction, n)
    _outline_on_face(sh.Faces[p["_faces"][0]], c, d, n, L, w, tol)
    through = depth is None or depth == "through"
    dep = diag * 2 if through else E._num(depth, "depth")
    if dep <= 0:
        raise E.EngineError("depth must be > 0")
    m = max(diag * 0.02, 1.0)
    cutter = E._slot_solid(c, n, d, w, L, dep, outline, above=m, below=m if through else 0.0)
    E._set_shape(ctx.doc, obj, sh.cut(cutter), f"add slot {E._r(w)}×{E._r(L)} on {p['id']}")
    ctx.expect_new = ("slot", "width", w, 1)
    return f"Added a {outline} slot {E._r(w)} × {E._r(L)} mm, {'through' if through else f'{E._r(dep)} mm deep'}, on {p['id']}"


def op_add_pocket(ctx, face, length, width, depth, center=None, direction="X", corner_radius=0.0):
    """Cut a rectangular pocket into a planar face, `depth` deep, with vertical corners of `corner_radius`
    (0 = sharp; a sharp pocket at least twice as long as wide is recognised as a rectangular slot)."""
    p = ctx.feature(face, ("plane",))
    obj = ctx.obj_of(p)
    sh = E._gshape(obj)
    diag, tol = E._tolerances(sh)
    L, w, dep = E._num(length, "length"), E._num(width, "width"), E._num(depth, "depth")
    r = E._num(corner_radius or 0, "corner_radius")
    if min(L, w, dep) <= 0 or r < 0 or 2 * r >= min(L, w):
        raise E.EngineError("length, width and depth must be > 0 and the corner radius less than half the width")
    n = p["_n"]
    c = _centre_on_face(ctx, p, center)
    c = c - n * (c - p["_c"]).dot(n)
    d = _in_plane_dir(direction, n)
    _outline_on_face(sh.Faces[p["_faces"][0]], c, d, n, L, w, tol)
    m = max(diag * 0.02, 1.0)
    box = Part.makeBox(L, w, dep + m, V(-L / 2, -w / 2, -dep))
    if r > 0:
        vert = [e for e in box.Edges if len(e.Vertexes) == 2
                and abs(e.Vertexes[0].X - e.Vertexes[1].X) < 1e-9 and abs(e.Vertexes[0].Y - e.Vertexes[1].Y) < 1e-9]
        box = box.makeFillet(r, vert)
    box.Placement = E._frame(c, d, n)
    probe = c - n * (dep + max(tol * 20, 0.01))
    if not E._inside_any(sh, probe + d * (L / 4), tol):
        raise E.EngineError(f"A {E._r(dep)} mm deep pocket would break through the part")
    E._set_shape(ctx.doc, obj, sh.cut(box), f"add pocket {E._r(L)}×{E._r(w)}×{E._r(dep)} on {p['id']}")
    kind = "slot" if r == 0 and max(L, w) >= 2 * min(L, w) else "pocket"
    ctx.expect_new = (kind, "depth", dep, 1)
    return f"Added a {E._r(L)} × {E._r(w)} × {E._r(dep)} mm pocket on {p['id']}" + (f", corners R{E._r(r)}" if r else "")


def op_add_counterbore(ctx, hole, diameter, depth):
    """Add (or resize) a counterbore at the hole's entry."""
    return E.op_set_hole_profile(ctx, hole, counterbore_diameter=diameter, counterbore_depth=depth, remove_countersink=True)


def op_add_countersink(ctx, hole, diameter, angle=90.0):
    """Add (or resize) a countersink at the hole's entry."""
    return E.op_set_hole_profile(ctx, hole, countersink_diameter=diameter, countersink_angle=angle, remove_counterbore=True)


# ── draft ────────────────────────────────────────────────────────────────────

def _reanalyze(ctx):
    """Re-analyse after a step of a multi-step op so the next step sees the current faces (IDs kept via patches)."""
    import copy
    reg = copy.deepcopy(ctx.an.reg)
    for fid, patch in ctx.registry_updates.items():
        if fid in reg["items"]:
            reg["items"][fid].update(patch)
    ctx.an = E._analyze(ctx.doc, reg)
    ctx.fmap = ctx.an.fmap
    ctx.registry = ctx.an.reg


def op_draft_faces(ctx, faces, angle, neutral):
    """Tilt each face by `angle` degrees about the edge it shares with the `neutral` face, so the part narrows away
    from the neutral face (positive angle; negative widens it). A vertical wall on a horizontal base becomes
    90° − angle to it."""
    if isinstance(faces, str):
        faces = [faces]
    a = E._num(angle, "angle")
    if not 0 < abs(a) < 45:
        raise E.EngineError("A draft angle must be between 0 and 45° (positive narrows the part away from the neutral face)")
    msgs = []
    for fid in faces:
        nf = ctx.feature(neutral, ("plane",))
        f = ctx.feature(fid, ("plane",))
        current = E.analysis.included_angle(nf["_n"], f["_n"])
        msgs.append(E._set_angle(ctx, nf, f, current - a, "start"))
        _reanalyze(ctx)
    return f"Draft {E._r(a)}° on {', '.join(str(x).upper() for x in faces)} (neutral {str(neutral).upper()}): " + "; ".join(msgs)


# ── split, shell ─────────────────────────────────────────────────────────────

def op_split_object(ctx, object=None, plane="XY", at=None, keep="both"):
    """Split a part with a plane: keep both halves (the part keeps the side the plane's normal points to, a new part
    gets the other) or only the 'positive' / 'negative' side."""
    obj = E._get_obj(ctx.doc, object)
    shape = E._gshape(obj)
    p, n = _plane(ctx, plane, at, shape)
    big = shape.BoundBox.DiagonalLength * 4 + 10
    pos = shape.common(E._half_space(p, n.negative(), big))
    neg = shape.common(E._half_space(p, n, big))
    if pos.isNull() or not pos.Solids or neg.isNull() or not neg.Solids or pos.Volume < 1e-9 or neg.Volume < 1e-9:
        raise E.EngineError("The plane doesn't cut through the part")
    keep = str(keep or "both").lower()
    if keep == "both":
        E._set_shape(ctx.doc, obj, pos, f"split, kept + side of {E._vl(n, 3)}")
        new = ctx.doc.addObject("Part::Feature", "Split")
        new.Label = f"{obj.Label} (split)"
        new.Shape = E._clean(neg)
        msg = f"Split {obj.Label}: {E._r(pos.Volume, 1)} mm³ stays, {E._r(neg.Volume, 1)} mm³ is the new part {new.Label}"
    elif keep in ("positive", "negative"):
        E._set_shape(ctx.doc, obj, pos if keep == "positive" else neg, f"split, kept {keep} side")
        msg = f"Cut {obj.Label} with the plane; kept the {keep} side ({E._r((pos if keep == 'positive' else neg).Volume, 1)} mm³)"
    else:
        raise E.EngineError("keep must be both, positive or negative")
    return msg


def op_shell_object(ctx, thickness, object=None, open_faces=None):
    """Hollow a part to a constant wall `thickness` (inwards). open_faces: planar face IDs left open (e.g. the top of a
    box); none gives a closed hollow part."""
    obj = E._get_obj(ctx.doc, object)
    shape = E._gshape(obj)
    t = E._num(thickness, "thickness")
    if t <= 0:
        raise E.EngineError("thickness must be > 0")
    _, tol = E._tolerances(shape)
    faces = []
    for fid in open_faces or []:
        f = ctx.feature(fid)
        if f["_obj"] != obj.Name:
            raise E.EngineError(f"{fid} is not a face of {obj.Label}")
        faces += [shape.Faces[i] for i in f["_faces"]]
    try:
        if faces:
            res = shape.makeThickness(faces, -t, tol)
        else:
            res = shape.cut(shape.makeOffsetShape(-t, tol, fill=True))
    except Exception as e:
        raise E.EngineError(f"Shell {E._r(t)} mm failed: {e}. Try a smaller thickness")
    if res.isNull() or not res.Solids or res.Volume >= shape.Volume - 1e-6:
        raise E.EngineError(f"Shell {E._r(t)} mm produced no hollow; the part may be too thin for it")
    E._set_shape(ctx.doc, obj, res, f"shell {t}")
    return f"Shelled {obj.Label} to {E._r(t)} mm walls" + (f", open at {', '.join(str(x).upper() for x in open_faces)}" if open_faces else "") + \
        f": volume {E._r(shape.Volume, 1)} → {E._r(res.Volume, 1)} mm³"


# ── version comparison ───────────────────────────────────────────────────────

def _doc_shapes(path):
    doc = App.openDocument(path, hidden=True)
    try:
        return {o.Name: (o.Label, E._gshape(o)) for o in E._solid_objects(doc)}
    finally:
        App.closeDocument(doc.Name)


def _tess(shape, defl):
    pos, idx = [], []
    for f in shape.Faces:
        try:
            pts, tris = f.tessellate(defl)
        except Exception:
            continue
        off = len(pos) // 3
        for p in pts:
            pos += [E._r(p.x, 3), E._r(p.y, 3), E._r(p.z, 3)]
        for t in tris:
            idx += [t[0] + off, t[1] + off, t[2] + off]
    return pos, idx


def compare_versions(path_a, path_b, max_triangles=60000):
    """Geometry difference from version A to version B: material added (B − A) and removed (A − B) per part, as
    meshes for the viewer, with volumes."""
    a, b = _doc_shapes(path_a), _doc_shapes(path_b)
    meshes, rows, total = [], [], 0
    for name in sorted(set(a) | set(b)):
        sa = a.get(name, (None, None))[1]
        sb = b.get(name, (None, None))[1]
        label = (b.get(name) or a.get(name))[0]
        diag = (sb or sa).BoundBox.DiagonalLength
        added = sb if sa is None else (sb.cut(sa) if sb is not None else None)
        removed = sa if sb is None else (sa.cut(sb) if sa is not None else None)
        row = {"object": name, "label": label, "added_volume": 0.0, "removed_volume": 0.0,
               "status": "new" if sa is None else "deleted" if sb is None else "changed"}
        for kind, s in (("added", added), ("removed", removed)):
            if s is None or s.isNull() or not s.Solids or s.Volume < 1e-6:
                continue
            row[f"{kind}_volume"] = E._r(s.Volume, 3)
            pos, idx = _tess(s, max(diag / 400.0, 0.02))
            total += len(idx) // 3
            if total <= max_triangles:
                meshes.append({"kind": kind, "object": name, "positions": pos, "indices": idx})
        if row["added_volume"] or row["removed_volume"] or row["status"] != "changed":
            rows.append(row)
    return {"parts": rows, "meshes": meshes}
