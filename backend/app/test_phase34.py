"""Phase 3 / 4 pure-Python parts: materials and cost, BOM exports, plan heuristics, tool groups, version tree.

Run from backend/:  venv\\Scripts\\python -m unittest discover -s app -t .
"""

import io
import unittest
import zipfile

from app import cad_service, materials, reports
from app.agent import plans, runner, tools


class MaterialsTest(unittest.TestCase):
    def test_resolve(self):
        self.assertEqual(materials.resolve("aluminum"), "aluminium_6061")
        self.assertEqual(materials.resolve("Stainless steel 304"), "stainless_304")
        self.assertEqual(materials.resolve("al 6061"), "aluminium_6061")
        self.assertEqual(materials.resolve("PLA"), "pla")
        with self.assertRaises(ValueError):
            materials.resolve("unobtainium")

    def test_mass(self):
        self.assertAlmostEqual(materials.mass_g(60000, "steel_1018"), 472.2)  # 60 cm³ × 7.87 g/cm³

    def test_cost(self):
        c = materials.cost_estimate("cnc", 60000, [100, 60, 10], "aluminium_6061", features=8, quantity=10)
        self.assertEqual(c["stock_mm"], [104, 64, 14])
        self.assertGreater(c["per_part"], c["material_cost"])
        self.assertAlmostEqual(c["total"], c["per_part"] * 10, delta=0.05)  # total uses the unrounded unit cost
        self.assertIn("estimate", c["note"])
        p = materials.cost_estimate("print3d", 60000, [100, 60, 10], "pla")
        self.assertAlmostEqual(p["print_hours"], 60 * 0.45 / 12, places=2)
        with self.assertRaises(ValueError):
            materials.cost_estimate("print3d", 1000, [10, 10, 10], "steel_1018")


class ReportsTest(unittest.TestCase):
    GROUPS = [{"parts": ["B2", "B3", "B4", "B5"], "labels": ["LEG-1", "LEG-2", "LEG-3", "LEG-4"], "volume": 8000.0,
               "size": [80, 10, 10], "quantity": 4},
              {"parts": ["B1"], "labels": ["TOP"], "volume": 200000.0, "size": [200, 100, 10], "quantity": 1}]

    def test_rows(self):
        rows = reports.bom_rows(self.GROUPS, {"B1": "Oak", "B2": "Steel"}, {"B1": 150.0, "B2": 62.96})
        self.assertEqual([(r["item"], r["description"], r["quantity"]) for r in rows], [(1, "TOP", 1), (2, "LEG", 4)])
        self.assertAlmostEqual(rows[1]["total_mass_g"], 251.84)

    def test_csv_and_xlsx(self):
        rows = reports.bom_rows(self.GROUPS, {}, {})
        text = reports.bom_csv(rows).decode("utf-8-sig")
        self.assertTrue(text.startswith("Item,Part IDs,Description,Quantity"))
        self.assertIn("B2 B3 B4 B5,LEG,4", text)
        data = reports.bom_xlsx(rows)
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertIn("xl/worksheets/sheet1.xml", z.namelist())
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("<t>LEG</t>", sheet)
        self.assertIn('<c r="D3"><v>4</v></c>', sheet)


class PlansTest(unittest.TestCase):
    def test_is_large(self):
        self.assertFalse(plans.is_large([{"tool": "resize_hole", "args": {"hole": "H1", "diameter": 8}}]))
        self.assertTrue(plans.is_large([{"tool": "shell_object", "args": {"thickness": 2}}]))
        self.assertTrue(plans.is_large([{"tool": "resize_hole", "args": {}}] * 3))
        self.assertTrue(plans.is_large([{"tool": "remove_features", "args": {"features": ["H1", "H2", "H3"]}}]))
        self.assertTrue(plans.is_large([{"tool": "add_primitive", "args": {"operation": "replace"}}]))

    def test_label_and_words(self):
        self.assertEqual(plans.label({"tool": "set_dimension", "args": {"dimension": "P1-P2", "value": 120}}),
                         "set_dimension(dimension=P1-P2, value=120)")
        for w in ("apply", "Yes", "go ahead", "do it."):
            self.assertTrue(runner._APPLY_WORDS.match(w), w)
        for w in ("cancel", "No"):
            self.assertTrue(runner._CANCEL_WORDS.match(w), w)
        self.assertIsNone(runner._APPLY_WORDS.match("apply a 2 mm chamfer"))
        self.assertEqual(runner._PREVIEW.match("preview P5-P6 = 120").group(1), "P5-P6 = 120")


class CompoundRequestTest(unittest.TestCase):
    def test_is_compound(self):
        self.assertTrue(runner.is_compound("Make the plate 150 mm long, add two 5 mm holes and round the corners R5"))
        self.assertTrue(runner.is_compound("Drill a 6 mm hole at 20, 20 then chamfer its entry 1 mm"))
        self.assertFalse(runner.is_compound("Make the plate 150 mm long"))
        self.assertFalse(runner.is_compound("Make all four holes 8 mm"))
        self.assertFalse(runner.is_compound("What is the mass and the centre of gravity?"))


class PlanRetryTest(unittest.TestCase):
    def test_keeps_steps_before_the_failure(self):
        pending = {"ok": False, "error": "step 3 (fillet_edges(radius=5)) failed: Fillet R5.0 failed",
                   "steps": [{"tool": "set_dimension", "args": {"dimension": "P5-P6", "value": 150}},
                             {"tool": "add_holes", "args": {"face": "P2", "diameter": 5}},
                             {"tool": "fillet_edges", "args": {"radius": 5}}]}
        kept, note = runner._plan_retry(pending)
        self.assertEqual([k["tool"] for k in kept], ["set_dimension", "add_holes"])
        self.assertIn("step 3 onwards", note)
        self.assertIn("set_dimension(dimension=P5-P6, value=150)", note)


class UnverifiedClaimsTest(unittest.TestCase):
    def test_guard(self):
        g = runner._guard_unverified_claims
        self.assertTrue(g("- ✓ P5-P6 = 150.000 mm (plate length increased)", [], {}).startswith("⚠ No change was made"))
        self.assertTrue(g("H3 was resized to 8 mm", [], {}).startswith("⚠"))
        self.assertEqual(g("The plate is 100 mm long and has 8 holes.", [], {}), "The plate is 100 mm long and has 8 holes.")
        self.assertEqual(g("- ✓ H3 diameter = 8.000", [{"ok": True}], {}), "- ✓ H3 diameter = 8.000")  # edits ran
        self.assertEqual(g("Preview ready ✓", [], {"plan": {"id": "x"}}), "Preview ready ✓")


class ToolGroupsTest(unittest.TestCase):
    def names(self, groups):
        return {t["function"]["name"] for t in tools.tools_for({"info": {"features": {"hole": [1]}}}, groups)}

    def test_groups(self):
        base = self.names([])
        self.assertTrue({"propose_plan", "ask_user", "load_tools", "lock_dimension"} <= base)
        self.assertFalse({"mirror_object", "dfm_check"} & base)
        mod = self.names(["modeling"])
        self.assertTrue({"mirror_object", "shell_object", "pattern_feature"} <= mod)
        self.assertNotIn("lock_dimension", mod)  # steps aside while a group is loaded…
        self.assertIn("lock_dimension", self.names(["modeling", "more"]))  # …and comes back with "more"
        self.assertTrue({"dfm_check", "bill_of_materials", "make_drawing"} <= self.names(["analysis"]))

    def test_keywords(self):
        self.assertEqual(tools.groups_for_message("Mirror H3 about the middle"), ["modeling"])
        self.assertEqual(tools.groups_for_message("What does it weigh in aluminium?"), ["analysis"])
        self.assertEqual(tools.groups_for_message("make it 120 long"), [])


class VersionTreeTest(unittest.TestCase):
    def test_tree(self):
        meta = {"versions": [{"v": 0}, {"v": 1}, {"v": 2}]}
        cad_service._ensure_tree(meta)
        self.assertEqual([v["parent"] for v in meta["versions"]], [None, 0, 1])
        meta["versions"].append({"v": 3, "parent": 1})  # an edit after undoing v2: a branch
        self.assertEqual(sorted(v["v"] for v in cad_service._children(meta, 1)), [2, 3])
        with self.assertRaises(cad_service.CadError):
            cad_service._version(meta, 9)


if __name__ == "__main__":
    unittest.main()
