"""Standard hole sizes (app/standards.py).

Run from backend/:  venv\\Scripts\\python -m unittest discover -s app -t .
"""

import unittest

from app import standards


class ResolveTest(unittest.TestCase):
    def test_clearance(self):
        self.assertEqual(standards.resolve("M6", "clearance")["diameter"], 6.6)
        self.assertEqual(standards.resolve("M8", "clearance", "fine")["diameter"], 8.4)
        self.assertEqual(standards.resolve("m8", "clearance", "coarse")["diameter"], 10.0)
        self.assertEqual(standards.resolve("M12", "clearance", "medium")["diameter"], 13.5)
        self.assertIn("ISO 273", standards.resolve("M6")["name"])

    def test_tap(self):
        self.assertEqual(standards.resolve("M6", "tap")["diameter"], 5.0)
        self.assertEqual(standards.resolve("M8", "tap")["diameter"], 6.8)
        self.assertEqual(standards.resolve("M12", "tap")["diameter"], 10.2)
        self.assertEqual(standards.resolve("M8x1", "tap")["diameter"], 7.0)
        self.assertEqual(standards.resolve("M8 x 1.25", "tap")["diameter"], 6.8)  # the coarse pitch, named
        self.assertEqual(standards.resolve("M10x1.25", "tapped")["thread"], "M10x1.25")
        with self.assertRaises(ValueError):
            standards.resolve("M8x0.3", "tap")

    def test_counterbore_and_countersink(self):
        cb = standards.resolve("M6", "counterbore")
        self.assertEqual((cb["diameter"], cb["counterbore_diameter"], cb["counterbore_depth"]), (6.6, 11.0, 6.5))
        cs = standards.resolve("M5", "csk")
        self.assertEqual((cs["diameter"], cs["countersink_diameter"], cs["countersink_angle"]), (5.5, 11.2, 90.0))
        with self.assertRaises(ValueError):
            standards.resolve("M7", "counterbore")
        with self.assertRaises(ValueError):
            standards.resolve("M6", "weld")
        with self.assertRaises(ValueError):
            standards.resolve("6mm", "clearance")


class IdentifyTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(standards.identify({"diameter": 6.6})[0], "M6 clearance (normal)")
        self.assertEqual(standards.identify({"diameter": 9.0})[0], "M8 clearance (normal)")
        self.assertIn("M6 tap drill", standards.identify({"diameter": 5.0}))
        self.assertIn('1/4" drill', standards.identify({"diameter": 6.35}))
        self.assertEqual(standards.identify({"diameter": 5.1}), ["Ø5.1 standard drill"])
        self.assertEqual(standards.identify({"diameter": 7.77}), [])
        self.assertEqual(standards.identify({"diameter": 21.0}),
                         ["M18 clearance (coarse)", "M20 clearance (fine)", "M24 tap drill"])

    def test_counterbore_and_countersink(self):
        h = {"diameter": 6.6, "counterbore": {"diameter": 11.0, "depth": 6.5}}
        self.assertEqual(standards.identify(h)[0], "M6 socket head counterbore (ISO 4762)")
        h = {"diameter": 6.6, "counterbore": {"diameter": 11.0, "depth": 5.0}}
        self.assertIn("depth 5 (std 6.5)", standards.identify(h)[0])
        h = {"diameter": 5.5, "countersinks": [{"outer_diameter": 11.2, "angle_deg": 90.0}]}
        self.assertEqual(standards.identify(h)[0], "M5 countersunk screw (ISO 10642)")
        h = {"diameter": 5.5, "countersinks": [{"outer_diameter": 11.2, "angle_deg": 82.0}]}
        self.assertEqual(standards.identify(h)[0], "M5 clearance (normal)")

    def test_annotate(self):
        info = {"features": {"hole": [{"id": "H1", "diameter": 6.6}, {"id": "H2", "diameter": 7.77, "standard": "old"}]}}
        standards.annotate(info)
        self.assertEqual(info["features"]["hole"][0]["standard"], "M6 clearance (normal)")
        self.assertNotIn("standard", info["features"]["hole"][1])
        self.assertIsNone(standards.annotate(None))


if __name__ == "__main__":
    unittest.main()
