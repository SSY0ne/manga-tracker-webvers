#!/usr/bin/env python3
"""Tests for mylist.js: reading a MyAnimeList export or a Manga Tracker file into My list.

    python3 -m unittest -v test_mylist

The code is JavaScript, so it runs in a JavaScript engine (node, or macOS's jsc); the tests are skipped if
neither is installed. Everything read from a file is untrusted, so most tests feed it bad input.
"""

import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
JSC = "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
SAMPLE = os.path.join(HERE, "fixtures", "mal_export_sample.xml")


def run_js(body):
    """Run `body` (JavaScript that calls out(value)) with mylist.js loaded; return the value as Python data."""
    path = os.path.join(HERE, "mylist.js")
    prelude = "var out = function (x) { (typeof print === 'function' ? print : console.log)(JSON.stringify(x)); };"
    if shutil.which("node"):
        script = f"const vm = require('vm'), fs = require('fs'); vm.runInThisContext(fs.readFileSync({json.dumps(path)}, 'utf8')); {prelude} {body}"
        cmd = ["node", "-e", script]
    elif os.path.exists(JSC):
        cmd = [JSC, "-e", f"load({json.dumps(path)}); {prelude} {body}"]
    else:
        raise unittest.SkipTest("no JavaScript engine (node or jsc)")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError((r.stderr or r.stdout)[:500])
    return json.loads(r.stdout.strip().splitlines()[-1])


def js_str(text):
    return json.dumps(text)


class MalExport(unittest.TestCase):
    def parse(self, wanted, text=None):
        text = open(SAMPLE, encoding="utf-8").read() if text is None else text
        return run_js(f"out(parseMalExport({js_str(text)}, {json.dumps(wanted)}));")

    def test_it_keeps_only_the_wanted_statuses_with_chapters_read(self):
        r = self.parse(["Reading"])
        self.assertEqual(r["rows"], [{"mal": 13, "status": "Reading", "read": 1180}, {"mal": 2, "status": "Reading", "read": 376}])

    def test_all_statuses(self):
        r = self.parse(["Reading", "Plan to Read", "Completed", "Dropped", "On-Hold"])
        self.assertEqual([row["mal"] for row in r["rows"]], [13, 2, 656, 28, 114745])

    def test_a_series_listed_twice_counts_once_with_the_first_row(self):
        rows = self.parse(["Reading"])["rows"]
        self.assertEqual([r["read"] for r in rows if r["mal"] == 13], [1180])

    def test_a_title_with_angle_brackets_and_quotes_does_not_break_the_next_rows(self):
        self.assertEqual(self.parse(["Completed"])["rows"], [{"mal": 28, "status": "Completed", "read": 84}])

    def test_something_that_is_not_an_export_is_refused(self):
        self.assertIn("MyAnimeList export", self.parse(["Reading"], "<html><body>hello</body></html>")["error"])
        self.assertIn("MyAnimeList export", self.parse(["Reading"], "")["error"])

    def test_no_matching_status_says_so(self):
        self.assertIn("No manga with those statuses", self.parse(["On-Hold"])["error"])

    def test_an_anime_export_has_no_manga_rows(self):
        anime = "<myanimelist><anime><series_animedb_id>1</series_animedb_id><my_status>Watching</my_status></anime></myanimelist>"
        self.assertIn("manga export, not anime", self.parse(["Watching", "Reading"], anime)["error"])

    def test_rows_without_a_usable_id_are_dropped(self):
        xml = "<myanimelist><manga><manga_mangadb_id>0</manga_mangadb_id><my_status>Reading</my_status></manga><manga><manga_mangadb_id>abc</manga_mangadb_id><my_status>Reading</my_status></manga><manga><manga_mangadb_id>7</manga_mangadb_id><my_status>Reading</my_status></manga></myanimelist>"
        self.assertEqual(self.parse(["Reading"], xml)["rows"], [{"mal": 7, "status": "Reading", "read": 0}])


class MineFile(unittest.TestCase):
    def parse(self, text):
        return run_js(f"out(parseMineFile({js_str(text)}));")

    def test_an_exported_file_is_read_back(self):
        data = {"app": "manga-tracker", "version": 1, "series": [{"id": 30013, "title": "One Piece", "read": 1180}, {"id": 30002, "title": "Berserk", "read": 0}]}
        r = self.parse(json.dumps(data))
        self.assertEqual(r["items"], data["series"])
        self.assertEqual(r["invalid"], 0)

    def test_a_bare_list_works_too(self):
        self.assertEqual(self.parse(json.dumps([{"id": 1, "title": "A"}]))["items"], [{"id": 1, "title": "A", "read": 0}])

    def test_bad_json_and_wrong_shapes_are_refused(self):
        self.assertIn("isn't valid JSON", self.parse("{not json")["error"])
        self.assertIn("doesn't look like", self.parse(json.dumps({"hello": 1}))["error"])
        self.assertIn("doesn't look like", self.parse(json.dumps("a string"))["error"])
        self.assertIn("empty", self.parse(json.dumps({"series": []}))["error"])

    def test_hostile_entries_are_dropped_or_clamped(self):
        series = [
            {"id": "7", "title": "string id"}, {"id": -3, "title": "negative"}, {"id": 1.5, "title": "fraction"},
            {"id": 4, "title": "   "}, {"id": 5}, None, 12, {"id": 9, "title": "ok", "read": -4},
            {"id": 10, "title": "x" * 5000, "read": 1e308}, {"id": 11, "title": "<img src=x onerror=alert(1)>", "read": "12"},
        ]
        r = self.parse(json.dumps({"series": series}))
        by_id = {i["id"]: i for i in r["items"]}
        self.assertEqual(sorted(by_id), [9, 10, 11])
        self.assertEqual(by_id[9]["read"], 0)            # negative: 0
        self.assertEqual(len(by_id[10]["title"]), 200)   # long title: cut
        self.assertEqual(by_id[10]["read"], 100000)      # absurd number: capped
        self.assertEqual(by_id[11]["read"], 0)           # a string is not a number
        self.assertEqual(by_id[11]["title"], "<img src=x onerror=alert(1)>")  # kept as text: the page only ever shows it as text
        self.assertEqual(r["invalid"], 7)

    def test_duplicate_ids_count_once(self):
        r = self.parse(json.dumps([{"id": 1, "title": "A", "read": 3}, {"id": 1, "title": "A again", "read": 9}]))
        self.assertEqual(r["items"], [{"id": 1, "title": "A", "read": 3}])

    def test_nothing_usable_is_an_error(self):
        self.assertIn("Nothing usable", self.parse(json.dumps([{"id": "x"}]))["error"])


class Merge(unittest.TestCase):
    def merge(self, existing, items, mx=500):
        return run_js(f"var ex = {json.dumps(existing)}; var r = mergeIntoMine(ex, {json.dumps(items)}, {mx}); out({{r: r, ex: ex}});")

    def test_new_series_are_added_waiting_for_their_data(self):
        r = self.merge([], [{"id": 1, "title": "A", "read": 5}])["r"]
        self.assertEqual(r["added"], [{"id": 1, "title": "A", "read": 5, "pending": True}])
        self.assertEqual((r["already"], r["skipped"]), (0, 0))

    def test_a_series_already_there_is_left_alone(self):
        out = self.merge([{"id": 1, "title": "Mine", "read": 40}], [{"id": 1, "title": "Theirs", "read": 99}])
        self.assertEqual(out["r"]["added"], [])
        self.assertEqual(out["r"]["already"], 1)
        self.assertEqual((out["ex"][0]["title"], out["ex"][0]["read"]), ("Mine", 40))  # not overwritten

    def test_progress_is_filled_in_only_when_you_had_none(self):
        out = self.merge([{"id": 1, "title": "Mine", "read": 0}], [{"id": 1, "title": "Theirs", "read": 99}])
        self.assertEqual(out["ex"][0]["read"], 99)

    def test_the_list_stops_at_the_limit(self):
        r = self.merge([{"id": 1, "title": "A", "read": 0}], [{"id": i, "title": f"S{i}", "read": 0} for i in range(2, 8)], mx=4)["r"]
        self.assertEqual([a["id"] for a in r["added"]], [2, 3, 4])
        self.assertEqual(r["skipped"], 3)


class Export(unittest.TestCase):
    def test_the_export_holds_only_what_cannot_be_looked_up_again(self):
        r = run_js('out(buildMineExport([{id: 1, title: "A", read: 7, cover: "x", chapter: "9", sources: [1]}, {id: 2, title: "B"}], "2026-11-06T12:00:00Z"));')
        self.assertEqual(r["series"], [{"id": 1, "title": "A", "read": 7}, {"id": 2, "title": "B", "read": 0}])
        self.assertEqual((r["app"], r["version"], r["exportedAt"]), ("manga-tracker", 1, "2026-11-06T12:00:00Z"))

    def test_an_export_survives_the_trip_back_in(self):
        r = run_js('var e = buildMineExport([{id: 30013, title: "One Piece", read: 1180}], "t"); out(parseMineFile(JSON.stringify(e)));')
        self.assertEqual(r["items"], [{"id": 30013, "title": "One Piece", "read": 1180}])


if __name__ == "__main__":
    unittest.main()
