"""
Deterministic commands that skip the LLM.

Only exact, unambiguous forms are accepted; anything else goes to the LLM:
  P5-P6 = 120            set a dimension (optional "mm", optional "anchor symmetric|end|start")
  H3-P5 = 12 mm symmetric
  H2 Ø8   /   H2 diameter 8   /   H2 dia = 8 mm
  PAT1 pcd 80   /   PAT1 spacing 50   /   PAT1 count 8

With one component selected in the viewer, the ID can be left out: "Ø8" with H2 selected, "= 120" or
"120" with P5-P6 selected, "pcd 80" with PAT1 selected.
"""

import re

_NUM = r"(\d+(?:\.\d+)?|\.\d+)"
_MM = r"(?:\s*mm)?"

_DIM = re.compile(
    rf"^\s*(?:set\s+)?([A-Z]+\d+)\s*-\s*([A-Z]+\d+)\s*(?:=|to|:)\s*{_NUM}{_MM}"
    r"(?:\s*(?:,|\()?\s*(?:anchor\s*[=:]?\s*)?(start|end|symmetric)\)?)?\s*$", re.I)
_HOLE = re.compile(
    rf"^\s*(?:set\s+)?(H\d+)\s*(?:Ø|⌀|diameter|dia\.?)\s*(?:=|to|:)?\s*Ø?\s*{_NUM}{_MM}\s*$", re.I)
_PAT = re.compile(
    rf"^\s*(?:set\s+)?(PAT\d+)\s+(pcd|spacing|spacing_u|spacing_v|count|start_angle)\s*(?:=|to|:)?\s*{_NUM}"
    r"(?:\s*(?:mm|deg|°))?\s*$", re.I)


_ANY_ID = re.compile(r"\b(?:PAT|[A-Z])\d+\b", re.I)


def parse(text, selection=None):
    """Return (tool_name, args) for an exact command, or None."""
    if not text or len(text) > 80:
        return None
    found = _parse(text)
    if found is None and selection and len(selection) == 1 and not _ANY_ID.search(text):
        sel = selection[0]
        found = _parse(f"{sel} {text}")
        if found is None and re.match(rf"^\s*{_NUM}", text):
            found = _parse(f"{sel} = {text}")
    return found


def _parse(text):
    m = _PAT.match(text)
    if m:
        key = m.group(2).lower()
        val = float(m.group(3))
        return "edit_hole_pattern", {"pattern": m.group(1).upper(), key: int(val) if key == "count" else val}
    m = _HOLE.match(text)
    if m:
        return "resize_hole", {"hole": m.group(1).upper(), "diameter": float(m.group(2))}
    m = _DIM.match(text)
    if m:
        args = {"dimension": f"{m.group(1).upper()}-{m.group(2).upper()}", "value": float(m.group(3))}
        if m.group(4):
            args["anchor"] = m.group(4).lower()
        return "set_dimension", args
    return None


def reply(result):
    """Short reply for a fast-path edit, quoting verified values."""
    if not result.get("ok"):
        return f"Could not apply that: {result.get('error')}"
    lines = [result["message"].split("\n")[0]]
    for v in result.get("verified") or []:
        if v.get("status") == "ok":
            val = v.get("value")
            if isinstance(val, float):
                val = f"{val:.3f}"
            elif isinstance(val, dict):
                val = ", ".join(f"{k} {x}" for k, x in val.items())
            lines.append(f"✓ verified {v['id']} {v['field']} = {val}")
        else:
            lines.append(f"⚠ {v['id']}: {v.get('note', 'could not verify')}")
    return "\n".join(f"- {line}" for line in lines)
