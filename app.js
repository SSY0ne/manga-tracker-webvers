// Manga Tracker page.
//
// The list comes from two files in this repo:
//   series.json  the manga you follow (you edit it, or the page does when run locally)
//   state.json   what the daily bot (check.py) last saw: chapters, rhythm, cover...
// So this page is a dashboard for the bot: same list, same "on break" rule.
//
// When started with `python3 server.py` it can also add/remove series and refresh
// live. On a static host (GitHub Pages) it is read-only.

const ANILIST = "https://graphql.anilist.co";
const DAY = 24 * 60 * 60 * 1000;
const FIELDS = `id title { romaji english } status coverImage { medium }`;

const STATUS_LABEL = {
  RELEASING: "Releasing",
  HIATUS: "On break",
  FINISHED: "Finished",
  CANCELLED: "Cancelled",
  NOT_YET_RELEASED: "Not yet released",
};

let canEdit = false; // true only when server.py is running
let entries = []; // what we show (one per series)
let checkedAt = null;
let lastResults = [];
let calMonth = firstOfMonth(new Date());

// ---------- small helpers ----------

async function fetchJSON(path) {
  try {
    const res = await fetch(path);
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

function startOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}
function addDays(d, n) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}
function firstOfMonth(d) {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}
function dayKey(d) {
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}
function localDay(iso) {
  return startOfDay(new Date(iso)); // a timestamp -> the local calendar day
}
function parseDay(ymd) {
  const [y, m, d] = ymd.split("-").map(Number); // "2026-11-09" -> local midnight
  return new Date(y, m - 1, d);
}
function fmtShort(d) {
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
function fmtLong(d) {
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

// ---------- the "on break" rule ----------
// Same rule as quiet_days() in server.py (the bot uses that one). A Releasing series is
// "quiet" when it is way overdue compared to ITS OWN rhythm: at least 2.5x its usual
// gap and at least 2 weeks more. No rhythm known yet -> flat 6 weeks.

function quietDays(e) {
  if (e.status !== "RELEASING" || !e.publishedAt) return 0;
  const days = Math.floor((Date.now() - new Date(e.publishedAt).getTime()) / DAY);
  const gap = e.typicalGapDays;
  const limit = gap ? Math.max(2.5 * gap, gap + 14) : 6 * 7;
  return days >= limit ? days : 0;
}

// ---------- data: AniList + our server ----------

async function anilist(query, variables) {
  const res = await fetch(ANILIST, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  if (!res.ok) throw new Error(`AniList error ${res.status}`);
  const json = await res.json();
  if (json.errors) throw new Error(json.errors[0].message);
  return json.data;
}

function fromAniList(m) {
  return {
    id: m.id,
    title: m.title.english || m.title.romaji,
    cover: m.coverImage?.medium || null,
    status: m.status,
  };
}

async function searchManga(text) {
  const data = await anilist(
    `query ($s: String) { Page(perPage: 8) {
       media(search: $s, type: MANGA, sort: SEARCH_MATCH) { ${FIELDS} } } }`,
    { s: text }
  );
  return data.Page.media.map(fromAniList);
}

// Latest chapter + rhythm from MangaDex, through server.py (MangaDex blocks browsers).
async function fetchLatest(e) {
  const params = new URLSearchParams({ al: e.id, title: e.title });
  const data = await fetchJSON(`api/latest?${params}`);
  return data && data.found ? data : null;
}

// Fill entries with live data (AniList for covers/status, server.py for chapters).
async function fillLive(list) {
  if (list.length === 0) return;
  try {
    const data = await anilist(
      `query ($ids: [Int]) { Page(perPage: 50) {
         media(id_in: $ids, type: MANGA) { ${FIELDS} } } }`,
      { ids: list.map((e) => e.id) }
    );
    const byId = new Map(data.Page.media.map((m) => [m.id, fromAniList(m)]));
    for (const e of list) Object.assign(e, byId.get(e.id) || {});
  } catch {
    /* offline: keep what we have */
  }
  for (const e of list) {
    if (canEdit) {
      // chapter lookups need server.py
      const info = await fetchLatest(e);
      if (info) {
        const { found, ...rest } = info;
        Object.assign(e, rest);
      }
    }
    e.pending = false;
  }
}

async function loadAll() {
  const series = (await fetchJSON("series.json")) || [];
  const raw = (await fetchJSON("state.json")) || {};
  const state = raw.series || raw; // the first version of state.json had no wrapper
  checkedAt = raw.checkedAt || null;

  // An entry from the OLD state.json format has no history/cover: treat it as pending too,
  // so the page fills it live until the bot rewrites state.json in the new format.
  entries = series.map((s) => {
    const known = state[s.id];
    return { id: s.id, title: s.title, ...known, pending: !known || !known.history };
  });
  await fillLive(entries.filter((e) => e.pending));
}

// ---------- rendering: My list ----------

function makeCard(e, button) {
  const li = document.createElement("li");
  li.className = "card";

  const img = document.createElement("img");
  img.src = e.cover || "";
  img.alt = "";
  img.loading = "lazy";

  const info = document.createElement("div");
  info.className = "info";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = e.title;

  const sub = document.createElement("div");
  sub.className = "sub";
  if (e.chapter) {
    const a = document.createElement("a"); // official reader when MangaDex knows one
    a.href = e.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = `Ch. ${e.chapter}`;
    sub.append(a, ` · ${fmtShort(new Date(e.publishedAt))}`);
    if (e.typicalGapDays) sub.append(` · about every ${Math.round(e.typicalGapDays)} days`);
  } else if (e.pending) {
    sub.textContent = "Waiting for the first check";
  }

  const badge = document.createElement("span");
  const quiet = quietDays(e);
  if (quiet) {
    badge.className = "badge QUIET";
    badge.textContent = `On break? ${Math.floor(quiet / 7)} wks quiet`;
    badge.title = `AniList says Releasing, but the last chapter was ${quiet} days ago.`;
  } else {
    badge.className = `badge ${e.status}`;
    badge.textContent = STATUS_LABEL[e.status] || e.status || "";
  }

  info.append(title, sub, badge);
  if (e.expectedNext && !quiet && e.status === "RELEASING") {
    const next = document.createElement("div");
    next.className = "sub";
    next.textContent = `Next expected around ${fmtShort(parseDay(e.expectedNext))}`;
    info.append(next);
  }

  li.append(img, info);
  if (button) li.append(button);
  return li;
}

function renderResults() {
  const ul = document.getElementById("results");
  const saved = new Set(entries.map((e) => e.id));
  ul.replaceChildren(
    ...lastResults.map((m) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = saved.has(m.id) ? "Added" : "Add";
      btn.disabled = saved.has(m.id);
      btn.addEventListener("click", () => addSeries(m, btn));
      return makeCard(m, btn);
    })
  );
}

function renderList() {
  const msg = document.getElementById("list-msg");
  msg.textContent = entries.length
    ? ""
    : canEdit
      ? "Nothing here yet. Search a manga above and press Add."
      : "series.json is empty.";
  document.getElementById("checked-at").textContent = checkedAt
    ? `Bot last checked ${new Date(checkedAt).toLocaleString()}`
    : "";

  document.getElementById("mylist").replaceChildren(
    ...entries.map((e) => {
      let btn = null;
      if (canEdit) {
        btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ghost";
        btn.textContent = "Remove";
        btn.addEventListener("click", () => removeSeries(e.id));
      }
      return makeCard(e, btn);
    })
  );
}

// ---------- actions (only with server.py) ----------

async function addSeries(m, btn) {
  btn.disabled = true;
  btn.textContent = "Adding...";
  const res = await fetch("api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: m.id, title: m.title }),
  });
  if (!res.ok) {
    btn.textContent = "Failed";
    return;
  }
  btn.textContent = "Added";
  const e = { ...m, pending: true };
  entries.push(e);
  renderList();
  await fillLive([e]);
  renderList();
  document.getElementById("list-msg").textContent =
    "Saved to series.json. Commit and push it so the daily bot tracks it too.";
}

async function removeSeries(id) {
  const res = await fetch(`api/series?id=${id}`, { method: "DELETE" });
  if (!res.ok) return;
  entries = entries.filter((e) => e.id !== id);
  renderList();
  renderResults();
  document.getElementById("list-msg").textContent =
    "Removed from series.json. Commit and push it to stop the bot tracking it.";
}

async function refreshNow() {
  const btn = document.getElementById("refresh");
  btn.disabled = true;
  document.getElementById("list-msg").textContent = "Checking AniList and MangaDex...";
  entries.forEach((e) => (e.pending = true));
  await fillLive(entries);
  checkedAt = new Date().toISOString();
  renderList();
  document.getElementById("list-msg").textContent =
    "Updated live. The daily bot is what writes state.json.";
  btn.disabled = false;
}

// ---------- rendering: Calendar ----------

// One list of calendar events: past releases, plus projected ones from each
// series' usual gap. Quiet series are NOT projected (they are listed as overdue).
function buildEvents() {
  const today = startOfDay(new Date());
  const horizon = addDays(today, 120);
  const events = [];

  for (const e of entries) {
    for (const h of e.history || []) {
      events.push({ date: localDay(h.date), kind: "released", id: e.id, title: e.title, label: `${e.title} ch. ${h.chapter}` });
    }
    if (e.status !== "RELEASING" || !e.expectedNext || !e.typicalGapDays || quietDays(e)) continue;

    const gap = Math.max(1, Math.round(e.typicalGapDays));
    let t = parseDay(e.expectedNext);
    if (t < today) {
      // Past its expected day but not "quiet" yet: show it as due now.
      events.push({ date: today, kind: "late", id: e.id, title: e.title, label: `${e.title} (due now)` });
      t = addDays(t, Math.ceil((today - t) / (gap * DAY)) * gap);
      if (t.getTime() === today.getTime()) t = addDays(t, gap);
    }
    for (; t <= horizon; t = addDays(t, gap)) {
      events.push({ date: t, kind: "expected", id: e.id, title: e.title, label: `${e.title} (expected)` });
    }
  }
  return events;
}

function li(...parts) {
  const el = document.createElement("li");
  el.append(...parts);
  return el;
}
function span(cls, text) {
  const s = document.createElement("span");
  s.className = cls;
  s.textContent = text;
  return s;
}

function renderCalendar() {
  const events = buildEvents();
  const today = startOfDay(new Date());

  // Overdue
  const overdue = entries.filter((e) => quietDays(e));
  document.getElementById("overdue-block").hidden = overdue.length === 0;
  document.getElementById("overdue").replaceChildren(
    ...overdue.map((e) => {
      const usual = e.typicalGapDays ? `, usually every ${Math.round(e.typicalGapDays)} days` : "";
      return li(
        span("when", e.title),
        span("note", `last chapter ${e.chapter}, ${Math.floor(quietDays(e) / 7)} weeks ago${usual}`)
      );
    })
  );

  // Coming up
  const end = addDays(today, 14);
  const soon = events
    .filter((ev) => ev.kind !== "released" && ev.date >= today && ev.date <= end)
    .sort((a, b) => a.date - b.date);
  const upcoming = document.getElementById("upcoming");
  upcoming.replaceChildren(
    ...(soon.length
      ? soon.map((ev) =>
          li(span("when", fmtLong(ev.date)), ev.title, " ", span("note", ev.kind === "late" ? "due now" : "expected"))
        )
      : [li(span("note", "Nothing expected in the next 14 days."))])
  );

  // Month grid (weeks start on Monday)
  document.getElementById("month-title").textContent = calMonth.toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
  const byDay = new Map();
  for (const ev of events) {
    const k = dayKey(ev.date);
    if (!byDay.has(k)) byDay.set(k, []);
    byDay.get(k).push(ev);
  }

  const offset = (calMonth.getDay() + 6) % 7;
  const daysInMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 0).getDate();
  const cells = Math.ceil((offset + daysInMonth) / 7) * 7;
  const grid = document.getElementById("cal-grid");
  grid.replaceChildren(
    ...["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => span("dow", d))
  );

  for (let i = 0; i < cells; i++) {
    const day = addDays(calMonth, i - offset);
    const cell = document.createElement("div");
    cell.className = "cell";
    if (day.getMonth() !== calMonth.getMonth()) cell.classList.add("other");
    if (day.getTime() === today.getTime()) cell.classList.add("today");
    cell.append(span("num", String(day.getDate())));

    const list = byDay.get(dayKey(day)) || [];
    for (const ev of list.slice(0, 3)) {
      const p = span(`pill ${ev.kind}`, ev.title);
      p.title = ev.label;
      cell.append(p);
    }
    if (list.length > 3) cell.append(span("num", `+${list.length - 3} more`));
    grid.append(cell);
  }
}

// .ics file: expected releases for the next ~4 months, for Apple/Google Calendar etc.
function downloadIcs() {
  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
  const esc = (s) => s.replace(/[\\;,]/g, (c) => "\\" + c);
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");

  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Manga Tracker//EN"];
  for (const ev of buildEvents().filter((x) => x.kind === "expected")) {
    lines.push(
      "BEGIN:VEVENT",
      `UID:${ev.id}-${ymd(ev.date)}@manga-tracker`,
      `DTSTAMP:${stamp}`,
      `DTSTART;VALUE=DATE:${ymd(ev.date)}`,
      `DTEND;VALUE=DATE:${ymd(addDays(ev.date, 1))}`,
      `SUMMARY:${esc(ev.title)} (expected chapter)`,
      "END:VEVENT"
    );
  }
  lines.push("END:VCALENDAR");

  const blob = new Blob([lines.join("\r\n")], { type: "text/calendar" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "manga-calendar.ics";
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---------- wiring ----------

document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.getElementById("tab-list").hidden = btn.dataset.tab !== "list";
    document.getElementById("tab-calendar").hidden = btn.dataset.tab !== "calendar";
    if (btn.dataset.tab === "calendar") renderCalendar();
  })
);

document.getElementById("prev-month").addEventListener("click", () => {
  calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() - 1, 1);
  renderCalendar();
});
document.getElementById("next-month").addEventListener("click", () => {
  calMonth = new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 1);
  renderCalendar();
});
document.getElementById("today-month").addEventListener("click", () => {
  calMonth = firstOfMonth(new Date());
  renderCalendar();
});
document.getElementById("ics").addEventListener("click", downloadIcs);
document.getElementById("refresh").addEventListener("click", refreshNow);

let timer;
document.getElementById("search").addEventListener("input", (ev) => {
  clearTimeout(timer);
  const text = ev.target.value.trim();
  const msg = document.getElementById("search-msg");
  if (text.length < 2) {
    lastResults = [];
    renderResults();
    msg.textContent = "";
    return;
  }
  timer = setTimeout(async () => {
    msg.textContent = "Searching...";
    try {
      lastResults = await searchManga(text);
      msg.textContent = lastResults.length ? "" : "No results.";
      renderResults();
    } catch (err) {
      msg.textContent = `Search failed: ${err.message}`;
    }
  }, 400);
});

async function init() {
  // Is server.py running? (On a static host this 404s and we stay read-only.)
  try {
    const res = await fetch("api/ping");
    canEdit = res.ok && (await res.json()).edit === true;
  } catch {
    canEdit = false;
  }
  document.getElementById("add-section").hidden = !canEdit;
  document.getElementById("refresh").hidden = !canEdit;

  await loadAll();
  renderList();
}

init();
