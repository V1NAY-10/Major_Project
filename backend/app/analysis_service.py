"""
Phase 4 analysis for a session: materials and mass, DFM, bill of materials, interference, 2D drawings and cost.

Functions taking `meta` expect the caller to hold the session lock (the agent turn or the route wrapper does).
"""

from . import cad_service as cad
from . import materials as mats
from . import precision, reports, units


async def mass(meta, part=None):
    res = await cad.query(meta, "mass_properties", densities=cad.densities(meta), **({"object": part} if part else {}))
    names = {pid: mats.MATERIALS[k][0] for pid, k in (meta.get("materials") or {}).items() if k in mats.MATERIALS}
    for r in res["parts"]:
        r["material"] = names.get(r["part"])
    return {"parts": res["parts"], "total": res.get("total")}


async def dfm(meta, process="cnc", part=None, params=None):
    res = await cad.query(meta, "dfm", process=process, params=params or {}, **({"object": part} if part else {}))
    return {k: res[k] for k in ("process", "params", "findings", "counts")}


async def bom(meta):
    groups = (await cad.query(meta, "bom_groups"))["groups"]
    m = await mass(meta)
    masses = {r["part"]: r["mass_g"] for r in m["parts"] if "mass_g" in r}
    names = {r["part"]: r.get("material") for r in m["parts"]}
    rows = reports.bom_rows(groups, names, masses)
    total = sum(r["total_mass_g"] or 0 for r in rows)
    return {"rows": rows, "parts": sum(r["quantity"] for r in rows), "total_mass_g": round(total, 3) if masses else None}


async def interference(meta, clearance=1.0):
    return {k: v for k, v in (await cad.query(meta, "interference", clearance=float(clearance))).items() if k != "registry"}


def drawing_paths(meta):
    d = cad._dir(meta["session_id"]) / "exports"
    stem = f"drawing_v{meta['head']}"
    return d / f"{stem}.svg", d / f"{stem}.pdf"


async def drawing(meta, part=None):
    svg, pdf = drawing_paths(meta)
    m = await mass(meta, part)
    row = m["parts"][0] if m["parts"] else {}
    res = await cad.query(meta, "drawing", svg_path=cad._p(svg), pdf_path=cad._p(pdf), material=row.get("material"),
                          mass_g=row.get("mass_g"), title=meta.get("filename"), **({"object": part} if part else {}))
    sid = meta["session_id"]
    return {"scale": res["scale"], "part": res["part"], "dimensions": res["dimensions"], "callouts": res["callouts"],
            "views": res["views"], "svg_url": f"/cad/drawing/{sid}?format=svg", "pdf_url": f"/cad/drawing/{sid}?format=pdf"}


async def cost(meta, process="cnc", quantity=1, part=None):
    info = meta.get("info") or {}
    objs = info.get("objects") or []
    if part:
        objs = [o for o in objs if o.get("id") == str(part).upper()]
        if not objs:
            raise cad.CadError(f"No part {part}")
    out = []
    for o in objs:
        key = (meta.get("materials") or {}).get(o.get("id"))
        if key is None:
            key = "pla" if process == "print3d" else "aluminium_6061"
            assumed = True
        else:
            assumed = False
        nfeat = sum(1 for fs in (info.get("features") or {}).values() for f in fs
                    if f.get("object") == o["name"] and f["kind"] in ("hole", "slot", "pocket", "chamfer", "round"))
        try:
            est = mats.cost_estimate(process, o["volume"], o["bbox"]["size"], key, nfeat, quantity)
        except ValueError as e:
            raise cad.CadError(str(e))
        out.append({"part": o.get("id"), "label": o["label"], **est,
                    **({"assumed_material": True} if assumed else {}), "features": nfeat})
    return {"parts": out, "total": round(sum(p["total"] for p in out), 2), "currency": "USD",
            "note": "Rough estimate (material, machine time and setup), not a quote."}


def _fmt_len(meta, v):
    st = precision.settings_of(meta)
    return units.fmt_length(v, st["display_unit"], st["precision"])


async def run_tool(meta, name, args):
    """Analysis tools for the agent: a compact result plus a one-line message."""
    try:
        if name == "set_material":
            m = cad.set_material(meta, args.get("part") or "all", args.get("material"))
            return {"ok": True, "message": "Materials: " + ", ".join(f"{k} {v}" for k, v in m.items()), "materials": m}
        if name == "mass_properties":
            r = await mass(meta, args.get("part"))
            lines = [f"{p['part']}: " + (f"{p['mass_g']:.2f} g ({p['material']})" if "mass_g" in p else
                                          f"volume {p['volume']:.1f} mm³ (no material set)")
                     + f", centre of mass {p['center_of_mass']}" for p in r["parts"]]
            if r.get("total"):
                lines.append(f"total {r['total']['mass_g']:.2f} g")
            return {"ok": True, "message": "; ".join(lines), **r}
        if name == "dfm_check":
            r = await dfm(meta, args.get("process") or "cnc", args.get("part"), args.get("params"))
            msg = (f"{r['counts']['error']} error(s), {r['counts']['warning']} warning(s): "
                   + "; ".join(f"[{f['severity']}] {f['message']}" for f in r["findings"][:8]))
            return {"ok": True, "message": msg, "findings": [{k: f[k] for k in ("severity", "rule", "message", "features")
                                                               if k in f} | ({"fix": f["fix"]["label"]} if f.get("fix") else {})
                                                              for f in r["findings"][:12]], "counts": r["counts"]}
        if name == "bill_of_materials":
            r = await bom(meta)
            return {"ok": True, "message": f"{len(r['rows'])} line(s), {r['parts']} part(s): " + "; ".join(
                f"{x['quantity']} × {x['description']} ({x['material']})" for x in r["rows"]), **r}
        if name == "interference_check":
            r = await interference(meta, args.get("clearance") or 1.0)
            if not r["pairs"]:
                return {"ok": True, "message": "No interference and no parts closer than the clearance", **r}
            return {"ok": True, "message": "; ".join(
                f"{p['parts'][0]}/{p['parts'][1]} {p['status']}" + (f" {p['overlap_volume']} mm³" if p["overlap_volume"] else
                                                                   f" {_fmt_len(meta, p['distance'])} apart") for p in r["pairs"]), **r}
        if name == "make_drawing":
            r = await drawing(meta, args.get("part"))
            return {"ok": True, "message": f"Drawing of {r['part']} at {r['scale']} (SVG and PDF ready to download): "
                    + ", ".join(r["callouts"]), **r}
        if name == "cost_estimate":
            r = await cost(meta, args.get("process") or "cnc", args.get("quantity") or 1, args.get("part"))
            return {"ok": True, "message": "; ".join(f"{p['part']}: about {p['per_part']} {p['currency']} each × {p['quantity']}"
                                                     + (" (assumed material)" if p.get("assumed_material") else "")
                                                     for p in r["parts"]) + ". " + r["note"], **r}
    except cad.CadError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"Unknown tool '{name}'"}
