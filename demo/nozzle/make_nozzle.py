"""Launch-vehicle conical (15°) convergent-divergent rocket nozzle with an integral bolted flange (one part)
and the engine thrust-mount ring it bolts to (second part).

Axis = Z (flow direction +Z), the flange / mount interface plane at z = 0. Units mm.
Inner contour: chamber Ø100 → blend R20 → 30° converging cone → throat arcs R30 / R10, throat Ø40 →
15° diverging cone to the exit Ø120 (area ratio 9). Wall 4 mm, offset of the inner contour.
"""
import math
import os
import sys

import FreeCAD as App
import Part
import MeshPart

V = App.Vector
OUT = r"D:\Soham_Jadhav\Major_Project\demo\nozzle"
os.makedirs(OUT, exist_ok=True)

# ── parameters ──────────────────────────────────────────────────────────────
RC = 50.0        # chamber radius (Ø100)
RT = 20.0        # throat radius (Ø40)
RE = 60.0        # exit radius (Ø120): area ratio (60/20)^2 = 9
TC = math.radians(30)  # converging half-angle
TD = math.radians(15)  # diverging half-angle
RB = 20.0        # chamber → cone blend radius
RU = 30.0        # throat upstream radius (1.5 Rt)
RD = 10.0        # throat downstream radius (0.5 Rt)
T = 4.0          # wall thickness
Z1 = 25.0        # end of the cylindrical chamber section
FL_R, FL_T = 80.0, 12.0          # flange Ø160 × 12
PCD, NB, BOLT = 140.0, 8, 9.0    # 8 × Ø9 bolt holes on PCD 140 (M8 clearance)
TAP, TAP_Z = 3.0, 18.5           # 2 × Ø3 radial pressure taps in the chamber wall
RING_OD, RING_ID, RING_T = 200.0, 108.0, 10.0  # thrust-mount ring below the flange
MOUNT_PCD, MOUNT_N, MOUNT_D = 180.0, 6, 11.0   # 6 × Ø11 vehicle attachment holes

# ── inner contour (r, z) ────────────────────────────────────────────────────
pb = (RC - RB + RB * math.cos(TC), Z1 + RB * math.sin(TC))              # blend arc → cone
Lc = (RC - RB + RB * math.cos(TC) - RT - RU + RU * math.cos(TC)) / math.sin(TC)
ZT = Z1 + RB * math.sin(TC) + Lc * math.cos(TC) + RU * math.sin(TC)    # throat plane
pu = (RT + RU - RU * math.cos(TC), ZT - RU * math.sin(TC))              # cone → upstream arc
pd = (RT + RD - RD * math.cos(TD), ZT + RD * math.sin(TD))              # downstream arc → diverging cone
ZE = pd[1] + (RE - pd[0]) / math.tan(TD)                               # exit plane


def P(r, z):
    return V(r, 0, z)


def arc(c, rad, a0, a1):
    """Arc in the (r, z) half-plane around c, from angle a0 to a1 (angles of the radius vector from +r)."""
    am = (a0 + a1) / 2
    pt = lambda a: P(c[0] + rad * math.cos(a), c[1] + rad * math.sin(a))
    return Part.Arc(pt(a0), pt(am), pt(a1)).toShape()


def contour(off):
    """The flow contour offset outward by `off` (0 = inner wall, T = outer wall): list of edges, inlet → exit."""
    cb, cu, cd = (RC - RB, Z1), (RT + RU, ZT), (RT + RD, ZT)
    n_c, n_d = (math.cos(TC), math.sin(TC)), (math.cos(TD), -math.sin(TD))  # outward normals of the cones
    pb_ = (pb[0] + off * n_c[0], pb[1] + off * n_c[1])
    pu_ = (pu[0] + off * n_c[0], pu[1] + off * n_c[1])
    pd_ = (pd[0] + off * n_d[0], pd[1] + off * n_d[1])
    z_exit_r = pd_[0] + (ZE - pd_[1]) * math.tan(TD)
    z0 = FL_T if off else 0.0
    e = [Part.LineSegment(P(RC + off, z0), P(RC + off, Z1)).toShape(),
         arc(cb, RB + off, 0, TC),
         Part.LineSegment(P(*pb_), P(*pu_)).toShape(),
         arc(cu, RU - off, math.pi + TC, math.pi),
         arc(cd, RD - off, math.pi, math.pi - TD),
         Part.LineSegment(P(*pd_), P(z_exit_r, ZE)).toShape()]
    return e, z_exit_r


inner, _ = contour(0.0)
outer, r_exit_out = contour(T)
edges = inner + outer + [
    Part.LineSegment(P(RE, ZE), P(r_exit_out, ZE)).toShape(),          # exit lip
    Part.LineSegment(P(RC, 0), P(FL_R, 0)).toShape(),                  # flange bottom (interface)
    Part.LineSegment(P(FL_R, 0), P(FL_R, FL_T)).toShape(),             # flange rim
    Part.LineSegment(P(FL_R, FL_T), P(RC + T, FL_T)).toShape(),        # flange top
]
profile = Part.Face(Part.Wire(Part.__sortEdges__(edges)))
nozzle = profile.revolve(V(0, 0, 0), V(0, 0, 1), 360)
nozzle = Part.Solid(Part.Shell(nozzle.Faces)) if nozzle.ShapeType != "Solid" else nozzle
if nozzle.Volume < 0:
    nozzle.reverse()
for k in range(NB):  # axial bolt holes through the flange
    a = 2 * math.pi * k / NB
    nozzle = nozzle.cut(Part.makeCylinder(BOLT / 2, FL_T + 2, V(PCD / 2 * math.cos(a), PCD / 2 * math.sin(a), -1)))
for k in range(2):   # radial pressure taps through the chamber wall
    d = V(math.cos(math.pi * k), math.sin(math.pi * k), 0)
    nozzle = nozzle.cut(Part.makeCylinder(TAP / 2, T + 4, V(0, 0, TAP_Z) + d * (RC - 2), d))
nozzle = nozzle.removeSplitter()

ring = Part.makeCylinder(RING_OD / 2, RING_T, V(0, 0, -RING_T)).cut(
    Part.makeCylinder(RING_ID / 2, RING_T + 2, V(0, 0, -RING_T - 1)))
for k in range(NB):  # matching bolt holes
    a = 2 * math.pi * k / NB
    ring = ring.cut(Part.makeCylinder(BOLT / 2, RING_T + 2, V(PCD / 2 * math.cos(a), PCD / 2 * math.sin(a), -RING_T - 1)))
for k in range(MOUNT_N):  # vehicle attachment holes, offset half a pitch
    a = 2 * math.pi * (k + 0.5) / MOUNT_N
    ring = ring.cut(Part.makeCylinder(MOUNT_D / 2, RING_T + 2,
                                      V(MOUNT_PCD / 2 * math.cos(a), MOUNT_PCD / 2 * math.sin(a), -RING_T - 1)))
ring = ring.removeSplitter()

for name, s in (("Nozzle", nozzle), ("MountRing", ring)):
    assert s.isValid() and len(s.Solids) == 1, name

doc = App.newDocument("Nozzle")
objs = []
for name, label, s in (("Nozzle", "NOZZLE", nozzle), ("MountRing", "THRUST-MOUNT-RING", ring)):
    o = doc.addObject("Part::Feature", name)
    o.Label = label
    o.Shape = s
    objs.append(o)
doc.recompute()
doc.saveAs(os.path.join(OUT, "nozzle.FCStd"))
Part.export(objs, os.path.join(OUT, "nozzle.step"))


def stl(shape, path):
    m = MeshPart.meshFromShape(Shape=shape, LinearDeflection=0.05, AngularDeflection=math.radians(5), Relative=False)
    m.write(path)
    return m.CountFacets


facets = {
    "nozzle_assembly.stl": stl(Part.makeCompound([nozzle, ring]), os.path.join(OUT, "nozzle_assembly.stl")),
    "nozzle.stl": stl(nozzle, os.path.join(OUT, "nozzle.stl")),
    "mount_ring.stl": stl(ring, os.path.join(OUT, "mount_ring.stl")),
}

res = {
    "throat_z": round(ZT, 3), "exit_z": round(ZE, 3), "pb": [round(x, 3) for x in pb], "pu": [round(x, 3) for x in pu],
    "pd": [round(x, 3) for x in pd], "exit_outer_r": round(r_exit_out, 3), "area_ratio": round((RE / RT) ** 2, 3),
    "nozzle_volume_mm3": round(nozzle.Volume, 2), "ring_volume_mm3": round(ring.Volume, 2),
    "nozzle_bbox": [round(x, 3) for x in (nozzle.BoundBox.XLength, nozzle.BoundBox.YLength, nozzle.BoundBox.ZLength)],
    "nozzle_faces": len(nozzle.Faces), "interference_mm3": round(nozzle.common(ring).Volume, 4), "facets": facets}
open(os.path.join(OUT, "make_nozzle.out"), "w").write(repr(res))
print("RESULT", res)
sys.stdout.flush()
os._exit(0)
