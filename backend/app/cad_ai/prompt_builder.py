"""
Prompt Builder — Constructs LLM prompts for CAD-aware generation.

Two main use-cases:
  1. Explain parsed CAD data to the user (no code output).
  2. Modify an existing CAD design based on user instruction + CAD context.
"""

import json
from typing import Dict, Any

# ── Token-budget guardrails ─────────────────────────────────────────────────
MAX_CONTEXT_CHARS = 14_000  # ~3 500 tokens


def _truncate_context(data: dict) -> str:
    """Serialize CAD data to JSON string, truncating if it exceeds budget."""
    raw = json.dumps(data, indent=2, default=str)
    if len(raw) > MAX_CONTEXT_CHARS:
        raw = raw[:MAX_CONTEXT_CHARS] + "\n... [truncated — context too large]"
    return raw


# ── 1. Explain CAD ──────────────────────────────────────────────────────────

EXPLAIN_SYSTEM_PROMPT = (
    "You are a CAD analysis assistant. Given structured CAD data, explain clearly:\n"
    "- What shapes exist and their types (cylinder, plane, cone, sphere, torus, hole)\n"
    "- Exact XYZ coordinates and which plane each feature lives on (XY, XZ, or YZ)\n"
    "- Dimensions (radius, diameter, height, length, width)\n"
    "- Features (holes, cylinders, fillets, chamfers, pockets, pads, etc.)\n"
    "- Spatial position: where features are in 3D space relative to origin (0,0,0)\n"
    "- Physical properties (volume, surface area, bounding box)\n"
    "- For FCStd files: list every object by name and its editable properties\n"
    "Do NOT generate code. Provide a concise, human-readable engineering summary."
)


def build_explain_prompt(parsed_data: dict) -> list[dict]:
    """Return the messages list for the CAD-explain LLM call."""
    context_str = _truncate_context(parsed_data)
    return [
        {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Analyze and explain the following CAD structure. For every feature, "
                "mention its XYZ position, alignment plane, and key dimensions:\n\n"
                f"{context_str}"
            ),
        },
    ]


# ── 2. Modify CAD ──────────────────────────────────────────────────────────

MODIFY_SYSTEM_PROMPT = (
    "You are an expert FreeCAD Python developer and CAD engineer.\n\n"
    "You will be given:\n"
    "1. Structured CAD data — every feature has:\n"
    "   - 'spatial_context.position_absolute': {x, y, z} in mm from origin\n"
    "   - 'spatial_context.alignment_plane': 'XY', 'XZ', 'YZ', or 'custom'\n"
    "   - 'spatial_context.bounding_box_xyz': per-axis min/max\n"
    "   - 'spatial_context.position_relative': semantic label e.g. 'top-left'\n"
    "   - For FCStd: 'features' list with object 'name' and 'properties' dict\n"
    "2. A user modification request\n\n"
    "COORDINATE RULES:\n"
    "- X-axis runs LEFT ↔ RIGHT. Y-axis runs FRONT ↔ BACK. Z-axis runs BOTTOM ↔ TOP.\n"
    "- The XY plane (Z=const) is the horizontal base plane.\n"
    "- The XZ plane (Y=const) is the front/back vertical plane.\n"
    "- The YZ plane (X=const) is the left/right vertical plane.\n"
    "- Always identify which plane a feature sits on before modifying it.\n\n"
    "FREECAD SCRIPT RULES:\n"
    "- Use 'import FreeCAD as App', 'import Part', 'import PartDesign', 'import Mesh'.\n"
    "- Use 'doc = App.ActiveDocument'. NEVER use 'App.newDocument()'.\n"
    "- For FCStd files: access objects by exact 'name' from the features list.\n"
    "  e.g. obj = doc.getObject('Pad001')\n"
    "- To set a length: obj.Length = <float_value>\n"
    "- To set a radius: obj.Radius = <float_value>\n"
    "- Always call doc.recompute() at the end.\n"
    "- Always call doc.save() after recompute() for FCStd files.\n"
    "- NEVER use App.Matrix4d() — use App.Matrix() instead.\n"
    "- Wrap ALL Python code in ```python ... ``` blocks.\n"
    "- After code, briefly explain which object you modified and at what XYZ location.\n"
)


def build_cad_modify_prompt(user_input: str, cad_data: dict) -> list[dict]:
    """
    Return the messages list for the CAD-aware code generation LLM call.

    Combines the user's natural-language modification request with
    the parsed CAD context so the LLM can produce targeted edits.
    """
    context_str = _truncate_context(cad_data)
    fmt = cad_data.get("format", "STEP")

    file_hint = ""
    if fmt == "FCStd":
        features = cad_data.get("features", [])
        names = [f.get("name", "?") for f in features[:20]]
        file_hint = (
            f"\n\nFILE TYPE: Native FreeCAD (.FCStd)\n"
            f"Available object names: {', '.join(names)}\n"
            "Modify objects directly by name using doc.getObject().\n"
        )

    user_message = (
        f"User Request:\n\"{user_input}\"\n"
        f"{file_hint}\n"
        f"Full CAD Context (with XYZ coordinates and plane info):\n{context_str}"
    )

    return [
        {"role": "system", "content": MODIFY_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]


def build_cad_prompt(user_input: str, cad_data: dict) -> list[dict]:
    """
    Public convenience alias used by /generate integration.
    Delegates to build_cad_modify_prompt.
    """
    return build_cad_modify_prompt(user_input, cad_data)
