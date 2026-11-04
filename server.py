#!/usr/bin/env python3
"""Tiny local server for Manga Tracker (standard library only, no installs).

    python3 server.py        ->  http://127.0.0.1:8123

It does three jobs:
  1. Serves the page files (index.html, series.json, state.json ...).
  2. Looks up chapters for the page (browsers can't call MangaDex directly):
       GET /api/latest?al=<AniList id>&title=<name>&title=<other name>&year=<y>&read=<n>&status=<s>
  3. Lets the page edit series.json (local use only):
       GET /api/ping, POST /api/series, DELETE /api/series?id=<AniList id>

How chapter data is decided (the "sources" design):
  * Each SOURCE (MangaDex, MangaUpdates) is asked separately and returns a candidate:
    its latest chapter, when, a short history and the series' usual rhythm.
  * reconcile() then compares the candidates and keeps the one that is coherent.
    Adding a source later = one more function that returns a candidate.

check.py imports chapter_info() and quiet_days() from here, so the page and the daily
bot use exactly the same rules.
"""

import difflib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8123
HERE = os.path.dirname(os.path.abspath(__file__))
SERIES_FILE = os.path.join(HERE, "series.json")
MANGADEX = "https://api.mangadex.org"
MANGAUPDATES = "https://api.mangaupdates.com/v1"
USER_AGENT = "manga-tracker-learning-project/0.1"
CACHE_SECONDS = 15 * 60  # don't hit a source more than once per series per 15 min

_cache = {}  # (source, AniList id) -> (timestamp, candidate or None)
_md_id_cache = {}  # AniList id -> MangaDex id (or None)
_mu_id_cache = {}  # AniList id -> {"id", "url"} on MangaUpdates (or None)


# ---------- small helpers ----------

def parse_time(iso):
    return datetime.fromisoformat(iso)


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def chapter_number(text):
    """'392' -> 392.0, '390-392' -> 392.0, '222 + Afterword (end)' -> 222.0, 'Epilogue' -> None."""
    nums = re.findall(r"\d+(?:\.\d+)?", str(text or ""))
    return max(float(n) for n in nums) if nums else None


def fmt_num(x):
    return str(int(x)) if x == int(x) else str(x)


def http_json(url, body=None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.load(res)


def build_candidate(source, pairs, series_url):
    """Turn (chapter text, ISO time, link or None) rows from ONE source into a candidate.

    The same chapter is often listed several times (several uploaders or groups), so
    per chapter number we keep the EARLIEST time. The usual gap is a gap that really
    happened: the lower quartile of the days between release days over the last 10
    gaps. Breaks only ever make a gap LONGER, so the short gaps show the real rhythm
    (a weekly series that skips weeks still says 7).
    """
    first = {}  # chapter number -> (time, link)
    for text, when, link in pairs:
        num = chapter_number(text)
        if num is None or not when:
            continue
        if num not in first or when < first[num][0]:
            first[num] = (when, link)
    if not first:
        return None

    ordered = sorted(first.items(), key=lambda kv: (kv[1][0], kv[0]), reverse=True)  # newest first
    num, (published, link) = ordered[0]

    days = sorted({parse_time(t).date() for _, (t, _) in ordered})
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    recent = sorted(gaps[-10:])
    typical = recent[(len(recent) - 1) // 4] if len(gaps) >= 4 else None
    expected = None
    if typical:
        expected = datetime.fromtimestamp(
            parse_time(published).timestamp() + typical * 86400, tz=timezone.utc
        ).date().isoformat()

    return {
        "source": source,
        "chapter": fmt_num(num),
        "chapterNum": num,
        "publishedAt": published,
        "url": link or series_url,
        "typicalGapDays": typical,
        "expectedNext": expected,
        "history": [{"chapter": fmt_num(n), "date": t} for n, (t, _) in ordered[:12]],
    }


# ---------- source 1: MangaDex ----------

def md_get(path, params):
    return http_json(f"{MANGADEX}{path}?{urllib.parse.urlencode(params)}")


def find_mangadex_id(al_id, titles):
    """Match an AniList manga to MangaDex using MangaDex's own 'al' link (exact)."""
    if al_id in _md_id_cache:
        return _md_id_cache[al_id]
    for title in [t for t in titles if t][:3]:
        for m in md_get("/manga", [("title", title), ("limit", 10)]).get("data", []):
            if (m["attributes"].get("links") or {}).get("al") == al_id:
                _md_id_cache[al_id] = m["id"]
                return m["id"]
    _md_id_cache[al_id] = None
    return None


def mangadex_candidate(al_id, titles, year):
    md_id = find_mangadex_id(al_id, titles)
    if not md_id:
        return None
    feed = md_get(
        f"/manga/{md_id}/feed",
        [("translatedLanguage[]", "en"), ("order[publishAt]", "desc"), ("limit", 40)],
    )
    pairs = []
    for c in feed.get("data", []):
        a = c["attributes"]
        # Official publisher link when there is one, else the MangaDex page.
        link = a.get("externalUrl") or f"https://mangadex.org/chapter/{c['id']}"
        pairs.append((a.get("chapter"), a.get("publishAt"), link))
    return build_candidate("MangaDex", pairs, f"https://mangadex.org/title/{md_id}")


# ---------- source 2: MangaUpdates ----------

MU_MIN_SCORE = 0.85  # how similar two titles must be (1.0 = identical) when the year also fits


def title_key(text):
    """Lowercase, drop (bracketed bits like '(KIM Carnby)'), keep only letters and digits."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\([^)]*\)", "", (text or "").lower()))


def mu_score(record, names, year):
    """How well a MangaUpdates search result fits our series: 0 = reject, else 0..1.

    Rejected outright: doujinshi, novels, and anything whose start year is more than a
    year away from ours (that is how two different manga called 'Sweet Home' are told
    apart). Otherwise the score is the best title similarity against any of our names.
    No known year on either side -> only an identical title is accepted.
    """
    title = record["title"]
    low = title.lower()
    if record.get("type") in ("Doujinshi", "Novel") or " dj - " in low or low.endswith("(novel)"):
        return 0
    try:
        theirs = int(record.get("year") or 0)
    except ValueError:
        theirs = 0
    ours = int(year) if year else 0
    if ours and theirs and abs(theirs - ours) > 1:
        return 0
    best = max((difflib.SequenceMatcher(None, title_key(n), title_key(title)).ratio() for n in names if n), default=0)
    needed = MU_MIN_SCORE if ours and theirs else 0.999
    return best if best >= needed else 0


def find_mu_series(al_id, titles, year):
    """MangaUpdates has no AniList link, so match on title similarity AND start year.

    Several search terms are tried (English, romaji, synonyms); the best-scoring result wins,
    and an identical title with the same year stops the search early.
    """
    if al_id in _mu_id_cache:
        return _mu_id_cache[al_id]
    names = [t for t in titles if t]
    best = (0, None)  # (score, record)
    for term in names[:4]:
        results = http_json(f"{MANGAUPDATES}/series/search", {"search": term, "perpage": 15}).get("results", [])
        for r in results:
            score = mu_score(r["record"], names, year)
            if score > best[0]:
                best = (score, r["record"])
        if best[0] >= 0.999:
            break
        time.sleep(0.3)
    found = {"id": best[1]["series_id"], "url": best[1].get("url")} if best[1] else None
    _mu_id_cache[al_id] = found
    return found


def mangaupdates_candidate(al_id, titles, year):
    series = find_mu_series(al_id, titles, year)
    if not series:
        return None
    if not series.get("url"):  # we were only given an id (manual override): ask for the link once
        series["url"] = http_json(f"{MANGAUPDATES}/series/{series['id']}").get("url")
    rel = http_json(
        f"{MANGAUPDATES}/releases/search",
        {"search": str(series["id"]), "search_type": "series", "perpage": 40, "orderby": "date"},
    ).get("results", [])
    pairs = [
        (r["record"].get("chapter"), f"{r['record']['release_date']}T00:00:00+00:00", None)
        for r in rel
        if r["record"].get("chapter") and r["record"].get("release_date")
    ]
    return build_candidate("MangaUpdates", pairs, series["url"])


SOURCES = [("MangaDex", mangadex_candidate), ("MangaUpdates", mangaupdates_candidate)]
PRIORITY = {"MangaDex": 1, "MangaUpdates": 0}  # tie-break only (MangaDex links the official reader)


# ---------- compare the sources and choose ----------

def reconcile(candidates, read, status):
    """Pick the coherent candidate and say how sure we are.

    A candidate is implausible when: its date is in the future, OR its chapter is far
    below what you have read (under 80%), OR it is under half of the highest chapter any
    source reports (stale data, or side-story numbering).
    Among the plausible ones we keep the FRESHEST (a stale source has an old date); ties go
    to the higher chapter, then to MangaDex.
      agreement: "agree"    two or more plausible sources within 3 chapters of each other
                 "conflict" plausible sources far apart (we still use the freshest)
                 "single"   only one plausible source
                 "none"     no coherent source -> "limited data": we claim nothing
    """
    candidates = [c for c in candidates if c]
    top = max((c["chapterNum"] for c in candidates), default=0)
    horizon = datetime.now(timezone.utc) + timedelta(days=1)

    def plausible(c):
        if parse_time(c["publishedAt"]) > horizon:
            return False
        if read and c["chapterNum"] < 0.8 * read:
            return False
        return c["chapterNum"] >= 0.5 * top

    good = [c for c in candidates if plausible(c)]
    summary = [
        {"name": c["source"], "chapter": c["chapter"], "publishedAt": c["publishedAt"], "plausible": plausible(c)}
        for c in candidates
    ]
    if not good:
        return {"found": False, "agreement": "none", "limited": status == "RELEASING", "sources": summary}

    best = max(
        good,
        key=lambda c: (parse_time(c["publishedAt"]).date(), c["chapterNum"], PRIORITY.get(c["source"], 0)),
    )
    if len(good) == 1:
        agreement = "single"
    elif all(abs(c["chapterNum"] - best["chapterNum"]) <= 3 for c in good):
        agreement = "agree"
    else:
        agreement = "conflict"

    result = {k: v for k, v in best.items() if k not in ("chapterNum", "source")}
    result.update({"found": True, "limited": False, "agreement": agreement, "source": best["source"], "sources": summary})
    return result


def chapter_info(al_id, titles, year=None, read=None, status=None, matches=None):
    """Ask every source, then reconcile. Raises only if every source failed.

    `matches` = what we already know about which entry each source uses for this series
    ({"MangaDex": uuid, "MangaUpdates": {"id", "url"}}), from state.json or a manual
    override in series.json. Known matches skip the title search; the result carries the
    (possibly new) matches back so the caller can remember them.
    """
    matches = matches or {}
    if matches.get("MangaDex"):
        _md_id_cache[al_id] = matches["MangaDex"]
    if matches.get("MangaUpdates"):
        _mu_id_cache[al_id] = dict(matches["MangaUpdates"])
    candidates, errors = [], []
    for name, fetch in SOURCES:
        key = (name, al_id)
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            candidates.append(hit[1])
            continue
        try:
            candidate = fetch(al_id, titles, year)
        except Exception as err:  # one source being down must not break the others
            errors.append(f"{name}: {err}")
            continue
        _cache[key] = (time.time(), candidate)
        candidates.append(candidate)
        time.sleep(0.3)  # be polite to the sources
    if errors and not any(candidates):
        raise RuntimeError("; ".join(errors))
    result = reconcile(candidates, read, status)
    found = {}
    if _md_id_cache.get(al_id):
        found["MangaDex"] = _md_id_cache[al_id]
    if _mu_id_cache.get(al_id):
        found["MangaUpdates"] = _mu_id_cache[al_id]
    result["matches"] = found
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
        if not al_id.isdigit():
            return self.send_json(400, {"error": "al must be a number"})
        year = (q.get("year") or [""])[0]
        read = (q.get("read") or [""])[0]
        try:
            self.send_json(
                200,
                chapter_info(
                    al_id,
                    q.get("title", []),
                    int(year) if year.isdigit() else None,
                    int(read) if read.isdigit() else None,
                    (q.get("status") or [None])[0],
                ),
            )
        except Exception as err:  # every source failed
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
