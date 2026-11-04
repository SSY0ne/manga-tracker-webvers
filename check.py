#!/usr/bin/env python3
"""Daily check: compare your series with yesterday's state, notify Discord.

    python3 check.py --add "one piece"   add a series to series.json (pick from results)
    python3 check.py --dry               check, print what would be sent, change nothing
    python3 check.py --test              send a test message to Discord
    python3 check.py                     real run (this is what GitHub Actions does)

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
BREAK_WEEKS = int(os.environ.get("BREAK_WEEKS", "6"))
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
        return []

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
    for s in series:
        m = by_id.get(s["id"])
        if not m:
            print(f"! {s['title']}: not found on AniList, skipped")
            continue
        name = title_of(m)
        old = old_state.get(str(m["id"]))

        # Ask every source (MangaDex, MangaUpdates...) and let server.reconcile() choose.
        names = [m["title"].get("english"), m["title"].get("romaji")] + (m.get("synonyms") or [])
        year = (m.get("startDate") or {}).get("year")
        try:
            info = server.chapter_info(str(m["id"]), names, year, s.get("read"), m["status"])
        except Exception as err:  # every source down: keep the old info, try again tomorrow
            print(f"! {name}: lookup failed ({err}), keeping old data")
            info = dict(old or {})
            info.setdefault("limited", False)
        chapter, published, url = info.get("chapter"), info.get("publishedAt"), info.get("url")
        gap = info.get("typicalGapDays")

        # No source is coherent (see server.reconcile)? "Limited data": claim nothing.
        limited = bool(info.get("limited"))
        # "On break?" = way overdue compared to this series' own rhythm (see server.py).
        quiet_for = 0 if limited else server.quiet_days(m["status"], published, gap, BREAK_WEEKS)
        quiet = quiet_for > 0
        new_state = {
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
        }

        if old is None:
            # First time we see this series: remember it, don't spam.
            print(f"+ {name}: first check (chapter {chapter}, {STATUS_TEXT.get(m['status'])})")
        else:
            was_quiet = old.get("quiet", False)
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
                if was_quiet:
                    messages.append(f"▶️ **{name}** is back from break: chapter **{chapter}**{link}")
                else:
                    messages.append(f"📖 **{name}**: new chapter **{chapter}**{link}")
            elif quiet and not was_quiet:
                usual = f", usually every {round(gap)} days" if gap else ""
                weeks = quiet_for // 7
                how_long = f"over {weeks // 52} years" if weeks >= 104 else f"{weeks} weeks"
                messages.append(
                    f"⏸️ **{name}**: no new chapter for {how_long}{usual}, maybe on break "
                    f"(last was {chapter})"
                )
            if m["status"] != old.get("status"):
                messages.append(
                    f"ℹ️ **{name}** is now {STATUS_TEXT.get(m['status'], m['status'])} on AniList"
                )
        new_state_map[str(m["id"])] = new_state

    if not dry:
        write_json(
            state_path,
            {"checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "series": new_state_map},
        )
    return messages


def discord_configured():
    return os.environ.get("DISCORD_WEBHOOK_URL", "").startswith("https://discord.com/api/webhooks/")


def email_configured():
    return bool(os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"))


def send_discord(text):
    # Discord messages max out at 2000 characters.
    for i in range(0, len(text), 1900):
        post_json(os.environ["DISCORD_WEBHOOK_URL"], {"content": text[i : i + 1900]})


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
    p.add_argument("--import-mal", metavar="FILE", help="import a MyAnimeList XML export into series.json")
    p.add_argument("--mal-status", default="Reading", help='comma list, e.g. "Reading,Plan to Read"')
    p.add_argument("--series", default="series.json")
    p.add_argument("--state", default="state.json")
    args = p.parse_args()

    if args.add:
        return add_series(args.series, args.add)
    if args.import_mal:
        return import_mal(args.import_mal, args.series, {s.strip() for s in args.mal_status.split(",")})
    if args.test:
        notify(
            "✅ Manga Tracker is connected. You'll get chapter and break alerts here.",
            "Manga Tracker: test message",
        )
        return

    messages = check(args.series, args.state, args.dry)
    if not messages:
        print("Nothing new.")
        return
    text = "\n".join(messages)
    if args.dry or args.no_send:
        print("--- would send ---\n" + text)
    else:
        n = len(messages)
        notify(text, f"Manga Tracker: {n} update{'s' if n != 1 else ''}")
        print(f"{n} notification(s).")


if __name__ == "__main__":
    main()
