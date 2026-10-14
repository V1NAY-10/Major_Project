"""
Units and number parsing shared by the fast path and the tool-argument layer.

Internal storage is always millimetres and degrees. Accepted input:
  lengths   120, 120mm, 12.5 cm, 0.12 m, 1/4", 1/4 in, 3 1/2 in, 2.5 inch, 1 ft
  angles    30, 30°, 30 deg, 0.5 rad
  sizes     Ø8, ⌀8, dia 8, diameter 8, D8  → diameter;   R4, r4, radius 4 → radius

A bare number uses the caller's default unit (the session display unit in the fast path, mm for tool
arguments, where the LLM is told numbers are mm).
"""

import re

MM_PER = {
    "mm": 1.0, "cm": 10.0, "m": 1000.0, "um": 0.001, "µm": 0.001,
    "in": 25.4, "inch": 25.4, "inches": 25.4, '"': 25.4, "″": 25.4,
    "ft": 304.8, "foot": 304.8, "feet": 304.8, "'": 304.8, "′": 304.8,
}
DEG_PER = {"deg": 1.0, "degree": 1.0, "degrees": 1.0, "°": 1.0, "rad": 57.29577951308232,
           "radian": 57.29577951308232, "radians": 57.29577951308232}
DISPLAY_UNITS = ("mm", "in")

# Regex fragments (no capturing groups) so callers can embed them.
DECIMAL = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
NUMBER = rf"(?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|{DECIMAL})"  # mixed fraction, fraction or decimal
UNIT = (r"(?:mm|cm|µm|um|m(?![a-z])|inch(?:es)?|in(?![a-z])|\"|″|ft|feet|foot|'|′"
        r"|deg(?:rees?)?|°|rad(?:ians?)?)")
QUANTITY = rf"(?:{NUMBER}\s*{UNIT}?)"

_QTY_RE = re.compile(rf"^\s*(?P<num>{NUMBER})\s*(?P<unit>{UNIT})?\s*$", re.I)
_SIZE_RE = re.compile(rf"^\s*(?P<kind>Ø|⌀|ø|dia(?:meter)?\.?|D|R|r|rad(?:ius)?)\s*[=:]?\s*(?P<qty>{QUANTITY})\s*$")


class UnitError(ValueError):
    """The text is not a number with a known unit."""


def parse_number(text):
    """'3 1/2' → 3.5, '1/4' → 0.25, '-2.5e1' → -25.0."""
    s = str(text).strip()
    m = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", s)
    if m:
        den = int(m.group(3))
        if den == 0:
            raise UnitError(f"Division by zero in '{text}'")
        return int(m.group(1)) + int(m.group(2)) / den
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", s)
    if m:
        den = int(m.group(2))
        if den == 0:
            raise UnitError(f"Division by zero in '{text}'")
        return int(m.group(1)) / den
    if re.fullmatch(DECIMAL, s):
        return float(s)
    raise UnitError(f"'{text}' is not a number")


def _unit_key(unit):
    u = unit.strip()
    return u if u in ('"', "″", "'", "′", "°", "µm") else u.lower()


def unit_kind(unit):
    """'length', 'angle' or None for a unit string."""
    if not unit:
        return None
    k = _unit_key(unit)
    return "length" if k in MM_PER else "angle" if k in DEG_PER else None


def parse_quantity(text):
    """(number, unit or None) for '12.5 mm', '1/4"', '30°', '7'. Raises UnitError."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text), None
    m = _QTY_RE.match(str(text))
    if not m:
        raise UnitError(f"Cannot read '{text}' as a number with a unit (e.g. 12.5 mm, 1/4 in, 30°)")
    return parse_number(m.group("num")), (_unit_key(m.group("unit")) if m.group("unit") else None)


def parse_length(text, default_unit="mm"):
    """Length in mm. A bare number is in `default_unit`."""
    value, unit = parse_quantity(text)
    unit = unit or default_unit
    if unit not in MM_PER:
        raise UnitError(f"'{text}' is an angle, a length was expected")
    return value * MM_PER[unit]


def parse_angle(text):
    """Angle in degrees. A bare number is in degrees."""
    value, unit = parse_quantity(text)
    unit = unit or "deg"
    if unit not in DEG_PER:
        raise UnitError(f"'{text}' is a length, an angle was expected")
    return value * DEG_PER[unit]


def parse_value(text, default_unit="mm"):
    """(value, kind) where kind is 'length' (mm), 'angle' (deg) or 'number' (no unit, as given).

    Used where the meaning depends on the target (e.g. set_dimension: a distance or an angle)."""
    value, unit = parse_quantity(text)
    if unit is None:
        return value, "number"
    if unit in DEG_PER:
        return value * DEG_PER[unit], "angle"
    return value * MM_PER[unit], "length"


def parse_size(text, default_unit="mm"):
    """('diameter' | 'radius', mm) for 'Ø8', 'dia 1/4"', 'R4', 'radius 3 mm'. Raises UnitError."""
    m = _SIZE_RE.match(str(text))
    if not m:
        raise UnitError(f"'{text}' is not a size like Ø8, dia 8 or R4")
    k = m.group("kind").lower().rstrip(".")
    kind = "radius" if k in ("r", "rad", "radius") else "diameter"
    return kind, parse_length(m.group("qty"), default_unit)


def to_mm(value, default_unit="mm"):
    """A tool argument: a number (already in default_unit) or a string with an optional unit → mm."""
    if isinstance(value, bool):
        raise UnitError("Expected a length, got a boolean")
    if isinstance(value, (int, float)):
        return float(value) * MM_PER[default_unit]
    return parse_length(value, default_unit)


def to_deg(value):
    if isinstance(value, bool):
        raise UnitError("Expected an angle, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    return parse_angle(value)


# ── Display ─────────────────────────────────────────────────────────────────

def fmt_number(x, precision=3):
    s = f"{float(x):.{int(precision)}f}"
    return s.lstrip("-") if float(s) == 0 else s  # no "-0.000"


def _length_digits(unit, precision):
    # inch gets one more decimal: 0.0001 in ≈ 0.0025 mm
    return int(precision) + (1 if unit == "in" else 0)


def fmt_length(mm, unit="mm", precision=3):
    """'120.000 mm' or '4.7244 in'."""
    factor = 25.4 if unit == "in" else 1.0
    return f"{fmt_number(float(mm) / factor, _length_digits(unit, precision))} {unit}"


def fmt_angle(deg, precision=3):
    return f"{fmt_number(deg, max(int(precision) - 1, 1))}°"


def fmt_value(value, kind="length", unit="mm", precision=3):
    """Format a length (mm), an angle (deg), a vector of lengths, or any other value for display."""
    if isinstance(value, (list, tuple)) and kind == "length":
        factor = 25.4 if unit == "in" else 1.0
        return "[" + ", ".join(fmt_number(float(v) / factor, _length_digits(unit, precision)) for v in value) + f"] {unit}"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if kind == "angle":
        return fmt_angle(value, precision)
    if kind == "length":
        return fmt_length(value, unit, precision)
    return fmt_number(value, precision) if isinstance(value, float) else str(value)
