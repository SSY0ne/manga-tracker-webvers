#!/usr/bin/env python3
"""Daily check: compare your series with yesterday's state, notify Discord.

    python3 check.py --add "one piece"   add a series to series.json (pick from results)
    python3 check.py --dry               check, print what would be sent, change nothing
    python3 check.py --test              send a test message to Discord
    python3 check.py --alert "text"      send an urgent message (used when a daily run fails)
    python3 check.py                     real run (this is what GitHub Actions does)

Each run sends ONE digest: sections for new chapters / back from break / maybe on break /
status changes, heads-ups when a source failed, and a one-line summary. Nothing new = no
message (set DIGEST_ALWAYS=1 to also get an "all quiet" message). Per series, series.json can set
"alerts": "breaks" (only break news) or "off" (muted), and "gapDays": N (you know its rhythm), "decimals": true (count bonus chapters like 12.5).

Notifications go to every channel that is set up (env vars, never in files; on GitHub
they are repo secrets):
    Discord   DISCORD_WEBHOOK_URL
    Email     GMAIL_USER, GMAIL_APP_PASSWORD (a Gmail app password), optional EMAIL_TO

Files:
    series.json  the manga you follow (AniList ids)        <- you edit this
    state.json   what we saw last time (written by this script)
"""

import argparse
import json
import os
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

import server  # reuses the MangaDex lookup (latest_chapter) from server.py

ANILIST = "https://graphql.anilist.co"
UA = "manga-tracker-learning-project/0.1"  # Discord and AniList reject empty/default agents

STATUS_TEXT = {
    "RELEASING": "releasing",
    "HIATUS": "on hiatus",
    "FINISHED": "finished",
    "CANCELLED": "cancelled",
    "NOT_YET_RELEASED": "not yet released",
}


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def post_json(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.status


def anilist(query, variables):
    result = post_json_read(ANILIST, {"query": query, "variables": variables})
    if "errors" in result:
        raise RuntimeError(result["errors"][0]["message"])
    return result["data"]


def post_json_read(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.load(res)


def title_of(m):
    return m["title"].get("english") or m["title"].get("romaji")


def weeks_since(iso):
    then = datetime.fromisoformat(iso)
    return int((datetime.now(timezone.utc) - then).days // 7)


# ---------- adding series ----------

def add_series(path, text):
    data = anilist(
        """query ($s: String) { Page(perPage: 6) {
             media(search: $s, type: MANGA, sort: SEARCH_MATCH) { id title { romaji english } status }
        } }""",
        {"s": text},
    )
    results = data["Page"]["media"]
    if not results:
        print("No results.")
        return
    for i, m in enumerate(results, 1):
        print(f"  {i}. {title_of(m)}  [{STATUS_TEXT.get(m['status'], m['status'])}]")
    choice = input("Number to add (Enter to cancel): ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(results):
        print("Cancelled.")
        return
    m = results[int(choice) - 1]
    series = read_json(path, [])
    if any(s["id"] == m["id"] for s in series):
        print("Already in your list.")
        return
    series.append({"id": m["id"], "title": title_of(m)})
    write_json(path, series)
    print(f"Added {title_of(m)}.")


# ---------- import from a MyAnimeList export ----------

def import_mal(xml_path, series_path, statuses):
    """Add the series of a MAL XML export (MAL > Profile > Export) to series.json.

    The export has each manga's MAL id, and AniList can look a manga up by MAL id,
    so the match is exact (no guessing from titles). Only the statuses you ask for
    are imported; default is just "Reading". The XML file itself is never copied
    into the repo.
    """
    import xml.etree.ElementTree as ET  # your own export file, so the stdlib parser is fine

    rows = []
    for m in ET.parse(xml_path).getroot().iter("manga"):
        status = (m.findtext("my_status") or "").strip()
        if status in statuses:
            rows.append(
                {
                    "mal": int(m.findtext("manga_mangadb_id")),
                    "title": (m.findtext("manga_title") or "").strip(),
                    "read": int(m.findtext("my_read_chapters") or 0),
                }
            )
    print(f"{len(rows)} entries with status {sorted(statuses)} in the export.")

    found = {}  # MAL id -> AniList media
    for i in range(0, len(rows), 50):
        mal_ids = [r["mal"] for r in rows[i : i + 50]]
        data = anilist(
            """query ($ids: [Int]) { Page(perPage: 50) {
                 media(idMal_in: $ids, type: MANGA) { id idMal title { romaji english } }
            } }""",
            {"ids": mal_ids},
        )
        found.update({m["idMal"]: m for m in data["Page"]["media"]})
        time.sleep(1)

    series = read_json(series_path, [])
    by_id = {s["id"]: s for s in series}
    added, updated, missing = 0, 0, []
    for r in rows:
        m = found.get(r["mal"])
        if not m:
            missing.append(r["title"])
            continue
        if m["id"] in by_id:
            by_id[m["id"]].update({"mal": r["mal"], "read": r["read"]})
            updated += 1
        else:
            entry = {"id": m["id"], "title": title_of(m), "mal": r["mal"], "read": r["read"]}
            series.append(entry)
            by_id[m["id"]] = entry
            added += 1
    write_json(series_path, series)
    print(f"Added {added}, already in your list {updated}, not found on AniList {len(missing)}.")
    for t in missing:
        print(f"  - {t}")


# ---------- the digest: what we actually send ----------
# Messages are collected as (kind, text) pairs during the run. Kinds: chapter, back, break, status.

DIGEST_SECTIONS = [
    ("chapter", "New chapters"),
    ("back", "Back from break"),
    ("break", "Maybe on break"),
    ("dormant", "Now dormant"),
    ("status", "Status changes"),
]


def should_alert(kind, setting):
    """Per-series alert setting from series.json: "all" (default), "breaks" or "off"."""
    setting = setting or "all"
    if setting == "off":
        return False
    if setting == "breaks":
        return kind in ("break", "back", "dormant")
    return True


def build_digest(items, notes=(), when=None, tracked=0, on_break=0, always=False, dormant=0):
    """One tidy message per run: grouped sections, then heads-ups, then a one-line summary.

    Returns "" when there is nothing to say (no news, no heads-ups), unless always=True, which
    sends a short "all quiet" message (set the repo variable DIGEST_ALWAYS=1 for that).
    """
    if not items and not notes and not always:
        return ""
    when = when or datetime.now(timezone.utc)
    lines = [f"📚 **Manga Tracker** · {when:%a %b} {when.day}"]
    for kind, label in DIGEST_SECTIONS:
        texts = [t for k, t in items if k == kind]
        if texts:
            lines += ["", f"**{label} ({len(texts)})**", *texts]
    if notes:
        lines += ["", "**Heads up**", *notes]
    if not items and not notes:
        lines += ["", "Nothing new today."]
    # Plain text on purpose: the same message goes out by email, where Discord's italics would show up as _ marks
    lines += ["", f"Tracking {tracked} series · {on_break} on break" + (f" · {dormant} dormant" if dormant else "")]
    return "\n".join(lines)


def dormancy_change(old, dormant):
    """What a change in the dormant flag means: "dormant" (say so), "dormant_silent" (the state had no
    flag yet, so this is the first run with the rule: log it, don't announce series that were quiet
    for years already), "revived" (it was dormant and is not any more) or None."""
    was = bool(old.get("dormant"))
    if dormant and not was:
        return "dormant" if "dormant" in old else "dormant_silent"
    if was and not dormant:
        return "revived"
    return None


def chunk_lines(text, limit=1900):
    """Split text into pieces of at most `limit` characters, breaking at line ends
    (Discord refuses messages over 2000 characters). A single huge line is cut hard."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # a line longer than a whole message
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def rule_counts(state_map):
    """{rule name: number of series it changed the answer for} from the per-series "rules" lists."""
    counts = {}
    for entry in state_map.values():
        for rule in entry.get("rules") or []:
            counts[rule] = counts.get(rule, 0) + 1
    return counts


def rule_notes(counts, previous, total_series):
    """Heads-ups when a rule suddenly fires for many more series than on the previous run.

    A rule was written for one kind of mistake in the data. If it starts changing the answer for a lot
    more series overnight, the data (or the rule) has probably changed: worth a look. Needs a previous
    run to compare with, and the jump must be at least 3 series and 10% of the list."""
    notes = []
    if not previous:
        return notes
    for rule, n in sorted(counts.items()):
        was = previous.get(rule, 0)
        if n - was >= max(3, total_series // 10):
            notes.append(f"⚠️ rule \"{server.RULE_LABELS.get(rule, rule)}\" changed the answer for {n} series (was {was}): check the data")
    return notes


def health_notes(stats, total_series, lookup_failed):
    """Heads-ups about the run itself: a source that mostly failed, or series nobody answered for."""
    notes = []
    for name, s in stats.items():
        attempts = s["ok"] + s["none"] + s["failed"]
        if attempts and s["failed"] / attempts >= 0.5:
            notes.append(f"⚠️ {name} failed for {s['failed']} of {attempts} lookups, so its data may be stale")
    if total_series and lookup_failed >= max(3, total_series // 4):
        notes.append(f"⚠️ {lookup_failed} of {total_series} series got no answer from any source")
    return notes


# ---------- the check ----------

def chapter_delta(old, new):
    """How many chapters ahead `new` is of `old` (None if either is missing or not a number)."""
    try:
        return float(new) - float(old)
    except (TypeError, ValueError):
        return None


def check(series_path, state_path, dry):
    series = read_json(series_path, [])
    raw = read_json(state_path, {})
    # state.json is {"checkedAt": ..., "series": {id: {...}}}. The first version of
    # this file was just {id: {...}}, so accept both.
    old_state = raw.get("series", raw)
    new_state_map = {}  # only series still in series.json are kept
    if not series:
        print("series.json is empty. Add one with: python3 check.py --add \"title\"")
        return {"messages": [], "events": [], "notes": [], "tracked": 0, "on_break": 0, "dormant": 0}

    ids = [s["id"] for s in series]
    by_id = {}
    for i in range(0, len(ids), 50):  # AniList returns at most 50 per page
        data = anilist(
            """query ($ids: [Int]) { Page(perPage: 50) {
                 media(id_in: $ids, type: MANGA) {
                   id title { romaji english } synonyms startDate { year } status coverImage { medium }
                 }
            } }""",
            {"ids": ids[i : i + 50]},
        )
        by_id.update({m["id"]: m for m in data["Page"]["media"]})

    messages = []
    events = []  # what changed this run; appended to history.jsonl (see log_events)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    server.reset_stats()  # per-source counters for the Health tab
    started = time.time()
    lookup_failed = 0  # series where EVERY source failed
    for s in series:
        m = by_id.get(s["id"])
        if not m:
            print(f"! {s['title']}: not found on AniList, skipped")
            continue
        name = title_of(m)
        old = old_state.get(str(m["id"]))

        def event(kind, **details):
            events.append({"t": now, "id": m["id"], "title": name, "type": kind, **details})

        def say(kind, text):  # an alert, if this series' own setting (all / breaks / off) allows it
            if should_alert(kind, s.get("alerts")):
                messages.append((kind, text))

        # Ask every source (MangaDex, MangaUpdates...) and let server.reconcile() choose.
        names = [m["title"].get("english"), m["title"].get("romaji")] + (m.get("synonyms") or [])
        year = (m.get("startDate") or {}).get("year")
        # Remembered matches skip the title search; a manual "mu" id in series.json wins.
        matches = dict((old or {}).get("matches") or {})
        if s.get("mu"):
            matches["MangaUpdates"] = {"id": int(s["mu"])}
        try:
            info = server.chapter_info(
                str(m["id"]), names, year, s.get("read"), m["status"], matches, gap_override=s.get("gapDays"), keep_decimals=bool(s.get("decimals"))
            )
        except Exception as err:  # every source down: keep the old info, try again tomorrow
            print(f"! {name}: lookup failed ({err}), keeping old data")
            lookup_failed += 1
            info = dict(old or {})
            info.setdefault("limited", False)
        chapter, published, url = info.get("chapter"), info.get("publishedAt"), info.get("url")
        gap = info.get("typicalGapDays")

        # No source is coherent (see server.reconcile)? "Limited data": claim nothing.
        limited = bool(info.get("limited"))
        # "On break?" = way overdue compared to this series' own rhythm (see server.py).
        quiet_for = 0 if limited else info.get("quietDays", 0)  # decided in server.chapter_info
        # A second opinion on breaks: MangaBaka lists the series as "hiatus".
        hiatus = bool(info.get("hiatus")) and m["status"] == "RELEASING" and not limited
        # Dormant (see server.dormant_days): years without a chapter. Not "on break" any more: a break is
        # a pause we expect to end, so a dormant series sends no break alerts.
        dormant = bool(info.get("dormant")) and not limited
        dormant_for = info.get("dormantDays", 0) if dormant else 0
        quiet = (quiet_for > 0 or hiatus) and not dormant
        new_state = {
            "hiatus": hiatus,
            "countOnly": bool(info.get("countOnly")),
            "matches": info.get("matches", matches),
            "agreement": info.get("agreement"),
            "source": info.get("source"),
            "sources": info.get("sources", []),
            "limited": limited,
            "title": name,
            "cover": (m.get("coverImage") or {}).get("medium"),
            "status": m["status"],
            "chapter": chapter,
            "publishedAt": published,
            "url": url,
            "typicalGapDays": gap,
            "expectedNext": info.get("expectedNext"),
            "history": info.get("history", []),
            "quiet": quiet,
            "quietDays": quiet_for,
            "dormant": dormant,
            "dormantDays": dormant_for,
            "rules": info.get("rules", []),  # which rules changed this answer (server.RULE_LABELS)
            "statusVotes": info.get("statusVotes", []),  # Shikimori: paused / discontinued / last chapter date
        }

        if old is None:
            # First time we see this series: remember it, don't spam.
            print(f"+ {name}: first check (chapter {chapter}, {STATUS_TEXT.get(m['status'])})")
            event("first_seen", chapter=chapter, source=info.get("source"), status=m["status"])
        else:
            was_quiet = old.get("quiet", False)
            change = dormancy_change(old, dormant)
            delta = chapter_delta(old.get("chapter"), chapter)
            # Only compare like with like: the same source as last time, and a series
            # that is actually running. Switching sources is a data correction.
            comparable = old.get("source") == info.get("source") and m["status"] in ("RELEASING", "HIATUS")
            if limited or not comparable:
                pass  # no source can be trusted (or we changed source): say nothing about chapters
            elif delta is not None and 0 < delta <= 5:
                # A real new chapter. (A drop or a big jump is just us switching to a
                # better source, a data correction, so it never triggers an alert.)
                link = f"\n{url}" if url else ""
                if old.get("dormant"):
                    say("back", f"▶️ **{name}** is back after years of silence: chapter **{chapter}**{link}")
                elif was_quiet:
                    say("back", f"▶️ **{name}** is back from break: chapter **{chapter}**{link}")
                else:
                    say("chapter", f"📖 **{name}**: new chapter **{chapter}**{link}")
            elif quiet and not was_quiet:
                if quiet_for:
                    usual = f", usually every {round(gap)} days" if gap else ""
                    weeks = quiet_for // 7
                    how_long = f"over {weeks // 52} years" if weeks >= 104 else f"{weeks} weeks"
                    say(
                        "break",
                        f"⏸️ **{name}**: no new chapter for {how_long}{usual}, maybe on break "
                        f"(last was {chapter})",
                    )
                else:  # only the hiatus listing says so
                    say("break", f"⏸️ **{name}** is listed as on hiatus (last chapter {chapter})")
            if change == "dormant":
                since = f"no new chapter for over {dormant_for // 365} years" if dormant_for >= 365 else "listed as discontinued"
                say("dormant", f"💤 **{name}**: {since}, marked dormant (the author may never return; last was {chapter})")
            if m["status"] != old.get("status"):
                say("status", f"ℹ️ **{name}** is now {STATUS_TEXT.get(m['status'], m['status'])} on AniList")

            # ---- the same facts, for the history log (alerts are a subset of these) ----
            if not limited and comparable and delta is not None and 0 < delta <= 5:
                event("chapter", chapter=chapter, previous=old.get("chapter"), source=info.get("source"), publishedAt=published)
            elif old.get("source") and info.get("source") and old["source"] != info["source"] and chapter != old.get("chapter"):
                # We switched to a better source: a data correction, kept so the log explains the jump.
                event("source_change", chapter=chapter, previous=old.get("chapter"), source=info["source"], previousSource=old["source"])
            if quiet and not was_quiet:
                event("break_start", reason="dates" if quiet_for else "hiatus", quietDays=quiet_for or None, chapter=chapter)
            elif was_quiet and not quiet and not dormant:  # (a break that became dormant did not end)
                event("break_end", chapter=chapter)
            if change in ("dormant", "dormant_silent"):
                event("dormant", chapter=chapter, dormantDays=dormant_for)
            elif change == "revived":
                event("revived", chapter=chapter)
            if m["status"] != old.get("status"):
                event("status", previous=old.get("status"), status=m["status"])
        new_state_map[str(m["id"])] = new_state

    if not dry:
        finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sources = {k: {**v, "seconds": round(v["seconds"], 1)} for k, v in server.STATS.items()}
        health = {
            "startedAt": now,
            "finishedAt": finished,
            "durationSec": round(time.time() - started),
            "series": len(new_state_map),
            "lookupFailed": lookup_failed,
            "sources": sources,
            "rules": rule_counts(new_state_map),
            "ruleLabels": {r: server.RULE_LABELS.get(r, r) for r in rule_counts(new_state_map)},  # so the page needn't copy them
        }
        # One compact "run" line per run, so the Health tab can show a trend.
        events.append(
            {
                "t": finished, "id": 0, "title": "(daily check)", "type": "run",
                "durationSec": health["durationSec"], "series": health["series"], "lookupFailed": lookup_failed,
                "sources": {k: {f: v[f] for f in ("ok", "none", "failed", "rateLimited")} for k, v in sources.items()},
            }
        )
        write_json(state_path, {"checkedAt": now, "series": new_state_map, "health": health})
        log_events(history_path_for(state_path), events)
    return {
        "messages": messages,
        "events": events,
        "notes": health_notes(server.STATS, len(series), lookup_failed)
        + (rule_notes(rule_counts(new_state_map), (raw.get("health") or {}).get("rules"), len(series)) if not dry else []),
        "tracked": len(new_state_map),
        "on_break": sum(1 for v in new_state_map.values() if v["quiet"]),
        "dormant": sum(1 for v in new_state_map.values() if v["dormant"]),
    }


def history_path_for(state_path):
    """history.jsonl sits next to state.json (so a sandbox copy never touches the real log)."""
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), "history.jsonl")


def log_events(path, events):
    """Append one JSON object per line. An append-only log: past lines are never rewritten."""
    if not events:
        return
    with open(path, "a") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def discord_configured():
    return os.environ.get("DISCORD_WEBHOOK_URL", "").startswith("https://discord.com/api/webhooks/")


def email_configured():
    return bool(os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"))


def send_discord(text):
    # Discord messages max out at 2000 characters: split at line ends so no entry is cut in half.
    for piece in chunk_lines(text, 1900):
        post_json(os.environ["DISCORD_WEBHOOK_URL"], {"content": piece})


def send_email(subject, text):
    """Send through Gmail. Needs a Gmail *app password* (not your normal password):
    Google Account > Security > 2-Step Verification > App passwords."""
    user = os.environ["GMAIL_USER"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ.get("EMAIL_TO") or user  # default: send to yourself
    msg.set_content(text.replace("**", ""))  # the ** are Discord bold marks
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))  # Google shows it with spaces
        smtp.send_message(msg)


def notify(text, subject):
    """Send to every configured channel. One failing channel doesn't block the other."""
    channels = []
    if discord_configured():
        channels.append(("Discord", lambda: send_discord(text)))
    if email_configured():
        channels.append(("Email", lambda: send_email(subject, text)))
    if not channels:
        sys.exit(
            "No notification channel set up. Set DISCORD_WEBHOOK_URL and/or "
            "GMAIL_USER + GMAIL_APP_PASSWORD."
        )
    ok, failed = [], []
    for name, send in channels:
        try:
            send()
            ok.append(name)
        except Exception as err:  # the message never contains a secret
            failed.append(f"{name}: {type(err).__name__}: {err}")
    if ok:
        print("Sent via " + ", ".join(ok) + ".")
    if failed:
        print("FAILED -> " + "; ".join(failed))
    if not ok:
        sys.exit(1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--add", metavar="TITLE", help="search AniList and add a series")
    p.add_argument("--dry", action="store_true", help="print messages, don't send, don't save state")
    p.add_argument("--no-send", action="store_true", help="save state and print messages, but send nothing")
    p.add_argument("--test", action="store_true", help="send a test message")
    p.add_argument("--alert", metavar="MESSAGE", help="send an urgent message (the workflow uses it when a run fails)")
    p.add_argument("--import-mal", metavar="FILE", help="import a MyAnimeList XML export into series.json")
    p.add_argument("--mal-status", default="Reading", help='comma list, e.g. "Reading,Plan to Read"')
    p.add_argument("--series", default="series.json")
    p.add_argument("--state", default="state.json")
    args = p.parse_args()

    if args.add:
        return add_series(args.series, args.add)
    if args.import_mal:
        return import_mal(args.import_mal, args.series, {s.strip() for s in args.mal_status.split(",")})
    if args.alert:
        notify(f"⚠️ {args.alert}", "Manga Tracker: the daily check failed")
        return
    if args.test:
        notify(
            "✅ Manga Tracker is connected. You'll get chapter and break alerts here.",
            "Manga Tracker: test message",
        )
        return

    result = check(args.series, args.state, args.dry)
    if result["events"]:
        print(f"{len(result['events'])} event(s) {'would be ' if args.dry else ''}logged to history.jsonl")
    items, notes = result["messages"], result["notes"]
    digest = build_digest(
        items, notes, tracked=result["tracked"], on_break=result["on_break"], dormant=result["dormant"],
        always=os.environ.get("DIGEST_ALWAYS") == "1",  # a repo variable: also send an "all quiet" day
    )
    if not digest:
        print("Nothing new.")
        return
    if args.dry or args.no_send:
        print("--- would send ---\n" + digest)
    else:
        n = len(items)
        subject = f"Manga Tracker: {n} update{'s' if n != 1 else ''}" if n else "Manga Tracker: heads up"
        notify(digest, subject)
        print(f"Digest sent ({n} update(s), {len(notes)} heads-up(s)).")


if __name__ == "__main__":
    main()
