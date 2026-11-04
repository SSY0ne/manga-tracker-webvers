#!/usr/bin/env python3
"""Daily check: compare your series with yesterday's state, notify Discord.

    python3 check.py --add "one piece"   add a series to series.json (pick from results)
    python3 check.py --dry               check, print what would be sent, change nothing
    python3 check.py --test              send a test message to Discord
    python3 check.py                     real run (this is what GitHub Actions does)

Needs the env var DISCORD_WEBHOOK_URL for real runs. Never put that URL in a file:
it is a secret. Locally:  export DISCORD_WEBHOOK_URL='...'  (GitHub: repo secret).

Files:
    series.json  the manga you follow (AniList ids)        <- you edit this
    state.json   what we saw last time (written by this script)
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

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


# ---------- the check ----------

def check(series_path, state_path, dry):
    series = read_json(series_path, [])
    state = read_json(state_path, {})
    if not series:
        print("series.json is empty. Add one with: python3 check.py --add \"title\"")
        return []

    ids = [s["id"] for s in series]
    data = anilist(
        """query ($ids: [Int]) { Page(perPage: 50) {
             media(id_in: $ids, type: MANGA) { id title { romaji english } status }
        } }""",
        {"ids": ids},
    )
    by_id = {m["id"]: m for m in data["Page"]["media"]}

    messages = []
    for s in series:
        m = by_id.get(s["id"])
        if not m:
            print(f"! {s['title']}: not found on AniList, skipped")
            continue
        name = title_of(m)
        old = state.get(str(m["id"]))

        try:
            latest = server.latest_chapter(str(m["id"]), [m["title"].get("english"), m["title"].get("romaji")])
        except Exception as err:  # MangaDex hiccup: keep the old info, try again tomorrow
            print(f"! {name}: MangaDex lookup failed ({err}), keeping old data")
            latest = None
        time.sleep(0.4)  # be polite to MangaDex

        if latest and latest.get("found") and latest.get("chapter"):
            chapter, published, url = latest["chapter"], latest["publishedAt"], latest["url"]
        elif old:
            chapter, published, url = old.get("chapter"), old.get("publishedAt"), old.get("url")
        else:
            chapter = published = url = None

        quiet = bool(
            m["status"] == "RELEASING" and published and weeks_since(published) >= BREAK_WEEKS
        )
        new_state = {
            "status": m["status"],
            "chapter": chapter,
            "publishedAt": published,
            "url": url,
            "quiet": quiet,
        }

        if old is None:
            # First time we see this series: remember it, don't spam.
            print(f"+ {name}: first check (chapter {chapter}, {STATUS_TEXT.get(m['status'])})")
        else:
            was_quiet = old.get("quiet", False)
            if chapter and chapter != old.get("chapter"):
                link = f"\n{url}" if url else ""
                if was_quiet:
                    messages.append(f"▶️ **{name}** is back from break: chapter **{chapter}**{link}")
                else:
                    messages.append(f"📖 **{name}**: new chapter **{chapter}**{link}")
            elif quiet and not was_quiet:
                messages.append(
                    f"⏸️ **{name}**: no new chapter for {weeks_since(published)} weeks, maybe on break "
                    f"(last was {chapter})"
                )
            if m["status"] != old.get("status"):
                messages.append(
                    f"ℹ️ **{name}** is now {STATUS_TEXT.get(m['status'], m['status'])} on AniList"
                )
        state[str(m["id"])] = new_state

    if not dry:
        write_json(state_path, state)
    return messages


def send_discord(text):
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url.startswith("https://discord.com/api/webhooks/"):
        sys.exit("DISCORD_WEBHOOK_URL is missing or doesn't look like a Discord webhook URL.")
    # Discord messages max out at 2000 characters.
    for i in range(0, len(text), 1900):
        post_json(url, {"content": text[i : i + 1900]})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--add", metavar="TITLE", help="search AniList and add a series")
    p.add_argument("--dry", action="store_true", help="print messages, don't send, don't save state")
    p.add_argument("--test", action="store_true", help="send a test message")
    p.add_argument("--series", default="series.json")
    p.add_argument("--state", default="state.json")
    args = p.parse_args()

    if args.add:
        return add_series(args.series, args.add)
    if args.test:
        send_discord("✅ Manga Tracker is connected. You'll get chapter and break alerts here.")
        print("Test message sent.")
        return

    messages = check(args.series, args.state, args.dry)
    if not messages:
        print("Nothing new.")
        return
    text = "\n".join(messages)
    if args.dry:
        print("--- would send ---\n" + text)
    else:
        send_discord(text)
        print(f"Sent {len(messages)} notification(s).")


if __name__ == "__main__":
    main()
