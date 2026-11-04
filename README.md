# Manga Tracker

Track the manga you read and see at a glance what has a new chapter and what looks
like it is on break.

## What it does

- Search manga (AniList) and add it to your list.
- Shows the latest chapter and its date, linked to the official reader (MangaDex data).
- Flags **On break?** when a Releasing series has had no new chapter for N weeks
  (3 to 12, default 6), because AniList is slow to mark hiatus.
- **Refresh statuses** reports new chapters, series back from break, and series that
  just went quiet.

Your list is stored in the browser (`localStorage`) for now.

## Run it

No installs, Python 3 only:

```bash
python3 server.py
```

Open http://127.0.0.1:8123.

`server.py` serves the page and proxies one request to MangaDex, because MangaDex
blocks calls made directly from a browser page (CORS).

## Roadmap

- [x] Search and list (AniList)
- [x] Latest chapter (MangaDex)
- [x] "On break?" detection
- [ ] Daily check with Discord or email alerts (GitHub Actions)
- [ ] Deploy (GitHub Pages plus a small hosted function)

## Data sources

[AniList](https://anilist.gg/) GraphQL API for titles, covers and status;
[MangaDex](https://api.mangadex.org/docs/) API for chapter dates. Only metadata and
links to official sources are used.
