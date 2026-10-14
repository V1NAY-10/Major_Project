"""
Tools exposed to the LLM. Each edit tool maps 1:1 to an engine operation in
freecad_bridge/engine.py; the engine validates, rolls back on failure and
returns measured results.

Numeric arguments go through one unit layer (app/units.py): numbers are mm (or degrees), strings may carry
a unit ("1/4 in", "12.5 cm", "30°") and are converted exactly before they reach the engine.
"""

import os
import re

from .. import cad_service as cad
from .. import freecad_client as fc
from .. import precision, standards, units

# run_freecad_python executes LLM-written code inside FreeCAD; off unless explicitly enabled.
ALLOW_PYTHON = os.getenv("AGENT_ALLOW_PYTHON", "0").lower() in ("1", "true", "yes")

VEC3 = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3}
FEATURE_ID = {"type": "string", "description": "Feature ID from the model data, e.g. H3, P1, C2, R4"}
OBJECT = {"type": "string", "description": "Object name (only needed when the model has several solid objects)"}
LEN = {"type": ["number", "string"]}  # mm number, or a string with a unit (explained once in the system prompt)
DATUM_POS = {"type": "object", "description": "Distances from datums, e.g. {\"B\": 15, \"C\": 20}"}

EDGE_SELECTOR = {
    "type": "object",
    "description": (
        "Selects edges. Combine keys as needed: "
        "{face: 'P3'} = all edges bounding face P3; "
        "{between: ['P1','P5']} = the edge(s) shared by two faces; "
        "{hole: 'H2', end: 'entry'|'exit'|'both'} = circular rim edges of a hole (entry = open side it is drilled from); "
        "{boss: 'C1', end: 'free'|'base'|'both'} = rim edges of a boss (free = its tip/top, base = where it joins the body); "
        "{parallel_to: 'Z'} = straight sharp edges parallel to an axis, e.g. the vertical corners (optionally limited "
        "with on_face: 'P2'); "
        "{all: true} = every sharp edge; {edge: 'E12'} = one edge by ID (find_edges / picked in the viewer)."
    ),
    "properties": {
        "edge": {"type": "string"},
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
            "kind": {"type": "string", "enum": ["hole", "boss", "round", "plane", "cone", "sphere", "slot", "pocket",
                                                "chamfer"]},
            "diameter_min": {"type": "number"}, "diameter_max": {"type": "number"},
            "normal": {"type": "string", "description": "For planes: +X, -X, +Y, -Y, +Z or -Z"},
            "near": {**VEC3, "description": "Sort by distance to this point [x,y,z]"},
            "object": {"type": "string", "description": "Only features of this object name"},
            "limit": {"type": "integer"},
        }),
    _fn("list_dimensions", "List the model's driving dimensions and hole patterns; each ID is usable with "
        "set_dimension.", {
            "object": {"type": "string", "description": "Only dimensions of this object (name or label)"},
            "feature": {"type": "string", "description": "Only dimensions involving this feature ID, e.g. H3"},
            "type": {"type": "string", "enum": ["extent", "thickness", "gap", "edge_distance", "center_distance",
                                                "angle", "diameter", "radius", "coaxial"]},
            "limit": {"type": "integer"},
        }),
    _fn("measure", "Measure between two references (feature IDs, object names or [x,y,z] points). Returns "
        "minimum distance, and for parallel holes/planes the axis-to-axis / plane-to-plane distance.", {
            "a": {"description": "Feature ID, object name or [x,y,z]"},
            "b": {"description": "Feature ID, object name or [x,y,z]"},
        }, ["a", "b"]),
    _fn("find_edges", "Edges with IDs (E#): filter by an adjacent feature ID, convexity (convex / concave / "
        "tangent), kind (line / circle / arc) or nearness to a point.", {
            "feature": {"type": "string"}, "convexity": {"type": "string", "enum": ["convex", "concave", "tangent"]},
            "kind": {"type": "string"}, "near": VEC3, "object": {"type": "string"}, "limit": {"type": "integer"}}),
    _fn("measure_wall_thickness", "Minimum wall thickness of each part (ray casting), where it is and between which "
        "faces; with threshold, the faces thinner than it.", {"object": {"type": "string"}, "threshold": LEN}),
    _fn("set_thread", "Record a thread on a hole, e.g. M8x1.25 12 deep (an annotation: the hole is not re-cut). "
        "size 'none' removes it.", {"hole": FEATURE_ID, "size": {"type": "string"}, "depth": LEN}, ["hole", "size"]),
    _fn("list_locks", "List the locks."),
    _fn("lock_dimension", "Lock a dimension ID, feature, PAT# or B# so no edit may change it; mode 'centered' "
        "keeps a feature centred in its part.", {
            "target": {"type": "string"}, "mode": {"type": "string", "enum": ["value", "centered"]},
            "axis": {"type": "string", "enum": ["X", "Y", "Z"]}}, ["target"]),
    _fn("unlock_dimension", "Remove the lock(s) on a target ('all' = every lock).", {
        "target": {"type": "string"}}, ["target"]),
    _fn("set_datums", "Set datum faces A/B/C to planar face IDs ('default' resets one).", {"A": {"type": "string"}, "B": {"type": "string"}, "C": {"type": "string"}}),

    # ── B-rep edits ──────────────────────────────────────────────────────────
    _fn("resize_hole", "Change a hole's diameter, keeping its axis, depth and countersink.", {
        "hole": FEATURE_ID, "diameter": LEN},
        ["hole", "diameter"]),
    _fn("set_hole_standard", "Make a hole a standard size: ISO 273 clearance, tap drill, ISO 4762 counterbore or "
        "ISO 10642 countersink.", {
            "hole": FEATURE_ID, "size": {"type": "string", "description": "e.g. M6 or M8x1"},
            "type": {"type": "string", "enum": ["clearance", "tap", "counterbore", "countersink"]},
            "fit": {"type": "string", "enum": ["fine", "normal", "coarse"]}}, ["hole", "size", "type"]),
    _fn("move_hole", "Move a hole sideways (perpendicular to its axis). Give offset, to, or to_datum.", {
        "hole": FEATURE_ID, "offset": {**VEC3, "description": "[dx,dy,dz] in mm"},
        "to": {**VEC3, "description": "Absolute point the hole axis should pass through"},
        "to_datum": DATUM_POS}, ["hole"]),
    _fn("add_holes", "Drill one or more holes into a planar face (P#), or into the floor of a pocket / blind slot "
        "(face = its PK# / SL#), perpendicular to it.", {
        "face": FEATURE_ID, "diameter": LEN,
        "positions": {"type": "array",
                      "items": {"anyOf": [{"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3},
                                          DATUM_POS]},
                      "description": "Absolute [x,y,z] points on the face (copy all three numbers the user "
                                     "gives) or datum distances {B: 15, C: 20}; omit for the face centre"},
        "depth": {**LEN, "description": "Blind depth; omit for through"}},
        ["face", "diameter"]),
    _fn("edit_slot", "Change a slot (SL#): width, length, depth (blind only), or move it (offset / to / to_datum).", {
        "slot": FEATURE_ID, "width": LEN, "length": LEN, "depth": LEN, "offset": VEC3, "to": VEC3,
        "to_datum": DATUM_POS}, ["slot"]),
    _fn("edit_pocket", "Change a pocket (PK#): depth (the floor moves) or corner_radius (0 = sharp).", {
        "pocket": FEATURE_ID, "depth": LEN, "corner_radius": LEN}, ["pocket"]),
    _fn("edit_chamfer", "Re-make a chamfer (CH#) with a new size, or turn it into a fillet of radius to_fillet.", {
        "chamfer": FEATURE_ID, "size": LEN, "to_fillet": LEN}, ["chamfer"]),
    _fn("remove_features", "Remove features (fill holes, slots, pockets; delete bosses; remove fillets, chamfers) "
        "by defeaturing.", {
        "features": {"type": "array", "items": {"type": "string"}}}, ["features"]),
    _fn("push_pull_face", "Move a planar face along its outward normal: positive adds material (longer/thicker), "
        "negative removes material.", {"face": FEATURE_ID, "distance": LEN}, ["face", "distance"]),
    _fn("set_face_distance", "Move planar 'face' so its distance to the parallel 'reference_face' is exactly "
        "'distance' (e.g. set overall length/width/thickness). 'face' moves; the reference stays.", {
            "face": FEATURE_ID, "reference_face": FEATURE_ID, "distance": LEN},
        ["face", "reference_face", "distance"]),
    _fn("set_dimension", "PREFERRED way to change a size, thickness, gap, hole position, angle or radius: sets a "
        "dimension ID from the model data (or two parallel faces) to an exact value; H1◎H2 = 0 makes holes coaxial. "
        "anchor: 'start' = first ID stays, 'end' = second stays, 'symmetric' = both move half. Omit anchor unless "
        "the user says which side stays (locks then choose it).", {
            "dimension": {"type": "string", "description": "e.g. 'P5-P6', 'H3-P5', 'P3∠P7', 'C1.Ø'"},
            "value": {**LEN, "description": "mm; degrees for angles"},
            "anchor": {"type": "string", "enum": ["start", "end", "symmetric"]}},
        ["dimension", "value"]),
    _fn("edit_hole_pattern", "Change a hole pattern (PAT# from model data): circular → pcd (bolt-circle "
        "diameter), count, start_angle (deg); linear → spacing, count; grid → spacing_u/v, count_u/v. Holes keep "
        "their size, depth, countersinks and counterbores. anchor: 'center' (default, pattern stays centred) or "
        "'first' (first hole stays, linear only).", {
            "pattern": {"type": "string", "description": "Pattern ID, e.g. PAT1"},
            "pcd": LEN, "count": {"type": "integer"}, "start_angle": {"type": ["number", "string"]},
            "spacing": LEN, "spacing_u": LEN, "spacing_v": LEN, "count_u": {"type": "integer"},
            "count_v": {"type": "integer"},
            "anchor": {"type": "string", "enum": ["center", "first"]}},
        ["pattern"]),
    _fn("fillet_edges", "Round edges with a radius.", {
        "radius": LEN, "edges": {"type": "array", "items": EDGE_SELECTOR}, "object": OBJECT},
        ["radius", "edges"]),
    _fn("chamfer_edges", "Chamfer edges with an equal-distance chamfer. 'edges' uses the same selector format "
        "as fillet_edges.", {
        "size": LEN, "edges": {"type": "array", "items": EDGE_SELECTOR_BRIEF}, "object": OBJECT},
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

    # ── Plans, questions, more tools ───────────────────────────────────────
    _fn("propose_plan", "For a request needing several edits (or when the user wants a preview): list the edit tool "
        "calls in order; they are dry-run together, shown to the user as a numbered plan with a 3D preview, and applied "
        "as one version only when the user approves (a failing step rolls back the whole plan). Then stop.", {
            "summary": {"type": "string"},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "tool": {"type": "string"}, "args": {"type": "object"}}, "required": ["tool", "args"]}}},
        ["summary", "steps"]),
    _fn("ask_user", "When a reference matches several features, ask which one: the user gets clickable choices "
        "(highlighted in the 3D view). Then stop.", {
            "question": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "label": {"type": "string"}}, "required": ["id", "label"]}}},
        ["question", "choices"]),
    _fn("load_tools", "Load more tools. modeling: mirror_object, mirror_features, pattern_feature, add_slot, add_pocket, "
        "add_counterbore, add_countersink, draft_faces, split_object, shell_object. analysis: set_material, "
        "mass_properties, dfm_check, bill_of_materials, interference_check, make_drawing, cost_estimate. more: locks, "
        "datums, threads, primitives, transforms, push/pull, wall thickness (when not listed).", {
            "group": {"type": "string", "enum": ["modeling", "analysis", "more"]}}, ["group"]),

    # ── Modeling group (load_tools) ─────────────────────────────────────────
    _fn("mirror_object", "Mirror a whole part about a plane: copy (new part), replace, or fuse (symmetric part; the "
        "plane must touch the part). plane: 'y=20' style (the plane where that coordinate is constant), YZ / XZ / XY (YZ = x const, XZ = y const, XY = z "
        "const; through the part centre or at 'at'), or a face ID.", {
            "object": OBJECT, "plane": {"type": "string"}, "at": LEN,
            "mode": {"type": "string", "enum": ["copy", "replace", "fuse"]}}, ["plane"]),
    _fn("mirror_features", "Mirror holes, slots, pockets or bosses about a plane: 'y=20' style (coordinate constant on the plane), "
        "YZ / XZ / XY with 'at' (YZ = x const, XZ = y const, XY = z const), or a face ID.", {
        "features": {"type": "array", "items": {"type": "string"}}, "plane": {"type": "string"}, "at": LEN},
        ["features", "plane"]),
    _fn("pattern_feature", "Copy a hole, slot, pocket or boss: linear (count incl. the original, spacing along "
        "direction) or circular (count over angle° about axis through center).", {
            "feature": FEATURE_ID, "type": {"type": "string", "enum": ["linear", "circular"]},
            "count": {"type": "integer"}, "spacing": LEN, "direction": {"type": "string"},
            "axis": {"type": "string"}, "center": VEC3, "angle": {"type": "number"}}, ["feature", "type", "count"]),
    _fn("add_slot", "Cut a new slot into a planar face (through when depth is omitted). center: [x, y] on the face or "
        "datum distances; direction: axis (X / Y …) of its length.", {
            "face": FEATURE_ID, "width": LEN, "length": LEN, "depth": LEN, "center": {}, "direction": {"type": "string"},
            "shape": {"type": "string", "enum": ["obround", "rectangular"]}}, ["face", "width", "length"]),
    _fn("add_pocket", "Cut a new rectangular pocket into a planar face, with optional corner radius.", {
        "face": FEATURE_ID, "length": LEN, "width": LEN, "depth": LEN, "center": {}, "direction": {"type": "string"},
        "corner_radius": LEN}, ["face", "length", "width", "depth"]),
    _fn("add_counterbore", "Add a counterbore to a hole's entry.", {"hole": FEATURE_ID, "diameter": LEN, "depth": LEN},
        ["hole", "diameter", "depth"]),
    _fn("add_countersink", "Add a countersink to a hole's entry.", {"hole": FEATURE_ID, "diameter": LEN,
                                                                     "angle": {"type": "number"}}, ["hole", "diameter"]),
    _fn("draft_faces", "Tilt faces by a draft angle about their edge with the neutral face (positive = the part narrows "
        "away from it).", {"faces": {"type": "array", "items": {"type": "string"}}, "angle": {"type": "number"},
                           "neutral": FEATURE_ID}, ["faces", "angle", "neutral"]),
    _fn("split_object", "Split a part with a plane (YZ / XZ / XY with 'at', or a face ID): keep both halves or one side.", {
        "object": OBJECT, "plane": {"type": "string"}, "at": LEN,
        "keep": {"type": "string", "enum": ["both", "positive", "negative"]}}, ["plane"]),
    _fn("shell_object", "Hollow a part to a wall thickness; open_faces are left open (e.g. the top face).", {
        "thickness": LEN, "object": OBJECT, "open_faces": {"type": "array", "items": {"type": "string"}}}, ["thickness"]),

    # ── Analysis group (load_tools) ─────────────────────────────────────────
    _fn("set_material", "Assign a material to a part (B#) or to 'all' parts: steel, stainless 304, aluminium 6061, "
        "7075, brass, copper, titanium, cast iron, ABS, PLA, PETG, nylon, POM, polycarbonate, oak.", {
            "part": {"type": "string"}, "material": {"type": "string"}}, ["part", "material"]),
    _fn("mass_properties", "Mass, centre of mass and inertia per part (needs materials), plus volume and area.", {
        "part": {"type": "string"}}),
    _fn("dfm_check", "Design-for-manufacturing check: cnc (hole depth, internal corners, standard holes, thin walls, "
        "deep slots), print3d (overhangs, small features, walls vs nozzle) or sheet (hole to edge / bend). Findings "
        "come with fixes the user can apply.", {
            "process": {"type": "string", "enum": ["cnc", "print3d", "sheet"]}, "part": {"type": "string"},
            "params": {"type": "object"}}, ["process"]),
    _fn("bill_of_materials", "Bill of materials: identical parts grouped with quantity, material and mass."),
    _fn("interference_check", "Parts that overlap (volume) or are closer than 'clearance' mm.", {"clearance": LEN}),
    _fn("make_drawing", "Make a 2D drawing (front / top / left / iso, dimensions, hole callouts) as SVG and PDF.", {
        "part": {"type": "string"}}),
    _fn("cost_estimate", "Rough per-part cost (cnc or print3d) from volume, stock, material and features.", {
        "process": {"type": "string", "enum": ["cnc", "print3d"]}, "quantity": {"type": "integer"},
        "part": {"type": "string"}}, ["process"]),

    # ── Escape hatch ────────────────────────────────────────────────────────
    _fn("run_freecad_python", "LAST RESORT when no other tool can do it. Runs Python in FreeCAD with `doc`, `App`, "
        "`Part`, `V` (Vector) available; runs in an undoable transaction; print() output is returned.", {
            "code": {"type": "string"}, "description": {"type": "string"}}, ["code", "description"]),
]

EDIT_TOOLS = {
    "resize_hole", "move_hole", "add_holes", "remove_features", "push_pull_face", "set_face_distance",
    "set_dimension", "edit_hole_pattern", "fillet_edges", "chamfer_edges", "add_primitive", "transform_object", "scale_object", "set_property",
    "set_sketch_constraint", "set_spreadsheet_cell", "delete_object", "run_freecad_python", "set_hole_standard",
    "edit_slot", "edit_pocket", "edit_chamfer", "mirror_object", "mirror_features", "pattern_feature", "add_slot",
    "add_pocket", "add_counterbore", "add_countersink", "draft_faces", "split_object", "shell_object",
}
GROUPS = {
    "modeling": ["mirror_object", "mirror_features", "pattern_feature", "add_slot", "add_pocket", "add_counterbore",
                 "add_countersink", "draft_faces", "split_object", "shell_object"],
    "analysis": ["set_material", "mass_properties", "dfm_check", "bill_of_materials", "interference_check",
                 "make_drawing", "cost_estimate"],
}
# With a group loaded, these general tools step aside (token budget) and come back with load_tools "more".
GROUPS["more"] = ["set_datums", "lock_dimension", "unlock_dimension", "list_locks", "set_thread", "add_primitive",
                  "transform_object", "scale_object", "set_face_distance", "push_pull_face", "measure_wall_thickness",
                  "list_objects"]
GROUPED = {t for g in ("modeling", "analysis") for t in GROUPS[g]}
ANALYSIS_TOOLS = set(GROUPS["analysis"])
CONTROL_TOOLS = {"propose_plan", "ask_user", "load_tools"}
_GROUP_WORDS = {
    "modeling": ("mirror", "symmetr", "pattern", "array", "copies", "copy the", "add a slot", "add slot", "new slot",
                 "add a pocket", "add pocket", "new pocket", "counterbore", "countersink", "cbore", "csk", "draft",
                 "taper", "split", "cut in half", "shell", "hollow"),
    "analysis": ("mass", "weight", "weigh", "inertia", "centre of mass", "center of mass", "center of gravity", "material",
                 "steel", "alumin", "brass", "titanium", "plastic", "dfm", "manufactur", "machin", "cnc", "print",
                 "overhang", "sheet metal", "bom", "bill of material", "parts list", "interfer", "clash", "collid",
                 "drawing", "blueprint", "techdraw", "pdf", "cost", "price", "quote", "estimate"),
}


def groups_for_message(text):
    """Tool groups a message obviously needs (saves the model a load_tools round trip)."""
    t = (text or "").lower()
    return [g for g, words in _GROUP_WORDS.items() if any(w in t for w in words)]
SESSION_TOOLS = {"list_locks", "lock_dimension", "unlock_dimension", "set_datums", "set_thread"}
OP_NAME = {"run_freecad_python": "run_python", "set_hole_standard": "set_hole_profile"}
PARAMETRIC_TOOLS = {"set_property", "set_sketch_constraint", "set_spreadsheet_cell"}

# Arguments that are lengths (mm) or angles (deg): accepted as numbers or strings with a unit.
LENGTH_ARGS = {
    "resize_hole": ("diameter",), "add_holes": ("diameter", "depth"), "push_pull_face": ("distance",),
    "set_face_distance": ("distance",), "edit_hole_pattern": ("pcd", "spacing", "spacing_u", "spacing_v"),
    "fillet_edges": ("radius",), "chamfer_edges": ("size",), "edit_slot": ("width", "length", "depth"),
    "edit_pocket": ("depth", "corner_radius"), "edit_chamfer": ("size", "to_fillet"),
    "measure_wall_thickness": ("threshold",), "set_thread": ("depth",), "mirror_object": ("at",),
    "mirror_features": ("at",), "pattern_feature": ("spacing",), "add_slot": ("width", "length", "depth"),
    "add_pocket": ("length", "width", "depth", "corner_radius"), "add_counterbore": ("diameter", "depth"),
    "add_countersink": ("diameter",), "split_object": ("at",), "shell_object": ("thickness",),
    "interference_check": ("clearance",),
}
ANGLE_ARGS = {"edit_hole_pattern": ("start_angle",)}
VECTOR_ARGS = {"move_hole": ("offset", "to"), "transform_object": ("translate",), "add_primitive": ("position",),
               "edit_slot": ("offset", "to"), "pattern_feature": ("center",)}


def tools_for(meta, groups=()):
    """Tool definitions to send for this model: parametric tools only when it has parameters, grouped tools only when
    their group is loaded (saves tokens)."""
    info = (meta or {}).get("info") or {}
    skip = set() if ALLOW_PYTHON else {"run_freecad_python"}
    loaded = {t for g in groups for t in GROUPS.get(g, [])}
    skip |= GROUPED - loaded
    if set(groups) & {"modeling", "analysis"}:
        skip |= set(GROUPS["more"]) - loaded
    if not (info.get("parametric") or info.get("sketches") or info.get("spreadsheets")):
        skip |= PARAMETRIC_TOOLS
    feats = info.get("features") or {}
    for kind, tool in (("slot", "edit_slot"), ("pocket", "edit_pocket"), ("chamfer", "edit_chamfer"), ("hole", "set_thread")):
        if not feats.get(kind):
            skip.add(tool)  # the model has nothing this tool edits
    return [t for t in TOOLS if t["function"]["name"] not in skip]


def _feature_ids(info):
    return {f["id"] for fs in (info or {}).get("features", {}).values() for f in fs}


def is_angle_dimension(info, dimension):
    """True when a dimension ID is an angle: written with ∠, or two non-parallel planar faces."""
    did = str(dimension)
    if "∠" in did or "<" in did:
        return True
    refs = [x.strip().upper() for x in re.split(r"[-◎,\s]+", did) if x.strip()]
    if len(refs) != 2 or "." in did:
        return False
    planes = {f["id"]: f for f in ((info or {}).get("features") or {}).get("plane", [])}
    a, b = planes.get(refs[0]), planes.get(refs[1])
    if not a or not b:
        return False
    dot = sum(x * y for x, y in zip(a["normal"], b["normal"]))
    return abs(dot) < 1 - 1e-6


def coerce_args(name, args, info=None, default_unit="mm"):
    """Convert length / angle arguments given with units to mm / degrees (in place). Numbers are taken as mm
    (degrees for angles); strings without a unit use default_unit. Raises units.UnitError."""
    for key in LENGTH_ARGS.get(name, ()):
        if args.get(key) is not None and args[key] != "through":
            args[key] = units.parse_length(args[key], default_unit) if isinstance(args[key], str) else units.to_mm(args[key])
    for key in ANGLE_ARGS.get(name, ()):
        if args.get(key) is not None:
            args[key] = units.to_deg(args[key])
    for key in VECTOR_ARGS.get(name, ()):
        if isinstance(args.get(key), (list, tuple)):
            args[key] = [units.parse_length(x, default_unit) if isinstance(x, str) else float(x) for x in args[key]]
    if name == "set_dimension" and isinstance(args.get("value"), str):
        value, kind = units.parse_value(args["value"], default_unit)
        angle = is_angle_dimension(info, args.get("dimension", ""))
        if angle and kind == "length":
            raise units.UnitError(f"{args.get('dimension')} is an angle; give degrees, not '{args['value']}'")
        if not angle and kind == "angle":
            raise units.UnitError(f"{args.get('dimension')} is a distance; give a length, not '{args['value']}'")
        args["value"] = value * (units.MM_PER[default_unit] if kind == "number" and not angle else 1.0)
    if name in ("move_hole", "edit_slot") and isinstance(args.get("to_datum"), dict):
        args["to_datum"] = {k.upper(): (units.parse_length(v, default_unit) if isinstance(v, str) else float(v))
                            for k, v in args["to_datum"].items()}
    if name == "add_holes" and isinstance(args.get("positions"), list):
        args["positions"] = [
            {k.upper(): (units.parse_length(v, default_unit) if isinstance(v, str) else float(v)) for k, v in p.items()}
            if isinstance(p, dict) else
            [units.parse_length(x, default_unit) if isinstance(x, str) else float(x) for x in p]
            for p in args["positions"]]
    if name == "add_primitive" and isinstance(args.get("dimensions"), dict):
        args["dimensions"] = {k: (units.parse_length(v, default_unit) if isinstance(v, str) else v)
                              for k, v in args["dimensions"].items()}
    return args


def hole_standard_params(meta, args):
    """Engine set_hole_profile parameters for a set_hole_standard request."""
    kind = args.get("type") or "clearance"
    try:
        std = standards.resolve(args.get("size"), kind, args.get("fit") or "normal")
    except ValueError as e:
        raise cad.CadError(str(e))
    params = {"hole": args["hole"], "diameter": std["diameter"], "note": std["name"]}
    for k in ("counterbore_diameter", "counterbore_depth", "countersink_diameter", "countersink_angle"):
        if k in std:
            params[k] = std[k]
    if "countersink_diameter" in std:
        params["remove_counterbore"] = True
    if "counterbore_diameter" in std:
        params["remove_countersink"] = True
    return params


async def _session_tool(meta, name, args):
    if name == "list_locks":
        locks = meta.get("locks") or []
        return {"ok": True, "locks": [lk["label"] for lk in locks],
                "message": ("Locked: " + "; ".join(lk["label"] for lk in locks)) if locks else "No locks."}
    if name == "lock_dimension":
        lk = await cad.add_lock(meta, args.get("target"), args.get("mode") or "value", args.get("axis"))
        return {"ok": True, "message": f"Locked {lk['label']}", "lock": lk["label"]}
    if name == "unlock_dimension":
        removed = cad.remove_lock(meta, args.get("target"), None)
        return {"ok": True, "message": "Unlocked " + ", ".join(lk["label"] for lk in removed)}
    if name == "set_thread":
        th = cad.set_thread(meta, args.get("hole"), args.get("size"), args.get("depth"))
        if th is None:
            return {"ok": True, "message": f"Removed the thread on {str(args.get('hole')).upper()}"}
        return {"ok": True, "message": f"{th['hole']}: thread {th['spec']}"
                + (f", {th['depth']:g} mm deep" if th.get("depth") else "") + (f". Note: {th['note']}" if th.get("note") else "")}
    if name == "set_datums":
        datums = await cad.set_datums(meta, {k: v for k, v in args.items() if k.upper() in ("A", "B", "C")})
        return {"ok": True, "datums": {k: v["id"] for k, v in datums.items()},
                "message": "Datums: " + ", ".join(f"{k} = {v['id']} ({v['label']})" for k, v in datums.items())}
    return {"ok": False, "error": f"Unknown tool '{name}'"}


def _edit_summary(meta, res, info_before):
    """Verified values with display text and the side-effect table for one applied edit."""
    settings = precision.settings_of(meta)
    verified = [precision.describe_verification(v, settings) for v in res["verified"]]
    se = precision.side_effects(info_before, res["info"], targets=precision.target_keys(res["verified"]),
                                touched=res.get("touched") or [], tolerance=settings["tolerance"])
    return verified, se, settings


async def execute(meta, name, args, default_unit="mm", preview=False, force=False):
    """Run one tool call. Returns (result_for_llm, record_for_ui).

    default_unit: unit of strings without one (the session display unit for fast-path commands).
    preview: for an edit, a dry run (result measured, mesh written to preview.stl, model unchanged).
    force: skip the pre-edit checks' refusals."""
    args = dict(args or {})
    record = {"tool": name, "args": dict(args), "ok": False}
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
        if name == "list_dimensions":
            res = await cad.query(meta, "dimensions", **args)
            return {"ok": True, **res}, None
        if name == "find_edges":
            res = await cad.query(meta, "find_edges", **args)
            cad.save(meta)  # new edge IDs live in the registry
            return {"ok": True, **res}, None
        if name == "measure_wall_thickness":
            coerce_args(name, args, meta.get("info"), default_unit)
            res = await cad.query(meta, "wall_thickness", **args)
            st = precision.settings_of(meta)
            fmt = lambda v: units.fmt_length(v, st["display_unit"], st["precision"])  # noqa: E731
            lines = [f"{p['part']} {p['label']}: thinnest wall {fmt(p['min_thickness'])} between "
                     f"{' and '.join(x for x in p['faces'] if x)}" + (f"; {len(p['thin'])} face(s) under the threshold"
                                                                      if p.get("thin") else "")
                     for p in res.get("parts", [])]
            return {"ok": True, "message": "; ".join(lines) or "No solid parts", **res}, None
        if name in SESSION_TOOLS:
            return await _session_tool(meta, name, args), None
        if name in ANALYSIS_TOOLS:
            from .. import analysis_service
            coerce_args(name, args, meta.get("info"), default_unit)
            return await analysis_service.run_tool(meta, name, args), None
        if name == "run_freecad_python" and not ALLOW_PYTHON:
            return {"ok": False, "error": "run_freecad_python is disabled on this server (AGENT_ALLOW_PYTHON=0)."}, None
        if name not in EDIT_TOOLS:
            return {"ok": False, "error": f"Unknown tool '{name}'"}, None

        if name == "scale_object":
            if args.get("factors") is not None:
                args["factor"] = args.pop("factors")
            if args.get("factor") is None:
                raise cad.CadError("scale_object needs 'factor' or 'factors'")
        coerce_args(name, args, meta.get("info"), default_unit)
        params = hole_standard_params(meta, args) if name == "set_hole_standard" else args
        info_before = meta["info"]
        before = _feature_ids(info_before)
        res = await cad.apply_op(meta, OP_NAME.get(name, name), params, preview=preview, force=force)
        after = _feature_ids(res["info"])
        verified, se, settings = _edit_summary(meta, res, info_before)
        record.update(ok=True, message=res["message"], verified=verified,
                      volume_before=res["volume_before"], volume_after=res["volume_after"],
                      new_features=sorted(after - before), removed_features=sorted(before - after),
                      side_effects=se, unit=settings["display_unit"])
        if res.get("lock_resolution"):
            record["lock_resolution"] = res["lock_resolution"]
        if res.get("warnings"):
            record["warnings"] = res["warnings"]
        if preview:
            record["preview"] = True
        bbox = (res["info"] or {}).get("bbox")
        out = {"ok": True, "message": res["message"],
               "verified": [{k: v[k] for k in ("id", "field", "display", "within_tolerance", "status", "note")
                             if k in v} for v in verified],
               "new_feature_ids": record["new_features"], "removed_feature_ids": record["removed_features"],
               "volume_before": res["volume_before"], "volume_after": res["volume_after"],
               "model_bbox_size": bbox and bbox["size"]}
        brief = precision.side_effects_brief(se, settings)
        if brief:
            out["side_effects"] = brief
        if res.get("warnings"):
            out["warnings"] = res["warnings"]
        return out, record
    except units.UnitError as e:
        record["error"] = str(e)
        return {"ok": False, "error": str(e)}, (record if name in EDIT_TOOLS else None)
    except (fc.FreeCADError, cad.CadError) as e:
        record["error"] = str(e)
        return {"ok": False, "error": str(e)}, (record if name in EDIT_TOOLS else None)
