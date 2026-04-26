"""
Component Mapper — Converts patterns into semantic components with roles
and inter-component relationships.

Responsibilities:
  - Assign each pattern a generic role (primary_surface / support /
    void / connector) using geometry heuristics.
  - Infer relationships between components (supports / contains / connects).
  - Emit the final compact, LLM-ready structure.

Works for ANY CAD file — zero object-specific naming logic.
"""

from __future__ import annotations

from typing import Any


# ── Role heuristics ──────────────────────────────────────────────────────────
#
# These rules are intentionally geometry-only.  No part names, no material
# assumptions.  The ordering matters: later rules can override earlier ones.

def _assign_role(pattern: dict[str, Any], all_patterns: list[dict[str, Any]]) -> str:
    """
    Derive a generic role label for *pattern* given all detected patterns.

    Role ladder
    -----------
    primary_surface : the single largest planar or body feature.
    support         : repeated vertical (Z-axis) cylindrical/block patterns.
    void            : internal cylindrical features (holes).
    connector       : small geometry linking other features (small cylinders,
                      small bodies, fillets / tori).
    """
    ftype = pattern.get("type", "")
    count = pattern.get("count", 1)

    # ── void: internal cylinders regardless of count ─────────────────────────
    if ftype in ("hole", "hole_pattern") or (
        "cylinder" in ftype and pattern.get("orientation") == "internal"
    ):
        return "void"

    # ── primary_surface: the largest body or the largest planar pattern ───────
    if "body" in ftype or "plane" in ftype:
        # Find max area/volume among all bodies and planes
        bodies_planes = [
            p for p in all_patterns
            if "body" in p.get("type", "") or "plane" in p.get("type", "")
        ]
        # Use length*width*height as proxy for body; area for plane
        def _size(p: dict[str, Any]) -> float:
            if "body" in p.get("type", ""):
                return (p.get("length", 0) * p.get("width", 0) * p.get("height", 0))
            return p.get("area", 0.0)

        if bodies_planes:
            largest = max(bodies_planes, key=_size)
            if largest["id"] == pattern["id"]:
                return "primary_surface"
        return "primary_surface"   # only one → still primary

    # ── support: repeated cylindrical / external features along vertical axis ─
    if count >= 2 and "cylinder" in ftype:
        axis = pattern.get("axis")
        # Consider Z-dominant axes as vertical
        if axis is not None:
            z_frac = abs(axis[2]) / (sum(c ** 2 for c in axis) ** 0.5 + 1e-9)
            if z_frac > 0.7:
                return "support"
        # Even without explicit axis, repeated external cylinders → support
        if pattern.get("orientation", "") != "internal":
            return "support"

    # ── connector: small single geometry (torus/fillet, small cylinder/cone) ──
    if count == 1 and ftype in ("torus", "cone", "torus_pattern", "cone_pattern"):
        return "connector"

    # External single cylinder: likely a boss/pin → connector if small
    if "cylinder" in ftype and count == 1:
        # No reliable size reference without comparing to body — default connector
        return "connector"

    # ── fallback ──────────────────────────────────────────────────────────────
    return "support"


# ── Relationship inference ────────────────────────────────────────────────────

def _infer_relationships(
    components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Produce a list of directed relationships between component IDs.

    Heuristics
    ----------
    primary ← support  :  support components physically sit on primary.
    primary → void     :  primary contains voids (holes through main body).
    support → connector:  connectors link support to primary.
    """
    relationships: list[dict[str, Any]] = []

    role_map: dict[str, list[str]] = {}
    for comp in components:
        role = comp.get("role", "")
        role_map.setdefault(role, []).append(comp["id"])

    primaries  = role_map.get("primary_surface", [])
    supports   = role_map.get("support", [])
    voids      = role_map.get("void", [])
    connectors = role_map.get("connector", [])

    # support → primary  (supports the primary surface)
    for p_id in primaries:
        for s_id in supports:
            relationships.append({
                "source": s_id,
                "relation": "supports",
                "target": p_id,
            })

    # primary → void  (primary contains each void)
    for p_id in primaries:
        for v_id in voids:
            relationships.append({
                "source": p_id,
                "relation": "contains",
                "target": v_id,
            })

    # connector → support  (connector links support elements)
    for c_id in connectors:
        for s_id in supports:
            relationships.append({
                "source": c_id,
                "relation": "connects",
                "target": s_id,
            })

    return relationships


# ── Public API ────────────────────────────────────────────────────────────────

def map_components(
    patterns: list[dict[str, Any]],
    geometry_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Convert pattern entries into a compact, LLM-friendly component structure.

    Parameters
    ----------
    patterns : list[dict]
        Output of ``pattern_detector.detect_patterns()``.
    geometry_summary : dict, optional
        Top-level geometry info (bounding box, volume, topology counts) to
        include in the output ``summary`` block.

    Returns
    -------
    dict with keys:
        ``summary``        — geometry metadata (topology, bbox, volume).
        ``components``     — list of component dicts.
        ``relationships``  — list of relationship dicts.
    """
    components: list[dict[str, Any]] = []

    for idx, pattern in enumerate(patterns, start=1):
        role = _assign_role(pattern, patterns)

        ftype = pattern.get("type", "other")
        count = pattern.get("count", 1)

        comp: dict[str, Any] = {
            "id": f"comp_{idx}",
            "type": ftype,
            "role": role,
            "count": count,
            "editable": pattern.get("editable", True),
        }

        # Attach key geometric fields for LLM context (skip bookkeeping keys)
        _skip = {"id", "type", "count", "editable", "source_ids",
                  "face_id", "pattern_group", "pattern_count"}
        for k, v in pattern.items():
            if k not in _skip:
                comp[k] = v

        # Keep a back-reference to the pattern so callers can trace
        comp["pattern_id"] = pattern.get("id")

        components.append(comp)

    relationships = _infer_relationships(components)

    # ── Build summary ─────────────────────────────────────────────────────────
    summary: dict[str, Any] = {}
    if geometry_summary:
        summary.update(geometry_summary)

    # High-level component statistics
    total_features = sum(c.get("count", 1) for c in components)
    summary["component_count"] = len(components)
    summary["total_feature_instances"] = total_features
    summary["compression_ratio"] = (
        round(total_features / len(components), 2) if components else 1.0
    )

    return {
        "summary": summary,
        "components": components,
        "relationships": relationships,
    }
