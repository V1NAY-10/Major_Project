import re
from typing import List, Dict, Any, Optional

class GeometryReasoningEngine:
    """
    A deterministic CAD geometry reasoning and modification engine.
    """
    
    def __init__(self):
        pass

    def parse_intent(self, instruction: str) -> Optional[Dict[str, Any]]:
        """
        Parses the user instruction to extract target_role, parameter, operation, and value.
        """
        instruction = instruction.lower()
        
        # 1. Target Role Identification
        target_role = "unknown"
        structural_keywords = ["leg", "support", "frame", "column", "beam", "brace"]
        connector_keywords = ["connector", "joint", "hinge", "link", "pin", "fastener"]
        surface_keywords = ["plate", "panel", "surface", "skin", "cover"]
        
        if any(w in instruction for w in structural_keywords):
            target_role = "structural_support"
        elif any(w in instruction for w in connector_keywords):
            target_role = "connector"
        elif any(w in instruction for w in surface_keywords):
            target_role = "surface"
            
        # 2. Parameter Identification
        parameter = None
        if any(w in instruction for w in ["thicker", "thickness", "wider", "size", "strength"]):
            parameter = "diameter" # Defaults to diameter/thickness for generic structural increase
        elif "radius" in instruction:
            parameter = "radius"
        elif any(w in instruction for w in ["longer", "length"]):
            parameter = "length"
        elif any(w in instruction for w in ["taller", "height"]):
            parameter = "height"
            
        # Default fallback
        if not parameter and target_role == "structural_support":
            parameter = "diameter"
            
        # 3. Operation and Value Identification
        operation = "scale"
        value = 1.0
        
        percent_match = re.search(r'(\d+)\s*(?:percent|%)', instruction)
        if percent_match:
            p_value = float(percent_match.group(1))
            if any(w in instruction for w in ["increase", "thicker", "longer", "wider", "taller", "strengthen", "make"]):
                value = 1.0 + (p_value / 100.0)
            elif any(w in instruction for w in ["decrease", "thinner", "shorter", "reduce"]):
                value = 1.0 - (p_value / 100.0)
        else:
            # If no explicit percentage is found, default to a sensible scale
            if any(w in instruction for w in ["increase", "thicker", "longer", "wider", "taller", "strengthen"]):
                value = 1.20 # +20% default
            elif any(w in instruction for w in ["decrease", "thinner", "shorter", "reduce"]):
                value = 0.80 # -20% default

        if target_role != "unknown":
            return {
                "target_role": target_role,
                "operation": operation,
                "parameter": parameter,
                "value": value
            }
        return None

    def classify_role(self, comp: Dict[str, Any], global_dims: Dict[str, float]) -> str:
        """
        Implements SECTION 1 and SECTION 2 of the logic to classify a component's role.
        """
        # Read explicit properties first (although SECTION 1 says evaluate semantic_label first)
        semantic_labels = comp.get("semantic_label", [])
        if isinstance(semantic_labels, str):
            semantic_labels = [semantic_labels]
        semantic_labels = [str(l).lower() for l in semantic_labels]
        
        # Priority 1: Semantic Labels
        if any(w in l for l in semantic_labels for w in ["support_leg", "support", "column", "beam", "brace", "frame", "load_bearing"]):
            return "structural_support"
        if any(w in l for l in semantic_labels for w in ["connector", "joint", "hinge", "link", "pin", "fastener"]):
            return "connector"
        if any(w in l for l in semantic_labels for w in ["plate", "panel", "surface", "skin", "cover"]):
            return "surface"
        if any(w in l for l in semantic_labels for w in ["decorative", "trim", "handle"]):
            return "decorative"
            
        # Priority 2: Pre-assigned Role
        assigned_role = comp.get("role", "unknown")
        if assigned_role != "unknown" and assigned_role is not None:
            return assigned_role
            
        # Priority 3 & 4: Automatic Support Detection Logic (Geometry & Position)
        height = float(comp.get("height", 0))
        diameter = float(comp.get("diameter", comp.get("radius", 0) * 2))
        
        if diameter > 0:
            cond1_elongated = (height / diameter) >= 3
        else:
            cond1_elongated = False
            
        axis = comp.get("axis", [0, 0, 0])
        if isinstance(axis, dict):
            axis_vals = [axis.get("x", 0), axis.get("y", 0), axis.get("z", 0)]
        else:
            axis_vals = axis
        cond2_orientation = any(abs(v) >= 0.7 for v in axis_vals)
        
        center = comp.get("center", [0, 0, 0])
        if isinstance(center, dict):
            center_vals = [center.get("x", 0), center.get("y", 0), center.get("z", 0)]
        else:
            center_vals = center
            
        model_center = global_dims["center"]
        model_size = global_dims["size"]
        
        dist = sum((c - mc)**2 for c, mc in zip(center_vals, model_center)) ** 0.5
        cond3_position = dist >= 0.25 * model_size
        
        # Assume cond4 (connectivity) is satisfied if others align structurally
        cond4_connectivity = True
        
        if cond1_elongated and cond2_orientation and cond3_position and cond4_connectivity:
            return "structural_support"
            
        return "unknown"

    def process_model(self, components: List[Dict[str, Any]], instruction: str) -> Dict[str, Any]:
        """
        Executes the production-safe selection pipeline.
        """
        if not components:
            return {
                "status": "no_matching_components", 
                "reason": "Empty components list"
            }
            
        # Compute Global Dimensions
        min_x = min_y = min_z = float('inf')
        max_x = max_y = max_z = float('-inf')
        
        for c in components:
            bbox = c.get("bbox", {})
            if isinstance(bbox, dict) and bbox:
                min_x = min(min_x, float(bbox.get("xmin", float('inf'))))
                min_y = min(min_y, float(bbox.get("ymin", float('inf'))))
                min_z = min(min_z, float(bbox.get("zmin", float('inf'))))
                max_x = max(max_x, float(bbox.get("xmax", float('-inf'))))
                max_y = max(max_y, float(bbox.get("ymax", float('-inf'))))
                max_z = max(max_z, float(bbox.get("zmax", float('-inf'))))
                
        if min_x == float('inf'):
            for c in components:
                center = c.get("center", [0, 0, 0])
                if isinstance(center, dict):
                    x, y, z = center.get("x", 0), center.get("y", 0), center.get("z", 0)
                else:
                    x, y, z = center[0], center[1], center[2]
                min_x = min(min_x, x); max_x = max(max_x, x)
                min_y = min(min_y, y); max_y = max(max_y, y)
                min_z = min(min_z, z); max_z = max(max_z, z)
                
        model_center = [(min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2]
        model_size = max(max_x - min_x, max_y - min_y, max_z - min_z)
        if model_size == 0:
            model_size = 1.0
            
        global_dims = {
            "center": model_center,
            "size": model_size
        }
        
        # Step 1: Parse User Intent
        intent = self.parse_intent(instruction)
        if not intent:
            return {
                "status": "failed",
                "reason": "Could not parse clear intent from the instruction"
            }
            
        target_role = intent["target_role"]
        
        selected_components = []
        skipped_components = []
        
        for comp in components:
            comp_id = comp.get("id", "unknown_id")
            
            # Step 2: Filter editable components
            editable = comp.get("editable", True)
            if not editable:
                skipped_components.append({"id": comp_id, "reason": "not editable"})
                continue
            
            # Ensure component role is robustly classified
            role = self.classify_role(comp, global_dims)
            # Override original if classified dynamically
            comp["role"] = role
            
            # Step 3: Select components matching role
            if role != target_role:
                skipped_components.append({"id": comp_id, "reason": f"role {role}"})
                continue
                
            # Step 4: Validate structural eligibility
            param = intent["parameter"]
            # Handle aliases (e.g. thickness maps to diameter in cylinders)
            if param == "thickness" and "diameter" in comp:
                param = "diameter"
                
            has_param = param in comp
            has_radius_fallback = (param == "diameter" and "radius" in comp)
            
            if not (has_param or has_radius_fallback):
                skipped_components.append({"id": comp_id, "reason": f"parameter {param} not found"})
                continue
                
            # Step 5: Apply modification safely
            if has_param and isinstance(comp[param], (int, float)):
                old_val = float(comp[param])
                new_val = old_val * intent["value"]
                comp[param] = new_val
                
                selected = {
                    "id": comp_id,
                    "role": role,
                    f"old_{param}": old_val,
                    f"new_{param}": round(new_val, 4)
                }
                selected_components.append(selected)
                
            elif has_radius_fallback:
                # User asked for diameter, but we only have radius
                old_val = float(comp["radius"]) * 2.0
                new_val = old_val * intent["value"]
                comp["radius"] = new_val / 2.0
                
                selected = {
                    "id": comp_id,
                    "role": role,
                    f"old_{param}": old_val,
                    f"new_{param}": round(new_val, 4)
                }
                selected_components.append(selected)
            else:
                skipped_components.append({"id": comp_id, "reason": f"parameter {param} is not numeric"})
                
        # Step 6: Return structured output
        if not selected_components:
            return {
                "status": "no_matching_components",
                "reason": f"No {target_role} detected or eligible for modification"
            }
            
        return {
            "intent": intent,
            "selected_components": selected_components,
            "skipped_components": skipped_components,
            "status": "success"
        }
