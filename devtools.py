#!/usr/bin/env python3
"""Dev helpers for Manga Tracker: the things we kept doing by hand.

    python3 devtools.py report            summarize state.json: sources agree / conflict / limited / on break
    python3 devtools.py history [NAME]    newest events of history.jsonl (optionally one series)
    python3 devtools.py backfill          one-time: fill history.jsonl from the chapter dates in state.json
    python3 devtools.py match NAME        why a series (title contains NAME) fails to match on MangaUpdates
    python3 devtools.py restart           stop whatever listens on :8123 and start server.py again
    python3 devtools.py sandbox           run the full check on COPIES of series.json/state.json
    python3 devtools.py sandbox FILE.xml  ...after importing a MyAnimeList export into the copy first
    python3 devtools.py compare NAME...   ask every source LIVE for these series (title contains NAME) and
                                          print what state.json says now next to what the code says today
    python3 devtools.py compare --all     the same for every series (about 3 minutes)
    python3 devtools.py record NAME...    freeze what every source says NOW for these series into fixtures/ (--all, or
                                          --id 123 456 for series that are not in series.json); needs the network
    python3 devtools.py replay            run the CURRENT rules on every fixture, offline, and show what changed
    python3 devtools.py replay --update   ...and accept the changes as the new expected output
    python3 devtools.py wiki TITLE...     what English Wikipedia's article says about the chapter count (the best
                                          sourced truth for finished series); one polite request per title
    python3 devtools.py rules             how many recorded series each rule changes (the rule footprint), per set, and which
                                          series the rare ones touch: a rule that fires for almost nothing is a candidate to remove
    python3 devtools.py score [SET]       how often the current rules agree with the web-checked truths, per set
                                          ("main" = the 40 series the rules were built on, "holdout" = never seen)
    python3 devtools.py mutate            break each rule on purpose, in a scratch copy, and say which tests notice
    python3 devtools.py preview [STATE]   serve a COPY of the site on :8125 using the newest sandbox state.json (or
                                          STATE), to see a change in the browser before the bot has run
    python3 devtools.py preview stop      stop that preview server

`sandbox` never touches your real files and never sends a notification, so it is the safe
way to try a change (new source, new rule, a different import) and read the result.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8123


def report(state_path=None):
    state_path = state_path or os.path.join(HERE, "state.json")
    raw = json.load(open(state_path))
    series = raw.get("series", raw)
    if not series:
        print("state.json is empty (run check.py first).")
        return

    counts = {}
    for v in series.values():
        key = v.get("agreement") or "old format"
        counts[key] = counts.get(key, 0) + 1
    print(f"{len(series)} series, checked {raw.get('checkedAt', '?')}")
    h = raw.get("health")
    if h:
        print(f"last run: {h['durationSec']}s, {h['lookupFailed']} series with no answer from any source")
        for name, s in h["sources"].items():
            flag = "  <-- PROBLEM" if s["failed"] or s["rateLimited"] else ""
            print(
                f"  {name:13} ok {s['ok']:3}  no-entry {s['none']:3}  failed {s['failed']:3}  "
                f"rate-limited {s['rateLimited']:2}  {s['seconds']}s{flag}"
            )
    print("agreement:", ", ".join(f"{k} {n}" for k, n in sorted(counts.items())))

    short = {"MangaDex": "MD", "MangaUpdates": "MU", "MangaBaka": "MB", "Webtoon": "WT"}  # new: first 4 letters

    def sources_text(v):
        return " | ".join(
            f"{short.get(s['name'], s['name'][:4])} {s['chapter']} ({(s['publishedAt'] or 'count')[:7]})"
            f"{'' if s['plausible'] else ' x'}"
            for s in v.get("sources", [])
        )

    for label, rows in (
        ("CONFLICT (freshest kept)", [v for v in series.values() if v.get("agreement") == "conflict"]),
        ("LIMITED (releasing, no source fits)", [v for v in series.values() if v.get("limited")]),
        ("ON BREAK?", [v for v in series.values() if v.get("quiet")]),
        ("DORMANT (5+ years without a chapter)", [v for v in series.values() if v.get("dormant")]),
    ):
        if rows:
            print(f"\n{label}:")
            for v in rows:
                print(f"  {v['title'][:34]:34} used={v.get('source') or '-':12} {sources_text(v)}")


def history(needle=None, limit=30):
    """Show the newest events of history.jsonl (optionally only for series whose title has NEEDLE)."""
    path = os.path.join(HERE, "history.jsonl")
    if not os.path.exists(path):
        sys.exit("no history.jsonl yet (the bot creates it; or run: python3 devtools.py backfill)")
    events = [json.loads(line) for line in open(path) if line.strip()]
    if needle:
        events = [e for e in events if needle.lower() in e["title"].lower()]
    counts = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    print(f"{len(events)} events:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for e in sorted(events, key=lambda e: e["t"], reverse=True)[:limit]:
        extra = {k: v for k, v in e.items() if k not in ("t", "id", "title", "type")}
        print(f"  {e['t'][:16]}  {e['type']:13} {e['title'][:30]:30} {extra}")


def backfill():
    """One-time: turn the chapter dates already stored in state.json into history events, so the
    log (and the page's Activity tab) isn't empty on day one. Marked backfilled=true."""
    path = os.path.join(HERE, "history.jsonl")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        sys.exit("history.jsonl already has content; not backfilling again")
    series = json.load(open(os.path.join(HERE, "state.json"))).get("series", {})
    events = []
    for sid, v in series.items():
        for h in v.get("history", []):
            events.append(
                {"t": h["date"], "id": int(sid), "title": v["title"], "type": "chapter",
                 "chapter": h["chapter"], "source": v.get("source"), "backfilled": True}
            )
    events.sort(key=lambda e: e["t"])
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"wrote {len(events)} backfilled chapter events to history.jsonl")


def match(needle):
    """Why does a series (title contains NEEDLE) not match on MangaUpdates?

    Prints the names and year we search with, then what MangaUpdates answers for each
    name, marking the results that pass our rule (same normalized title + year +/- 1).
    """
    sys.path.insert(0, HERE)
    import server

    series = [s for s in json.load(open(os.path.join(HERE, "series.json"))) if needle.lower() in s["title"].lower()]
    if not series:
        sys.exit(f"nothing in series.json contains '{needle}'")
    for s in series:
        data = server.http_json(
            "https://graphql.anilist.co",
            {
                "query": "query($id:Int){Media(id:$id,type:MANGA){title{romaji english native} synonyms startDate{year}}}",
                "variables": {"id": s["id"]},
            },
        )["data"]["Media"]
        year = (data["startDate"] or {}).get("year")
        names = [data["title"]["english"], data["title"]["romaji"]] + (data["synonyms"] or [])
        names = [n for n in names if n]
        print(f"\n== {s['title']}  (AniList {s['id']}, year {year}, saved override mu={s.get('mu')})")
        print("   we search with:", names[:4], "| all names:", len(names))
        for term in names[:4]:
            res = server.http_json(f"{server.MANGAUPDATES}/series/search", {"search": term, "perpage": 15}).get("results", [])
            print(f"   search '{term}' -> {len(res)} results")
            for r in res[:6]:
                x = r["record"]
                score = server.mu_score(x, names, year)
                flag = f"ACCEPTED ({score:.2f})" if score else ""
                print(f"      {x['series_id']} | {x['title'][:48]:48} | {x.get('year')} | {flag}")
            time.sleep(0.5)


def restart():
    pids = subprocess.run(
        ["lsof", "-ti", f":{PORT}", "-sTCP:LISTEN"], capture_output=True, text=True
    ).stdout.split()
    for pid in pids:
        os.kill(int(pid), 15)
    time.sleep(1)
    subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=HERE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # keeps running after this script exits
    )
    for _ in range(20):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1)
            print(f"server restarted: http://127.0.0.1:{PORT}")
            return
        except Exception:
            time.sleep(0.5)
    sys.exit("server did not come up")


def sandbox(xml=None):
    tmp = tempfile.mkdtemp(prefix="manga-sandbox-")
    series, state = os.path.join(tmp, "series.json"), os.path.join(tmp, "state.json")
    for name in ("series.json", "state.json"):
        shutil.copy(os.path.join(HERE, name), os.path.join(tmp, name))
    print(f"working on copies in {tmp}\n")

    if xml:
        subprocess.run([sys.executable, "check.py", "--import-mal", xml, "--series", series], cwd=HERE, check=True)
    subprocess.run(
        [sys.executable, "check.py", "--no-send", "--series", series, "--state", state], cwd=HERE, check=True
    )
    print()
    report(state)


PREVIEW_PORT = 8125


def preview(state=None):
    """The page with data it doesn't have yet: copy the site files and a state.json (default: the newest
    sandbox run) to a temp folder and serve it on :8125 (never cached, never touching the real files)."""
    import glob

    if state is None:
        found = sorted(glob.glob(os.path.join(tempfile.gettempdir(), "manga-sandbox-*", "state.json")), key=os.path.getmtime)
        found += sorted(glob.glob("/var/folders/*/*/T/manga-sandbox-*/state.json"), key=os.path.getmtime)
        if not found:
            print("No sandbox run yet: run `python3 devtools.py sandbox` first, or pass a state.json path.")
            return
        state = found[-1]
    folder = tempfile.mkdtemp(prefix="manga-preview-")
    for name in ("index.html", "app.js", "style.css", "manifest.webmanifest", "series.json", "history.jsonl"):
        shutil.copy(os.path.join(HERE, name), folder)
    shutil.copytree(os.path.join(HERE, "assets"), os.path.join(folder, "assets"))
    shutil.copy(state, os.path.join(folder, "state.json"))
    stop_preview()
    code = (
        "import functools, http.server, sys\n"
        "class H(http.server.SimpleHTTPRequestHandler):\n"
        "    def end_headers(self):\n"
        "        self.send_header('Cache-Control', 'no-store'); super().end_headers()\n"
        "    def log_message(self, *a): pass\n"
        f"http.server.ThreadingHTTPServer(('127.0.0.1', {PREVIEW_PORT}), functools.partial(H, directory={folder!r})).serve_forever()\n"
    )
    subprocess.Popen([sys.executable, "-c", code], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    print(f"preview of {state}\n  -> http://127.0.0.1:{PREVIEW_PORT}/index.html   (stop it with: python3 devtools.py preview stop)")


def stop_preview():
    subprocess.run(["pkill", "-f", f"ThreadingHTTPServer\\(\\('127.0.0.1', {PREVIEW_PORT}\\)"], capture_output=True)


ANILIST_FIELDS = "id title { romaji english } synonyms startDate { year } status"


def anilist_media(ids):
    """{AniList id: media} (title, synonyms, start year, status), 50 ids per request."""
    import server

    found = {}
    ids = list(ids)
    for i in range(0, len(ids), 50):
        data = server.http_json(
            server.ANILIST,
            {"query": f"query ($ids: [Int]) {{ Page(perPage: 50) {{ media(id_in: $ids, type: MANGA) {{ {ANILIST_FIELDS} }} }} }}",
             "variables": {"ids": ids[i:i + 50]}},
        )["data"]["Page"]["media"]
        found.update({m["id"]: m for m in data})
    return found


def titles_of(m):
    return [t for t in [m["title"]["english"], m["title"]["romaji"], *m["synonyms"]] if t][:4]


def record(args):
    import corpus
    import server

    series = json.load(open(os.path.join(HERE, "series.json")))
    state = json.load(open(os.path.join(HERE, "state.json"))).get("series", {})
    if "--id" in args:
        chosen = [{"id": int(a)} for a in args[args.index("--id") + 1:] if a.isdigit()]
    else:
        names = [a for a in args if not a.startswith("--")]
        chosen = [s for s in series if "--all" in args or any(n.lower() in s["title"].lower() for n in names)]
    if not chosen:
        print("Nothing to record: give names, --all, or --id 123 456")
        return
    media = anilist_media([s["id"] for s in chosen])
    for s in chosen:
        m = media.get(s["id"])
        if not m:
            print(f"{s['id']}: not found on AniList")
            continue
        matches = (state.get(str(s["id"])) or {}).get("matches")
        try:
            fx = corpus.record_live(s["id"], titles_of(m), m["startDate"]["year"], m["status"], s.get("read"), s.get("gapDays"), matches, titles_of(m)[0])
        except Exception as err:
            print(f"{titles_of(m)[0][:30]:30} FAILED: {err}")
            continue
        old = os.path.exists(corpus.path_for(fx["id"]))
        corpus.save(fx)
        g = fx["golden"]
        print(f"{'updated ' if old else 'recorded'} {fx['title'][:30]:30} {fx['status']:10} ch {g['chapter']} {g['date'] or 'no date'} {g['agreement']}")


# (file, text to find, replacement, what it breaks). Each is applied alone to a scratch copy.
MUTATIONS = [
    ("server.py", 'abs(c["chapterNum"] - count) > 0.05 * count', 'abs(c["chapterNum"] - count) > 0.5 * count', "a count is outvoted at 5% -> 50%"),
    ("server.py", 'best["chapterNum"] < 0.95 * count and count - best["chapterNum"] > 5', 'best["chapterNum"] < 0.5 * count and count - best["chapterNum"] > 5', "a lone feed is stale at 95% of the count -> 50%"),
    ("server.py", 'and not backed\n', 'and backed\n', "the lagging rule ignores whether another feed agrees"),
    ("server.py", 'DORMANT_YEARS = float(os.environ.get("DORMANT_YEARS", "5"))', 'DORMANT_YEARS = float(os.environ.get("DORMANT_YEARS", "50"))', "dormant after 5 years -> 50"),
    ("server.py", "ANY_LANGUAGE_MAX_AGE_DAYS = 120", "ANY_LANGUAGE_MAX_AGE_DAYS = 120000", "the any-language answer may be any age"),
    ("server.py", 'cand["chapterNum"] > 1.1 * count + 5', 'cand["chapterNum"] > 100 * count + 5', "the any-language answer may be far above the count"),
    ("server.py", 'key=lambda c: (math.floor(c["chapterNum"]), parse_time', 'key=lambda c: (0, parse_time', "the highest chapter no longer wins"),
    ("server.py", 'if status == "FINISHED" and ended and', 'if False and ended and', "upload dates after the end are kept"),
    ("server.py", 'if status == "RELEASING" and result.get("agreement") == "count":', 'if False:', "the any-language fallback never runs"),
    ("server.py", 'return days if days >= limit else 0', 'return days if days >= 100 * limit else 0', "the on-break rule never fires"),
    ("server.py", 'count = lower_median([c["chapterNum"] for c in counts if c["chapterNum"]])', 'count = max([c["chapterNum"] for c in counts if c["chapterNum"]], default=0)', "the count consensus takes the maximum, not the lower median"),
]


def wiki(titles, pause=5.0):
    """Chapter counts mentioned in the lead of the English Wikipedia article for each title.

    One request per title (search + extract together), `pause` seconds apart, and it waits as long as
    Wikipedia asks (Retry-After) when it says "too many requests". Prints the page it read so the
    answer can be traced: Wikipedia's counts are written by editors from the publishers' volume lists."""
    import re
    import urllib.error
    import urllib.parse

    headers = {"User-Agent": "manga-tracker-factcheck/1.0 (learning project; github.com/SSY0ne/manga-tracker-webvers)"}
    for title in titles:
        params = urllib.parse.urlencode({
            "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{title} manga",
            "gsrlimit": 2, "prop": "extracts", "exintro": 1, "explaintext": 1, "exlimit": 2,
        })
        data = None
        for attempt in range(4):
            time.sleep(pause)
            try:
                req = urllib.request.Request("https://en.wikipedia.org/w/api.php?" + params, headers=headers)
                data = json.load(urllib.request.urlopen(req, timeout=25))
                break
            except urllib.error.HTTPError as err:
                if err.code != 429:
                    print(f"{title}: HTTP {err.code}")
                    break
                wait = int(err.headers.get("Retry-After", "30")) + 3
                print(f"  (Wikipedia asked to wait {wait}s)", flush=True)
                time.sleep(wait)
        if not data:
            print(f"{title[:28]:28} no answer")
            continue
        pages = sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 9))
        hits = []
        for page in pages:
            text = page.get("extract", "")
            for m in re.finditer(r"([^.]*?\b(\d[\d,]*)\s+(?:individual\s+)?chapters[^.]*\.)", text):
                hits.append((page["title"], m.group(2).replace(",", ""), re.sub(r"\s+", " ", m.group(1)).strip()[:150]))
        if hits:
            for page_title, n, sentence in hits[:2]:
                print(f"{title[:28]:28} {n:>5} chapters  [{page_title}]  {sentence}")
        else:
            print(f"{title[:28]:28} (no chapter count in the lead of: {', '.join(p['title'] for p in pages) or 'nothing found'})")


def score(set_name=None):
    import corpus

    rows = corpus.score(set_name)
    truth = corpus.load_truth()
    fixtures = {str(fx["id"]): fx for fx in corpus.load_all()}
    for name in sorted({r[1] for r in rows}):
        part = [r for r in rows if r[1] == name]
        hit = [r for r in part if r[2]]
        print(f"{name:8} {len(hit)} of {len(part)} agree with the web ({100 * len(hit) // max(1, len(part))}%)")
        for title, _, ok, why, enforced in part:
            if not ok:
                print(f"   MISS {title[:30]:30} {why}{'' if enforced else '   (not enforced)'}")
        untested = [fx["title"] for k, fx in fixtures.items() if k not in truth and (name != "holdout" or True)]
    checked = {r[0] for r in rows}
    left = [fx["title"] for k, fx in fixtures.items() if (truth.get(k, {}).get("title") or fx["title"]) not in checked and k not in truth]
    print(f"\n{len(left)} recorded series have no web-checked truth yet (they count for nothing here).")


def rules_footprint():
    import corpus
    import server

    truth = corpus.load_truth()
    per_rule, sets = {}, {"main": 0, "holdout": 0}
    for fx in corpus.load_all():
        which = (truth.get(str(fx["id"]), {}).get("set")) or ("main" if fx["id"] in {s["id"] for s in json.load(open(os.path.join(HERE, "series.json")))} else "holdout")
        sets[which] += 1
        _, snap = corpus.replay(fx)
        for r in snap["rules"]:
            per_rule.setdefault(r, {"main": [], "holdout": []})[which].append(fx["title"])
    print(f"{sets['main']} series the rules were built on, {sets['holdout']} never seen\n")
    print(f"{'rule':22} {'main':>5} {'holdout':>8}   what it does")
    for r, where in sorted(per_rule.items(), key=lambda kv: -(len(kv[1]['main']) + len(kv[1]['holdout']))):
        print(f"{r:22} {len(where['main']):5} {len(where['holdout']):8}   {server.RULE_LABELS.get(r, r)}")
        if len(where["main"]) + len(where["holdout"]) <= 4:
            print(f"{'':22} {', '.join(where['main'] + where['holdout'])}")
    silent = [r for r in server.RULE_LABELS if r not in per_rule]
    if silent:
        print("\nnever fired on any recorded series:", ", ".join(silent))


def mutate():
    """Mutation testing: if breaking a rule on purpose doesn't make a test fail, that rule is unprotected."""
    import glob

    here = os.path.dirname(os.path.abspath(__file__))
    print(f"{len(MUTATIONS)} deliberate breakages, each tried alone in a scratch copy:\n")
    survived = 0
    for path, old, new, what in MUTATIONS:
        tmp = tempfile.mkdtemp(prefix="manga-mutant-")
        for f in glob.glob(os.path.join(here, "*.py")):
            shutil.copy(f, tmp)
        shutil.copytree(os.path.join(here, "fixtures"), os.path.join(tmp, "fixtures"))
        text = open(os.path.join(tmp, path)).read()
        if text.count(old) != 1:
            print(f"?  {what}: pattern found {text.count(old)}x, fix MUTATIONS")
            continue
        open(os.path.join(tmp, path), "w").write(text.replace(old, new))
        caught = []
        for suite in ("test_rules", "test_fixtures"):
            r = subprocess.run([sys.executable, "-m", "unittest", suite], cwd=tmp, capture_output=True, text=True)
            if r.returncode != 0:
                caught.append(suite.replace("test_", ""))
        shutil.rmtree(tmp, ignore_errors=True)
        survived += not caught
        print(f"{'caught by ' + ' + '.join(caught) if caught else 'NOT CAUGHT':22} {what}")
    print(f"\n{survived} not caught" if survived else "\nevery breakage was caught")


def replay_all(update=False):
    import corpus

    fixtures = corpus.load_all()
    if not fixtures:
        print("No fixtures yet: run `python3 devtools.py record --all`")
        return 0
    changed = 0
    for fx in fixtures:
        _, snap = corpus.replay(fx)
        d = corpus.diff(fx["golden"], snap)
        if not d:
            continue
        changed += 1
        flips = []
        if "sources" in d:  # which source stopped or started "fitting"
            before = {x["name"]: x["plausible"] for x in d["sources"][0]}
            flips = [f"{x['name']} {'now fits' if x['plausible'] else 'no longer fits'}" for x in d["sources"][1] if before.get(x["name"]) != x["plausible"]]
        print(f"CHANGED {fx['title'][:34]:34} " + "; ".join([f"{k}: {a} -> {b}" for k, (a, b) in d.items() if k != "sources"] + flips))
        if update:
            fx["golden"] = snap
            corpus.save(fx)
    print(f"\n{len(fixtures)} fixtures, {changed} changed" + (" (saved as the new expected output)" if changed and update else ""))
    return 1 if changed and not update else 0


def compare(needles, everything=False):
    """The check we kept rebuilding by hand: after changing a source or a rule, run the CURRENT code
    on a few series (live requests, nothing saved) and show old versus new, with each source's answer.
    A source's number marked x does not fit (see reconcile() in server.py)."""
    import server  # imported here: the other commands don't need it

    series = json.load(open(os.path.join(HERE, "series.json")))
    state = json.load(open(os.path.join(HERE, "state.json"))).get("series", {})
    chosen = [s for s in series if everything or any(n.lower() in s["title"].lower() for n in needles)]
    if not chosen:
        print("No series matches", needles or "(give a name, or --all)")
        return
    info = anilist_media([s["id"] for s in chosen])
    changed = 0
    for s in chosen:
        m = info.get(s["id"])
        if not m:
            print(f"{s['title'][:26]:26} not found on AniList")
            continue
        titles = titles_of(m)
        old = state.get(str(s["id"]), {})
        try:
            new = server.chapter_info(s["id"], titles, m["startDate"]["year"], s.get("read"), m["status"], old.get("matches"), s.get("gapDays"))  # gapDays: your own rhythm, like the bot
        except Exception as err:
            print(f"{s['title'][:26]:26} FAILED: {err}")
            continue
        day = lambda e: (e.get("publishedAt") or "")[:10] or "no date"
        now = (new.get("chapter"), day(new), new.get("agreement"))
        before = (old.get("chapter"), day(old), old.get("agreement"))
        mark = "  " if now == before else "<>"
        changed += now != before
        srcs = " ".join(f"{x['name'][:4]}:{x['chapter']}{'' if x['plausible'] else 'x'}" for x in new.get("sources", []))
        print(f"{mark} {s['title'][:24]:24} state.json: ch {before[0]} {before[1]} {before[2]}  |  now: ch {now[0]} {now[1]} {now[2]} (rhythm {new.get('typicalGapDays')}, quiet {new.get('quietDays')}{', DORMANT ' + str(new['dormantDays'] // 365) + 'y' if new.get('dormant') else ''})  [{srcs}]")
    print(f"\n{changed} of {len(chosen)} differ from state.json (<> marks them).")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "report":
        report()
    elif cmd == "history":
        history(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "backfill":
        backfill()
    elif cmd == "match" and len(sys.argv) > 2:
        match(sys.argv[2])
    elif cmd == "restart":
        restart()
    elif cmd == "record":
        record(sys.argv[2:])
    elif cmd == "wiki":
        wiki(sys.argv[2:])
    elif cmd == "rules":
        rules_footprint()
    elif cmd == "score":
        score(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "mutate":
        mutate()
    elif cmd == "replay":
        sys.exit(replay_all(update="--update" in sys.argv))
    elif cmd == "preview":
        if len(sys.argv) > 2 and sys.argv[2] == "stop":
            stop_preview()
        else:
            preview(sys.argv[2] if len(sys.argv) > 2 else None)
    elif cmd == "compare":
        names = [a for a in sys.argv[2:] if not a.startswith("--")]
        compare(names, everything="--all" in sys.argv)
    elif cmd == "sandbox":
        sandbox(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
