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


def cnt(chapter, status="releasing", source="MangaBaka", ended=None):
    """A ready-made 'count' candidate (MangaBaka, Kitsu, AniList): a chapter count, no date.
    `ended` = the date of the last chapter that Kitsu knows."""
    return {
        "kind": "count", "source": source, "official": False, "ended": ended,
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


class ReconcileAgainstTheWeb(unittest.TestCase):
    """Cases found by checking the real data against the web (Vagabond, Clover, Omniscient Reader...)."""

    def test_highest_chapter_wins_over_a_later_dated_older_chapter(self):
        # Omniscient Reader: MangaUpdates had 311 (May 19), the Webtoon feed 308 but dated two weeks later
        r = server.reconcile([cnt(311), rel("MangaUpdates", 311, 40), rel("Webtoon", 308, 26, official=True)], read=None, status="RELEASING")
        self.assertEqual((r["source"], r["chapter"]), ("MangaUpdates", "311"))

    def test_a_bonus_chapter_does_not_beat_the_real_last_one(self):
        # Oshi no Ko ends at 166; MangaUpdates also lists 166.5 (a bonus)
        r = server.reconcile([cnt(168), rel("MangaDex", 166, 30), rel("MangaUpdates", 166.5, 700)], read=None, status="FINISHED")
        self.assertEqual(r["chapter"], "166")

    def test_a_lone_feed_far_behind_the_count_is_stale(self):
        # Vagabond: MangaDex stopped at 303, MangaBaka and Kitsu both say 327
        r = server.reconcile(
            [cnt(327), cnt(327, source="Kitsu"), rel("MangaDex", 303, 2000), rel("MangaUpdates", 2, 1900)],
            read=None, status="RELEASING")
        self.assertEqual((r["agreement"], r["chapter"]), ("count", "327"))
        self.assertIsNone(r["publishedAt"])

    def test_two_feeds_that_agree_beat_a_count_that_is_higher(self):
        # Berserk: MangaBaka says 401, MangaDex and MangaUpdates both say 386 (the web agrees with them)
        r = server.reconcile([cnt(401), rel("MangaDex", 386, 120), rel("MangaUpdates", 386, 120)], read=None, status="RELEASING")
        self.assertEqual((r["chapter"], r["agreement"]), ("386", "conflict"))

    def test_the_lagging_rule_has_edges_at_5_percent_and_more_than_5_chapters(self):
        def used(n, count):  # which answer wins: the lone MangaDex feed at chapter n, or the count
            r = server.reconcile([cnt(count), rel("MangaDex", n, 5)], read=None, status="RELEASING")
            return "count" if r.get("countOnly") else r["source"]
        self.assertEqual(used(96, 100), "MangaDex")   # 4% behind: still a fit
        self.assertEqual(used(95, 100), "MangaDex")   # exactly 5% behind, and only 5 chapters: kept
        self.assertEqual(used(94, 100), "count")      # 6% and 6 chapters behind: stale
        self.assertEqual(used(190, 200), "MangaDex")  # exactly 5% behind (10 chapters): kept
        self.assertEqual(used(189, 200), "count")     # just past 5%: stale
        self.assertEqual(used(96, 101), "MangaDex")   # 4.95% behind: kept

    def test_a_webtoon_feed_is_never_called_stale(self):
        r = server.reconcile([cnt(425), rel("Webtoon", 300, 3, official=True)], read=None, status="RELEASING")
        self.assertEqual(r["source"], "Webtoon")

    def test_a_count_far_from_the_other_two_is_outvoted(self):
        # Clover: MangaBaka said 487, Kitsu and AniList say 401
        r = server.reconcile([cnt(487), cnt(401, source="Kitsu"), cnt(401, source="AniList")], read=None, status="FINISHED")
        self.assertEqual((r["chapter"], r["agreement"]), ("401", "count"))
        plausible = {s["name"]: s["plausible"] for s in r["sources"]}
        self.assertEqual(plausible, {"MangaBaka": False, "Kitsu": True, "AniList": True})

    def test_one_odd_count_is_outvoted_but_a_small_difference_is_fine(self):
        # Chainsaw Man: Kitsu lists only Part 1 (97), the other two say 232
        r = server.reconcile([cnt(232), cnt(97, source="Kitsu"), cnt(232, source="AniList")], read=None, status="FINISHED")
        self.assertEqual(r["chapter"], "232")
        # Usogui: 541 vs 539 is the usual "do extras count" difference: nobody is outvoted
        r = server.reconcile([cnt(541), cnt(541, source="Kitsu"), cnt(539, source="AniList")], read=None, status="FINISHED")
        self.assertTrue(all(s["plausible"] for s in r["sources"]))

    def test_an_upload_date_after_the_series_ended_is_dropped(self):
        # Tokyo Ghoul:re ended in 2018, but MangaDex's copy was uploaded recently
        r = server.reconcile([cnt(181), cnt(179, source="Kitsu", ended="2018-07-05"), rel("MangaDex", 179, 70)], read=None, status="FINISHED")
        self.assertEqual(r["chapter"], "179")
        self.assertIsNone(r["publishedAt"])
        self.assertTrue(r["dateDropped"])

    def test_a_date_in_the_ending_year_is_kept(self):
        year = datetime.now(timezone.utc).year
        r = server.reconcile([cnt(392), cnt(392, source="Kitsu", ended=f"{year}-04-30"), rel("MangaDex", 392, 30)], read=None, status="FINISHED")
        self.assertIsNotNone(r["publishedAt"])

    def test_the_date_is_kept_when_kitsu_describes_a_different_entry(self):
        # Chainsaw Man: Kitsu's entry (Part 1) ended in 2020, but the 232 chapters really ended this year
        r = server.reconcile([cnt(232), cnt(97, source="Kitsu", ended="2020-12-14"), cnt(232, source="AniList"), rel("MangaUpdates", 232, 30)], read=None, status="FINISHED")
        self.assertIsNotNone(r["publishedAt"])

    def test_the_date_is_kept_while_the_series_is_running(self):
        r = server.reconcile([cnt(100), cnt(100, source="Kitsu", ended="2010-01-01"), rel("MangaDex", 100, 3)], read=None, status="RELEASING")
        self.assertIsNotNone(r["publishedAt"])


class KitsuAndAniListCandidates(unittest.TestCase):
    """No network: the HTTP call is replaced by a canned answer."""

    def test_kitsu_asks_with_the_json_api_media_type_and_reads_count_and_end_date(self):
        # Kitsu answers 406 to "Accept: application/json": that bug failed 18 of 18 lookups once
        seen = {}

        def fake(url, body=None, accept="application/json"):
            seen["accept"], seen["url"] = accept, url
            return {"included": [{"id": "1482", "attributes": {"chapterCount": 327, "status": "finished", "endDate": "2015-05-21"}}]}

        with mock.patch.object(server, "http_json", fake):
            c = server.kitsu_candidate(30656, [], None)
        self.assertEqual(seen["accept"], "application/vnd.api+json")
        self.assertIn("externalId", seen["url"])
        self.assertEqual((c["kind"], c["source"], c["chapter"], c["ended"]), ("count", "Kitsu", "327", "2015-05-21"))

    def test_kitsu_without_a_match_or_a_count_gives_nothing(self):
        with mock.patch.object(server, "http_json", lambda *a, **k: {"included": []}):
            self.assertIsNone(server.kitsu_candidate(1, [], None))
        no_count = {"included": [{"id": "9", "attributes": {"chapterCount": None, "status": "current", "endDate": None}}]}
        with mock.patch.object(server, "http_json", lambda *a, **k: no_count):
            self.assertIsNone(server.kitsu_candidate(1, [], None))

    def test_anilist_count(self):
        with mock.patch.object(server, "http_json", lambda *a, **k: {"data": {"Media": {"chapters": 401}}}):
            c = server.anilist_candidate(44568, [], None)
        self.assertEqual((c["source"], c["chapter"]), ("AniList", "401"))
        with mock.patch.object(server, "http_json", lambda *a, **k: {"data": {"Media": {"chapters": None}}}):
            self.assertIsNone(server.anilist_candidate(30013, [], None))

    def test_the_two_count_sources_are_only_asked_for_finished_series(self):
        asked = []
        fake_sources = [(n, (lambda n: lambda *a: asked.append(n))(n)) for n in ("MangaBaka", "Kitsu", "AniList")]
        with mock.patch.object(server, "SOURCES", fake_sources), mock.patch.object(server, "_cache", {}), mock.patch("time.sleep"):
            server.chapter_info(1, ["x"], status="RELEASING")
            self.assertEqual(asked, ["MangaBaka"])
            asked.clear()
            server._cache.clear()
            server.chapter_info(1, ["x"], status="FINISHED")
            self.assertEqual(asked, ["MangaBaka", "Kitsu", "AniList"])


class MangaDexAnyLanguage(unittest.TestCase):
    """Blue Lock: English stops at 177, Spanish and Portuguese are at 367. No network."""

    def run_candidate(self, rows, aggregate=None, calls=None):
        """rows = (chapter, language, time, externalUrl) in the answer. aggregate = chapter numbers the
        `aggregate` call lists (default: every chapter in the rows)."""
        data = [{"id": f"id{i}", "attributes": {"chapter": ch, "publishAt": when, "translatedLanguage": lang, "externalUrl": ext}}
                for i, (ch, lang, when, ext) in enumerate(rows)]
        numbers = aggregate if aggregate is not None else sorted({r[0] for r in rows})
        agg = {"volumes": {"none": {"volume": "none", "chapters": {n: {"chapter": n} for n in numbers}}}}

        def fake_md_get(path, params):
            if calls is not None:
                calls.append((path, params))
            if path.endswith("/aggregate"):
                return agg
            asked = {v for k, v in params if k == "chapter[]"}  # like the real API: only the chapters asked for
            return {"data": [d for d in data if not asked or d["attributes"]["chapter"] in asked]}

        server._md_id_cache[999] = "uuid-999"
        with mock.patch.object(server, "md_get", fake_md_get):
            return server.mangadex_any_language_candidate(999, ["Blue Lock"], None)

    def test_the_highest_chapter_in_any_language_wins_and_the_earliest_time_is_kept(self):
        c = self.run_candidate([
            ("367", "es-la", "2026-10-31T10:00:00+00:00", None),
            ("367", "pt-br", "2026-10-30T09:00:00+00:00", None),   # same chapter, earlier: the real release is closer to this
            ("177", "en", "2024-05-01T00:00:00+00:00", None),
        ])
        self.assertEqual((c["source"], c["chapter"], c["publishedAt"]), (server.ANY_LANGUAGE, "367", "2026-10-30T09:00:00+00:00"))

    def test_only_english_chapters_get_their_own_link(self):
        other = self.run_candidate([("367", "es-la", "2026-10-31T10:00:00+00:00", None)])
        self.assertEqual(other["url"], "https://mangadex.org/title/uuid-999")  # never a fan translation page
        self.assertFalse(other["official"])
        english = self.run_candidate([("12", "en", "2026-10-31T10:00:00+00:00", None)])
        self.assertTrue(english["url"].startswith("https://mangadex.org/chapter/"))

    def test_it_asks_for_the_top_chapters_in_every_language(self):
        calls = []
        numbers = [str(n) for n in range(1, 41)]  # 40 chapters exist
        self.run_candidate([("40", "es", "2026-10-31T10:00:00+00:00", None)], aggregate=numbers, calls=calls)
        params = [p for path, p in calls if path == "/chapter"][0]  # only /chapter can filter by chapter number
        self.assertIn(("manga", "uuid-999"), params)
        self.assertFalse(any(k.startswith("translatedLanguage") for k, _ in params))
        self.assertEqual([v for k, v in params if k == "chapter[]"], [str(n) for n in range(40, 28, -1)])  # the 12 highest

    def test_old_chapters_uploaded_late_cannot_hide_the_newest_ones(self):
        # a language catching up uploads chapters 1-90 AFTER chapter 420 was posted
        rows = [(str(n), "pt-br", f"2026-10-{20 + n % 9:02d}T00:00:00+00:00", None) for n in range(1, 91)]
        rows.append(("420", "en", "2026-09-06T00:00:00+00:00", None))
        c = self.run_candidate(rows, aggregate=[str(n) for n in range(1, 91)] + ["420"])
        self.assertEqual(c["chapter"], "420")

    def test_a_whole_chapter_beats_its_bonus_decimal_even_if_the_decimal_is_newer(self):
        # Black Clover: 392 is the end, a later 392.5 is a bonus
        c = self.run_candidate([("392", "es", "2026-04-30T00:00:00+00:00", None), ("392.5", "es", "2026-08-16T00:00:00+00:00", None)])
        self.assertEqual(c["chapter"], "392")

    def test_no_rhythm_comes_from_fan_upload_habits(self):
        rows = [(str(n), "es", f"2026-10-{n:02d}T00:00:00+00:00", None) for n in range(1, 9)]
        c = self.run_candidate(rows)
        self.assertIsNone(c["typicalGapDays"])
        self.assertIsNone(c["expectedNext"])

    def test_an_empty_series_gives_nothing(self):
        self.assertIsNone(self.run_candidate([], aggregate=[]))


class AnyLanguageFallback(unittest.TestCase):
    """When the fallback may be used and when its answer is thrown away."""

    def fallback(self, count, chapter, days_ago):
        candidates = [cnt(count)] if count else []
        cand = rel(server.ANY_LANGUAGE, chapter, days_ago)
        return server.any_language_fallback(candidates, lambda: cand)

    def test_a_recent_chapter_close_to_the_count_is_used(self):
        self.assertIsNotNone(self.fallback(366, 367, 5))   # Blue Lock

    def test_a_late_upload_is_not_a_release(self):
        self.assertIsNone(self.fallback(327, 327, 2900))   # Vagabond's last chapter, uploaded in 2018

    def test_the_age_limit_has_an_edge_at_120_days(self):
        self.assertIsNotNone(self.fallback(112, 112, 120))
        self.assertIsNone(self.fallback(112, 112, 121))

    def test_a_different_numbering_far_above_the_count_is_refused(self):
        self.assertIsNone(self.fallback(240, 313, 10))     # One-Punch Man
        self.assertIsNotNone(self.fallback(240, 269, 10))  # 1.1 x 240 + 5 = 269 is still allowed
        self.assertIsNone(self.fallback(240, 270, 10))

    def test_without_a_count_nothing_can_be_checked(self):
        self.assertIsNone(self.fallback(None, 367, 5))

    def test_nothing_found_means_nothing_added(self):
        self.assertIsNone(server.any_language_fallback([cnt(100)], lambda: None))

    def run_info(self, status, md_candidate, fallback=None):
        """chapter_info with fake sources: MangaBaka says 366; the others are given."""
        sources = [("MangaBaka", lambda *a: cnt(366)), ("MangaDex", lambda *a: md_candidate)]
        calls = []

        def fake_fallback(*a):
            calls.append(1)
            return fallback

        with mock.patch.object(server, "SOURCES", sources), mock.patch.object(server, "_cache", {}), \
                mock.patch("time.sleep"), mock.patch.object(server, "mangadex_any_language_candidate", fake_fallback):
            return server.chapter_info(1, ["x"], status=status, read=None), calls

    def test_a_running_series_with_only_a_count_tries_every_language(self):
        r, calls = self.run_info("RELEASING", rel("MangaDex", 150, 900), fallback=rel(server.ANY_LANGUAGE, 367, 5))
        self.assertEqual(len(calls), 1)
        self.assertEqual((r["chapter"], r["source"]), ("367", server.ANY_LANGUAGE))

    def test_the_fallback_is_not_used_when_a_normal_source_fits(self):
        r, calls = self.run_info("RELEASING", rel("MangaDex", 366, 3), fallback=rel(server.ANY_LANGUAGE, 367, 1))
        self.assertEqual(calls, [])
        self.assertEqual(r["chapter"], "366")

    def test_the_fallback_is_not_used_for_a_finished_series(self):
        r, calls = self.run_info("FINISHED", rel("MangaDex", 150, 900), fallback=rel(server.ANY_LANGUAGE, 367, 5))
        self.assertEqual(calls, [])
        self.assertTrue(r.get("countOnly"))

    def test_a_refused_fallback_keeps_the_count(self):
        r, calls = self.run_info("RELEASING", rel("MangaDex", 150, 900), fallback=None)
        self.assertEqual(len(calls), 1)
        self.assertEqual((r["chapter"], r["agreement"]), ("366", "count"))

    def test_a_failing_fallback_never_breaks_the_answer(self):
        sources = [("MangaBaka", lambda *a: cnt(366)), ("MangaDex", lambda *a: rel("MangaDex", 150, 900))]

        def boom(*a):
            raise RuntimeError("MangaDex is down")

        with mock.patch.object(server, "SOURCES", sources), mock.patch.object(server, "_cache", {}), \
                mock.patch("time.sleep"), mock.patch.object(server, "mangadex_any_language_candidate", boom):
            r = server.chapter_info(1, ["x"], status="RELEASING")
        self.assertEqual(r["chapter"], "366")


class DormantRule(unittest.TestCase):
    """No chapter for 5 years: the author has probably stopped (Nana since 2009, Vagabond since 2015)."""

    YEARS = 5 * 365

    def test_the_limit_is_five_years(self):
        self.assertEqual(server.dormant_days("RELEASING", day(self.YEARS - 1)), 0)
        self.assertEqual(server.dormant_days("RELEASING", day(self.YEARS)), self.YEARS)  # exactly 5 years: dormant
        self.assertEqual(server.dormant_days("HIATUS", day(self.YEARS + 100)), self.YEARS + 100)

    def test_only_a_series_still_listed_as_running_can_be_dormant(self):
        for status in ("FINISHED", "CANCELLED", "NOT_YET_RELEASED", None):
            self.assertEqual(server.dormant_days(status, day(9000)), 0, status)

    def test_without_a_date_nothing_is_claimed(self):
        self.assertEqual(server.dormant_days("RELEASING", None), 0)

    def test_the_limit_can_be_changed(self):
        self.assertGreater(server.dormant_days("RELEASING", day(800), years=2), 0)
        self.assertEqual(server.dormant_days("RELEASING", day(800), years=3), 0)

    def test_an_old_upload_date_is_evidence_only_when_it_is_the_counted_last_chapter(self):
        stale = rel(server.ANY_LANGUAGE, 327, 2900)
        self.assertEqual(server.last_upload_evidence([cnt(327)], stale), stale["publishedAt"])   # Vagabond
        self.assertIsNone(server.last_upload_evidence([cnt(327)], rel(server.ANY_LANGUAGE, 400, 2900)))  # another numbering, far above
        self.assertIsNone(server.last_upload_evidence([cnt(327)], rel(server.ANY_LANGUAGE, 100, 2900)))  # an old chapter
        self.assertIsNone(server.last_upload_evidence([], stale))                                          # no count to check
        self.assertIsNone(server.last_upload_evidence([cnt(327)], None))

    def info(self, status, sources, fallback=None):
        with mock.patch.object(server, "SOURCES", sources), mock.patch.object(server, "_cache", {}), \
                mock.patch("time.sleep"), mock.patch.object(server, "mangadex_any_language_candidate", lambda *a: fallback):
            return server.chapter_info(1, ["x"], status=status)

    def test_a_release_feed_with_a_date_years_ago_makes_it_dormant(self):
        r = self.info("RELEASING", [("MangaBaka", lambda *a: cnt(84, status="hiatus")), ("MangaUpdates", lambda *a: rel("MangaUpdates", 84, 6000))])
        self.assertTrue(r["dormant"])
        self.assertGreaterEqual(r["dormantDays"], 6000)

    def test_a_series_that_is_merely_on_break_is_not_dormant(self):
        r = self.info("RELEASING", [("MangaBaka", lambda *a: cnt(420, status="hiatus")), ("MangaUpdates", lambda *a: rel("MangaUpdates", 420, 60))])
        self.assertFalse(r["dormant"])
        self.assertEqual(r["dormantDays"], 0)

    def test_a_count_only_series_is_dormant_when_the_refused_late_upload_proves_it(self):
        # Vagabond: nothing fits, MangaDex has chapter 327 "dated" 2018 in Spanish
        sources = [("MangaBaka", lambda *a: cnt(327)), ("MangaDex", lambda *a: rel("MangaDex", 303, 2000))]
        r = self.info("RELEASING", sources, fallback=rel(server.ANY_LANGUAGE, 327, 2900))
        self.assertTrue(r["dormant"])
        self.assertTrue(r["countOnly"])

    def test_the_same_series_without_that_evidence_is_not_declared_dormant(self):
        sources = [("MangaBaka", lambda *a: cnt(327)), ("MangaDex", lambda *a: rel("MangaDex", 303, 2000))]
        self.assertFalse(self.info("RELEASING", sources, fallback=None)["dormant"])

    def test_a_finished_series_is_never_dormant(self):
        r = self.info("FINISHED", [("MangaBaka", lambda *a: cnt(84)), ("MangaUpdates", lambda *a: rel("MangaUpdates", 84, 6000))])
        self.assertFalse(r["dormant"])

    def test_limited_data_claims_nothing(self):
        r = self.info("RELEASING", [("MangaDex", lambda *a: rel("MangaDex", 4, 30))])
        r2 = server.reconcile([rel("MangaDex", 4, 30)], read=373, status="RELEASING")
        self.assertTrue(r2["limited"])
        self.assertFalse(r["dormant"])


class DormancyInTheBot(unittest.TestCase):
    def test_the_transitions(self):
        self.assertEqual(check.dormancy_change({"dormant": False}, True), "dormant")
        self.assertEqual(check.dormancy_change({}, True), "dormant_silent")     # first run with the rule: no announcement
        self.assertEqual(check.dormancy_change({"dormant": True}, True), None)  # still dormant: nothing new
        self.assertEqual(check.dormancy_change({"dormant": True}, False), "revived")
        self.assertIsNone(check.dormancy_change({"dormant": False}, False))
        self.assertIsNone(check.dormancy_change({}, False))

    def test_a_dormant_notice_goes_to_those_who_want_break_news_but_not_to_muted_series(self):
        self.assertTrue(check.should_alert("dormant", None))
        self.assertTrue(check.should_alert("dormant", "breaks"))
        self.assertFalse(check.should_alert("dormant", "off"))

    def test_the_digest_has_a_dormant_section_and_count(self):
        text = check.build_digest([("dormant", "💤 **Nana**: no new chapter for over 17 years")], tracked=40, on_break=4, dormant=2)
        self.assertIn("**Now dormant (1)**", text)
        self.assertIn("Tracking 40 series · 4 on break · 2 dormant", text)
        self.assertNotIn("dormant", check.build_digest([("chapter", "x")], tracked=40, on_break=4).split("Tracking")[1])


class RuleFootprint(unittest.TestCase):
    """Every answer says which rules changed it, so a rule that suddenly fires a lot is visible."""

    def rules(self, candidates, status="RELEASING", read=None):
        return server.reconcile(candidates, read, status)["rules"]

    def test_nothing_fired_when_the_sources_simply_agree(self):
        self.assertEqual(self.rules([cnt(100), rel("MangaDex", 100, 2), rel("MangaUpdates", 100, 2)]), [])

    def test_the_count_vote(self):
        r = self.rules([cnt(487), cnt(401, source="Kitsu"), cnt(401, source="AniList")], "FINISHED")
        self.assertIn("count_vote", r)
        self.assertIn("count_only", r)

    def test_a_feed_far_behind_the_count_is_stale(self):
        self.assertIn("stale_feed", self.rules([cnt(366), rel("MangaDex", 150, 900), rel("MangaUpdates", 272, 900)]))

    def test_a_lone_lagging_feed(self):
        r = self.rules([cnt(327), cnt(327, source="Kitsu"), rel("MangaDex", 303, 2000)])
        self.assertIn("lagging_feed", r)

    def test_the_highest_chapter_beating_a_fresher_date(self):
        r = self.rules([cnt(311), rel("MangaUpdates", 311, 40), rel("Webtoon", 308, 26, official=True)])
        self.assertEqual(r, ["highest_chapter"])

    def test_an_upload_date_after_the_end(self):
        r = self.rules([cnt(181), cnt(179, source="Kitsu", ended="2018-07-05"), rel("MangaDex", 179, 70)], "FINISHED")
        self.assertIn("upload_date_dropped", r)

    def test_the_fallback_and_dormant_footprints_come_from_chapter_info(self):
        helper = DormantRule("test_a_finished_series_is_never_dormant")
        sources = [("MangaBaka", lambda *a: cnt(327)), ("MangaDex", lambda *a: rel("MangaDex", 303, 2000))]
        refused = helper.info("RELEASING", sources, fallback=rel(server.ANY_LANGUAGE, 327, 2900))
        self.assertTrue({"any_language_refused", "dormant"} <= set(refused["rules"]))
        used = helper.info("RELEASING", [("MangaBaka", lambda *a: cnt(366)), ("MangaDex", lambda *a: rel("MangaDex", 150, 900))], fallback=rel(server.ANY_LANGUAGE, 367, 5))
        self.assertIn("any_language", used["rules"])

    def test_every_rule_has_a_label(self):
        for rule in ("count_vote", "stale_feed", "lagging_feed", "highest_chapter", "upload_date_dropped", "any_language", "any_language_refused", "dormant", "count_only"):
            self.assertIn(rule, server.RULE_LABELS)


class RuleFootprintInTheBot(unittest.TestCase):
    def test_counting_per_rule(self):
        state = {"1": {"rules": ["count_only"]}, "2": {"rules": ["count_only", "dormant"]}, "3": {"rules": []}, "4": {}}
        self.assertEqual(check.rule_counts(state), {"count_only": 2, "dormant": 1})

    def test_no_warning_without_a_previous_run(self):
        self.assertEqual(check.rule_notes({"count_vote": 30}, None, 40), [])

    def test_a_sudden_jump_warns_and_names_the_rule(self):
        notes = check.rule_notes({"count_vote": 9}, {"count_vote": 2}, 40)
        self.assertEqual(len(notes), 1)
        self.assertIn("outvoted", notes[0])
        self.assertIn("9", notes[0])

    def test_the_jump_needs_to_be_at_least_3_series_and_10_percent(self):
        self.assertEqual(check.rule_notes({"x": 4}, {"x": 2}, 40), [])         # +2: normal movement
        self.assertEqual(check.rule_notes({"x": 5}, {"x": 2}, 40), [])         # +3: under 10% of 40 (= 4)
        self.assertEqual(len(check.rule_notes({"x": 6}, {"x": 2}, 40)), 1)      # +4 = 10% of 40
        self.assertEqual(len(check.rule_notes({"x": 3}, {"x": 0}, 20)), 1)      # on a short list the floor is 3
        self.assertEqual(check.rule_notes({"x": 14}, {"x": 11}, 100), [])       # +3 but under 10% of 100
        self.assertEqual(len(check.rule_notes({"x": 21}, {"x": 11}, 100)), 1)   # +10 = 10% of 100

    def test_a_new_rule_counts_from_zero(self):
        self.assertEqual(len(check.rule_notes({"fresh_rule": 6}, {"count_vote": 1}, 40)), 1)


class TypicalGap(unittest.TestCase):
    """server.typical_gap: the rhythm the on-break rule and the backtest both use."""

    def days(self, *gaps):
        from datetime import date, timedelta
        out = [date(2026, 1, 1)]
        for g in gaps:
            out.append(out[-1] + timedelta(days=g))
        return out

    def test_too_little_history_gives_no_rhythm(self):
        self.assertIsNone(server.typical_gap(self.days(7, 7, 7)))        # 3 gaps
        self.assertEqual(server.typical_gap(self.days(7, 7, 7, 7)), 7)   # 4 gaps

    def test_breaks_do_not_change_the_rhythm_because_the_short_gaps_win(self):
        self.assertEqual(server.typical_gap(self.days(7, 7, 28, 7, 14, 7, 7, 42, 7, 7)), 7)

    def test_only_the_last_ten_gaps_count(self):
        self.assertEqual(server.typical_gap(self.days(30, 30, 30, *([7] * 10))), 7)


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


class ShouldAlert(unittest.TestCase):
    def test_default_and_all_send_everything(self):
        for kind in ("chapter", "back", "break", "status"):
            self.assertTrue(check.should_alert(kind, None))
            self.assertTrue(check.should_alert(kind, "all"))

    def test_breaks_only_sends_break_news(self):
        self.assertTrue(check.should_alert("break", "breaks"))
        self.assertTrue(check.should_alert("back", "breaks"))
        self.assertFalse(check.should_alert("chapter", "breaks"))
        self.assertFalse(check.should_alert("status", "breaks"))

    def test_off_sends_nothing(self):
        for kind in ("chapter", "back", "break", "status"):
            self.assertFalse(check.should_alert(kind, "off"))


class BuildDigest(unittest.TestCase):
    WHEN = datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)

    def test_nothing_to_say_sends_nothing(self):
        self.assertEqual(check.build_digest([], [], self.WHEN, 40, 6), "")

    def test_always_sends_an_all_quiet_message(self):
        text = check.build_digest([], [], self.WHEN, 40, 6, always=True)
        self.assertIn("Nothing new today.", text)
        self.assertIn("Tracking 40 series · 6 on break", text)

    def test_sections_are_grouped_counted_and_ordered(self):
        items = [
            ("break", "⏸️ **Berserk** quiet"),
            ("chapter", "📖 **One Piece** 1198"),
            ("chapter", "📖 **Dandadan** 252"),
            ("back", "▶️ **Nana** is back"),
            ("status", "ℹ️ **X** is now finished"),
        ]
        text = check.build_digest(items, [], self.WHEN, 40, 6)
        self.assertTrue(text.startswith("📚 **Manga Tracker** · Thu Nov 5"))
        order = [text.index(h) for h in ("**New chapters (2)**", "**Back from break (1)**", "**Maybe on break (1)**", "**Status changes (1)**")]
        self.assertEqual(order, sorted(order))
        self.assertLess(text.index("One Piece"), text.index("Dandadan"))  # keeps the order found

    def test_heads_ups_alone_still_send_a_digest(self):
        text = check.build_digest([], ["⚠️ Webtoon failed for 40 of 40 lookups"], self.WHEN, 40, 6)
        self.assertIn("**Heads up**", text)
        self.assertNotIn("Nothing new today.", text)

    def test_no_italic_marks_that_would_show_up_in_email(self):
        self.assertNotIn("_", check.build_digest([("chapter", "x")], [], self.WHEN, 3, 1).split("\n")[-1])


class ChunkLines(unittest.TestCase):
    def test_short_text_is_one_piece(self):
        self.assertEqual(check.chunk_lines("a\nb", 100), ["a\nb"])

    def test_splits_at_line_ends_and_respects_the_limit(self):
        text = "\n".join(["x" * 40] * 10)
        chunks = check.chunk_lines(text, 100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertEqual("\n".join(chunks), text)  # nothing lost or cut in half
        self.assertTrue(all(len(line) == 40 for c in chunks for line in c.split("\n")))

    def test_a_single_huge_line_is_cut_hard(self):
        chunks = check.chunk_lines("y" * 250, 100)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])


class HealthNotes(unittest.TestCase):
    def stats(self, **kw):
        base = {"ok": 0, "none": 0, "failed": 0, "cached": 0, "rateLimited": 0, "seconds": 0.0, "errors": []}
        return {name: {**base, **vals} for name, vals in kw.items()}

    def test_healthy_run_has_no_notes(self):
        self.assertEqual(check.health_notes(self.stats(MangaDex={"ok": 38, "none": 2}), 40, 0), [])

    def test_a_source_that_mostly_failed_is_reported(self):
        notes = check.health_notes(self.stats(Webtoon={"failed": 20, "ok": 2, "none": 18}), 40, 0)
        self.assertEqual(len(notes), 1)
        self.assertIn("Webtoon", notes[0])

    def test_one_in_four_failing_is_not_yet_a_problem(self):
        self.assertEqual(check.health_notes(self.stats(MangaDex={"failed": 9, "ok": 31}), 40, 0), [])

    def test_many_series_with_no_answer_at_all_is_reported(self):
        self.assertEqual(len(check.health_notes(self.stats(), 40, 10)), 1)
        self.assertEqual(check.health_notes(self.stats(), 40, 2), [])  # just two bad lookups is noise


class GapOverride(unittest.TestCase):
    """series.json "gapDays": a rhythm you know replaces the measured one."""

    def info(self, gap_override):
        sources = [("S0", lambda *a: rel("MangaDex", 100, 40))]  # last chapter 40 days ago, measured rhythm 7
        server._cache.clear()
        with mock.patch.object(server, "SOURCES", sources), mock.patch.object(server.time, "sleep"):
            return server.chapter_info("1", ["x"], None, None, "RELEASING", gap_override=gap_override)

    def test_without_an_override_the_series_looks_on_break(self):
        self.assertEqual(self.info(None)["quietDays"], 40)

    def test_a_monthly_override_makes_40_days_perfectly_normal(self):
        r = self.info(30)
        self.assertEqual(r["typicalGapDays"], 30)
        self.assertEqual(r["quietDays"], 0)  # limit for a 30-day rhythm is 75 days
        self.assertTrue(r["gapOverride"])

    def test_next_chapter_follows_the_override(self):
        published = datetime.fromisoformat(day(40))
        self.assertEqual(self.info(30)["expectedNext"], (published + timedelta(days=30)).date().isoformat())


if __name__ == "__main__":
    unittest.main(verbosity=2)
