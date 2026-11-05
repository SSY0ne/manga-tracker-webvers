#!/usr/bin/env python3
"""Tests for the rules that decide what the tracker tells you (no network, no installs).

    python3 -m unittest -v test_rules

These functions hold many thresholds (0.8, 0.5, 2.5x, 3 days...). A small change to one of
them can quietly change what the bot reports, so each rule has a test that says what it must do.
"""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import check
import server


def day(days_ago):
    """ISO timestamp at 00:00 UTC, `days_ago` days back (negative = in the future)."""
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def rel(source, chapter, days_ago, official=False, url=None):
    """A ready-made 'release' candidate (chapter + date), like a source function returns."""
    return {
        "kind": "release", "source": source, "official": official,
        "chapter": str(chapter), "chapterNum": float(chapter), "publishedAt": day(days_ago),
        "url": url or f"https://{source.lower()}.example/", "typicalGapDays": 7,
        "expectedNext": None, "history": [],
    }


def cnt(chapter, status="releasing"):
    """A ready-made 'count' candidate (MangaBaka): a chapter count and a status, no date."""
    return {
        "kind": "count", "source": "MangaBaka", "official": False,
        "chapter": str(chapter) if chapter else None, "chapterNum": float(chapter) if chapter else None,
        "publishedAt": None, "status": status, "url": "https://mangabaka.example/",
        "typicalGapDays": None, "expectedNext": None, "history": [],
    }


class ChapterNumber(unittest.TestCase):
    def test_plain_and_decimal(self):
        self.assertEqual(server.chapter_number("392"), 392.0)
        self.assertEqual(server.chapter_number("12.5"), 12.5)

    def test_ranges_and_extras_use_the_highest_number(self):
        self.assertEqual(server.chapter_number("390-392"), 392.0)
        self.assertEqual(server.chapter_number("222 + Afterword (end)"), 222.0)

    def test_no_number_is_none(self):
        self.assertIsNone(server.chapter_number("Epilogue (end)"))
        self.assertIsNone(server.chapter_number(None))

    def test_fmt_num(self):
        self.assertEqual(server.fmt_num(5.0), "5")
        self.assertEqual(server.fmt_num(5.5), "5.5")


class BuildCandidate(unittest.TestCase):
    def weekly(self, n, skip_week_at=None):
        """n weekly chapters, newest first in time order; optionally one skipped week."""
        pairs, d = [], n * 7 + 7
        for i in range(n):
            if skip_week_at is not None and i == skip_week_at:
                d -= 7
            pairs.append((str(100 + i), day(d), f"https://x/{100 + i}"))
            d -= 7
        return pairs

    def test_weekly_rhythm_survives_a_skipped_week(self):
        cand = server.build_candidate("MangaDex", self.weekly(9, skip_week_at=4), "https://series")
        self.assertEqual(cand["typicalGapDays"], 7)  # breaks only make gaps longer: the short gap wins
        self.assertEqual(cand["chapter"], "108")

    def test_rhythm_is_the_short_gap_even_when_most_gaps_are_long(self):
        # 3 weekly gaps and 6 two-week gaps: the median is 14, but the real rhythm is 7
        gaps, d, pairs = [7, 7, 7, 14, 14, 14, 14, 14, 14], 200, []
        for i in range(len(gaps) + 1):
            pairs.append((str(100 + i), day(d), None))
            if i < len(gaps):
                d -= gaps[i]
        cand = server.build_candidate("MangaDex", pairs, "https://series")
        self.assertEqual(cand["typicalGapDays"], 7)

    def test_too_little_history_means_no_rhythm(self):
        cand = server.build_candidate("MangaDex", self.weekly(3), "https://series")
        self.assertIsNone(cand["typicalGapDays"])
        self.assertIsNone(cand["expectedNext"])

    def test_same_chapter_uploaded_twice_keeps_the_earliest(self):
        pairs = [("10", day(5), "late"), ("10", day(9), "early")]
        cand = server.build_candidate("MangaDex", pairs, "https://series")
        self.assertEqual(cand["publishedAt"], day(9))
        self.assertEqual(cand["url"], "early")

    def test_expected_next_is_published_plus_rhythm(self):
        cand = server.build_candidate("MangaDex", self.weekly(9), "https://series")
        published = datetime.fromisoformat(cand["publishedAt"])
        self.assertEqual(cand["expectedNext"], (published + timedelta(days=7)).date().isoformat())

    def test_nothing_usable_returns_none(self):
        self.assertIsNone(server.build_candidate("MangaDex", [], "https://series"))
        self.assertIsNone(server.build_candidate("MangaDex", [("Epilogue", day(1), None)], "https://series"))


class QuietDays(unittest.TestCase):
    def test_only_releasing_series_can_be_on_break(self):
        self.assertEqual(server.quiet_days("FINISHED", day(500), 7), 0)
        self.assertEqual(server.quiet_days("RELEASING", None, 7), 0)

    def test_weekly_series_flags_after_three_weeks(self):
        self.assertEqual(server.quiet_days("RELEASING", day(20), 7), 0)
        self.assertGreaterEqual(server.quiet_days("RELEASING", day(22), 7), 22)

    def test_monthly_series_flags_after_about_75_days(self):
        self.assertEqual(server.quiet_days("RELEASING", day(70), 30), 0)
        self.assertGreaterEqual(server.quiet_days("RELEASING", day(80), 30), 80)

    def test_unknown_rhythm_falls_back_to_six_weeks(self):
        self.assertEqual(server.quiet_days("RELEASING", day(41), None, fallback_weeks=6), 0)
        self.assertGreaterEqual(server.quiet_days("RELEASING", day(43), None, fallback_weeks=6), 43)

    def test_the_default_fallback_is_six_weeks(self):
        # (someone may override it with the BREAK_WEEKS environment variable on purpose)
        if "BREAK_WEEKS" not in os.environ:
            self.assertEqual(server.FALLBACK_WEEKS, 6)
            self.assertEqual(server.quiet_days("RELEASING", day(41), None), 0)
            self.assertGreaterEqual(server.quiet_days("RELEASING", day(43), None), 43)


class MangaUpdatesScore(unittest.TestCase):
    def score(self, title, names, year):
        return server.mu_score({"title": title, "year": str(year) if year else ""}, names, year)

    def test_identical_title_and_year(self):
        self.assertGreaterEqual(self.score("Blue Lock", ["Blue Lock"], 2018), 0.999)

    def test_bracketed_disambiguation_is_ignored(self):
        self.assertGreaterEqual(self.score("Sweet Home (KIM Carnby)", ["Sweet Home"], 2017), 0.999)

    def test_similar_title_with_matching_year_is_accepted(self):
        s = server.mu_score(
            {"title": "JoJo no Kimyou na Bouken Part 9: The JOJOLands", "year": "2023"},
            ["JoJo no Kimyou na Bouken: The JOJOLands"], 2023)
        self.assertGreaterEqual(s, server.MU_MIN_SCORE)

    def test_same_title_but_wrong_decade_is_rejected(self):
        # two different manga both called "Sweet Home": 1989 vs 2017
        self.assertEqual(server.mu_score({"title": "Sweet Home", "year": "1989"}, ["Sweet Home"], 2017), 0)

    def test_year_within_one_is_allowed_but_two_is_not(self):
        self.assertGreater(server.mu_score({"title": "Berserk", "year": "1990"}, ["Berserk"], 1989), 0)
        self.assertEqual(server.mu_score({"title": "Berserk", "year": "1991"}, ["Berserk"], 1989), 0)

    def test_doujinshi_and_novels_are_rejected(self):
        self.assertEqual(server.mu_score({"title": "Blue Lock", "year": "2018", "type": "Doujinshi"}, ["Blue Lock"], 2018), 0)
        self.assertEqual(server.mu_score({"title": "Blue Lock dj - Alter", "year": "2018"}, ["Blue Lock"], 2018), 0)
        self.assertEqual(server.mu_score({"title": "Blue Lock (Novel)", "year": "2018"}, ["Blue Lock"], 2018), 0)

    def test_unrelated_title_is_rejected(self):
        self.assertEqual(server.mu_score({"title": "Stray Dogs", "year": "2019"}, ["Bungo Stray Dogs Beast"], 2019), 0)

    def test_without_a_year_only_an_identical_title_passes(self):
        self.assertEqual(server.mu_score({"title": "JoJo Part 9: The JOJOLands", "year": ""}, ["JoJo: The JOJOLands"], None), 0)
        self.assertGreater(server.mu_score({"title": "Blue Lock", "year": ""}, ["Blue Lock"], None), 0)


class Reconcile(unittest.TestCase):
    def test_sources_that_agree(self):
        r = server.reconcile([cnt(100), rel("MangaDex", 100, 2), rel("MangaUpdates", 100, 2)], read=90, status="RELEASING")
        self.assertEqual((r["agreement"], r["found"], r["limited"]), ("agree", True, False))
        self.assertEqual(r["source"], "MangaDex")  # tie on date and chapter: MangaDex has the better link

    def test_conflict_keeps_the_freshest_release(self):
        r = server.reconcile([cnt(425), rel("MangaDex", 390, 130), rel("MangaUpdates", 425, 3)], read=422, status="RELEASING")
        self.assertEqual((r["agreement"], r["source"], r["chapter"]), ("conflict", "MangaUpdates", "425"))

    def test_release_feeds_far_behind_the_count_fall_back_to_the_count(self):
        r = server.reconcile([cnt(366), rel("MangaDex", 150, 50), rel("MangaUpdates", 272, 900)], read=367, status="RELEASING")
        self.assertEqual((r["agreement"], r["chapter"], r["source"]), ("count", "366", "MangaBaka"))
        self.assertTrue(r["countOnly"])
        self.assertIsNone(r["publishedAt"])
        self.assertFalse(r["limited"])

    def test_nothing_fits_means_limited_for_a_releasing_series_only(self):
        sources = [rel("MangaDex", 4, 30)]
        self.assertTrue(server.reconcile(sources, read=373, status="RELEASING")["limited"])
        r = server.reconcile(sources, read=373, status="FINISHED")
        self.assertEqual(r["agreement"], "none")
        self.assertFalse(r["limited"])

    def test_read_progress_rule_has_a_sharp_edge_at_80_percent(self):
        # you have read 367 chapters: 80% of that is 293.6, so 293 is too low and 294 fits
        low = server.reconcile([rel("MangaDex", 293, 3)], read=367, status="RELEASING")
        ok = server.reconcile([rel("MangaDex", 294, 3)], read=367, status="RELEASING")
        self.assertEqual(low["agreement"], "none")
        self.assertEqual(ok["agreement"], "single")

    def test_sources_within_three_chapters_agree_but_four_apart_conflict(self):
        near = server.reconcile([rel("MangaDex", 100, 30), rel("MangaUpdates", 103, 3)], read=None, status="RELEASING")
        far = server.reconcile([rel("MangaDex", 100, 30), rel("MangaUpdates", 104, 3)], read=None, status="RELEASING")
        self.assertEqual(near["agreement"], "agree")
        self.assertEqual(far["agreement"], "conflict")

    def test_same_story_by_date_counts_as_agreement(self):
        # Webtoon episode 410 vs manga chapter 425, but released the same day: they agree
        r = server.reconcile([rel("MangaUpdates", 425, 3), rel("Webtoon", 410, 3, official=True)], read=None, status="RELEASING")
        self.assertEqual(r["agreement"], "agree")

    def test_a_date_in_the_future_is_rejected(self):
        r = server.reconcile([rel("MangaDex", 50, -10)], read=None, status="RELEASING")
        self.assertEqual(r["agreement"], "none")

    def test_a_tiny_chapter_number_is_rejected_next_to_a_big_one(self):
        r = server.reconcile([rel("MangaDex", 303, 100), rel("MangaUpdates", 2, 10)], read=None, status="RELEASING")
        self.assertEqual((r["source"], r["agreement"]), ("MangaDex", "single"))

    def test_hiatus_is_reported_from_the_count_source(self):
        r = server.reconcile([cnt(100, status="hiatus"), rel("MangaDex", 100, 3)], read=90, status="RELEASING")
        self.assertTrue(r["hiatus"])

    def test_webtoon_is_exempt_from_the_count_rule(self):
        # Webtoon numbers its episodes differently, so being below the manga count is normal for it
        r = server.reconcile([cnt(425), rel("MangaDex", 300, 3), rel("Webtoon", 300, 3, official=True)], read=None, status="RELEASING")
        plausible = {s["name"]: s["plausible"] for s in r["sources"]}
        self.assertFalse(plausible["MangaDex"])
        self.assertTrue(plausible["Webtoon"])
        self.assertEqual(r["source"], "Webtoon")

    def test_prefers_an_official_reader_link_when_dates_match(self):
        r = server.reconcile(
            [rel("MangaUpdates", 425, 3, url="https://mu"), rel("Webtoon", 410, 3, official=True, url="https://webtoon")],
            read=None, status="RELEASING")
        self.assertEqual(r["source"], "MangaUpdates")
        self.assertEqual(r["url"], "https://webtoon")

    def test_official_link_is_only_borrowed_from_a_source_that_agrees_on_the_date(self):
        # the Webtoon episode is 17 days older, so it is not "the same release": keep MangaUpdates' link
        r = server.reconcile(
            [rel("MangaUpdates", 425, 3, url="https://mu"), rel("Webtoon", 410, 20, official=True, url="https://webtoon")],
            read=None, status="RELEASING")
        self.assertEqual(r["url"], "https://mu")

    def test_a_chapter_below_half_of_the_highest_is_rejected_even_without_other_clues(self):
        r = server.reconcile([rel("MangaDex", 49, 3), rel("MangaUpdates", 100, 40)], read=None, status="RELEASING")
        self.assertEqual({s["name"]: s["plausible"] for s in r["sources"]}, {"MangaDex": False, "MangaUpdates": True})

    def test_no_candidates_at_all(self):
        r = server.reconcile([None, None], read=None, status="RELEASING")
        self.assertEqual((r["agreement"], r["limited"]), ("none", True))


class ChapterInfoIsTheOnlyPlaceOfTheBreakRule(unittest.TestCase):
    """chapter_info() must return quietDays, because the bot and the page both trust it."""

    def info(self, candidates, status, read=None):
        sources = [(f"S{i}", (lambda c: lambda *a: c)(c)) for i, c in enumerate(candidates)]
        server._cache.clear()
        with mock.patch.object(server, "SOURCES", sources), mock.patch.object(server.time, "sleep"):
            return server.chapter_info("1", ["x"], None, read, status)

    def test_quiet_days_come_with_the_answer(self):
        r = self.info([rel("MangaDex", 100, 40)], "RELEASING")  # weekly series, 40 days silent
        self.assertEqual(r["quietDays"], 40)

    def test_finished_series_are_never_on_break(self):
        self.assertEqual(self.info([rel("MangaDex", 100, 400)], "FINISHED")["quietDays"], 0)

    def test_limited_data_makes_no_claim(self):
        # "latest chapter 4" while you have read 373 is bad data: it must not claim a break
        r = self.info([rel("MangaDex", 4, 400)], "RELEASING", read=373)
        self.assertTrue(r["limited"])
        self.assertEqual(r["quietDays"], 0)


class ChapterDelta(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(check.chapter_delta("1196", "1197"), 1.0)
        self.assertEqual(check.chapter_delta("10", "8"), -2.0)

    def test_missing_or_non_numeric_is_none(self):
        self.assertIsNone(check.chapter_delta(None, "5"))
        self.assertIsNone(check.chapter_delta("a", "5"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
