#!/usr/bin/env python3
"""Tests that replay RECORDED source answers (fixtures/) through the current rules, offline.

    python3 -m unittest -v test_fixtures

Two things are checked (see corpus.py for the idea):
  1. Every fixture still gives the output recorded for it. If a rule change makes this fail, run
     `python3 devtools.py replay` to see exactly which series changed and how. If the new output
     is better, accept it with `python3 devtools.py replay --update`; if not, the rule is wrong.
  2. The truths marked "enforced" in fixtures/truth.json (what the web says) still hold.
"""

import unittest

import corpus


class Fixtures(unittest.TestCase):
    def test_there_are_fixtures_to_replay(self):
        self.assertGreaterEqual(len(corpus.load_all()), 40)

    def test_every_fixture_gives_its_recorded_output(self):
        for fx in corpus.load_all():
            with self.subTest(fx["title"]):
                _, snap = corpus.replay(fx)
                changed = corpus.diff(fx["golden"], snap)  # includes which sources "fit" (plausible)
                self.assertEqual(changed, {}, f"{fx['title']}: a rule changed its mind. Run: python3 devtools.py replay")

    def test_enforced_truths_from_the_web_hold(self):
        truth = corpus.load_truth()
        fixtures = {str(fx["id"]): fx for fx in corpus.load_all()}
        checked = 0
        for al_id, t in truth.items():
            if not t.get("enforced"):
                continue
            self.assertIn(al_id, fixtures, f"{t['title']} has a truth but no fixture")
            with self.subTest(t["title"]):
                result, _ = corpus.replay(fixtures[al_id])
                ok, why = corpus.check_truth(t, result)
                self.assertTrue(ok, f"{t['title']}: {why}. Source: {t['source']}")
            checked += 1
        self.assertGreaterEqual(checked, 10)

    def test_every_truth_names_its_source(self):
        for al_id, t in corpus.load_truth().items():
            self.assertTrue(t.get("source"), f"{t.get('title')} has no source")


if __name__ == "__main__":
    unittest.main()
