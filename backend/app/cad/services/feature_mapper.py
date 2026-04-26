"""
Feature Mapper — Converts raw geometry data into semantic, LLM-friendly features.
Removes pattern grouping entirely to preserve spatial independence.
"""

from typing import Any

_HOLE_RADIUS_THRESHOLD = 100.0


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
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": True,
        }
        mapped["semantic_label"] = _compute_semantic_labels(mapped, global_bbox)
        return mapped

    if ftype == "cone":
        mapped = {
            "id": _make_id("cone", counter),
            "type": "cone",
            "semi_angle_deg": face.get("semi_angle_deg", 0),
            "ref_radius": face.get("ref_radius", 0),
            "center": face.get("center"),
            "axis": face.get("axis"),
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": True,
        }
        mapped["semantic_label"] = _compute_semantic_labels(mapped, global_bbox)
        return mapped

    if ftype == "sphere":
        mapped = {
            "id": _make_id("sphere", counter),
            "type": "sphere",
            "radius": face.get("radius", 0),
            "diameter": round(face.get("radius", 0) * 2, 6),
            "center": face.get("center"),
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": True,
        }
        mapped["semantic_label"] = _compute_semantic_labels(mapped, global_bbox)
        return mapped

    if ftype == "torus":
        mapped = {
            "id": _make_id("torus", counter),
            "type": "torus",
            "major_radius": face.get("major_radius", 0),
            "minor_radius": face.get("minor_radius", 0),
            "center": face.get("center"),
            "axis": face.get("axis"),
            "bbox": face.get("bbox"),
            "face_id": face["face_id"],
            "editable": True,
        }
        mapped["semantic_label"] = _compute_semantic_labels(mapped, global_bbox)
        return mapped

    return None


def map_features(geometry: dict[str, Any]) -> list[dict[str, Any]]:
    counter: dict[str, int] = {}
    features: list[dict[str, Any]] = []

    global_bbox = geometry.get("bounding_box", {})

    for face in geometry.get("faces", []):
        mapped = _map_single_face(face, counter, global_bbox)
        if mapped is not None:
            features.append(mapped)

    return features


def map_features_compact(geometry: dict[str, Any]) -> dict[str, Any]:
    """
    Returns the flattened features list directly, abandoning the old grouping system.
    Maintains the top-level dictionary structure expected by routes.
    """
    raw_features = map_features(geometry)

    summary: dict[str, Any] = {}
    if "topology" in geometry:
        summary["topology"] = geometry["topology"]
    if "bounding_box" in geometry:
        summary["bounding_box"] = geometry["bounding_box"]
    if "volume" in geometry:
        summary["volume"] = geometry["volume"]
        
    summary["component_count"] = len(raw_features)
    summary["total_feature_instances"] = len(raw_features)

    return {
        "summary": summary,
        "components": raw_features,
        "relationships": [],
    }
