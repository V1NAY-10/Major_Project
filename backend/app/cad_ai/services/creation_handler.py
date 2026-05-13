CREATION_TEMPLATES = {
    "cylinder": """
        _new_cyl = Part.makeCylinder({radius}, {height})
        _new_cyl.Placement.Base = App.Vector({cx}, {cy}, {cz})
        shape_obj.Shape = shape_obj.Shape.fuse(_new_cyl)
""",
    "hole": """
        _cut_cyl = Part.makeCylinder({radius}, {height})
        _cut_cyl.Placement.Base = App.Vector({cx}, {cy}, {cz})
        shape_obj.Shape = shape_obj.Shape.cut(_cut_cyl)
""",
    "dome": """
        _dome = Part.makeSphere({radius})
        _dome.Placement.Base = App.Vector({cx}, {cy}, {cz})
        _half_space = Part.makeBox({size}, {size}, {radius}, App.Vector({cx}-{size}/2, {cy}-{size}/2, {cz}))
        _dome = _dome.common(_half_space)
        shape_obj.Shape = shape_obj.Shape.fuse(_dome)
""",
    "semisphere": """
        _dome = Part.makeSphere({radius})
        _dome.Placement.Base = App.Vector({cx}, {cy}, {cz})
        _half_space = Part.makeBox({size}, {size}, {radius}, App.Vector({cx}-{size}/2, {cy}-{size}/2, {cz}))
        _dome = _dome.common(_half_space)
        shape_obj.Shape = shape_obj.Shape.fuse(_dome)
""",
    "sphere": """
        _sphere = Part.makeSphere({radius})
        _sphere.Placement.Base = App.Vector({cx}, {cy}, {cz})
        shape_obj.Shape = shape_obj.Shape.fuse(_sphere)
""",
    "flange": """
        _flange = Part.makeCylinder({outer_r}, {thickness})
        _inner_cut = Part.makeCylinder({inner_r}, {thickness})
        _flange = _flange.cut(_inner_cut)
        _flange.Placement.Base = App.Vector({cx}, {cy}, {cz})
        shape_obj.Shape = shape_obj.Shape.fuse(_flange)
""",
    "fillet": """
        _shape = shape_obj.Shape
        _edges_to_fillet = []
        for _e in _shape.Edges:
            _edge_center = _e.CenterOfMass
            _dist = App.Vector({cx},{cy},{cz}).distanceToPoint(_edge_center)
            if _dist < {tolerance}:
                _edges_to_fillet.append(_e)
        if _edges_to_fillet:
            shape_obj.Shape = _shape.makeFillet({radius}, _edges_to_fillet)
"""
}

class CreationHandler:
    @staticmethod
    def generate_creation_script(intent: dict) -> str:
        """
        Generates FreeCAD python script snippet for creating new geometric features.
        """
        feature_type = intent.get("feature_type", "").lower()
        params = intent.get("parameters", {})
        placement = intent.get("placement", {})
        
        template = CREATION_TEMPLATES.get(feature_type)
        if not template:
            return f"        # Warning: Unsupported creation feature '{feature_type}'"
            
        # Format params
        fmt_args = {
            "radius": params.get("radius", 10.0),
            "height": params.get("height", 10.0),
            "outer_r": params.get("outer_r", 20.0),
            "inner_r": params.get("inner_r", 10.0),
            "thickness": params.get("thickness", 5.0),
            "size": params.get("radius", 10.0) * 3, # for bounding boxes
            "tolerance": params.get("tolerance", 50.0),
            "cx": placement.get("cx", 0.0),
            "cy": placement.get("cy", 0.0),
            "cz": placement.get("cz", 0.0)
        }
        
        try:
            return template.format(**fmt_args)
        except Exception as e:
            return f"        # Error formatting creation template: {e}"
