"""
Composite Structure Detection Layer
Detects and groups related geometries into hierarchical assemblies.

Pure-Python implementation — no scipy or numpy required.
"""

import math
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional


@dataclass
class GeometryCluster:
    """A hierarchical group of related geometries."""
    cluster_id: str
    component_type: str
    member_ids: List[str]
    primary_geometry: str
    secondary_geometries: Dict[str, str]
    confidence: float
    spatial_center: Tuple[float, float, float]
    bounding_box: Dict
    editing_hints: Dict
    role: str


# ── Pure-Python math helpers ─────────────────────────────────────────────────

def _distance(a: List[float], b: List[float]) -> float:
    """Euclidean distance between two 3-D points."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _mean_center(centers: List[List[float]]) -> Tuple[float, float, float]:
    n = len(centers)
    if n == 0:
        return (0.0, 0.0, 0.0)
    sx = sum(c[0] for c in centers)
    sy = sum(c[1] for c in centers)
    sz = sum(c[2] for c in centers)
    return (sx / n, sy / n, sz / n)


def _compute_size(component: Dict) -> float:
    """Approximate volume of a component."""
    pi = math.pi
    comp_type = component.get("type", "")
    if comp_type in ("cylinder", "hole"):
        r = component.get("radius", 0.0)
        h = component.get("height", 0.0)
        return pi * r ** 2 * h
    elif comp_type == "box":
        bbox = component.get("bbox", {})
        w = bbox.get("xmax", 0) - bbox.get("xmin", 0)
        d = bbox.get("ymax", 0) - bbox.get("ymin", 0)
        h = bbox.get("zmax", 0) - bbox.get("zmin", 0)
        return abs(w * d * h)
    elif comp_type == "sphere":
        r = component.get("radius", 0.0)
        return (4 / 3) * pi * r ** 3
    elif comp_type == "cone":
        r = component.get("ref_radius", component.get("radius", 0.0))
        h = component.get("height", 0.0)
        return (1 / 3) * pi * r ** 2 * h
    else:
        bbox = component.get("bbox", {})
        w = abs(bbox.get("xmax", 0) - bbox.get("xmin", 0))
        d = abs(bbox.get("ymax", 0) - bbox.get("ymin", 0))
        h = abs(bbox.get("zmax", 0) - bbox.get("zmin", 0))
        return w * d * h


# ── Greedy single-linkage clustering (pure Python) ───────────────────────────

def _greedy_cluster(components: List[Dict], threshold: float) -> List[List[int]]:
    """
    Groups component indices by proximity.
    Two components join the same cluster if their center-to-center distance
    is below *threshold*.  Uses single-linkage: a new component joins a
    cluster if it is close to ANY existing member.
    """
    n = len(components)

    # Pre-extract centers (handle missing center gracefully)
    centers = []
    for c in components:
        ctr = c.get("center")
        if ctr and len(ctr) >= 3:
            centers.append([float(ctr[0]), float(ctr[1]), float(ctr[2])])
        else:
            bbox = c.get("bbox", {})
            cx = (bbox.get("xmin", 0) + bbox.get("xmax", 0)) / 2
            cy = (bbox.get("ymin", 0) + bbox.get("ymax", 0)) / 2
            cz = (bbox.get("zmin", 0) + bbox.get("zmax", 0)) / 2
            centers.append([cx, cy, cz])

    cluster_of = list(range(n))  # each component starts in its own cluster

    for i in range(n):
        for j in range(i + 1, n):
            if cluster_of[i] == cluster_of[j]:
                continue
            if _distance(centers[i], centers[j]) <= threshold:
                # Merge cluster j's label into cluster i's label
                old_label = cluster_of[j]
                new_label = cluster_of[i]
                for k in range(n):
                    if cluster_of[k] == old_label:
                        cluster_of[k] = new_label

    # Group indices by cluster label
    groups: Dict[int, List[int]] = {}
    for idx, label in enumerate(cluster_of):
        groups.setdefault(label, []).append(idx)

    return list(groups.values())


# ── Main analyser class ──────────────────────────────────────────────────────

class CompositeStructureAnalyzer:
    """Detects and groups related geometries into logical assemblies."""

    def __init__(self, distance_threshold: float = 150.0, semantic_weight: float = 0.6):
        self.distance_threshold = distance_threshold
        self.semantic_weight = semantic_weight

    def analyze(self, parsed_components: List[Dict]) -> List[GeometryCluster]:
        """Main entry point: cluster geometries."""
        if not parsed_components:
            return []

        # Filter to components that have a center
        valid = [c for c in parsed_components if c.get("center") or c.get("bbox")]
        if not valid:
            # Treat everything as one cluster
            valid = parsed_components

        groups = _greedy_cluster(valid, self.distance_threshold)

        clusters: List[GeometryCluster] = []
        for i, member_indices in enumerate(groups):
            members = [valid[idx] for idx in member_indices]
            cluster = self._build_cluster(
                cluster_id=f"cluster_{i + 1}",
                members=members,
                all_components=parsed_components
            )
            clusters.append(cluster)

        return clusters

    # ── Private helpers ──────────────────────────────────────────────────────

    def _build_cluster(self, cluster_id: str, members: List[Dict],
                       all_components: List[Dict]) -> GeometryCluster:
        member_ids = [m["id"] for m in members]
        sizes = [_compute_size(m) for m in members]

        primary_idx = sizes.index(max(sizes))
        primary_geometry = member_ids[primary_idx]

        secondary_geometries: Dict[str, str] = {}
        for i, member in enumerate(members):
            if i == primary_idx:
                continue
            role = member.get("role", "unknown")
            if role not in secondary_geometries:
                secondary_geometries[role] = member_ids[i]

        centers = []
        for m in members:
            ctr = m.get("center")
            if ctr and len(ctr) >= 3:
                centers.append(ctr)
            else:
                bbox = m.get("bbox", {})
                centers.append([
                    (bbox.get("xmin", 0) + bbox.get("xmax", 0)) / 2,
                    (bbox.get("ymin", 0) + bbox.get("ymax", 0)) / 2,
                    (bbox.get("zmin", 0) + bbox.get("zmax", 0)) / 2,
                ])

        spatial_center = _mean_center(centers)

        bboxes = [m.get("bbox", {}) for m in members if m.get("bbox")]
        if bboxes:
            aggregate_bbox = {
                "xmin": min(b.get("xmin", 0) for b in bboxes),
                "xmax": max(b.get("xmax", 0) for b in bboxes),
                "ymin": min(b.get("ymin", 0) for b in bboxes),
                "ymax": max(b.get("ymax", 0) for b in bboxes),
                "zmin": min(b.get("zmin", 0) for b in bboxes),
                "zmax": max(b.get("zmax", 0) for b in bboxes),
            }
        else:
            aggregate_bbox = {}

        component_type = self._infer_component_type(members)
        editing_hints = self._generate_editing_hints(primary_geometry, secondary_geometries, all_components)

        # Confidence: ratio of compact internal distances
        if len(centers) > 1:
            dists = [
                _distance(centers[i], centers[j])
                for i in range(len(centers))
                for j in range(i + 1, len(centers))
            ]
            avg_dist = sum(dists) / len(dists)
            confidence = max(0.0, min(1.0, 1.0 - (avg_dist / max(self.distance_threshold, 1))))
        else:
            confidence = 0.9

        return GeometryCluster(
            cluster_id=cluster_id,
            component_type=component_type,
            member_ids=member_ids,
            primary_geometry=primary_geometry,
            secondary_geometries=secondary_geometries,
            confidence=confidence,
            spatial_center=spatial_center,
            bounding_box=aggregate_bbox,
            editing_hints=editing_hints,
            role=members[primary_idx].get("role", "unknown")
        )

    def _infer_component_type(self, members: List[Dict]) -> str:
        types_present = {m["type"] for m in members}
        labels_present: set = set()
        for m in members:
            labels_present.update(m.get("semantic_label", []))

        if "support_leg" in labels_present:
            return "leg" if "cylinder" in types_present else "support_structure"
        if "connector" in labels_present and len(members) >= 2:
            return "mounting_bracket"
        if "handle" in labels_present:
            return "handle"
        if len(types_present) == 1:
            return next(iter(types_present))
        return "composite_structure"

    def _generate_editing_hints(self, primary_id: str, secondary: Dict,
                                all_components: List[Dict]) -> Dict:
        primary = next((c for c in all_components if c.get("id") == primary_id), {})
        primary_type = primary.get("type", "unknown")
        hints: Dict = {}

        if primary_type in ("cylinder", "hole"):
            hints["diameter"] = {
                "target": primary_id, "reason": "Primary cylindrical component",
                "impact": "Changes width/thickness", "property": "diameter"
            }
            hints["height"] = {
                "target": primary_id, "reason": "Primary cylindrical component",
                "impact": "Changes length/proportion", "property": "height"
            }
        elif primary_type == "box":
            for prop in ("width", "height", "depth"):
                hints[prop] = {
                    "target": primary_id, "reason": "Primary rectangular component",
                    "impact": f"Changes {prop}", "property": prop
                }
        elif primary_type == "sphere":
            hints["radius"] = {
                "target": primary_id, "reason": "Primary spherical component",
                "impact": "Changes size", "property": "radius"
            }

        return hints
