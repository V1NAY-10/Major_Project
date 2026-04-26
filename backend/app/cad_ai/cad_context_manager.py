"""
CAD Context Manager — Orchestrates CAD parsing, storage, and retrieval.

Acts as the high-level interface consumed by routes and the /generate pipeline.
Delegates storage to cad_memory_store and prompt construction to prompt_builder.

STEP files are parsed EXCLUSIVELY via pythonOCC (app.cad.services.step_parser).
There is NO text-based fallback — if OCC is not installed, the server will
fail at startup with a clear ImportError.
"""

import uuid
import json
from typing import Optional, Dict, Any, Tuple

from .cad_memory_store import cad_store
from .prompt_builder import build_explain_prompt, build_cad_prompt

# ── Direct import — no fallback ─────────────────────────────────────────────
from app.cad.services.step_parser import parse_step, STEPParseError


# ── CAD File Parsing ────────────────────────────────────────────────────────

def _parse_step_file(file_content: bytes, filename: str) -> dict:
    """
    Parse a STEP file using the pythonOCC-based pipeline.

    Returns a structured dict with summary, parameters, physical_properties,
    and a semantic feature list (holes, cylinders, body, …).

    Raises STEPParseError if the file is invalid or empty.
    """
    return parse_step(file_content, filename)


def _parse_fcstd_metadata(file_content: bytes, filename: str) -> dict:
    """
    Parse an FCStd file's metadata.
    FCStd is a zip archive — we read Document.xml for shape info.
    """
    import zipfile, io, xml.etree.ElementTree as ET

    features = []
    parameters = {}

    try:
        with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
            if "Document.xml" in zf.namelist():
                with zf.open("Document.xml") as f:
                    tree = ET.parse(f)
                    root = tree.getroot()
                    for obj in root.iter("Object"):
                        name = obj.attrib.get("name", "unknown")
                        obj_type = obj.attrib.get("type", "unknown")
                        features.append({"name": name, "type": obj_type})

            # Try GuiDocument.xml for view properties
            if "GuiDocument.xml" in zf.namelist():
                parameters["has_gui_data"] = True

    except Exception as e:
        parameters["parse_error"] = str(e)

    return {
        "filename": filename,
        "format": "FCStd",
        "features": features[:50],
        "parameters": parameters,
        "dimensions": {},
    }


def _parse_generic(file_content: bytes, filename: str) -> dict:
    """Fallback parser for non-STEP/non-FCStd formats."""
    text = file_content.decode("utf-8", errors="ignore")[:5000]
    return {
        "filename": filename,
        "format": filename.rsplit(".", 1)[-1].upper() if "." in filename else "UNKNOWN",
        "features": [],
        "parameters": {"raw_preview": text},
        "dimensions": {},
    }


def parse_cad_file(file_content: bytes, filename: str) -> dict:
    """
    Dispatch to the right parser based on extension.
    Returns a structured dict suitable for LLM context injection.

    STEP/STP files → pythonOCC parser (no fallback)
    FCStd files    → zip/xml metadata parser
    Other formats  → generic preview parser
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in ("step", "stp"):
        return _parse_step_file(file_content, filename)
    elif ext in ("fcstd",):
        return _parse_fcstd_metadata(file_content, filename)
    elif ext in ("iges", "igs", "brep", "stl", "obj"):
        return _parse_generic(file_content, filename)
    else:
        return _parse_generic(file_content, filename)


# ── Storage Helpers ─────────────────────────────────────────────────────────

def store_cad_context(session_id: str, file_id: str, parsed_data: dict, stl_data: bytes = None, raw_cad_data: bytes = None) -> None:
    """Persist parsed CAD data in memory for the given session."""
    cad_store.store(session_id, file_id, parsed_data, stl_data, raw_cad_data)


def get_cad_context(session_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored CAD context for a session (or None)."""
    return cad_store.get(session_id)


def get_cad_context_by_file(file_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored CAD context by file_id."""
    return cad_store.get_by_file_id(file_id)


def session_has_cad(session_id: str) -> bool:
    """Check whether a session has active CAD context."""
    return cad_store.has_context(session_id)


# ── Prompt Helpers ──────────────────────────────────────────────────────────

def get_explain_messages(parsed_data: dict) -> list[dict]:
    """Build LLM messages for the explain endpoint."""
    return build_explain_prompt(parsed_data)


def get_modify_messages(user_input: str, cad_data: dict) -> list[dict]:
    """Build LLM messages for CAD-aware code generation."""
    return build_cad_prompt(user_input, cad_data)

