"""
Preview Generator — Simulates before vs after changes for CAD patterns.

Does not modify actual CAD geometry. Only provides a UI-ready preview
of what would happen based on intended actions.
"""

from typing import Any, Dict, List


def generate_preview(intent: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate a before/after preview for a specific intent.
    
    Returns:
    {
        "pattern": "Human readable pattern label",
        "before": float/str,
        "after": float/str,
        "count": int
    }
    """
    target_pattern = intent.get("pattern_data")
    if not target_pattern:
        raise ValueError(f"Pattern data missing for intent on {intent.get('target_pattern')}")

    action = intent.get("action", "")
    after = intent.get("value")
    if after is None:
        raise ValueError(f"Target 'after' value is missing for {intent.get('target_pattern')}")

    # Format label
    ftype = target_pattern.get("type", "").replace("_pattern", "")
    label = ftype.capitalize() + ("s" if target_pattern.get("count", 1) > 1 else "")

    from .size_ranker import _get_size
    before = _get_size(target_pattern)

    # _get_size returns 0.0 if it cannot find anything, which is invalid for our preview
    if before == 0.0:
        raise ValueError(f"Cannot determine 'before' value for pattern {intent.get('target_pattern')}")

    return {
        "pattern": label,
        "before": before,
        "after": after,
        "count": target_pattern.get("count", 1)
    }
