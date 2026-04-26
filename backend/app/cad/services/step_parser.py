"""
STEP Parser — Loads a STEP file via pythonOCC, extracts geometry,
maps features, and returns a structured LLM-ready dict.

This is the ONLY parser for STEP files. No legacy/text fallback exists.

All OCP imports are LAZY — done inside functions at parse time, not at
module load time. This lets the FastAPI server start even when OCP is
only installed in a venv that uvicorn's reloader subprocess can't see.
When a STEP file is actually uploaded, OCP loads on demand.
"""

import os
import tempfile
from typing import Any

from .geometry_extractor import extract_geometry
from .feature_mapper import map_features


class STEPParseError(Exception):
    """Raised when a STEP file cannot be loaded or contains no geometry."""
    pass


def _load_step_shape(file_content: bytes):
    """
    Load STEP bytes via STEPControl_Reader and return the resulting shape.

    pythonOCC requires a filesystem path, so we write to a temp file,
    read it, and clean up.
    """
    # Lazy OCP imports — will raise ImportError with a clear message if missing
    try:
        from OCP.STEPControl import STEPControl_Reader
        from OCP.IFSelect import IFSelect_RetDone
    except ImportError as e:
        raise STEPParseError(
            f"pythonOCC (OCP) is not installed or not on sys.path. "
            f"Install cadquery: pip install cadquery. Original error: {e}"
        ) from e

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".step")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(file_content)

        reader = STEPControl_Reader()
        status = reader.ReadFile(tmp_path)

        if status != IFSelect_RetDone:
            raise STEPParseError(
                f"STEPControl_Reader failed with status {status}. "
                "The file may be corrupted or not a valid STEP/STP."
            )

        reader.TransferRoots()
        shape = reader.OneShape()

        if shape.IsNull():
            raise STEPParseError("STEP file was read but produced an empty (null) shape.")

        return shape

    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def parse_step(file_content: bytes, filename: str) -> dict[str, Any]:
    """
    Full STEP → structured-output pipeline.

    1. Load STEP via OCC kernel
    2. Extract raw geometry (topology, bbox, volume, face classification)
    3. Map geometry to semantic features (holes, cylinders, body, …)
    4. Assemble final dict matching the required output schema

    Parameters
    ----------
    file_content : bytes
        Raw bytes of the uploaded .step/.stp file.
    filename : str
        Original filename (for metadata).

    Returns
    -------
    dict
        LLM-ready structured CAD data.

    Raises
    ------
    STEPParseError
        If the file is invalid, the shape is empty, or OCP is not installed.
    """
    print("🚀 USING pythonOCC PARSER")

    # ── Step 1: Load ────────────────────────────────────────────────
    shape = _load_step_shape(file_content)

    # ── Step 2: Geometry extraction ─────────────────────────────────
    geometry = extract_geometry(shape)

    # ── Step 3 & 4: Feature mapping and assembly ─────────────────────
    from .feature_mapper import map_features_compact
    
    # map_features_compact handles the full pipeline (features -> patterns -> components)
    # and returns a dict with 'summary', 'components', and 'relationships'.
    # We pass the raw geometry directly to it.
    # To preserve some topology details, we inject it back into geometry if needed
    # but map_features_compact already pulls topology, bounding_box, volume.
    
    compact_data = map_features_compact(geometry)
    
    topo = geometry["topology"]
    print(
        f"✅ pythonOCC parse complete: "
        f"solids={topo['solids']}, faces={topo['faces']}, "
        f"edges={topo['edges']}, volume={geometry['volume']}"
    )

    # Merge metadata with the compact pipeline output
    return {
        "filename": filename,
        "format": "STEP",
        "parser": "pythonOCC",
        **compact_data
    }
