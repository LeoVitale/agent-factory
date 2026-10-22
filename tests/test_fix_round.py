"""A Fix round that ends without a new commit.

  python3 -m unittest tests.test_fix_round
"""
import unittest

from tests.helpers import load_engine, project


class FixRoundTest(unittest.TestCase):

    def setUp(self):
        project("fix-round-")
        self.f = load_engine()
        self.f.require_clean_and_pushed = lambda r: "same-head"
        self.f.notify = lambda *a, **k: None
        self.finals = []
        self.f.check_final = lambda r: self.finals.append(r["stage"])
        self.rec = {"identifier": "ENG-1", "stage": "fix", "fix_from": "same-head", "round": 1, "worktree": "/w"}

    def test_no_commit_with_green_ci_goes_to_final(self):
        self.f.pr_checks = lambda r, h: {"pass": ["CI"], "fail": [], "pending": []}
        self.f.after_fix(self.rec, {"status": "done"})
        self.assertEqual(self.rec["stage"], "final")
        self.assertEqual(self.finals, ["final"])

    def test_no_commit_with_red_or_pending_ci_stops(self):
        for checks in ({"pass": [], "fail": ["CI (failure)"], "pending": []},
                       {"pass": [], "fail": [], "pending": ["CI"]},
                       {"pass": [], "fail": [], "pending": []}):
            self.f.pr_checks = lambda r, h, c=checks: c
            with self.subTest(checks=checks), self.assertRaises(self.f.Stop):
                self.f.after_fix(dict(self.rec), {"status": "done"})
        self.assertEqual(self.finals, [])


if __name__ == "__main__":
    unittest.main()
