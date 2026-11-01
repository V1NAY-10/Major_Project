"""Simple launch-vehicle nose cone: tangent ogive shell + shoulder (one part) and a recovery bulkhead (second part).

Axis = Z, the shoulder/base plane at z = 0, tip at z = +L. Units mm.
"""
import math
import os
import sys

import FreeCAD as App
import Mesh
import Part
import MeshPart

V = App.Vector
OUT = r"D:\Soham_Jadhav\Major_Project\demo\nosecone"
os.makedirs(OUT, exist_ok=True)

# ── parameters ──────────────────────────────────────────────────────────────
D = 100.0          # body diameter (matches the body tube OD)
L = 300.0          # ogive length (fineness ratio 3)
T = 2.0            # wall thickness
SH_LEN = 80.0      # shoulder length (inside the body tube)
SH_OD = 96.0       # shoulder OD = body tube ID (100 OD, 2 mm wall)
SH_ID = 92.0
BH_T = 6.0         # bulkhead thickness
EYEBOLT = 8.4      # M8 eyebolt clearance hole
SCREW = 4.0        # 4 × radial screws holding the bulkhead
R = D / 2
rho = (R * R + L * L) / (2 * R)  # tangent-ogive radius


def arc_r_at(arc_r, z):
    """Radius of the ogive arc (centre r = R - rho, z = 0; radius arc_r) at height z."""
    return (R - rho) + math.sqrt(arc_r ** 2 - z ** 2)


def arc_z_at(arc_r, r):
    return math.sqrt(arc_r ** 2 - (r - (R - rho)) ** 2)


# One closed cross-section (in the XZ half-plane), revolved once: shoulder + ogive wall with constant thickness T.
# The inner surface is the exact inward offset of the outer arc (same centre, radius rho - T).
MEPLAT_R = 1.5  # small flat tip (meplat), Ø3: keeps the ogive surface off the axis (clean STEP export)
z_tip = arc_z_at(rho, MEPLAT_R)
TIP_R = 3.0  # the hollow stops where it is 3 mm in radius: a small solid tip (no edge on the axis)
z_tip_in = arc_z_at(rho - T, TIP_R)
z_in46 = arc_z_at(rho - T, SH_ID / 2)  # where the inner ogive surface meets the shoulder bore
outer_arc = Part.Arc(V(R, 0, 0), V(arc_r_at(rho, z_tip / 2), 0, z_tip / 2), V(MEPLAT_R, 0, z_tip)).toShape()
inner_arc = Part.Arc(V(TIP_R, 0, z_tip_in), V(arc_r_at(rho - T, (z_tip_in + z_in46) / 2), 0, (z_tip_in + z_in46) / 2),
                     V(SH_ID / 2, 0, z_in46)).toShape()
pts = [V(SH_ID / 2, 0, z_in46), V(SH_ID / 2, 0, -SH_LEN), V(SH_OD / 2, 0, -SH_LEN), V(SH_OD / 2, 0, 0), V(R, 0, 0)]
edges = [Part.LineSegment(pts[i], pts[i + 1]).toShape() for i in range(len(pts) - 1)]
edges += [outer_arc, Part.LineSegment(V(MEPLAT_R, 0, z_tip), V(0, 0, z_tip)).toShape(),
          Part.LineSegment(V(0, 0, z_tip), V(TIP_R, 0, z_tip_in)).toShape(), inner_arc]
profile = Part.Face(Part.Wire(Part.__sortEdges__(edges)))
nose = profile.revolve(V(0, 0, 0), V(0, 0, 1), 360)
nose = Part.Solid(Part.Shell(nose.Faces)) if nose.ShapeType != "Solid" else nose
if nose.Volume < 0:
    nose.reverse()
# 4 radial screw holes through the shoulder, 10 mm above its bottom end, for the bulkhead
for k in range(4):
    a = math.radians(45 + 90 * k)
    d = V(math.cos(a), math.sin(a), 0)
    nose = nose.cut(Part.makeCylinder(SCREW / 2, 10, V(0, 0, -SH_LEN + 10) + d * (SH_ID / 2 - 3), d))
nose = nose.removeSplitter()

# bulkhead: disc in the shoulder's lower end, eyebolt hole and 4 radial tapped-hole positions
bh = Part.makeCylinder(SH_ID / 2, BH_T, V(0, 0, -SH_LEN + 10 - BH_T / 2))
bh = bh.cut(Part.makeCylinder(EYEBOLT / 2, BH_T + 2, V(0, 0, -SH_LEN + 10 - BH_T / 2 - 1)))
for k in range(4):
    a = math.radians(45 + 90 * k)
    d = V(math.cos(a), math.sin(a), 0)
    bh = bh.cut(Part.makeCylinder(3.3 / 2, 12, V(0, 0, -SH_LEN + 10) + d * (SH_ID / 2 - 11), d))  # M4 tap drill
bh = bh.removeSplitter()

for name, s in (("NoseCone", nose), ("Bulkhead", bh)):
    assert s.isValid() and len(s.Solids) == 1, name

doc = App.newDocument("NoseCone")
objs = []
for name, label, s in (("NoseCone", "NOSE-CONE", nose), ("Bulkhead", "BULKHEAD", bh)):
    o = doc.addObject("Part::Feature", name)
    o.Label = label
    o.Shape = s
    objs.append(o)
doc.recompute()
doc.saveAs(os.path.join(OUT, "nosecone.FCStd"))
Part.export(objs, os.path.join(OUT, "nosecone.step"))


def stl(shape, path):
    m = MeshPart.meshFromShape(Shape=shape, LinearDeflection=0.05, AngularDeflection=math.radians(5), Relative=False)
    m.write(path)
    return m.CountFacets


facets = {
    "nosecone_assembly.stl": stl(Part.makeCompound([nose, bh]), os.path.join(OUT, "nosecone_assembly.stl")),
    "nosecone_shell.stl": stl(nose, os.path.join(OUT, "nosecone_shell.stl")),
    "bulkhead.stl": stl(bh, os.path.join(OUT, "bulkhead.stl")),
}

print("RESULT", {
    "ogive_radius": round(rho, 3), "tip_z": round(z_tip, 3), "inner_tip_z": round(z_tip_in, 3), "z_in46": round(z_in46, 3),
    "nose_volume_mm3": round(nose.Volume, 1), "bulkhead_volume_mm3": round(bh.Volume, 1),
    "nose_bbox": [round(x, 2) for x in (nose.BoundBox.XLength, nose.BoundBox.YLength, nose.BoundBox.ZLength)],
    "interference_mm3": round(nose.common(bh).Volume, 4), "facets": facets})
sys.stdout.flush()
os._exit(0)
