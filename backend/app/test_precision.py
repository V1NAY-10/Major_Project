"""Side-effect report and verification display (app/precision.py), plus the tool-argument unit layer.

Run from backend/:  venv\\Scripts\\python -m unittest discover -s app -t .
"""

import unittest

from app import precision
from app.agent import tools


def _info(x_size=100.0, thick=10.0, hole_d=6.0, edge=10.0, pcd=None):
    info = {
        "dimensions": [
            {"id": "P5-P6", "type": "extent", "value": x_size, "label": "overall X size", "refs": ["P5", "P6"]},
            {"id": "P1-P2", "type": "thickness", "value": thick, "label": "thickness along Z", "refs": ["P1", "P2"]},
            {"id": "H1-P6", "type": "edge_distance", "value": edge, "label": "H1 axis to right face", "refs": ["H1", "P6"]},
            {"id": "P3∠P7", "type": "angle", "value": 135.0, "unit": "deg", "label": "angle", "refs": ["P3", "P7"]},
        ],
        "features": {"hole": [{"id": "H1", "diameter": hole_d, "depth": thick}]},
        "patterns": [],
    }
    if pcd:
        info["patterns"] = [{"id": "PAT1", "type": "circular", "pcd": pcd, "count": 6, "holes": ["H1"]}]
    return info


class SideEffectsTest(unittest.TestCase):
    def test_target_excluded_and_expected_changes(self):
        before, after = _info(), _info(x_size=120.0, edge=30.0)
        se = precision.side_effects(before, after, targets={"P5-P6"}, touched={"P6"})
        self.assertEqual(se["total"], 1)
        row = se["rows"][0]
        self.assertEqual((row["key"], row["before"], row["after"], row["expected"]), ("H1-P6", 10.0, 30.0, True))
        self.assertEqual(se["unexpected"], 0)

    def test_unexpected_change_flagged_first(self):
        before, after = _info(), _info(x_size=120.0, thick=8.0, edge=30.0)
        se = precision.side_effects(before, after, targets={"P5-P6"}, touched={"P6"})
        # the thickness and the through hole's depth changed although neither was touched: both unexpected,
        # listed before the expected change
        self.assertEqual(se["unexpected"], 2)
        self.assertEqual([(r["key"], r["expected"]) for r in se["rows"]],
                         [("H1 depth", False), ("P1-P2", False), ("H1-P6", True)])
        brief = precision.side_effects_brief(se, precision.DEFAULT_SETTINGS)
        self.assertEqual(brief["unexpected_changes"], ["H1 depth 10.000 mm → 8.000 mm", "P1-P2 10.000 mm → 8.000 mm"])

    def test_tolerance_and_removed(self):
        before = _info()
        after = _info(x_size=100.0004)  # within ±0.001: not a change
        self.assertEqual(precision.side_effects(before, after)["total"], 0)
        after = _info()
        after["dimensions"] = [d for d in after["dimensions"] if d["id"] != "H1-P6"]
        se = precision.side_effects(before, after)
        self.assertEqual((se["total"], se["removed"], se["rows"][0]["after"]), (1, 1, None))

    def test_target_keys(self):
        keys = precision.target_keys([{"id": "H3", "field": "diameter"}, {"id": "C1.Ø", "field": "dimension"},
                                      {"id": "PAT1", "field": "pattern"}])
        self.assertTrue({"H3 diameter", "C1.Ø", "C1 diameter", "PAT1 pcd"} <= keys)

    def test_angles_use_angle_tolerance(self):
        before, after = _info(), _info()
        after["dimensions"][3]["value"] = 135.005  # < 0.01°
        self.assertEqual(precision.side_effects(before, after)["total"], 0)
        after["dimensions"][3]["value"] = 135.5
        se = precision.side_effects(before, after)
        self.assertEqual(se["rows"][0]["kind"], "angle")

    def test_dim_refs(self):
        self.assertEqual(precision.dim_refs("P5-P6"), ["P5", "P6"])
        self.assertEqual(precision.dim_refs("P3∠P7"), ["P3", "P7"])
        self.assertEqual(precision.dim_refs("C1.Ø"), ["C1"])
        self.assertEqual(precision.dim_refs("H1 depth"), ["H1"])


class VerificationDisplayTest(unittest.TestCase):
    def test_display(self):
        s = precision.DEFAULT_SETTINGS
        v = precision.describe_verification({"id": "P5-P6", "field": "dimension", "value": 120.0, "achieved": 120.0,
                                             "requested": 120.0, "deviation": 0.0, "within_tolerance": True,
                                             "unit": "mm", "status": "ok"}, s)
        self.assertEqual(v["display"], "✓ P5-P6 = 120.000 mm (Δ 0.0000 mm)")
        inch = {**s, "display_unit": "in"}
        v = precision.describe_verification({"id": "H1", "field": "diameter", "value": 6.35, "achieved": 6.35,
                                             "requested": 6.35, "deviation": 0.0, "within_tolerance": True,
                                             "unit": "mm", "status": "ok"}, inch)
        self.assertTrue(v["display"].startswith("✓ H1 diameter = 0.2500 in"), v["display"])
        v = precision.describe_verification({"id": "P3∠P7", "field": "dimension", "value": 119.98, "achieved": 119.98,
                                             "requested": 120.0, "deviation": 0.02, "within_tolerance": False,
                                             "unit": "deg", "status": "ok"}, s)
        self.assertTrue(v["display"].startswith("⚠ P3∠P7 = 119.98°") and "> ±0.01" in v["display"], v["display"])
        missing = {"id": "H1", "field": "diameter", "status": "missing", "note": "gone"}
        self.assertEqual(precision.describe_verification(missing, s), missing)

    def test_settings_defaults(self):
        st = precision.settings_of({"settings": {"display_unit": "in", "tolerance": {"angle": 0.5}}})
        self.assertEqual((st["display_unit"], st["precision"], st["tolerance"]),
                         ("in", 3, {"length": 0.001, "angle": 0.5}))


class ToolArgumentUnitsTest(unittest.TestCase):
    INFO = {"features": {"plane": [{"id": "P1", "normal": [0, 0, 1]}, {"id": "P2", "normal": [0, 0, -1]},
                                   {"id": "P3", "normal": [0, 0.7071068, 0.7071068]}]}}

    def test_lengths_with_units(self):
        args = tools.coerce_args("resize_hole", {"hole": "H1", "diameter": "1/4 in"})
        self.assertAlmostEqual(args["diameter"], 6.35)
        args = tools.coerce_args("resize_hole", {"hole": "H1", "diameter": 8})
        self.assertEqual(args["diameter"], 8.0)  # numbers from the LLM are mm
        args = tools.coerce_args("edit_hole_pattern", {"pattern": "PAT1", "pcd": "3 in", "start_angle": "0.5 rad"})
        self.assertAlmostEqual(args["pcd"], 76.2)
        self.assertAlmostEqual(args["start_angle"], 28.64788975654116)
        args = tools.coerce_args("add_holes", {"face": "P1", "diameter": 5, "positions": [["1 in", 10, 5], {"b": "15 mm"}]})
        self.assertEqual(args["positions"], [[25.4, 10.0, 5.0], {"B": 15.0}])
        args = tools.coerce_args("move_hole", {"hole": "H1", "to_datum": {"c": "0.5 in"}})
        self.assertEqual(args["to_datum"], {"C": 12.7})

    def test_set_dimension_value(self):
        args = tools.coerce_args("set_dimension", {"dimension": "P1-P2", "value": "2 in"}, self.INFO)
        self.assertAlmostEqual(args["value"], 50.8)
        args = tools.coerce_args("set_dimension", {"dimension": "P1-P2", "value": "2"}, self.INFO, default_unit="in")
        self.assertAlmostEqual(args["value"], 50.8)
        args = tools.coerce_args("set_dimension", {"dimension": "P1-P3", "value": "60"}, self.INFO, default_unit="in")
        self.assertEqual(args["value"], 60.0)  # P1-P3 is an angle: bare numbers are degrees
        with self.assertRaises(Exception):
            tools.coerce_args("set_dimension", {"dimension": "P1∠P3", "value": "2 in"}, self.INFO)
        with self.assertRaises(Exception):
            tools.coerce_args("set_dimension", {"dimension": "P1-P2", "value": "30°"}, self.INFO)

    def test_is_angle_dimension(self):
        self.assertTrue(tools.is_angle_dimension(self.INFO, "P1∠P3"))
        self.assertTrue(tools.is_angle_dimension(self.INFO, "P1-P3"))
        self.assertFalse(tools.is_angle_dimension(self.INFO, "P1-P2"))
        self.assertFalse(tools.is_angle_dimension(self.INFO, "C1.Ø"))

    def test_phase2_arguments(self):
        args = tools.coerce_args("edit_slot", {"slot": "SL1", "width": "3/8 in", "to_datum": {"b": "1 in"}})
        self.assertAlmostEqual(args["width"], 9.525)
        self.assertEqual(args["to_datum"], {"B": 25.4})
        args = tools.coerce_args("edit_pocket", {"pocket": "PK1", "corner_radius": "2 mm", "depth": 5})
        self.assertEqual((args["corner_radius"], args["depth"]), (2.0, 5.0))

    def test_tools_only_for_present_features(self):
        names = lambda feats: {t["function"]["name"] for t in tools.tools_for({"info": {"features": feats}})}  # noqa: E731
        plain = names({"plane": [1]})
        self.assertFalse({"edit_slot", "edit_pocket", "edit_chamfer", "set_thread"} & plain)
        self.assertTrue({"find_edges", "measure_wall_thickness", "fillet_edges"} <= plain)
        self.assertTrue({"edit_slot", "set_thread"} <= names({"slot": [1], "hole": [1]}))

    def test_thread_annotations(self):
        from app import cad_service
        meta = {"threads": {"H2": {"spec": "M6x1", "depth": 8.0, "source": "session"}},
                "info": {"threads": [{"hole": "H1", "spec": "M6x1.0", "depth": 8.0, "source": "FCStd Hole"},
                                     {"hole": "H2", "spec": "M5x0.8", "depth": 6.0, "source": "FCStd Hole"}],
                         "features": {"hole": [{"id": "H1", "diameter": 5.0}, {"id": "H2", "diameter": 5.0},
                                               {"id": "H3", "diameter": 5.0, "thread": {"spec": "stale"}}]}}}
        cad_service.annotate_threads(meta)
        holes = {h["id"]: h for h in meta["info"]["features"]["hole"]}
        self.assertEqual(holes["H1"]["thread"]["spec"], "M6x1.0")       # from the FCStd hole feature
        self.assertEqual(holes["H2"]["thread"]["spec"], "M6x1")         # the session annotation wins
        self.assertNotIn("thread", holes["H3"])                          # stale annotations are cleared

    def test_phase2_snapshot(self):
        before = {"features": {"slot": [{"id": "SL1", "width": 8.0, "length": 30.0, "depth": 10.0}],
                               "chamfer": [{"id": "CH1", "size": 2.0, "angle_deg": 45.0}]}}
        after = {"features": {"slot": [{"id": "SL1", "width": 10.0, "length": 30.0, "depth": 10.0}],
                              "chamfer": [{"id": "CH1", "size": 2.0, "angle_deg": 45.0}]}}
        se = precision.side_effects(before, after, targets={"SL1 width"})
        self.assertEqual(se["total"], 0)
        se = precision.side_effects(before, after)
        self.assertEqual([r["key"] for r in se["rows"]], ["SL1 width"])

    def test_hole_standard_params(self):
        p = tools.hole_standard_params({}, {"hole": "H3", "size": "M6", "type": "counterbore"})
        self.assertEqual((p["diameter"], p["counterbore_diameter"], p["counterbore_depth"], p["remove_countersink"]),
                         (6.6, 11.0, 6.5, True))
        p = tools.hole_standard_params({}, {"hole": "H3", "size": "M8", "type": "clearance", "fit": "fine"})
        self.assertEqual((p["diameter"], p["note"]), (8.4, "M8 clearance (fine, ISO 273)"))


if __name__ == "__main__":
    unittest.main()
