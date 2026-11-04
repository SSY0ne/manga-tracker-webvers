#!/usr/bin/env python3
"""Tiny local server for Manga Tracker (standard library only, no installs).

Why it exists: MangaDex blocks calls made straight from a browser page (CORS),
so the page asks THIS server, and this server asks MangaDex.

    python3 server.py        ->  http://127.0.0.1:8123

It serves the page files and one endpoint:

    GET /api/latest?al=<AniList id>&title=<title>&alt=<other title>
    -> {"found": true, "chapter": "1197", "publishedAt": "...", "url": "..."}
"""

import json
import time
import urllib.parse
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8123
MANGADEX = "https://api.mangadex.org"
USER_AGENT = "manga-tracker-learning-project/0.1"
CACHE_SECONDS = 15 * 60  # don't hit MangaDex more than once per series per 15 min

_cache = {}  # key -> (timestamp, result)
_id_cache = {}  # AniList id -> MangaDex id (or None)


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


def latest_chapter(al_id, titles):
    key = al_id
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]

    md_id = find_mangadex_id(al_id, titles)
    if not md_id:
        result = {"found": False}
    else:
        feed = md_get(
            f"/manga/{md_id}/feed",
            [
                ("translatedLanguage[]", "en"),
                ("order[publishAt]", "desc"),
                ("limit", 1),
            ],
        )
        chapters = feed.get("data", [])
        if not chapters:
            result = {"found": False}
        else:
            a = chapters[0]["attributes"]
            result = {
                "found": True,
                "chapter": a.get("chapter"),
                "publishedAt": a.get("publishAt"),
                # Official publisher link when there is one, else the MangaDex page.
                "url": a.get("externalUrl") or f"https://mangadex.org/chapter/{chapters[0]['id']}",
            }
    _cache[key] = (time.time(), result)
    return result


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
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

    def send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    handler = partial(Handler, directory=here)
    print(f"Manga Tracker on http://127.0.0.1:{PORT}  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", PORT), handler).serve_forever()
