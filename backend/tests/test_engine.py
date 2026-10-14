"""
Engine test suite — runs INSIDE headless FreeCAD:

    "C:/Program Files/FreeCAD 1.1/bin/freecadcmd.exe" backend/tests/test_engine.py

Builds parts with known ground truth (fixtures.py), uploads them through the same path as the app
(STEP export -> open_model), checks recognition, then applies edits through engine.apply and
verifies the resulting geometry independently (bounding boxes, analytic volume changes, re-analysis).
Exits non-zero if any check fails.
"""

import math
import os
import shutil
import sys
import tempfile
import time
import traceback

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import FreeCAD as App  # noqa: E402  # type: ignore

import fixtures as fx  # noqa: E402
from freecad_bridge import engine as E  # noqa: E402

TMP = tempfile.mkdtemp(prefix="aethercad_tests_")
RESULTS = {"pass": 0, "fail": 0, "failed": []}
_section = [""]


# ─────────────────────────────────────────────────────────────────────────────
# Tiny assert framework
# ─────────────────────────────────────────────────────────────────────────────

def section(name):
    _section[0] = name
    print(f"\n== {name}")


def check(name, cond, detail=""):
    label = f"{_section[0]}: {name}"
    if cond:
        RESULTS["pass"] += 1
        print(f"  PASS {name}")
    else:
        RESULTS["fail"] += 1
        RESULTS["failed"].append(label + (f" — {detail}" if detail else ""))
        print(f"  FAIL {name}" + (f"  [{detail}]" if detail else ""))
    return cond


def close(a, b, tol=1e-3):
    try:
        if isinstance(a, (list, tuple)):
            return len(a) == len(b) and all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))
        return abs(float(a) - float(b)) <= tol
    except Exception:
        return False


def check_close(name, got, want, tol=1e-3):
    return check(name, close(got, want, tol), f"got {got}, want {want}")


def guarded(fn):
    try:
        fn()
    except Exception as e:
        RESULTS["fail"] += 1
        RESULTS["failed"].append(f"{fn.__name__}: crashed {type(e).__name__}: {e}")
        print(f"  FAIL {fn.__name__} crashed: {type(e).__name__}: {e}")
        traceback.print_exc()


# ─────────────────────────────────────────────────────────────────────────────
# A session mimicking the backend (registry carried between calls)
# ─────────────────────────────────────────────────────────────────────────────

class Session:
    _n = 0

    def __init__(self, source_path):
        Session._n += 1
        self.sid = f"t{Session._n}_{os.path.splitext(os.path.basename(source_path))[0]}"[:60]
        self.work = os.path.join(TMP, self.sid, "work.FCStd")
        res = E.open_model(self.sid, source_path, self.work)
        self.info, self.registry = res["info"], res["registry"]

    def apply(self, op, tolerance=None, locks=None, datums=None, checks=None, **params):
        res = E.apply(self.sid, self.work, op, params, self.registry, tolerance=tolerance, locks=locks, datums=datums,
                      checks=checks)
        self.info, self.registry = res["info"], res["registry"]
        return res

    def call(self, method, **params):
        res = getattr(E, method)(self.sid, self.work, registry=self.registry, **params)
        self.registry = res.get("registry", self.registry)
        return res

    # Lookups
    def feats(self, kind):
        return self.info["features"].get(kind, [])

    def f(self, fid):
        for fs in self.info["features"].values():
            for f in fs:
                if f["id"] == fid:
                    return f
        return None

    def hole_at(self, x, y, z=None, d=None):
        """The hole whose axis passes nearest (x, y) (Z-axis holes)."""
        best = None
        for h in self.feats("hole"):
            if d is not None and abs(h["diameter"] - d) > 1e-3:
                continue
            dist = math.hypot(h["entry"][0] - x, h["entry"][1] - y)
            if best is None or dist < best[0]:
                best = (dist, h)
        return best[1] if best and best[0] < 1e-2 else None

    def plane(self, label_start):
        for p in self.feats("plane"):
            if p["label"].startswith(label_start):
                return p
        return None

    def dim(self, did):
        for d in self.info["dimensions"]:
            if d["id"] == did:
                return d
        return None

    def dims(self, type_=None, label=None):
        return [d for d in self.info["dimensions"] if (type_ is None or d["type"] == type_)
                and (label is None or label in d["label"])]

    def bbox(self):
        return self.info["bbox"]

    def volume(self):
        return sum(o["volume"] for o in self.info["objects"])

    def doc(self):
        return E._find_doc(self.sid, self.work)


def step(name, shape):
    return fx.write_step(shape, os.path.join(TMP, name + ".step"))


def ok_verified(res, field=None):
    vs = [v for v in res["verified"] if field is None or v["field"] == field or v["field"].startswith(field)]
    return bool(vs) and all(v["status"] == "ok" for v in vs), vs


# ─────────────────────────────────────────────────────────────────────────────
# Recognition
# ─────────────────────────────────────────────────────────────────────────────

def test_plate_recognition():
    section("plate recognition")
    s = Session(step("plate", fx.plate()))
    check_close("bbox size", s.bbox()["size"], [100, 60, 10])
    check("8 holes", len(s.feats("hole")) == 8, str(s.info["counts"]))
    for x, y in ((10, 10), (90, 10), (10, 50), (90, 50)):
        h = s.hole_at(x, y, d=6)
        check(f"Ø6 through at ({x},{y})", h is not None and h["through"] and close(h["depth"], 10)
              and close(h["entry"], [x, y, 10]), str(h and {k: h[k] for k in ("diameter", "depth", "entry")}))
    b = s.hole_at(50, 30)
    check("blind Ø8 × 5 flat", b is not None and close(b["diameter"], 8) and not b["through"]
          and close(b["depth"], 5) and not b["drill_tip"] and close(b["direction"], [0, 0, -1]))
    cs = s.hole_at(30, 30)
    check("countersunk Ø5 through", cs is not None and close(cs["diameter"], 5) and cs["through"])
    check_close("countersunk hole depth includes countersink", cs["depth"], 10)
    check_close("countersunk entry at top", cs["entry"], [30, 30, 10])
    check("countersink Ø10 × 90°", bool(cs.get("countersinks")) and close(cs["countersinks"][0]["outer_diameter"], 10)
          and close(cs["countersinks"][0]["angle_deg"], 90, 1e-2), str(cs.get("countersinks")))
    inner = s.hole_at(70, 30, d=6.6)
    outer = s.hole_at(70, 30, d=11)
    check("counterbore inner hole Ø6.6", inner is not None and inner["through"])
    check("counterbore linked", inner is not None and outer is not None and inner.get("counterbore", {}).get("feature") == outer["id"]
          and outer.get("counterbore_of") == inner["id"], str(inner and inner.get("counterbore")))
    check_close("counterbore Ø11 × 6.5", [inner["counterbore"]["diameter"], inner["counterbore"]["depth"]], [11, 6.5])
    check_close("counterbored hole depth / entry", inner["depth"] + 0 * 1, 10)
    check_close("counterbored hole entry", inner["entry"], [70, 30, 10])
    caps = [p for p in s.feats("plane") if p["is_cap"]]
    check("2 cap planes (blind bottom + counterbore floor)", len(caps) == 2, str(len(caps)))
    pats = s.info["patterns"]
    check("one grid pattern", len(pats) == 1 and pats[0]["type"] == "grid", str(pats))
    if pats:
        p = pats[0]
        check("grid 2×2, 80 × 40, centre (50,30,10)", p["count_u"] == 2 and p["count_v"] == 2
              and close([p["spacing_u"], p["spacing_v"]], [80, 40]) and close(p["center"], [50, 30, 10]), str(p))
        check("pattern hole ids tagged", all(s.f(h).get("pattern") == p["id"] for h in p["holes"]))
    ext = {d["label"]: d["value"] for d in s.dims("extent")}
    check("extents X/Y/Z", close([ext.get("overall X size"), ext.get("overall Y size"), ext.get("overall Z size")],
                                 [100, 60, 10]), str(ext))
    h = s.hole_at(10, 10, d=6)
    edge = sorted(d["value"] for d in s.dims("edge_distance") if d["refs"][0] == h["id"])
    check_close("corner hole edge distances 10 / 10", edge, [10, 10])
    hb = s.hole_at(50, 30)
    edge = sorted(d["value"] for d in s.dims("edge_distance") if d["refs"][0] == hb["id"])
    check_close("central blind hole edge distances 30 / 50", edge, [30, 50])
    check("dimension ids are 'A-B' of feature ids", all(d["id"] == f"{d['refs'][0]}-{d['refs'][1]}" for d in s.info["dimensions"]))
    check("no unrecognised faces", not any(o.get("unrecognized_faces") for o in s.info["objects"]))


def test_flange_recognition():
    section("flange recognition")
    s = Session(step("flange", fx.flange()))
    bolts = [h for h in s.feats("hole") if close(h["diameter"], 8)]
    check("6 bolt holes Ø8 + bore Ø30", len(bolts) == 6 and any(close(h["diameter"], 30) for h in s.feats("hole")))
    pats = s.info["patterns"]
    check("circular pattern", len(pats) == 1 and pats[0]["type"] == "circular", str(pats))
    if pats:
        p = pats[0]
        check("PCD 70, 6 × 60°, full circle, centre on axis", close(p["pcd"], 70) and p["count"] == 6
              and close(p["angle_step_deg"], 60) and p["full_circle"] and close(p["center"], [0, 0, 12]), str(p))
        check("start angle is a multiple of 60°", close((p["start_angle_deg"] + 1e-6) % 60, 0, 1e-2), str(p["start_angle_deg"]))
    check("outer cylinder is a boss Ø100 × 12", any(close(c["diameter"], 100) and close(c["length"], 12) for c in s.feats("boss")))
    top = s.plane("top face")
    check("top/bottom faces visible (not caps)", top is not None and not top["is_cap"])
    ext = [d for d in s.dims("extent") if d["label"] == "overall Z size"]
    check("overall Z size 12 found on an annular face pair", len(ext) == 1 and close(ext[0]["value"], 12), str(ext))


def test_linear_and_bracket():
    section("linear pattern + bracket")
    s = Session(step("bar", fx.linear_pattern()))
    p = s.info["patterns"]
    check("linear pattern 5 × 40 along X", len(p) == 1 and p[0]["type"] == "linear" and p[0]["count"] == 5
          and close(p[0]["spacing"], 40) and close(p[0]["direction"], [1, 0, 0]) and close(p[0]["start"], [20, 15, 8]), str(p))
    b = Session(step("bracket", fx.bracket()))
    check_close("bracket bbox", b.bbox()["size"], [80, 40, 60])
    boss = b.feats("boss")
    check("boss Ø20 × 8, free end on top", len(boss) == 1 and close(boss[0]["diameter"], 20) and close(boss[0]["length"], 8)
          and close(boss[0]["free_end"], [50, 20, 18]) and close(boss[0]["base_end"], [50, 20, 10]), str(boss))
    rounds = b.feats("round")
    check("2 fillets R5", len(rounds) == 2 and all(close(r["radius"], 5) for r in rounds), str(rounds))
    th = {(d["label"], d["value"]) for d in b.dims("thickness")}
    check("base thickness 10 along Z and upright 10 along X", ("thickness along Z", 10.0) in th and ("thickness along X", 10.0) in th, str(th))
    check("boss top face is a visible plane", any(close(p["center"], [50, 20, 18]) and not p["is_cap"] for p in b.feats("plane")))


def test_other_recognition():
    section("drill tip / side holes / slot / NURBS")
    s = Session(step("tip", fx.drill_tip_block()))
    h = s.feats("hole")
    check("blind Ø10 with drill tip", len(h) == 1 and close(h[0]["diameter"], 10) and h[0]["drill_tip"] and not h[0]["through"]
          and close(h[0]["depth"], 12) and close(h[0]["entry"], [20, 20, 20]), str(h))
    check("tip cone not reported separately", not s.feats("cone"), str(s.feats("cone")))
    s = Session(step("side", fx.side_holes_block()))
    hx = [x for x in s.feats("hole") if close(x["axis"], [1, 0, 0])]
    hy = [x for x in s.feats("hole") if close(x["axis"], [0, 1, 0])]
    check("Ø8 through along X", len(hx) == 1 and close(hx[0]["diameter"], 8) and hx[0]["through"] and close(hx[0]["depth"], 50))
    check("Ø6 × 10 blind along Y from the front", len(hy) == 1 and not hy[0]["through"] and close(hy[0]["depth"], 10)
          and close(hy[0]["entry"], [25, 0, 22]) and close(hy[0]["direction"], [0, 1, 0]), str(hy))
    edge = sorted(d["value"] for d in s.dims("edge_distance") if hx and d["refs"][0] == hx[0]["id"])
    check_close("X hole edge distances 15 / 15", edge, [15, 15])
    s = Session(step("slot", fx.slot_block()))
    gaps = s.dims("gap")
    check("slot width 10 (gap along X)", any(close(d["value"], 10) and d["label"].endswith("along X") for d in gaps), str(gaps))
    th = [d for d in s.dims("thickness") if d["label"] == "thickness along Z"]
    check("material under the slot 12", any(close(d["value"], 12) for d in th), str(th))
    s = Session(step("nurbs", fx.nurbs_block()))
    check("NURBS: 6 planes, 1 hole", len(s.feats("plane")) == 6 and len(s.feats("hole")) == 1, str(s.info["counts"]))
    if s.feats("hole"):
        h = s.feats("hole")[0]
        check("NURBS hole Ø10 through at (30,20)", close(h["diameter"], 10, 1e-2) and h["through"]
              and close(h["entry"][:2], [30, 20], 1e-2), str(h))
    check_close("NURBS extents", [d["value"] for d in sorted(s.dims("extent"), key=lambda d: d["label"])], [60, 40, 20], 1e-2)


def test_assembly():
    section("assembly (FCStd and STEP)")
    for kind, path in (("fcstd", fx.write_assembly_fcstd(os.path.join(TMP, "asm.FCStd"))),
                       ("step", fx.write_assembly_step(os.path.join(TMP, "asm.step")))):
        s = Session(path)
        objs = {o["label"]: o for o in s.info["objects"]}
        check(f"{kind}: 2 parts, containers skipped", len(objs) == 2, str(list(objs)))
        top = next((o for o in objs.values() if close(o["bbox"]["size"], [120, 80, 6])), None)
        leg = next((o for o in objs.values() if close(o["bbox"]["size"], [10, 10, 100])), None)
        check(f"{kind}: table top in global coords", top is not None and close(top["bbox"]["min"], [0, 0, 100]), str(top and top["bbox"]))
        check(f"{kind}: leg in global coords", leg is not None and close(leg["bbox"]["min"], [5, 5, 0]), str(leg and leg["bbox"]))
        h = s.feats("hole")
        check(f"{kind}: top hole entry in global coords", len(h) == 1 and close(h[0]["entry"], [60, 40, 106]), str(h))
        if kind == "fcstd" and leg:
            s.apply("transform_object", object=leg["name"], translate=[10, 0, 0])
            leg2 = next(o for o in s.info["objects"] if o["name"] == leg["name"])
            check("transform inside App::Part uses global offset", close(leg2["bbox"]["min"], [15, 5, 0]), str(leg2["bbox"]))
            s.apply("transform_object", object=leg["name"], rotate={"axis": "Z", "angle": 90, "center": [20, 10, 0]})
            leg3 = next(o for o in s.info["objects"] if o["name"] == leg["name"])
            # leg footprint x 15..25, y 5..15 rotated 90° about (20,10): x 15..25, y 5..15 (square about its centre)
            check("rotation about a global centre", close(leg3["bbox"]["min"], [15, 5, 0]) and close(leg3["bbox"]["max"], [25, 15, 100]),
                  str(leg3["bbox"]))


def _face_coverage(s):
    """(faces with no ID, faces in total) over all objects: every face must belong to a feature."""
    an = E._analyze(s.doc(), s.registry)
    owned = {(o, i) for f in an.feats for i in E._feature_faces(f) for o in [f["_obj"]]}
    total = [(o.Name, i) for o in an.objs for i in range(len(an.shapes[o.Name].Faces))]
    return [x for x in total if x not in owned], len(total)


def test_component_ids_and_pick():
    section("component IDs and picking")
    for name, shape in (("plate", fx.plate()), ("flange", fx.flange()), ("bracket", fx.bracket()),
                        ("nurbs", fx.nurbs_block()), ("vase", fx.vase())):
        s = Session(step("ids_" + name, shape))
        missing, total = _face_coverage(s)
        check(f"{name}: every face has an ID ({total} faces)", not missing, str(missing[:10]))
        ids = [f["id"] for fs in s.info["features"].values() for f in fs] + [o["id"] for o in s.info["objects"]]
        check(f"{name}: IDs unique", len(ids) == len(set(ids)), str(sorted(ids)))

    s = Session(step("ids_plate2", fx.plate()))
    check("plate: part ID B1", [o.get("id") for o in s.info["objects"]] == ["B1"], str(s.info["objects"]))
    check("every feature names its part", all(f.get("part") == "B1" for fs in s.info["features"].values() for f in fs))
    blind = s.hole_at(50, 30)
    top = s.plane("top face")
    res = s.call("pick", point=[54, 30, 7])  # on the Ø8 blind hole wall
    check("pick hole wall → the hole", res["id"] == blind["id"] and res["part"] == "B1", str(res))
    res = s.call("pick", point=[50, 30, 5])  # on the blind hole's flat floor
    check("pick blind hole floor → the hole", res["id"] == blind["id"], str(res))
    res = s.call("pick", point=[50, 5, 10.01])  # top face, mesh point slightly off the surface
    check("pick top face → top plane", res["id"] == top["id"], str(res))
    res = s.call("pick", point=[30 + 4, 30, 9])  # on the countersink cone
    check("pick countersink → its hole", res["id"] == s.hole_at(30, 30)["id"], str(res))
    res = s.call("pick", point=[50, 5, 10], mode="part")
    check("pick in part mode → B1", res["id"] == "B1", str(res))
    res = s.call("pick", point=[500, 500, 500])
    check("pick in empty space → nothing", res["id"] is None, str(res))
    hl = s.call("highlight", ids=["B1", blind["id"]])
    check("highlight a part: whole-part mesh", [m["kind"] for m in hl["meshes"]] == ["part", "hole"]
          and len(hl["meshes"][0]["indices"]) > len(hl["meshes"][1]["indices"]), str([m["kind"] for m in hl["meshes"]]))
    check("FreeCAD selection is empty headless", s.call("gui_selection")["ids"] == [])
    before = {h["id"] for h in s.feats("hole")}
    s.apply("add_holes", face=top["id"], diameter=4, positions=[[30, 10], [40, 10, 10]])
    new = sorted((h for h in s.feats("hole") if h["id"] not in before), key=lambda h: h["entry"][0])
    check("add_holes accepts [x, y] on a Z face and [x, y, z]", len(new) == 2 and close(new[0]["entry"], [30, 10, 10])
          and close(new[1]["entry"], [40, 10, 10]) and close(new[0]["diameter"], 4), str([h["entry"] for h in new]))
    fl = Session(step("ids_flange_bore", fx.flange()))
    try:
        fl.apply("add_holes", face=fl.plane("top face")["id"], diameter=5, positions=[[-10, 0, 12]])
        check("hole inside the Ø30 bore is refused", False)
    except Exception as e:
        check("hole inside the Ø30 bore is refused, naming the bore", "would cut into hole" in str(e)
              and "Ø30" in str(e), str(e))
    try:
        E._face_point({"id": "PX", "_n": E.V(0.6, 0.8, 0), "_c": E.V(0, 0, 0)}, [1, 2])
        check("2-number point on a slanted face is refused", False)
    except E.EngineError:
        check("2-number point on a slanted face is refused", True)

    v = Session(step("ids_vase", fx.vase()))
    ff = v.feats("face")
    check("vase: free-form side face gets an F ID", len(ff) == 1 and ff[0]["id"] == "F1", str(v.info["counts"]))
    check("vase: still counted as unrecognised", v.info["objects"][0].get("unrecognized_faces") == 1)
    res = v.call("pick", point=[14, 0, 10])
    check("pick the free-form face → F1", res["id"] == "F1", str(res))
    v.apply("push_pull_face", face=v.plane("top face")["id"], distance=3)
    check("F1 keeps its ID after an edit elsewhere", [f["id"] for f in v.feats("face")] == ["F1"], str(v.feats("face")))

    a = Session(fx.write_assembly_fcstd(os.path.join(TMP, "ids_asm.FCStd")))
    parts = {o["label"]: o for o in a.info["objects"]}
    check("assembly: B1, B2", sorted(o["id"] for o in parts.values()) == ["B1", "B2"], str(parts))
    leg = next(o for o in parts.values() if close(o["bbox"]["size"], [10, 10, 100]))
    a.apply("transform_object", object=leg["id"], translate=[0, 7, 0])  # part ID as the object reference
    leg2 = next(o for o in a.info["objects"] if o["name"] == leg["name"])
    check("transform by part ID", close(leg2["bbox"]["min"], [5, 12, 0]) and leg2["id"] == leg["id"], str(leg2))
    res = a.call("pick", point=[15, 17, 50])  # on the moved leg's +X side
    check("pick on the leg → a feature of the leg's part", res["part"] == leg["id"], str(res))
    f = a.call("find_features", object=leg["id"])
    check("find_features accepts a part ID", f["total"] > 0 and all(x["part"] == leg["id"] for x in f["features"]))


# ─────────────────────────────────────────────────────────────────────────────
# Edits
# ─────────────────────────────────────────────────────────────────────────────

def test_hole_edits():
    section("hole edits")
    s = Session(step("plate_e1", fx.plate()))
    ids_before = sorted(h["id"] for h in s.feats("hole"))
    h = s.hole_at(10, 10, d=6)
    v0 = s.volume()
    res = s.apply("resize_hole", hole=h["id"], diameter=8)
    ok, vs = ok_verified(res, "diameter")
    check("resize Ø6→Ø8 verified", ok and close(vs[0]["value"], 8), str(vs))
    check_close("resize volume change -π(16-9)·10", s.volume() - v0, -math.pi * 7 * 10, 1e-2)
    check("hole ids unchanged after resize", sorted(x["id"] for x in s.feats("hole")) == ids_before)
    check("resized hole keeps its id and position", s.f(h["id"]) and close(s.f(h["id"])["entry"], [10, 10, 10]))

    cs = s.hole_at(30, 30)
    v0 = s.volume()
    res = s.apply("resize_hole", hole=cs["id"], diameter=6)
    n = s.f(cs["id"])
    check("countersunk resize keeps countersink Ø10 90°", n is not None and close(n["diameter"], 6)
          and n.get("countersinks") and close(n["countersinks"][0]["outer_diameter"], 10, 1e-2)
          and close(n["countersinks"][0]["angle_deg"], 90, 0.05), str(n))
    check("countersunk resize removed material", s.volume() < v0)

    inner = s.hole_at(70, 30, d=6.6)
    outer_id = inner["counterbore"]["feature"]
    res = s.apply("resize_hole", hole=inner["id"], diameter=5.5)
    n = s.f(inner["id"])
    check("counterbored inner resize Ø6.6→Ø5.5, counterbore kept", n is not None and close(n["diameter"], 5.5)
          and n.get("counterbore", {}).get("feature") == outer_id and close(n["counterbore"]["diameter"], 11), str(n))
    try:
        s.apply("resize_hole", hole=inner["id"], diameter=12)
        check("resize larger than its counterbore is refused", False)
    except E.EngineError:
        check("resize larger than its counterbore is refused", True)

    res = s.apply("move_hole", hole=inner["id"], offset=[0, 10, 0])
    n, o = s.f(inner["id"]), s.f(outer_id)
    check("move counterbored hole: both move", n is not None and o is not None and close(n["entry"], [70, 40, 10])
          and close(o["entry"], [70, 40, 10]) and n.get("counterbore", {}).get("feature") == outer_id,
          str(n and n["entry"]) + " / " + str(o and o["entry"]))
    check("move verified", ok_verified(res, "center")[0], str(res["verified"]))

    b = s.hole_at(50, 30)
    v0 = s.volume()
    s.apply("remove_features", features=[b["id"]])
    check_close("remove blind Ø8×5 adds π·16·5", s.volume() - v0, math.pi * 16 * 5, 1e-2)
    check("removed hole gone, id not reused", s.f(b["id"]) is None)

    v0 = s.volume()
    top = s.plane("top face")
    res = s.apply("add_holes", face=top["id"], diameter=4, positions=[[50, 30, 10]])
    check_close("add Ø4 through: -π·4·10", s.volume() - v0, -math.pi * 4 * 10, 1e-2)
    new = s.hole_at(50, 30, d=4)
    check("new hole gets a fresh id", new is not None and new["id"] not in ids_before, str(new and new["id"]))


def test_dimension_edits():
    section("set_dimension")
    for anchor, want_min, want_max in (("start", 0, 120), ("end", -20, 100), ("symmetric", -10, 110)):
        s = Session(step(f"plate_d_{anchor}", fx.plate()))
        d = s.dims("extent", "overall X size")[0]
        holes_before = {h["id"]: h["entry"] for h in s.feats("hole")}
        res = s.apply("set_dimension", dimension=d["id"], value=120, anchor=anchor)
        bb = s.bbox()
        check(f"X 100→120 anchor={anchor}: bbox", close([bb["min"][0], bb["max"][0]], [want_min, want_max]), str(bb))
        ok, vs = ok_verified(res, "dimension")
        check(f"anchor={anchor}: verified 120", ok and close(vs[0]["value"], 120), str(vs))
        check(f"anchor={anchor}: face ids kept", s.dim(d["id"]) is not None and close(s.dim(d["id"])["value"], 120))
        check(f"anchor={anchor}: holes untouched", {h["id"]: h["entry"] for h in s.feats("hole")} == holes_before)

    # Features opening on a moved face move with it (outward and inward).
    for label, value, anchor, top_z in (("outward symmetric 10→14", 14, "symmetric", 12),
                                        ("inward 10→8", 8, "start", 8)):
        s = Session(step(f"plate_mv_{value}", fx.plate()))
        z = s.dims("extent", "overall Z size")[0]
        blind, cs = s.hole_at(50, 30), s.hole_at(30, 30)
        inner = s.hole_at(70, 30, d=6.6)
        outer_id = inner["counterbore"]["feature"]
        ids = sorted(h["id"] for h in s.feats("hole"))
        s.apply("set_dimension", dimension=z["id"], value=value, anchor=anchor)
        check_close(f"{label}: thickness", s.bbox()["size"][2], value)
        check(f"{label}: same hole ids, no phantom holes", sorted(h["id"] for h in s.feats("hole")) == ids,
              str(sorted(h["id"] for h in s.feats("hole"))))
        b = s.f(blind["id"])
        check(f"{label}: blind hole keeps depth 5 from the top", b is not None and close(b["depth"], 5)
              and close(b["entry"][2], top_z) and not b["through"], str(b and (b["depth"], b["entry"])))
        c = s.f(cs["id"])
        check(f"{label}: countersink Ø10 90° at the new top, hole through", c is not None and c["through"]
              and c.get("countersinks") and close(c["countersinks"][0]["outer_diameter"], 10, 1e-2)
              and close(c["countersinks"][0]["at"][2], top_z) and close(c["depth"], value),
              str(c and (c.get("countersinks"), c["depth"])))
        i, o = s.f(inner["id"]), s.f(outer_id)
        check(f"{label}: counterbore keeps Ø11 × 6.5 from the top", i is not None and o is not None
              and i.get("counterbore", {}).get("feature") == outer_id and close(i["counterbore"]["depth"], 6.5)
              and close(o["entry"][2], top_z) and i["through"] and close(i["depth"], value),
              str(i and i.get("counterbore")) + " " + str(o and o["entry"]))
        check(f"{label}: through holes still through", all(h["through"] for h in s.feats("hole") if close(h["diameter"], 6)))

    s = Session(step("plate_d2", fx.plate()))
    z = s.dims("extent", "overall Z size")[0]
    v0 = s.volume()
    s.apply("set_dimension", dimension=z["id"], value=12)
    check_close("thickness 10→12 bbox Z", s.bbox()["size"][2], 12)
    check("through holes still through after thickening", all(h["through"] for h in s.feats("hole")
                                                              if close(h["diameter"], 6)))
    check("volume grew", s.volume() > v0)

    h = s.hole_at(10, 10, d=6)
    left = [d for d in s.dims("edge_distance") if d["refs"][0] == h["id"] and "left" in d["label"]][0]
    res = s.apply("set_dimension", dimension=left["id"], value=15)
    check_close("hole-plane: H to left face 10→15 moves hole to x=15", s.f(h["id"])["entry"][:2], [15, 10])
    check("hole-plane verified", ok_verified(res, "dimension")[0], str(res["verified"]))
    # reversed id order works too
    p_id = left["refs"][1]
    s.apply("set_dimension", dimension=f"{p_id}-{h['id']}", value=12)
    check_close("plane-hole order also accepted", s.f(h["id"])["entry"][0], 12)

    h2 = s.hole_at(90, 10, d=6)
    res = s.apply("set_dimension", dimension=f"{h['id']}-{h2['id']}", value=70)
    check_close("hole-hole start: second hole moves to x=82", s.f(h2["id"])["entry"][:2], [82, 10])
    check("hole-hole verified 70", ok_verified(res, "dimension")[0] and close(res["verified"][0]["value"], 70), str(res["verified"]))

    try:
        s.apply("set_dimension", dimension=f"{h['id']}-{s.plane('top face')['id']}", value=5)
        check("non-measurable dimension refused", False)
    except E.EngineError:
        check("non-measurable dimension refused", True)

    b = Session(step("bracket_d", fx.bracket()))
    base_t = [d for d in b.dims("thickness") if d["label"] == "thickness along Z"][0]
    b.apply("set_dimension", dimension=base_t["id"], value=15)
    boss = b.feats("boss")[0]
    check_close("bracket base 10→15 thick", b.dim(base_t["id"])["value"], 15)
    check("boss stays rooted", boss is not None and close(boss["free_end"][2], 18), str(boss))

    sl = Session(step("slot_d", fx.slot_block()))
    gap = [d for d in sl.dims("gap") if d["label"].endswith("along X")][0]
    v0 = sl.volume()
    res = sl.apply("set_dimension", dimension=gap["id"], value=14, anchor="symmetric")
    check("slot 10→14 symmetric verified", ok_verified(res, "dimension")[0] and close(res["verified"][0]["value"], 14), str(res["verified"]))
    check_close("slot widening removes 4·8·40", v0 - sl.volume(), 4 * 8 * 40, 1e-2)


def test_pattern_edits():
    section("edit_hole_pattern")
    s = Session(step("flange_p", fx.flange()))
    pid = s.info["patterns"][0]["id"]
    ids = list(s.info["patterns"][0]["holes"])
    res = s.apply("edit_hole_pattern", pattern=pid, pcd=80)
    p = s.info["patterns"][0] if s.info["patterns"] else {}
    check("PCD 70→80", close(p.get("pcd"), 80) and p.get("count") == 6, str(p))
    check("bolt holes keep ids", sorted(p.get("holes", [])) == sorted(ids))
    check("all bolt holes at r=40", all(close(math.hypot(*s.f(i)["entry"][:2]), 40) for i in ids))
    check("pattern verified", ok_verified(res, "pattern")[0], str(res["verified"]))
    res = s.apply("edit_hole_pattern", pattern=s.info["patterns"][0]["id"], count=8)
    p = s.info["patterns"][0] if s.info["patterns"] else {}
    check("count 6→8 at 45°", p.get("count") == 8 and close(p.get("angle_step_deg"), 45) and close(p.get("pcd"), 80), str(p))
    check("original 6 ids kept, 2 new", len(set(ids) & set(p.get("holes", []))) == 6 and len(p.get("holes", [])) == 8)

    s = Session(step("bar_p", fx.linear_pattern()))
    pid = s.info["patterns"][0]["id"]
    s.apply("edit_hole_pattern", pattern=pid, spacing=30)
    p = s.info["patterns"][0]
    xs = sorted(s.f(i)["entry"][0] for i in p["holes"])
    check_close("linear spacing 40→30, centred", xs, [40, 70, 100, 130, 160])
    s.apply("edit_hole_pattern", pattern=s.info["patterns"][0]["id"], count=3)
    check("linear count 5→3", s.info["patterns"] and s.info["patterns"][0]["count"] == 3 and len(s.feats("hole")) == 3,
          str(s.info["patterns"]))

    s = Session(step("plate_p", fx.plate()))
    pid = s.info["patterns"][0]["id"]
    s.apply("edit_hole_pattern", pattern=pid, spacing_u=70, spacing_v=30)
    p = s.info["patterns"][0]
    xs = sorted({round(s.f(i)["entry"][0], 3) for i in p["holes"]})
    ys = sorted({round(s.f(i)["entry"][1], 3) for i in p["holes"]})
    check_close("grid spacing 70 × 30 about the same centre", xs + ys, [15, 85, 15, 45])


def test_face_edge_edits():
    section("push/pull, fillet, chamfer, rollback")
    s = Session(step("plate_f", fx.plate()))
    top = s.plane("top face")
    s.apply("push_pull_face", face=top["id"], distance=2)
    check_close("push top +2 → Z 12", s.bbox()["size"][2], 12)
    check("top face keeps its id", s.f(top["id"]) is not None and close(s.f(top["id"])["center"][2], 12))
    s.apply("push_pull_face", face=top["id"], distance=-4)
    check_close("pull top -4 → Z 8", s.bbox()["size"][2], 8)

    b = Session(step("bracket_pull", fx.bracket()))
    base_top = next(p for p in b.feats("plane") if close(p["normal"], [0, 0, 1]) and close(p["center"][2], 10))
    b.apply("push_pull_face", face=base_top["id"], distance=-3)
    boss = b.feats("boss")
    one_solid = all(o["solids"] == 1 for o in b.info["objects"])
    check("pull face under a boss keeps the boss attached", one_solid and len(boss) == 1 and close(boss[0]["free_end"][2], 18)
          and close(boss[0]["length"], 11), str(boss))

    s = Session(step("plate_fl", fx.plate()))
    v0 = s.volume()
    res = s.apply("fillet_edges", radius=5, edges=[{"parallel_to": "Z"}])
    rounds = [r for r in s.feats("round") if close(r["radius"], 5)]
    check("fillet 4 vertical edges R5", len(rounds) == 4, str(len(rounds)))
    check_close("fillet volume -4·(25-25π/4)·10", v0 - s.volume(), 4 * (25 - 25 * math.pi / 4) * 10, 1e-2)
    check("fillet verified", ok_verified(res, "radius")[0], str(res["verified"]))

    s = Session(step("plate_ch", fx.plate()))
    v0 = s.volume()
    top, front = s.plane("top face"), s.plane("front face")
    s.apply("chamfer_edges", size=1, edges=[{"between": [top["id"], front["id"]]}])
    check_close("chamfer 1 mm along 100 mm edge: -50", v0 - s.volume(), 50, 1e-2)

    reg_before = dict(s.registry["items"])
    info_before = s.info
    try:
        s.apply("fillet_edges", radius=50, edges=[{"parallel_to": "Z"}])
        check("impossible fillet raises", False)
    except E.EngineError:
        check("impossible fillet raises", True)
    an = E._analyze(s.doc(), s.registry)
    vol = sum(an.shapes[o.Name].Volume for o in an.objs)
    check_close("rolled back: volume unchanged", vol, sum(o["volume"] for o in info_before["objects"]), 1e-2)
    check("rolled back: registry unchanged", s.registry["items"] == reg_before)


def test_parametric():
    section("parametric FCStd")
    s = Session(fx.write_pad_fcstd(os.path.join(TMP, "pad.FCStd")))
    check("is parametric", s.info["is_parametric"])
    names = [o["name"] for o in s.info["parametric"]]
    check("Pad listed with Length", "Pad" in names and close(next(o for o in s.info["parametric"] if o["name"] == "Pad")["properties"]["Length"], 10))
    sk = next((x for x in s.info["sketches"] if x["name"] == "Sketch"), None)
    check("sketch constraints Width/Depth", sk is not None and {c["name"] for c in sk["constraints"]} >= {"Width", "Depth"}, str(sk))
    res = s.apply("set_property", object="Pad", property="Length", value=25)
    check_close("Pad.Length 10→25: Z size", s.bbox()["size"][2], 25)
    check("set_property verified", ok_verified(res, "property")[0] and close(res["verified"][0]["value"], 25), str(res["verified"]))
    res = s.apply("set_sketch_constraint", sketch="Sketch", constraint="Width", value=55)
    check_close("Width 40→55: X size", s.bbox()["size"][0], 55)
    check("body edited parametrically (no AIEdit object)", not any(o["name"].startswith("AIEdit") for o in s.info["objects"]))


def test_queries():
    section("queries / highlight")
    s = Session(step("plate_q", fx.plate()))
    h = s.hole_at(10, 10, d=6)
    r = s.call("dimensions", feature=h["id"])
    types = sorted(d["type"] for d in r["dimensions"])
    check("dimensions(feature=…) filters: 2 edge distances + centre distance to its nearest hole",
          types == ["center_distance", "edge_distance", "edge_distance"] and all(h["id"] in d["refs"] for d in r["dimensions"]),
          str(r["dimensions"]))
    r = s.call("dimensions", type="extent")
    check("dimensions(type=extent) → 3", r["total"] == 3)
    check("dimensions returns patterns", len(r["patterns"]) == 1)
    pid = s.info["patterns"][0]["id"]
    ext = s.dims("extent")[0]["id"]
    r = s.call("highlight", ids=[h["id"], pid, ext, "H999"])
    ids = [m["id"] for m in r["meshes"]]
    check("highlight expands pattern and dimension refs", h["id"] in ids and len(ids) >= 6, str(ids))
    check("highlight meshes have triangles", all(len(m["indices"]) >= 3 and len(m["positions"]) % 3 == 0 for m in r["meshes"]))
    check("highlight reports unknown ids", r["missing"] == ["H999"], str(r["missing"]))
    m = next(x for x in r["meshes"] if x["id"] == h["id"])
    xs, ys = m["positions"][0::3], m["positions"][1::3]
    check("hole mesh lies on the hole wall", close([min(xs), max(xs), min(ys), max(ys)], [7, 13, 7, 13], 0.05))
    r = s.call("find_features", kind="hole", diameter_min=5.9, diameter_max=6.1)
    check("find_features by diameter", r["total"] == 4)
    r = s.call("find_features", kind="plane", object=s.info["objects"][0]["label"])
    check("find_features accepts object label", r["total"] >= 6)
    r = s.call("model_info")
    check("model_info manual_edit false headless", r["manual_edit"] is False)

    # undo/redo path: close + reopen from disk keeps every ID
    before = {f["id"]: f.get("center") for fs in s.info["features"].values() for f in fs}
    res = E.reload(s.sid, s.work, s.registry)
    after = {f["id"]: f.get("center") for fs in res["info"]["features"].values() for f in fs}
    check("ids identical after reload from disk", before == after)
    dims_before = [d["id"] for d in s.info["dimensions"]]
    check("dimension ids identical after reload", [d["id"] for d in res["info"]["dimensions"]] == dims_before)

    from freecad_bridge import server
    server._reload()
    from freecad_bridge import engine as E2
    check("bridge hot-reload re-imports analysis + engine", "set_dimension" in E2.OPS and "highlight" in E2.METHODS)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1: precision foundations
# ─────────────────────────────────────────────────────────────────────────────

def _faces_ok(name, s):
    missing, total = _face_coverage(s)
    check(f"{name}: every face has exactly one ID ({total} faces)", not missing, str(missing[:10]))


def test_angular_radial_dimensions():
    section("angle / radial / centre-distance / coaxial dimensions")
    s = Session(step("chamfer", fx.chamfer_block()))
    _faces_ok("chamfer block", s)
    ang = s.dims("angle")
    check("chamfer: 2 angle dimensions of 135°", len(ang) == 2 and all(close(d["value"], 135) for d in ang)
          and all(d.get("unit") == "deg" and "∠" in d["id"] for d in ang), str(ang))
    cham = next(p for p in s.feats("plane") if p["label"] == "inclined face")
    top = s.plane("top face")
    back = s.plane("back face")
    a_top = next(d for d in ang if set(d["refs"]) == {cham["id"], top["id"]})
    check("no angle dimension for square corners", not any(top["id"] in d["refs"] and s.plane("front face")["id"] in d["refs"]
                                                          for d in ang))

    v0 = s.volume()
    res = s.apply("set_dimension", dimension=f"{top['id']}∠{cham['id']}", value=120)
    ok, vs = ok_verified(res, "dimension")
    check("angle 135°→120° (chamfer rotates about its edge with the top) verified", ok and close(vs[0]["achieved"], 120, 1e-6)
          and vs[0]["within_tolerance"] and vs[0]["unit"] == "deg", str(vs))
    # removed corner triangle grows from legs 10×10 to 10×17.3205 → ΔV = -(86.6025 - 50)·60
    check_close("angle edit removes the exact wedge", s.volume() - v0, -(10 * 10 * math.sqrt(3) / 2 - 50) * 60, 1e-2)
    check("chamfer face keeps its ID", s.f(cham["id"]) is not None and s.dim(a_top["id"]) is not None
          and close(s.dim(a_top["id"])["value"], 120, 1e-3), str(s.f(cham["id"])))
    check("top and back faces unchanged", close(s.f(top["id"])["center"][2], 30) and close(s.f(back["id"])["normal"], [0, 1, 0]))
    res = s.apply("set_dimension", dimension=f"{cham['id']}-{top['id']}", value=150, anchor="end")
    check_close("angle 120°→150° (face swings out, material added)", s.volume() - v0,
                (50 - 10 * 10 / math.sqrt(3) / 2) * 60, 1e-2)
    check("'A-B' of two non-parallel faces is an angle too", ok_verified(res, "dimension")[0], str(res["verified"]))
    _faces_ok("chamfer block after angle edits", s)
    try:
        s.apply("set_dimension", dimension=f"{s.plane('front face')['id']}∠{back['id']}", value=30)
        check("angle between parallel faces refused", False)
    except E.EngineError:
        check("angle between parallel faces refused", True)

    b = Session(step("bracket_rad", fx.bracket()))
    boss = b.feats("boss")[0]
    rnd = b.feats("round")[0]
    check("radial dims: boss Ø20 and fillets R5", close(b.dim(f"{boss['id']}.Ø")["value"], 20)
          and close(b.dim(f"{rnd['id']}.R")["value"], 5) and len(b.dims("radius")) == 2, str(b.dims("diameter") + b.dims("radius")))
    v0 = b.volume()
    res = b.apply("set_dimension", dimension=f"{boss['id']}.Ø", value=24)
    nb = b.f(boss["id"])
    check("boss Ø20→Ø24 keeps ID, length and top", nb is not None and close(nb["diameter"], 24) and close(nb["length"], 8)
          and close(nb["free_end"], [50, 20, 18]) and ok_verified(res, "dimension")[0], str(nb))
    check_close("boss grow volume +π(144-100)·8", b.volume() - v0, math.pi * 44 * 8, 1e-2)
    b.apply("set_dimension", dimension=f"{boss['id']}.dia", value=16)
    check_close("boss shrink Ø24→Ø16 (.dia alias)", b.f(boss["id"])["diameter"], 16)
    v0 = b.volume()
    res = b.apply("set_dimension", dimension=f"{rnd['id']}.R", value=3)
    nr = b.f(rnd["id"])
    check("fillet R5→R3 keeps its ID", nr is not None and close(nr["radius"], 3) and ok_verified(res, "dimension")[0],
          str(res["verified"]))
    check_close("fillet R5→R3 volume +10·[(25-25π/4)-(9-9π/4)]", b.volume() - v0, 10 * 16 * (1 - math.pi / 4), 1e-2)
    _faces_ok("bracket after radial edits", b)

    f = Session(step("flange_cd", fx.flange()))
    cd = [d for d in f.dims("center_distance") if all(f.f(r)["diameter"] == 8 for r in d["refs"])]
    check("flange: bolt-hole centre distances = 35 (PCD 70 · sin 30°)", len(cd) == 6 and all(close(d["value"], 35) for d in cd),
          str(cd))

    c = Session(step("coax", fx.coaxial_block()))
    _faces_ok("coaxial block", c)
    co = sorted(c.dims("coaxial"), key=lambda d: d["value"])
    check("coaxial pairs: offsets 0 and 0.2", len(co) == 2 and close(co[0]["value"], 0) and close(co[1]["value"], 0.2)
          and "◎" in co[0]["id"], str(co))
    res = c.apply("set_dimension", dimension=co[1]["id"], value=0)
    check("set coaxiality 0.2 → 0 (second hole moves onto the first axis)", ok_verified(res, "dimension")[0]
          and close(c.dim(co[1]["id"])["value"], 0), str(res["verified"]))


def test_tolerance_and_verification():
    section("tolerance-aware verification")
    s = Session(step("plate_tol", fx.plate()))
    h = s.hole_at(10, 10, d=6)
    res = s.apply("resize_hole", hole=h["id"], diameter=8)
    v = res["verified"][0]
    check("verified entry has requested / achieved / deviation / within_tolerance",
          close(v["requested"], 8) and close(v["achieved"], 8, 1e-9) and v["deviation"] < 1e-9 and v["within_tolerance"] is True
          and v["unit"] == "mm", str(v))
    check("apply returns the touched features", h["id"] in res["touched"], str(res["touched"]))

    # An op that misses its target by 0.01 mm must be rolled back.
    def op_sloppy(ctx, hole):
        f = ctx.feature(hole, ("hole",))
        E._recut_holes(ctx, [(f, None, 4.005)], "sloppy")
        ctx.verify.append((f["id"], "diameter", 8.0))
        return "sloppy"
    E.OPS["sloppy_resize"] = op_sloppy
    h2 = s.hole_at(90, 10, d=6)
    v0, reg0 = s.volume(), dict(s.registry["items"])
    try:
        s.apply("sloppy_resize", hole=h2["id"])
        check("out-of-tolerance edit raises", False)
    except E.EngineError as e:
        check("out-of-tolerance edit raises with requested/achieved", "Outside tolerance" in str(e) and "8.01" in str(e), str(e))
    an = E._analyze(s.doc(), s.registry)
    check_close("out-of-tolerance edit rolled back (volume)", sum(an.shapes[o.Name].Volume for o in an.objs), v0, 1e-3)
    check("out-of-tolerance edit rolled back (hole still Ø6)", close(an.fmap[h2["id"]]["diameter"], 6))
    res = s.apply("sloppy_resize", hole=h2["id"], tolerance={"length": 0.02})
    check("a looser tolerance accepts it, deviation reported", close(res["verified"][0]["deviation"], 0.01, 1e-6)
          and res["verified"][0]["within_tolerance"], str(res["verified"]))
    del E.OPS["sloppy_resize"]

    pid = s.info["patterns"][0]["id"] if s.info["patterns"] else None
    f = Session(step("flange_tol", fx.flange()))
    res = f.apply("edit_hole_pattern", pattern=f.info["patterns"][0]["id"], pcd=76, start_angle=90)
    v = next(x for x in res["verified"] if x["field"] == "pattern")
    check("pattern verification: requested pcd/start angle within tolerance", v["within_tolerance"] and close(v["achieved"]["pcd"], 76)
          and close(v["requested"]["start_angle_deg"], 90), str(v))
    del pid

    p = Session(step("plate_tol2", fx.plate()))
    top = p.plane("top face")
    res = p.apply("push_pull_face", face=top["id"], distance=1.5)
    check("push/pull verifies the face offset", ok_verified(res, "offset")[0] and close(res["verified"][0]["achieved"], 11.5),
          str(res["verified"]))
    res = p.apply("add_holes", face=top["id"], diameter=3, positions=[[50, 50], [60, 50]])
    news = [v for v in res["verified"] if v["field"] == "diameter"]
    check("add_holes verifies each new hole's diameter", len(news) == 2 and all(v["within_tolerance"] for v in news), str(res["verified"]))
    a = Session(fx.write_assembly_fcstd(os.path.join(TMP, "tol_asm.FCStd")))
    leg = next(o for o in a.info["objects"] if close(o["bbox"]["size"], [10, 10, 100]))
    res = a.apply("transform_object", object=leg["name"], translate=[3, 0, 0])
    check("translate verifies the new bbox min", ok_verified(res, "bbox_min")[0] and close(res["verified"][0]["achieved"], [8, 5, 0]),
          str(res["verified"]))
    res = a.apply("scale_object", object=leg["name"], factor=[2, 1, 0.5])
    check("scale verifies the new bbox size", ok_verified(res, "bbox_size")[0] and close(res["verified"][0]["achieved"], [20, 10, 50]),
          str(res["verified"]))
    pad = Session(fx.write_pad_fcstd(os.path.join(TMP, "tol_pad.FCStd")))
    res = pad.apply("set_property", object="Pad", property="Length", value=12.5)
    check("set_property verified with deviation", res["verified"][0]["within_tolerance"] and close(res["verified"][0]["deviation"], 0),
          str(res["verified"]))


def test_locks():
    section("locked dimensions / design intent")
    s = Session(step("plate_lock", fx.plate()))
    x = s.dims("extent", "overall X size")[0]
    z = s.dims("extent", "overall Z size")[0]
    blind = s.hole_at(50, 30)
    vals = s.call("evaluate_locks", locks=[{"target": x["id"]}, {"target": blind["id"], "mode": "centered", "axis": "X"},
                                           {"target": "NOPE-P1"}])["locks"]
    check("evaluate_locks: dimension value, centred offset, missing target", close(vals[0]["value"], 100)
          and close(vals[1]["value"]["X"], 0) and vals[2]["value"] is None, str(vals))
    locks = [{"target": z["id"], "value": 10.0}]
    res = E.apply(s.sid, s.work, "set_dimension", {"dimension": x["id"], "value": 110}, s.registry, locks=locks)
    s.info, s.registry = res["info"], res["registry"]
    check("unrelated edit allowed with a lock", close(s.bbox()["size"], [110, 60, 10]))
    v0 = s.volume()
    try:
        E.apply(s.sid, s.work, "set_dimension", {"dimension": z["id"], "value": 12}, s.registry, locks=locks)
        check("edit of a locked dimension is refused", False)
    except E.EngineError as e:
        check("edit of a locked dimension is refused and names the lock", "Refused" in str(e) and z["id"] in str(e), str(e))
    an = E._analyze(s.doc(), s.registry)
    check_close("refused edit left the document unchanged", sum(an.shapes[o.Name].Volume for o in an.objs), v0, 1e-3)

    s = Session(step("plate_lock2", fx.plate()))
    x = s.dims("extent", "overall X size")[0]
    blind = s.hole_at(50, 30)
    locks = [{"target": blind["id"], "mode": "centered", "axis": "X", "value": {"X": 0.0}}]
    res = E.apply(s.sid, s.work, "set_dimension", {"dimension": x["id"], "value": 120}, s.registry, locks=locks)
    s.info, s.registry = res["info"], res["registry"]
    check("'keep H centred' solved by anchor symmetric", close([s.bbox()["min"][0], s.bbox()["max"][0]], [-10, 110])
          and res.get("lock_resolution") == "anchor 'symmetric'" and "Lock:" in res["message"], res["message"])
    right = s.plane("right face")
    locks = [{"target": right["id"], "value": E._feature_params(E._analyze(s.doc(), s.registry), E._analyze(s.doc(), s.registry).fmap[right["id"]])}]
    res = E.apply(s.sid, s.work, "set_dimension", {"dimension": x["id"], "value": 115}, s.registry, locks=locks)
    s.info, s.registry = res["info"], res["registry"]
    check("locked right face: the left face moves instead (anchor end)", close([s.bbox()["min"][0], s.bbox()["max"][0]], [-5, 110]),
          str(s.bbox()))
    try:
        E.apply(s.sid, s.work, "set_dimension", {"dimension": x["id"], "value": 112, "anchor": "start"}, s.registry, locks=locks)
        check("explicit anchor that breaks a lock is refused (no silent change)", False)
    except E.EngineError as e:
        check("explicit anchor that breaks a lock is refused (no silent change)", "Refused" in str(e), str(e))
    h = s.hole_at(10, 10, d=6)
    hl = [{"target": h["id"], "value": E._lock_value(E._analyze(s.doc(), s.registry), {"target": h["id"]})}]
    for op, params in (("remove_features", {"features": [h["id"]]}), ("resize_hole", {"hole": h["id"], "diameter": 7})):
        try:
            E.apply(s.sid, s.work, op, params, s.registry, locks=hl)
            check(f"locked hole: {op} refused", False)
        except E.EngineError as e:
            check(f"locked hole: {op} refused", "Refused" in str(e), str(e))

    t = Session(step("plate_lock3", fx.plate()))
    xs = t.dims("extent", "overall X size")[0]
    a, b = xs["refs"]
    lock_b = [{"target": b, "value": E._lock_value(E._analyze(t.doc(), t.registry), {"target": b})}]
    res = E.apply(t.sid, t.work, "set_face_distance", {"face": b, "reference_face": a, "distance": 104}, t.registry, locks=lock_b)
    t.info, t.registry = res["info"], res["registry"]
    check("set_face_distance with the moving face locked moves the reference instead",
          close([t.bbox()["min"][0], t.bbox()["max"][0]], [-4, 100]), str(t.bbox()))


def test_datums():
    section("datums")
    s = Session(step("plate_datum", fx.plate()))
    d = s.info["datums"]
    bottom, front, left = s.plane("bottom face"), s.plane("front face"), s.plane("left face")
    check("default datums: A = largest face (bottom), B = front, C = left",
          d["A"]["id"] == bottom["id"] and d["B"]["id"] == front["id"] and d["C"]["id"] == left["id"]
          and not d["A"]["explicit"], str(d))
    h = s.hole_at(10, 10, d=6)
    b = s.hole_at(50, 30)
    check("hole datum positions: A (entry) 10, B 10, C 10", close([h["datum_position"][k] for k in "ABC"], [10, 10, 10]),
          str(h.get("datum_position")))
    check("blind hole datum positions: B 30, C 50", close([b["datum_position"]["B"], b["datum_position"]["C"]], [30, 50]))
    res = s.apply("move_hole", hole=b["id"], to_datum={"B": 20, "C": 35})
    nb = s.f(b["id"])
    check("move_hole to datum coordinates (B 20, C 35) → (35, 20)", close(nb["entry"][:2], [35, 20])
          and all(v["within_tolerance"] for v in res["verified"] if v["field"].startswith("datum")), str(res["verified"]))
    top = s.plane("top face")
    before = {x["id"] for x in s.feats("hole")}
    s.apply("add_holes", face=top["id"], diameter=4, positions=[{"B": 45, "C": 40}])
    new = [x for x in s.feats("hole") if x["id"] not in before]
    check("add_holes at datum coordinates (B 45, C 40) → (40, 45)", len(new) == 1 and close(new[0]["entry"], [40, 45, 10]),
          str([x["entry"] for x in new]))
    try:
        s.apply("move_hole", hole=b["id"], to_datum={"A": 3})
        check("datum parallel to the hole axis plane can't place it", False)
    except E.EngineError as e:
        check("datum A (⟂ hole axis) can't place a hole sideways", "parallel" in str(e), str(e))
    right = s.plane("right face")
    res = E.model_info(s.sid, s.work, s.registry, datums={"C": right["id"]})
    d2 = res["info"]["datums"]
    hh = next(x for x in res["info"]["features"]["hole"] if x["id"] == h["id"])
    check("explicit datum C = right face: positions measured from it", d2["C"]["id"] == right["id"] and d2["C"]["explicit"]
          and close(hh["datum_position"]["C"], 90), str(hh.get("datum_position")))


def test_hole_profile():
    section("standard-size hole profiles")
    s = Session(step("plate_std", fx.plate()))
    h = s.hole_at(10, 10, d=6)
    res = s.apply("set_hole_profile", hole=h["id"], diameter=6.6)
    check("M6 clearance: Ø6 → Ø6.6, still through", close(s.f(h["id"])["diameter"], 6.6) and s.f(h["id"])["through"]
          and all(v["within_tolerance"] for v in res["verified"]), str(res["verified"]))
    res = s.apply("set_hole_profile", hole=h["id"], diameter=6.6, counterbore_diameter=11, counterbore_depth=6.5)
    n = s.f(h["id"])
    check("M6 counterbore added: Ø11 × 6.5, hole keeps its ID", n is not None and n.get("counterbore")
          and close([n["counterbore"]["diameter"], n["counterbore"]["depth"]], [11, 6.5]) and close(n["entry"], [10, 10, 10]),
          str(n and n.get("counterbore")))
    check("counterbore profile verified", len(res["verified"]) == 3 and all(v["within_tolerance"] for v in res["verified"]),
          str(res["verified"]))
    inner = s.hole_at(70, 30, d=6.6)
    cb_id = inner["counterbore"]["feature"]
    s.apply("set_hole_profile", hole=cb_id, diameter=9, counterbore_diameter=15, counterbore_depth=8.6)
    n = s.f(inner["id"])
    check("existing counterbore → M8 (Ø9, Ø15 × 8.6), both IDs kept", n is not None and close(n["diameter"], 9)
          and n.get("counterbore", {}).get("feature") == cb_id and close([n["counterbore"]["diameter"], n["counterbore"]["depth"]], [15, 8.6]),
          str(n and n.get("counterbore")))
    b = s.hole_at(50, 30)
    s.apply("set_hole_profile", hole=b["id"], diameter=5.5, countersink_diameter=11.2, countersink_angle=90)
    n = s.f(b["id"])
    check("blind hole gets an ISO 10642 countersink Ø11.2 × 90°, keeps blind depth 5",
          n is not None and n.get("countersinks") and close(n["countersinks"][0]["outer_diameter"], 11.2)
          and close(n["countersinks"][0]["angle_deg"], 90, 1e-2) and not n["through"] and close(n["depth"], 5), str(n))
    _faces_ok("plate after hole profiles", s)
    try:
        s.apply("set_hole_profile", hole=b["id"], counterbore_diameter=4, counterbore_depth=2)
        check("counterbore smaller than the hole refused", False)
    except E.EngineError:
        check("counterbore smaller than the hole refused", True)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: feature recognition
# ─────────────────────────────────────────────────────────────────────────────

# Faces that legitimately carry two IDs: a hole's floor / a boss's end face (also a plane P#).
def _face_conflicts(s):
    an = E._analyze(s.doc(), s.registry)
    owners = {}
    for f in an.feats:
        for i in f["_faces"]:
            owners.setdefault((f["_obj"], i), []).append(f["id"])
    return {k: v for k, v in owners.items() if len(v) > 1}


def _ids_ok(name, s):
    _faces_ok(name, s)
    conflicts = _face_conflicts(s)
    check(f"{name}: no face owned by two features", not conflicts, str(conflicts))
    ids = [f["id"] for fs in s.info["features"].values() for f in fs]
    check(f"{name}: IDs unique", len(ids) == len(set(ids)), str(sorted(ids)))


def _obround_area(w, L):
    return (L - w) * w + math.pi * (w / 2) ** 2


def test_slots():
    section("slots SL#")
    s = Session(step("slots", fx.slot_plate()))
    _ids_ok("slot plate", s)
    sl = {(round(x["center"][0]), round(x["center"][1])): x for x in s.feats("slot")}
    check("3 slots recognised", len(sl) == 3 and all(x["id"].startswith("SL") for x in sl.values()), str(s.info["counts"]))
    a, b, c = sl.get((30, 30)), sl.get((70, 45)), sl.get((80, 15))
    check("obround through slot 8 × 30 along X", a is not None and a["shape"] == "obround" and a["through"]
          and close([a["width"], a["length"], a["depth"]], [8, 30, 10]) and close(a["direction"], [1, 0, 0])
          and close(a["center"], [30, 30, 10]) and a["face"] == s.plane("top face")["id"], str(a))
    check("rectangular blind slot 6 × 24 × 4", b is not None and b["shape"] == "rectangular" and not b["through"]
          and close([b["width"], b["length"], b["depth"]], [6, 24, 4]) and close(b["direction"], [1, 0, 0]), str(b))
    check("obround blind slot 10 × 20 × 5 along Y", c is not None and c["shape"] == "obround" and not c["through"]
          and close([c["width"], c["length"], c["depth"]], [10, 20, 5]) and close(c["direction"], [0, 1, 0]), str(c))
    check("slot walls are not planes / rounds any more", not s.feats("round") and len(s.feats("plane")) == 6,
          str(s.info["counts"]))
    ed = sorted(d["value"] for d in s.dims("edge_distance") if d["refs"][0] == a["id"])
    check("slot centre edge distances 30 / 30", close(ed, [30, 30]), str(ed))

    v0 = s.volume()
    res = s.apply("edit_slot", slot=a["id"], width=10)
    n = s.f(a["id"])
    check("through slot width 8 → 10 keeps ID, length, centre", n is not None and close([n["width"], n["length"]], [10, 30])
          and close(n["center"], [30, 30, 10]) and all(v["within_tolerance"] for v in res["verified"]), str(n))
    check_close("width edit volume -(area(10,30) - area(8,30))·10", s.volume() - v0,
                -(_obround_area(10, 30) - _obround_area(8, 30)) * 10, 1e-2)
    s.apply("edit_slot", slot=a["id"], length=40, offset=[0, 5, 0])
    n = s.f(a["id"])
    check("slot length 30 → 40 and moved +5 in Y", n is not None and close(n["length"], 40) and close(n["center"], [30, 35, 10]),
          str(n))
    v0 = s.volume()
    s.apply("edit_slot", slot=b["id"], depth=6)
    check_close("rectangular slot depth 4 → 6 removes 6·24·2", v0 - s.volume(), 6 * 24 * 2, 1e-2)
    check("depth verified", close(s.f(b["id"])["depth"], 6))
    v0 = s.volume()
    s.apply("remove_features", features=[c["id"]])
    check_close("remove blind obround slot adds area(10,20)·5", s.volume() - v0, _obround_area(10, 20) * 5, 1e-2)
    check("removed slot gone", s.f(c["id"]) is None)
    _ids_ok("slot plate after edits", s)
    try:
        s.apply("edit_slot", slot=b["id"], depth=12)
        check("slot deeper than the part refused", False)
    except E.EngineError:
        check("slot deeper than the part refused", True)


def test_pockets():
    section("pockets PK#")
    s = Session(step("pockets", fx.pocket_block()))
    _ids_ok("pocket block", s)
    pk = {(round(x["center"][0]), round(x["center"][1])): x for x in s.feats("pocket")}
    big, sq = pk.get((40, 30)), pk.get((10, 10))
    check("2 pockets", len(pk) == 2, str(s.info["counts"]))
    check("rounded pocket 40 × 30 × 8, corner R5", big is not None and close([big["length"], big["width"], big["depth"]], [40, 30, 8])
          and close(big["corner_radius"], 5) and not big["through"], str(big))
    check("square pocket 12 × 12 × 5, sharp", sq is not None and close([sq["length"], sq["width"], sq["depth"]], [12, 12, 5])
          and close(sq["corner_radius"], 0), str(sq))
    h = s.hole_at(40, 30, d=5)
    check("hole in the pocket floor stays a hole, entry on the floor", h is not None and h["through"]
          and close(h["entry"], [40, 30, 12]) and close(h["depth"], 12), str(h))
    floor_area = 40 * 30 - 4 * 25 * (1 - math.pi / 4) - math.pi * 2.5 ** 2
    v0 = s.volume()
    res = s.apply("edit_pocket", pocket=big["id"], depth=10)
    n = s.f(big["id"])
    check("pocket depth 8 → 10 verified", n is not None and close(n["depth"], 10)
          and all(v["within_tolerance"] for v in res["verified"]), str(res["verified"]))
    check_close("deeper pocket removes floor area · 2", v0 - s.volume(), floor_area * 2, 1e-2)
    h2 = s.f(h["id"])
    check("floor hole still through, now 10 deep from the new floor", h2 is not None and h2["through"] and close(h2["depth"], 10),
          str(h2))
    v0 = s.volume()
    s.apply("edit_pocket", pocket=big["id"], corner_radius=8)
    check("corner radius 5 → 8", close(s.f(big["id"])["corner_radius"], 8), str(s.f(big["id"])))
    check_close("corner radius 5 → 8 adds 4·(64-25)(1-π/4)·10", s.volume() - v0, 4 * 39 * (1 - math.pi / 4) * 10, 1e-2)
    v0 = s.volume()
    s.apply("edit_pocket", pocket=sq["id"], corner_radius=3)
    check_close("sharp pocket gets R3 corners: +4·9(1-π/4)·5", s.volume() - v0, 4 * 9 * (1 - math.pi / 4) * 5, 1e-2)
    v0 = s.volume()
    s.apply("remove_features", features=[sq["id"]])
    check_close("remove square pocket fills it", s.volume() - v0, (144 - 4 * 9 * (1 - math.pi / 4)) * 5, 1e-2)
    try:
        s.apply("edit_pocket", pocket=big["id"], depth=25)
        check("pocket deeper than the block refused", False)
    except E.EngineError:
        check("pocket deeper than the block refused", True)
    before = {x["id"] for x in s.feats("hole")}
    s.apply("add_holes", face=big["id"], diameter=4, positions=[[30, 30]])
    new = [x for x in s.feats("hole") if x["id"] not in before]
    check("add_holes into a pocket floor (face = PK#)", len(new) == 1 and close(new[0]["entry"], [30, 30, 10]), str(new))
    _ids_ok("pocket block after edits", s)


def test_chamfers():
    section("chamfers CH#")
    s = Session(step("cham4", fx.chamfer_box()))
    _ids_ok("chamfered box", s)
    ch = s.feats("chamfer")
    check("4 chamfers 2 mm × 45°", len(ch) == 4 and all(close(c["size"], 2) and close(c["angle_deg"], 45) for c in ch), str(ch))
    check("chamfers name their two faces", all(len(c["faces"]) == 2 and all(f.startswith("P") for f in c["faces"]) for c in ch))
    check("chamfer faces are not planes", len(s.feats("plane")) == 6, str(s.info["counts"]))
    p = Session(step("plate_ch2", fx.plate()))
    top, front = p.plane("top face"), p.plane("front face")
    p.apply("chamfer_edges", size=1, edges=[{"between": [top["id"], front["id"]]}])
    check("a chamfer made by chamfer_edges is recognised", len(p.feats("chamfer")) == 1 and close(p.feats("chamfer")[0]["size"], 1),
          str(p.feats("chamfer")))
    b = Session(step("cham1", fx.chamfer_box(all_top=False)))
    c = b.feats("chamfer")[0]
    v0 = b.volume()
    res = b.apply("edit_chamfer", chamfer=c["id"], size=3)
    check("chamfer 2 → 3 keeps its ID, verified", b.f(c["id"]) is not None and close(b.f(c["id"])["size"], 3)
          and all(v["within_tolerance"] for v in res["verified"]), str(res["verified"]))
    check_close("chamfer 2 → 3 removes (9-4)/2 · 60", v0 - b.volume(), 2.5 * 60, 1e-2)
    v0 = b.volume()
    res = b.apply("edit_chamfer", chamfer=c["id"], to_fillet=2)
    rounds = b.feats("round")
    check("chamfer → fillet R2", not b.feats("chamfer") and len(rounds) == 1 and close(rounds[0]["radius"], 2), str(rounds))
    check_close("chamfer 3 → fillet R2 volume", b.volume() - v0, 4.5 * 60 - (4 - math.pi) * 60, 1e-2)
    b2 = Session(step("cham1b", fx.chamfer_box(all_top=False)))
    v0 = b2.volume()
    b2.apply("remove_features", features=[b2.feats("chamfer")[0]["id"]])
    check_close("remove chamfer restores the sharp edge", b2.volume() - v0, 2 * 60, 1e-2)


def test_fillet_chains():
    section("fillet chains and toroidal fillets")
    s = Session(step("chain", fx.fillet_chain_box()))
    _ids_ok("all-edges fillet box", s)
    r = s.feats("round")
    check("12 fillets + 8 spherical corners = one R3 chain", len(r) == 1 and close(r[0]["radius"], 3)
          and r[0].get("chain_faces") == 20 and not s.feats("sphere"), str([(x["id"], x.get("chain_faces")) for x in r]))
    t = Session(step("rim", fx.boss_rim_plate()))
    _ids_ok("boss with rim fillet", t)
    tr = [x for x in t.feats("round") if x.get("toroidal")]
    check("toroidal rim fillet R2 (major R8)", len(tr) == 1 and close(tr[0]["radius"], 2) and close(tr[0]["torus_major_radius"], 8),
          str(t.feats("round")))
    b = Session(step("bracket_chain", fx.bracket()))
    check("separate fillets stay separate", len(b.feats("round")) == 2)


def test_edges():
    section("edge IDs E#")
    s = Session(step("edges_box", fx.chamfer_box(all_top=False)))
    top = s.plane("top face")
    r = s.call("find_edges", feature=top["id"])
    check("find_edges(feature=top): 4 edges with IDs", r["total"] == 4 and all(e["id"].startswith("E") for e in r["edges"]),
          str(r["edges"]))
    conv = s.call("find_edges", convexity="convex", kind="line")
    check("box edges are convex lines", conv["total"] >= 12, str(conv["total"]))
    e = next(x for x in r["edges"] if set(x["faces"]) == {top["id"], s.plane("back face")["id"]})
    check("edge data: type, length, faces, convexity", e["kind"] == "line" and close(e["length"], 60) and e["convexity"] == "convex", str(e))
    res = s.call("pick", point=[30, 40.2, 20.1], mode="edge")
    check("pick in edge mode → that edge", res["id"] == e["id"] and res["edge"]["length"] == e["length"], str(res))
    hl = s.call("highlight", ids=[e["id"]])
    m = hl["meshes"][0] if hl["meshes"] else {}
    check("highlight an edge → line segments along it", m.get("kind") == "edge" and len(m.get("lines", [])) >= 6
          and close([min(m["lines"][0::3]), max(m["lines"][0::3])], [0, 60]), str({k: v for k, v in m.items() if k != "lines"}))
    s.apply("add_holes", face=top["id"], diameter=4, positions=[[30, 20]])
    r2 = s.call("find_edges", feature=s.plane("back face")["id"])
    check("edge ID stable after an unrelated edit", e["id"] in [x["id"] for x in r2["edges"]], str([x["id"] for x in r2["edges"]]))
    v0 = s.volume()
    res = s.apply("fillet_edges", radius=2, edges=[{"edge": e["id"]}])
    check_close("fillet E# R2 removes (4-π)·60", v0 - s.volume(), (4 - math.pi) * 60, 1e-2)
    check("fillet by edge ID verified", ok_verified(res, "radius")[0], str(res["verified"]))
    try:
        s.apply("chamfer_edges", size=1, edges=[{"edge": "E999"}])
        check("unknown edge ID refused", False)
    except E.EngineError as ex:
        check("unknown edge ID refused", "Unknown edge" in str(ex), str(ex))


def test_wall_thickness_and_threads():
    section("wall thickness / threads")
    s = Session(step("thin", fx.thin_wall_plate()))
    r = s.call("wall_thickness", threshold=3)
    part = r["parts"][0]
    h = s.feats("hole")[0]
    check_close("minimum wall = 2.0 (hole to left face)", part["min_thickness"], 2.0, 1e-3)
    check("thin wall faces named", set(part["faces"]) == {h["id"], s.plane("left face")["id"]} and part["thin"], str(part))
    p = Session(step("plate_wall", fx.plate()))
    r = p.call("wall_thickness")
    check("plate: thinnest wall is the 10 - 6.5 = 3.5 under the counterbore", close(r["parts"][0]["min_thickness"], 3.5, 1e-3),
          str(r["parts"][0]))
    t = Session(fx.write_threaded_fcstd(os.path.join(TMP, "threads.FCStd")))
    th = t.info.get("threads") or []
    holes = {x["id"]: x for x in t.feats("hole")}
    check("cosmetic threads from the FCStd hole feature: 2 × M6x1.0, 8 deep", len(th) == 2
          and all(x["spec"] == "M6x1.0" and close(x["depth"], 8) and x["hole"] in holes for x in th), str(th))
    check("threaded holes are the Ø5 tap drill", all(close(holes[x["hole"]]["diameter"], 5) for x in th))


def test_phase2_regressions():
    section("known limitations fixed (STATUS §5)")
    b = Session(step("bracket_fmove", fx.bracket()))
    right = next(p for p in b.feats("plane") if close(p["normal"], [1, 0, 0]) and close(p["center"][0], 80))
    rounds = {r["id"]: r for r in b.feats("round")}
    v0 = b.volume()
    b.apply("push_pull_face", face=right["id"], distance=10)
    moved = {r["id"]: r for r in b.feats("round")}
    check("moving a face moves the fillets along its edges (same IDs, R5, x = 85)", set(moved) == set(rounds)
          and all(close(r["radius"], 5) and close(r["center"][0], 85) for r in moved.values()), str(moved))
    check_close("…and the part grows by exactly 10 · 40 · 10", b.volume() - v0, 4000, 1e-2)
    check("no step left behind (one solid, X 0..90)", close(b.bbox()["size"][0], 90) and b.info["objects"][0]["solids"] == 1)

    b = Session(step("bracket_pastboss", fx.bracket()))
    base_top = next(p for p in b.feats("plane") if close(p["normal"], [0, 0, 1]) and close(p["center"][2], 10))
    v0 = b.volume()
    area = 70 * 40 - 2 * 25 * (1 - math.pi / 4)  # the face incl. the boss footprint (two filleted corners)
    b.apply("push_pull_face", face=base_top["id"], distance=10)
    check("pushing a face past a boss leaves no pocket above it", not b.feats("boss") and not b.feats("hole")
          and not any(close(r.get("radius", 0), 10) for r in b.feats("round")), str(b.info["counts"]))
    check_close("…volume = old + face area · 10 - buried boss", b.volume() - v0, area * 10 - math.pi * 100 * 8, 1e-2)
    b = Session(step("bracket_boss_partial", fx.bracket()))
    base_top = next(p for p in b.feats("plane") if close(p["normal"], [0, 0, 1]) and close(p["center"][2], 10))
    b.apply("push_pull_face", face=base_top["id"], distance=3)
    boss = b.feats("boss")
    check("pushing a face part-way up a boss shortens it (free end stays at 18)", len(boss) == 1
          and close(boss[0]["free_end"][2], 18) and close(boss[0]["length"], 5), str(boss))

    s = Session(step("plate_grid", fx.plate()))
    pat = s.info["patterns"][0]
    ids = set(pat["holes"])
    res = s.apply("edit_hole_pattern", pattern=pat["id"], count_u=3, spacing_u=35)
    p2 = next((p for p in s.info["patterns"] if p["type"] == "grid"), {})
    check("grid 2×2 → 3×2 (count_u 3, spacing 35)", p2.get("count_u") == 3 and p2.get("count_v") == 2
          and close(p2.get("spacing_u"), 35) and close(p2.get("spacing_v"), 40), str(p2))
    check("original 4 IDs kept, 2 new", ids <= set(p2.get("holes", [])) and len(p2.get("holes", [])) == 6)
    check("grid count change verified", ok_verified(res, "pattern")[0], str(res["verified"]))
    g = Session(step("plate_grid_off", fx.plate()))
    gp = g.info["patterns"][0]
    v0 = g.volume()
    try:
        g.apply("edit_hole_pattern", pattern=gp["id"], count_u=3)  # spacing stays 80: two holes would be off the plate
        check("grid holes off the part are refused (found in the simulation)", False)
    except E.EngineError as ex:
        an = E._analyze(g.doc(), g.registry)
        check("grid holes off the part are refused (found in the simulation)", "off the part" in str(ex)
              and close(sum(an.shapes[o.Name].Volume for o in an.objs), v0, 1e-3) and len(an.patterns) == 1, str(ex))
    try:
        s.apply("edit_hole_pattern", pattern=p2["id"], count=8)
        check("plain count on a grid asks for count_u / count_v", False)
    except E.EngineError as ex:
        check("plain count on a grid asks for count_u / count_v", "count_u" in str(ex), str(ex))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3: safer, smarter editing
# ─────────────────────────────────────────────────────────────────────────────

def test_preview():
    section("preview (dry run)")
    s = Session(step("plate_prev", fx.plate()))
    x = s.dims("extent", "overall X size")[0]
    sig0 = E._doc_signature(s.doc())
    reg0 = json_copy(s.registry)
    mesh = os.path.join(TMP, "preview.stl")
    res = E.apply(s.sid, s.work, "set_dimension", {"dimension": x["id"], "value": 130}, s.registry, preview_mesh=mesh)
    check("preview returns the measured result", res.get("preview") and close(res["verified"][0]["achieved"], 130)
          and close(res["info"]["bbox"]["size"][0], 130), str(res["verified"]))
    check("preview writes the result mesh", os.path.exists(mesh) and os.path.getsize(mesh) > 1000)
    check("preview leaves the document unchanged (geometry signature identical)", E._doc_signature(s.doc()) == sig0)
    check("preview leaves the registry unchanged", res["registry"] == reg0 or res["registry"]["items"] == reg0["items"])
    an = E._analyze(s.doc(), s.registry)
    check_close("document still 100 long after the preview", an.shapes[an.objs[0].Name].BoundBox.XLength, 100)


def json_copy(x):
    import json
    return json.loads(json.dumps(x))


def test_new_ops():
    section("new operations (mirror, pattern, add slot / pocket / counterbore / countersink, draft, split, shell)")
    s = Session(step("plate_mirror", fx.plate()))
    v0 = s.volume()
    s.apply("mirror_object", plane="YZ", at=0, mode="copy")
    objs = s.info["objects"]
    check("mirror_object copy: a second, mirrored part", len(objs) == 2 and any(close(o["bbox"]["min"][0], -100) for o in objs)
          and close(sum(o["volume"] for o in objs), 2 * v0, 1e-2), str([o["bbox"] for o in objs]))
    s = Session(step("plate_mirror_fuse", fx.plate()))
    left = s.plane("left face")
    s.apply("mirror_object", plane=left["id"], mode="fuse")
    check("mirror_object fuse about the left face: symmetric part, X −100…100, volume ×2",
          close([s.bbox()["min"][0], s.bbox()["max"][0]], [-100, 100]) and close(s.volume(), 2 * v0, 1e-2), str(s.bbox()))

    s = Session(step("plate_mfeat", fx.plate()))
    h = s.hole_at(10, 10, d=6)
    n0 = len(s.feats("hole"))
    res = s.apply("mirror_features", features=[h["id"]], plane="XZ", at=20)
    new = s.hole_at(10, 30, d=6)
    check("mirror_features: Ø6 hole mirrored about y = 20 → (10, 30)", new is not None and new["through"] and len(s.feats("hole")) == n0 + 1
          and ok_verified(res, "count")[0], str(res["verified"]))
    h = s.hole_at(90, 10, d=6)
    s.apply("mirror_features", features=[h["id"]], plane="y = 20")
    check("mirror_features: plane given as 'y = 20' (coordinate form) → (90, 30)", s.hole_at(90, 30, d=6) is not None)
    sp = Session(step("slots_mirror", fx.slot_plate()))
    b = next(x for x in sp.feats("slot") if close(x["center"][:2], [70, 45]))
    sp.apply("mirror_features", features=[b["id"]], plane="YZ", at=50)
    check("mirror_features: a slot mirrored about x = 50 → (30, 45), same size",
          any(close(x["center"][:2], [30, 45]) and close([x["width"], x["length"], x["depth"]], [6, 24, 4]) for x in sp.feats("slot")),
          str([(x["id"], x["center"]) for x in sp.feats("slot")]))

    s = Session(step("plate_pat", fx.plate()))
    h = s.hole_at(10, 10, d=6)
    s.apply("pattern_feature", feature=h["id"], type="linear", count=3, spacing=15, direction="X")
    check("pattern_feature linear: 3 × every 15 → holes at x 10, 25, 40",
          s.hole_at(25, 10, 6) is not None and s.hole_at(40, 10, 6) is not None)
    br = Session(step("bracket_pat", fx.bracket()))
    boss = br.feats("boss")[0]
    br.apply("pattern_feature", feature=boss["id"], type="linear", count=2, spacing=-25, direction="X")
    check("pattern_feature of a boss: a second Ø20 boss at x = 25",
          len(br.feats("boss")) == 2 and any(close(c["free_end"], [25, 20, 18]) for c in br.feats("boss")), str(br.feats("boss")))
    pb = Session(step("pocket_pat", fx.pocket_block()))
    sq = next(x for x in pb.feats("pocket") if close(x["center"][:2], [10, 10]))
    pb.apply("pattern_feature", feature=sq["id"], type="linear", count=2, spacing=58, direction="X")
    check("pattern_feature of a pocket → (68, 10)", any(close(x["center"][:2], [68, 10]) for x in pb.feats("pocket")))
    fl = Session(step("flange_circ", fx.flange()))
    bolt = next(x for x in fl.feats("hole") if close(x["diameter"], 8))
    n0 = len(fl.feats("hole"))
    fl.apply("edit_hole_pattern", pattern=fl.info["patterns"][0]["id"], count=3)
    bolt = next(x for x in fl.feats("hole") if close(x["diameter"], 8) and close(math.hypot(*x["entry"][:2]), 35))
    fl.apply("pattern_feature", feature=bolt["id"], type="circular", count=6, axis="Z", center=[0, 0, 0], angle=360)
    check("pattern_feature circular: 6 × 60° restores a 6-hole bolt circle", len([x for x in fl.feats("hole") if close(x["diameter"], 8)]) == 6
          and fl.info["patterns"] and close(fl.info["patterns"][0]["angle_step_deg"], 60), str(fl.info["patterns"]))
    del n0

    s = Session(step("plate_addslot", fx.plate()))
    top = s.plane("top face")
    v0 = s.volume()
    res = s.apply("add_slot", face=top["id"], width=6, length=20, depth=4, center=[50, 50], direction="X")
    sl = s.feats("slot")
    check("add_slot: obround 6 × 20 × 4 at (50, 50)", len(sl) == 1 and close([sl[0]["width"], sl[0]["length"], sl[0]["depth"]], [6, 20, 4])
          and close(sl[0]["center"][:2], [50, 50]), str(sl))
    check_close("add_slot volume −area(6,20)·4", v0 - s.volume(), _obround_area(6, 20) * 4, 1e-2)
    try:
        s.apply("add_slot", face=top["id"], width=6, length=30, center=[95, 30], direction="X")
        check("a slot running off the face is refused", False)
    except E.EngineError as e:
        check("a slot running off the face is refused", "runs off" in str(e), str(e))
    res = s.apply("add_pocket", face=top["id"], length=20, width=12, depth=3, center=[50, 15], corner_radius=2)
    pk = s.feats("pocket")
    check("add_pocket: 20 × 12 × 3, corners R2", len(pk) == 1 and close([pk[0]["length"], pk[0]["width"], pk[0]["depth"],
                                                                         pk[0]["corner_radius"]], [20, 12, 3, 2]), str(pk))
    h = s.hole_at(10, 10, d=6)
    s.apply("add_counterbore", hole=h["id"], diameter=11, depth=5)
    n = s.f(h["id"])
    check("add_counterbore Ø11 × 5", n.get("counterbore") and close([n["counterbore"]["diameter"], n["counterbore"]["depth"]], [11, 5]),
          str(n.get("counterbore")))
    h2 = s.hole_at(90, 10, d=6)
    s.apply("add_countersink", hole=h2["id"], diameter=12, angle=90)
    n = s.f(h2["id"])
    check("add_countersink Ø12 × 90°", n.get("countersinks") and close(n["countersinks"][0]["outer_diameter"], 12),
          str(n.get("countersinks")))

    d = Session(step("draft_block", Part_box(60, 40, 30)))
    bottom, right, left = d.plane("bottom face"), d.plane("right face"), d.plane("left face")
    v0 = d.volume()
    res = d.apply("draft_faces", faces=[right["id"], left["id"]], angle=5, neutral=bottom["id"])
    angs = [v for v in res["verified"] if v.get("unit") == "deg"]
    check("draft 5° on two walls: both at 85° to the base, verified", len(angs) == 2 and all(close(v["achieved"], 85, 1e-6) for v in angs),
          str(angs))
    check_close("draft removes 2 wedges of 30 · 30·tan5° / 2 · 40", v0 - d.volume(), 2 * 30 * 30 * math.tan(math.radians(5)) / 2 * 40, 1e-2)

    sp = Session(step("plate_split", fx.plate()))
    v0 = sp.volume()
    sp.apply("split_object", plane="XY", at=5, keep="both")
    objs = sp.info["objects"]
    check("split_object at z = 5: two parts, volumes add up", len(objs) == 2 and close(sum(o["volume"] for o in objs), v0, 1e-2)
          and all(close(o["bbox"]["size"][2], 5) for o in objs), str([(o["label"], o["volume"]) for o in objs]))
    sh = Session(step("shell_box", Part_box(60, 40, 30)))
    top = sh.plane("top face")
    sh.apply("shell_object", thickness=2, open_faces=[top["id"]])
    check_close("shell 2 mm, open top: 60·40·30 − 56·36·28", sh.volume(), 60 * 40 * 30 - 56 * 36 * 28, 1e-2)


def Part_box(x, y, z):
    import Part
    return Part.makeBox(x, y, z)


def test_pre_edit_checks():
    section("pre-edit checks")
    s = Session(step("plate_chk", fx.plate()))
    top = s.plane("top face")
    v0 = s.volume()
    try:
        s.apply("add_holes", face=top["id"], diameter=6, positions=[[14, 10]])
        check("a hole overlapping another is refused", False)
    except E.EngineError as e:
        check("a hole overlapping another is refused (names it)", "overlap" in str(e) and s.hole_at(10, 10, 6)["id"] in str(e), str(e))
    h = s.hole_at(10, 10, 6)
    try:
        s.apply("move_hole", hole=h["id"], to=[1, 10, 10])
        check("a hole moved so it breaks out of the part is refused", False)
    except E.EngineError as e:
        check("a hole moved so it breaks out of the part is refused", "rolled back" in str(e) or "Pre-edit" in str(e), str(e))
    an = E._analyze(s.doc(), s.registry)
    check_close("refused edits changed nothing", sum(an.shapes[o.Name].Volume for o in an.objs), v0, 1e-3)
    res = s.apply("move_hole", hole=h["id"], to=[4.5, 10, 10], checks={"min_wall": 3})
    check("a wall thinner than the minimum gives a warning (edit kept)", any("thinnest wall" in w for w in res.get("warnings", []))
          and close(s.f(h["id"])["entry"][0], 4.5), str(res.get("warnings")))
    a = Session(fx.write_assembly_fcstd(os.path.join(TMP, "chk_asm.FCStd")))
    leg = next(o for o in a.info["objects"] if close(o["bbox"]["size"], [10, 10, 100]))
    try:
        a.apply("transform_object", object=leg["name"], translate=[0, 0, 5])
        check("moving a part into another is refused (interference)", False)
    except E.EngineError as e:
        check("moving a part into another is refused (interference)", "interfere" in str(e), str(e))


def test_compare_versions():
    section("version compare (geometry diff)")
    s = Session(step("plate_cmp", fx.plate()))
    pa = os.path.join(TMP, "cmp_a.FCStd")
    s.doc().saveCopy(pa)
    x = s.dims("extent", "overall X size")[0]
    s.apply("set_dimension", dimension=x["id"], value=110)
    top = s.plane("top face")
    s.apply("add_holes", face=top["id"], diameter=10, positions=[[50, 50]])
    pb = os.path.join(TMP, "cmp_b.FCStd")
    s.doc().saveCopy(pb)
    r = E.compare_versions(pa, pb)
    part = r["parts"][0]
    check_close("added material = the 10 mm extension (10 · 60 · 10)", part["added_volume"], 6000, 1e-2)
    check_close("removed material = the new Ø10 hole (π · 25 · 10)", part["removed_volume"], math.pi * 25 * 10, 1e-2)
    check("diff meshes for the viewer (added + removed)", {m["kind"] for m in r["meshes"]} == {"added", "removed"})


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4: analysis and manufacturing
# ─────────────────────────────────────────────────────────────────────────────

def test_mass_properties():
    section("mass properties")
    b = Session(step("mass_box", Part_box(100, 60, 10)))
    pid = b.info["objects"][0]["id"]
    r = b.call("mass_properties", densities={pid: 7.85})["parts"][0]
    m = 100 * 60 * 10 * 7.85 / 1000  # g
    check("steel box mass 471 g within 0.1 %", abs(r["mass_g"] - m) / m < 1e-3, str(r["mass_g"]))
    ixx = m / 1000 * (60 ** 2 + 10 ** 2) / 12  # kg·mm²
    izz = m / 1000 * (100 ** 2 + 60 ** 2) / 12
    check("inertia about the centre of mass within 0.1 % (Ixx, Izz)", abs(r["inertia_kg_mm2"][0][0] - ixx) / ixx < 1e-3
          and abs(r["inertia_kg_mm2"][2][2] - izz) / izz < 1e-3, str(r["inertia_kg_mm2"]))
    check_close("centre of mass (50, 30, 5)", r["center_of_mass"], [50, 30, 5])
    import Part
    c = Session(step("mass_cyl", Part.makeCylinder(25, 100)))
    r = c.call("mass_properties", densities={c.info["objects"][0]["id"]: 2.70})["parts"][0]
    m = math.pi * 25 ** 2 * 100 * 2.70 / 1000
    check("aluminium cylinder Ø50 × 100 mass within 0.1 %", abs(r["mass_g"] - m) / m < 1e-3, f"{r['mass_g']} vs {m}")
    p = Session(step("mass_plate", fx.plate()))
    r = p.call("mass_properties", densities={p.info["objects"][0]["id"]: 7.85})
    check("plate with holes: mass from the exact volume", close(r["parts"][0]["mass_g"], p.volume() * 7.85 / 1000, 1e-3)
          and "total" in r, str(r.get("total")))


def test_dfm():
    section("DFM checks")
    s = Session(step("dfm_bad", fx.dfm_bad_block()))
    r = s.call("dfm", process="cnc")
    rules = {}
    for f in r["findings"]:
        rules.setdefault(f["rule"], []).append(f)
    deep = rules.get("hole depth / diameter", [])
    check("CNC: Ø1.5 × 20 hole flagged as an error (13×)", any(f["severity"] == "error" for f in deep), str(deep))
    corner = [f for f in rules.get("internal corner radius", []) if any(x.startswith("PK") for x in f["features"])]
    check("CNC: sharp pocket corners flagged with a fix (edit_pocket)", corner and corner[0]["severity"] == "error"
          and corner[0].get("fix", {}).get("tool") == "edit_pocket", str(corner))
    ns = rules.get("non-standard hole size", [])
    check("CNC: Ø7.77 flagged as non-standard with a resize fix to Ø7.8", any(close(f["fix"]["args"]["diameter"], 7.8) for f in ns), str(ns))
    check("CNC: thin wall flagged (0.5 mm)", any(close(f["value"], 0.5, 1e-2) for f in rules.get("thin wall", [])), str(rules.get("thin wall")))
    check("CNC: deep narrow slot flagged (4:1)", any(close(f["value"], 4) for f in rules.get("deep narrow slot", [])),
          str(rules.get("deep narrow slot")))
    fix = corner[0]["fix"]
    s.apply(fix["tool"], **fix["args"])
    r2 = s.call("dfm", process="cnc")
    check("applying the fix clears the pocket finding", not [f for f in r2["findings"] if f["rule"] == "internal corner radius"
                                                             and any(x.startswith("PK") for x in f["features"])])
    o = Session(step("dfm_over", fx.overhang_part()))
    r = o.call("dfm", process="print3d")
    over = [f for f in r["findings"] if f["rule"] == "overhang"]
    check("3D print: the shelf's underside is an overhang (2 × 20 × 20 mm²)", over and close(sum(f["value"] for f in over), 800, 1.0),
          str(over))
    sh = Session(step("dfm_sheet", fx.sheet_bracket()))
    r = sh.call("dfm", process="sheet")
    rules = {f["rule"] for f in r["findings"]}
    check("sheet metal: hole too close to the edge and hole too close to the bend", {"hole to edge", "hole to bend"} <= rules,
          str([(f["rule"], f["message"]) for f in r["findings"]]))
    check("sheet metal: only the bad holes are flagged", all(sh.f(x)["entry"][0] in (8.0, 37.0) for f in r["findings"]
                                                              for x in f["features"] if x.startswith("H")), str(r["findings"]))


def test_assembly_tools():
    section("BOM, interference, part meshes")
    a = Session(fx.write_bom_assembly_step(os.path.join(TMP, "bom.step")))
    g = a.call("bom_groups")["groups"]
    qty = sorted(x["quantity"] for x in g)
    check("BOM: 3 lines — 1 top, 4 legs, 2 rails", qty == [1, 2, 4] and len(a.info["objects"]) == 7, str([(x["labels"][0], x["quantity"]) for x in g]))
    m = a.call("part_meshes")["parts"]
    check("one mesh per part for the exploded view", len(m) == 7 and all(len(x["indices"]) > 0 for x in m))
    c = Session(fx.write_clash_fcstd(os.path.join(TMP, "clash.FCStd")))
    r = c.call("interference", clearance=3)["pairs"]
    inter = [p for p in r if p["status"] == "interference"]
    clear = [p for p in r if p["status"] == "clearance"]
    check("interference: A/B overlap 4000 mm³", len(inter) == 1 and close(inter[0]["overlap_volume"], 4000), str(r))
    check("clearance: B/C 2 mm apart", len(clear) == 1 and close(clear[0]["distance"], 2), str(r))


def test_drawing():
    section("2D drawing (SVG + PDF)")
    s = Session(step("plate_draw", fx.plate()))
    svg, pdf = os.path.join(TMP, "draw", "plate.svg"), os.path.join(TMP, "draw", "plate.pdf")
    r = E.drawing(s.sid, s.work, svg, pdf, s.registry, material="Steel", mass_g=450.1)
    data = open(pdf, "rb").read()
    check("PDF written and well-formed", data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF") and b"/Type /Page" in data)
    check("PDF carries the overall dimensions 100 / 60 / 10", all(f"({v}) Tj".encode() in data for v in ("100", "60", "10")))
    check("hole callouts: 4× Ø6 THRU (and the others)", any(c.startswith("4× Ø6 THRU") for c in r["callouts"]) and len(r["callouts"]) == 4,
          str(r["callouts"]))
    check("PDF text in WinAnsi includes the callout", "4× Ø6 THRU".encode("cp1252") in data)
    text = open(svg, encoding="utf-8").read()
    check("SVG has the four views and a title block", all(w in text for w in ("FRONT", "TOP", "LEFT", "ISOMETRIC", "First angle")))
    check("every view has visible edges; hidden edges in front/left", all(v["visible"] > 0 for v in r["views"].values())
          and r["views"]["front"]["hidden"] > 0, str(r["views"]))


def test_performance():
    section("performance: 30-part assembly")
    path = fx.write_big_assembly_step(os.path.join(TMP, "big.step"), 30)
    t0 = time.time()
    s = Session(path)
    t_open = time.time() - t0
    check("30 objects", len(s.info["objects"]) == 30, str(len(s.info["objects"])))
    check("120 holes, 30 grid patterns", len(s.feats("hole")) == 120 and len(s.info["patterns"]) == 30,
          f"{len(s.feats('hole'))} holes, {len(s.info['patterns'])} patterns")
    t0 = time.time()
    an = E._analyze(s.doc(), s.registry)
    t_an = time.time() - t0
    t0 = time.time()
    n = len(an.dims)
    t_dims = time.time() - t0
    print(f"  timings: open {t_open:.2f}s, analyse {t_an:.2f}s, dimensions {t_dims:.2f}s ({n} dims)")
    check("analysis < 5 s", t_an < 5, f"{t_an:.2f}s")
    check("dimensions < 5 s", t_dims < 5, f"{t_dims:.2f}s")


def main():
    t0 = time.time()
    for fn in (test_plate_recognition, test_flange_recognition, test_linear_and_bracket, test_other_recognition,
               test_assembly, test_component_ids_and_pick, test_hole_edits, test_dimension_edits, test_pattern_edits, test_face_edge_edits,
               test_parametric, test_queries, test_angular_radial_dimensions, test_tolerance_and_verification,
               test_locks, test_datums, test_hole_profile, test_slots, test_pockets, test_chamfers, test_fillet_chains,
               test_edges, test_wall_thickness_and_threads, test_phase2_regressions, test_preview, test_new_ops,
               test_pre_edit_checks, test_compare_versions, test_mass_properties, test_dfm, test_assembly_tools,
               test_drawing, test_performance):
        guarded(fn)
    for name in list(App.listDocuments()):
        try:
            App.closeDocument(name)
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + "=" * 70)
    print(f"SUMMARY: {RESULTS['pass']} passed, {RESULTS['fail']} failed ({time.time() - t0:.1f}s)")
    for f in RESULTS["failed"]:
        print("  FAILED:", f)
    sys.stdout.flush()
    os._exit(1 if RESULTS["fail"] else 0)


main()
