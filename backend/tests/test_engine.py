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

    def apply(self, op, **params):
        res = E.apply(self.sid, self.work, op, params, self.registry)
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
    check("dimensions(feature=…) filters", r["total"] == 2 and all(h["id"] in d["refs"] for d in r["dimensions"]), str(r["dimensions"]))
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
               test_parametric, test_queries, test_performance):
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
