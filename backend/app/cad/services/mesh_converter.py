import os
import tempfile

def convert_step_to_stl(file_content: bytes) -> bytes:
    """
    Converts raw STEP file bytes to STL bytes using pythonOCC.
    """
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.StlAPI import StlAPI_Writer
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".step")
    tmp_stl_fd, tmp_stl_path = tempfile.mkstemp(suffix=".stl")

    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(file_content)

        reader = STEPControl_Reader()
        status = reader.ReadFile(tmp_path)
        if status != IFSelect_RetDone:
            raise ValueError("Failed to read STEP file for mesh conversion")
            
        reader.TransferRoots()
        shape = reader.OneShape()
        if shape.IsNull():
            raise ValueError("Parsed shape is null")

        # Mesh the shape
        BRepMesh_IncrementalMesh(shape, 0.1) # 0.1 linear deflection

        # Write STL
        writer = StlAPI_Writer()
        writer.Write(shape, tmp_stl_path)

        with os.fdopen(tmp_stl_fd, "rb") as f:
            stl_bytes = f.read()

        return stl_bytes

    finally:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        try:
            os.remove(tmp_stl_path)
        except Exception:
            pass
