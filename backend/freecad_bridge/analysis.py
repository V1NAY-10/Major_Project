"""
Geometry analysis helpers — run INSIDE FreeCAD, used by engine.py.

  * surface_info      classify a face as plane / cylinder / cone / sphere / torus. Analytic
                      surfaces are read directly; B-spline, offset, extrusion and revolution
                      surfaces (common in STEP files from some exporters) are recognised by
                      least-squares fitting of sampled points and normals.
  * angular_span      how far a cylindrical face wraps around its axis (full hole vs fillet).
  * link_counterbores pair a counterbore with the hole it belongs to.
  * hole_patterns     circular (bolt circle), rectangular grid and linear hole patterns.
  * dimensions        the driving dimensions of each part: overall sizes, wall thicknesses,
                      gaps/slot widths and hole-to-edge distances, each tied to the feature IDs
                      that define it so it can be changed with set_dimension.
"""

import math

import FreeCAD as App  # type: ignore
import Part  # type: ignore

try:
    import numpy as np  # FreeCAD bundles numpy
except Exception:  # pragma: no cover
    np = None

V = App.Vector


def _r(x, n=4):
    return round(float(x), n) + 0.0


def _vl(v, n=4):
    return [_r(v.x, n), _r(v.y, n), _r(v.z, n)]


def canon_axis(a):
    """Unit axis with a deterministic sign (first significant component positive: z, then y, then x)."""
    a = V(a).normalize()
    eps = 1e-9
    if a.z < -eps or (abs(a.z) <= eps and (a.y < -eps or (abs(a.y) <= eps and a.x < 0))):
        a = a.negative()
    return a


def perp_base(point, axis):
    """Closest point on the axis line to the origin (unique description of the line)."""
    return point - axis * point.dot(axis)


def basis(a):
    """Two unit vectors perpendicular to a, aligned with global axes where possible."""
    for g in (V(1, 0, 0), V(0, 1, 0), V(0, 0, 1)):
        e1 = g - a * g.dot(a)
        if e1.Length > 0.5:
            e1.normalize()
            return e1, a.cross(e1).normalize()
    raise ValueError("degenerate axis")


# ─────────────────────────────────────────────────────────────────────────────
# Surface classification
# ─────────────────────────────────────────────────────────────────────────────

def surface_info(face, tol):
    """{'type': plane|cylinder|cone|sphere|torus|other, ...} for a face (global coordinates)."""
    s = face.Surface
    if isinstance(s, Part.Plane):
        return {"type": "plane"}
    if isinstance(s, Part.Cylinder):
        return {"type": "cylinder", "axis": V(s.Axis), "center": V(s.Center), "radius": float(s.Radius)}
    if isinstance(s, Part.Cone):
        return {"type": "cone", "axis": V(s.Axis), "apex": V(s.Apex), "semi": abs(float(s.SemiAngle))}
    if isinstance(s, Part.Sphere):
        return {"type": "sphere", "center": V(s.Center), "radius": float(s.Radius)}
    if isinstance(s, Part.Toroid):
        return {"type": "torus", "axis": V(s.Axis), "center": V(s.Center),
                "major": float(s.MajorRadius), "minor": float(s.MinorRadius)}
    return _fit_surface(face, tol)


def _samples(face, n=9):
    try:
        u0, u1, v0, v1 = face.ParameterRange
    except Exception:
        return [], []
    pts, nrm = [], []
    for i in range(n):
        for j in range(n):
            u = u0 + (u1 - u0) * (i + 0.5) / n
            v = v0 + (v1 - v0) * (j + 0.5) / n
            try:
                if not face.isPartOfDomain(u, v):
                    continue
                p = face.valueAt(u, v)
                nn = face.normalAt(u, v)
            except Exception:
                continue
            if nn.Length > 1e-12:
                pts.append(p)
                nrm.append(V(nn).normalize())
    return pts, nrm


def _fit_surface(face, tol):
    """Recognise analytic geometry hidden in a B-spline / offset / swept surface."""
    fit_tol = max(tol * 10, 1e-4)
    try:
        if face.findPlane(fit_tol) is not None:
            return {"type": "plane", "fitted": True}
    except Exception:
        pass
    if np is None:
        return {"type": "other"}
    pts, nrm = _samples(face)
    if len(pts) < 8:
        return {"type": "other"}
    P = np.array([[p.x, p.y, p.z] for p in pts])
    N = np.array([[n.x, n.y, n.z] for n in nrm])
    w, vecs = np.linalg.eigh(N.T @ N / len(N))
    if w[1] < 1e-6:  # all normals parallel but not a plane within tolerance
        return {"type": "other"}

    # Sphere: all normal lines meet in one point.
    A = np.zeros((3, 3))
    b = np.zeros(3)
    for p, n in zip(P, N):
        M = np.eye(3) - np.outer(n, n)
        A += M
        b += M @ p
    if np.linalg.eigvalsh(A)[0] > 0.05 * len(P):
        c = np.linalg.solve(A, b)
        radii = np.linalg.norm(P - c, axis=1)
        if radii.std() < fit_tol and radii.mean() > fit_tol:
            return {"type": "sphere", "center": V(*c), "radius": float(radii.mean()), "fitted": True}

    # Cylinder (normals ⟂ axis) or cone (normals at a constant angle to the axis).
    if w[0] < 1e-7:
        a = vecs[:, 0]
        kind = "cylinder"
    else:
        x, *_ = np.linalg.lstsq(N, np.ones(len(N)), rcond=None)
        if np.linalg.norm(x) < 1e-9:
            return {"type": "other"}
        a = x / np.linalg.norm(x)
        if (N @ a).std() > 1e-4:
            return {"type": "other"}
        kind = "cone"
    av = canon_axis(V(*a))
    a = np.array([av.x, av.y, av.z])
    e1, e2 = basis(av)
    E = np.array([[e1.x, e1.y, e1.z], [e2.x, e2.y, e2.z]])
    # Normal lines of a surface of revolution cross the axis: their 2D projections meet at the axis point.
    A2 = np.zeros((2, 2))
    b2 = np.zeros(2)
    for p, n in zip(P, N):
        m = E @ n
        ln = np.linalg.norm(m)
        if ln < 1e-9:
            continue
        m /= ln
        M = np.eye(2) - np.outer(m, m)
        A2 += M
        b2 += M @ (E @ p)
    if abs(np.linalg.det(A2)) < 1e-12:
        return {"type": "other"}
    q = np.linalg.solve(A2, b2)
    axis_pt = q[0] * E[0] + q[1] * E[1]
    t = P @ a
    radial = P - np.outer(t, a) - axis_pt
    rr = np.linalg.norm(radial, axis=1)
    if kind == "cylinder":
        if rr.std() > fit_tol:
            return {"type": "other"}
        return {"type": "cylinder", "axis": av, "center": V(*axis_pt), "radius": float(rr.mean()), "fitted": True}
    k, r0 = np.polyfit(t, rr, 1)
    if abs(k) < 1e-9 or (rr - (k * t + r0)).std() > fit_tol:
        return {"type": "other"}
    apex = axis_pt + a * (-r0 / k)
    return {"type": "cone", "axis": av, "apex": V(*apex), "semi": float(math.atan(abs(k))), "fitted": True}


def angular_span(face, axis, center, info):
    """Angle (radians) a cylindrical face covers around its axis."""
    if not info.get("fitted"):
        try:
            u0, u1, _, _ = face.ParameterRange
            return abs(u1 - u0)
        except Exception:
            pass
    e1, e2 = basis(axis)
    angles = []
    pts = []
    for e in face.Edges:
        try:
            if e.isClosed() and e.Length > 1e-6:
                c = sum((p for p in e.discretize(8)[:-1]), V()) * (1.0 / 7)
                if (c - center - axis * (c - center).dot(axis)).Length < max(1e-3, 0.01 * e.Length):
                    return 2 * math.pi  # a closed rim around the axis: the face wraps all the way round
            pts.extend(e.discretize(120))
        except Exception:
            pass
    pts.extend(_samples(face, 7)[0])
    for p in pts:
        w = p - center
        angles.append(math.atan2(w.dot(e2), w.dot(e1)))
    if len(angles) < 3:
        return 0.0
    angles.sort()
    gaps = [b - a for a, b in zip(angles, angles[1:])] + [angles[0] + 2 * math.pi - angles[-1]]
    return 2 * math.pi - max(gaps)


# ─────────────────────────────────────────────────────────────────────────────
# Counterbores
# ─────────────────────────────────────────────────────────────────────────────

def link_counterbores(holes, tol):
    """Attach coaxial counterbores to their holes (in place).

    A counterbore is a larger blind, flat-bottomed hole whose bottom meets an open end of a
    smaller hole on the same axis. The smaller hole gets `counterbore` and its entry/depth
    cover the whole stack; the larger one is kept as its own feature with `counterbore_of`.
    """
    for outer in holes:
        if outer["through"] or outer["drill_tip"]:
            continue
        o0, o1 = outer["_t"]
        cap_t = o0 if not outer["_open"][0] else o1
        for inner in holes:
            if inner is outer or inner["_r"] >= outer["_r"] - tol or "_cb" in inner:
                continue
            if abs(inner["_a"].dot(outer["_a"])) < 1 - 1e-7 or (inner["_base"] - outer["_base"]).Length > tol * 10:
                continue
            i0, i1 = inner["_t"]
            if abs(i1 - cap_t) < tol * 10 and inner["_open"][1]:
                far = i0
            elif abs(i0 - cap_t) < tol * 10 and inner["_open"][0]:
                far = i1
            else:
                continue
            a = outer["_a"]
            entry_t = o1 if cap_t == o0 else o0
            inner["_cb"] = outer
            outer["_cb_inner"] = inner
            inner["counterbore"] = {"diameter": _r(2 * outer["_r"]), "depth": _r(o1 - o0)}
            inner["entry"] = _vl(inner["_base"] + a * entry_t)
            inner["depth"] = _r(abs(entry_t - far))
            inner["direction"] = _vl(a.negative() if entry_t > far else a)
            break


def finish_links(feats):
    """After IDs are assigned: name the partner features of counterbores."""
    for f in feats:
        if "_cb" in f:
            f["counterbore"]["feature"] = f["_cb"]["id"]
            f["_cb"]["counterbore_of"] = f["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Hole patterns
# ─────────────────────────────────────────────────────────────────────────────

def _cluster(values, tol):
    out = []
    for v in sorted(values):
        if out and abs(v - out[-1][-1]) < tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(c) / len(c) for c in out]


def _uniform(steps, tol):
    return steps and max(steps) - min(steps) < tol


def hole_patterns(feats, tol):
    """Find circular, grid and linear patterns among equal holes. Returns public pattern dicts."""
    holes = [f for f in feats if f["kind"] == "hole" and "_cb_inner" not in f]
    groups = []
    for h in holes:
        a = h["_a"]
        et = _v3(h["entry"]).dot(a)
        for g in groups:
            r = g[0]
            if (r["_obj"] == h["_obj"] and abs(r["_r"] - h["_r"]) < tol * 10 and abs(r["_a"].dot(a)) > 1 - 1e-7
                    and r["through"] == h["through"] and (h["through"] or abs(r["depth"] - h["depth"]) < tol * 10)
                    and abs(_v3(r["entry"]).dot(r["_a"]) - et) < tol * 10):
                g.append(h)
                break
        else:
            groups.append([h])

    patterns = []
    for g in groups:
        if len(g) < 3:
            continue
        g = sorted(g, key=lambda f: int("".join(c for c in f["id"] if c.isdigit()) or 0))
        a = g[0]["_a"]
        e1, e2 = basis(a)
        entry_t = _v3(g[0]["entry"]).dot(a)
        pts = [(h["_base"].dot(e1), h["_base"].dot(e2)) for h in g]
        pat = _circular(g, pts, a, e1, e2, entry_t, tol) or _grid(g, pts, a, e1, e2, entry_t, tol) \
            or _linear(g, pts, a, e1, e2, entry_t, tol)
        if pat:
            pat["object"] = g[0]["_obj"]
            pat["hole_diameter"] = g[0]["diameter"]
            pat["holes"] = [h["id"] for h in pat.pop("_members")]
            patterns.append(pat)
    patterns.sort(key=lambda p: int("".join(c for c in p["holes"][0] if c.isdigit()) or 0))
    for i, p in enumerate(patterns, 1):
        p["id"] = f"PAT{i}"
        for hid in p["holes"]:
            for h in holes:
                if h["id"] == hid:
                    h["pattern"] = p["id"]
    return patterns


def _v3(seq):
    return V(float(seq[0]), float(seq[1]), float(seq[2]))


def _circular(g, pts, a, e1, e2, entry_t, tol):
    if np is None:
        return None
    M = np.array([[2 * x, 2 * y, 1.0] for x, y in pts])
    rhs = np.array([x * x + y * y for x, y in pts])
    (cx, cy, c), *_ = np.linalg.lstsq(M, rhs, rcond=None)
    R2 = c + cx * cx + cy * cy
    if R2 <= 0:
        return None
    R = math.sqrt(R2)
    if R < tol * 100 or max(abs(math.hypot(x - cx, y - cy) - R) for x, y in pts) > tol * 10:
        return None
    ang = sorted(((math.atan2(y - cy, x - cx)) % (2 * math.pi), h) for (x, y), h in zip(pts, g))
    n = len(ang)
    gaps = [((ang[(i + 1) % n][0] - ang[i][0]) % (2 * math.pi)) or 2 * math.pi for i in range(n)]
    if _uniform(gaps, 1e-3):  # full circle: start at the hole with the smallest angle from ref_dir
        full, first = True, 0
    else:  # arc: start right after the largest gap; the remaining gaps must be equal
        big = max(range(n), key=lambda i: gaps[i])
        first = (big + 1) % n
        if not _uniform([gaps[(first + k) % n] for k in range(n - 1)], 1e-3):
            return None
        full = False
    order = [ang[(first + k) % n][1] for k in range(n)]
    step = sum(gaps[(first + k) % n] for k in range(n - 1)) / (n - 1)
    center = e1 * cx + e2 * cy + a * entry_t
    start_ang = math.degrees(ang[first][0])
    return {"type": "circular", "count": len(g), "pcd": _r(2 * R), "center": _vl(center),
            "axis": _vl(a), "angle_step_deg": _r(math.degrees(step), 3), "full_circle": full,
            "start_angle_deg": _r(start_ang % 360, 3), "ref_dir": _vl(e1), "_members": order}


def _grid(g, pts, a, e1, e2, entry_t, tol):
    us = _cluster([p[0] for p in pts], tol * 10)
    vs = _cluster([p[1] for p in pts], tol * 10)
    if len(us) < 2 or len(vs) < 2 or len(us) * len(vs) != len(g):
        return None
    cells = {}
    for (x, y), h in zip(pts, g):
        iu = min(range(len(us)), key=lambda i: abs(us[i] - x))
        iv = min(range(len(vs)), key=lambda i: abs(vs[i] - y))
        if abs(us[iu] - x) > tol * 10 or abs(vs[iv] - y) > tol * 10 or (iu, iv) in cells:
            return None
        cells[(iu, iv)] = h
    su = [b - a_ for a_, b in zip(us, us[1:])]
    sv = [b - a_ for a_, b in zip(vs, vs[1:])]
    if not (_uniform(su, tol * 10) and _uniform(sv, tol * 10)):
        return None
    center = e1 * ((us[0] + us[-1]) / 2) + e2 * ((vs[0] + vs[-1]) / 2) + a * entry_t
    order = [cells[(iu, iv)] for iv in range(len(vs)) for iu in range(len(us))]
    return {"type": "grid", "count": len(g), "count_u": len(us), "count_v": len(vs),
            "spacing_u": _r(su[0]), "spacing_v": _r(sv[0]), "dir_u": _vl(e1), "dir_v": _vl(e2),
            "center": _vl(center), "axis": _vl(a), "_members": order}


def _linear(g, pts, a, e1, e2, entry_t, tol):
    (x0, y0), (x1, y1) = pts[0], max(pts, key=lambda p: math.hypot(p[0] - pts[0][0], p[1] - pts[0][1]))
    L = math.hypot(x1 - x0, y1 - y0)
    if L < tol * 10:
        return None
    dx, dy = (x1 - x0) / L, (y1 - y0) / L
    proj = []
    for (x, y), h in zip(pts, g):
        if abs((x - x0) * dy - (y - y0) * dx) > tol * 10:
            return None
        proj.append(((x - x0) * dx + (y - y0) * dy, h))
    proj.sort(key=lambda t: t[0])
    steps = [b[0] - a_[0] for a_, b in zip(proj, proj[1:])]
    if not _uniform(steps, tol * 10):
        return None
    d3 = canon_axis(e1 * dx + e2 * dy)
    order = sorted((h for _, h in proj), key=lambda h: h["_base"].dot(d3))
    start = order[0]["_base"] + a * entry_t
    return {"type": "linear", "count": len(g), "spacing": _r(steps[0]), "direction": _vl(d3),
            "start": _vl(start), "axis": _vl(a), "_members": order}


def pattern_positions(pat, pcd=None, count=None, spacing=None, spacing_u=None, spacing_v=None,
                      start_angle=None, anchor="center"):
    """New hole axis points (on the entry plane) for an edited pattern."""
    a = _v3(pat["axis"])
    if pat["type"] == "circular":
        e1 = _v3(pat["ref_dir"])
        e2 = a.cross(e1)
        n = int(count or pat["count"])
        R = (float(pcd) if pcd is not None else pat["pcd"]) / 2
        step = (2 * math.pi / n) if pat["full_circle"] else math.radians(pat["angle_step_deg"])
        if count and not pat["full_circle"]:
            step = math.radians(pat["angle_step_deg"]) * (pat["count"] - 1) / max(n - 1, 1)
        s0 = math.radians(float(start_angle) if start_angle is not None else pat["start_angle_deg"])
        c = _v3(pat["center"])
        return [c + e1 * (R * math.cos(s0 + k * step)) + e2 * (R * math.sin(s0 + k * step)) for k in range(n)]
    if pat["type"] == "grid":
        du, dv = _v3(pat["dir_u"]), _v3(pat["dir_v"])
        su = float(spacing_u) if spacing_u is not None else pat["spacing_u"]
        sv = float(spacing_v) if spacing_v is not None else pat["spacing_v"]
        nu, nv = pat["count_u"], pat["count_v"]
        c = _v3(pat["center"])
        return [c + du * (su * (iu - (nu - 1) / 2)) + dv * (sv * (iv - (nv - 1) / 2))
                for iv in range(nv) for iu in range(nu)]
    # linear
    d = _v3(pat["direction"])
    n = int(count or pat["count"])
    s = float(spacing) if spacing is not None else pat["spacing"]
    start = _v3(pat["start"])
    if anchor == "center":
        mid = start + d * (pat["spacing"] * (pat["count"] - 1) / 2)
        start = mid - d * (s * (n - 1) / 2)
    return [start + d * (s * k) for k in range(n)]


# ─────────────────────────────────────────────────────────────────────────────
# Driving dimensions
# ─────────────────────────────────────────────────────────────────────────────

AXIS_NAMES = {(1, 0, 0): "X", (0, 1, 0): "Y", (0, 0, 1): "Z"}


def axis_name(u):
    for vec, name in AXIS_NAMES.items():
        if abs(u.dot(V(*vec))) > 1 - 1e-6:
            return name
    return None


def _on_face(face, p, tol):
    try:
        return face.isInside(p, tol, True)
    except Exception:
        return False


def _inside(shape, p, tol):
    for s in shape.Solids:
        try:
            if s.isInside(p, tol, False):
                return True
        except Exception:
            pass
    return False


def _face_overlap_point(fa, fb, pa, pb, n, tol):
    """A point on face A whose projection along n lands on face B (or None)."""
    e1, e2 = basis(n)
    ba, bb = fa.BoundBox, fb.BoundBox

    def rng(box, e):
        vals = [V(x, y, z).dot(e) for x in (box.XMin, box.XMax) for y in (box.YMin, box.YMax)
                for z in (box.ZMin, box.ZMax)]
        return min(vals), max(vals)

    a1, b1 = rng(ba, e1), rng(bb, e1)
    a2, b2 = rng(ba, e2), rng(bb, e2)
    lo1, hi1 = max(a1[0], b1[0]), min(a1[1], b1[1])
    lo2, hi2 = max(a2[0], b2[0]), min(a2[1], b2[1])
    if hi1 < lo1 - tol or hi2 < lo2 - tol:
        return None
    da, db = pa.dot(n), pb.dot(n)
    for fu, fv in ((0.5, 0.5), (0.25, 0.25), (0.75, 0.75), (0.25, 0.75), (0.75, 0.25), (0.5, 0.15),
                   (0.5, 0.85), (0.15, 0.5), (0.85, 0.5)):
        u = lo1 + (hi1 - lo1) * fu
        v = lo2 + (hi2 - lo2) * fv
        q = e1 * u + e2 * v
        qa, qb = q + n * da, q + n * db
        if _on_face(fa, qa, tol * 10) and _on_face(fb, qb, tol * 10):
            return qa, qb
    return None


def dimensions(shapes, feats, tol, limit_per_object=400):
    """Driving dimensions per object. `shapes` maps object name -> global shape."""
    out = []
    for objname, shape in shapes.items():
        bb = shape.BoundBox
        planes = [f for f in feats if f["_obj"] == objname and f["kind"] == "plane" and not f.get("is_cap")]
        holes = [f for f in feats if f["_obj"] == objname and f["kind"] == "hole" and "_cb_inner" not in f]
        dims = {}

        # Plane pairs with opposite normals: thickness (material between) or gap (air between).
        for p in planes:
            best = {}
            for q in planes:
                if q is p or p["_n"].dot(q["_n"]) > -1 + 1e-6:
                    continue
                s = (q["_c"] - p["_c"]).dot(p["_n"])
                if abs(s) < tol * 10:
                    continue
                kind = "thickness" if s < 0 else "gap"
                if kind in best and abs(s) >= best[kind][0]:
                    continue
                hit = _face_overlap_point(shape.Faces[p["_faces"][0]], shape.Faces[q["_faces"][0]],
                                          p["_c"], q["_c"], p["_n"], tol)
                if hit is None:
                    continue
                mid = (hit[0] + hit[1]) * 0.5
                if _inside(shape, mid, tol) != (kind == "thickness"):
                    continue
                best[kind] = (abs(s), q)
            for kind, (dist, q) in best.items():
                _add_plane_dim(dims, p, q, dist, kind, objname)

        # Overall sizes: outermost face pairs along X, Y, Z.
        for axis, lo, hi in (("X", bb.XMin, bb.XMax), ("Y", bb.YMin, bb.YMax), ("Z", bb.ZMin, bb.ZMax)):
            u = V(*(1.0 if c == axis else 0.0 for c in "XYZ"))
            neg = [p for p in planes if p["_n"].dot(u) < -1 + 1e-6 and abs(p["_c"].dot(u) - lo) < tol * 10]
            pos = [p for p in planes if p["_n"].dot(u) > 1 - 1e-6 and abs(p["_c"].dot(u) - hi) < tol * 10]
            if neg and pos:
                a = max(neg, key=lambda f: f["area"])
                b = max(pos, key=lambda f: f["area"])
                key = _pair_id(a, b)
                d = dims.get(key) or _add_plane_dim(dims, a, b, hi - lo, "extent", objname)
                d["type"] = "extent"
                d["label"] = f"overall {axis} size"

        # Hole axis to the nearest bounding face on each side (edge distances).
        for h in holes:
            a = h["_a"]
            mid = h["_base"] + a * ((h["_t"][0] + h["_t"][1]) / 2)
            per_axis = {}
            for p in planes:
                n = p["_n"]
                if abs(n.dot(a)) > 1e-6:
                    continue
                d = (mid - p["_c"]).dot(n)
                if d > -tol:  # hole must be on the material side of the face
                    continue
                if not _on_face(shape.Faces[p["_faces"][0]], mid - n * d, tol * 10):
                    continue
                u = canon_axis(n)
                key = (round(u.x, 4), round(u.y, 4), round(u.z, 4))
                if key not in per_axis or -d < per_axis[key][0]:
                    per_axis[key] = (-d, p, u)
            for dist, p, u in per_axis.values():
                did = f"{h['id']}-{p['id']}"
                an = axis_name(u)
                dims[did] = {"id": did, "type": "edge_distance", "value": _r(dist), "refs": [h["id"], p["id"]],
                             "axis": _vl(u), "object": objname,
                             "label": f"{h['id']} axis to {p.get('label', p['id'])}" + (f" (along {an})" if an else "")}

        rows = sorted(dims.values(), key=lambda d: ({"extent": 0, "thickness": 1, "gap": 2, "edge_distance": 3}[d["type"]],
                                                     -d.get("_area", 0), d["id"]))
        for d in rows[:limit_per_object]:
            d.pop("_area", None)
            out.append(d)
    return out


def _pair_id(a, b):
    return f"{a['id']}-{b['id']}"


def _add_plane_dim(dims, p, q, dist, kind, objname):
    u = canon_axis(p["_n"])
    a, b = (p, q) if p["_c"].dot(u) <= q["_c"].dot(u) else (q, p)
    did = _pair_id(a, b)
    if did in dims:
        return dims[did]
    an = axis_name(u)
    word = {"thickness": "thickness", "gap": "gap / slot width", "extent": "overall size"}[kind]
    d = {"id": did, "type": kind, "value": _r(dist), "refs": [a["id"], b["id"]], "axis": _vl(u),
         "object": objname, "label": f"{word}" + (f" along {an}" if an else ""),
         "_area": min(a["area"], b["area"])}
    dims[did] = d
    return d
