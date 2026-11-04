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
- **Limited data**: MangaDex loses the English chapters of many licensed series. When its
  latest chapter is far below what you have read, the series is marked "Limited data" and
  gets no break or new-chapter alerts, instead of wrong ones.
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

## Roadmap

- [x] Search and list (AniList)
- [x] Latest chapter (MangaDex)
- [x] "On break?" detection from each series' own rhythm
- [x] Calendar tab (+ .ics)
- [x] Daily check with Discord alerts (GitHub Actions)
- [ ] Deploy (GitHub Pages plus a small hosted function)

## Data sources

[AniList](https://anilist.gg/) GraphQL API for titles, covers and status;
[MangaDex](https://api.mangadex.org/docs/) API for chapter dates. Only metadata and
links to official sources are used.
