"""
Deterministic commands that skip the LLM.

Only exact, unambiguous forms are accepted; anything else goes to the LLM. Numbers may carry units
(mm, cm, m, in, ", ft, fractions like 1/4" or 3 1/2 in, °/deg); a bare number is in the session's display unit.
  P5-P6 = 120            set a dimension (optional unit, optional "anchor symmetric|end|start")
  H3-P5 = 12 mm symmetric
  P3∠P7 = 60   P3<P7 = 60°   P3-P7 = 60°        angle between two faces (degrees)
  H2 Ø8   /   H2 diameter 8   /   H2 dia = 1/4"   /   H2 R4 (radius)
  C1 Ø30  /   C1.Ø = 30   /   R2 R3              boss diameter, fillet radius (radial dimensions)
  PAT1 pcd 80   /   PAT1 spacing 2 in   /   PAT1 count 8   /   PAT1 start_angle 30°
  H3 M8 clearance [fine|normal|coarse]   /   H3 M6 tap   /   tap H3 M8x1   /   H3 M6 counterbore | countersink
  lock P5-P6   /   lock H1 centred [X]   /   unlock P5-P6   /   unlock all   /   locks
  datum A = P1
  SL1 width 10   /   SL1 length 40   /   SL1 depth 6        slots
  PK1 depth 10   /   PK1 corner R3                         pockets
  CH1 3   /   CH1 size 1.5   /   CH1 to fillet R2           chamfers
  fillet E12 R2   /   chamfer E3 E4 1   /   E12 R2          edges
  H3 thread M8x1.25 depth 12   /   H3 thread none          thread annotations
  wall thickness   /   min wall                            thinnest wall of each part
  PAT1 count_u 3                                           grid hole counts
  material all steel   /   material B2 aluminium 6061      materials
  mass   /   dfm cnc | print | sheet   /   bom   /   interference   /   drawing   /   cost cnc 10
  preview <command>   /   apply   /   cancel              (handled by the runner: plans)

With one component selected in the viewer, the ID can be left out: "Ø8" with H2 selected, "= 120" or
"120" with P5-P6 selected, "pcd 80" with PAT1 selected, "M8 clearance" or "lock" with anything selected.
"""

import re

from .. import precision, units

_Q = rf"({units.QUANTITY})"
_ID = r"([A-Z]+\d+)"
_ANCHOR = r"(?:\s*(?:,|\()?\s*(?:anchor\s*[=:]?\s*)?(start|end|symmetric)\)?)?"

_DIM = re.compile(rf"^\s*(?:set\s+)?{_ID}\s*(-|∠|<|◎)\s*{_ID}\s*(?:=|to|:)\s*{_Q}{_ANCHOR}\s*$", re.I)
_RADIAL = re.compile(
    rf"^\s*(?:set\s+)?{_ID}\s*(?:\.\s*)?(Ø|⌀|ø|diameter|dia\.?|radius|rad|R)\s*(?:=|to|:)?\s*Ø?\s*{_Q}\s*$", re.I)
_PAT = re.compile(
    rf"^\s*(?:set\s+)?(PAT\d+)\s+(pcd|spacing|spacing_u|spacing_v|count|count_u|count_v|start_angle)\s*(?:=|to|:)?\s*{_Q}\s*$", re.I)
_SLOT = re.compile(rf"^\s*(?:set\s+)?(SL\d+)\s+(width|length|depth)\s*(?:=|to|:)?\s*{_Q}\s*$", re.I)
_POCKET = re.compile(rf"^\s*(?:set\s+)?(PK\d+)\s+(depth|corner(?:_radius|\s+radius)?\s*R?|R)\s*(?:=|to|:)?\s*{_Q}\s*$", re.I)
_CHAMFER = re.compile(rf"^\s*(?:set\s+)?(CH\d+)\s*(?:size\s*)?(?:=|to|:)?\s*{_Q}\s*$", re.I)
_CH_FILLET = re.compile(rf"^\s*(CH\d+)\s+(?:to\s+)?(?:a\s+)?(?:fillet\s*)?R\s*{_Q}\s*$", re.I)
_EDGE_OP = re.compile(rf"^\s*(fillet|round|chamfer)\s+((?:E\d+[\s,]*)+?)\s*(?:with\s+)?(?:R|size)?\s*{_Q}\s*$", re.I)
_EDGE_SHORT = re.compile(rf"^\s*((?:E\d+[\s,]*)+?)\s*(R|C|fillet|chamfer)\s*{_Q}\s*$", re.I)
_THREAD_NOTE = re.compile(rf"^\s*(?:thread\s+)?(H\d+)\s+(?:thread\s+)?(none|M\s*\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?)"
                     rf"(?:\s*,?\s*(?:depth\s*)?{_Q})?\s*$", re.I)
_MATERIAL = re.compile(r"^\s*(?:set\s+)?material\s+(all|B\d+)\s*(?:=|to|:)?\s*(.+?)\s*$", re.I)
_ANALYSIS = re.compile(r"^\s*(mass(?:\s+properties)?|weight|bom|bill of materials|interference|clash(?:es)?|drawing|"
                       r"dfm(?:\s+(cnc|print|print3d|3d|sheet))?|cost(?:\s+(cnc|print|print3d))?(?:\s+(?:x|qty|×)?\s*(\d+))?)\s*\??\s*$", re.I)
_WALL = re.compile(r"^\s*(?:(?:measure|check|show)\s+)?(?:the\s+)?(?:min(?:imum)?\s+)?wall(?:\s+thickness)?\s*\??\s*$", re.I)
_THREAD = r"(M\s*\d+(?:\.\d+)?(?:\s*[x×]\s*\d+(?:\.\d+)?)?)"
_KIND = r"(clearance|tap(?:ped)?|thread(?:ed)?|counterbore[d]?|cbore|countersink|countersunk|csk)"
_FIT = r"(?:\s*\(?\s*(fine|normal|medium|coarse|close|loose)\s*(?:fit)?\)?)?"
_STD = re.compile(rf"^\s*(?:make\s+)?{_ID}\s*(?:=|to|an?)?\s*{_THREAD}\s+{_KIND}(?:\s+hole)?{_FIT}\s*$", re.I)
_TAP = re.compile(rf"^\s*tap\s+{_ID}\s+(?:for\s+)?{_THREAD}\s*$", re.I)
_TARGET = r"((?:PAT|[A-Z])\d+(?:\s*[-∠<◎]\s*[A-Z]+\d+|\.\S+)?)"  # an ID or a dimension ID
_LOCK = re.compile(rf"^\s*lock\s+{_TARGET}(?:\s+(centred|centered|center|centre)(?:\s+(?:in|on|along)?\s*([XYZ]))?)?\s*$", re.I)
_UNLOCK = re.compile(rf"^\s*unlock\s+(all|{_TARGET[1:-1]})\s*$", re.I)
_LOCKS = re.compile(r"^\s*(?:list\s+|show\s+)?locks\s*$", re.I)
_DATUM = re.compile(r"^\s*(?:set\s+)?datum\s+([ABC])\s*(?:=|to|:|is)?\s*(P\d+|default)\s*$", re.I)

_ANY_ID = re.compile(r"\b(?:PAT|SL|PK|CH|[HCRPKSFBE])\d+\b", re.I)  # not M8 (a thread size)
_NO_ID_COMMANDS = re.compile(r"^\s*(?:(?:un)?lock(?:\s+(?:centred|centered|center|centre)(?:\s+(?:in|on|along)?\s*[XYZ])?)?)\s*$", re.I)


def parse(text, selection=None, unit="mm", angle_dim=None):
    """Return (tool_name, args) for an exact command, or None.

    unit: the display unit bare numbers are in. angle_dim(dimension_id) → True if it is an angle (bare numbers
    are then degrees); without it only IDs written with ∠ count as angles."""
    if not text or len(text) > 80:
        return None
    found = _parse(text, unit, angle_dim)
    if found is None and selection and len(selection) == 1 and str(selection[0]).upper().startswith("E"):
        found = _parse(f"{selection[0]} {text}", unit, angle_dim)  # an edge selected: "R2" is a radius, not R2
    if found is None and selection and len(selection) == 1 and not _ANY_ID.search(text):
        sel = selection[0]
        if _NO_ID_COMMANDS.match(text):
            words = text.strip().split(None, 1)
            found = _parse(f"{words[0]} {sel} {words[1] if len(words) > 1 else ''}", unit, angle_dim)
        else:
            found = _parse(f"{sel} {text}", unit, angle_dim)
            if found is None and re.match(rf"^\s*{units.NUMBER}", text):
                found = _parse(f"{sel} = {text}", unit, angle_dim)
    return found


def _length(q, unit):
    return units.parse_length(q, unit)


def _parse(text, unit="mm", angle_dim=None):
    try:
        return _parse_strict(text, unit, angle_dim)
    except (units.UnitError, ValueError):
        return None


def _parse_strict(text, unit, angle_dim):
    m = _LOCKS.match(text)
    if m:
        return "list_locks", {}
    m = _UNLOCK.match(text)
    if m:
        return "unlock_dimension", {"target": re.sub(r"\s+", "", m.group(1)).upper().replace("<", "∠")}
    m = _LOCK.match(text)
    if m:
        args = {"target": re.sub(r"\s+", "", m.group(1)).upper().replace("<", "∠")}
        if m.group(2):
            args["mode"] = "centered"
            if m.group(3):
                args["axis"] = m.group(3).upper()
        return "lock_dimension", args
    m = _DATUM.match(text)
    if m:
        return "set_datums", {m.group(1).upper(): m.group(2).upper() if m.group(2).lower() != "default" else "default"}
    if _WALL.match(text):
        return "measure_wall_thickness", {}
    m = _MATERIAL.match(text)
    if m:
        return "set_material", {"part": m.group(1).upper() if m.group(1).lower() != "all" else "all", "material": m.group(2)}
    m = _ANALYSIS.match(text)
    if m:
        word = m.group(1).lower()
        if word.startswith(("mass", "weight")):
            return "mass_properties", {}
        if word.startswith(("bom", "bill")):
            return "bill_of_materials", {}
        if word.startswith(("interference", "clash")):
            return "interference_check", {}
        if word.startswith("drawing"):
            return "make_drawing", {}
        if word.startswith("dfm"):
            proc = (m.group(2) or "cnc").lower()
            return "dfm_check", {"process": {"print": "print3d", "3d": "print3d"}.get(proc, proc)}
        proc = (m.group(3) or "cnc").lower()
        return "cost_estimate", {"process": "print3d" if proc.startswith("print") else "cnc", "quantity": int(m.group(4) or 1)}
    m = _THREAD_NOTE.match(text)
    if m and (m.group(2).lower() == "none" or "thread" in text.lower()):
        args = {"hole": m.group(1).upper(), "size": "none" if m.group(2).lower() == "none" else _thread(m.group(2))}
        if m.group(3):
            args["depth"] = _length(m.group(3), unit)
        return "set_thread", args
    m = _SLOT.match(text)
    if m:
        return "edit_slot", {"slot": m.group(1).upper(), m.group(2).lower(): _length(m.group(3), unit)}
    m = _POCKET.match(text)
    if m:
        key = "depth" if m.group(2).lower() == "depth" else "corner_radius"
        return "edit_pocket", {"pocket": m.group(1).upper(), key: _length(m.group(3), unit)}
    m = _CH_FILLET.match(text)
    if m:
        return "edit_chamfer", {"chamfer": m.group(1).upper(), "to_fillet": _length(m.group(2), unit)}
    m = _CHAMFER.match(text)
    if m:
        return "edit_chamfer", {"chamfer": m.group(1).upper(), "size": _length(m.group(2), unit)}
    m = _EDGE_OP.match(text) or _EDGE_SHORT.match(text)
    if m:
        op_word, ids = (m.group(1), m.group(2)) if m.re is _EDGE_OP else (m.group(2), m.group(1))
        edges = [{"edge": e.upper()} for e in re.findall(r"E\d+", ids, re.I)]
        val = _length(m.group(3), unit)
        if op_word.lower() in ("chamfer", "c"):
            return "chamfer_edges", {"size": val, "edges": edges}
        return "fillet_edges", {"radius": val, "edges": edges}
    m = _TAP.match(text)
    if m:
        return "set_hole_standard", {"hole": m.group(1).upper(), "size": _thread(m.group(2)), "type": "tap"}
    m = _STD.match(text)
    if m:
        kind = m.group(3).lower()
        kind = ("tap" if kind.startswith(("tap", "thread")) else "counterbore" if kind.startswith(("counterbore", "cbore"))
                else "countersink" if kind.startswith(("countersink", "countersunk", "csk")) else "clearance")
        args = {"hole": m.group(1).upper(), "size": _thread(m.group(2)), "type": kind}
        if m.group(4):
            args["fit"] = {"close": "fine", "medium": "normal", "loose": "coarse"}.get(m.group(4).lower(), m.group(4).lower())
        return "set_hole_standard", args
    m = _PAT.match(text)
    if m:
        key = m.group(2).lower()
        q = m.group(3)
        if key in ("count", "count_u", "count_v"):
            val = units.parse_number(q)
            if not float(val).is_integer():
                return None
            return "edit_hole_pattern", {"pattern": m.group(1).upper(), key: int(val)}
        val = units.parse_angle(q) if key == "start_angle" else _length(q, unit)
        return "edit_hole_pattern", {"pattern": m.group(1).upper(), key: val}
    m = _RADIAL.match(text)
    if m:
        fid, word, q = m.group(1).upper(), m.group(2).lower().rstrip("."), m.group(3)
        radius = word in ("r", "rad", "radius")
        val = _length(q, unit)
        if fid.startswith("H"):
            return "resize_hole", {"hole": fid, "diameter": 2 * val if radius else val}
        return "set_dimension", {"dimension": f"{fid}.{'R' if radius else 'Ø'}", "value": val}
    m = _DIM.match(text)
    if m:
        sep = {"<": "∠"}.get(m.group(2), m.group(2))
        did = f"{m.group(1).upper()}{sep}{m.group(3).upper()}"
        value, kind = units.parse_value(m.group(4), unit)
        angle = sep == "∠" or kind == "angle" or bool(angle_dim and angle_dim(did))
        if kind == "number":
            value = value if angle else value * units.MM_PER[unit]
        elif kind == "length" and angle:
            return None  # a length for an angle: let the LLM sort it out
        args = {"dimension": did, "value": value}
        if m.group(5):
            args["anchor"] = m.group(5).lower()
        return "set_dimension", args
    return None


def _thread(spec):
    return re.sub(r"\s+", "", spec).upper().replace("×", "X").replace("X", "x")


def reply(result, settings=None):
    """Short reply for a fast-path command, quoting verified values."""
    settings = settings or precision.DEFAULT_SETTINGS
    if not result.get("ok"):
        return f"Could not apply that: {result.get('error')}"
    lines = [str(result.get("message") or "Done").split("\n")[0]]
    lines += [x for x in str(result.get("message") or "").split("\n")[1:] if x.startswith("Lock:")]
    for v in result.get("verified") or []:
        if v.get("status") == "ok":
            if v.get("display"):
                lines.append(v["display"].replace("✓ ", "✓ verified ", 1))
            else:
                val = v.get("value")
                if isinstance(val, float):
                    val = f"{val:.3f}"
                elif isinstance(val, dict):
                    val = ", ".join(f"{k} {x}" for k, x in val.items())
                lines.append(f"✓ verified {v['id']} {v['field']} = {val}")
        else:
            lines.append(f"⚠ {v['id']}: {v.get('note', 'could not verify')}")
    se = result.get("side_effects")
    if se:
        lines.append(f"{se['changed']} other value(s) changed" + (f", {se['unexpected']} unexpected: "
                                                                  + "; ".join(se.get("unexpected_changes", []))
                                                                  if se.get("unexpected") else " (all on the edited features)"))
    return "\n".join(f"- {line}" for line in lines)
