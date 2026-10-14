"""
Precision bookkeeping around an edit (pure Python, no FreeCAD):

  * side_effects   every dimension / feature parameter / pattern value that changed other than the edit's
                   target, from the model info before and after the edit. Changes involving a feature the
                   edit touched are `expected`; others are flagged as unexpected (amber in the UI).
  * describe_verification  display strings for a verified value in the session's unit and precision.
"""

import re

from . import units

DEFAULT_SETTINGS = {"display_unit": "mm", "precision": 3, "tolerance": {"length": 0.001, "angle": 0.01},
                    "min_wall": 1.0}  # mm; edits that make a wall thinner get a warning (pre-edit check)
MAX_ROWS = 100

_FEATURE_FIELDS = {
    "hole": ("diameter", "depth"),
    "boss": ("diameter", "length"),
    "round": ("radius",),
    "sphere": ("radius",),
    "cone": ("half_angle_deg",),
    "slot": ("width", "length", "depth"),
    "pocket": ("width", "length", "depth", "corner_radius"),
    "chamfer": ("size", "angle_deg"),
}
_PATTERN_FIELDS = ("pcd", "count", "angle_step_deg", "start_angle_deg", "spacing", "spacing_u", "spacing_v")
_ANGLE_FIELDS = {"half_angle_deg", "angle_step_deg", "start_angle_deg", "countersink_angle", "angle_deg"}
_REF_SPLIT = re.compile(r"[-∠◎]")


def settings_of(meta):
    s = dict(DEFAULT_SETTINGS)
    s.update({k: v for k, v in ((meta or {}).get("settings") or {}).items() if k in s})
    tol = dict(DEFAULT_SETTINGS["tolerance"])
    tol.update(((meta or {}).get("settings") or {}).get("tolerance") or {})
    s["tolerance"] = tol
    return s


def dim_refs(did):
    """Feature IDs a dimension / value key refers to: 'P5-P6' → [P5, P6], 'C1.Ø' → [C1]."""
    base = str(did).split(".")[0].split(" ")[0]
    return [x for x in _REF_SPLIT.split(base) if x]


def snapshot(info):
    """{key: {value, label, refs, kind}} of every comparable value in a model info."""
    out = {}
    info = info or {}
    for d in info.get("dimensions") or []:
        out[d["id"]] = {"value": d["value"], "label": d.get("label", ""), "refs": d.get("refs") or dim_refs(d["id"]),
                        "kind": "angle" if d.get("unit") == "deg" else "length"}
    for kind, fields in _FEATURE_FIELDS.items():
        for f in (info.get("features") or {}).get(kind, []) or []:
            for field in fields:
                if f.get(field) is not None:
                    out[f"{f['id']} {field}"] = {"value": f[field], "label": f"{kind} {field}", "refs": [f["id"]],
                                                 "kind": "angle" if field in _ANGLE_FIELDS else "length"}
            if kind == "hole":
                cb = f.get("counterbore")
                if cb:
                    for k in ("diameter", "depth"):
                        out[f"{f['id']} counterbore_{k}"] = {"value": cb[k], "label": f"counterbore {k}",
                                                             "refs": [f["id"]], "kind": "length"}
                for cs in (f.get("countersinks") or [])[:1]:
                    out[f"{f['id']} countersink_diameter"] = {"value": cs["outer_diameter"], "label": "countersink diameter",
                                                              "refs": [f["id"]], "kind": "length"}
                    out[f"{f['id']} countersink_angle"] = {"value": cs["angle_deg"], "label": "countersink angle",
                                                           "refs": [f["id"]], "kind": "angle"}
    for p in info.get("patterns") or []:
        for field in _PATTERN_FIELDS:
            if p.get(field) is not None:
                out[f"{p['id']} {field}"] = {"value": p[field], "label": f"{p['type']} pattern {field}",
                                             "refs": [p["id"]] + list(p.get("holes") or []),
                                             "kind": "angle" if field in _ANGLE_FIELDS else "count" if field == "count" else "length"}
    return out


def _tol(kind, tolerance):
    return 0.5 if kind == "count" else float(tolerance.get("angle" if kind == "angle" else "length", 0.001))


def side_effects(before_info, after_info, targets=(), touched=(), tolerance=None):
    """Changed values other than the targets.

    targets: IDs / keys the edit was asked to change (verified values) — excluded.
    touched: feature IDs the edit moved or rebuilt — changes involving them are `expected`.
    Returns {"rows": [{key, label, before, after, delta, kind, expected}], "total", "unexpected",
             "added", "removed"}; rows are capped at MAX_ROWS, unexpected first.
    """
    tolerance = tolerance or DEFAULT_SETTINGS["tolerance"]
    a, b = snapshot(before_info), snapshot(after_info)
    targets = {str(t).upper() for t in targets}
    touched = {str(t).upper() for t in touched}

    def is_target(key):
        return key.upper() in targets

    rows = []
    for key, old in a.items():
        new = b.get(key)
        if is_target(key):
            continue
        if new is None:
            rows.append({"key": key, "label": old["label"], "before": old["value"], "after": None, "delta": None,
                         "kind": old["kind"], "expected": bool(set(old["refs"]) & touched)})
            continue
        try:
            delta = float(new["value"]) - float(old["value"])
        except (TypeError, ValueError):
            continue
        if abs(delta) > _tol(old["kind"], tolerance):
            rows.append({"key": key, "label": old["label"], "before": old["value"], "after": new["value"],
                         "delta": round(delta, 6), "kind": old["kind"],
                         "expected": bool(set(old["refs"]) & touched)})
    added = [k for k in b if k not in a and not is_target(k)]
    rows.sort(key=lambda r: (r["expected"], r["after"] is None, r["key"]))
    unexpected = sum(1 for r in rows if not r["expected"])
    return {"rows": rows[:MAX_ROWS], "total": len(rows), "unexpected": unexpected,
            "added": len(added), "removed": sum(1 for r in rows if r["after"] is None)}


def target_keys(verified):
    """Snapshot keys an edit was asked to change, from its verified entries: dimension IDs, 'H3 diameter', …"""
    keys = set()
    for v in verified or []:
        vid, field = str(v.get("id", "")), str(v.get("field", ""))
        if field == "dimension":
            keys.add(vid)
            m = re.fullmatch(r"([A-Z]+\d+)\.(Ø|R)", vid.upper())
            if m:  # a radial dimension is also the feature's own diameter / radius
                keys.add(f"{m.group(1)} {'diameter' if m.group(2) == 'Ø' else 'radius'}")
        elif field == "pattern":
            keys.update(f"{vid} {k}" for k in _PATTERN_FIELDS)
        else:
            keys.add(f"{vid} {field}")
    return keys


def side_effects_brief(se, settings, limit=6):
    """Compact form for the LLM (token budget): counts + the first few unexpected changes as text."""
    if not se or not se["total"]:
        return None
    out = {"changed": se["total"], "unexpected": se["unexpected"]}
    lines = [_row_text(r, settings) for r in se["rows"] if not r["expected"]][:limit]
    if lines:
        out["unexpected_changes"] = lines
    return out


def _fmt(v, kind, settings):
    if v is None:
        return "—"
    if kind == "count":
        return str(int(v)) if float(v).is_integer() else str(v)
    return units.fmt_value(float(v), "angle" if kind == "angle" else "length", settings["display_unit"],
                           settings["precision"])


def _row_text(r, settings):
    if r["after"] is None:
        return f"{r['key']} ({r['label']}) removed (was {_fmt(r['before'], r['kind'], settings)})"
    return f"{r['key']} {_fmt(r['before'], r['kind'], settings)} → {_fmt(r['after'], r['kind'], settings)}"


def describe_verification(v, settings):
    """Add a `display` line to a verified entry: '✓ P5-P6 = 120.000 mm (Δ 0.000)' or '⚠ … outside ±0.001'."""
    if v.get("status") != "ok":
        return v
    kind = "angle" if v.get("unit") == "deg" else "length" if v.get("unit") == "mm" else "number"
    unit, prec = settings["display_unit"], settings["precision"]
    val = v.get("achieved", v.get("value"))
    if isinstance(val, dict):
        shown = ", ".join(f"{k} {_fmt(x, 'count' if k == 'count' else ('angle' if k in _ANGLE_FIELDS else 'length'), settings)}"
                          for k, x in val.items())
    elif isinstance(val, (int, float)) and not isinstance(val, bool) or isinstance(val, list):
        shown = units.fmt_value(val, kind, unit, prec)
    else:
        shown = str(val)
    text = f"{v['id']}{' ' + v['field'] if v.get('field') not in ('dimension',) else ''} = {shown}"
    if v.get("deviation") is not None:
        tol = settings["tolerance"]["angle" if kind == "angle" else "length"]
        dev = units.fmt_value(float(v["deviation"]), "angle" if kind == "angle" else "length", unit, max(prec, 4) if unit == "mm" else prec + 1)
        mark = "✓" if v.get("within_tolerance") else "⚠"
        text = f"{mark} {text} (Δ {dev}" + ("" if v.get("within_tolerance") else f" > ±{tol:g}") + ")"
    else:
        text = f"✓ {text}"
    return {**v, "display": text}
