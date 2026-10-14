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


def chamfer_block():
    """60 (X) × 40 (Y) × 30 (Z) block whose top-back edge is cut by a 45° chamfer 10 × 10:
    the chamfer face runs from (y=30, z=30) to (y=40, z=20). Included angles chamfer/top = chamfer/back = 135°."""
    pts = [V(0, 0, 0), V(0, 40, 0), V(0, 40, 20), V(0, 30, 30), V(0, 0, 30), V(0, 0, 0)]
    return Part.Face(Part.makePolygon(pts)).extrude(V(60, 0, 0))


def coaxial_block():
    """40 × 40 × 20 block with two pairs of blind holes drilled from the top (z=20) and the bottom (z=0):
      Ø6 × 6 deep at (20, 20) from both sides — exactly coaxial;
      Ø8 × 5 deep at (10, 30) from the top and at (10.2, 30) from the bottom — axes 0.2 apart."""
    s = Part.makeBox(40, 40, 20)
    s = s.cut(Part.makeCylinder(3, 6, V(20, 20, 14), Z)).cut(Part.makeCylinder(3, 6, V(20, 20, 0), Z))
    s = s.cut(Part.makeCylinder(4, 5, V(10, 30, 15), Z)).cut(Part.makeCylinder(4, 5, V(10.2, 30, 0), Z))
    return s.removeSplitter()


def _obround(cx, cy, width, length, z0, h, along="X"):
    """Obround prism (slot cutter): centre (cx, cy), from z0 up h."""
    straight = length - width
    if along == "X":
        a, b = V(cx - straight / 2, cy, z0), V(cx + straight / 2, cy, z0)
        box = Part.makeBox(straight, width, h, V(cx - straight / 2, cy - width / 2, z0))
    else:
        a, b = V(cx, cy - straight / 2, z0), V(cx, cy + straight / 2, z0)
        box = Part.makeBox(width, straight, h, V(cx - width / 2, cy - straight / 2, z0))
    return box.fuse(Part.makeCylinder(width / 2, h, a, Z)).fuse(Part.makeCylinder(width / 2, h, b, Z)).removeSplitter()


def slot_plate():
    """100 × 60 × 10 plate with three slots, all opening on the top face (z = 10):
      obround through slot 8 wide × 30 long along X, centre (30, 30);
      rectangular blind slot 6 wide × 24 long × 4 deep along X, centre (70, 45);
      obround blind slot 10 wide × 20 long × 5 deep along Y, centre (80, 15)."""
    s = Part.makeBox(100, 60, 10)
    s = s.cut(_obround(30, 30, 8, 30, -1, 12, "X"))
    s = s.cut(Part.makeBox(24, 6, 5, V(58, 42, 6)))
    s = s.cut(_obround(80, 15, 10, 20, 5, 6, "Y"))
    return s.removeSplitter()


def pocket_block():
    """80 × 60 × 20 block with two pockets opening on the top face (z = 20):
      40 × 30 rounded-rectangle pocket, corner R5, 8 deep, centre (40, 30), with a Ø5 through hole in its floor at (40, 30);
      12 × 12 square pocket with sharp corners, 5 deep, centre (10, 10)."""
    s = Part.makeBox(80, 60, 20)
    pocket = Part.makeBox(40, 30, 9, V(20, 15, 12))
    edges = [e for e in pocket.Edges if len(e.Vertexes) == 2
             and abs(e.Vertexes[0].X - e.Vertexes[1].X) < 1e-9 and abs(e.Vertexes[0].Y - e.Vertexes[1].Y) < 1e-9]
    s = s.cut(pocket.makeFillet(5, edges))
    s = _drill(s, 40, 30, 5, 12, through_len=12)
    s = s.cut(Part.makeBox(12, 12, 6, V(4, 4, 15)))
    return s.removeSplitter()


def _edges_of_box(shape, test):
    return [e for e in shape.Edges if len(e.Vertexes) == 2 and test(e.Vertexes[0].Point, e.Vertexes[1].Point)]


def chamfer_box(all_top=True):
    """60 × 40 × 20 box with a 2 mm 45° chamfer on its 4 top edges (all_top) or only on the top-front edge."""
    s = Part.makeBox(60, 40, 20)
    if all_top:
        edges = _edges_of_box(s, lambda a, b: abs(a.z - 20) < 1e-9 and abs(b.z - 20) < 1e-9)
    else:
        edges = _edges_of_box(s, lambda a, b: abs(a.z - 20) < 1e-9 and abs(b.z - 20) < 1e-9 and abs(a.y) < 1e-9 and abs(b.y) < 1e-9)
    return s.makeChamfer(2, edges)


def fillet_chain_box():
    """60 × 40 × 20 box with every edge filleted R3: 12 cylindrical fillets + 8 spherical corners, one chain."""
    s = Part.makeBox(60, 40, 20)
    return s.makeFillet(3, s.Edges)


def boss_rim_plate():
    """60 × 40 × 10 plate with a Ø20 × 10 boss at (30, 20) whose top rim is filleted R2 (a toroidal fillet)."""
    s = Part.makeBox(60, 40, 10).fuse(Part.makeCylinder(10, 10, V(30, 20, 10), Z)).removeSplitter()
    rim = [e for e in s.Edges if isinstance(e.Curve, Part.Circle) and abs(e.Curve.Center.z - 20) < 1e-9]
    return s.makeFillet(2, rim)


def thin_wall_plate():
    """60 × 40 × 10 plate with a Ø6 through hole at (5, 20): the wall between the hole and the left face is 2 mm."""
    return _drill(Part.makeBox(60, 40, 10), 5, 20, 6, 10, through_len=10).removeSplitter()


def write_threaded_fcstd(path):
    """PartDesign Body: 40 × 30 × 10 box with a threaded (cosmetic) M6x1.0 hole feature, 8 deep, at (10, 15) and
    (30, 15) from the top. FreeCAD cuts the Ø5 tap drill and records the thread as properties."""
    doc = App.newDocument("ThreadFixture")
    body = doc.addObject("PartDesign::Body", "Body")
    box = body.newObject("PartDesign::AdditiveBox", "Box")
    box.Length, box.Width, box.Height = 40, 30, 10
    doc.recompute()
    sk = body.newObject("Sketcher::SketchObject", "HoleSketch")
    sk.Placement = App.Placement(V(0, 0, 10), App.Rotation())
    sk.addGeometry(Part.Circle(V(10, 15, 0), Z, 2.5), False)
    sk.addGeometry(Part.Circle(V(30, 15, 0), Z, 2.5), False)
    doc.recompute()
    h = body.newObject("PartDesign::Hole", "Hole")
    h.Profile = sk
    h.Threaded = True
    h.ThreadType = "ISOMetricProfile"
    h.ThreadSize = "M6x1.0"
    h.DepthType = "Dimension"
    h.Depth = 8
    doc.recompute()
    doc.saveAs(path)
    App.closeDocument(doc.Name)
    return path


def dfm_bad_block():
    """80 × 50 × 20 block with deliberate manufacturing problems (all from the top, z = 20):
      Ø1.5 × 20 through hole at (45, 10)            — depth 13.3 × diameter
      sharp-cornered pocket 20 × 15 × 6 at (20, 25)  — internal corners can't be milled
      Ø7.77 through hole at (65, 35)                 — not a standard drill
      Ø6 through hole at (3.5, 8)                    — 0.5 mm wall to the left face
      rectangular blind slot 3 × 20 × 12 at (50, 40) — 4:1 deep and narrow, sharp corners"""
    s = Part.makeBox(80, 50, 20)
    s = _drill(s, 45, 10, 1.5, 20, through_len=20)
    s = s.cut(Part.makeBox(20, 15, 7, V(10, 17.5, 14)))
    s = _drill(s, 65, 35, 7.77, 20, through_len=20)
    s = _drill(s, 3.5, 8, 6, 20, through_len=20)
    s = s.cut(Part.makeBox(20, 3, 13, V(40, 38.5, 8)))
    return s.removeSplitter()


def overhang_part():
    """A T: 20 × 20 × 30 column on the build plate with a 60 × 20 × 5 shelf on top (x −10…50): the shelf's underside
    overhangs 20 mm on each side."""
    col = Part.makeBox(20, 20, 30, V(10, 10, 0))
    shelf = Part.makeBox(60, 20, 5, V(-10, 10, 30))
    return col.fuse(shelf).removeSplitter()


def sheet_bracket():
    """2 mm sheet bent 90° (inner radius 2) into an L, 30 wide (Y): horizontal flange x 4…40, vertical flange z 4…40.
    Holes Ø5 in the horizontal flange: at x = 8 (1.5 mm from the bend), x = 22 (fine), x = 37 (0.5 mm from the edge)."""
    t, R, W = 2.0, 2.0, 30.0
    flat = Part.makeBox(36, W, t, V(4, 0, 0))
    up = Part.makeBox(t, W, 36, V(0, 0, 4))
    ring = Part.makeCylinder(R + t, W, V(4, 0, 4), V(0, 1, 0)).cut(Part.makeCylinder(R, W, V(4, 0, 4), V(0, 1, 0)))
    bend = ring.common(Part.makeBox(4, W, 4, V(0, 0, 0)))
    s = flat.fuse(up).fuse(bend).removeSplitter()
    for x in (8, 22, 37):
        s = _drill(s, x, 15, 5, t, through_len=t)
    return s.removeSplitter()


def write_bom_assembly_step(path):
    """A small table: 1 top 200 × 100 × 10, 4 identical legs 10 × 10 × 80, 2 identical stretchers 180 × 10 × 10."""
    import Import  # type: ignore
    doc = App.newDocument("BomAsm")
    asm = doc.addObject("App::Part", "Table")

    def add(name, label, shape, at):
        part = doc.addObject("App::Part", name + "Part")
        asm.addObject(part)
        o = doc.addObject("Part::Feature", name)
        o.Shape = shape
        o.Label = label
        part.addObject(o)
        part.Placement = App.Placement(at, App.Rotation())

    add("Top", "TOP", Part.makeBox(200, 100, 10), V(0, 0, 80))
    for k, (x, y) in enumerate(((0, 0), (190, 0), (0, 90), (190, 90))):
        add(f"Leg{k}", f"LEG-{k + 1}", Part.makeBox(10, 10, 80), V(x, y, 0))
    for k, y in enumerate((0, 90)):
        add(f"Rail{k}", f"RAIL-{k + 1}", Part.makeBox(180, 10, 10), V(10, y, 30))
    doc.recompute()
    Import.export([asm], path)
    App.closeDocument(doc.Name)
    return path


def write_clash_fcstd(path):
    """Three 20 mm cubes: A at x 0…20, B at x 10…30 (overlaps A by 10 × 20 × 20 = 4000 mm³), C at x 32…52 (2 mm from B)."""
    doc = App.newDocument("Clash")
    for name, x in (("A", 0), ("B", 10), ("C", 32)):
        o = doc.addObject("Part::Feature", name)
        o.Shape = Part.makeBox(20, 20, 20, V(x, 0, 0))
    doc.recompute()
    doc.saveAs(path)
    App.closeDocument(doc.Name)
    return path


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
