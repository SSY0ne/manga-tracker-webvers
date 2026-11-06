#!/usr/bin/env python3
"""Checks on the page's JavaScript that the Python tests can't see.

    python3 -m unittest -v test_js

Why this file exists: an edit once split an `if / else if` chain in app.js. Every Python test stayed green,
but the page's script could not even be parsed, so the published site was blank. These checks catch that
class of mistake: a syntax error, and an element id the script asks for that the page does not have.
Needs `node` or macOS's `jsc`; the syntax check is skipped (with a message) if neither is installed.
"""

import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"


def syntax_error(path):
    """None if the file parses, else the engine's complaint. Returns "skip" if there is no engine."""
    if shutil.which("node"):
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        return None if r.returncode == 0 else (r.stderr or r.stdout).strip()[:400]
    if os.path.exists(JSC):
        script = f'var src = read("{path}"); try {{ new Function(src); print("OK"); }} catch (e) {{ print("SYNTAX ERROR: " + e); }}'
        r = subprocess.run([JSC, "-e", script], capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        return None if out.endswith("OK") else out[:400]
    return "skip"


class PageScript(unittest.TestCase):
    def test_the_page_scripts_parse(self):
        for name in ("app.js", "mylist.js"):
            with self.subTest(name):
                err = syntax_error(os.path.join(HERE, name))
                if err == "skip":
                    self.skipTest("no JavaScript engine (node or jsc) to check the syntax with")
                self.assertIsNone(err, f"{name} does not parse, so the published page would be blank: {err}")

    def test_the_checker_really_catches_the_bug_that_shipped(self):
        broken = os.path.join(HERE, "fixtures", "_broken_example.js.txt")
        err = syntax_error(broken)
        if err == "skip":
            self.skipTest("no JavaScript engine")
        self.assertIsNotNone(err, "the syntax check let a known-broken script through")

    def test_every_element_the_script_asks_for_exists(self):
        js = open(os.path.join(HERE, "app.js"), encoding="utf-8").read() + open(os.path.join(HERE, "mylist.js"), encoding="utf-8").read()
        html = open(os.path.join(HERE, "index.html"), encoding="utf-8").read()
        asked = set(re.findall(r'getElementById\("([^"$]+)"\)', js))
        in_page = set(re.findall(r'\bid="([^"]+)"', html))
        made_by_script = set(re.findall(r'\.id = "([^"]+)"', js))
        for_tabs = {f"{kind}-{t}" for kind in ("tabbtn", "tab") for t in re.findall(r'"([a-z]+)"', re.search(r"const TAB_ORDER = \[(.*?)\]", js).group(1))}
        missing = sorted(asked - in_page - made_by_script - for_tabs)
        self.assertEqual(missing, [], f"app.js asks for ids that index.html does not have: {missing}")


if __name__ == "__main__":
    unittest.main()
