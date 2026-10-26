"""Unit tests for alternatives (propose_options): open-ended detection, option picks, unsent tools, reply text."""

import unittest

from app.agent import plans, runner, tools


class OptionsTest(unittest.TestCase):
    def test_open_ended(self):
        for t in ("make the nose end smoother", "make it lighter", "what are my options to reduce weight",
                  "make the edges look nicer"):
            self.assertTrue(runner.is_open_ended(t), t)
        for t in ("make it 120 long", "make the nose smoother with R5", "fillet E3 R2", "remove H2"):
            self.assertFalse(runner.is_open_ended(t), t)

    def test_option_pick(self):
        for t, key in (("2", "2"), ("option 2", "2"), ("apply option 3", "3"), ("use 1", "1"), ("#2", "2")):
            m = runner._OPTION_PICK.match(t)
            self.assertTrue(m, t)
            self.assertEqual(m.group(2), key)
        for t in ("120", "option twelve", "P5-P6 = 2"):
            self.assertFalse(runner._OPTION_PICK.match(t), t)

    def test_unsent_tools(self):
        sent = tools.tools_for({}, [])
        rejected = ('Tool choice is none, but model called a tool Rejected call: {"name": "edit_tool", '
                    '"arguments": {"name":"draft_faces","arguments":{"faces":["F1"]}}}')
        self.assertEqual([t["function"]["name"] for t in runner._unsent_tools(rejected, sent)], ["draft_faces"])
        self.assertEqual(runner._unsent_tools("set_dimension failed", sent), [])  # already sent

    def test_propose_options_is_always_offered(self):
        names = {t["function"]["name"] for t in tools.tools_for({}, [])}
        self.assertIn("propose_options", names)

    def test_describe_options(self):
        offer = {"question": "Two ways:", "options": [
            {"key": 1, "title": "Round tip", "description": "R3 fillet", "ok": True,
             "records": [{"verified": [{"display": "R1 = 3 mm"}]}]},
            {"key": 2, "title": "Draft", "description": "x", "ok": False, "error": "F1 is not planar", "records": []}]}
        text = plans.describe_options(offer)
        self.assertIn("**1. Round tip**", text)
        self.assertIn("Draft: F1 is not planar", text)
        self.assertIn("Nothing has been changed", text)
        offer["options"][0]["ok"] = False
        offer["options"][0]["error"] = "no edge"
        self.assertIn("None of the approaches works", plans.describe_options(offer))

    def test_gave_up_lists_attempts_once(self):
        text = runner._gave_up([("draft_faces", "F1 is not planar")] * 3)
        self.assertEqual(text.count("draft_faces"), 1)
        self.assertIn("nothing was modified", text)


if __name__ == "__main__":
    unittest.main()
