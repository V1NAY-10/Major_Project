"""
Intent Disambiguation Layer
Converts user prompts to structured modification instructions.
"""

from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import re


@dataclass
class DisambiguatedIntent:
    """Fully resolved user intent."""
    original_prompt: str
    target_clusters: List[Tuple[str, float]]
    target_geometries: List[Tuple[str, float]]
    target_property: str
    property_confidence: float
    modification_type: str
    quantitative_value: Optional[float]
    qualitative_direction: Optional[str]
    rationale: str
    secondary_modifications: List[Dict]
    confidence_overall: float
    alternative_interpretations: List[str]


class IntentDisambiguator:
    """Converts natural language into structured CAD operations."""
    
    def __init__(self):
        self.component_terms = {
            "leg": ["leg", "legs", "support", "column"],
            "handle": ["handle", "grip", "knob"],
            "base": ["base", "base plate", "footer", "stand"],
            "connector": ["connector", "joint", "bracket", "mount"],
            "body": ["body", "frame", "shell", "case"]
        }
        self.spatial_terms = ["front", "back", "left", "right", "top", "bottom", "upper", "lower"]
        self.properties = {
            "width": ["width", "wideness", "thick", "thickness"],
            "diameter": ["diameter", "thick"],
            "height": ["height", "length", "tall"],
            "depth": ["depth", "deep"],
        }
    
    def disambiguate(self, prompt: str, clusters: List, 
                     all_components: List[Dict]) -> DisambiguatedIntent:
        """Main entry point."""
        
        # Step 1: Extract component references
        component_matches = self._extract_component_references(prompt)
        target_clusters = self._resolve_clusters(component_matches, clusters)
        
        # Step 2: Extract property references
        property_matches = self._extract_property_references(prompt)
        target_property, property_conf = self._resolve_property(
            property_matches, 
            target_clusters, 
            clusters,
            all_components
        )
        
        # Step 3: Extract modification
        mod_type, mod_value, qual_dir = self._extract_modification(prompt)
        
        # Step 4: Generate secondary modifications
        secondary_mods = self._generate_secondary_modifications(
            target_clusters, 
            target_property, 
            mod_type,
            clusters
        )
        
        # Step 5: Alternatives
        alternatives = self._generate_alternatives(prompt, target_clusters)
        
        # Step 6: Overall confidence
        overall_conf = self._compute_confidence(
            component_matches, 
            property_matches, 
            target_clusters,
            property_conf
        )
        
        return DisambiguatedIntent(
            original_prompt=prompt,
            target_clusters=target_clusters,
            target_geometries=self._resolve_target_geometries(target_clusters, clusters),
            target_property=target_property,
            property_confidence=property_conf,
            modification_type=mod_type,
            quantitative_value=mod_value,
            qualitative_direction=qual_dir,
            rationale=self._build_rationale(
                prompt, 
                component_matches, 
                property_matches, 
                target_clusters, 
                target_property
            ),
            secondary_modifications=secondary_mods,
            confidence_overall=overall_conf,
            alternative_interpretations=alternatives
        )
    
    def _extract_component_references(self, prompt: str) -> List[Dict]:
        """Extract component references from prompt."""
        matches = []
        prompt_lower = prompt.lower()
        
        for comp_name, aliases in self.component_terms.items():
            for alias in aliases:
                if alias in prompt_lower:
                    spatial = None
                    for spatial_term in self.spatial_terms:
                        pattern = f"{spatial_term}\\s+{re.escape(alias)}"
                        if re.search(pattern, prompt_lower):
                            spatial = spatial_term
                            break
                    
                    matches.append({
                        "term": alias,
                        "component_type": comp_name,
                        "spatial_descriptor": spatial,
                        "confidence": 0.9 if spatial else 0.7
                    })
        
        return matches
    
    def _resolve_clusters(self, component_matches: List[Dict], 
                         clusters: List) -> List[Tuple[str, float]]:
        """Map component references to clusters."""
        resolved = []
        
        for match in component_matches:
            comp_type = match["component_type"]
            spatial_desc = match["spatial_descriptor"]
            
            candidates = [c for c in clusters if c.component_type == comp_type]
            
            if not candidates:
                continue
            
            if spatial_desc:
                filtered = self._filter_by_spatial_descriptor(candidates, spatial_desc)
                candidates = filtered if filtered else candidates
            
            for cluster in candidates:
                conf = match["confidence"] * cluster.confidence
                resolved.append((cluster.cluster_id, conf))
        
        if not resolved:
            resolved = [(c.cluster_id, c.confidence) for c in clusters if c.confidence > 0.6]
        
        return resolved
    
    def _extract_property_references(self, prompt: str) -> List[Dict]:
        """Extract property references."""
        matches = []
        prompt_lower = prompt.lower()
        
        for prop_name, aliases in self.properties.items():
            for alias in aliases:
                if alias in prompt_lower:
                    matches.append({
                        "property": prop_name,
                        "alias_used": alias,
                        "confidence": 0.9
                    })
        
        return matches
    
    def _resolve_property(self, property_matches: List[Dict], 
                         target_clusters: List[Tuple[str, float]],
                         clusters: List,
                         all_components: List[Dict]) -> Tuple[str, float]:
        """Resolve property based on geometry type."""
        
        if not property_matches:
            return "diameter", 0.3
        
        # Get primary geometries
        primary_geos = []
        for cluster_id, _ in target_clusters:
            cluster = next((c for c in clusters if c.cluster_id == cluster_id), None)
            if cluster:
                primary_geos.append(cluster.primary_geometry)
        
        # Check types
        type_counts = {}
        for geo_id in primary_geos:
            geo = next((c for c in all_components if c["id"] == geo_id), None)
            if geo:
                geo_type = geo["type"]
                type_counts[geo_type] = type_counts.get(geo_type, 0) + 1
        
        dominant_type = max(type_counts, key=type_counts.get) if type_counts else "cylinder"
        
        prop_ref = property_matches[0]
        user_property = prop_ref["property"]
        
        if dominant_type == "cylinder":
            mapping = {"width": "diameter", "diameter": "diameter", "height": "height", "depth": "height"}
            resolved = mapping.get(user_property, "diameter")
            confidence = 0.95
        elif dominant_type == "box":
            mapping = {"width": "width", "diameter": "width", "height": "height", "depth": "depth"}
            resolved = mapping.get(user_property, "width")
            confidence = 0.95
        else:
            resolved = user_property
            confidence = 0.6
        
        return resolved, confidence
    
    def _extract_modification(self, prompt: str) -> Tuple[str, Optional[float], str]:
        """Extract modification type and value."""
        mod_type = "scale"
        mod_value = None
        qual_dir = None
        
        prompt_lower = prompt.lower()
        
        if any(word in prompt_lower for word in ["increase", "bigger", "larger", "expand", "widen"]):
            qual_dir = "increase"
        elif any(word in prompt_lower for word in ["decrease", "smaller", "reduce", "shrink"]):
            qual_dir = "decrease"
        
        # Percentage
        percent_match = re.search(r"by\s+(\d+(?:\.\d+)?)\s*%", prompt_lower)
        if percent_match:
            percent = float(percent_match.group(1))
            mod_type = "scale"
            mod_value = (100 + percent) / 100 if qual_dir == "increase" else (100 - percent) / 100
        
        # Absolute
        abs_match = re.search(r"by\s+(\d+(?:\.\d+)?)\s*(cm|mm|in)", prompt_lower)
        if abs_match:
            value = float(abs_match.group(1))
            unit = abs_match.group(2)
            if unit == "cm":
                value *= 10
            elif unit == "in":
                value *= 25.4
            mod_type = "offset"
            mod_value = value if qual_dir == "increase" else -value
        
        return mod_type, mod_value, qual_dir
    
    def _generate_secondary_modifications(self, target_clusters: List[Tuple[str, float]], 
                                        target_property: str, mod_type: str,
                                        clusters: List) -> List[Dict]:
        """Generate related modifications."""
        secondary = []
        
        for cluster_id, _ in target_clusters:
            cluster = next((c for c in clusters if c.cluster_id == cluster_id), None)
            if not cluster:
                continue
            
            if cluster.component_type == "leg" and target_property == "diameter":
                if "connector" in cluster.secondary_geometries:
                    secondary.append({
                        "target": cluster.secondary_geometries["connector"],
                        "property": "width",
                        "type": mod_type,
                        "ratio": 0.75,
                        "reason": "Proportional adjustment to maintain visual balance"
                    })
        
        return secondary
    
    def _generate_alternatives(self, prompt: str, target_clusters: List[Tuple[str, float]]) -> List[str]:
        """Generate alternatives."""
        alternatives = []
        
        if len(target_clusters) > 1:
            alternatives.append(f"Apply to all {len(target_clusters)} components")
        
        if "width" in prompt.lower() and "leg" in prompt.lower():
            alternatives.append("Increase leg diameter only")
            alternatives.append("Increase diameter AND base together")
        
        return alternatives
    
    def _compute_confidence(self, component_matches: List[Dict], 
                           property_matches: List[Dict],
                           target_clusters: List[Tuple[str, float]],
                           property_conf: float) -> float:
        """Compute overall confidence."""
        
        if not target_clusters or not property_matches:
            return 0.2
        
        component_conf = sum(c["confidence"] for c in component_matches) / max(len(component_matches), 1)
        cluster_conf = sum(conf for _, conf in target_clusters) / max(len(target_clusters), 1)
        
        overall = (component_conf + cluster_conf + property_conf) / 3
        return min(overall, 1.0)
    
    def _build_rationale(self, prompt: str, component_matches: List[Dict], 
                        property_matches: List[Dict], target_clusters: List[Tuple[str, float]],
                        target_property: str) -> str:
        """Build explanation."""
        
        parts = []
        
        if component_matches:
            comp_names = ", ".join(m["component_type"] for m in component_matches)
            parts.append(f"Identified: {comp_names}")
        
        parts.append(f"Resolved {len(target_clusters)} cluster(s)")
        
        if property_matches:
            prop_name = property_matches[0]["property"]
            parts.append(f"'{prop_name}' → '{target_property}'")
        
        return "; ".join(parts)
    
    def _resolve_target_geometries(self, target_clusters: List[Tuple[str, float]], 
                                  clusters: List) -> List[Tuple[str, float]]:
        """Extract geometry IDs from clusters."""
        geometries = []
        for cluster_id, cluster_conf in target_clusters:
            cluster = next((c for c in clusters if c.cluster_id == cluster_id), None)
            if cluster:
                geometries.append((cluster.primary_geometry, cluster_conf))
                for role, geo_id in cluster.secondary_geometries.items():
                    geometries.append((geo_id, cluster_conf * 0.6))
        
        return geometries
    
    def _filter_by_spatial_descriptor(self, candidates: List, 
                                     descriptor: str) -> List:
        """Filter by spatial position."""
        
        filtered = []
        for cluster in candidates:
            center = cluster.spatial_center
            
            if descriptor == "front" and center[1] < 0:
                filtered.append(cluster)
            elif descriptor == "back" and center[1] > 0:
                filtered.append(cluster)
            elif descriptor == "left" and center[0] < 0:
                filtered.append(cluster)
            elif descriptor == "right" and center[0] > 0:
                filtered.append(cluster)
            elif descriptor == "top" and center[2] > 0:
                filtered.append(cluster)
            elif descriptor == "bottom" and center[2] < 0:
                filtered.append(cluster)
        
        return filtered
