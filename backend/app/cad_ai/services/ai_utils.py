"""
AI Utilities — Sizing and preview helpers for CAD-aware conversational editing.
Combined from preview_generator.py and size_ranker.py.
"""

from __future__ import annotations
from typing import Any, Dict, List


# ── Sizing Logic ─────────────────────────────────────────────────────────────

def get_pattern_size(pattern: Dict[str, Any]) -> float:
    """Extract the primary size metric (diameter preferred over radius)."""
    if "diameter" in pattern:
        return float(pattern["diameter"])
    if "radius" in pattern:
        return float(pattern["radius"]) * 2.0
    if "major_radius" in pattern:
        return float(pattern["major_radius"]) * 2.0
    if "ref_radius" in pattern:
        return float(pattern["ref_radius"]) * 2.0
    
    # Fallback size for bodies/planes
    length = float(pattern.get("length", 0))
    width = float(pattern.get("width", 0))
    height = float(pattern.get("height", 0))
    if length or width or height:
        return max(length, width, height)
    return 0.0


def rank_patterns_by_size(patterns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Groups patterns by type (e.g., hole, cylinder) and ranks them by size.
    """
    by_type: Dict[str, List[Dict[str, Any]]] = {}

    for p in patterns:
        t = p.get("type", "").replace("_pattern", "")
        if not t:
            continue
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(p)

    rankings: Dict[str, Any] = {}

    for t, pats in by_type.items():
        sized_pats = [(p["id"], get_pattern_size(p)) for p in pats]
        # Sort ascending by size
        sized_pats.sort(key=lambda x: x[1])
        
        ordered_ids = [pid for pid, _ in sized_pats]
        sizes = {pid: size for pid, size in sized_pats}

        smallest = ordered_ids[0] if ordered_ids else None
        largest = ordered_ids[-1] if ordered_ids else None
        
        if len(ordered_ids) > 1:
            second_largest = ordered_ids[-2]
        else:
            second_largest = None

        rankings[t] = {
            "smallest": smallest,
            "second_largest": second_largest,
            "largest": largest,
            "ordered": ordered_ids,
            "sizes": sizes
        }

    return rankings


# ── Preview Logic ───────────────────────────────────────────────────────────

def generate_intent_preview(intent: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate a before/after preview for a specific intent.
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

    before = get_pattern_size(target_pattern)
    if before == 0.0:
        raise ValueError(f"Cannot determine 'before' value for pattern {intent.get('target_pattern')}")

    return {
        "pattern": label,
        "before": before,
        "after": after,
        "count": target_pattern.get("count", 1)
    }
