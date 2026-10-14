"""Pure-Python tests for the LLM-free command parser and the prompt summary budget.

Run from backend/:  venv\\Scripts\\python -m unittest app.agent.test_fastpath
"""

import json
import unittest

from app.agent import fastpath, prompt
from app.agent.runner import _highlight_ids


class FastPathTest(unittest.TestCase):
    def test_dimension(self):
        self.assertEqual(fastpath.parse("P5-P6 = 120"), ("set_dimension", {"dimension": "P5-P6", "value": 120.0}))
        self.assertEqual(fastpath.parse("p5 - p6 = 120.5 mm"),
                         ("set_dimension", {"dimension": "P5-P6", "value": 120.5}))
        self.assertEqual(fastpath.parse("H3-P5 = 12 mm symmetric"),
                         ("set_dimension", {"dimension": "H3-P5", "value": 12.0, "anchor": "symmetric"}))
        self.assertEqual(fastpath.parse("set P1-P2 to 25 anchor end"),
                         ("set_dimension", {"dimension": "P1-P2", "value": 25.0, "anchor": "end"}))

    def test_hole(self):
        self.assertEqual(fastpath.parse("H2 Ø8"), ("resize_hole", {"hole": "H2", "diameter": 8.0}))
        self.assertEqual(fastpath.parse("h2 diameter 8.5 mm"), ("resize_hole", {"hole": "H2", "diameter": 8.5}))
        self.assertEqual(fastpath.parse("H12 dia = 6"), ("resize_hole", {"hole": "H12", "diameter": 6.0}))

    def test_pattern(self):
        self.assertEqual(fastpath.parse("PAT1 pcd 80"), ("edit_hole_pattern", {"pattern": "PAT1", "pcd": 80.0}))
        self.assertEqual(fastpath.parse("pat2 count 8"), ("edit_hole_pattern", {"pattern": "PAT2", "count": 8}))
        self.assertEqual(fastpath.parse("PAT1 spacing = 50 mm"),
                         ("edit_hole_pattern", {"pattern": "PAT1", "spacing": 50.0}))

    def test_natural_language_goes_to_llm(self):
        for text in ("make H2 8 mm", "P5-P6 = 120 and fillet the edges", "make it longer", "H2", "",
                     "what is P1-P2?", "P5-P6 = abc"):
            self.assertIsNone(fastpath.parse(text), text)

    def test_units(self):
        self.assertEqual(fastpath.parse("P5-P6 = 4.5 in"), ("set_dimension", {"dimension": "P5-P6", "value": 114.3}))
        self.assertEqual(fastpath.parse("P5-P6 = 12 cm"), ("set_dimension", {"dimension": "P5-P6", "value": 120.0}))
        self.assertEqual(fastpath.parse("P5-P6 = 4.5", unit="in"), ("set_dimension", {"dimension": "P5-P6", "value": 114.3}))
        self.assertEqual(fastpath.parse("P5-P6 = 120 mm", unit="in"), ("set_dimension", {"dimension": "P5-P6", "value": 120.0}))
        name, args = fastpath.parse('H2 Ø1/4"')
        self.assertEqual(name, "resize_hole")
        self.assertAlmostEqual(args["diameter"], 6.35)
        self.assertEqual(fastpath.parse("H2 R4"), ("resize_hole", {"hole": "H2", "diameter": 8.0}))
        self.assertEqual(fastpath.parse("H2 radius 3 mm"), ("resize_hole", {"hole": "H2", "diameter": 6.0}))
        name, args = fastpath.parse("PAT1 pcd 3 1/2 in")
        self.assertAlmostEqual(args["pcd"], 88.9)
        self.assertEqual(fastpath.parse("PAT1 start_angle 30°"), ("edit_hole_pattern", {"pattern": "PAT1", "start_angle": 30.0}))
        self.assertIsNone(fastpath.parse("PAT1 count 2.5"))

    def test_angles_and_radial(self):
        self.assertEqual(fastpath.parse("P3∠P7 = 60"), ("set_dimension", {"dimension": "P3∠P7", "value": 60.0}))
        self.assertEqual(fastpath.parse("P3<P7 = 60°", unit="in"), ("set_dimension", {"dimension": "P3∠P7", "value": 60.0}))
        self.assertEqual(fastpath.parse("P3-P7 = 60°"), ("set_dimension", {"dimension": "P3-P7", "value": 60.0}))
        # a bare number on a pair the session knows is an angle is in degrees, not the display unit
        self.assertEqual(fastpath.parse("P3-P7 = 60", unit="in", angle_dim=lambda d: d == "P3-P7"),
                         ("set_dimension", {"dimension": "P3-P7", "value": 60.0}))
        self.assertIsNone(fastpath.parse("P3∠P7 = 2 in"))
        self.assertEqual(fastpath.parse("C1 Ø30"), ("set_dimension", {"dimension": "C1.Ø", "value": 30.0}))
        self.assertEqual(fastpath.parse("C1.Ø = 30"), ("set_dimension", {"dimension": "C1.Ø", "value": 30.0}))
        self.assertEqual(fastpath.parse("R2 R3"), ("set_dimension", {"dimension": "R2.R", "value": 3.0}))
        self.assertEqual(fastpath.parse("H1◎H2 = 0"), ("set_dimension", {"dimension": "H1◎H2", "value": 0.0}))

    def test_standards_locks_datums(self):
        self.assertEqual(fastpath.parse("H3 M8 clearance"),
                         ("set_hole_standard", {"hole": "H3", "size": "M8", "type": "clearance"}))
        self.assertEqual(fastpath.parse("make H3 an M8 clearance hole fine"),
                         ("set_hole_standard", {"hole": "H3", "size": "M8", "type": "clearance", "fit": "fine"}))
        self.assertEqual(fastpath.parse("tap H3 for M6"), ("set_hole_standard", {"hole": "H3", "size": "M6", "type": "tap"}))
        self.assertEqual(fastpath.parse("H3 M8x1 tap"), ("set_hole_standard", {"hole": "H3", "size": "M8x1", "type": "tap"}))
        self.assertEqual(fastpath.parse("H3 M6 cbore"), ("set_hole_standard", {"hole": "H3", "size": "M6", "type": "counterbore"}))
        self.assertEqual(fastpath.parse("H3 M5 countersink"),
                         ("set_hole_standard", {"hole": "H3", "size": "M5", "type": "countersink"}))
        self.assertEqual(fastpath.parse("M8 clearance", ["H3"]),
                         ("set_hole_standard", {"hole": "H3", "size": "M8", "type": "clearance"}))
        self.assertEqual(fastpath.parse("lock P5-P6"), ("lock_dimension", {"target": "P5-P6"}))
        self.assertEqual(fastpath.parse("lock H1 centred X"), ("lock_dimension", {"target": "H1", "mode": "centered", "axis": "X"}))
        self.assertEqual(fastpath.parse("lock", ["P1-P2"]), ("lock_dimension", {"target": "P1-P2"}))
        self.assertEqual(fastpath.parse("lock centred", ["H1"]), ("lock_dimension", {"target": "H1", "mode": "centered"}))
        self.assertEqual(fastpath.parse("unlock all"), ("unlock_dimension", {"target": "ALL"}))
        self.assertEqual(fastpath.parse("locks"), ("list_locks", {}))
        self.assertEqual(fastpath.parse("datum A = P1"), ("set_datums", {"A": "P1"}))
        self.assertEqual(fastpath.parse("set datum b P4"), ("set_datums", {"B": "P4"}))
        for text in ("make H3 an M8", "lock the length", "H3 M8 bolt"):
            self.assertIsNone(fastpath.parse(text), text)

    def test_phase2_features(self):
        self.assertEqual(fastpath.parse("SL1 width 10"), ("edit_slot", {"slot": "SL1", "width": 10.0}))
        self.assertEqual(fastpath.parse("SL1 depth 0.25 in"), ("edit_slot", {"slot": "SL1", "depth": 6.35}))
        self.assertEqual(fastpath.parse("PK1 depth 10"), ("edit_pocket", {"pocket": "PK1", "depth": 10.0}))
        self.assertEqual(fastpath.parse("PK1 corner R3"), ("edit_pocket", {"pocket": "PK1", "corner_radius": 3.0}))
        self.assertEqual(fastpath.parse("CH1 3"), ("edit_chamfer", {"chamfer": "CH1", "size": 3.0}))
        self.assertEqual(fastpath.parse("CH1 to fillet R2"), ("edit_chamfer", {"chamfer": "CH1", "to_fillet": 2.0}))
        self.assertEqual(fastpath.parse("fillet E12 R2"), ("fillet_edges", {"radius": 2.0, "edges": [{"edge": "E12"}]}))
        self.assertEqual(fastpath.parse("chamfer E3, E4 1"),
                         ("chamfer_edges", {"size": 1.0, "edges": [{"edge": "E3"}, {"edge": "E4"}]}))
        self.assertEqual(fastpath.parse("R2", ["E5"]), ("fillet_edges", {"radius": 2.0, "edges": [{"edge": "E5"}]}))
        self.assertEqual(fastpath.parse("width 12", ["SL2"]), ("edit_slot", {"slot": "SL2", "width": 12.0}))
        self.assertEqual(fastpath.parse("H3 thread M8x1.25 depth 12"),
                         ("set_thread", {"hole": "H3", "size": "M8x1.25", "depth": 12.0}))
        self.assertEqual(fastpath.parse("H3 thread none"), ("set_thread", {"hole": "H3", "size": "none"}))
        self.assertEqual(fastpath.parse("wall thickness"), ("measure_wall_thickness", {}))
        self.assertEqual(fastpath.parse("PAT1 count_u 3"), ("edit_hole_pattern", {"pattern": "PAT1", "count_u": 3}))
        self.assertIsNone(fastpath.parse("R2"))  # without an edge selected, R2 alone is a fillet ID, not a command

    def test_phase4_analysis(self):
        self.assertEqual(fastpath.parse("material all steel"), ("set_material", {"part": "all", "material": "steel"}))
        self.assertEqual(fastpath.parse("material B2 aluminium 6061"),
                         ("set_material", {"part": "B2", "material": "aluminium 6061"}))
        self.assertEqual(fastpath.parse("mass"), ("mass_properties", {}))
        self.assertEqual(fastpath.parse("dfm print"), ("dfm_check", {"process": "print3d"}))
        self.assertEqual(fastpath.parse("dfm"), ("dfm_check", {"process": "cnc"}))
        self.assertEqual(fastpath.parse("bom"), ("bill_of_materials", {}))
        self.assertEqual(fastpath.parse("drawing"), ("make_drawing", {}))
        self.assertEqual(fastpath.parse("cost cnc 10"), ("cost_estimate", {"process": "cnc", "quantity": 10}))
        self.assertEqual(fastpath.parse("cost print"), ("cost_estimate", {"process": "print3d", "quantity": 1}))
        self.assertIsNone(fastpath.parse("what does it cost to make the slot wider"))

    def test_reply_quotes_verified(self):
        text = fastpath.reply({"ok": True, "message": "P5-P6: 100 → 120 mm (start)",
                               "verified": [{"id": "P5-P6", "field": "dimension", "value": 120.0, "status": "ok"}]})
        self.assertIn("120.000", text)
        self.assertIn("Could not", fastpath.reply({"ok": False, "error": "boom"}))


def _big_info(n=400):
    holes = [{"id": f"H{i}", "kind": "hole", "diameter": 6.0, "depth": 10.0, "through": True, "drill_tip": False,
              "entry": [i, 0, 10], "direction": [0, 0, -1], "position": "central", "object": "A"} for i in range(n)]
    holes.append({"id": "HX", "kind": "hole", "diameter": 11.0, "depth": 6.5, "through": False, "drill_tip": False,
                  "entry": [0, 0, 10], "direction": [0, 0, -1], "counterbore_of": "H1", "object": "A"})
    dims = [{"id": f"H{i}-P1", "type": "edge_distance", "value": float(i), "label": "x", "object": "A",
             "refs": [f"H{i}", "P1"], "axis": [1, 0, 0]} for i in range(n)]
    dims.append({"id": "P1-P2", "type": "extent", "value": 100.0, "label": "overall X size", "object": "A",
                 "refs": ["P1", "P2"], "axis": [1, 0, 0]})
    return {"objects": [{"name": "A", "label": "A", "type": "Part::Feature", "volume": 1.0,
                         "bbox": {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]}}],
            "bbox": {"min": [0, 0, 0], "max": [1, 1, 1], "size": [1, 1, 1]},
            "counts": {"hole": n + 1}, "features": {"hole": holes}, "dimensions": dims,
            "patterns": [{"id": "PAT1", "type": "linear", "count": 5, "spacing": 40.0, "hole_diameter": 6.0,
                          "holes": ["H1", "H2"], "direction": [1, 0, 0], "object": "A", "start": [0, 0, 0]}]}


class SummaryTest(unittest.TestCase):
    def test_budget_and_priorities(self):
        out = prompt.summarize_model(_big_info())
        self.assertLessEqual(len(json.dumps(out, separators=(",", ":"))), prompt.SUMMARY_CHARS)
        self.assertEqual(out["dimensions"][0]["id"], "P1-P2")  # extents come first
        self.assertEqual(out["patterns"][0]["id"], "PAT1")
        self.assertTrue(all("HX" not in g["ids"] for g in out["hole_groups"]))  # counterbore not a separate hole
        self.assertIn("dimension", out["truncated_counts"])

    def test_highlight_ids(self):
        meta = {"info": _big_info(5)}
        recs = [{"ok": True, "args": {"dimension": "P1-P2", "value": 3}, "new_features": [],
                 "verified": [{"id": "H3", "status": "ok"}]}]
        ids = _highlight_ids(meta, recs, "Moved H4 and PAT1; P9 does not exist")
        self.assertEqual(ids, ["P1-P2", "H3", "H4", "PAT1"])

    def test_selection_fills_in_the_id(self):
        self.assertEqual(fastpath.parse("Ø8", ["H2"]), ("resize_hole", {"hole": "H2", "diameter": 8.0}))
        self.assertEqual(fastpath.parse("120", ["P5-P6"]), ("set_dimension", {"dimension": "P5-P6", "value": 120.0}))
        self.assertEqual(fastpath.parse("= 12 symmetric", ["H3-P5"]),
                         ("set_dimension", {"dimension": "H3-P5", "value": 12.0, "anchor": "symmetric"}))
        self.assertEqual(fastpath.parse("count 8", ["PAT1"]), ("edit_hole_pattern", {"pattern": "PAT1", "count": 8}))
        self.assertEqual(fastpath.parse("H3 Ø8", ["H2"]), ("resize_hole", {"hole": "H3", "diameter": 8.0}))  # explicit ID wins
        self.assertIsNone(fastpath.parse("Ø8", ["H2", "H3"]))  # several selected: let the LLM do one call per hole
        self.assertIsNone(fastpath.parse("make it bigger", ["H2"]))

    def test_describe_selection(self):
        info = _big_info(5)
        info["objects"] = [{"id": "B1", "name": "Body", "label": "Plate", "volume": 10.0,
                            "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}]
        text = prompt.with_selection("make it 8", info, ["H3", "B1", "ZZ9"])
        self.assertIn("[Selected in the viewer:", text)
        self.assertIn("H3: hole", text)
        self.assertIn("B1: part 'Plate'", text)
        self.assertIn("ZZ9: (not in the current model data)", text)
        self.assertEqual(prompt.with_selection("hi", info, []), "hi")


if __name__ == "__main__":
    unittest.main()
