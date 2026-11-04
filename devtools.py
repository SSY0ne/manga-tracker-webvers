#!/usr/bin/env python3
"""Dev helpers for Manga Tracker: the things we kept doing by hand.

    python3 devtools.py report            summarize state.json: sources agree / conflict / limited / on break
    python3 devtools.py restart           stop whatever listens on :8123 and start server.py again
    python3 devtools.py sandbox           run the full check on COPIES of series.json/state.json
    python3 devtools.py sandbox FILE.xml  ...after importing a MyAnimeList export into the copy first

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
    print("agreement:", ", ".join(f"{k} {n}" for k, n in sorted(counts.items())))

    short = {"MangaDex": "MD", "MangaUpdates": "MU"}  # new sources: first 4 letters

    def sources_text(v):
        return " | ".join(
            f"{short.get(s['name'], s['name'][:4])} {s['chapter']} ({s['publishedAt'][:7]})"
            f"{'' if s['plausible'] else ' x'}"
            for s in v.get("sources", [])
        )

    for label, rows in (
        ("CONFLICT (freshest kept)", [v for v in series.values() if v.get("agreement") == "conflict"]),
        ("LIMITED (releasing, no source fits)", [v for v in series.values() if v.get("limited")]),
        ("ON BREAK?", [v for v in series.values() if v.get("quiet")]),
    ):
        if rows:
            print(f"\n{label}:")
            for v in rows:
                print(f"  {v['title'][:34]:34} used={v.get('source') or '-':12} {sources_text(v)}")


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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "report":
        report()
    elif cmd == "restart":
        restart()
    elif cmd == "sandbox":
        sandbox(sys.argv[2] if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
