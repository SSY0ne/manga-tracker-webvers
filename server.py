#!/usr/bin/env python3
"""Tiny local server for Manga Tracker (standard library only, no installs).

    python3 server.py        ->  http://127.0.0.1:8123

It does three jobs:
  1. Serves the page files (index.html, series.json, state.json ...).
  2. Proxies MangaDex, because MangaDex blocks calls made straight from a web page:
       GET /api/latest?al=<AniList id>&title=<title>&alt=<other title>
  3. Lets the page edit series.json (local use only):
       GET /api/ping, POST /api/series, DELETE /api/series?id=<AniList id>

check.py also imports latest_chapter() and quiet_days() from here, so the page and
the daily bot use exactly the same rules.
"""

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8123
HERE = os.path.dirname(os.path.abspath(__file__))
SERIES_FILE = os.path.join(HERE, "series.json")
MANGADEX = "https://api.mangadex.org"
USER_AGENT = "manga-tracker-learning-project/0.1"
CACHE_SECONDS = 15 * 60  # don't hit MangaDex more than once per series per 15 min

_cache = {}  # AniList id -> (timestamp, result)
_id_cache = {}  # AniList id -> MangaDex id (or None)


# ---------- MangaDex ----------

def md_get(path, params):
    """GET from MangaDex. `params` is a list of (key, value) pairs."""
    url = f"{MANGADEX}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.load(res)


def find_mangadex_id(al_id, titles):
    """Match an AniList manga to a MangaDex manga using MangaDex's 'al' link."""
    if al_id in _id_cache:
        return _id_cache[al_id]
    for title in titles:
        if not title:
            continue
        data = md_get("/manga", [("title", title), ("limit", 10)])
        for m in data.get("data", []):
            links = m["attributes"].get("links") or {}
            if links.get("al") == al_id:
                _id_cache[al_id] = m["id"]
                return m["id"]
    _id_cache[al_id] = None
    return None


def parse_time(iso):
    return datetime.fromisoformat(iso)


def analyse_chapters(chapters):
    """Turn MangaDex's chapter feed into: latest chapter, history, usual gap.

    The same chapter is often uploaded several times, so for each chapter number we
    keep the EARLIEST publish time. The usual gap is the lower quartile of the days
    between release days, which ignores breaks and batch uploads.
    """
    first = {}  # chapter number -> (publishAt, chapter object)
    for c in chapters:
        a = c["attributes"]
        num = a.get("chapter")
        if num is None or not a.get("publishAt"):
            continue
        if num not in first or a["publishAt"] < first[num][0]:
            first[num] = (a["publishAt"], c)

    ordered = sorted(first.items(), key=lambda kv: kv[1][0], reverse=True)  # newest first
    if not ordered:
        return None

    num, (published, c) = ordered[0]
    history = [{"chapter": n, "date": t} for n, (t, _) in ordered[:12]]

    days = sorted({parse_time(t).date() for _, (t, _) in ordered})
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    # Lower quartile of the last 10 gaps. Breaks only ever make a gap LONGER, so the
    # short gaps show the real rhythm (a weekly series that skips weeks still says 7).
    # We take a gap that really happened (not an average of two), so a weekly series
    # says exactly 7 and its projected dates stay on the same weekday.
    recent = sorted(gaps[-10:])
    typical = recent[(len(recent) - 1) // 4] if len(gaps) >= 4 else None

    expected = None
    if typical:
        expected = datetime.fromtimestamp(
            parse_time(published).timestamp() + typical * 86400, tz=timezone.utc
        ).date().isoformat()

    return {
        "found": True,
        "chapter": num,
        "publishedAt": published,
        # Official publisher link when there is one, else the MangaDex page.
        "url": c["attributes"].get("externalUrl") or f"https://mangadex.org/chapter/{c['id']}",
        "typicalGapDays": typical,
        "expectedNext": expected,
        "history": history,
    }


def latest_chapter(al_id, titles):
    hit = _cache.get(al_id)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    md_id = find_mangadex_id(al_id, titles)
    result = {"found": False}
    if md_id:
        feed = md_get(
            f"/manga/{md_id}/feed",
            [("translatedLanguage[]", "en"), ("order[publishAt]", "desc"), ("limit", 40)],
        )
        result = analyse_chapters(feed.get("data", [])) or {"found": False}
    _cache[al_id] = (time.time(), result)
    return result


# ---------- the "on break" rule (shared with check.py; app.js has a copy) ----------

def quiet_days(status, published_iso, typical_gap, fallback_weeks=6):
    """How many days it has been quiet, or 0 if it doesn't look on break.

    A Releasing series is "quiet" when it is way overdue compared to ITS OWN rhythm:
    at least 2.5x its usual gap and at least 2 weeks more than the gap. A weekly
    series flags after 3 weeks, a monthly one after about 75 days. With too little
    history we fall back to a flat number of weeks.
    """
    if status != "RELEASING" or not published_iso:
        return 0
    days = (datetime.now(timezone.utc) - parse_time(published_iso)).days
    limit = max(2.5 * typical_gap, typical_gap + 14) if typical_gap else fallback_weeks * 7
    return days if days >= limit else 0


def is_limited(chapter, read):
    """True when MangaDex's data for a series looks incomplete.

    Licensed series often lose their English chapters on MangaDex, so the "latest"
    one can be tiny or missing. We notice that by comparing with how far you have
    read (from your MAL import): latest chapter far below your progress, or none.
    For those we make no "on break" or "new chapter" claims. app.js has a copy.
    """
    if chapter is None:
        return True
    try:
        return bool(read) and float(chapter) < 0.8 * read
    except ValueError:
        return False


# ---------- series.json (local editing) ----------

def read_series():
    try:
        with open(SERIES_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def write_series(series):
    with open(SERIES_FILE, "w") as f:
        json.dump(series, f, indent=2, ensure_ascii=False)
        f.write("\n")


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/ping":
            return self.send_json(200, {"edit": True})
        if parsed.path != "/api/latest":
            return super().do_GET()

        q = urllib.parse.parse_qs(parsed.query)
        al_id = (q.get("al") or [""])[0]
        titles = (q.get("title") or []) + (q.get("alt") or [])
        if not al_id.isdigit():
            return self.send_json(400, {"error": "al must be a number"})
        try:
            self.send_json(200, latest_chapter(al_id, titles))
        except Exception as err:  # network error, MangaDex down, rate limit...
            self.send_json(502, {"error": str(err)})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/series":
            return self.send_json(404, {"error": "not found"})
        # Requiring JSON means a random website can't make your browser post here:
        # browsers must ask permission first for that content type, and we never grant it.
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send_json(415, {"error": "send JSON"})
        # Body: one entry {"id", "title", optional "mal", "read"} or a list of them
        # (the MAL import sends a list). Existing series just get mal/read updated.
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            items = body if isinstance(body, list) else [body]
            clean = []
            for it in items[:500]:
                entry = {"id": int(it["id"]), "title": str(it["title"]).strip()[:200]}
                for key in ("mal", "read"):
                    if it.get(key) is not None:
                        entry[key] = int(it[key])
                clean.append(entry)
        except (ValueError, KeyError, TypeError, AttributeError):
            return self.send_json(400, {"error": "need id (number) and title"})
        series = read_series()
        by_id = {s["id"]: s for s in series}
        for entry in clean:
            if entry["id"] in by_id:
                by_id[entry["id"]].update({k: v for k, v in entry.items() if k in ("mal", "read")})
            else:
                series.append(entry)
                by_id[entry["id"]] = entry
        write_series(series)
        self.send_json(200, series)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/series":
            return self.send_json(404, {"error": "not found"})
        sid = (urllib.parse.parse_qs(parsed.query).get("id") or [""])[0]
        if not sid.isdigit():
            return self.send_json(400, {"error": "id must be a number"})
        series = [s for s in read_series() if s["id"] != int(sid)]
        write_series(series)
        self.send_json(200, series)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # The page re-reads series.json / state.json often; never serve a stale copy.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    handler = partial(Handler, directory=HERE)
    print(f"Manga Tracker on http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()
