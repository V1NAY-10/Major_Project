"""
Mappers — Converts raw geometry data into semantic, LLM-friendly features and components.
Combined from feature_mapper.py and component_mapper.py.
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional


# ── Constants ───────────────────────────────────────────────────────────────

_HOLE_RADIUS_THRESHOLD = 100.0


# ── ID & Label Helpers ──────────────────────────────────────────────────────

def _make_id(prefix: str, counter: dict[str, int]) -> str:
    counter[prefix] = counter.get(prefix, 0) + 1
    return f"{prefix}_{counter[prefix]}"


def _compute_semantic_labels(feature: dict[str, Any], global_bbox: dict[str, Any]) -> list[str]:
    labels = []
    center = feature.get("center")
    if not center or not global_bbox:
        return labels

    cx, cy, cz = center
    gx_min, gx_max = global_bbox.get("xmin", 0), global_bbox.get("xmax", 0)
    gy_min, gy_max = global_bbox.get("ymin", 0), global_bbox.get("ymax", 0)
    gz_min, gz_max = global_bbox.get("zmin", 0), global_bbox.get("zmax", 0)

    dx = gx_max - gx_min
    dy = gy_max - gy_min
    dz = gz_max - gz_min

    # Semantic positional tags
    if dz > 0:
        if cz > gz_max - dz * 0.2:
            labels.append("top")
        elif cz < gz_min + dz * 0.2:
            labels.append("bottom")

    if dx > 0:
        if cx < gx_min + dx * 0.2:
            labels.append("left")
        elif cx > gx_max - dx * 0.2:
            labels.append("right")

    if dy > 0:
        if cy < gy_min + dy * 0.2:
            labels.append("front")
        elif cy > gy_max - dy * 0.2:
            labels.append("back")

    # Leg detection: vertical cylinder near bottom
    if feature.get("type") in ("cylinder", "hole"):
        axis = feature.get("axis", [0, 0, 0])
        is_vertical = abs(axis[2]) > 0.9
        is_bottom = "bottom" in labels
        if is_vertical and is_bottom and feature.get("type") == "cylinder":
            labels.append("support_leg")

    return labels


def _build_spatial_context(
    feature: dict[str, Any],
    global_bbox: dict[str, Any],
    semantic_labels: list[str],
) -> dict[str, Any]:
    """
    Build an explicit XYZ spatial context block for LLM consumption.
    Converts raw center/bbox arrays to labelled dicts and infers
    the primary reference plane from the feature's axis or alignment_plane.
    """
    center = feature.get("center")
    pos_abs = {"x": center[0], "y": center[1], "z": center[2]} if center else None

    bbox = feature.get("bbox", {})
    bbox_named = {
        "x": {"min": bbox.get("xmin"), "max": bbox.get("xmax")},
        "y": {"min": bbox.get("ymin"), "max": bbox.get("ymax")},
        "z": {"min": bbox.get("zmin"), "max": bbox.get("zmax")},
    } if bbox else None

    return {
        "position_absolute": pos_abs,
        "bounding_box_xyz": bbox_named,
        "alignment_plane": feature.get("alignment_plane", "unknown"),
        "position_relative": "-".join(semantic_labels) if semantic_labels else "center",
    }


# ── Role Heuristics ──────────────────────────────────────────────────────────

def _assign_role(pattern: dict[str, Any], all_patterns: list[dict[str, Any]]) -> str:
    """
    Derive a generic role label for *pattern* given all detected patterns.
    """
    ftype = pattern.get("type", "")
    count = pattern.get("count", 1)

    # void: internal cylinders regardless of count
    if ftype in ("hole", "hole_pattern") or (
        "cylinder" in ftype and pattern.get("orientation") == "internal"
    ):
        return "void"

    # primary_surface: the largest body or the largest planar pattern
    if "body" in ftype or "plane" in ftype:
        bodies_planes = [
            p for p in all_patterns
            if "body" in p.get("type", "") or "plane" in p.get("type", "")
        ]
        def _size(p: dict[str, Any]) -> float:
            if "body" in p.get("type", ""):
                return (p.get("length", 0) * p.get("width", 0) * p.get("height", 0))
            return p.get("area", 0.0)

        if bodies_planes:
            largest = max(bodies_planes, key=_size)
            if largest["id"] == pattern["id"]:
                return "primary_surface"
        return "primary_surface"

    # support: repeated cylindrical / external features along vertical axis
    if count >= 2 and "cylinder" in ftype:
        axis = pattern.get("axis")
        if axis is not None:
            z_frac = abs(axis[2]) / (sum(c ** 2 for c in axis) ** 0.5 + 1e-9)
            if z_frac > 0.7:
                return "support"
        if pattern.get("orientation", "") != "internal":
            return "support"

    # connector: small single geometry
    if count == 1 and ftype in ("torus", "cone", "torus_pattern", "cone_pattern"):
        return "connector"

    if "cylinder" in ftype and count == 1:
        return "connector"

    return "support"


# ── Relationship Inference ────────────────────────────────────────────────────

def _infer_relationships(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Produce a list of directed relationships between component IDs.
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

    # support → primary
    for p_id in primaries:
        for s_id in supports:
            relationships.append({"source": s_id, "relation": "supports", "target": p_id})

    # primary → void
    for p_id in primaries:
        for v_id in voids:
            relationships.append({"source": p_id, "relation": "contains", "target": v_id})

    # connector → support
    for c_id in connectors:
        for s_id in supports:
            relationships.append({"source": c_id, "relation": "connects", "target": s_id})

    return relationships


# ── Mapping Logic ────────────────────────────────────────────────────────────

def _map_single_face(face: dict[str, Any], counter: dict[str, int], global_bbox: dict[str, Any]) -> dict[str, Any] | None:
    ftype = face.get("type")
    
    if ftype == "cylinder":
        radius = face.get("radius", 0)
        orientation = face.get("orientation", "external")
        is_hole = orientation == "internal" and radius <= _HOLE_RADIUS_THRESHOLD
        feat_type = "hole" if is_hole else "cylinder"
        
        mapped = {
            "id": _make_id(feat_type, counter),
            "type": feat_type,
            "radius": radius,
            "diameter": round(radius * 2, 6),
            "height": face.get("height", 0),
            "center": face.get("center"),
            "axis": face.get("axis"),
            "alignment_plane": face.get("alignment_plane", "unknown"),
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": True,
        }
    elif ftype == "plane":
        mapped = {
            "id": _make_id("plane", counter),
            "type": "plane",
            "center": face.get("center"),
            "axis": face.get("axis"),
            "alignment_plane": face.get("alignment_plane", "unknown"),
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": False,
        }
    elif ftype == "cone":
        mapped = {
            "id": _make_id("cone", counter),
            "type": "cone",
            "semi_angle_deg": face.get("semi_angle_deg", 0),
            "ref_radius": face.get("ref_radius", 0),
            "center": face.get("center"),
            "axis": face.get("axis"),
            "alignment_plane": face.get("alignment_plane", "unknown"),
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": True,
        }
    elif ftype == "sphere":
        mapped = {
            "id": _make_id("sphere", counter),
            "type": "sphere",
            "radius": face.get("radius", 0),
            "diameter": round(face.get("radius", 0) * 2, 6),
            "center": face.get("center"),
            "alignment_plane": "N/A",
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": True,
        }
    elif ftype == "torus":
        mapped = {
            "id": _make_id("torus", counter),
            "type": "torus",
            "major_radius": face.get("major_radius", 0),
            "minor_radius": face.get("minor_radius", 0),
            "center": face.get("center"),
            "axis": face.get("axis"),
            "alignment_plane": face.get("alignment_plane", "unknown"),
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": True,
        }
    else:
        return None

    labels = _compute_semantic_labels(mapped, global_bbox)
    mapped["semantic_label"] = labels
    mapped["spatial_context"] = _build_spatial_context(mapped, global_bbox, labels)
    return mapped


def map_geometry_to_context(geometry: dict[str, Any]) -> dict[str, Any]:
    """
    Main entry point: Converts raw geometry into a structured context for the LLM.
    """
    counter: dict[str, int] = {}
    features: list[dict[str, Any]] = []
    global_bbox = geometry.get("bounding_box", {})

    # 1. Map raw faces to semantic features
    for face in geometry.get("faces", []):
        mapped = _map_single_face(face, counter, global_bbox)
        if mapped:
            features.append(mapped)

    # 2. Assign roles to each feature
    for feat in features:
        feat["role"] = _assign_role(feat, features)

    # 3. Infer relationships
    relationships = _infer_relationships(features)

    # 4. Build summary
    summary: dict[str, Any] = {}
    for key in ["topology", "bounding_box", "volume"]:
        if key in geometry:
            summary[key] = geometry[key]
        
    summary["component_count"] = len(features)
    summary["total_feature_instances"] = len(features)

    return {
        "summary": summary,
        "components": features,
        "relationships": relationships,
    }
