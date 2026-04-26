"""
Size Ranking Engine — Ranks parsed CAD patterns by size.

Extracts the primary dimension (diameter or radius) from each pattern
and sorts them. Supports answering queries like "smallest", "largest",
and ordinal sizing.
"""

from typing import Any, Dict, List


def _get_size(pattern: Dict[str, Any]) -> float:
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


def rank_patterns(patterns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Groups patterns by type (e.g., hole, cylinder) and ranks them by size.
    
    Returns a dictionary mapping base_type to ranking info:
    {
      "hole": {
          "smallest": pattern_id,
          "largest": pattern_id,
          "ordered": [pattern_id1, pattern_id2, ...],
          "sizes": {pattern_id: size, ...}
      },
      ...
    }
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
        # Filter out patterns with size 0 if they shouldn't be ranked, 
        # but here we rank everything.
        sized_pats = [(p["id"], _get_size(p)) for p in pats]
        # Sort ascending by size
        sized_pats.sort(key=lambda x: x[1])
        
        ordered_ids = [pid for pid, _ in sized_pats]
        sizes = {pid: size for pid, size in sized_pats}

        smallest = ordered_ids[0] if ordered_ids else None
        largest = ordered_ids[-1] if ordered_ids else None
        
        # Determine "second_largest" safely, ensuring it never equals "largest"
        if len(ordered_ids) > 1:
            second_largest = ordered_ids[-2]
        else:
            second_largest = None # Cannot be same as largest

        rankings[t] = {
            "smallest": smallest,
            "second_largest": second_largest,
            "largest": largest,
            "ordered": ordered_ids,
            "sizes": sizes
        }

    return rankings
