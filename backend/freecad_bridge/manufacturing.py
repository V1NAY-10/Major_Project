"""
Phase 4 analysis — runs INSIDE FreeCAD, registered into engine.METHODS (read-only: nothing here edits the model).

  mass_properties  volume, area, centre of mass and inertia per part (and the assembly) for given densities
  dfm              design-for-manufacturing findings for CNC machining, 3D printing or sheet metal, each tied to IDs
                   and, where possible, a one-click fix (a tool call)
  interference     overlapping parts (overlap volume) and near misses (clearance) between parts
  bom_groups       identical parts grouped (geometry signature) for the bill of materials
  part_meshes      one mesh per part (visibility toggles / exploded view in the viewer)
  drawing          a 2D drawing (front / top / left / isometric views in first-angle projection, overall dimensions,
                   hole callouts, title block) from TechDraw projections, written as SVG and PDF
"""

import datetime
import math

import FreeCAD as App  # type: ignore
import Part  # type: ignore

V = App.Vector
E = None


def register(engine):
    global E
    E = engine
    engine.METHODS.update({"mass_properties": mass_properties, "dfm": dfm, "interference": interference,
                           "bom_groups": bom_groups, "part_meshes": part_meshes, "drawing": drawing})
    for _name, _fn in list(engine.METHODS.items()):
        if not hasattr(engine, _name):
            setattr(engine, _name, _fn)  # callable as engine.<method> like the built-in ones


def _parts(an, object=None):
    objs = an.objs
    if object:
        q = str(object).lower()
        objs = [o for o in objs if q in (o.Name.lower(), o.Label.lower(), an.parts.get(o.Name, "").lower())]
        if not objs:
            raise E.EngineError(f"No part '{object}'")
    return objs


# ── mass properties ──────────────────────────────────────────────────────────

def mass_properties(session_id, work_path, registry=None, densities=None, object=None):
    """densities: {part ID: g/cm³}. Inertia is about each part's centre of mass, in kg·mm² (volume inertia × density).
    Parts without a density report geometry only."""
    doc = E._get_doc(session_id, work_path)
    an = E._analyze(doc, registry)
    densities = {str(k).upper(): float(v) for k, v in (densities or {}).items() if v}
    rows, m_tot, mc = [], 0.0, V()
    for o in _parts(an, object):
        s = an.shapes[o.Name]
        pid = an.parts[o.Name]
        rho = densities.get(pid)
        sol = s.Solids[0] if len(s.Solids) == 1 else s
        com = sol.CenterOfMass if hasattr(sol, "CenterOfMass") else s.BoundBox.Center
        row = {"part": pid, "object": o.Name, "label": o.Label, "volume": E._r(s.Volume, 3), "area": E._r(s.Area, 3),
               "center_of_mass": E._vl(com, 4)}
        if len(s.Solids) == 1:
            I = sol.MatrixOfInertia  # mm⁵ (unit density), about the centre of mass
            moments = list(sol.PrincipalProperties["Moments"])
        else:
            I, moments = None, None
        if rho:
            mass_g = rho * s.Volume / 1000.0           # g/cm³ · mm³ → g
            k = rho * 1e-6                              # (g/cm³ → kg/mm³) = ρ·1e-6
            row.update(density=rho, mass_g=E._r(mass_g, 4), mass_kg=E._r(mass_g / 1000.0, 6))
            if I is not None:
                row["inertia_kg_mm2"] = [[E._r(I.A11 * k, 4), E._r(I.A12 * k, 4), E._r(I.A13 * k, 4)],
                                         [E._r(I.A21 * k, 4), E._r(I.A22 * k, 4), E._r(I.A23 * k, 4)],
                                         [E._r(I.A31 * k, 4), E._r(I.A32 * k, 4), E._r(I.A33 * k, 4)]]
                row["principal_moments_kg_mm2"] = [E._r(x * k, 4) for x in moments]
            m_tot += mass_g
            mc = mc + com * mass_g
        rows.append(row)
    out = {"parts": rows, "registry": an.reg}
    if m_tot > 0:
        out["total"] = {"mass_g": E._r(m_tot, 4), "mass_kg": E._r(m_tot / 1000.0, 6),
                        "center_of_mass": E._vl(mc * (1.0 / m_tot), 4),
                        "parts_without_material": [r["part"] for r in rows if "mass_g" not in r]}
    return out


# ── DFM ──────────────────────────────────────────────────────────────────────

DFM_DEFAULTS = {
    "cnc": {"tool_radius": 2.0, "max_hole_depth_ratio": 5.0, "hard_hole_depth_ratio": 10.0, "min_wall": 1.0,
            "max_slot_depth_ratio": 3.0},
    "print3d": {"nozzle": 0.4, "overhang_deg": 45.0, "min_hole": 1.0, "min_boss": 2.0},
    "sheet": {"hole_edge_factor": 2.0, "hole_bend_factor": 2.5},
}


def _finding(sev, rule, msg, ids, value=None, limit=None, fix=None):
    out = {"severity": sev, "rule": rule, "message": msg, "features": [i for i in ids if i]}
    if value is not None:
        out["value"] = E._r(value)
    if limit is not None:
        out["limit"] = E._r(limit)
    if fix:
        out["fix"] = fix
    return out


def dfm(session_id, work_path, registry=None, process="cnc", params=None, object=None):
    """Findings [{severity: error | warning | info, rule, message, features, value, limit, fix?}] per part.
    process: cnc | print3d | sheet."""
    process = str(process or "cnc").lower().replace("3d", "print3d").replace("printprint3d", "print3d")
    if process in ("print", "printing", "3d_printing", "fdm"):
        process = "print3d"
    if process not in DFM_DEFAULTS:
        raise E.EngineError("process must be cnc, print3d or sheet")
    p = dict(DFM_DEFAULTS[process])
    p.update({k: float(v) for k, v in (params or {}).items() if v is not None})
    doc = E._get_doc(session_id, work_path)
    an = E._analyze(doc, registry)
    from freecad_bridge import analysis
    findings = []
    for o in _parts(an, object):
        feats = [f for f in an.feats if f["_obj"] == o.Name]
        shape = an.shapes[o.Name]
        if process == "cnc":
            findings += _dfm_cnc(an, o, shape, feats, p, session_id, work_path, registry)
        elif process == "print3d":
            findings += _dfm_print(an, o, shape, feats, p, session_id, work_path, registry)
        else:
            findings += _dfm_sheet(an, o, shape, feats, p, analysis)
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["rule"]))
    return {"process": process, "params": p, "findings": findings,
            "counts": {s: sum(1 for f in findings if f["severity"] == s) for s in order}, "registry": an.reg}


def _nearest_drill(d):
    sizes = [round(0.5 + 0.1 * i, 2) for i in range(126)] + [13.0 + 0.5 * i for i in range(25)]
    return min(sizes, key=lambda s: abs(s - d))


def _std_hole(d):
    """True when d is a standard drill / clearance / tap size (within 0.0015 mm)."""
    for x in [round(0.5 + 0.1 * i, 2) for i in range(126)] + [13.0 + 0.5 * i for i in range(25)] + \
            [2.05, 2.15, 2.65, 3.3, 4.2, 6.8, 8.5, 10.2, 5.2, 8.8, 10.8, 2.9, 5.5, 6.6, 13.5, 15.5, 17.5, 4.3, 4.5]:
        if abs(d - x) <= 0.0015:
            return True
    return any(abs(d - k * 25.4 / 64) <= 0.0015 for k in range(1, 65))


def _dfm_cnc(an, o, shape, feats, p, sid, wp, reg):
    out = []
    for h in feats:
        if h["kind"] == "hole" and not h.get("counterbore_of"):
            ratio = h["depth"] / h["diameter"]
            if ratio > p["max_hole_depth_ratio"]:
                sev = "error" if ratio > p["hard_hole_depth_ratio"] else "warning"
                out.append(_finding(sev, "hole depth / diameter",
                                    f"{h['id']}: Ø{h['diameter']:g} × {h['depth']:g} deep is {ratio:.1f}× its diameter "
                                    f"(drills reach about {p['max_hole_depth_ratio']:g}×; deeper needs peck drilling or a gun drill)",
                                    [h["id"]], ratio, p["max_hole_depth_ratio"]))
            if not _std_hole(h["diameter"]):
                nd = _nearest_drill(h["diameter"])
                out.append(_finding("warning", "non-standard hole size",
                                    f"{h['id']}: Ø{h['diameter']:g} is not a standard drill size (nearest Ø{nd:g})",
                                    [h["id"]], h["diameter"], nd,
                                    {"tool": "resize_hole", "args": {"hole": h["id"], "diameter": nd},
                                     "label": f"Resize {h['id']} to Ø{nd:g}"}))
        if h["kind"] == "pocket":
            r, tr = h["_corner_radius"], p["tool_radius"]
            if r < tr - 1e-9:
                fix_r = min(tr, h["width"] / 2 - 0.1)
                out.append(_finding("error" if r == 0 else "warning", "internal corner radius",
                                    f"{h['id']}: corner radius {r:g} mm is smaller than the cutter radius {tr:g} mm"
                                    + (" (sharp internal corners can't be milled)" if r == 0 else ""),
                                    [h["id"]], r, tr,
                                    {"tool": "edit_pocket", "args": {"pocket": h["id"], "corner_radius": E._r(fix_r)},
                                     "label": f"Round {h['id']} corners to R{E._r(fix_r):g}"} if fix_r > 0 else None))
        if h["kind"] == "slot":
            ratio = h["_depth"] / h["_width"]
            if ratio > p["max_slot_depth_ratio"]:
                out.append(_finding("warning", "deep narrow slot",
                                    f"{h['id']}: {h['depth']:g} deep × {h['width']:g} wide ({ratio:.1f}:1) needs a long, thin cutter",
                                    [h["id"]], ratio, p["max_slot_depth_ratio"]))
            if h["shape"] == "rectangular" and not h["through"]:
                out.append(_finding("warning", "internal corner radius",
                                    f"{h['id']}: a blind rectangular slot has sharp internal corners a round cutter can't make",
                                    [h["id"]]))
    wt = _wall_of(an, o)
    if wt is not None and wt["min_thickness"] < p["min_wall"]:
        out.append(_finding("warning", "thin wall", f"{o.Label}: thinnest wall {wt['min_thickness']:g} mm between "
                            f"{' and '.join(x for x in wt['faces'] if x)} (min {p['min_wall']:g} mm)",
                            wt["faces"], wt["min_thickness"], p["min_wall"]))
    return out


def _wall_of(an, o):
    """Thinnest wall of one part (the engine's ray cast) without re-opening the document."""
    try:
        return E._wall_thickness_of(an, o, threshold=None)
    except Exception:
        return None


def _dfm_print(an, o, shape, feats, p, sid, wp, reg):
    out = []
    bb = shape.BoundBox
    _, tol = E._tolerances(shape)
    limit = math.sin(math.radians(p["overhang_deg"]))
    owners = E._face_owners(an)
    per_feat = {}
    for i, face in enumerate(shape.Faces):
        if face.BoundBox.ZMax <= bb.ZMin + tol * 10:
            continue  # on the build plate
        try:
            pts, tris = face.tessellate(max(bb.DiagonalLength / 300.0, 0.05))
        except Exception:
            continue
        area = 0.0
        for a, b, c in tris:
            A, B, C = pts[a], pts[b], pts[c]
            n = (B - A).cross(C - A)
            if n.Length < 1e-12:
                continue
            nn = V(n).normalize()
            mid = (A + B + C) * (1.0 / 3)
            nf = E.analysis.normal_at(face, mid)
            if nn.dot(nf) < 0:
                nn = nn.negative()
            if -nn.z > limit and mid.z > bb.ZMin + tol * 10:
                area += n.Length / 2
        if area > max(1.0, face.Area * 0.02):
            f = owners.get((o.Name, i))
            fid = f["id"] if f else f"face {i + 1}"
            per_feat[fid] = per_feat.get(fid, 0.0) + area
    for fid, area in sorted(per_feat.items(), key=lambda x: -x[1]):
        out.append(_finding("warning", "overhang", f"{fid}: {area:.1f} mm² faces downwards steeper than "
                            f"{p['overhang_deg']:g}° from vertical and needs support (or re-orient the part)", [fid], area))
    for f in feats:
        if f["kind"] == "hole" and f["diameter"] < p["min_hole"]:
            out.append(_finding("warning", "minimum feature size", f"{f['id']}: Ø{f['diameter']:g} hole is below "
                                f"{p['min_hole']:g} mm and may close up when printed", [f["id"]], f["diameter"], p["min_hole"]))
        if f["kind"] == "boss" and f["diameter"] < p["min_boss"]:
            out.append(_finding("warning", "minimum feature size", f"{f['id']}: Ø{f['diameter']:g} pin is below "
                                f"{p['min_boss']:g} mm and may break", [f["id"]], f["diameter"], p["min_boss"]))
    wt = _wall_of(an, o)
    if wt is not None:
        two = 2 * p["nozzle"]
        if wt["min_thickness"] < two:
            out.append(_finding("error", "wall vs nozzle", f"{o.Label}: thinnest wall {wt['min_thickness']:g} mm is under "
                                f"two extrusion widths ({two:g} mm for a {p['nozzle']:g} mm nozzle)",
                                wt["faces"], wt["min_thickness"], two))
    return out


def sheet_thickness(an, o):
    """Sheet thickness if the part is a constant-thickness plate (volume ≈ half the surface area × thickness), else None."""
    s = an.shapes[o.Name]
    ths = [d["value"] for d in an.dims if d["object"] == o.Name and d["type"] == "thickness"]
    if not ths:
        return None
    t = min(ths)
    if t <= 0 or abs(s.Volume - s.Area / 2 * t) > 0.15 * s.Volume:
        return None
    return t


def _dfm_sheet(an, o, shape, feats, p, analysis):
    out = []
    t = sheet_thickness(an, o)
    if t is None:
        return [_finding("info", "not sheet metal", f"{o.Label} is not a constant-thickness plate; sheet-metal rules skipped",
                         [an.parts.get(o.Name)])]
    rounds = [f for f in feats if f["kind"] == "round" and f.get("_a") is not None and not f.get("toroidal")]
    bends = []
    for r1 in rounds:
        for r2 in rounds:
            if r1 is r2 or not r1["concave"] or r2["concave"]:
                continue
            if abs(abs(r1["_a"].dot(r2["_a"])) - 1) < 1e-6 and analysis.axis_offset(r1, r2) < 1e-3 \
                    and abs((r2["_r"] - r1["_r"]) - t) < 0.01:
                bends.append((r1, r2))
    holes = [f for f in feats if f["kind"] == "hole" and not f.get("counterbore_of")]
    edge_dims = {}
    for d in an.dims:
        if d["object"] == o.Name and d["type"] == "edge_distance":
            edge_dims.setdefault(d["refs"][0], []).append(d)
    for h in holes:
        for d in edge_dims.get(h["id"], []):
            edge = d["value"] - h["radius"]
            need = p["hole_edge_factor"] * t
            if edge < need - 1e-6:
                out.append(_finding("warning", "hole to edge", f"{h['id']}: hole edge is {edge:.2f} mm from {d['refs'][1]} "
                                    f"(min {p['hole_edge_factor']:g} × t = {need:g} mm); it may tear when punched",
                                    [h["id"], d["refs"][1]], edge, need,
                                    {"tool": "set_dimension", "args": {"dimension": d["id"], "value": E._r(h["radius"] + need)},
                                     "label": f"Move {h['id']} to {E._r(need):g} mm from the edge"}))
        for inner, outer in bends:
            faces_h = Part.makeCompound([shape.Faces[i] for i in h["_faces"]])
            dist = faces_h.distToShape(Part.makeCompound([shape.Faces[i] for i in inner["_faces"] + outer["_faces"]]))[0]
            need = p["hole_bend_factor"] * t + inner["_r"]
            if dist < need - 1e-6:
                out.append(_finding("warning", "hole to bend", f"{h['id']}: {dist:.2f} mm from bend {inner['id']} "
                                    f"(min {p['hole_bend_factor']:g} × t + R = {need:g} mm); it will distort when bent",
                                    [h["id"], inner["id"]], dist, need))
    if not out:
        out.append(_finding("info", "sheet metal", f"{o.Label}: sheet {t:g} mm, {len(bends)} bend(s), no problems found",
                            [an.parts.get(o.Name)]))
    return out


# ── interference ─────────────────────────────────────────────────────────────

def interference(session_id, work_path, registry=None, clearance=1.0):
    """Pairs of parts that overlap (overlap volume) or come closer than `clearance` mm."""
    doc = E._get_doc(session_id, work_path)
    an = E._analyze(doc, registry)
    objs = an.objs
    rows = []
    for i in range(len(objs)):
        for j in range(i + 1, len(objs)):
            a, b = an.shapes[objs[i].Name], an.shapes[objs[j].Name]
            bba = App.BoundBox(a.BoundBox)
            bba.enlarge(float(clearance))
            if not bba.intersect(b.BoundBox):
                continue
            common = a.common(b)
            vol = common.Volume if not common.isNull() and common.Solids else 0.0
            dist = 0.0 if vol > 1e-6 else a.distToShape(b)[0]
            if vol > 1e-6 or dist < float(clearance):
                rows.append({"parts": [an.parts[objs[i].Name], an.parts[objs[j].Name]],
                             "labels": [objs[i].Label, objs[j].Label],
                             "status": "interference" if vol > 1e-6 else "touching" if dist < 1e-6 else "clearance",
                             "overlap_volume": E._r(vol, 3), "distance": E._r(dist)})
    return {"pairs": rows, "clearance": float(clearance), "registry": an.reg}


# ── BOM ──────────────────────────────────────────────────────────────────────

def bom_groups(session_id, work_path, registry=None):
    """Parts grouped by geometry (volume, area, sorted bounding-box sizes): identical copies are one BOM line."""
    doc = E._get_doc(session_id, work_path)
    an = E._analyze(doc, registry)
    groups = []
    for o in an.objs:
        s = an.shapes[o.Name]
        bb = s.BoundBox
        sig = (round(s.Volume, 1), round(s.Area, 1), tuple(sorted(round(x, 2) for x in (bb.XLength, bb.YLength, bb.ZLength))))
        for g in groups:
            if g["_sig"] == sig:
                g["parts"].append(an.parts[o.Name])
                g["labels"].append(o.Label)
                break
        else:
            groups.append({"_sig": sig, "parts": [an.parts[o.Name]], "labels": [o.Label], "volume": E._r(s.Volume, 3),
                           "size": sorted([E._r(bb.XLength, 2), E._r(bb.YLength, 2), E._r(bb.ZLength, 2)], reverse=True)})
    for g in groups:
        g.pop("_sig")
        g["quantity"] = len(g["parts"])
    return {"groups": groups, "registry": an.reg}


# ── meshes per part ──────────────────────────────────────────────────────────

def part_meshes(session_id, work_path, registry=None, max_triangles=120000):
    doc = E._get_doc(session_id, work_path)
    an = E._analyze(doc, registry)
    from freecad_bridge.editing import _tess
    out, total = [], 0
    for o in an.objs:
        s = an.shapes[o.Name]
        pos, idx = _tess(s, max(s.BoundBox.DiagonalLength / 250.0, 0.05))
        total += len(idx) // 3
        if total > max_triangles:
            break
        out.append({"part": an.parts[o.Name], "label": o.Label, "center": E._vl(s.BoundBox.Center, 3),
                    "positions": pos, "indices": idx})
    return {"parts": out, "registry": an.reg}


# ── 2D drawing ───────────────────────────────────────────────────────────────

SHEET = (420.0, 297.0)  # A3 landscape, mm
SCALES = [(10, 1), (5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10), (1, 20), (1, 50)]


def _view_frames():
    """name → (x axis, y axis, towards the viewer) — first-angle projection (ISO 128)."""
    iso_t = V(1, -1, 1).normalize()
    iso_x = V(1, 1, 0).normalize()
    return {"front": (V(1, 0, 0), V(0, 0, 1), V(0, -1, 0)),
            "top": (V(1, 0, 0), V(0, 1, 0), V(0, 0, 1)),
            "left": (V(0, -1, 0), V(0, 0, 1), V(-1, 0, 0)),
            "iso": (iso_x, iso_t.cross(iso_x).normalize(), iso_t)}


def _project(shape, frame, defl):
    """Visible and hidden 2D polylines of `shape` seen along `frame` (TechDraw.projectEx), in view coordinates."""
    import TechDraw  # type: ignore
    x, y, t = frame
    m = App.Matrix(x.x, x.y, x.z, 0, y.x, y.y, y.z, 0, t.x, t.y, t.z, 0, 0, 0, 0, 1)
    s = shape.copy()
    s.transformShape(m)
    groups = TechDraw.projectEx(s, V(0, 0, 1))
    vis, hid = [], []
    for k, g in enumerate(groups):
        if g is None or g.isNull():
            continue
        target = vis if k in (0, 1, 3) else hid if k in (5, 6, 8) else None
        if target is None:
            continue
        for e in g.Edges:
            try:
                pts = e.discretize(Deflection=defl)
            except Exception:
                continue
            target.append([(p.x, p.y) for p in pts])
    return vis, hid, (s.BoundBox.XMin, s.BoundBox.YMin, s.BoundBox.XMax, s.BoundBox.YMax)


class _Sheet:
    """Collects lines, dashed lines, circles and text in sheet mm (origin bottom-left) and writes SVG / PDF."""

    def __init__(self):
        self.items = []

    def line(self, pts, width=0.35, dash=False):
        self.items.append(("line", pts, width, dash))

    def text(self, x, y, s, size=3.5, anchor="start"):
        self.items.append(("text", x, y, s, size, anchor))

    def svg(self):
        W, H = SHEET
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}">',
               f'<rect width="{W}" height="{H}" fill="white"/>',
               '<g fill="none" stroke="black" stroke-linecap="round" stroke-linejoin="round">']
        for it in self.items:
            if it[0] == "line":
                _, pts, w, dash = it
                d = " ".join(f"{'M' if i == 0 else 'L'}{x:.3f},{H - y:.3f}" for i, (x, y) in enumerate(pts))
                dash_attr = ' stroke-dasharray="1.5,1"' if dash else ""
                out.append(f'<path d="{d}" stroke-width="{w}"{dash_attr}/>')
        out.append("</g><g font-family=\"Arial, Helvetica, sans-serif\" fill=\"black\">")
        for it in self.items:
            if it[0] == "text":
                _, x, y, s, size, anchor = it
                esc = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                out.append(f'<text x="{x:.3f}" y="{H - y:.3f}" font-size="{size}" text-anchor="{anchor}">{esc}</text>')
        out.append("</g></svg>")
        return "\n".join(out)

    def pdf(self):
        k = 72.0 / 25.4
        W, H = SHEET
        ops = ["1 J 1 j"]
        for it in self.items:
            if it[0] == "line":
                _, pts, w, dash = it
                ops.append(f"{w * k:.3f} w " + ("[4.25 2.83] 0 d" if dash else "[] 0 d"))
                ops.append(" ".join(f"{x * k:.3f} {y * k:.3f} {'m' if i == 0 else 'l'}" for i, (x, y) in enumerate(pts)) + " S")
        for it in self.items:
            if it[0] == "text":
                _, x, y, s, size, anchor = it
                width = 0.5 * size * len(s)  # Helvetica average glyph width ≈ 0.5 em
                x0 = x - width / 2 if anchor == "middle" else x - width if anchor == "end" else x
                txt = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                ops.append(f"BT /F1 {size * k:.2f} Tf {x0 * k:.3f} {y * k:.3f} Td ({txt}) Tj ET")
        content = "\n".join(ops).encode("cp1252", "replace")
        objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W * k:.2f} {H * k:.2f}] /Contents 4 0 R "
                f"/Resources << /Font << /F1 5 0 R >> >> >>".encode(),
                b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
        out = b"%PDF-1.4\n"
        offsets = []
        for i, o in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return out


def _dim_h(sheet, x0, x1, y, text, ext_from=None):
    """Horizontal dimension line at height y between x0 and x1 with arrows and centred text."""
    sheet.line([(x0, y), (x1, y)], 0.18)
    for x, s in ((x0, 1), (x1, -1)):
        sheet.line([(x + 2.5 * s, y + 0.8), (x, y), (x + 2.5 * s, y - 0.8)], 0.18)
        if ext_from is not None:
            sheet.line([(x, ext_from), (x, y - 1.5 if y < ext_from else y + 1.5)], 0.13)
    sheet.text((x0 + x1) / 2, y + 1.2, text, 3.2, "middle")


def _dim_v(sheet, y0, y1, x, text, ext_from=None):
    sheet.line([(x, y0), (x, y1)], 0.18)
    for y, s in ((y0, 1), (y1, -1)):
        sheet.line([(x - 0.8, y + 2.5 * s), (x, y), (x + 0.8, y + 2.5 * s)], 0.18)
        if ext_from is not None:
            sheet.line([(ext_from, y), (x - 1.5 if x < ext_from else x + 1.5, y)], 0.13)
    sheet.text(x - 1.2, (y0 + y1) / 2, text, 3.2, "end")


def _fmt(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def drawing(session_id, work_path, svg_path, pdf_path, registry=None, object=None, title=None, material=None,
            mass_g=None):
    """A3 first-angle drawing of one part: front (top-left), top (below it), left (right of it), isometric, overall
    dimensions, hole callouts with centre marks on the top view, and a title block. Writes SVG and PDF."""
    import os
    doc = E._get_doc(session_id, work_path)
    an = E._analyze(doc, registry)
    objs = _parts(an, object)
    if len(objs) > 1 and not object:
        objs = [max(objs, key=lambda o: an.shapes[o.Name].Volume)]
    o = objs[0]
    shape = an.shapes[o.Name]
    bb = shape.BoundBox
    frames = _view_frames()
    defl = max(bb.DiagonalLength / 500.0, 0.02)
    views = {name: _project(shape, fr, defl) for name, fr in frames.items()}

    def size(name):
        b = views[name][2]
        return b[2] - b[0], b[3] - b[1]

    (fw, fh), (tw, th), (lw, lh), (iw, ih) = size("front"), size("top"), size("left"), size("iso")
    gap = 28.0
    avail_w, avail_h = SHEET[0] - 20 - 2 * gap, SHEET[1] - 20 - 50 - 2 * gap
    scale = SCALES[-1]
    for num, den in SCALES:
        f = num / den
        if (fw + lw + iw * 0.8) * f <= avail_w and (fh + th) * f <= avail_h:
            scale = (num, den)
            break
    f = scale[0] / scale[1]
    sheet = _Sheet()
    W, H = SHEET
    sheet.line([(10, 10), (W - 10, 10), (W - 10, H - 10), (10, H - 10), (10, 10)], 0.7)
    # view origins (sheet mm of each view's lower-left corner)
    fx0, fy0 = 10 + gap + 6, H - 10 - gap - fh * f
    origins = {"front": (fx0, fy0), "top": (fx0, fy0 - gap - th * f), "left": (fx0 + fw * f + gap, fy0),
               "iso": (W - 10 - gap - iw * f * 0.8, fy0 - gap * 0.5 - ih * f * 0.8)}
    vscale = {"front": f, "top": f, "left": f, "iso": f * 0.8}

    def place(name, pt):
        b = views[name][2]
        ox, oy = origins[name]
        return ox + (pt[0] - b[0]) * vscale[name], oy + (pt[1] - b[1]) * vscale[name]

    for name in ("front", "top", "left", "iso"):
        vis, hid, _ = views[name]
        for pl in hid:
            if name != "iso":
                sheet.line([place(name, p) for p in pl], 0.18, dash=True)
        for pl in vis:
            sheet.line([place(name, p) for p in pl], 0.5)
        lx, ly = origins[name]
        sheet.text(lx, ly - 6 if name != "front" else ly - 14, {"front": "FRONT", "top": "TOP", "left": "LEFT",
                                                                 "iso": "ISOMETRIC"}[name], 3.0)
    # overall dimensions
    fl, fb = origins["front"]
    _dim_h(sheet, fl, fl + fw * f, fb - 8, _fmt(bb.XLength), ext_from=fb)
    _dim_v(sheet, fb, fb + fh * f, fl - 8, _fmt(bb.ZLength), ext_from=fl)
    tl, tb = origins["top"]
    _dim_v(sheet, tb, tb + th * f, tl - 8, _fmt(bb.YLength), ext_from=tl)
    # hole callouts on the top view (holes along Z)
    holes = [h for h in an.feats if h["_obj"] == o.Name and h["kind"] == "hole" and not h.get("counterbore_of")
             and abs(abs(h["_a"].z) - 1) < 1e-6]
    groups = {}
    for h in holes:
        key = (h["diameter"], h["through"], None if h["through"] else h["depth"],
               (h.get("counterbore") or {}).get("diameter"), (h.get("counterbore") or {}).get("depth"),
               (h.get("countersinks") or [{}])[0].get("outer_diameter"))
        groups.setdefault(key, []).append(h)
    callouts = []
    for k, (key, hs) in enumerate(sorted(groups.items(), key=lambda kv: -kv[0][0])):
        d, thru, dep, cbd, cbdep, csd = key
        txt = f"{len(hs)}× Ø{_fmt(d)} " + ("THRU" if thru else f"↧{_fmt(dep)}")
        if cbd:
            txt += f" ⌴Ø{_fmt(cbd)} ↧{_fmt(cbdep)}"
        if csd:
            txt += f" ⌵Ø{_fmt(csd)}×90°"
        txt = txt.replace("↧", "DP ").replace("⌴", "CBORE ").replace("⌵", "CSK ")
        for h in hs:
            cx, cy = place("top", (h["_base"].x, h["_base"].y))
            r = h["radius"] * f
            sheet.line([(cx - r - 1.5, cy), (cx + r + 1.5, cy)], 0.13)
            sheet.line([(cx, cy - r - 1.5), (cx, cy + r + 1.5)], 0.13)
        h0 = hs[0]
        cx, cy = place("top", (h0["_base"].x, h0["_base"].y))
        lx = tl + tw * f + 6
        ly = tb + th * f - 4 - 6 * k
        r = h0["radius"] * f
        sheet.line([(cx + r * 0.7, cy + r * 0.7), (lx - 1, ly), (lx + 1.5 * len(txt) + 2, ly)], 0.18)
        sheet.text(lx, ly + 1, txt, 3.0)
        callouts.append(txt)
    # title block
    tbx, tby = W - 10 - 180, 10
    sheet.line([(tbx, tby), (tbx, tby + 40), (W - 10, tby + 40)], 0.5)
    for yy in (tby + 10, tby + 20, tby + 30):
        sheet.line([(tbx, yy), (W - 10, yy)], 0.25)
    sheet.line([(tbx + 90, tby), (tbx + 90, tby + 30)], 0.25)
    name = title or o.Label
    rows = [(tbx + 2, tby + 33, f"{name}", 5.0), (tbx + 2, tby + 23, f"Part {an.parts[o.Name]}  ·  {o.Label}", 3.2),
            (tbx + 92, tby + 23, f"Scale {scale[0]}:{scale[1]}  ·  First angle", 3.2),
            (tbx + 2, tby + 13, f"Material: {material or '—'}", 3.2),
            (tbx + 92, tby + 13, f"Mass: {(_fmt(mass_g) + ' g') if mass_g else '—'}", 3.2),
            (tbx + 2, tby + 3, f"Units: mm  ·  {datetime.date.today().isoformat()}", 3.2),
            (tbx + 92, tby + 3, "AetherCAD (FreeCAD TechDraw projection)", 3.2)]
    for x, y, s, sz in rows:
        sheet.text(x, y, s, sz)
    # first-angle symbol
    sx, sy = tbx - 28, tby + 8
    sheet.line([(sx, sy), (sx + 8, sy + 3), (sx + 8, sy + 9), (sx, sy + 12), (sx, sy)], 0.25)
    sheet.line([(sx + 22, sy + 6 - 6), (sx + 22, sy + 6 + 6)], 0.13)
    for rr in (3, 6):
        pts = [(sx + 22 + rr * math.cos(a / 24 * 2 * math.pi), sy + 6 + rr * math.sin(a / 24 * 2 * math.pi)) for a in range(25)]
        sheet.line(pts, 0.25)
    os.makedirs(os.path.dirname(svg_path), exist_ok=True)
    open(svg_path, "w", encoding="utf-8").write(sheet.svg())
    open(pdf_path, "wb").write(sheet.pdf())
    return {"svg": svg_path, "pdf": pdf_path, "scale": f"{scale[0]}:{scale[1]}", "part": an.parts[o.Name],
            "dimensions": [_fmt(bb.XLength), _fmt(bb.YLength), _fmt(bb.ZLength)], "callouts": callouts,
            "views": {n: {"visible": len(v[0]), "hidden": len(v[1])} for n, v in views.items()}, "registry": an.reg}
