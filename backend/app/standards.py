"""
Standard hole sizes: ISO 273 clearance holes, ISO tap drills (coarse and fine pitch), counterbores for
ISO 4762 socket head cap screws, countersinks for ISO 10642 countersunk screws, and standard drill sizes
(metric and fractional inch).

Used to (1) name recognised holes ("Ø6.6 = M6 clearance, normal") and (2) resolve requests such as
"make H3 an M8 clearance hole" or "tap H3 for M6" to exact diameters.
"""

import re
from fractions import Fraction

# ISO 273 clearance holes: size → (fine, normal (medium), coarse)
CLEARANCE = {
    1.6: (1.7, 1.8, 2.0), 2: (2.2, 2.4, 2.6), 2.5: (2.7, 2.9, 3.1), 3: (3.2, 3.4, 3.6), 4: (4.3, 4.5, 4.8),
    5: (5.3, 5.5, 5.8), 6: (6.4, 6.6, 7.0), 8: (8.4, 9.0, 10.0), 10: (10.5, 11.0, 12.0), 12: (13.0, 13.5, 14.5),
    14: (15.0, 15.5, 16.5), 16: (17.0, 17.5, 18.5), 18: (19.0, 20.0, 21.0), 20: (21.0, 22.0, 24.0),
    22: (23.0, 24.0, 26.0), 24: (25.0, 26.0, 28.0), 27: (28.0, 30.0, 32.0), 30: (31.0, 33.0, 35.0),
    33: (34.0, 36.0, 38.0), 36: (37.0, 39.0, 42.0),
}
FITS = ("fine", "normal", "coarse")
FIT_ALIASES = {"fine": "fine", "close": "fine", "normal": "normal", "medium": "normal", "standard": "normal",
               "coarse": "coarse", "loose": "coarse", "free": "coarse"}

# ISO 261 coarse pitches and the standard tap drill (ISO 2306 / DIN 336) for each.
COARSE = {  # size: (pitch, tap drill)
    2: (0.4, 1.6), 2.5: (0.45, 2.05), 3: (0.5, 2.5), 4: (0.7, 3.3), 5: (0.8, 4.2), 6: (1.0, 5.0),
    8: (1.25, 6.8), 10: (1.5, 8.5), 12: (1.75, 10.2), 14: (2.0, 12.0), 16: (2.0, 14.0), 18: (2.5, 15.5),
    20: (2.5, 17.5), 22: (2.5, 19.5), 24: (3.0, 21.0),
}
FINE = {  # (size, pitch): tap drill
    (2, 0.25): 1.75, (2.5, 0.35): 2.15, (3, 0.35): 2.65, (4, 0.5): 3.5, (5, 0.5): 4.5, (6, 0.75): 5.2,
    (8, 1.0): 7.0, (10, 1.0): 9.0, (10, 1.25): 8.8, (12, 1.25): 10.8, (12, 1.5): 10.5, (14, 1.5): 12.5,
    (16, 1.5): 14.5, (18, 1.5): 16.5, (20, 1.5): 18.5, (22, 1.5): 20.5, (24, 2.0): 22.0,
}

# Counterbores for ISO 4762 socket head cap screws: diameter per DIN 974-1 (row for ISO 4762),
# depth = head height k + clearance (typical design values).
COUNTERBORE = {
    2: (4.3, 2.4), 2.5: (5.0, 2.9), 3: (6.5, 3.4), 4: (8.0, 4.4), 5: (10.0, 5.4), 6: (11.0, 6.5),
    8: (15.0, 8.6), 10: (18.0, 10.8), 12: (20.0, 13.0), 14: (24.0, 15.0), 16: (26.0, 17.5),
    20: (33.0, 21.5), 24: (40.0, 25.5),
}

# Countersinks (90°) for ISO 10642 countersunk socket screws: theoretical head diameter dk so the head sits flush.
COUNTERSINK = {3: 6.72, 4: 8.96, 5: 11.2, 6: 13.44, 8: 17.92, 10: 22.4, 12: 26.88, 14: 30.8, 16: 33.6, 20: 40.32}
COUNTERSINK_ANGLE = 90.0


def _metric_drills():
    sizes = {round(0.5 + 0.1 * i, 2) for i in range(126)}            # 0.5 … 13.0 by 0.1
    sizes |= {round(13.0 + 0.5 * i, 2) for i in range(25)}           # 13 … 25 by 0.5
    sizes |= {float(x) for x in range(26, 51)}                       # 26 … 50 by 1
    sizes |= set(COARSE[k][1] for k in COARSE) | set(FINE.values())  # tap drills such as 2.05, 10.2
    return sorted(sizes)


METRIC_DRILLS = _metric_drills()
FRACTIONAL_DRILLS = [Fraction(i, 64) for i in range(1, 65)]  # 1/64" … 1"

MATCH_TOL = 0.0015  # mm: recognised values are exact to 1e-4, standards are given to 0.01


def _size_name(size):
    return f"M{size:g}"


def parse_thread(spec):
    """'M8' → (8.0, None); 'M8x1' / 'M8 x 1.0' → (8.0, 1.0). Raises ValueError."""
    m = re.fullmatch(r"\s*M\s*(\d+(?:\.\d+)?)\s*(?:[x×*]\s*(\d+(?:\.\d+)?))?\s*", str(spec), re.I)
    if not m:
        raise ValueError(f"'{spec}' is not a metric thread size like M8 or M8x1")
    return float(m.group(1)), (float(m.group(2)) if m.group(2) else None)


def _key(table, size):
    for k in table:
        if abs(float(k) - size) < 1e-9:
            return k
    return None


def clearance(size, fit="normal"):
    fit = FIT_ALIASES.get(str(fit or "normal").lower())
    if fit is None:
        raise ValueError("fit must be fine, normal or coarse")
    k = _key(CLEARANCE, size)
    if k is None:
        raise ValueError(f"No ISO 273 clearance hole for M{size:g}. Sizes: "
                         + ", ".join(_size_name(s) for s in CLEARANCE))
    return CLEARANCE[k][FITS.index(fit)], fit


def tap_drill(size, pitch=None):
    """(tap drill diameter, pitch, 'coarse'|'fine')."""
    k = _key(COARSE, size)
    if pitch is None or (k is not None and abs(COARSE[k][0] - pitch) < 1e-9):
        if k is None:
            raise ValueError(f"No coarse thread M{size:g}. Sizes: " + ", ".join(_size_name(s) for s in COARSE))
        return COARSE[k][1], COARSE[k][0], "coarse"
    for (s, p), d in FINE.items():
        if abs(s - size) < 1e-9 and abs(p - pitch) < 1e-9:
            return d, p, "fine"
    known = ", ".join(f"M{s:g}x{p:g}" for s, p in FINE if abs(s - size) < 1e-9)
    raise ValueError(f"No tap drill for M{size:g}x{pitch:g}. Fine pitches for this size: {known or 'none'}")


def resolve(spec, kind="clearance", fit="normal"):
    """Exact hole geometry for a standard request.

    kind: clearance | tap | counterbore | countersink.
    Returns {diameter, name, counterbore_diameter?, counterbore_depth?, countersink_diameter?, countersink_angle?}.
    """
    size, pitch = parse_thread(spec)
    kind = str(kind or "clearance").lower()
    kind = {"tapped": "tap", "thread": "tap", "threaded": "tap", "cbore": "counterbore", "csk": "countersink",
            "countersunk": "countersink", "counterbored": "counterbore"}.get(kind, kind)
    if kind == "clearance":
        d, fit = clearance(size, fit)
        return {"diameter": d, "name": f"{_size_name(size)} clearance ({fit}, ISO 273)"}
    if kind == "tap":
        d, p, series = tap_drill(size, pitch)
        return {"diameter": d, "name": f"{_size_name(size)}x{p:g} tap drill ({series})", "thread": f"M{size:g}x{p:g}"}
    if kind == "counterbore":
        k = _key(COUNTERBORE, size)
        if k is None:
            raise ValueError("No ISO 4762 counterbore for M%g. Sizes: %s" % (size, ", ".join(_size_name(s) for s in COUNTERBORE)))
        d, fit = clearance(size, fit)
        cd, depth = COUNTERBORE[k]
        return {"diameter": d, "counterbore_diameter": cd, "counterbore_depth": depth,
                "name": f"{_size_name(size)} socket head counterbore (ISO 4762)"}
    if kind == "countersink":
        k = _key(COUNTERSINK, size)
        if k is None:
            raise ValueError("No ISO 10642 countersink for M%g. Sizes: %s" % (size, ", ".join(_size_name(s) for s in COUNTERSINK)))
        d, fit = clearance(size, fit)
        return {"diameter": d, "countersink_diameter": COUNTERSINK[k], "countersink_angle": COUNTERSINK_ANGLE,
                "name": f"{_size_name(size)} countersunk screw (ISO 10642)"}
    raise ValueError("kind must be clearance, tap, counterbore or countersink")


def _close(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) <= MATCH_TOL


def _frac_name(fr):
    return f'{fr.numerator}/{fr.denominator}"' if fr.denominator != 1 else f'{fr.numerator}"'


def identify(hole):
    """Standard names that match a recognised hole (most specific first); [] if none."""
    d = hole.get("diameter")
    if d is None:
        return []
    out = []
    cb = hole.get("counterbore")
    if cb:
        for size, (cd, depth) in COUNTERBORE.items():
            if _close(cb.get("diameter"), cd) and any(_close(d, c) for c in CLEARANCE.get(size, ())):
                out.append(f"{_size_name(size)} socket head counterbore (ISO 4762)"
                           + ("" if _close(cb.get("depth"), depth) else f", depth {cb.get('depth'):g} (std {depth:g})"))
    for cs in hole.get("countersinks") or []:
        if abs(float(cs.get("angle_deg") or 0) - COUNTERSINK_ANGLE) > 0.05:
            continue
        for size, dk in COUNTERSINK.items():
            if _close(cs.get("outer_diameter"), dk):
                out.append(f"{_size_name(size)} countersunk screw (ISO 10642)")
    for size, row in CLEARANCE.items():
        for fit, c in zip(FITS, row):
            if _close(d, c):
                out.append(f"{_size_name(size)} clearance ({fit})")
    for size, (p, t) in COARSE.items():
        if _close(d, t):
            out.append(f"{_size_name(size)} tap drill")
    for (size, p), t in FINE.items():
        if _close(d, t):
            out.append(f"{_size_name(size)}x{p:g} tap drill (fine)")
    for fr in FRACTIONAL_DRILLS:
        if _close(d, float(fr) * 25.4):
            out.append(f"{_frac_name(fr)} drill")
    if not out and any(_close(d, x) for x in METRIC_DRILLS):
        out.append(f"Ø{d:g} standard drill")
    return out[:4]


def annotate(info):
    """Add `standard` (best match) and `standards` (all) to every hole in a model-info dict, in place."""
    for h in ((info or {}).get("features") or {}).get("hole", []) or []:
        names = identify(h)
        if names:
            h["standard"] = names[0]
            h["standards"] = names
        else:
            h.pop("standard", None)
            h.pop("standards", None)
    return info
