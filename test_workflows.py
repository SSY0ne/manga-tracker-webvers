#!/usr/bin/env python3
"""Checks on the GitHub workflow files, because a mistake in one is silent until the day it matters.

    python3 -m unittest -v test_workflows

An earlier version of check.yml had a colon-and-space inside plain text in one line: GitHub could not read the
file, so the daily bot did not run, and nothing said so. These tests parse the files (with PyYAML or, failing
that, Ruby) and check the settings the bot's reliability depends on.
"""

import glob
import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS = os.path.join(HERE, ".github", "workflows")


def load(path):
    """Parse a YAML file into Python data, or return None when no YAML parser is available."""
    try:
        import yaml  # PyYAML (present on GitHub's runners)

        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ImportError:
        pass
    if shutil.which("ruby"):
        r = subprocess.run(
            ["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.load_file(ARGV[0]))", path],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise AssertionError(f"{os.path.basename(path)} does not parse as YAML: {r.stderr.strip()[:300]}")
        return json.loads(r.stdout)
    return None


def trigger(wf):
    # YAML 1.1 reads the key `on` as the boolean True (PyYAML keeps True, Ruby's JSON turns it into "true")
    return wf.get("on") or wf.get(True) or wf.get("true")


class Workflows(unittest.TestCase):
    def setUp(self):
        self.files = sorted(glob.glob(os.path.join(WORKFLOWS, "*.yml")) + glob.glob(os.path.join(HERE, ".github", "dependabot.yml")))
        if load(self.files[0]) is None:
            self.skipTest("no YAML parser (PyYAML or Ruby)")

    def test_every_workflow_file_parses(self):
        for path in self.files:
            with self.subTest(os.path.basename(path)):
                self.assertIsInstance(load(path), dict)

    def check_yml(self):
        return load(os.path.join(WORKFLOWS, "check.yml"))

    def test_the_daily_check_is_scheduled_and_can_be_run_by_hand(self):
        on = trigger(self.check_yml())
        self.assertTrue(on["schedule"][0]["cron"])
        self.assertIn("workflow_dispatch", on)

    def test_a_hung_run_is_cut_off_and_two_runs_never_overlap(self):
        wf = self.check_yml()
        job = wf["jobs"]["check"]
        self.assertLessEqual(job["timeout-minutes"], 30)
        self.assertEqual(wf["concurrency"]["group"], "daily-check")
        self.assertFalse(wf["concurrency"]["cancel-in-progress"])  # the second one waits, it doesn't kill the first

    def test_a_failed_run_triggers_the_alert_step(self):
        steps = self.check_yml()["jobs"]["check"]["steps"]
        alert = [s for s in steps if "--alert" in s.get("run", "")]
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert[0]["if"], "failure()")
        self.assertIn("DISCORD_WEBHOOK_URL", alert[0]["env"])

    def test_the_state_is_pushed_with_a_retry(self):
        save = next(s for s in self.check_yml()["jobs"]["check"]["steps"] if s.get("name", "").startswith("Save state"))
        self.assertIn("git pull --rebase", save["run"])
        self.assertIn("for attempt", save["run"])
        self.assertEqual(self.check_yml()["permissions"]["contents"], "write")

    def test_the_watchdog_ping_is_optional_and_never_fails_the_run(self):
        steps = self.check_yml()["jobs"]["check"]["steps"]
        ping = next(s for s in steps if s.get("name", "").startswith("Tell the watchdog"))
        self.assertEqual(ping["if"], "success()")
        self.assertIn('[ -n "$HEALTHCHECK_URL" ]', ping["run"])  # no secret set: it does nothing
        self.assertIn("|| echo", ping["run"])                      # a failing ping is ignored

    def test_every_test_file_runs_on_github(self):
        tests_yml = load(os.path.join(WORKFLOWS, "tests.yml"))
        command = " ".join(s.get("run", "") for s in tests_yml["jobs"]["test"]["steps"])
        for path in glob.glob(os.path.join(HERE, "test_*.py")):
            module = os.path.splitext(os.path.basename(path))[0]
            self.assertIn(module, command, f"{module}.py is not run by .github/workflows/tests.yml")

    def test_dependabot_watches_the_actions(self):
        d = load(os.path.join(HERE, ".github", "dependabot.yml"))
        self.assertEqual(d["updates"][0]["package-ecosystem"], "github-actions")


if __name__ == "__main__":
    unittest.main()
