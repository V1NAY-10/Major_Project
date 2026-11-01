"""
STL / OBJ / PLY import: rebuild an exact, editable B-rep solid from a triangle mesh.

A mesh has no faces, holes or dimensions, only triangles, so it can't be edited as it is. Each separate body
of the mesh is rebuilt as an exact solid:

* **Turned (axisymmetric) bodies** (nose cones, body tubes, couplers, bulkheads, centering rings, adapters):
  the axis is found from the triangle normals (every normal line of a surface of revolution crosses its axis), the
  half cross-section is cut from the mesh, split into straight lines and circular arcs, each refitted to the mesh
  vertices (which lie exactly on the true surface), and revolved. Triangles that don't lie on that surface of
  revolution are the non-axisymmetric features: each group is fitted as a cylinder (radial / off-axis holes) and
  cut, blind or through.
* **Prismatic bodies** (fins, plates, brackets): coplanar triangles are merged into exact planar faces; each
  cylindrical patch (hole or boss) is fitted and snapped to an exact cylinder by a boolean cut / fuse.

The result is checked against the mesh (volume and the largest distance of the mesh vertices from the new surface)
and the report is returned with the model info, so the user sees how faithful the reconstruction is.
"""

import math

import numpy as np

import FreeCAD as App  # type: ignore
import Mesh  # type: ignore
import Part  # type: ignore

V = App.Vector
MESH_EXTS = (".stl", ".obj", ".ply")


class MeshImportError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Mesh arrays
# ─────────────────────────────────────────────────────────────────────────────

class _M:
    """One mesh body as numpy arrays: points P (n,3), triangles T (m,3), unit normals N, areas A, centroids C."""

    def __init__(self, mesh):
        pts, tris = mesh.Topology
        self.P = np.array([[p.x, p.y, p.z] for p in pts], float)
        self.T = np.array(tris, int)
        a, b, c = (self.P[self.T[:, k]] for k in range(3))
        cr = np.cross(b - a, c - a)
        ln = np.linalg.norm(cr, axis=1)
        keep = ln > 1e-12
        self.T, a, b, c, cr, ln = self.T[keep], a[keep], b[keep], c[keep], cr[keep], ln[keep]
        self.N = cr / ln[:, None]
        self.A = ln / 2
        self.C = (a + b + c) / 3
        self.mesh = mesh
        self.size = float(np.linalg.norm(self.P.max(0) - self.P.min(0)))

    def adjacency(self):
        """Triangle neighbours across shared edges: list of (i, j) pairs."""
        e = np.concatenate([self.T[:, [0, 1]], self.T[:, [1, 2]], self.T[:, [2, 0]]])
        tid = np.tile(np.arange(len(self.T)), 3)
        e.sort(axis=1)
        order = np.lexsort((e[:, 1], e[:, 0]))
        e, tid = e[order], tid[order]
        same = np.all(e[1:] == e[:-1], axis=1)
        return np.stack([tid[:-1][same], tid[1:][same]], axis=1)


def _components(n, pairs):
    """Connected components (union-find) of n items joined by pairs; returns a label per item."""
    parent = np.arange(n)

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in pairs:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj
    return np.array([find(i) for i in range(n)])


# ─────────────────────────────────────────────────────────────────────────────
# Axis of revolution (line-complex fit of the normal lines)
# ─────────────────────────────────────────────────────────────────────────────

def _fit_axis(m):
    """Axis (point q, unit direction a) whose line meets the most normal lines, and the area fraction of the mesh
    that is a surface of revolution about it. Normal line (p, n) meets axis (a, moment mo) when
    a·(p×n) + mo·n = 0, linear in (a, mo): the smallest eigenvector of the weighted scatter matrix."""
    c0 = (m.C * m.A[:, None]).sum(0) / m.A.sum()
    p = (m.C - c0) / m.size
    w = m.A / m.A.sum()
    best = None
    sel = np.ones(len(p), bool)
    for _ in range(4):  # re-weighted: drop the non-axisymmetric triangles (radial holes, flats) and refit
        X = np.hstack([np.cross(p[sel], m.N[sel]), m.N[sel]])
        M = (X * w[sel, None]).T @ X
        vals, vecs = np.linalg.eigh(M)
        best = None
        for k in range(3):  # several near-zero eigenvalues happen (e.g. pure planes); take the first proper line
            v = vecs[:, k]
            a, mo = v[:3], v[3:]
            na = np.linalg.norm(a)
            if na < 1e-6:
                continue
            a, mo = a / na, mo / na
            q = np.cross(a, mo)  # closest point of the axis to c0 (scaled frame)
            res = _axis_residual(m, p, a, q)
            if best is None or res[0] > best[2]:
                best = (a, q, res[0], res[1])
        if best is None:
            return None
        sel = best[3]
    a, q, frac, _ = best
    return q * m.size + c0, a / np.linalg.norm(a), frac


def _axis_residual(m, p, a, q):
    """Area fraction of triangles whose normal line passes within a small distance of the axis (or is parallel
    to it — flat faces square to the axis)."""
    d = p - q
    cr = np.cross(m.N, a)
    ncr = np.linalg.norm(cr, axis=1)
    par = ncr < 1e-3
    dist = np.abs(np.einsum("ij,ij->i", d, cr)) / np.where(par, 1, ncr)
    # radial distance of the triangle from the axis (scaled): tolerance relative to it
    rad = np.linalg.norm(d - np.outer(d @ a, a), axis=1)
    ok = par | (dist < 2e-3 + 2e-2 * rad)
    return (m.A[ok].sum() / m.A.sum()), ok


# ─────────────────────────────────────────────────────────────────────────────
# Profile: lines and arcs in the (z, r) half-plane
# ─────────────────────────────────────────────────────────────────────────────

def _zr(P, q, a):
    d = P - q
    z = d @ a
    r = np.linalg.norm(d - np.outer(z, a), axis=1)
    return z, r


def _fit_circle(pts):
    """Least-squares circle (Kasa) through 2D points: centre, radius, max abs error."""
    x, y = pts[:, 0], pts[:, 1]
    A = np.stack([x, y, np.ones_like(x)], 1)
    b = x * x + y * y
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    cx, cy = sol[0] / 2, sol[1] / 2
    r = math.sqrt(max(sol[2] + cx * cx + cy * cy, 0))
    err = np.abs(np.hypot(x - cx, y - cy) - r)
    return np.array([cx, cy]), r, float(err.max()) if len(err) else 0.0


def _fit_line(pts):
    c = pts.mean(0)
    u, s, vt = np.linalg.svd(pts - c)
    d = vt[0]
    nrm = np.array([-d[1], d[0]])
    err = np.abs((pts - c) @ nrm)
    return c, d, float(err.max())


def _ordered_profile(m, q, a, tol):
    """Closed outline of the half cross-section in (z, r), as an ordered polygon (closed, no repeat).
    The section is cut by a plane through the axis at the angle that crosses the fewest non-axisymmetric
    features (largest section area)."""
    u = np.cross(a, [1, 0, 0])
    if np.linalg.norm(u) < 0.1:
        u = np.cross(a, [0, 1, 0])
    u /= np.linalg.norm(u)
    v = np.cross(a, u)
    best = None
    for k in range(24):
        t = 2 * math.pi * k / 24 + 0.0137  # off the round angles tessellators put vertices on
        dirn = u * math.cos(t) + v * math.sin(t)  # in-plane radial direction
        nrm = np.cross(a, dirn)
        try:
            secs = m.mesh.crossSections([(V(*q), V(*nrm))], 1e-6, True)[0]
        except Exception:
            continue
        polys = []
        for pl in secs:
            P = np.array([[p.x, p.y, p.z] for p in pl])
            if len(P) < 3:
                continue
            d = P - q
            side = d @ dirn
            if side.max() <= tol:
                continue  # the other half of the section
            # a loop that crosses the axis (solid tip / closed end) is clipped to the half-plane r >= 0
            clipped = _clip_half(np.stack([d @ a, side], 1))
            if len(clipped) >= 3:
                polys.append(clipped)
        if not polys:
            continue
        poly = max(polys, key=lambda pp: abs(_area(pp)))
        ar = abs(_area(poly))
        if best is None or ar > best[0] * (1 + 1e-4):
            best = (ar, poly, t)
    if best is None:
        raise MeshImportError("could not cut a cross-section through the axis")
    poly = best[1]
    if np.allclose(poly[0], poly[-1]):
        poly = poly[:-1]
    # drop duplicate consecutive points
    keep = np.ones(len(poly), bool)
    keep[1:] = np.linalg.norm(np.diff(poly, axis=0), axis=1) > 1e-7
    return poly[keep], best[2]


def _vertex_profile(poly, zr_pts, tol):
    """The mesh vertices that lie on the section outline, in order along it. The section points are on the
    triangles' chords, up to the faceting sag (≈ the tolerance) inside a curved surface, which hides small arcs and
    shifts tangent points; the vertices are exactly on the true surface. Each vertex (z, r) is placed on the
    outline by its nearest segment and sorted by the distance along it."""
    pts = np.unique(np.round(zr_pts, 6), axis=0)
    a = poly
    ab = np.roll(poly, -1, axis=0) - a
    seg = np.linalg.norm(ab, axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)[:-1]])
    best_d = np.full(len(pts), np.inf)
    best_s = np.zeros(len(pts))
    for k in range(len(a)):
        if seg[k] < 1e-12:
            continue
        t = np.clip(((pts - a[k]) @ ab[k]) / seg[k] ** 2, 0, 1)
        d = np.linalg.norm(pts - (a[k] + np.outer(t, ab[k])), axis=1)
        better = d < best_d
        best_d[better] = d[better]
        best_s[better] = cum[k] + t[better] * seg[k]
    on = best_d < 3 * tol
    out = pts[on][np.argsort(best_s[on], kind="stable")]
    if len(out) < len(poly):
        return None
    keep = [0]  # drop near-duplicates: noise between points a few microns apart reads as sharp turns
    for i in range(1, len(out)):
        if np.linalg.norm(out[i] - out[keep[-1]]) > tol / 20:
            keep.append(i)
    return out[keep]


def _tangent_point(g1, g2, near, max_deg=2.0):
    """The exact junction of two primitives that meet tangentially (blends, throat arcs, a cone running into a
    radius), or None. Their intersection is ill-conditioned there: the tangency point is the foot of the arc
    centre on the line, or lies on the line through both arc centres."""
    def tangent(g, p):
        if g["kind"] == "line":
            return g["d"]
        v = p - g["c"]
        return np.array([-v[1], v[0]]) / (np.linalg.norm(v) + 1e-15)
    if g1["kind"] == "line" and g2["kind"] == "line":
        return None
    t1, t2 = tangent(g1, near), tangent(g2, near)
    if abs(float(t1 @ t2)) < math.cos(math.radians(max_deg)):
        return None
    if g1["kind"] == "arc" and g2["kind"] == "arc":
        u = g2["c"] - g1["c"]
        if np.linalg.norm(u) < 1e-9:
            return None
        u /= np.linalg.norm(u)
        p1 = min((g1["c"] + s * g1["r"] * u for s in (1, -1)), key=lambda p: np.linalg.norm(p - near))
        p2 = min((g2["c"] + s * g2["r"] * u for s in (1, -1)), key=lambda p: np.linalg.norm(p - near))
        return (p1 + p2) / 2
    ln, ar = (g1, g2) if g1["kind"] == "line" else (g2, g1)
    return ln["c"] + ((ar["c"] - ln["c"]) @ ln["d"]) * ln["d"]


def _clip_half(poly):
    """Sutherland-Hodgman clip of a closed polygon (z, s) to s >= 0."""
    if np.allclose(poly[0], poly[-1]):
        poly = poly[:-1]
    out = []
    n = len(poly)
    for i in range(n):
        p, c = poly[i - 1], poly[i]
        pin, cin = p[1] >= 0, c[1] >= 0
        if cin != pin:
            t = p[1] / (p[1] - c[1])
            out.append(np.array([p[0] + t * (c[0] - p[0]), 0.0]))
        if cin:
            out.append(c)
    return np.array(out) if out else np.zeros((0, 2))


def _max_turn(pts):
    """Largest turning angle (rad) between consecutive segments: tessellated arcs turn a little at every point,
    sharp corners don't belong to an arc."""
    d = np.diff(pts, axis=0)
    if len(d) < 2:
        return 0.0
    a = np.arctan2(d[:, 1], d[:, 0])
    t = np.abs((np.diff(a) + np.pi) % (2 * np.pi) - np.pi)
    return float(t.max())


def _smooth_turns(pts):
    """A tessellated arc turns by a similar small angle at every point; a corner (even a shallow 3° one between
    two tangent-looking curves) is a jump well above the typical turn."""
    d = np.diff(pts, axis=0)
    if len(d) < 2:
        return True
    a = np.arctan2(d[:, 1], d[:, 0])
    t = np.abs((np.diff(a) + np.pi) % (2 * np.pi) - np.pi)
    return float(t.max()) < min(0.45, 3 * float(np.median(t)) + 0.005)


def _area(p):
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _segment_profile(poly, tol):
    """Split a closed polygon into primitives: ('line', i, j) or ('arc', i, j) over index ranges (j inclusive,
    wrapping). Greedy longest-fit starting from a sharp corner."""
    n = len(poly)
    # start at the sharpest corner so no primitive wraps across it
    turn = []
    for i in range(n):
        d1 = poly[i] - poly[i - 1]
        d2 = poly[(i + 1) % n] - poly[i]
        c = np.dot(d1, d2) / (np.linalg.norm(d1) * np.linalg.norm(d2) + 1e-15)
        turn.append(c)
    s = int(np.argmin(turn))
    P = np.roll(poly, -s, axis=0)
    P = np.vstack([P, P[:1]])  # closed: last == first
    rmax = 20 * float(np.linalg.norm(P.max(0) - P.min(0)))  # flatter than this is a straight line
    prims = []
    i = 0
    N = len(P) - 1
    while i < N:
        # longest line
        j = i + 1
        while j + 1 <= N and _fit_line(P[i:j + 2])[2] <= tol:
            j += 1
        best = ("line", i, j)
        # longest arc (≥ 4 points, must beat the line by length)
        if j - i < N - i:
            k = i + 2
            if k <= N:
                kk = None
                while k <= N:
                    c, r, err = _fit_circle(P[i:k + 1])
                    if err <= tol and r < rmax and _max_turn(P[i:k + 1]) < 0.45:
                        kk = k
                        k += 1
                    else:
                        break
                if kk is not None and kk - i >= 3 and kk > j:
                    best = ("arc", i, kk)
        prims.append(best)
        i = best[2]
    return P, _refine_breaks(P, _merge_prims(P, prims, tol, rmax))


def _merge_prims(P, prims, tol, rmax):
    """Join neighbours that one arc fits within the tolerance (the greedy pass can chip a short near-straight
    piece off the flat end of a long arc), and arcs that are one circle."""
    prims = list(prims)
    changed = True
    while changed and len(prims) > 1:
        changed = False
        for k in range(len(prims) - 1):
            (k1, i, j), (k2, _, l) = prims[k], prims[k + 1]
            if k1 == k2 == "line":
                continue
            short = min(j - i, l - j) <= 0.25 * (l - i) or k1 == k2 == "arc"
            if not short:
                continue
            c, r, err = _fit_circle(P[i:l + 1])
            if err <= tol and r < rmax and _max_turn(P[i:l + 1]) < 0.45:
                prims[k:k + 2] = [("arc", i, l)]
                changed = True
                break
    return prims


def _sse(kind, pts):
    if kind == "line":
        c, d, _ = _fit_line(pts)
        e = (pts - c) @ np.array([-d[1], d[0]])
    else:
        cen, r, _ = _fit_circle(pts)
        e = np.hypot(*(pts - cen).T) - r
    return float(e @ e)


def _refine_breaks(P, prims, window=40):
    """Move each break between two primitives to where their combined squared fit error is least. The greedy
    pass lets a curve run past a shallow corner (e.g. an ogive meeting a tip cone at 3°) while it stays within the
    tolerance; the least-squares break finds the real corner."""
    prims = [list(p) for p in prims]
    for _ in range(2):
        for k in range(len(prims) - 1):
            k1, i, j = prims[k]
            k2, _, l = prims[k + 1]
            lo, hi = max(i + (3 if k1 == "arc" else 1), j - window), min(l - (3 if k2 == "arc" else 1), j + window)
            if lo >= hi:
                continue
            b = min(range(lo, hi + 1), key=lambda b: _sse(k1, P[i:b + 1]) + _sse(k2, P[b:l + 1]))
            prims[k][2] = prims[k + 1][1] = b
    return [tuple(p) for p in prims]


def _refit(prims, P, zr_pts, tol):
    """Refit each primitive to the mesh vertices near it (they lie exactly on the true surface; the section
    points are on chords). Returns geometric primitives in (z, r)."""
    out = []
    for kind, i, j in prims:
        seg = P[i:j + 1]
        if kind == "line":
            c, d, _ = _fit_line(seg)
            nrm = np.array([-d[1], d[0]])
            lo, hi = sorted([(seg[0] - c) @ d, (seg[-1] - c) @ d])
            t = (zr_pts - c) @ d
            near = (np.abs((zr_pts - c) @ nrm) < 3 * tol) & (t > lo - tol) & (t < hi + tol)
            if near.sum() >= 2:
                c2, d2, _ = _fit_line(zr_pts[near])
                if abs(np.dot(d2, d)) > 0.999:
                    c, d = c2, d2 if np.dot(d2, d) > 0 else -d2
            d = _snap_dir(d)
            out.append({"kind": "line", "c": c, "d": d})
        else:
            cen, r, _ = _fit_circle(seg)
            dist = np.abs(np.hypot(*(zr_pts - cen).T) - r)
            ch = seg[-1] - seg[0]
            L = float(np.linalg.norm(ch))
            t = (zr_pts - seg[0]) @ (ch / L) if L > 0 else np.zeros(len(zr_pts))
            near = (dist < 3 * tol) & (t >= 0) & (t <= L)  # within the arc's span along its chord
            if near.sum() >= 4:
                cen2, r2, _ = _fit_circle(zr_pts[near])
                # keep the vertex fit only when it still runs through the section (stray vertices of a
                # neighbouring surface can pull a long shallow arc)
                if np.abs(np.hypot(*(seg - cen2).T) - r2).max() <= 2 * tol:
                    cen, r = cen2, r2
            out.append({"kind": "arc", "c": cen, "r": r, "mid": seg[len(seg) // 2]})
    return out


def _snap_dir(d):
    """Axial / radial lines are by far the most common: snap a nearly axial or radial direction exactly."""
    for e in (np.array([1.0, 0.0]), np.array([0.0, 1.0])):
        if abs(abs(np.dot(d, e)) - 1) < 1e-5:
            return e * np.sign(np.dot(d, e))
    return d


def _in_arc(ang, a0, am, a1):
    def norm(x):
        return np.mod(x - a0, 2 * np.pi)
    span, mid = norm(a1), norm(am)
    t = norm(ang)
    if mid <= span:
        return t <= span + 0.02
    return (t >= span - 0.02) | (t == 0)


def _intersect(p1, p2, near):
    """Intersection of two primitives closest to the point `near` (z, r)."""
    if p1["kind"] == "line" and p2["kind"] == "line":
        A = np.array([p1["d"], -p2["d"]]).T
        if abs(np.linalg.det(A)) < 1e-12:
            return near
        t = np.linalg.solve(A, p2["c"] - p1["c"])
        return p1["c"] + t[0] * p1["d"]
    if p1["kind"] == "arc" and p2["kind"] == "arc":
        return _circle_circle(p1, p2, near)
    ln, ar = (p1, p2) if p1["kind"] == "line" else (p2, p1)
    f = ln["c"] - ar["c"]
    d = ln["d"]
    b = 2 * f @ d
    c = f @ f - ar["r"] ** 2
    disc = b * b - 4 * c
    if disc < 0:
        return near
    sols = [ln["c"] + t * d for t in ((-b - math.sqrt(disc)) / 2, (-b + math.sqrt(disc)) / 2)]
    return min(sols, key=lambda s: np.linalg.norm(s - near))


def _circle_circle(c1, c2, near):
    d = np.linalg.norm(c2["c"] - c1["c"])
    if d < 1e-12 or d > c1["r"] + c2["r"] or d < abs(c1["r"] - c2["r"]):
        return near
    a = (c1["r"] ** 2 - c2["r"] ** 2 + d * d) / (2 * d)
    h = math.sqrt(max(c1["r"] ** 2 - a * a, 0))
    m = c1["c"] + a * (c2["c"] - c1["c"]) / d
    perp = np.array([-(c2["c"] - c1["c"])[1], (c2["c"] - c1["c"])[0]]) / d
    sols = [m + h * perp, m - h * perp]
    return min(sols, key=lambda s: np.linalg.norm(s - near))


def _snap_value(x, tol):
    """Round a measured value to 0.01 mm (0.1 mm / whole mm when that is within the tolerance)."""
    for step in (1.0, 0.5, 0.1, 0.05, 0.01):
        r = round(x / step) * step
        if abs(r - x) <= min(tol, step * 0.2):
            return r
    return x


def _world_frame(q, a, tol):
    """Axis pointing along +X/+Y/+Z (its largest component positive), origin at the axis point nearest the world
    origin, rounded: profile values then read in world coordinates (shoulder at Z -80, not -79.992)."""
    if a[np.argmax(np.abs(a))] < 0:
        a = -a
    q = q - a * (q @ a)
    q = np.array([_snap_value(x, tol) for x in q])
    return q, a


def _revolved_solid(m, q, a, tol):
    poly, angle = _ordered_profile(m, q, a, tol)
    zr_pts = np.stack(_zr(m.P, q, a), 1)
    P, prims = _segment_profile(poly, tol)
    # segment again on the exact vertices with a tight tolerance; kept unless it splits the outline into more
    # pieces (a mesh whose vertices are not on the true surface, e.g. smoothed or decimated)
    vp = _vertex_profile(poly, zr_pts, tol)
    snap_tol = tol
    if vp is not None:
        try:
            P2, prims2 = _segment_profile(vp, max(0.005, tol / 8))
            if len(prims2) <= len(prims) + 2:
                P, prims = P2, prims2
                snap_tol = max(0.005, tol / 8)  # exact corners: only rounding noise is snapped away
        except (ValueError, np.linalg.LinAlgError):
            pass
    geo = _refit(prims, P, zr_pts, tol)
    corners, tangent = [], []
    n = len(geo)
    for k in range(n):
        g1, g2 = geo[k], geo[(k + 1) % n]
        near = P[prims[k][2]]
        x = _tangent_point(g1, g2, near)
        tangent.append(x is not None and np.linalg.norm(x - near) <= 20 * tol)
        if not tangent[-1]:
            x = _intersect(g1, g2, near)
            if np.linalg.norm(x - near) > 20 * tol:
                x = near  # no intersection: keep the section point
        corners.append(x)
    # points on the axis and nice values (a tangent point is where the curves meet, not a design value)
    corners = [np.array([z, r]) if tg else
               np.array([_snap_value(z, snap_tol), 0.0 if r < tol else _snap_value(r, snap_tol)])
               for (z, r), tg in zip(corners, tangent)]
    # frame: the profile lives in the XZ-like plane spanned by the axis and a radial direction
    u = np.cross(a, [1, 0, 0])
    if np.linalg.norm(u) < 0.1:
        u = np.cross(a, [0, 1, 0])
    u /= np.linalg.norm(u)

    def to3(zr):
        return V(*(q + a * zr[0] + u * zr[1]))

    edges = []
    for k in range(n):
        s, e = corners[k - 1], corners[k]  # primitive k runs from corner k-1 to corner k
        if np.linalg.norm(e - s) < 1e-7:
            continue
        g = geo[k]
        if g["kind"] == "line" or (s[1] == 0 and e[1] == 0):
            edges.append(Part.LineSegment(to3(s), to3(e)).toShape())
        else:
            ang_s = math.atan2(s[1] - g["c"][1], s[0] - g["c"][0])
            ang_e = math.atan2(e[1] - g["c"][1], e[0] - g["c"][0])
            mid_ref = g["mid"]
            am = math.atan2(mid_ref[1] - g["c"][1], mid_ref[0] - g["c"][0])
            # midpoint on the same side as the section's mid point
            span = (ang_e - ang_s) % (2 * math.pi)
            mid1 = ang_s + span / 2
            if not _in_arc(np.array([am]), ang_s, mid1, ang_e)[0]:
                mid1 = mid1 + math.pi
            mz = g["c"] + g["r"] * np.array([math.cos(mid1), math.sin(mid1)])
            edges.append(Part.Arc(to3(s), to3(mz), to3(e)).toShape())
    wire = Part.Wire(Part.__sortEdges__(edges))
    if not wire.isClosed():
        raise MeshImportError("the rebuilt cross-section is not closed")
    face = Part.Face(wire)
    solid = face.revolve(V(*q), V(*a), 360)
    if solid.ShapeType != "Solid":
        solid = Part.Solid(Part.Shell(solid.Faces))
    if solid.Volume < 0:
        solid.reverse()
    info = {"type": "turned", "axis": [round(float(x), 6) for x in a], "axis_point": [round(float(x), 4) for x in q],
            "profile": [{"kind": g["kind"], **({"radius": round(float(g["r"]), 3)} if g["kind"] == "arc" else {})}
                        for g in geo],
            "profile_corners": [[round(float(c[0]), 3), round(float(c[1]), 3)] for c in corners]}
    return solid, corners, geo, info


def _profile_distance(zr, corners, geo):
    """Distance of (z, r) points from the rebuilt profile curve."""
    best = np.full(len(zr), np.inf)
    n = len(geo)
    for k in range(n):
        s, e = corners[k - 1], corners[k]
        g = geo[k]
        if g["kind"] == "line" or np.linalg.norm(e - s) < 1e-9:
            d = e - s
            L2 = d @ d
            t = np.clip(((zr - s) @ d) / (L2 if L2 > 0 else 1), 0, 1)
            dist = np.linalg.norm(zr - (s + np.outer(t, d)), axis=1)
        else:
            dist = np.abs(np.hypot(*(zr - g["c"]).T) - g["r"])
            # outside the arc's angular span: distance to the nearer end
            ends = np.minimum(np.linalg.norm(zr - s, axis=1), np.linalg.norm(zr - e, axis=1))
            ang = np.arctan2((zr - g["c"])[:, 1], (zr - g["c"])[:, 0])
            a0 = math.atan2(s[1] - g["c"][1], s[0] - g["c"][0])
            a1 = math.atan2(e[1] - g["c"][1], e[0] - g["c"][0])
            am = math.atan2(g["mid"][1] - g["c"][1], g["mid"][0] - g["c"][0])
            inside = _in_arc(ang, a0, am, a1)
            dist = np.where(inside, dist, ends)
        best = np.minimum(best, dist)
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Cylinder fit (holes and bosses)
# ─────────────────────────────────────────────────────────────────────────────

def _fit_cylinder(pts, normals, areas):
    """Cylinder through points with normals: axis = direction most perpendicular to the normals, then a circle
    fit in the plane square to it. Returns (point on axis, axis, radius, max error)."""
    M = (normals * areas[:, None]).T @ normals
    vals, vecs = np.linalg.eigh(M)
    ax = vecs[:, 0]
    ax = _snap_axis(ax)
    u = np.cross(ax, [1, 0, 0])
    if np.linalg.norm(u) < 0.1:
        u = np.cross(ax, [0, 1, 0])
    u /= np.linalg.norm(u)
    v = np.cross(ax, u)
    c0 = pts.mean(0)
    xy = np.stack([(pts - c0) @ u, (pts - c0) @ v], 1)
    cen, r, err = _fit_circle(xy)
    p0 = c0 + u * cen[0] + v * cen[1]
    return p0, ax, r, err


def _snap_axis(ax):
    ax = ax / np.linalg.norm(ax)
    for e in np.eye(3):
        if abs(abs(ax @ e) - 1) < 1e-4:
            return e * np.sign(ax @ e)
    return ax


def _cut_feature_cylinders(m, solid, tri_mask, tol, axis=None, q0=None):
    """Fit each connected group of triangles in tri_mask as a cylinder and cut it (holes) from the solid.
    Each end is extended past the surface when the hole opens there (no bottom triangles at that end)."""
    feats = []
    idx = np.where(tri_mask)[0]
    if not len(idx):
        return solid, feats
    pairs = m.adjacency()
    pairs = pairs[tri_mask[pairs[:, 0]] & tri_mask[pairs[:, 1]]]
    lab = _components(len(m.T), pairs)
    for g in np.unique(lab[idx]):
        tri = idx[lab[idx] == g]
        if m.A[tri].sum() < 1e-6 * m.A.sum():
            continue
        # wall triangles first: a hole's bottom (if any) has normals along the axis
        p0, ax, r, err = _fit_cylinder(m.C[tri], m.N[tri], m.A[tri])
        along = np.abs(m.N[tri] @ ax)
        wall = tri[along < 0.2]
        bottom = tri[along > 0.98]
        if len(wall) < 6:
            continue
        vid = np.unique(m.T[wall].ravel())
        pts = m.P[vid]
        p0, ax, r, err = _fit_cylinder_pts(pts, ax)
        if err > max(5 * tol, 0.02 * r) or r <= 0:
            continue
        if axis is not None:
            p0, ax = _snap_to_turned(p0, ax, q0, axis, tol)
            p0, ax, r, err = _fit_cylinder_pts(pts, ax, p0)
        t = (pts - p0) @ ax
        t0, t1 = float(t.min()), float(t.max())
        # concave (hole) when the wall normals point at the axis
        rad = (m.C[wall] - p0) - np.outer((m.C[wall] - p0) @ ax, ax)
        concave = float(np.mean(np.einsum("ij,ij->i", rad, m.N[wall]))) < 0
        if not concave:
            continue  # bosses on turned parts are rare; left to the revolved shape
        bt = (m.C[bottom] - p0) @ ax if len(bottom) else np.array([])
        ext = max(1.0, 0.05 * (t1 - t0))
        lo = t0 if (len(bt) and np.any(np.abs(bt - t0) < 5 * tol)) else t0 - ext
        hi = t1 if (len(bt) and np.any(np.abs(bt - t1) < 5 * tol)) else t1 + ext
        r_s = _snap_value(r * 2, tol) / 2
        cyl = Part.makeCylinder(r_s, hi - lo, V(*(p0 + ax * lo)), V(*ax))
        solid = solid.cut(cyl)
        feats.append({"kind": "hole", "diameter": round(2 * r_s, 3), "axis": [round(float(x), 4) for x in ax],
                      "blind": bool(lo == t0 or hi == t1), "depth": round(t1 - t0, 3)})
    return solid, feats


def _fit_cylinder_pts(pts, ax, centre=None):
    """Circle fit of the points seen along the axis; with `centre` given, the axis is held through it and only
    the radius is fitted."""
    u = np.cross(ax, [1, 0, 0])
    if np.linalg.norm(u) < 0.1:
        u = np.cross(ax, [0, 1, 0])
    u /= np.linalg.norm(u)
    v = np.cross(ax, u)
    if centre is not None:
        d = pts - centre
        rr = np.linalg.norm(d - np.outer(d @ ax, ax), axis=1)
        r = float(rr.mean())
        return centre, ax, r, float(np.abs(rr - r).max())
    c0 = pts.mean(0)
    xy = np.stack([(pts - c0) @ u, (pts - c0) @ v], 1)
    cen, r, err = _fit_circle(xy)
    return c0 + u * cen[0] + v * cen[1], ax, r, err


def _snap_to_turned(p0, ax, q, a, tol, step_deg=0.5):
    """Holes in a turned part are almost always radial (square to the main axis and through it) at a round angle,
    or parallel to the axis. Snap a fitted axis that is within a small error of that, so that opposite radial holes
    are exactly coaxial and the hole positions read as design values."""
    c = float(ax @ a)
    u = np.cross(a, [1, 0, 0]) if abs(a[0]) < 0.9 else np.cross(a, [0, 1, 0])
    u /= np.linalg.norm(u)
    v = np.cross(a, u)
    if abs(c) > 1 - 1e-4:  # axial hole: parallel to the main axis
        ax = a * np.sign(c)
        d = p0 - q
        d = d - a * (d @ a)
        # snapped in polar coordinates (radius, angle about the axis): a bolt circle's holes at 45° sit at
        # 70·cos 45° = 49.497, which X/Y rounding would move to 49.5
        rho = float(np.linalg.norm(d))
        if rho < tol:
            return q.copy(), ax
        th = math.degrees(math.atan2(d @ v, d @ u))
        ths = round(th / step_deg) * step_deg
        if abs(ths - th) < 0.05:
            th = ths
        t = math.radians(th)
        p0 = q + (u * math.cos(t) + v * math.sin(t)) * _snap_value(rho, tol)
        return np.where(np.abs(p0) < 1e-9, 0.0, p0), ax
    if abs(c) > 2e-3:
        return p0, ax
    ax = ax - a * c
    ax /= np.linalg.norm(ax)
    th = math.degrees(math.atan2(ax @ v, ax @ u))
    ths = round(th / step_deg) * step_deg
    if abs(ths - th) < 0.05:
        t = math.radians(ths)
        ax = u * math.cos(t) + v * math.sin(t)
        # snap tiny components (cos 90° etc.) to exactly zero
        ax = np.where(np.abs(ax) < 1e-12, 0.0, ax)
        ax /= np.linalg.norm(ax)
    # through the main axis when it passes within the tolerance
    d = p0 - q
    z = float(d @ a)
    perp = d - a * z - ax * float((d - a * z) @ ax)
    if np.linalg.norm(perp) < 5 * tol:
        p0 = q + a * _snap_value(z, tol)
    return p0, ax


# ─────────────────────────────────────────────────────────────────────────────
# Prismatic bodies
# ─────────────────────────────────────────────────────────────────────────────

def _prismatic_solid(m, tol):
    """Planar faces merged exactly; each smooth curved patch fitted as a cylinder and snapped (cut for holes,
    fused for bosses)."""
    sh = Part.Shape()
    sh.makeShapeFromMesh(m.mesh.Topology, tol)
    solid = Part.Solid(Part.Shell(sh.Faces))
    if solid.Volume < 0:
        solid.reverse()
    solid = solid.removeSplitter()
    ref = solid.copy()  # the mesh as a solid: what is inside the part
    # curved triangles: those whose neighbours bend smoothly (dihedral between 0.5° and 30°)
    pairs = m.adjacency()
    cosang = np.einsum("ij,ij->i", m.N[pairs[:, 0]], m.N[pairs[:, 1]])
    smooth = (cosang < math.cos(math.radians(0.5))) & (cosang > math.cos(math.radians(30)))
    curved = np.zeros(len(m.T), bool)
    curved[pairs[smooth].ravel()] = True
    feats = []
    # patches: curved triangles joined across any smooth edge (the two halves of a quad are coplanar)
    gentle = cosang > math.cos(math.radians(30))
    sp = pairs[gentle & curved[pairs[:, 0]] & curved[pairs[:, 1]]]
    lab = _components(len(m.T), sp)
    for g in np.unique(lab[curved]):
        tri = np.where((lab == g) & curved)[0]
        if len(tri) < 6:
            continue
        vid = np.unique(m.T[tri].ravel())
        M = (m.N[tri] * m.A[tri, None]).T @ m.N[tri]
        ax = _snap_axis(np.linalg.eigh(M)[1][:, 0])
        p0, ax, r, err = _fit_cylinder_pts(m.P[vid], ax)
        if err > max(5 * tol, 0.02 * r):
            continue
        t = (m.P[vid] - p0) @ ax
        t0, t1 = float(t.min()), float(t.max())
        rad = (m.C[tri] - p0) - np.outer((m.C[tri] - p0) @ ax, ax)
        concave = float(np.mean(np.einsum("ij,ij->i", rad, m.N[tri]))) < 0
        r_s = _snap_value(r * 2, tol) / 2
        # an end is extended past the surface where that is the open side: outside the part for a hole
        # (through / entry), inside it for a boss (its base); flush ends would leave split slivers
        ext = max(0.5, 0.05 * (t1 - t0))
        lo, hi = t0, t1
        for end, sgn in ((t0, -1), (t1, 1)):
            beyond = V(*(p0 + ax * (end + sgn * ext * 0.5)))
            inside = ref.isInside(beyond, 1e-6, True)
            if inside != concave:
                if sgn < 0:
                    lo = end - ext
                else:
                    hi = end + ext
        # The tessellation's vertices lie exactly on the circle, so an exact-size tool would only touch the polygon
        # walls along lines and leave the cylinder in strips. First the faceted feature is filled (hole) or removed
        # (boss) slightly oversize over its own extent, then the exact cylinder is cut / added.
        span = Part.makeCylinder(r_s + 1e-3, t1 - t0, V(*(p0 + ax * t0)), V(*ax))
        solid = (solid.fuse(span) if concave else solid.cut(span)).removeSplitter()
        cyl = Part.makeCylinder(r_s, hi - lo, V(*(p0 + ax * lo)), V(*ax))
        solid = solid.cut(cyl) if concave else solid.fuse(cyl)
        feats.append({"kind": "hole" if concave else "boss", "diameter": round(2 * r_s, 3),
                      "axis": [round(float(x), 4) for x in ax]})
    solid = solid.removeSplitter()
    return solid, feats


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _deviation(solid, m, samples=1500):
    """Largest distance of (sampled) mesh vertices from the rebuilt solid's surface, mm."""
    rng = np.random.default_rng(0)
    pts = m.P if len(m.P) <= samples else m.P[rng.choice(len(m.P), samples, replace=False)]
    sh = solid.Shells[0] if solid.Shells else solid
    worst = 0.0
    for p in pts:
        d = sh.distToShape(Part.Vertex(V(*p)))[0]
        worst = max(worst, d)
    return worst


def rebuild(path, tol=None):
    """Exact solids for every body of the mesh file. Returns ([(name, solid)], report)."""
    mesh = Mesh.Mesh(path)
    if mesh.CountFacets == 0:
        raise MeshImportError("the mesh file has no triangles")
    mesh.harmonizeNormals()
    comps = mesh.getSeparateComponents() or [mesh]
    comps = sorted(comps, key=lambda c: -abs(c.Volume))
    solids, report = [], {"source": "mesh", "facets": int(mesh.CountFacets), "bodies": []}
    for k, cm in enumerate(comps):
        if cm.CountFacets < 4:
            continue
        m = _M(cm)
        t = tol or max(0.02, 2e-4 * m.size)
        body = {"facets": int(cm.CountFacets), "mesh_volume": round(abs(cm.Volume), 2)}
        solid = None
        try:
            ax = _fit_axis(m)
            if ax is not None and ax[2] > 0.85:
                q, a, frac = ax
                q, a = _world_frame(q, _snap_axis(a), t)
                solid, corners, geo, info = _revolved_solid(m, q, a, t)
                body.update(info)
                body["axisymmetric_fraction"] = round(float(frac), 4)
                # features that are not part of the surface of revolution
                z, r = _zr(m.C, q, a)
                off = _profile_distance(np.stack([z, r], 1), corners, geo) > 4 * t
                solid, feats = _cut_feature_cylinders(m, solid, off, t, a, q)
                body["features"] = feats
        except (MeshImportError, Part.OCCError, ValueError, np.linalg.LinAlgError) as e:
            body["turned_error"] = str(e)
            solid = None
        if solid is None or not solid.isValid() or abs(solid.Volume - abs(cm.Volume)) > 0.02 * abs(cm.Volume):
            if solid is not None:
                body["turned_rejected"] = f"volume {solid.Volume:.1f} vs mesh {abs(cm.Volume):.1f}"
            solid, feats = _prismatic_solid(m, t)
            body.update({"type": "prismatic", "features": feats})
        solid = solid.removeSplitter()
        if not solid.isValid():
            solid.fix(1e-7, 1e-7, 1e-7)
        if solid.ShapeType == "Compound" and len(solid.Solids) == 1:
            solid = solid.Solids[0]
        body["volume"] = round(solid.Volume, 2)
        body["volume_error_pct"] = round(100 * (solid.Volume - abs(cm.Volume)) / abs(cm.Volume), 3)
        body["faces"] = len(solid.Faces)
        body["max_deviation_mm"] = round(_deviation(solid, m), 4)
        name = "Body" if len(comps) == 1 else f"Body{k + 1}"
        body["name"] = name
        report["bodies"].append(body)
        solids.append((name, solid))
    if not solids:
        raise MeshImportError("no closed body found in the mesh")
    return solids, report
