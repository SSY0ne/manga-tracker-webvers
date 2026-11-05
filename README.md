# Manga Tracker

Track the manga you read and see at a glance what has a new chapter and what looks
like it is on break.

## What it does

- Search manga (AniList) and add it to your list (`series.json`).
- Shows the latest chapter and its date, linked to the official reader (MangaDex data).
- Learns each series' **own rhythm** from its chapter history and flags **On break?**
  when it is far more overdue than usual (at least 2.5x its usual gap and 2 weeks more).
  AniList is slow to mark hiatus, and a fixed "6 weeks" would flag slow series all the time.
- **Import from MyAnimeList**: upload the .xml export (MAL, Profile, Export, manga list)
  on the page, or run `python3 check.py --import-mal FILE.xml`. Series are matched
  exactly by MAL id, and your chapters-read is kept so cards show "17 behind".
- **Several chapter sources, compared.** Four sources, each a small function in
  `server.py`: **MangaDex**, **MangaUpdates** and **Webtoon RSS** give chapters with dates;
  **MangaBaka** gives the chapter count and a hiatus status, and also lists the series' ids on
  the other sites, so those are matched exactly instead of by title. `reconcile()` keeps the
  answer that is coherent: not in the future, not far below what you have read or below the
  MangaBaka count, and the freshest when they differ. If no dated source fits, the MangaBaka
  count is used ("count only", no date). Each card says whether the sources agree, disagree
  or only one has data.
- **Matching a series on each source.** MangaDex has an exact AniList link. MangaUpdates
  does not, so a series is matched by title similarity (at least 0.85, ignoring bracketed
  bits) AND start year within one year, which also tells two manga with the same name apart.
  Matches are remembered in `state.json`, so later runs skip the search. If one never
  matches, find its id with `python3 devtools.py match NAME` and add `"mu": <id>` to its
  entry in `series.json`.
- **Limited data**: when no source fits (licensed series lose their chapters on both),
  the series is marked "Limited data" and gets no break or new-chapter alerts instead of
  wrong ones. Alerts also only fire when the same source reports a +1 to +5 chapter step,
  so switching sources never causes a fake "new chapter".
- **A cover-first design**: your library is a grid of covers, each with one label (NEW, ON BREAK,
  FINISHED), a "+N behind" badge and a thin progress bar. A "new this week" row sits on top, and
  the stat chips under the header (tracked / new / on break / behind) are click-to-filter. Click a
  cover to open a detail panel (a side panel on desktop, a bottom sheet on phones) with the
  chapter facts, your progress editor and what every source said. The search to add a manga is
  at the top and the MyAnimeList import is at the very bottom of the list (both only when
  `server.py` is running).
- **Your reading progress**: every card has an "update" link next to "You've read N · M behind".
  Type the chapter you are really on, or press "I'm up to date" to jump to the latest known
  chapter. With `server.py` it is saved to `series.json` (commit it so the bot knows); on a
  read-only host it is kept in your browser only. Filters, sorts and the "behind" count follow.
- **Search, filter and sort** your list: type to filter by title; show only releasing, on
  break, new this week, you're behind, sources disagree, count only, or finished; sort by
  title, most chapters behind, newest chapter, next chapter soonest, or on break first.
  The view is remembered in your browser.
- **History log** (`history.jsonl`, one JSON event per line, append-only). The bot adds an event
  whenever something changes: `first_seen`, `chapter`, `break_start`, `break_end`, `status`,
  and `source_change` (a data correction, so a jump in numbers is explained). The **Activity
  tab** shows the newest events and a 7-day summary.
- **Health tab** ("can I trust what I see?"): the bot records how each source behaved on its
  last run (answered / no entry / failed / rate-limited / seconds / error messages, saved in
  `state.json`) plus a short `run` line per day in the log. The tab shows a red or orange banner
  (and a dot on the tab) when a source was down or the bot hasn't run for 36 hours, the per-source
  numbers, how many series rest on a single source, and the last 14 runs.
- **Calendar tab**: released chapters, expected next chapters, an overdue list, and a
  `.ics` download for your calendar app.
- The page is a dashboard for the daily bot: it reads `series.json` and `state.json`.
  Run with `server.py` it can also add/remove series and refresh live; on a static
  host it is read-only.

## Run it

No installs, Python 3 only:

```bash
python3 server.py
```

Open http://127.0.0.1:8123.

`server.py` serves the page and proxies one request to MangaDex, because MangaDex
blocks calls made directly from a browser page (CORS).

## Discord alerts

`check.py` compares your series (`series.json`) with what it saw last time
(`state.json`) and posts to a Discord channel when there is a new chapter, a series
goes quiet (maybe on break), comes back from break, or AniList changes its status.
A GitHub Actions workflow (`.github/workflows/check.yml`) runs it every day.

```bash
python3 check.py --add "one piece"   # add a series
python3 check.py --dry               # see what would be sent
python3 check.py --test              # needs DISCORD_WEBHOOK_URL
```

## Email alerts (Gmail)

Same messages by email. Create a Gmail **app password** (Google Account, Security,
2-Step Verification, App passwords) and set these as repo secrets: `GMAIL_USER`,
`GMAIL_APP_PASSWORD`, and optionally `EMAIL_TO` (defaults to yourself). Discord and
email can both be on; either one alone also works.

Webhook URLs and passwords are secrets: only ever put them in repo secrets or
environment variables, never in a file you commit.

## Dev tools

```bash
python3 devtools.py report            # summary of state.json: agree / conflict / limited / on break
python3 devtools.py history [NAME]    # newest events of the history log (optionally one series)
python3 devtools.py backfill          # one-time: fill the log from chapter dates already in state.json
python3 devtools.py match NAME        # why a series doesn't match on MangaUpdates (+ ids to copy)
python3 devtools.py restart           # restart server.py on :8123
python3 devtools.py sandbox [FILE.xml]  # full check on COPIES of your files, sends nothing
python3 check.py --no-send            # same check on the real files: saves state, sends nothing
```

## Roadmap

- [x] Search and list (AniList)
- [x] Latest chapter (MangaDex)
- [x] "On break?" detection from each series' own rhythm
- [x] Calendar tab (+ .ics)
- [x] Daily check with Discord alerts (GitHub Actions)
- [ ] Deploy (GitHub Pages plus a small hosted function)

## Data sources

[AniList](https://anilist.gg/) GraphQL API for titles, covers and status;
[MangaDex](https://api.mangadex.org/docs/), [MangaUpdates](https://api.mangaupdates.com/) and [MangaBaka](https://mangabaka.org/) APIs and Webtoon's public RSS feeds for chapters and dates. Only metadata and
links to official sources are used.
