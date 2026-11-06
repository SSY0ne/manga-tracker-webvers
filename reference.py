#!/usr/bin/env python3
"""A reference chapter count from English Wikipedia, for FINISHED series.

Why: AniList, Kitsu, MangaBaka and Shikimori all copy the same upstream list, which counts extras as chapters
(Bleach 706, Initial D 725, Dragon Ball 520). A vote among copies cannot fix that. Wikipedia's "List of X
chapters" pages are written by editors from the publishers' volume lists, so they give the highest NUMBERED
chapter (Bleach 686) and say where they got it.

A finished series never gets new chapters, so a number read once stays true: it is saved in
`reference_counts.json` (with the page it came from and the date) and the daily bot never calls Wikipedia.
Not for running series: Wikipedia lags there.

    python3 devtools.py reference measure    try it on the finished series that have a web-checked truth
"""

import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "manga-tracker-reference/1.0 (learning project; github.com/SSY0ne/manga-tracker-webvers)"}
PAUSE = 4.0  # seconds between requests: Wikipedia rate-limits quickly


def _get(params, pause=None):
    """One polite request. Waits as long as Wikipedia asks when it answers 429."""
    params = {**params, "format": "json", "formatversion": 2}
    url = API + "?" + urllib.parse.urlencode(params)
    for _ in range(4):
        time.sleep(PAUSE if pause is None else pause)
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30))
        except urllib.error.HTTPError as err:
            if err.code != 429:
                raise
            time.sleep(int(err.headers.get("Retry-After", "30")) + 3)
    raise RuntimeError("Wikipedia kept answering 'too many requests'")


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _template_args(text, start):
    """The |-separated arguments of the template whose body begins at text[start], with nested {{...}} and
    [[...]] kept whole (a link or a Nihongo title contains | too)."""
    args, buf, depth, i = [], [], 0, start
    while i < len(text):
        two = text[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            buf.append(two)
            i += 2
            continue
        if two in ("}}", "]]"):
            if depth == 0 and two == "}}":
                args.append("".join(buf))
                return args
            depth -= 1
            buf.append(two)
            i += 2
            continue
        if text[i] == "|" and depth == 0:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(text[i])
        i += 1
    return args


def chapter_numbers(wikitext):
    """Every chapter NUMBER a list page gives. Two ways editors write them:
    {{Numbered list|start=5| title | title | ...}} (the list continues from `start`), bullet lines with the
    number printed (* 424. "Title"), and ChapterNumber= values in a chapter-list template."""
    nums = []
    for m in re.finditer(r"\{\{\s*Numbered list\s*\|", wikitext, re.I):
        args = _template_args(wikitext, m.end())
        start, items = 1, 0
        for a in args:
            sm = re.match(r"\s*start\s*=\s*(-?\d+)\s*$", a)
            if sm:
                start = int(sm.group(1))
            elif a.strip():
                items += 1
        nums += [float(start + k) for k in range(items)]
    # Bullet lines with the number printed: "* 424. "The Lost Agent"" (Bleach, Initial D)
    for m in re.finditer(r"(?m)^\*\s*(-?\d{1,4}(?:\.\d+)?)\.\s", wikitext):
        nums.append(float(m.group(1)))
    for raw in re.findall(r"\|\s*ChapterNumber\s*=\s*([^\n|}]+)", wikitext):
        m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*$", re.sub(r"<[^>]+>|\[\[|\]\]|'''?", "", raw))
        if m:
            nums.append(float(m.group(1)))
    return nums


def list_pages(title):
    """Titles of Wikipedia 'List of X chapters' pages for this series (a long series is split in several)."""
    data = _get({"action": "query", "list": "search", "srsearch": f"List of {title} chapters", "srlimit": 6})
    key = _norm(title)
    out = []
    for hit in data.get("query", {}).get("search", []):
        t = hit["title"]
        if t.lower().startswith("list of") and "chapters" in t.lower() and key.split(" ")[0] in _norm(t):
            out.append(t)
    return out[:3]


def lookup(titles):
    """{'count': highest numbered chapter, 'entries': how many numbers, 'pages': [...]} or None."""
    for title in titles:
        pages = list_pages(title)
        if not pages:
            continue
        nums, used = [], []
        for page in pages:
            data = _get({"action": "parse", "page": page, "prop": "wikitext"})
            found = chapter_numbers(data.get("parse", {}).get("wikitext", ""))
            if found:
                nums += found
                used.append(page)
        if nums:
            whole = [n for n in nums if n == int(n) and n > 0]
            return {
                "count": int(max(whole)) if whole else None,
                "entries": len(set(nums)),
                "pages": used,
                "urls": ["https://en.wikipedia.org/wiki/" + urllib.parse.quote(p.replace(" ", "_")) for p in used],
                "retrieved": dt.date.today().isoformat(),
            }
    return None
