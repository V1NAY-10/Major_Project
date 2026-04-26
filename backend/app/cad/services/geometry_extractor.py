"""
Geometry Extractor — Extracts raw geometric data from a TopoDS_Shape using pythonOCC.

Responsibilities:
  - Topology counts (solids, faces, edges)
  - Bounding box dimensions
  - Volume computation
  - Per-face surface classification with full spatial data (center, axis, height, bbox)
"""

from typing import Any


# ── Lazy OCP import helper ───────────────────────────────────────────────────

def _ocp():
    from OCP.TopoDS import TopoDS_Shape, TopoDS
    from OCP.TopAbs import (
        TopAbs_SOLID,
        TopAbs_FACE,
        TopAbs_EDGE,
        TopAbs_REVERSED,
    )
    from OCP.TopExp import TopExp_Explorer
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.BRep import BRep_Tool
    from OCP.GeomAdaptor import GeomAdaptor_Surface
    from OCP.GeomAbs import (
        GeomAbs_Plane,
        GeomAbs_Cylinder,
        GeomAbs_Cone,
        GeomAbs_Sphere,
        GeomAbs_Torus,
        GeomAbs_BezierSurface,
        GeomAbs_BSplineSurface,
    )

    return {
        "TopoDS": TopoDS,
        "TopAbs_SOLID": TopAbs_SOLID,
        "TopAbs_FACE": TopAbs_FACE,
        "TopAbs_EDGE": TopAbs_EDGE,
        "TopAbs_REVERSED": TopAbs_REVERSED,
        "TopExp_Explorer": TopExp_Explorer,
        "Bnd_Box": Bnd_Box,
        "BRepBndLib": BRepBndLib,
        "BRepGProp": BRepGProp,
        "GProp_GProps": GProp_GProps,
        "BRep_Tool": BRep_Tool,
        "GeomAdaptor_Surface": GeomAdaptor_Surface,
        "GeomAbs_Plane": GeomAbs_Plane,
        "GeomAbs_Cylinder": GeomAbs_Cylinder,
        "GeomAbs_Cone": GeomAbs_Cone,
        "GeomAbs_Sphere": GeomAbs_Sphere,
        "GeomAbs_Torus": GeomAbs_Torus,
        "GeomAbs_BezierSurface": GeomAbs_BezierSurface,
        "GeomAbs_BSplineSurface": GeomAbs_BSplineSurface,
    }


def _surface_type_map(ocp):
    return {
        ocp["GeomAbs_Plane"]: "plane",
        ocp["GeomAbs_Cylinder"]: "cylinder",
        ocp["GeomAbs_Cone"]: "cone",
        ocp["GeomAbs_Sphere"]: "sphere",
        ocp["GeomAbs_Torus"]: "torus",
        ocp["GeomAbs_BezierSurface"]: "bezier",
        ocp["GeomAbs_BSplineSurface"]: "bspline",
    }


def count_topology(shape) -> dict[str, int]:
    ocp = _ocp()
    counts: dict[str, int] = {"solids": 0, "faces": 0, "edges": 0}

    for kind, key in [
        (ocp["TopAbs_SOLID"], "solids"),
        (ocp["TopAbs_FACE"], "faces"),
        (ocp["TopAbs_EDGE"], "edges"),
    ]:
        explorer = ocp["TopExp_Explorer"](shape, kind)
        while explorer.More():
            counts[key] += 1
            explorer.Next()

    return counts


def bounding_box(shape) -> dict[str, Any]:
    ocp = _ocp()
    bbox = ocp["Bnd_Box"]()
    ocp["BRepBndLib"].Add_s(shape, bbox)

    xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()

    return {
        "xmin": round(xmin, 4),
        "ymin": round(ymin, 4),
        "zmin": round(zmin, 4),
        "xmax": round(xmax, 4),
        "ymax": round(ymax, 4),
        "zmax": round(zmax, 4),
        "length": round(xmax - xmin, 4),
        "width": round(ymax - ymin, 4),
        "height": round(zmax - zmin, 4),
    }


def compute_volume(shape) -> float:
    ocp = _ocp()
    props = ocp["GProp_GProps"]()
    ocp["BRepGProp"].VolumeProperties_s(shape, props)
    return round(props.Mass(), 6)


def classify_faces(shape) -> list[dict[str, Any]]:
    ocp = _ocp()
    stmap = _surface_type_map(ocp)
    faces_data: list[dict[str, Any]] = []
    face_idx = 0

    explorer = ocp["TopExp_Explorer"](shape, ocp["TopAbs_FACE"])
    while explorer.More():
        face = ocp["TopoDS"].Face_s(explorer.Current())
        
        # Face Bounding Box
        bbox = ocp["Bnd_Box"]()
        ocp["BRepBndLib"].Add_s(face, bbox)
        xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()
        
        f_bbox = {
            "xmin": round(xmin, 4), "ymin": round(ymin, 4), "zmin": round(zmin, 4),
            "xmax": round(xmax, 4), "ymax": round(ymax, 4), "zmax": round(zmax, 4)
        }
        
        surface = ocp["BRep_Tool"].Surface_s(face)
        adaptor = ocp["GeomAdaptor_Surface"](surface)
        stype = adaptor.GetType()

        entry: dict[str, Any] = {
            "face_id": face_idx,
            "type": stmap.get(stype, "other"),
            "bbox": f_bbox
        }

        if stype == ocp["GeomAbs_Cylinder"]:
            cyl = adaptor.Cylinder()
            entry["radius"] = round(cyl.Radius(), 6)
            entry["orientation"] = "internal" if face.Orientation() == ocp["TopAbs_REVERSED"] else "external"
            
            loc = cyl.Location()
            entry["center"] = [round(loc.X(), 4), round(loc.Y(), 4), round(loc.Z(), 4)]
            
            d = cyl.Axis().Direction()
            dx, dy, dz = d.X(), d.Y(), d.Z()
            entry["axis"] = [round(dx, 4), round(dy, 4), round(dz, 4)]
            
            # Compute height based on bbox projection along axis
            corners = [
                (xmin, ymin, zmin), (xmax, ymin, zmin),
                (xmin, ymax, zmin), (xmax, ymax, zmin),
                (xmin, ymin, zmax), (xmax, ymin, zmax),
                (xmin, ymax, zmax), (xmax, ymax, zmax),
            ]
            projections = [
                (cx - loc.X()) * dx + (cy - loc.Y()) * dy + (cz - loc.Z()) * dz
                for cx, cy, cz in corners
            ]
            entry["height"] = round(max(projections) - min(projections), 4)

        elif stype == ocp["GeomAbs_Cone"]:
            cone = adaptor.Cone()
            entry["semi_angle_deg"] = round(cone.SemiAngle() * 180 / 3.141592653589793, 4)
            entry["ref_radius"] = round(cone.RefRadius(), 6)
            
            loc = cone.Location()
            entry["center"] = [round(loc.X(), 4), round(loc.Y(), 4), round(loc.Z(), 4)]
            
            d = cone.Axis().Direction()
            entry["axis"] = [round(d.X(), 4), round(d.Y(), 4), round(d.Z(), 4)]

        elif stype == ocp["GeomAbs_Sphere"]:
            sph = adaptor.Sphere()
            entry["radius"] = round(sph.Radius(), 6)
            
            loc = sph.Location()
            entry["center"] = [round(loc.X(), 4), round(loc.Y(), 4), round(loc.Z(), 4)]

        elif stype == ocp["GeomAbs_Torus"]:
            tor = adaptor.Torus()
            entry["major_radius"] = round(tor.MajorRadius(), 6)
            entry["minor_radius"] = round(tor.MinorRadius(), 6)
            
            loc = tor.Location()
            entry["center"] = [round(loc.X(), 4), round(loc.Y(), 4), round(loc.Z(), 4)]
            
            d = tor.Axis().Direction()
            entry["axis"] = [round(d.X(), 4), round(d.Y(), 4), round(d.Z(), 4)]

        faces_data.append(entry)
        face_idx += 1
        explorer.Next()

    return faces_data


def extract_geometry(shape) -> dict[str, Any]:
    return {
        "topology": count_topology(shape),
        "bounding_box": bounding_box(shape),
        "volume": compute_volume(shape),
        "faces": classify_faces(shape),
    }
