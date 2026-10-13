"""
Test parts with known ground truth, built with FreeCAD's Part API (runs inside FreeCAD).

Each builder returns a Part.Shape; `write_step` saves it so tests go through the same
STEP import path as a real upload.
"""

import math

import FreeCAD as App  # type: ignore
import Part  # type: ignore

V = App.Vector
Z = V(0, 0, 1)


def _drill(shape, x, y, d, z_top, depth=None, through_len=None):
    """Cut a hole downward (-Z) from z_top. depth=None → through."""
    length = through_len if depth is None else depth
    start = z_top + (1.0 if depth is None else 0.0)
    cyl = Part.makeCylinder(d / 2, length + (2.0 if depth is None else 0.0), V(x, y, start - length - (1.0 if depth is None else 0.0)), Z)
    return shape.cut(cyl)


def plate():
    """100 × 60 × 10 plate (X × Y × Z), origin at min corner.

    Holes (all drilled from the top, +Z):
      4 × Ø6 through at (10,10) (90,10) (10,50) (90,50)
      1 × Ø8 blind, 5 deep, flat bottom, at (50,30)
      1 × Ø5 through with 90° countersink Ø10 at (30,30)
      1 × Ø6.6 through with counterbore Ø11 × 6.5 deep at (70,30)
    """
    s = Part.makeBox(100, 60, 10)
    for x, y in ((10, 10), (90, 10), (10, 50), (90, 50)):
        s = _drill(s, x, y, 6, 10, through_len=10)
    s = _drill(s, 50, 30, 8, 10, depth=5)
    # countersink: through Ø5 + 90° cone from Ø10 at the top surface
    s = _drill(s, 30, 30, 5, 10, through_len=10)
    cone = Part.makeCone(0, 7.5, 7.5, V(30, 30, 10 - 5), Z)  # 45° semi-angle, r=5 at z=10
    s = s.cut(cone)
    # counterbore
    s = _drill(s, 70, 30, 6.6, 10, through_len=10)
    s = s.cut(Part.makeCylinder(5.5, 6.5 + 1, V(70, 30, 10 - 6.5), Z))
    return s.removeSplitter()


def flange():
    """Disk Ø100 × 12 (Z 0..12) centred at origin, centre bore Ø30, 6 × Ø8 holes on PCD 70 (first at +X)."""
    s = Part.makeCylinder(50, 12, V(0, 0, 0), Z)
    s = s.cut(Part.makeCylinder(15, 14, V(0, 0, -1), Z))
    for k in range(6):
        a = math.radians(60 * k)
        s = s.cut(Part.makeCylinder(4, 14, V(35 * math.cos(a), 35 * math.sin(a), -1), Z))
    return s.removeSplitter()


def bracket():
    """L-shaped block: base 80 × 40 × 10, upright 10 × 40 × 50 at x=0..10 (total height 60).

    Boss Ø20 × 8 on top of the base at (50, 20), vertical outer edges of the base filleted R5 at x=80.
    """
    base = Part.makeBox(80, 40, 10)
    up = Part.makeBox(10, 40, 60)
    s = base.fuse(up)
    s = s.fuse(Part.makeCylinder(10, 8, V(50, 20, 10), Z))
    s = s.removeSplitter()
    edges = [e for e in s.Edges
             if len(e.Vertexes) == 2 and abs(e.Vertexes[0].X - 80) < 1e-6 and abs(e.Vertexes[1].X - 80) < 1e-6
             and abs(e.Vertexes[0].Y - e.Vertexes[1].Y) < 1e-6]
    s = s.makeFillet(5, edges)
    return s.removeSplitter()


def linear_pattern():
    """Bar 200 × 30 × 8 with 5 × Ø6 through holes at x = 20, 60, 100, 140, 180 (y=15)."""
    s = Part.makeBox(200, 30, 8)
    for i in range(5):
        s = _drill(s, 20 + 40 * i, 15, 6, 8, through_len=8)
    return s.removeSplitter()


def nurbs_block():
    """60 × 40 × 20 block with a Ø10 through hole at (30,20), converted entirely to B-spline faces."""
    s = Part.makeBox(60, 40, 20)
    s = _drill(s, 30, 20, 10, 20, through_len=20)
    return s.removeSplitter().toNurbs()


def vase():
    """Solid of revolution about Z (height 30) whose side is a revolved B-spline: a free-form face no rule
    classifies, between a flat bottom (r 10) and a flat top (r 12)."""
    spline = Part.BSplineCurve()
    spline.interpolate([V(10, 0, 0), V(14, 0, 10), V(9, 0, 20), V(12, 0, 30)])
    wire = Part.Wire([Part.LineSegment(V(0, 0, 0), V(10, 0, 0)).toShape(), spline.toShape(),
                      Part.LineSegment(V(12, 0, 30), V(0, 0, 30)).toShape(),
                      Part.LineSegment(V(0, 0, 30), V(0, 0, 0)).toShape()])
    return Part.Face(wire).revolve(V(0, 0, 0), Z, 360)


def drill_tip_block():
    """40 × 40 × 20 block, blind Ø10 hole 12 deep (cylindrical part) with a 118° drill tip, at (20,20) from the top."""
    s = Part.makeBox(40, 40, 20)
    r, depth = 5.0, 12.0
    tip_h = r / math.tan(math.radians(59))
    tool = Part.makeCylinder(r, depth + 1, V(20, 20, 20 - depth), Z)
    tool = tool.fuse(Part.makeCone(r, 0, tip_h, V(20, 20, 20 - depth), V(0, 0, -1)))
    return s.cut(tool).removeSplitter()


def side_holes_block():
    """50 × 30 × 30 block: Ø8 through hole along X at (y=15, z=15); Ø6 blind 10 deep along Y from the front at (x=25, z=22)."""
    s = Part.makeBox(50, 30, 30)
    s = s.cut(Part.makeCylinder(4, 52, V(-1, 15, 15), V(1, 0, 0)))
    s = s.cut(Part.makeCylinder(3, 11, V(25, -1, 22), V(0, 1, 0)))
    return s.removeSplitter()


def slot_block():
    """60 × 40 × 20 block with a 10 wide, 8 deep slot through along Y at x = 25..35 (open at the top)."""
    s = Part.makeBox(60, 40, 20)
    s = s.cut(Part.makeBox(10, 42, 9, V(25, -1, 12)))
    return s.removeSplitter()


def write_pad_fcstd(path):
    """PartDesign Body: 40 × 30 rectangle sketch padded 10 mm (Pad.Length drives the Z size)."""
    import Sketcher  # type: ignore
    doc = App.newDocument("PadFixture")
    body = doc.addObject("PartDesign::Body", "Body")
    sk = body.newObject("Sketcher::SketchObject", "Sketch")
    pts = [V(0, 0, 0), V(40, 0, 0), V(40, 30, 0), V(0, 30, 0)]
    for i in range(4):
        sk.addGeometry(Part.LineSegment(pts[i], pts[(i + 1) % 4]), False)
    for i in range(4):
        sk.addConstraint(Sketcher.Constraint("Coincident", i, 2, (i + 1) % 4, 1))
    sk.addConstraint(Sketcher.Constraint("Horizontal", 0))
    sk.addConstraint(Sketcher.Constraint("Horizontal", 2))
    sk.addConstraint(Sketcher.Constraint("Vertical", 1))
    sk.addConstraint(Sketcher.Constraint("Vertical", 3))
    sk.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, 40.0))
    sk.renameConstraint(sk.ConstraintCount - 1, "Width")
    sk.addConstraint(Sketcher.Constraint("DistanceY", 1, 1, 1, 2, 30.0))
    sk.renameConstraint(sk.ConstraintCount - 1, "Depth")
    pad = body.newObject("PartDesign::Pad", "Pad")
    pad.Profile = sk
    pad.Length = 10
    doc.recompute()
    doc.saveAs(path)
    App.closeDocument(doc.Name)
    return path


def write_assembly_step(path):
    """The same two-part assembly as write_assembly_fcstd, exported as a STEP assembly."""
    import Import  # type: ignore
    fc = path[:-5] + "_src.FCStd"
    write_assembly_fcstd(fc)
    doc = App.openDocument(fc)
    Import.export([doc.getObject("Assembly")], path)
    App.closeDocument(doc.Name)
    return path


def write_big_assembly_step(path, n=30):
    """n plates (each 60 × 40 × 5 with 4 Ø5 holes) in their own App::Part, stacked along Z."""
    import Import  # type: ignore
    doc = App.newDocument("BigAsm")
    asm = doc.addObject("App::Part", "Assembly")
    for k in range(n):
        part = doc.addObject("App::Part", f"P{k}")
        asm.addObject(part)
        s = Part.makeBox(60, 40, 5)
        for x, y in ((8, 8), (52, 8), (8, 32), (52, 32)):
            s = _drill(s, x, y, 5, 5, through_len=5)
        o = doc.addObject("Part::Feature", f"Plate{k}")
        o.Shape = s.removeSplitter()
        part.addObject(o)
        part.Placement = App.Placement(V(0, 0, 10 * k), App.Rotation())
    doc.recompute()
    Import.export([asm], path)
    App.closeDocument(doc.Name)
    return path


def write_step(shape, path):
    shape.exportStep(path)
    return path


def write_assembly_fcstd(path):
    """Two plates inside App::Part containers with offsets, saved as FCStd, then re-used as STEP."""
    doc = App.newDocument("AsmFixture")
    asm = doc.addObject("App::Part", "Assembly")
    p1 = doc.addObject("App::Part", "PartA")
    p2 = doc.addObject("App::Part", "PartB")
    asm.addObject(p1)
    asm.addObject(p2)
    a = doc.addObject("Part::Feature", "TopPlate")
    a.Shape = _drill(Part.makeBox(120, 80, 6), 60, 40, 10, 6, through_len=6)
    a.Label = "TABLE-TOP"
    p1.addObject(a)
    b = doc.addObject("Part::Feature", "Leg")
    b.Shape = Part.makeBox(10, 10, 100)
    b.Label = "LEG"
    p2.addObject(b)
    p1.Placement = App.Placement(V(0, 0, 100), App.Rotation())
    p2.Placement = App.Placement(V(5, 5, 0), App.Rotation())
    doc.recompute()
    doc.saveAs(path)
    App.closeDocument(doc.Name)
    return path
