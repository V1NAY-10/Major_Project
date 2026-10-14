"""System prompt and compact model summary for the CAD agent."""

import json
import os

from .. import precision

LIMITS = {"object": 40, "hole": 60, "boss": 30, "round": 30, "plane": 40, "cone": 20, "sphere": 20, "face": 20,
          "dimension": 60, "pattern": 20, "slot": 20, "pocket": 20, "chamfer": 20}
LIMITS_FULL = {k: v * 3 for k, v in LIMITS.items()}
# Character budgets for the JSON summary (~3.5 chars/token). Small LLM rate limits (e.g. Groq free
# tier: 8k tokens/minute) need the system prompt summary to stay well under that.
SUMMARY_CHARS = int(os.getenv("MODEL_SUMMARY_CHARS", "4500"))
SUMMARY_CHARS_FULL = int(os.getenv("MODEL_SUMMARY_CHARS_FULL", "9000"))

_KEEP = {
    "hole": ("id", "diameter", "depth", "through", "drill_tip", "entry", "direction", "position", "countersinks",
             "counterbore", "counterbore_of", "pattern", "thread", "object"),
    "boss": ("id", "diameter", "length", "free_end", "base_end", "position", "object"),
    "round": ("id", "radius", "concave", "center", "axis", "chain_faces", "toroidal", "object"),
    "plane": ("id", "label", "normal", "center", "area", "object"),
    "cone": ("id", "half_angle_deg", "apex", "axis", "center", "object"),
    "sphere": ("id", "radius", "center", "object"),
    "face": ("id", "surface", "area", "center", "object"),
    "slot": ("id", "shape", "width", "length", "depth", "through", "center", "direction", "face", "object"),
    "pocket": ("id", "length", "width", "depth", "corner_radius", "center", "face", "object"),
    "chamfer": ("id", "size", "distances", "angle_deg", "faces", "length", "object"),
}


_DIM_ORDER = {"extent": 0, "thickness": 1, "gap": 2, "edge_distance": 3, "center_distance": 4, "angle": 5,
              "diameter": 6, "radius": 7, "coaxial": 8}
_PATTERN_KEEP = ("id", "type", "count", "hole_diameter", "pcd", "angle_step_deg", "start_angle_deg", "center",
                 "spacing", "direction", "count_u", "count_v", "spacing_u", "spacing_v", "holes", "object")


def _round(v):
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, list):
        return [_round(x) for x in v]
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    return v


def _bbox(b):
    return {"min": b["min"], "max": b["max"]} if isinstance(b, dict) and "min" in b else b


def summarize_model(info, full=False):
    """Trim engine model info to what the LLM needs, shrinking list lengths until it fits the char budget."""
    if not info:
        return None
    budget = SUMMARY_CHARS_FULL if full else SUMMARY_CHARS
    limits = dict(LIMITS_FULL if full else LIMITS)
    out = None
    for _ in range(6):
        out = _round(_summarize(info, limits, full))
        if len(json.dumps(out, separators=(",", ":"))) <= budget:
            break
        limits = {k: max(3, v // 2) for k, v in limits.items()}
    return out


def _summarize(info, limits, full):
    multi = len(info.get("objects", [])) > 1
    feats, truncated = {}, {}
    for kind, items in (info.get("features") or {}).items():
        keep = _KEEP.get(kind, ("id",))
        if kind == "plane" and full:
            keep = keep + ("bbox",)
        if kind == "hole" and full:
            keep = keep + ("standard", "datum_position")
        if kind == "plane":
            items = sorted(items, key=lambda f: ("outermost" not in f.get("label", ""), -f.get("area", 0)))
        rows = [{k: f[k] for k in keep if k in f and (multi or k != "object")} for f in items[: limits.get(kind, 30)]]
        if kind == "plane":
            rows = [r for r, f in zip(rows, items) if not f.get("is_cap")]
        feats[kind] = rows
        if len(items) > limits.get(kind, 30):
            truncated[kind] = len(items) - limits.get(kind, 30)
    hole_groups, hole_std = {}, {}
    for h in (info.get("features") or {}).get("hole", []):
        if h.get("counterbore_of"):
            continue  # a counterbore is part of the hole it belongs to
        key = (h["diameter"], h["through"], None if h["through"] else h["depth"])
        hole_groups.setdefault(key, []).append(h["id"])
        if h.get("standard"):
            hole_std.setdefault(key, h["standard"])
    # Largest parts first, so truncation drops small hardware rather than the main bodies.
    objects = sorted(info.get("objects", []), key=lambda o: -(o.get("volume") or 0))
    if len(objects) > limits["object"]:
        truncated["object"] = len(objects) - limits["object"]
    out = {
        "units": "mm",
        "objects": [{**({"id": o["id"]} if o.get("id") else {}), "name": o["name"], **({"label": o["label"]} if o.get("label") != o["name"] else {}),
                     **({"parent": o["parent"]} if o.get("parent") else {}),
                     "type": o["type"], "volume": o.get("volume"), "bbox": _bbox(o.get("bbox")),
                     **({"solids": o["solids"]} if o.get("solids", 1) > 1 else {})}
                    for o in objects[: limits["object"]]],
        "overall_bbox": info.get("bbox"),
        "feature_counts": info.get("counts"),
        "hole_groups": [{"diameter": d, "through": t, **({} if t else {"depth": dep}), "count": len(ids), "ids": ids,
                         **({"std": hole_std[(d, t, dep)]} if (d, t, dep) in hole_std else {})}
                        for (d, t, dep), ids in sorted(hole_groups.items())],
        "features": feats,
    }
    dims = sorted(info.get("dimensions") or [], key=lambda d: _DIM_ORDER.get(d.get("type"), 9))
    if info.get("datums"):
        out["datums"] = {k: v["id"] for k, v in info["datums"].items()}
    if dims:
        out["dimensions"] = [{"id": d["id"], "type": d["type"], "value": d["value"], "label": d.get("label"),
                              **({"object": d["object"]} if multi and d.get("object") else {})}
                             for d in dims[: limits["dimension"]]]
        if len(dims) > limits["dimension"]:
            truncated["dimension"] = len(dims) - limits["dimension"]
    pats = info.get("patterns") or []
    if pats:
        out["patterns"] = [{k: p[k] for k in _PATTERN_KEEP if k in p and (multi or k != "object")}
                           for p in pats[: limits["pattern"]]]
        if len(pats) > limits["pattern"]:
            truncated["pattern"] = len(pats) - limits["pattern"]
    if truncated:
        out["truncated_counts"] = truncated
        out["note"] = "Lists are truncated; use list_objects / find_features / list_dimensions to see the rest."
    if info.get("parametric"):
        out["parametric_objects"] = info["parametric"][:60]
    if info.get("sketches"):
        out["sketches"] = [{**s, "constraints": s["constraints"][:40]} for s in info["sketches"][:30]]
    if info.get("spreadsheets"):
        out["spreadsheets"] = [{"name": s["name"], "cells": [c for c in s["cells"] if c.get("alias")][:80]}
                               for s in info["spreadsheets"]]
    return out


SYSTEM_PROMPT = """You are AetherCAD, a precise CAD engineering assistant connected to a live FreeCAD document.
The user uploaded a CAD model; you answer questions about it and edit it on request through tools.

COORDINATES & UNITS
- Millimetres and degrees. Global axes: X = left(-)/right(+), Y = front(-)/back(+), Z = bottom(-)/top(+).
- Features have stable IDs: H# holes, C# bosses/cylinders, R# fillets/rounded faces, P# planar faces,
  K# cones, S# spheres, F# free-form faces. Planar faces carry a 'label' such as "top face (+Z, outermost)".
- Every solid object (part / body) has a stable part ID B# ('id' in objects); any 'object' parameter accepts it.
- 'dimensions' are the part's driving dimensions, each with an ID made of the two features that define it:
  "P5-P6" (overall X size), "P1-P2" (thickness), "H3-P5" (hole axis to a face). 'patterns' (PAT#) are hole
  patterns: circular (pcd = bolt-circle diameter), grid or linear (spacing). A hole with 'counterbore' includes
  its counterbore (a separate H# marked 'counterbore_of').

- Also "H1-H2" hole to hole, "P3∠P7" angle in degrees (included: 90 = square corner), "C1.Ø" boss diameter,
  "R2.R" fillet radius, "H1◎H2" coaxiality. 'datums' A/B/C are reference faces (holes carry datum_position
  in get_model_info / find_features); 'std' names standard hole sizes.
- SL# slots, PK# pockets, CH# chamfers are single features (edit_slot / edit_pocket / edit_chamfer,
  remove_features). E# are edge IDs (find_edges; fillet/chamfer selector {edge: "E12"}). A round with
  chain_faces is a whole tangent fillet chain.

- Face labels (top/front/…) use these GLOBAL axes. Imported models are often Y-up (SolidWorks STEP):
  for furniture/assemblies decide what is "top" from part labels and sizes, not only from label words.
- Assemblies: each part is a separate object with a label (e.g. "TABLE-TOP") and a parent assembly.
  Find parts with list_objects (by name or size); pass 'object' to edit tools when several exist.

SELECTION
- A user message may end with "[Selected in the viewer: …]": the components the user clicked, with their data.
  Apply the request to them unless the message clearly names something else; "this", "it", "the selected
  hole/face/part" mean them. Don't ask which feature when the selection answers it. Several selected
  components + "these"/"them" = one tool call per component.

ANSWERING QUESTIONS
- Use only the model data and the tools (list_objects, find_features, measure, get_model_info).
  Never invent a dimension, and never ask the user for a value you can look up or measure: query it.
- Quote feature IDs and exact values (e.g. "H3: Ø6.000 mm through hole at entry [10, 50, 10]").

EDITING
- Call edit tools with exact absolute numbers. Convert relative requests yourself from current values
  ("increase by 2 mm", "double", "10% longer").
- To change a size, length, width, height, thickness, gap or a hole's position from an edge, use
  set_dimension with the dimension ID from 'dimensions' (e.g. "make it 120 mm long" → the X 'extent'
  dimension = 120). If no listed dimension fits, pass any two parallel face IDs, or a hole and a face.
  Use anchor "symmetric" when the user wants the part to stay centred, otherwise the default.
- Holes that belong to a pattern: change the bolt circle, spacing or number of holes with
  edit_hole_pattern on the PAT# (not by moving holes one by one). Resizing pattern holes is still
  resize_hole per hole.
- A request covering several features ("all 6 mm holes") means one tool call per feature ID.
- Replacing a whole part's shape (e.g. "make the rectangular top round") = add_primitive with
  operation "replace" on that object, sized and positioned from its bbox (keep its thickness and centre).
- If a reference is ambiguous (several plausible features and the user didn't say "all"), do NOT edit:
  call ask_user with the candidate IDs as choices (label: size and location).
- A request needing 2+ different edits, or "preview …": call propose_plan with all steps (the user approves a
  preview; nothing changes before). Mirror / pattern / add slot or pocket / counterbore / draft / split / shell:
  load_tools "modeling"; materials, mass, DFM, BOM, interference, drawings, cost: load_tools "analysis".
- If the request is physically impossible or would break the part (e.g. a hole larger than the wall),
  say so instead of editing.
- When the model is parametric (parametric_objects / sketches present) prefer set_property,
  set_sketch_constraint or set_spreadsheet_cell, so design intent is kept. Use B-rep tools only when no
  parameter controls the feature. Properties with an "expressions" entry are driven by formulas: edit the source.
- Lengths: mm numbers, or strings with a unit ("1/4 in", "12 cm") converted exactly by the backend. A bare
  number in the user's message is in the SESSION display unit.
- "15 from datum B, 20 from C" → move_hole to_datum / add_holes positions [{"B":15,"C":20}].
- "M8 clearance", "tap for M6", "M6 counterbore/countersink" → set_hole_standard; "thread H3 M8x1.25" →
  set_thread (annotation only). "Is any wall too thin?" → measure_wall_thickness.
- "keep / don't change X" → lock_dimension. A refused edit names the blocking lock: tell the user, don't work
  around it. Edits outside the tolerance are rolled back automatically.
- Use run_freecad_python only when no other tool can do the job.
- If a tool returns ok=false, read the error, correct the call and retry (at most twice), or explain.

REPLY STYLE
- After edits, reply in 1-4 short lines: what changed, quoting the "display" text of the tool results'
  "verified" entries (e.g. "✓ H2 diameter = 8.000 mm (Δ 0.0000 mm)"). Only call a value verified when the tool
  result contains it. If 'side_effects' lists unexpected changes, mention them in one short line. No code, no JSON.
- Keep answers concise and technical; use markdown lists for multiple items.
"""

NO_MODEL_PROMPT = """You are AetherCAD, a CAD engineering assistant. No CAD model is loaded in this chat yet.
Answer general CAD/FreeCAD questions briefly. If the user wants to inspect or edit a part, ask them to
attach a STEP, IGES, BREP or FreeCAD (.FCStd) file with the paperclip button."""


def _brief(x, keys):
    return ", ".join(f"{k} {_round(x[k])}" for k in keys if x.get(k) not in (None, "", [], {}))


def describe_selection(info, ids):
    """One line per selected ID with its key data, for the LLM."""
    info = info or {}
    feats = {f["id"]: f for fs in (info.get("features") or {}).values() for f in fs}
    parts = {o.get("id"): o for o in info.get("objects", []) if o.get("id")}
    pats = {p["id"]: p for p in info.get("patterns") or []}
    dims = {d["id"]: d for d in info.get("dimensions") or []}
    label = {o["name"]: f"{o.get('id', '')} '{o['label']}'".strip() for o in info.get("objects", [])}
    lines = []
    for i in ids:
        if i in parts:
            o = parts[i]
            lines.append(f"{i}: part '{o['label']}' (object {o['name']}), " + _brief(o, ("volume",))
                         + f", bbox {_round(_bbox(o.get('bbox')))}")
        elif i in feats:
            f = feats[i]
            keys = _KEEP.get(f["kind"], ("id",))
            lines.append(f"{i}: {f['kind']} — " + _brief(f, [k for k in keys if k not in ("id", "object")])
                         + (f", in part {label.get(f.get('object'), f.get('object'))}" if len(label) > 1 else ""))
        elif i in pats:
            lines.append(f"{i}: hole pattern — " + _brief(pats[i], [k for k in _PATTERN_KEEP if k not in ("id", "object")]))
        elif i in dims:
            d = dims[i]
            lines.append(f"{i}: dimension '{d.get('label')}' = {d['value']}")
        else:
            lines.append(f"{i}: (not in the current model data)")
    return "\n".join(lines)


def with_selection(message, info, ids):
    if not ids:
        return message
    return f"{message}\n\n[Selected in the viewer:\n{describe_selection(info, ids)}]"


def session_line(meta):
    """Display unit, tolerance and locks of the session, in one short line for the system prompt."""
    s = precision.settings_of(meta)
    locks = [lk.get("label") or lk["target"] for lk in (meta or {}).get("locks") or []]
    return (f"SESSION: display unit {s['display_unit']}, tolerance ±{s['tolerance']['length']:g} mm / "
            f"±{s['tolerance']['angle']:g}°" + (f", locked: {'; '.join(locks[:20])}" if locks else ", no locks"))


def build_system(meta):
    if meta is None:
        return NO_MODEL_PROMPT
    model = summarize_model(meta["info"])
    return (SYSTEM_PROMPT + "\n" + session_line(meta) + f"\nCURRENT MODEL ({meta['filename']}, version {meta['head']}):\n"
            + json.dumps(model, separators=(",", ":")))
