"""Unit parsing and formatting (app/units.py).

Run from backend/:  venv\\Scripts\\python -m unittest discover -s app -t .
"""

import unittest

from app import units


class ParseTest(unittest.TestCase):
    def test_lengths(self):
        cases = {
            "120": 120.0, "120mm": 120.0, "120 mm": 120.0, "12.5 cm": 125.0, "0.12 m": 120.0, "0.5m": 500.0,
            '1/4"': 6.35, "1/4 in": 6.35, "1/4in": 6.35, "3 1/2 in": 88.9, "3 1/2\"": 88.9, "2.5 inch": 63.5,
            "2 inches": 50.8, "1 ft": 304.8, "1'": 304.8, ".5 in": 12.7, "-5 mm": -5.0, "1e1 mm": 10.0,
            "250 um": 0.25, "1/64 in": 25.4 / 64,
        }
        for text, want in cases.items():
            self.assertAlmostEqual(units.parse_length(text), want, places=9, msg=text)

    def test_default_unit(self):
        self.assertAlmostEqual(units.parse_length("2", "in"), 50.8)
        self.assertAlmostEqual(units.parse_length("2 mm", "in"), 2.0)  # an explicit unit wins
        self.assertAlmostEqual(units.to_mm(3, "in"), 76.2)
        self.assertAlmostEqual(units.to_mm(3), 3.0)
        self.assertAlmostEqual(units.to_mm("3 in"), 76.2)

    def test_angles(self):
        for text, want in {"30": 30.0, "30°": 30.0, "30 deg": 30.0, "30 degrees": 30.0, "0.5 rad": 28.64788975654116,
                           "-15°": -15.0}.items():
            self.assertAlmostEqual(units.parse_angle(text), want, places=9, msg=text)
        with self.assertRaises(units.UnitError):
            units.parse_angle("30 mm")
        with self.assertRaises(units.UnitError):
            units.parse_length("30°")

    def test_value_kind(self):
        self.assertEqual(units.parse_value("120"), (120.0, "number"))
        self.assertEqual(units.parse_value("2 in"), (50.8, "length"))
        self.assertEqual(units.parse_value("45°"), (45.0, "angle"))

    def test_sizes(self):
        self.assertEqual(units.parse_size("Ø8"), ("diameter", 8.0))
        self.assertEqual(units.parse_size("⌀ 8 mm"), ("diameter", 8.0))
        self.assertEqual(units.parse_size("dia 1/4\""), ("diameter", 6.35))
        self.assertEqual(units.parse_size("diameter = 10"), ("diameter", 10.0))
        self.assertEqual(units.parse_size("R4"), ("radius", 4.0))
        self.assertEqual(units.parse_size("r 2.5"), ("radius", 2.5))
        self.assertEqual(units.parse_size("radius 3 mm"), ("radius", 3.0))
        self.assertEqual(units.parse_size("D12"), ("diameter", 12.0))

    def test_rejects(self):
        for bad in ("abc", "12 parsecs", "", "1/0 in", "mm", "1/2/3", "12 mm 3"):
            with self.assertRaises(units.UnitError, msg=bad):
                units.parse_length(bad)
        with self.assertRaises(units.UnitError):
            units.to_mm(True)


class FormatTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(units.fmt_length(120), "120.000 mm")
        self.assertEqual(units.fmt_length(120, "mm", 2), "120.00 mm")
        self.assertEqual(units.fmt_length(25.4, "in", 3), "1.0000 in")
        self.assertEqual(units.fmt_length(-0.00001, "mm", 3), "0.000 mm")
        self.assertEqual(units.fmt_angle(135), "135.00°")
        self.assertEqual(units.fmt_value([1, 2.5, 3], "length", "mm", 1), "[1.0, 2.5, 3.0] mm")
        self.assertEqual(units.fmt_value(True), "True")


if __name__ == "__main__":
    unittest.main()
