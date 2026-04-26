"""
Pattern Detector — Groups raw features into deduplicated geometric patterns.

Responsibilities:
  - Cluster features by (type, primary dimension, orientation) within a
    configurable similarity threshold.
  - Emit ONE pattern entry per cluster instead of N individual duplicates.
  - Dramatically reduce JSON payload while preserving all information needed
    for LLM reasoning and CAD edits.

Works for ANY CAD file — zero object-specific logic.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


# ── Thresholds ────────────────────────────────────────────────────────────────

# Two dimensions are "the same" if they differ by less than this fraction.
_REL_TOLERANCE = 0.02          # 2 % relative tolerance
# Or by less than this absolute value (mm).
_ABS_TOLERANCE = 0.05          # 0.05 mm absolute tolerance

# Axis vectors are "the same orientation" if the angle between them is < this.
_AXIS_ANGLE_TOLERANCE_DEG = 5.0


# ── Helpers ──────────────────────────────────────────────────────────────────

def _dims_similar(a: float, b: float) -> bool:
    """Return True when |a-b| is within relative or absolute tolerance."""
    if a == 0 and b == 0:
        return True
    abs_diff = abs(a - b)
    if abs_diff <= _ABS_TOLERANCE:
        return True
    ref = max(abs(a), abs(b))
    return (abs_diff / ref) <= _REL_TOLERANCE


def _axis_similar(ax1: list[float] | None, ax2: list[float] | None) -> bool:
    """Return True if two 3-D axis vectors point in (anti-)parallel directions."""
    if ax1 is None and ax2 is None:
        return True
    if ax1 is None or ax2 is None:
        return False

    def _norm(v: list[float]) -> list[float]:
        mag = math.sqrt(sum(c * c for c in v))
        return [c / mag for c in v] if mag else v

    n1, n2 = _norm(ax1), _norm(ax2)
    dot = sum(a * b for a, b in zip(n1, n2))
    dot = max(-1.0, min(1.0, dot))          # clamp for numerical safety
    angle_deg = math.degrees(math.acos(abs(dot)))   # anti-parallel = same axis
    return angle_deg <= _AXIS_ANGLE_TOLERANCE_DEG


def _feature_key(feature: dict[str, Any]) -> tuple:
    """
    Build a coarse bucket key for a feature so we only compare features
    of the same type+orientation family in fine-grained clustering.
    """
    ftype = feature.get("type", "other")
    orientation = feature.get("orientation", "")
    return (ftype, orientation)


def _primary_dim(feature: dict[str, Any]) -> float:
    """Extract the single most representative dimension for a feature."""
    ftype = feature.get("type", "")
    if ftype in ("hole", "cylinder"):
        return feature.get("radius", 0.0)
    if ftype == "sphere":
        return feature.get("radius", 0.0)
    if ftype == "cone":
        return feature.get("ref_radius", 0.0)
    if ftype == "torus":
        return feature.get("major_radius", 0.0)
    if ftype == "body":
        bb = feature
        return max(bb.get("length", 0), bb.get("width", 0), bb.get("height", 0))
    if ftype == "plane":
        return feature.get("area", 0.0)
    return 0.0


def _secondary_dim(feature: dict[str, Any]) -> float:
    """Secondary dimension for finer grouping (e.g. torus minor radius)."""
    ftype = feature.get("type", "")
    if ftype == "torus":
        return feature.get("minor_radius", 0.0)
    if ftype == "cone":
        return feature.get("semi_angle_deg", 0.0)
    return 0.0


def _representative(members: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Build a single representative dict from a cluster of identical features.
    Uses the first member's values (they're all similar by construction).
    """
    first = members[0]
    rep: dict[str, Any] = {k: v for k, v in first.items()
                           if k not in ("id", "face_id", "pattern_group", "pattern_count")}
    return rep


# ── Public API ────────────────────────────────────────────────────────────────

def detect_patterns(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Collapse a list of raw features into a compact list of pattern entries.

    Each pattern entry represents ONE or MORE geometrically identical features.
    When count == 1 the entry is just a regular (non-repeated) feature.

    Parameters
    ----------
    features : list[dict]
        Output of ``feature_mapper.map_features()``.

    Returns
    -------
    list[dict]
        Pattern entries, e.g.::

            {
                "id": "pattern_1",
                "type": "cylinder_pattern",
                "radius": 5.0,
                "diameter": 10.0,
                "orientation": "internal",
                "count": 4,
                "editable": True,
                "source_ids": ["hole_1", "hole_2", "hole_3", "hole_4"]
            }

        Single (non-repeated) features keep their original type name.
    """
    # ── Step 1: bucket by coarse key ─────────────────────────────────────────
    buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for f in features:
        buckets[_feature_key(f)].append(f)

    patterns: list[dict[str, Any]] = []
    pattern_counter = 0

    # ── Step 2: fine-grained clustering within each bucket ───────────────────
    for _bucket_key, bucket_features in buckets.items():
        clusters: list[list[dict[str, Any]]] = []

        for feature in bucket_features:
            placed = False
            pdim = _primary_dim(feature)
            sdim = _secondary_dim(feature)
            faxis = feature.get("axis")

            for cluster in clusters:
                rep = cluster[0]
                if (
                    _dims_similar(pdim, _primary_dim(rep))
                    and _dims_similar(sdim, _secondary_dim(rep))
                    and _axis_similar(faxis, rep.get("axis"))
                ):
                    cluster.append(feature)
                    placed = True
                    break

            if not placed:
                clusters.append([feature])

        # ── Step 3: emit one pattern per cluster ─────────────────────────────
        for cluster in clusters:
            pattern_counter += 1
            count = len(cluster)
            base = _representative(cluster)

            ftype = base.get("type", "other")
            source_ids = [m.get("id", "") for m in cluster]

            if count > 1:
                entry_type = f"{ftype}_pattern"
            else:
                entry_type = ftype

            entry: dict[str, Any] = {
                "id": f"pattern_{pattern_counter}",
                "type": entry_type,
                "count": count,
                "editable": base.get("editable", True),
                "source_ids": source_ids,
            }

            # Copy over all geometric fields from representative
            for k, v in base.items():
                if k not in ("id", "type", "editable", "face_id",
                             "pattern_group", "pattern_count"):
                    entry[k] = v

            patterns.append(entry)

    return patterns
