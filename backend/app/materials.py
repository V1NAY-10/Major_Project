"""
Material library, mass from volume, and rough manufacturing cost estimates (pure Python).

Densities are typical handbook values (g/cm³). Prices and machining rates are illustrative defaults for a rough
estimate only; the estimate says so wherever it is shown.
"""

# key: (name, density g/cm³, price per kg (USD), material removal rate cm³/min for CNC roughing, category)
MATERIALS = {
    "steel_1018": ("Steel, mild (1018)", 7.87, 2.5, 4.0, "metal"),
    "steel_4140": ("Steel, alloy (4140)", 7.85, 3.5, 3.0, "metal"),
    "stainless_304": ("Stainless steel 304", 8.00, 6.0, 2.0, "metal"),
    "stainless_316": ("Stainless steel 316", 8.00, 8.0, 1.8, "metal"),
    "aluminium_6061": ("Aluminium 6061-T6", 2.70, 6.0, 15.0, "metal"),
    "aluminium_7075": ("Aluminium 7075-T6", 2.81, 10.0, 12.0, "metal"),
    "brass_c360": ("Brass C360", 8.50, 9.0, 10.0, "metal"),
    "copper_c110": ("Copper C110", 8.96, 11.0, 6.0, "metal"),
    "titanium_ti6al4v": ("Titanium Ti-6Al-4V", 4.43, 35.0, 1.0, "metal"),
    "cast_iron": ("Cast iron (grey)", 7.20, 2.0, 5.0, "metal"),
    "abs": ("ABS", 1.04, 22.0, 20.0, "plastic"),
    "pla": ("PLA", 1.24, 20.0, 20.0, "plastic"),
    "petg": ("PETG", 1.27, 24.0, 20.0, "plastic"),
    "nylon_pa12": ("Nylon PA12", 1.01, 60.0, 18.0, "plastic"),
    "pom": ("POM (acetal)", 1.41, 12.0, 20.0, "plastic"),
    "polycarbonate": ("Polycarbonate", 1.20, 15.0, 18.0, "plastic"),
    "wood_oak": ("Oak", 0.75, 4.0, 40.0, "wood"),
}
ALIASES = {"steel": "steel_1018", "mild steel": "steel_1018", "stainless": "stainless_304", "ss304": "stainless_304",
           "aluminium": "aluminium_6061", "aluminum": "aluminium_6061", "al6061": "aluminium_6061", "6061": "aluminium_6061",
           "7075": "aluminium_7075", "brass": "brass_c360", "copper": "copper_c110", "titanium": "titanium_ti6al4v",
           "ti": "titanium_ti6al4v", "nylon": "nylon_pa12", "acetal": "pom", "delrin": "pom", "pc": "polycarbonate",
           "oak": "wood_oak", "wood": "wood_oak", "iron": "cast_iron"}


def resolve(name):
    """Material key for a name or alias ('aluminum', 'AL 6061', 'Stainless steel 304'). Raises ValueError."""
    q = str(name or "").strip().lower()
    if q in MATERIALS:
        return q
    if q in ALIASES:
        return ALIASES[q]
    flat = q.replace(" ", "").replace("-", "").replace("_", "")
    for k, (label, *_rest) in MATERIALS.items():
        if flat in (k.replace("_", ""), label.lower().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")):
            return k
    for alias, k in ALIASES.items():
        if alias.replace(" ", "") in flat:
            return k
    raise ValueError(f"Unknown material '{name}'. Known: " + ", ".join(v[0] for v in MATERIALS.values()))


def library():
    return [{"key": k, "name": v[0], "density": v[1], "price_per_kg": v[2], "category": v[4]} for k, v in MATERIALS.items()]


def density(key):
    return MATERIALS[key][1]


def mass_g(volume_mm3, key):
    return volume_mm3 * density(key) / 1000.0


# ── cost ────────────────────────────────────────────────────────────────────

MACHINE_RATE = 60.0       # USD / h, 3-axis CNC
SETUP_MIN = 30.0          # minutes per batch
FEATURE_MIN = 0.5         # minutes per drilled / milled feature
STOCK_ALLOWANCE = 2.0     # mm on every side
PRINT_RATE = 3.0          # USD / h, FDM printer
PRINT_CM3_PER_H = 12.0
PRINT_FILL = 0.45         # walls + 20 % infill, fraction of the solid volume printed
PRINT_SETUP = 5.0         # USD per batch


def cost_estimate(process, volume_mm3, bbox_size, material_key, features=0, quantity=1):
    """Rough per-part cost (USD) for 'cnc' or 'print3d'. Returns the breakdown; everything is an estimate."""
    name, rho, price, mrr, category = MATERIALS[material_key]
    q = max(1, int(quantity))
    if process == "cnc":
        stock = 1.0
        for s in bbox_size:
            stock *= s + 2 * STOCK_ALLOWANCE
        stock_kg = stock * rho / 1e6
        removed_cm3 = max(stock - volume_mm3, 0) / 1000.0
        cut_min = removed_cm3 / mrr + features * FEATURE_MIN
        per_part = stock_kg * price + (cut_min + SETUP_MIN / q) / 60.0 * MACHINE_RATE
        out = {"process": "cnc", "material": name, "stock_mm": [round(s + 2 * STOCK_ALLOWANCE, 1) for s in bbox_size],
               "material_cost": round(stock_kg * price, 2), "machining_minutes": round(cut_min, 1),
               "setup_minutes_per_part": round(SETUP_MIN / q, 1),
               "machine_cost": round((cut_min + SETUP_MIN / q) / 60.0 * MACHINE_RATE, 2)}
    elif process == "print3d":
        if category != "plastic":
            raise ValueError(f"{name} can't be FDM-printed; choose a plastic (PLA, PETG, ABS, nylon…)")
        printed_cm3 = volume_mm3 / 1000.0 * PRINT_FILL
        hours = printed_cm3 / PRINT_CM3_PER_H
        mat = printed_cm3 * rho / 1000.0 * price
        per_part = mat + hours * PRINT_RATE + PRINT_SETUP / q
        out = {"process": "print3d", "material": name, "material_cost": round(mat, 2), "print_hours": round(hours, 2),
               "machine_cost": round(hours * PRINT_RATE + PRINT_SETUP / q, 2)}
    else:
        raise ValueError("process must be cnc or print3d")
    out.update(per_part=round(per_part, 2), quantity=q, total=round(per_part * q, 2), currency="USD",
               note="Rough estimate from volume, stock size, material and feature count; not a quote.")
    return out
