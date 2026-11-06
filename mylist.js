// My list: the pure parts (no page, no network), so they can be tested without a browser.
// Loaded by index.html before app.js; test_js.py loads it in a JavaScript engine and checks it.

// Rows of a MyAnimeList manga export (the .xml you get from Profile > Export), keeping only the wanted
// statuses. Read with plain text matching, not a DOM parser, so it runs anywhere.
function parseMalExport(text, wanted) {
  if (!/<myanimelist[\s>]/i.test(text)) return { error: "That doesn't look like a MyAnimeList export (.xml)." };
  const blocks = text.match(/<manga>[\s\S]*?<\/manga>/gi) || [];
  const tag = (block, name) => {
    const m = block.match(new RegExp("<" + name + ">\\s*(?:<!\\[CDATA\\[)?([^<\\]]*)", "i"));
    return m ? m[1].trim() : "";
  };
  const seen = new Set();
  const rows = [];
  for (const b of blocks) {
    const mal = Number(tag(b, "manga_mangadb_id"));
    const status = tag(b, "my_status");
    if (!(mal > 0) || !wanted.includes(status) || seen.has(mal)) continue;
    seen.add(mal);
    rows.push({ mal, status, read: Number(tag(b, "my_read_chapters")) || 0 });
  }
  if (rows.length === 0) return { error: "No manga with those statuses in this file (is it the manga export, not anime?)." };
  return { rows };
}

// A file exported from My list (or a bare list of {id, title, read}). Everything in it is untrusted.
function parseMineFile(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (err) {
    return { error: "That file isn't valid JSON." };
  }
  const list = Array.isArray(data) ? data : data && Array.isArray(data.series) ? data.series : null;
  if (!list) return { error: "That doesn't look like a Manga Tracker list." };
  const items = [];
  const seen = new Set();
  let invalid = 0;
  for (const x of list) {
    if (!x || !Number.isInteger(x.id) || x.id <= 0 || typeof x.title !== "string" || !x.title.trim()) {
      invalid += 1;
      continue;
    }
    if (seen.has(x.id)) continue;
    seen.add(x.id);
    const read = typeof x.read === "number" && Number.isFinite(x.read) && x.read >= 0 ? Math.min(100000, Math.floor(x.read)) : 0;
    items.push({ id: x.id, title: x.title.trim().slice(0, 200), read });
  }
  if (items.length === 0) return { error: invalid ? "Nothing usable in that file." : "That list is empty." };
  return { items, invalid };
}

// Put imported items into the list. A series already there stays as it is (its chapters-read is only filled in
// when it had none). Stops at `max` entries. Returns the new entries and what happened to the rest.
function mergeIntoMine(existing, items, max) {
  const byId = new Map(existing.map((e) => [e.id, e]));
  const added = [];
  let already = 0;
  let skipped = 0;
  for (const it of items) {
    const e = byId.get(it.id);
    if (e) {
      already += 1;
      if (!e.read && it.read) e.read = it.read;
      continue;
    }
    if (existing.length + added.length >= max) {
      skipped += 1;
      continue;
    }
    added.push({ id: it.id, title: it.title, read: it.read, pending: true });
  }
  return { added, already, skipped };
}

// What "Export my list" writes: only what cannot be looked up again (the rest is fetched fresh on import).
function buildMineExport(entries, isoNow) {
  return {
    app: "manga-tracker",
    version: 1,
    exportedAt: isoNow,
    series: entries.map((e) => ({ id: e.id, title: e.title, read: e.read || 0 })),
  };
}
