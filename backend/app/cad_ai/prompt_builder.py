"""
Prompt Builder — Constructs LLM prompts for CAD-aware generation.

Two main use-cases:
  1. Explain parsed CAD data to the user (no code output).
  2. Modify an existing CAD design based on user instruction + CAD context.
"""

import json
from typing import Dict, Any

# ── Token-budget guardrails ─────────────────────────────────────────────────
MAX_CONTEXT_CHARS = 12_000  # ~3 000 tokens — keeps prompt within budget


def _truncate_context(data: dict) -> str:
    """Serialize CAD data to JSON string, truncating if it exceeds budget."""
    raw = json.dumps(data, indent=2, default=str)
    if len(raw) > MAX_CONTEXT_CHARS:
        raw = raw[:MAX_CONTEXT_CHARS] + "\n... [truncated — context too large]"
    return raw


# ── 1. Explain CAD ──────────────────────────────────────────────────────────

EXPLAIN_SYSTEM_PROMPT = (
    "You are a CAD analysis assistant. Given structured CAD data, explain clearly:\n"
    "- What shapes exist\n"
    "- Dimensions\n"
    "- Features (holes, cylinders, fillets, chamfers, pockets, pads, etc.)\n"
    "- Physical properties (volume, surface area, bounding box)\n"
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
                "Analyze and explain the following CAD structure:\n\n"
                f"{context_str}"
            ),
        },
    ]


# ── 2. Modify CAD ──────────────────────────────────────────────────────────

MODIFY_SYSTEM_PROMPT = (
    "You are an expert FreeCAD Python developer.\n\n"
    "You are given:\n"
    "1. Existing CAD structure (features, dimensions, parameters)\n"
    "2. A user modification request\n\n"
    "Your job:\n"
    "- Modify the existing design logically\n"
    "- Use Part and PartDesign modules\n"
    "- DO NOT recreate from scratch unless necessary\n"
    "- Preserve structure where possible\n"
    "- A document is pre-initialized. Use: 'doc = App.ActiveDocument'. NEVER use 'App.newDocument()'\n"
    "- Output ONLY valid FreeCAD Python code\n"
    "- No explanations, no markdown, no comments outside code"
)


def build_cad_modify_prompt(user_input: str, cad_data: dict) -> list[dict]:
    """
    Return the messages list for the CAD-aware code generation LLM call.

    Combines the user's natural-language modification request with
    the parsed CAD context so the LLM can produce targeted edits.
    """
    context_str = _truncate_context(cad_data)

    user_message = (
        f"User Request:\n\"{user_input}\"\n\n"
        f"CAD Context:\n{context_str}"
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
