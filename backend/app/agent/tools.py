"""
Tools exposed to the LLM. Each edit tool maps 1:1 to an engine operation in
freecad_bridge/engine.py; the engine validates, rolls back on failure and
returns measured results.
"""

import os

from .. import cad_service as cad
from .. import freecad_client as fc

# run_freecad_python executes LLM-written code inside FreeCAD; off unless explicitly enabled.
ALLOW_PYTHON = os.getenv("AGENT_ALLOW_PYTHON", "0").lower() in ("1", "true", "yes")

VEC3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
FEATURE_ID = {"type": "string", "description": "Feature ID from the model data, e.g. H3, P1, C2, R4"}
OBJECT = {"type": "string", "description": "Object name (only needed when the model has several solid objects)"}

EDGE_SELECTOR = {
    "type": "object",
    "description": (
        "Selects edges. Combine keys as needed: "
        "{face: 'P3'} = all edges bounding face P3; "
        "{between: ['P1','P5']} = the edge(s) shared by two faces; "
        "{hole: 'H2', end: 'entry'|'exit'|'both'} = circular rim edges of a hole (entry = open side it is drilled from); "
        "{boss: 'C1', end: 'free'|'base'|'both'} = rim edges of a boss (free = its tip/top, base = where it joins the body); "
        "{parallel_to: 'Z'} = straight sharp edges parallel to an axis (optionally limited with on_face: 'P2'); "
        "{all: true} = every sharp edge."
    ),
    "properties": {
        "face": {"type": "string"},
        "between": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 2},
        "hole": {"type": "string"},
        "boss": {"type": "string"},
        "end": {"type": "string", "enum": ["entry", "exit", "free", "base", "both"]},
        "parallel_to": {"type": "string", "enum": ["X", "Y", "Z"]},
        "on_face": {"type": "string"},
        "all": {"type": "boolean"},
    },
}

EDGE_SELECTOR_BRIEF = {k: v for k, v in EDGE_SELECTOR.items() if k != "description"}


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


TOOLS = [
    # ── Queries ──────────────────────────────────────────────────────────────
    _fn("get_model_info", "Re-read the full current model data (objects, features with IDs, parameters)."),
    _fn("list_objects", "List every part/solid object (largest first) with label, parent assembly, volume and "
        "bbox min/max/size. Use it to find a part by name or size (e.g. 'the table top') and to get its dimensions.", {
            "name_contains": {"type": "string", "description": "Optional case-insensitive filter on name/label"}}),
    _fn("find_features", "List features matching filters. Use for large models or to resolve references like "
        "'the 6 mm holes' or 'the hole nearest the corner'.", {
            "kind": {"type": "string", "enum": ["hole", "boss", "round", "plane", "cone", "sphere"]},
            "diameter_min": {"type": "number"}, "diameter_max": {"type": "number"},
            "normal": {"type": "string", "description": "For planes: +X, -X, +Y, -Y, +Z or -Z"},
            "near": {**VEC3, "description": "Sort by distance to this point [x,y,z]"},
            "object": {"type": "string", "description": "Only features of this object name"},
            "limit": {"type": "integer"},
        }),
    _fn("measure", "Measure between two references (feature IDs, object names or [x,y,z] points). Returns "
        "minimum distance, and for parallel holes/planes the axis-to-axis / plane-to-plane distance.", {
            "a": {"description": "Feature ID, object name or [x,y,z]"},
            "b": {"description": "Feature ID, object name or [x,y,z]"},
        }, ["a", "b"]),

    # ── B-rep edits ──────────────────────────────────────────────────────────
    _fn("resize_hole", "Change a hole's diameter, keeping its axis, depth and countersink.", {
        "hole": FEATURE_ID, "diameter": {"type": "number", "description": "New diameter in mm"}},
        ["hole", "diameter"]),
    _fn("move_hole", "Move a hole sideways (perpendicular to its axis). Give either offset or to.", {
        "hole": FEATURE_ID, "offset": {**VEC3, "description": "[dx,dy,dz] in mm"},
        "to": {**VEC3, "description": "Absolute point the hole axis should pass through"}}, ["hole"]),
    _fn("add_holes", "Drill one or more holes into a planar face, perpendicular to it.", {
        "face": FEATURE_ID, "diameter": {"type": "number"},
        "positions": {"type": "array", "items": VEC3,
                      "description": "Absolute [x,y,z] points on the face; omit for the face centre"},
        "depth": {"type": "number", "description": "Blind depth in mm; omit for a through hole"}},
        ["face", "diameter"]),
    _fn("remove_features", "Remove features (fill holes, delete bosses, remove fillets/rounds) by defeaturing.", {
        "features": {"type": "array", "items": {"type": "string"}}}, ["features"]),
    _fn("push_pull_face", "Move a planar face along its outward normal: positive adds material (longer/thicker), "
        "negative removes material.", {"face": FEATURE_ID, "distance": {"type": "number"}}, ["face", "distance"]),
    _fn("set_face_distance", "Move planar 'face' so its distance to the parallel 'reference_face' is exactly "
        "'distance' (e.g. set overall length/width/thickness). 'face' moves; the reference stays.", {
            "face": FEATURE_ID, "reference_face": FEATURE_ID, "distance": {"type": "number"}},
        ["face", "reference_face", "distance"]),
    _fn("fillet_edges", "Round edges with a radius.", {
        "radius": {"type": "number"}, "edges": {"type": "array", "items": EDGE_SELECTOR}, "object": OBJECT},
        ["radius", "edges"]),
    _fn("chamfer_edges", "Chamfer edges with an equal-distance chamfer. 'edges' uses the same selector format "
        "as fillet_edges.", {
        "size": {"type": "number"}, "edges": {"type": "array", "items": EDGE_SELECTOR_BRIEF}, "object": OBJECT},
        ["size", "edges"]),
    _fn("add_primitive", "Add (fuse), subtract (cut), intersect (common), create (new) a primitive solid, or "
        "replace an object's whole shape with it (replace, e.g. turn a rectangular top into a round one). "
        "Box: position = box centre, "
        "dimensions {length (X), width (Y), height (Z)}. Cylinder: position = centre of its base, dimensions "
        "{radius|diameter, height}, direction = axis. Cone: {radius1, radius2, height}. Sphere: position = centre, "
        "{radius}. Torus: {major_radius, minor_radius}.", {
            "shape": {"type": "string", "enum": ["box", "cylinder", "cone", "sphere", "torus"]},
            "dimensions": {"type": "object"},
            "position": VEC3,
            "direction": {"type": "string", "description": "Axis direction: X, Y, Z, -X, -Y or -Z (default Z)"},
            "operation": {"type": "string", "enum": ["fuse", "cut", "common", "replace", "new"]},
            "object": OBJECT}, ["shape", "dimensions", "position"]),
    _fn("transform_object", "Move and/or rotate a whole object.", {
        "object": OBJECT, "translate": {**VEC3, "description": "[dx,dy,dz] mm"},
        "rotate": {"type": "object", "properties": {
            "axis": {"type": "string", "description": "X, Y or Z"}, "angle": {"type": "number", "description": "degrees"},
            "center": VEC3}}}),
    _fn("scale_object", "Scale a whole object uniformly (factor) or per axis (factors [sx,sy,sz]).", {
        "factor": {"type": "number"}, "factors": VEC3, "object": OBJECT, "center": VEC3}),

    # ── Parametric edits (FCStd) ────────────────────────────────────────────
    _fn("set_property", "Set a parametric property, e.g. Pad.Length, Box.Height, Cylinder.Radius, "
        "PolarPattern.Occurrences.", {
            "object": {"type": "string"}, "property": {"type": "string"},
            "value": {"description": "Number (mm / degrees), boolean or enum string"}},
        ["object", "property", "value"]),
    _fn("set_sketch_constraint", "Set a dimensional sketch constraint (by name or index).", {
        "sketch": {"type": "string"}, "constraint": {"type": "string", "description": "Constraint name or index"},
        "value": {"type": "number", "description": "mm, or degrees for angles"}},
        ["sketch", "constraint", "value"]),
    _fn("set_spreadsheet_cell", "Set a spreadsheet cell or alias that drives the model.", {
        "sheet": {"type": "string"}, "cell": {"type": "string", "description": "Alias or address like B2"},
        "value": {"type": "number"}}, ["sheet", "cell", "value"]),
    _fn("delete_object", "Delete an object that nothing depends on.", {"object": {"type": "string"}}, ["object"]),

    # ── Escape hatch ────────────────────────────────────────────────────────
    _fn("run_freecad_python", "LAST RESORT when no other tool can do it. Runs Python in FreeCAD with `doc`, `App`, "
        "`Part`, `V` (Vector) available; runs in an undoable transaction; print() output is returned.", {
            "code": {"type": "string"}, "description": {"type": "string"}}, ["code", "description"]),
]

EDIT_TOOLS = {
    "resize_hole", "move_hole", "add_holes", "remove_features", "push_pull_face", "set_face_distance",
    "fillet_edges", "chamfer_edges", "add_primitive", "transform_object", "scale_object", "set_property",
    "set_sketch_constraint", "set_spreadsheet_cell", "delete_object", "run_freecad_python",
}
OP_NAME = {"run_freecad_python": "run_python"}
PARAMETRIC_TOOLS = {"set_property", "set_sketch_constraint", "set_spreadsheet_cell"}


def tools_for(meta):
    """Tool definitions to send for this model: parametric tools only when it has parameters (saves tokens)."""
    info = (meta or {}).get("info") or {}
    skip = set() if ALLOW_PYTHON else {"run_freecad_python"}
    if not (info.get("parametric") or info.get("sketches") or info.get("spreadsheets")):
        skip |= PARAMETRIC_TOOLS
    return [t for t in TOOLS if t["function"]["name"] not in skip]


def _feature_ids(info):
    return {f["id"] for fs in (info or {}).get("features", {}).values() for f in fs}


async def execute(meta, name, args):
    """Run one tool call. Returns (result_for_llm, record_for_ui)."""
    args = dict(args or {})
    record = {"tool": name, "args": args, "ok": False}
    try:
        if name == "get_model_info":
            from .prompt import summarize_model
            info = await cad.refresh_info(meta)
            return {"ok": True, "model": summarize_model(info, full=True)}, None
        if name == "find_features":
            res = await cad.query(meta, "find_features", **args)
            return {"ok": True, **res}, None
        if name == "measure":
            res = await cad.query(meta, "measure", **args)
            return {"ok": True, **res}, None
        if name == "list_objects":
            res = await cad.query(meta, "list_objects", **args)
            return {"ok": True, **res}, None
        if name == "run_freecad_python" and not ALLOW_PYTHON:
            return {"ok": False, "error": "run_freecad_python is disabled on this server (AGENT_ALLOW_PYTHON=0)."}, None
        if name not in EDIT_TOOLS:
            return {"ok": False, "error": f"Unknown tool '{name}'"}, None

        if name == "scale_object":
            if args.get("factors") is not None:
                args["factor"] = args.pop("factors")
            if args.get("factor") is None:
                raise cad.CadError("scale_object needs 'factor' or 'factors'")
        before = _feature_ids(meta["info"])
        res = await cad.apply_op(meta, OP_NAME.get(name, name), args)
        after = _feature_ids(res["info"])
        record.update(ok=True, message=res["message"], verified=res["verified"],
                      volume_before=res["volume_before"], volume_after=res["volume_after"],
                      new_features=sorted(after - before), removed_features=sorted(before - after))
        bbox = (res["info"] or {}).get("bbox")
        return {"ok": True, "message": res["message"], "verified": res["verified"],
                "new_feature_ids": record["new_features"], "removed_feature_ids": record["removed_features"],
                "volume_before": res["volume_before"], "volume_after": res["volume_after"],
                "model_bbox_size": bbox and bbox["size"]}, record
    except (fc.FreeCADError, cad.CadError) as e:
        record["error"] = str(e)
        return {"ok": False, "error": str(e)}, (record if name in EDIT_TOOLS else None)
