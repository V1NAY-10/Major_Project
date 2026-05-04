"""
geometry_modifier.py — Solid-level geometry modification for pythonOCC (OCP wrapper).

Pipeline:
  1. Load STEP bytes → OCC shape
  2. Iterate every SOLID in the shape
  3. For each solid: detect if it is primarily cylindrical (has a cylindrical face)
     and extract its cylinder axis + height via bounding box
  4. If the solid matches a modification intent, rebuild it with
     BRepPrimAPI_MakeCylinder(axis, new_radius, height)
  5. Compose all (modified + untouched) solids into a TopoDS_Compound
  6. BRepMesh → StlAPI_Writer → return STL bytes

KEY RULE: We operate on SOLIDS, never individual faces.
          Face-level replacement produces open shells that cannot be meshed.
"""

from typing import List, Dict, Any
import math


# ─────────────────────────────────────────────────────────────────────────────
# Debug hard-test entry (set FORCE_CYLINDER_TEST=True to verify the pipeline)
# ─────────────────────────────────────────────────────────────────────────────
FORCE_CYLINDER_TEST = False   # ← flip to True for PART 2 debug


def _make_test_cylinder():
    """Part 2 debug: return a big cylinder as the sole shape."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    print("[CAD Modifier] *** FORCE TEST: exporting a 20-radius / 200-height cylinder ***")
    return BRepPrimAPI_MakeCylinder(20, 200).Shape()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _solid_cylinder_info(solid):
    """
    Examine the faces of a solid.  If the solid contains at least one
    GeomAbs_Cylinder face, return (axis_gp_Ax2, height, radius) from the
    *largest* such face (by radius).  Otherwise return None.

    Height is derived from the bounding box projected along the cylinder axis.
    """
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    best = None   # (radius, ax2)

    from OCP.TopoDS import TopoDS
    exp = TopExp_Explorer(solid, TopAbs_FACE)
    while exp.More():
        shape = exp.Current()
        face = TopoDS.Face_s(shape)
        adaptor = BRepAdaptor_Surface(face, True)
        if adaptor.GetType() == GeomAbs_Cylinder:
            cyl = adaptor.Cylinder()
            r = cyl.Radius()
            if best is None or r > best[0]:
                best = (r, cyl.Position())   # gp_Ax3
        exp.Next()

    if best is None:
        return None

    radius, ax3 = best

    # Height = bounding box extent along the cylinder axis direction
    bbox = Bnd_Box()
    BRepBndLib.Add_s(solid, bbox)
    xmin, ymin, zmin, xmax, ymax, zmax = bbox.Get()

    # Project bounding box diagonal onto axis direction
    from OCP.gp import gp_Dir, gp_Pnt, gp_Vec
    d = ax3.Direction()          # gp_Dir — axis direction (already unit)
    dx = d.X(); dy = d.Y(); dz = d.Z()

    # Project all 8 corners onto axis, take span
    corners = [
        (xmin, ymin, zmin), (xmax, ymin, zmin),
        (xmin, ymax, zmin), (xmax, ymax, zmin),
        (xmin, ymin, zmax), (xmax, ymin, zmax),
        (xmin, ymax, zmax), (xmax, ymax, zmax),
    ]
    ox = ax3.Location().X()
    oy = ax3.Location().Y()
    oz = ax3.Location().Z()

    projections = [
        (cx - ox) * dx + (cy - oy) * dy + (cz - oz) * dz
        for cx, cy, cz in corners
    ]
    height = max(projections) - min(projections)

    if height < 1e-6:
        height = max(xmax - xmin, ymax - ymin, zmax - zmin)

    # Convert gp_Ax3 → gp_Ax2 for MakeCylinder
    from OCP.gp import gp_Ax2
    ax2 = gp_Ax2(ax3.Location(), ax3.Direction(), ax3.XDirection())

    return radius, ax2, height


def _rebuild_cylinder(ax2, new_radius: float, height: float):
    """Return a new solid using BRepPrimAPI_MakeCylinder."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    maker = BRepPrimAPI_MakeCylinder(ax2, new_radius, height)
    maker.Build()
    if not maker.IsDone():
        raise RuntimeError("BRepPrimAPI_MakeCylinder failed to build")
    return maker.Shape()


# ─────────────────────────────────────────────────────────────────────────────
# Main public function
# ─────────────────────────────────────────────────────────────────────────────

def modify_geometry_and_export(
    file_content: bytes,
    intents: List[Dict[str, Any]],
    fallback_stl: bytes = None,
) -> tuple:
    """
    Loads STEP bytes, modifies cylinder solids, and exports STL.

    Returns (stl_bytes: bytes, warnings: list[str]).
    """
    import os
    import tempfile

    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS_Compound
    from OCP.BRep import BRep_Builder
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.StlAPI import StlAPI_Writer

    # ── temp files ────────────────────────────────────────────────────────────
    tmp_fd,     tmp_path     = tempfile.mkstemp(suffix=".step")
    tmp_stl_fd, tmp_stl_path = tempfile.mkstemp(suffix=".stl")

    warnings: list = []

    try:
        # ── PART 2: hard test override ────────────────────────────────────────
        if FORCE_CYLINDER_TEST:
            shape = _make_test_cylinder()
        else:
            # ── PART 1 / PART 4: load STEP ───────────────────────────────────
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(file_content)
            tmp_fd = None  # ownership transferred

            reader = STEPControl_Reader()
            status = reader.ReadFile(tmp_path)
            if status != IFSelect_RetDone:
                raise ValueError("Failed to read STEP file for modification")

            reader.TransferRoots()
            shape = reader.OneShape()
            if shape.IsNull():
                raise ValueError("Parsed shape is null")

        print("[CAD Modifier] STEP loaded successfully")

        # ── PART 3: build modification map ───────────────────────────────────
        # modification_map: list of { orig_rad, new_rad }
        modification_map: list = []

        for intent in intents:
            action     = intent.get("action", "")
            target_val = intent.get("value")
            p_data     = intent.get("pattern_data") or {}

            if target_val is None:
                continue
            try:
                new_val = float(target_val)
            except (TypeError, ValueError):
                continue

            # Resolve original radius
            orig_rad = p_data.get("radius") or 0.0
            if not orig_rad and "diameter" in p_data:
                try:
                    orig_rad = float(p_data["diameter"]) / 2.0
                except (TypeError, ValueError):
                    pass
            if not orig_rad:
                continue

            # New radius from value
            new_rad = new_val / 2.0 if "diameter" in action.lower() else new_val

            # Clamp to [0.2×, 3.0×] of original
            clamped = max(orig_rad * 0.2, min(orig_rad * 3.0, new_rad))
            if abs(clamped - new_rad) > 1e-6:
                msg = f"Radius {new_rad:.4f} clamped to {clamped:.4f} (was {new_val} from action '{action}')"
                print(f"[CAD Modifier] WARNING: {msg}")
                warnings.append(msg)
                new_rad = clamped

            modification_map.append({"orig_rad": orig_rad, "new_rad": new_rad})
            print(f"[CAD Modifier] Intent queued: orig_r={orig_rad:.4f} → new_r={new_rad:.4f}")

        # ── PART 3 continued: iterate SOLIDS and rebuild ──────────────────────
        builder = BRep_Builder()
        compound = TopoDS_Compound()
        builder.MakeCompound(compound)

        modified_count = 0
        solid_count    = 0

        exp = TopExp_Explorer(shape, TopAbs_SOLID)
        while exp.More():
            solid = exp.Current()
            solid_count += 1

            # Detect cylinder and get geometry
            cyl_info = _solid_cylinder_info(solid)

            replaced = False
            if cyl_info and modification_map:
                s_radius, ax2, height = cyl_info

                for mod in modification_map:
                    if abs(s_radius - mod["orig_rad"]) < 1.0:   # 1 mm tolerance
                        print(
                            f"[CAD Modifier] Rebuilding cylinder "
                            f"r={s_radius:.4f} h={height:.4f} → r={mod['new_rad']:.4f}"
                        )
                        try:
                            new_solid = _rebuild_cylinder(ax2, mod["new_rad"], height)
                            builder.Add(compound, new_solid)
                            modified_count += 1
                            replaced = True
                        except Exception as rebuild_err:
                            print(f"[CAD Modifier] Rebuild failed for solid: {rebuild_err}")
                        break

            if not replaced:
                builder.Add(compound, solid)

            exp.Next()

        print(f"[CAD Modifier] Solids processed: {solid_count}, modified: {modified_count}")

        # If no solids were found (shape is a shell / compound of faces),
        # fall back to exporting the raw shape as-is so the viewer at least
        # shows something.
        if solid_count == 0:
            warnings.append("No solid bodies found; exporting raw shape.")
            final_shape = shape
        else:
            final_shape = compound

        # ── PART 4 + PART 5: mesh and export modified shape ───────────────────
        print("[CAD Modifier] Meshing modified shape...")
        BRepMesh_IncrementalMesh(final_shape, 0.1, False, 0.5)

        print("[CAD Modifier] Exporting MODIFIED shape to STL and STEP...")
        writer = StlAPI_Writer()
        writer.Write(final_shape, tmp_stl_path)
        print(f"[CAD Modifier] STL written → {tmp_stl_path}")

        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCP.Interface import Interface_Static
        
        step_writer = STEPControl_Writer()
        Interface_Static.SetCVal_s("write.step.schema", "AP214")
        step_writer.Transfer(final_shape, STEPControl_AsIs)
        step_writer.Write(tmp_path)

        with open(tmp_stl_path, "rb") as f:
            stl_bytes = f.read()
            
        with open(tmp_path, "rb") as f:
            step_bytes = f.read()

        if len(stl_bytes) < 84:          # 84 bytes is the minimum valid binary STL header
            raise ValueError(
                f"STL output is too small ({len(stl_bytes)} bytes) — "
                "mesh step may have produced no triangles."
            )

        print(f"[CAD Modifier] STL size: {len(stl_bytes)} bytes, STEP size: {len(step_bytes)} bytes — done.")
        return stl_bytes, step_bytes, warnings

    except Exception as exc:
        print(f"[CAD Modifier] ERROR: {exc}")
        if fallback_stl:
            print("[CAD Modifier] Returning fallback STL (original shape).")
            return fallback_stl, file_content, [f"Modification failed: {exc}. Showing original."]
        raise

    finally:
        # Clean up temp files
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except Exception:
                pass
        for path in (tmp_path, tmp_stl_path):
            try:
                os.remove(path)
            except Exception:
                pass
