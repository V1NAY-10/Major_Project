import os
import tempfile

def convert_step_to_stl(file_content: bytes) -> bytes:
    """
    Converts raw STEP file bytes to STL bytes using gmsh.
    """
    import gmsh

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".step")
    tmp_stl_fd, tmp_stl_path = tempfile.mkstemp(suffix=".stl")
    os.close(tmp_stl_fd) # Close so gmsh can write to it
    
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(file_content)

        if not gmsh.isInitialized():
            gmsh.initialize()
            
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.clear()
        
        # Load the STEP file
        gmsh.merge(tmp_path)
        
        # Generate surface mesh
        gmsh.model.mesh.generate(2)
        
        # Write to STL
        gmsh.write(tmp_stl_path)
        
        with open(tmp_stl_path, "rb") as f:
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
