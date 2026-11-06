#!/usr/bin/env python3
"""Recorded source answers ("fixtures") and how to replay them offline.

Why: the rules in server.reconcile() were each written to fix a real series, and every one was
checked on the same 40 series. To know whether a rule change helps or hurts, we need the SAME
inputs every time. A fixture freezes what every source answered for one series on one day, so a
rule can be replayed against all of them in a second, without the network and without the
sources moving underneath us. The time is frozen too (dates like "quiet for 57 days" depend on
today).

A fixture file (fixtures/<AniList id>.json) holds:
    the INPUT   what each source function returned, the any-language MangaDex answer, the AniList
                status, your reading progress and rhythm override, and the day it was recorded
    the GOLDEN  what the code answered on that input at recording time. A replay that differs
                means a rule changed its mind: review it, then `devtools.py replay --update`.
fixtures/truth.json holds what the WEB says (hand-checked, each with its source), kept apart
because it is evidence, not output. A truth marked "enforced" fails the tests when the code
disagrees; the others only count in `devtools.py score`.

    python3 devtools.py record NAME... | --all | --id 123 456     record live (needs the network)
    python3 devtools.py replay [--update]                         replay offline, show what changed
"""

import datetime as dt
import glob
import json
import os
from unittest import mock

import server

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "fixtures")
TRUTH_FILE = os.path.join(DIR, "truth.json")
FIXTURE_VERSION = 1


# ---------- frozen time ----------

def frozen_datetime(when_iso):
    """A datetime class whose now() is always `when_iso` (what server.py uses for "today")."""
    frozen = server.parse_time(when_iso)

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    return Frozen


# ---------- replay ----------

GOLDEN_KEYS = ("chapter", "agreement", "source", "countOnly", "hiatus", "dormant", "limited", "quietDays", "typicalGapDays")


def snapshot(result):
    """The part of an answer worth freezing (dates cut to the day)."""
    snap = {k: result.get(k) for k in GOLDEN_KEYS}
    snap["date"] = (result.get("publishedAt") or "")[:10] or None
    snap["sources"] = [
        {"name": s["name"], "chapter": s["chapter"], "plausible": s["plausible"]} for s in result.get("sources", [])
    ]
    return snap


def replay(fx):
    """Run the current rules on a fixture's recorded input. Returns (full result, snapshot)."""
    def source(name):
        recorded = fx["sources"].get(name)
        return lambda *a: recorded  # a source the fixture doesn't have (added later) answers "nothing"

    sources = [(name, source(name)) for name, _ in server.SOURCES]
    any_language = fx.get("anyLanguage")
    with mock.patch.object(server, "SOURCES", sources), mock.patch.object(server, "_cache", {}), \
            mock.patch.object(server, "datetime", frozen_datetime(fx["recordedAt"])), \
            mock.patch("time.sleep"), \
            mock.patch.object(server, "mangadex_any_language_candidate", lambda *a: any_language):
        result = server.chapter_info(
            fx["id"], fx["titles"], fx.get("year"), fx.get("read"), fx["status"], None, gap_override=fx.get("gapDays")
        )
    return result, snapshot(result)


def diff(old, new):
    """{key: (old, new)} for what differs between two snapshots (empty = identical)."""
    return {k: (old.get(k), new.get(k)) for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)}


# ---------- files ----------

def path_for(al_id):
    return os.path.join(DIR, f"{al_id}.json")


def load_all():
    out = []
    for p in sorted(glob.glob(os.path.join(DIR, "[0-9]*.json"))):
        with open(p, encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def save(fx):
    os.makedirs(DIR, exist_ok=True)
    with open(path_for(fx["id"]), "w", encoding="utf-8") as f:
        json.dump(fx, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def load_truth():
    if not os.path.exists(TRUTH_FILE):
        return {}
    with open(TRUTH_FILE, encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


# ---------- truth ----------

def check_truth(truth, result):
    """Compare an answer with what the web says. Returns (ok, why-not)."""
    problems = []
    if truth.get("latest") is not None:
        want, got = server.chapter_number(truth["latest"]), server.chapter_number(result.get("chapter"))
        tol = truth.get("tolerance", 0)
        if got is None or want is None or abs(got - want) > tol:
            problems.append(f"chapter {result.get('chapter')} (web: {truth['latest']}{f' ±{tol}' if tol else ''})")
    if truth.get("dormant") is not None and bool(result.get("dormant")) != truth["dormant"]:
        problems.append(f"dormant={bool(result.get('dormant'))} (web: {truth['dormant']})")
    if truth.get("onBreak") is not None and (result.get("quietDays", 0) > 0 or bool(result.get("hiatus"))) != truth["onBreak"]:
        problems.append(f"onBreak={result.get('quietDays', 0) > 0} (web: {truth['onBreak']})")
    return (not problems, "; ".join(problems))


# ---------- recording (live) ----------

def record_live(al_id, titles, year, status, read=None, gap=None, matches=None, title=None):
    """Ask every source for real and freeze the answers. Needs the network."""
    import time

    raw = {}

    def wrap(name, fn):
        def inner(*a):
            raw[name] = fn(*a)
            return raw[name]
        return inner

    sources = [(name, wrap(name, fn)) for name, fn in server.SOURCES]
    with mock.patch.object(server, "SOURCES", sources), mock.patch.object(server, "_cache", {}):
        server.chapter_info(al_id, titles, year, read, status, matches, gap_override=gap)
    any_language = None
    if status == "RELEASING":  # always keep it, so a later rule that wants it can be replayed
        try:
            any_language = server.mangadex_any_language_candidate(al_id, titles, year)
        except Exception:
            any_language = None
    fx = {
        "version": FIXTURE_VERSION,
        "id": int(al_id),
        "title": title or titles[0],
        "titles": titles,
        "year": year,
        "status": status,
        "read": read,
        "gapDays": gap,
        "recordedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "sources": raw,
        "anyLanguage": any_language,
    }
    _, fx["golden"] = replay(fx)
    return fx
