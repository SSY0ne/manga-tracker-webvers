#!/usr/bin/env python3
"""Tests for reference.py (Wikipedia chapter lists) and reference_counts.json. No network.

    python3 -m unittest -v test_reference
"""

import json
import os
import re
import unittest
from unittest import mock

import reference

HERE = os.path.dirname(os.path.abspath(__file__))


class ChapterNumbers(unittest.TestCase):
    def test_a_numbered_list_continues_from_its_start(self):
        wt = '| ChapterListCol2 = \n{{Numbered list|start=5\n|{{nihongo|"Eyeballs"|眼球|"Gankyū"}}\n|{{nihongo|"Target"|標的|"Hyōteki"}}\n}}'
        self.assertEqual(reference.chapter_numbers(wt), [5.0, 6.0])

    def test_a_link_with_a_pipe_inside_an_item_is_still_one_item(self):
        wt = "{{Numbered list|start=1\n|[[Light Yagami|Light]] meets L\n|{{nihongo|\"L\"|L|\"Eru\"}}\n}}"
        self.assertEqual(reference.chapter_numbers(wt), [1.0, 2.0])

    def test_bullet_lines_with_the_number_printed(self):
        wt = 'ChapterList = \n* 424. "The Lost Agent"\n* 425. "A Day Without Melodies"\n* 426. "The Starter 2"\n'
        self.assertEqual(reference.chapter_numbers(wt), [424.0, 425.0, 426.0])

    def test_a_chapter_number_field(self):
        self.assertEqual(reference.chapter_numbers("{{Chapter list\n|ChapterNumber=12\n|ChapterTitle=x\n}}"), [12.0])

    def test_text_that_only_looks_like_a_number_is_not_read(self):
        wt = "* Volume 3 was released\n* 12 volumes in all\nSee also: 2006 film. * 5. maybe\n|ChapterNumber=Extra\n"
        self.assertEqual(reference.chapter_numbers(wt), [])

    def test_several_lists_are_all_read(self):
        wt = "{{Numbered list|start=1\n|a\n|b\n}}\n{{Numbered list|start=3\n|c\n}}"
        self.assertEqual(sorted(reference.chapter_numbers(wt)), [1.0, 2.0, 3.0])


class Lookup(unittest.TestCase):
    def fake(self, pages):
        """A fake Wikipedia: `pages` = {title: wikitext}."""
        def get(params, pause=None):
            if params.get("list") == "search":
                return {"query": {"search": [{"title": t} for t in pages] + [{"title": "Bleach (manga)"}]}}
            return {"parse": {"wikitext": pages[params["page"]]}}
        return get

    def lst(self, start, n):
        return "{{Numbered list|start=%d\n%s\n}}" % (start, "\n".join(f"|item {i}" for i in range(n)))

    def test_the_highest_numbered_chapter_across_split_pages(self):
        pages = {
            "List of Bleach chapters (1–187)": self.lst(1, 187),
            "List of Bleach chapters (188–423)": self.lst(188, 236),
            "List of Bleach chapters (424–686)": self.lst(424, 263),
        }
        with mock.patch.object(reference, "_get", self.fake(pages)):
            r = reference.lookup(["Bleach"])
        self.assertEqual((r["count"], r["entries"]), (686, 686))
        self.assertEqual(len(r["pages"]), 3)
        self.assertTrue(all(u.startswith("https://en.wikipedia.org/wiki/List_of_Bleach") for u in r["urls"]))

    def test_only_list_of_chapters_pages_of_that_series_are_used(self):
        pages = {"List of Bleach characters": self.lst(1, 50), "List of Naruto chapters": self.lst(1, 700)}
        with mock.patch.object(reference, "_get", self.fake(pages)):
            self.assertIsNone(reference.lookup(["Bleach"]))

    def test_a_page_with_no_chapter_numbers_gives_nothing(self):
        with mock.patch.object(reference, "_get", self.fake({"List of Bleach chapters": "just prose"})):
            self.assertIsNone(reference.lookup(["Bleach"]))

    def test_an_extra_with_a_decimal_number_is_not_the_highest_whole_chapter(self):
        pages = {"List of Bleach chapters": self.lst(1, 10) + "\n|ChapterNumber=10.5"}
        with mock.patch.object(reference, "_get", self.fake(pages)):
            self.assertEqual(reference.lookup(["Bleach"])["count"], 10)


class ReferenceFile(unittest.TestCase):
    def test_the_saved_file_is_well_formed(self):
        data = json.load(open(os.path.join(HERE, "reference_counts.json"), encoding="utf-8"))
        self.assertGreaterEqual(len(data), 40)
        found = 0
        for key, v in data.items():
            self.assertTrue(key.isdigit(), key)
            self.assertTrue(v.get("title"), key)
            if v.get("count") is None:
                continue
            found += 1
            self.assertIsInstance(v["count"], int, v["title"])
            self.assertTrue(0 < v["count"] <= 5000, f"{v['title']}: {v['count']}")
            self.assertTrue(v["pages"] and v["urls"], v["title"])
            self.assertTrue(all(u.startswith("https://en.wikipedia.org/wiki/") for u in v["urls"]), v["title"])
            self.assertRegex(v["retrieved"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertGreaterEqual(found, 25)

    def test_the_web_checked_extras_cases_it_exists_for(self):
        data = {v["title"]: v["count"] for v in json.load(open(os.path.join(HERE, "reference_counts.json"), encoding="utf-8")).values()}
        self.assertEqual(data["Bleach"], 686)
        self.assertEqual(data["Initial D"], 719)
        self.assertEqual(data["Fullmetal Alchemist"], 108)


if __name__ == "__main__":
    unittest.main()
