from dataclasses import dataclass
from typing import Dict, List, Any

@dataclass  
class Constraint:
    constraint_type: str  # "tangent_join" | "coaxial" | "flush_face" | "diameter_match"
    source_node: str      # scene node id
    source_property: str  # "base_diameter" | "top_diameter" | "height"
    target_node: str
    target_property: str
    ratio: float = 1.0    # target = source * ratio (default 1:1)

class ConstraintGraph:
    def __init__(self, components: list[dict], adjacency: dict[str, list[int]]):
        self.components = {c.get("id"): c for c in components}
        self.adjacency = adjacency
        # Map face_id to component_id
        self.face_to_comp = {}
        for c in components:
            if "face_id" in c:
                self.face_to_comp[c["face_id"]] = c["id"]
                
        self.constraints = self._infer_constraints()
    
    def _infer_constraints(self):
        """Auto-detect constraints from geometry."""
        constraints = []
        seen_pairs = set()
        
        for face_id_a, adj_list in self.adjacency.items():
            comp_a_id = self.face_to_comp.get(int(face_id_a))
            if not comp_a_id: continue
            
            for face_id_b in adj_list:
                comp_b_id = self.face_to_comp.get(int(face_id_b))
                if not comp_b_id or comp_a_id == comp_b_id: continue
                
                # Prevent duplicate symmetric constraints
                pair = tuple(sorted([comp_a_id, comp_b_id]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                
                comp_a = self.components[comp_a_id]
                comp_b = self.components[comp_b_id]
                
                if self._diameters_match(comp_a, comp_b):
                    val_a = comp_a.get("diameter") or (comp_a.get("radius", 1.0) * 2)
                    val_b = comp_b.get("diameter") or (comp_b.get("radius", 1.0) * 2)
                    
                    if val_a > 0:
                        ratio = val_b / val_a
                        constraints.append(Constraint(
                            constraint_type="diameter_match",
                            source_node=comp_a_id,
                            source_property="diameter",
                            target_node=comp_b_id,
                            target_property="diameter",
                            ratio=ratio
                        ))
                        # Reverse constraint
                        constraints.append(Constraint(
                            constraint_type="diameter_match",
                            source_node=comp_b_id,
                            source_property="diameter",
                            target_node=comp_a_id,
                            target_property="diameter",
                            ratio=1.0/ratio
                        ))
                        
        return constraints
        
    def _diameters_match(self, comp_a, comp_b) -> bool:
        has_dim_a = "diameter" in comp_a or "radius" in comp_a
        has_dim_b = "diameter" in comp_b or "radius" in comp_b
        
        if has_dim_a and has_dim_b:
            ax_a = comp_a.get("axis", [0,0,1])
            ax_b = comp_b.get("axis", [0,0,1])
            if not ax_a or not ax_b: return False
            
            # Dot product to check for parallelism
            dot = sum(a*b for a,b in zip(ax_a, ax_b))
            return abs(abs(dot) - 1.0) < 0.1
        return False
    
    def propagate(self, changed_node: str, changed_property: str, new_value: float) -> list[dict]:
        """
        Returns list of secondary modifications to apply.
        BFS through constraint graph from changed_node.
        """
        secondary = []
        queue = [(changed_node, changed_property, new_value)]
        visited = set()
        
        while queue:
            src, prop, val = queue.pop(0)
            key = (src, prop)
            if key in visited:
                continue
            visited.add(key)
            
            for c in self.constraints:
                if c.source_node == src and c.source_property == prop:
                    if (c.target_node, c.target_property) not in visited:
                        derived_val = val * c.ratio
                        secondary.append({
                            "action": "scale_" + c.target_property,
                            "target_pattern": c.target_node,
                            "property": c.target_property,
                            "value": derived_val,
                            "reason": f"Constraint '{c.constraint_type}': {src}.{prop} -> {c.target_node}.{c.target_property}"
                        })
                        queue.append((c.target_node, c.target_property, derived_val))
        
        return secondary
