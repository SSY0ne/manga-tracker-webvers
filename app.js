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
const FIELDS = `id title { romaji english } synonyms startDate { year } status chapters coverImage { medium }`;

const STATUS_LABEL = {
  RELEASING: "Releasing",
  HIATUS: "On break",
  FINISHED: "Finished",
  CANCELLED: "Cancelled",
  NOT_YET_RELEASED: "Not yet released",
};

let canEdit = false; // true only when server.py is running
let entries = []; // what we show (one per series)
// A visitor of the published site can keep a personal list in their own browser ("mine"), next to
// the sample library (the owner's list). The owner (server.py) always sees their own list.
const MINE_KEY = "manga-tracker:mine";
const MODE_KEY = "manga-tracker:mode";
let viewMode = "sample"; // "sample" | "mine" (visitors only)
let sampleEntries = [];
let mineEntries = [];
// Your own reading progress and the "+N behind" numbers show for the owner and in "My list".
function personal() {
  return canEdit || viewMode === "mine";
}
// The Calendar, Activity and Health tabs describe what the daily bot tracks (the owner's list), so a
// visitor looking at My list still sees those, not a list with no release dates.
function botEntries() {
  return canEdit ? entries : sampleEntries;
}
let checkedAt = null;
let health = null;
let reference = {}; // reference_counts.json: Wikipedia's chapter-list count for finished series (see reference.py)
const REFERENCE_TOLERANCE = 0.10; // a Wikipedia number more than 10% away from the one shown is a page we misread: ignored
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

// ---------- the "on break" verdict ----------
// The rule itself lives in ONE place: quiet_days() in server.py. The daily bot saves its
// verdict (quietDays) in state.json and the live refresh gets it from the same code, so this
// page only reads it. It never recomputes the rule.

// "Limited data" = no source has chapter data that makes sense (see reconcile() in
// server.py, which decides). The bot stores the verdict in state.json.
function isLimited(e) {
  return e.limited === true;
}

// MangaBaka lists a series as "hiatus": a second opinion next to our date rule.
function onHiatus(e) {
  return e.hiatus === true && e.status === "RELEASING" && !isLimited(e);
}

// How the sources compare, as a short line plus a tooltip with each source's answer.
function sourcesLine(e) {
  const list = e.sources || [];
  if (list.length === 0 || e.pending) return null;
  const names = list.filter((s) => s.plausible).map((s) => s.name);
  const used = e.source;
  const text =
    e.agreement === "counts"
      ? list.every((s) => s.plausible)
        ? `Counts agree (${names.join(" + ")})`
        : `Counts differ: ${names.join(" + ")} outvote ${list.filter((s) => !s.plausible).map((s) => s.name).join(", ")}`
      : e.agreement === "agree"
      ? `Sources agree (${names.join(" + ")})`
      : e.agreement === "conflict"
        ? `Sources disagree, using ${used} (fresher)`
        : e.agreement === "single"
          ? `1 source: ${used}`
          : e.agreement === "count"
            ? `Count only (${used}): no release feed fits` +
              (list.some((s) => s.kind === "count" && !s.plausible)
                ? `, ${list.filter((s) => s.kind === "count" && !s.plausible).map((s) => s.name).join(" and ")} outvoted`
                : "")
            : "No reliable source";
  const detail = list
    .map((s) => {
      const when = s.publishedAt ? fmtShort(new Date(s.publishedAt)) : "count";
      return `${s.name}: ch. ${s.chapter ?? "?"} (${when})${s.plausible ? "" : " ✗ doesn't fit"}`;
    })
    .join(" · ");
  return { text, detail, kind: e.agreement };
}

// Do the sources that FIT disagree about the chapter? (Sources we already threw out as stale or in another
// numbering don't count: they are listed in the detail panel.) Whole chapters only: 223.9 and 223 agree.
// The tile gets a note when they are at least 2 apart; a difference of 1 is normal (extras are counted
// differently) and only appears in the detail panel.
// Wikipedia's chapter list as a REFERENCE for a finished series. It never changes the answer: it is one more
// opinion in the "sources differ" note (it read Bleach as 686 where AniList, Kitsu and MangaBaka all say 706).
// Only used when it is within REFERENCE_TOLERANCE of the chapter shown: further away means its page was misread.
function wikipediaRef(e) {
  const r = reference[String(e.id)];
  const shown = parseFloat(e.chapter);
  if (!r || !r.count || e.status !== "FINISHED" || !Number.isFinite(shown)) return null;
  if (Math.abs(r.count - shown) > REFERENCE_TOLERANCE * shown) return null;
  const url = (r.urls || [])[0] || "";
  return { count: r.count, url: url.startsWith("https://en.wikipedia.org/") ? url : null };
}

function chapterSpread(e) {
  const nums = (e.sources || [])
    .filter((s) => s.plausible && s.chapter != null && s.kind !== "status")
    .map((s) => Math.floor(parseFloat(s.chapter)))
    .filter((n) => Number.isFinite(n));
  const wiki = wikipediaRef(e);
  if (wiki) nums.push(Math.floor(wiki.count));
  return nums.length > 1 ? { min: Math.min(...nums), max: Math.max(...nums) } : null;
}
const CONFLICT_AT = 2;
function hasConflict(e) {
  const sp = chapterSpread(e);
  return Boolean(sp) && sp.max - sp.min >= CONFLICT_AT;
}
function conflictText(e) {
  const sp = chapterSpread(e);
  return sp ? `Sources differ: ${sp.min} to ${sp.max}` : "";
}

function fmtQuiet(days) {
  const weeks = Math.floor(days / 7);
  return weeks >= 104 ? `over ${Math.floor(weeks / 52)} years` : `${weeks} wks`;
}

// Days without a chapter if the series looks on break, else 0.
function quietDays(e) {
  if (isLimited(e)) return 0;
  if (e.quietDays !== undefined) return e.quietDays || 0;
  // A state.json saved before the verdict existed only has the bot's yes/no (`quiet`).
  return e.quiet && e.publishedAt ? Math.floor((Date.now() - new Date(e.publishedAt).getTime()) / DAY) : 0;
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
    alChapters: m.chapters || null, // AniList only knows the count once a series has finished
    // Used to match the series on the other chapter sources (title variants + start year)
    names: [m.title.english, m.title.romaji, ...(m.synonyms || [])].filter(Boolean),
    year: m.startDate?.year || null,
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

// Chapter info through server.py, which asks every source (MangaDex, MangaUpdates...)
// and reconciles them. Browsers can't call those APIs directly.
async function fetchLatest(e) {
  const params = new URLSearchParams({ al: e.id });
  (e.names || [e.title]).slice(0, 4).forEach((n) => params.append("title", n));
  if (e.year) params.set("year", e.year);
  if (e.read) params.set("read", e.read);
  if (e.status) params.set("status", e.status);
  if (e.gapDays) params.set("gap", e.gapDays); // a rhythm you set by hand
  if (e.decimals) params.set("decimals", "1"); // this series really numbers chapters like 12.5
  return await fetchJSON(`api/latest?${params}`);
}

// Put a fresh answer from server.py on an entry. It replaces everything chapter-related, so an
// old wrong value can't linger.
function applyLatest(e, info) {
  if (!info) return;
  const { found, ...rest } = info;
  Object.assign(
    e,
    { chapter: null, publishedAt: null, url: null, typicalGapDays: null, expectedNext: null, history: [], limited: false, quietDays: 0, hiatus: false, countOnly: false, gapOverride: false },
    rest
  );
}

// Covers, titles and status from AniList, 50 at a time.
async function fillBasics(list) {
  for (let i = 0; i < list.length; i += 50) {
    const chunk = list.slice(i, i + 50);
    try {
      const data = await anilist(
        `query ($ids: [Int]) { Page(perPage: 50) {
           media(id_in: $ids, type: MANGA) { ${FIELDS} } } }`,
        { ids: chunk.map((e) => e.id) }
      );
      const byId = new Map(data.Page.media.map((m) => [m.id, fromAniList(m)]));
      for (const e of chunk) Object.assign(e, byId.get(e.id) || {});
    } catch {
      /* offline: keep what we have */
    }
  }
}

// Fill entries with live data (AniList for covers/status, server.py for chapters).
async function fillLive(list, onProgress) {
  if (list.length === 0) return;
  await fillBasics(list);
  let done = 0;
  for (const e of list) {
    if (canEdit) {
      // chapter lookups need server.py
      applyLatest(e, await fetchLatest(e));
    }
    e.pending = false;
    if (onProgress) onProgress(++done, list.length);
  }
}

// What went wrong while loading, if anything: shown as a banner instead of a silent empty page.
let loadProblem = null;

async function loadAll(onProgress) {
  loadProblem = null;
  const seriesFile = await fetchJSON("series.json");
  const stateFile = await fetchJSON("state.json");
  reference = (await fetchJSON("reference_counts.json")) || {};
  if (seriesFile === null) {
    loadProblem = {
      level: "bad",
      text: "Couldn't load the list (series.json). If you opened index.html straight from a folder, start python3 server.py and open http://127.0.0.1:8123 instead.",
    };
  } else if (stateFile === null && seriesFile.length) {
    loadProblem = { level: "warn", text: "No bot data yet (state.json is missing): chapters appear after the daily bot has run." };
  }
  const series = seriesFile || [];
  const raw = stateFile || {};
  const state = raw.series || raw; // the first version of state.json had no wrapper
  checkedAt = raw.checkedAt || null;
  health = raw.health || null; // how the last bot run went (see the Health tab)

  // An entry from the OLD state.json format has no history/cover: treat it as pending too,
  // so the page fills it live until the bot rewrites state.json in the new format.
  entries = series.map((s) => {
    const known = state[s.id];
    // mal / read (chapters you've read) come from series.json, not from the bot's state
    return {
      id: s.id,
      title: s.title,
      ...known,
      mal: s.mal,
      read: s.read,
      alerts: s.alerts, // per-series settings from series.json (see the panel)
      gapDays: s.gapDays,
      decimals: s.decimals,
      pending: !known || !known.history,
    };
  });
  await fillLive(entries.filter((e) => e.pending), onProgress);
}

// ---------- rendering: My list ----------

// ---------- reading progress: "I'm up to date" / "I'm actually at chapter N" ----------
// Reading progress is the OWNER's: it is only shown and editable when server.py is running, and
// saved to series.json (the bot reads it too). A visitor to the published site never sees it.

async function setRead(e, n) {
  n = Math.min(100000, Math.max(0, Math.floor(n)));
  if (!canEdit) {
    if (viewMode !== "mine") return false;
    e.read = n; // a visitor's list lives in their browser
    saveMine();
    return true;
  }
  const res = await fetch("api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: e.id, title: e.title, read: n }),
  });
  if (!res.ok) return false;
  e.read = n;
  return true;
}

function progressRow(e) {
  const wrap = document.createElement("div");
  const row = document.createElement("div");
  row.className = "progress";

  const behind = behindBy(e);
  const text = document.createElement("span");
  text.textContent = !e.read
    ? "Reading progress not set"
    : behind > 0
      ? `You've read ${e.read} · ${behind} behind`
      : e.chapter
        ? `You've read ${e.read} · caught up`
        : `You've read ${e.read}`;
  const edit = document.createElement("button");
  edit.type = "button";
  edit.className = "link-btn";
  edit.textContent = e.read ? "update" : "set";
  row.append(text, edit);

  const form = document.createElement("div");
  form.className = "progress-form";
  form.hidden = true;
  const input = document.createElement("input");
  input.type = "number";
  input.min = "0";
  input.step = "1";
  input.value = e.read || "";
  input.placeholder = "chapter";
  input.setAttribute("aria-label", "Chapter you have read up to");
  const mk = (label, ghost) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    if (ghost) b.className = "ghost";
    return b;
  };
  const save = mk("Save");
  const upToDate = mk("I'm up to date", true);
  upToDate.hidden = !e.chapter; // needs a known latest chapter to mean anything
  const cancel = mk("Cancel", true);
  form.append(input, save, upToDate, cancel);

  async function commit(n) {
    [save, upToDate, cancel].forEach((b) => (b.disabled = true));
    const before = e.read; // kept so "Undo" can put it back
    const ok = await setRead(e, n);
    renderList(); // the grid, chips and "behind" counts all follow the new number
    if (detailEntry === e) renderDetail(e); // refresh the open panel
    const note = document.getElementById("detail-msg");
    if (!note) return;
    note.textContent = !ok
      ? "Could not save your progress."
      : canEdit
        ? "Saved to series.json. Commit and push it so the daily bot knows your new progress."
        : "Saved in this browser only.";
    if (ok) {
      const undo = el("button", "link-btn", "Undo");
      undo.type = "button";
      undo.addEventListener("click", async () => {
        await setRead(e, before || 0);
        renderList();
        if (detailEntry === e) renderDetail(e);
        const again = document.getElementById("detail-msg");
        if (again) again.textContent = "Back to what it was.";
      });
      note.append(" ", undo);
    }
  }
  edit.addEventListener("click", () => {
    form.hidden = !form.hidden;
    if (!form.hidden) input.focus();
  });
  cancel.addEventListener("click", () => (form.hidden = true));
  save.addEventListener("click", () => {
    const n = parseInt(input.value, 10);
    if (Number.isNaN(n) || n < 0) return input.focus();
    commit(n);
  });
  upToDate.addEventListener("click", () => commit(Math.max(Math.floor(parseFloat(e.chapter)), e.read || 0)));
  input.addEventListener("keydown", (ev) => ev.key === "Enter" && save.click());

  wrap.append(row, form);
  return wrap;
}

// ---------- cover tiles ----------
// The list is a grid of covers. A tile shows only what is worth a glance: one label
// (NEW / ON BREAK / FINISHED / LIMITED), how many chapters you're behind, and a thin
// progress bar. Everything else lives in the detail panel that opens on click.

// AniList serves the same cover in several sizes at the same address: we store the small
// one, the grid uses "medium" and the detail panel "large".
function coverUrl(e, size = "medium") {
  return e.cover ? e.cover.replace(/\/cover\/(small|medium|large)\//, `/cover/${size}/`) : "";
}

function tileChip(e) {
  if (isLimited(e)) return { cls: "limited", text: "Limited" };
  if (isDormant(e)) return { cls: "dormant", text: "Dormant" };
  if (isOnBreak(e)) return { cls: "break", text: "On break" };
  if (isNew(e)) return { cls: "new", text: "New" };
  if (e.status === "RELEASING") return { cls: "releasing", text: "Releasing" };
  if (e.status === "FINISHED") return { cls: "finished", text: "Finished" };
  return null;
}

function tileSubtitle(e) {
  if (e.chapter) {
    // For a series on break the date is old news: say WHY it is flagged instead.
    const q = isDormant(e) ? e.dormantDays : quietDays(e);
    const why = q ? `${fmtQuiet(q)} quiet` : isDormant(e) ? "listed as discontinued" : onHiatus(e) ? "listed as hiatus" : hasConflict(e) ? "sources differ" : null;
    if (why) return `Ch. ${e.chapter} · ${why}`;
    return `Ch. ${e.chapter}` + (e.publishedAt ? ` · ${fmtShort(new Date(e.publishedAt))}` : "");
  }
  return e.pending ? "Checking..." : e.lookupFailed && !e.chapter ? "Count unavailable, retrying later" : STATUS_LABEL[e.status] || "";
}

// opts.button = a search result with an Add button (not clickable);
// opts.compact = the small "new this week" strip.
function makeTile(e, opts = {}) {
  const li = document.createElement("li");
  li.className = "tile" + (opts.button ? " static" : "");

  const cover = document.createElement("span");
  cover.className = "cover";
  const img = document.createElement("img");
  img.src = coverUrl(e);
  img.alt = "";
  img.loading = "lazy";
  img.decoding = "async";
  cover.append(img);

  if (!opts.button && !opts.compact) {
    const chip = tileChip(e);
    if (chip) {
      const c = document.createElement("span");
      c.className = `chip ${chip.cls}`;
      c.textContent = chip.text;
      cover.append(c);
    }
    if (hasConflict(e)) {
      const x = document.createElement("span");
      x.className = "conflict-pill";
      x.textContent = "≠";
      x.title = conflictText(e);
      cover.append(x);
    }
    const behind = behindShown(e);
    if (behind > 0) {
      const p = document.createElement("span");
      p.className = "behind-pill";
      p.textContent = `+${behind}`;
      p.title = `${behind} chapters behind`;
      cover.append(p);
    }
    if (personal() && e.read && e.chapter) {
      const pct = Math.max(0, Math.min(100, Math.round((100 * e.read) / parseFloat(e.chapter))));
      const bar = document.createElement("span");
      bar.className = "bar";
      bar.innerHTML = "<i></i>"; // static markup, no user text
      bar.firstChild.style.width = `${pct}%`;
      cover.append(bar);
    }
  }
  if (opts.button) {
    opts.button.classList.add("add-btn");
    cover.append(opts.button);
  }

  const meta = document.createElement("span");
  meta.className = "meta";
  const t = document.createElement("span");
  t.className = "t";
  t.textContent = e.title;
  const s = document.createElement("span");
  s.className = "s";
  s.textContent = tileSubtitle(e);
  meta.append(t, s);
  if (opts.button) {
    li.append(cover, meta); // a search result: the Add button is inside the cover
  } else {
    // A real <button> (not a clickable list item): keyboard and screen readers get it for free.
    const open = document.createElement("button");
    open.type = "button";
    open.className = "tile-open";
    open.setAttribute("aria-haspopup", "dialog");
    open.append(cover, meta);
    open.addEventListener("click", () => openDetail(e));
    li.append(open);
  }
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
      return makeTile(m, { button: btn });
    })
  );
}

// ---------- detail panel (opens when you click a cover) ----------

let detailEntry = null;

function openDetail(e) {
  detailEntry = e;
  renderDetail(e);
  const dlg = document.getElementById("detail");
  if (!dlg.open) dlg.showModal();
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function fact(label, value) {
  const row = el("div", "fact");
  row.append(el("span", "", label));
  if (value instanceof Node) row.append(value);
  else row.append(el("span", "", value));
  return row;
}

// Save per-series settings to series.json, then ask server.py again so a new rhythm
// changes the next-chapter date and the "on break" verdict straight away.
async function saveSettings(e, alerts, gapDays, decimals) {
  const res = await fetch("api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: e.id, title: e.title, alerts: alerts === "all" ? null : alerts, gapDays: gapDays || null, decimals: decimals || null }),
  });
  if (!res.ok) return false;
  e.alerts = alerts === "all" ? undefined : alerts;
  e.gapDays = gapDays || undefined;
  e.decimals = decimals || undefined;
  applyLatest(e, await fetchLatest(e));
  return true;
}

function settingsSection(e) {
  const sec = el("div", "d-section");
  sec.append(el("h3", "", "Alerts and rhythm"));

  const alertsRow = el("div", "setting-row");
  const sel = el("select");
  sel.setAttribute("aria-label", "Which alerts to send for this series");
  [
    ["all", "All alerts"],
    ["breaks", "Only breaks and comebacks"],
    ["off", "Muted (still tracked)"],
  ].forEach(([value, label]) => {
    const o = el("option", "", label);
    o.value = value;
    sel.append(o);
  });
  sel.value = e.alerts || "all";
  alertsRow.append(el("label", "", "Discord / email"), sel);

  const gapRow = el("div", "setting-row");
  const gap = el("input");
  gap.type = "number";
  gap.min = "1";
  gap.max = "730";
  gap.step = "1";
  gap.value = e.gapDays || "";
  gap.placeholder = e.typicalGapDays && !e.gapDays ? `auto: ${Math.round(e.typicalGapDays)}` : "auto";
  gap.setAttribute("aria-label", "Days between chapters, if you know it");
  gapRow.append(el("label", "", "New chapter every"), gap, el("span", "note", "days"));
  const hint = el("p", "msg", "Leave it empty to let the tool measure it. Set it for irregular series, so a long gap isn't called a break.");

  const decRow = el("div", "setting-row");
  const dec = el("input");
  dec.type = "checkbox";
  dec.checked = Boolean(e.decimals);
  dec.id = "setting-decimals";
  const decLabel = el("label", "", "Count bonus chapters like 12.5");
  decLabel.htmlFor = "setting-decimals";
  decRow.append(dec, decLabel);
  const decHint = el("p", "msg", "Off by default: a bonus page or split part isn't a new chapter. Turn it on only for a series that really numbers its story chapters this way.");

  const save = el("button", "", "Save");
  save.type = "button";
  const msg = el("p", "msg");
  msg.id = "settings-msg";
  save.addEventListener("click", async () => {
    const days = gap.value === "" ? null : parseInt(gap.value, 10);
    if (days !== null && (Number.isNaN(days) || days < 1 || days > 730)) {
      msg.textContent = "Days must be a number from 1 to 730.";
      return gap.focus();
    }
    save.disabled = true;
    save.textContent = "Saving...";
    msg.textContent = "Saving, then re-checking this series on every source (a few seconds)...";
    const ok = await saveSettings(e, sel.value, days, dec.checked);
    renderList();
    if (detailEntry === e) renderDetail(e);
    const note = document.getElementById("settings-msg");
    if (note) note.textContent = ok ? "Saved to series.json. Commit and push it so the daily bot uses it." : "Could not save.";
  });
  sec.append(alertsRow, gapRow, hint, decRow, decHint, save, msg);
  return sec;
}

function renderDetail(e) {
  const body = document.getElementById("detail-body");
  const quiet = quietDays(e);

  // Hero: blurred cover as the backdrop, sharp cover + title + labels on top
  const hero = el("div", "d-hero");
  hero.style.setProperty("--cover", `url("${coverUrl(e, "large").replace(/"/g, "%22")}")`);
  const img = el("img", "d-cover");
  img.src = coverUrl(e, "large");
  img.alt = `Cover of ${e.title}`;
  const head = el("div", "d-head");
  const h = el("h2", "d-title", e.title);
  h.id = "detail-title";
  const chips = el("div", "chips");
  const lead = tileChip(e);
  if (lead && lead.cls !== "finished" && lead.cls !== "releasing") chips.append(el("span", `chip ${lead.cls}`, lead.text));
  if (e.status) chips.append(el("span", "chip status", STATUS_LABEL[e.status] || e.status));
  head.append(h, chips);
  const close = el("button", "close-btn", "✕");
  close.type = "button";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", () => document.getElementById("detail").close());
  hero.append(img, head, close);

  const main = el("div", "d-body");

  // The facts
  const facts = el("div", "d-section");
  facts.append(el("h3", "", "Chapters"));
  if (e.chapter) {
    const a = el("a", "", `Ch. ${e.chapter}`);
    a.href = e.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    const latest = el("span");
    latest.append(a, e.publishedAt ? ` · ${fmtShort(new Date(e.publishedAt))}` : " · no date");
    facts.append(fact("Latest", latest));
  } else {
    facts.append(fact("Latest", e.pending ? "Checking..." : "No reliable chapter data"));
  }
  const spread = chapterSpread(e);
  if (spread && spread.max > spread.min) {
    facts.append(fact("Sources differ", `${spread.min} to ${spread.max}: shown is ${e.chapter || "?"}${e.source ? ` (${e.source})` : ""}`));
  }
  const wiki = wikipediaRef(e);
  if (wiki && Math.floor(wiki.count) !== Math.floor(parseFloat(e.chapter))) {
    const note = el("span");
    if (wiki.url) {
      const a = el("a", "", `${wiki.count} chapters`);
      a.href = wiki.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      note.append(a);
    } else note.append(`${wiki.count} chapters`);
    note.append(" · its chapter list's highest numbered chapter, a reference: not what is shown");
    facts.append(fact("Wikipedia says", note));
  }
  if (e.typicalGapDays) facts.append(fact("Usually every", `${Math.round(e.typicalGapDays)} days`));
  if (e.expectedNext && e.status === "RELEASING" && !isOnBreak(e) && !isLimited(e)) {
    facts.append(fact("Next expected", `around ${fmtShort(parseDay(e.expectedNext))}`));
  }
  if (isDormant(e)) {
    facts.append(
      fact("Dormant", e.dormantDays ? `${fmtQuiet(e.dormantDays)} without a chapter: the author may never return` : "listed as discontinued: the author may never return")
    );
  }
  else if (quiet) facts.append(fact("On break?", `${fmtQuiet(quiet)} without a chapter`));
  else if (onHiatus(e)) facts.append(fact("On break?", "listed as hiatus"));
  for (const v of e.statusVotes || []) {
    if (v.status === "hiatus" || v.status === "discontinued") {
      facts.append(fact(`${v.name} says`, `${v.status === "hiatus" ? "paused" : "discontinued"}${v.ended ? ` (last chapter ${v.ended})` : ""}`));
    }
  }
  main.append(facts);

  // Your progress and this series' alert settings belong to the OWNER: a visitor to the published
  // site never sees them (they appear only while server.py is running).
  if (personal()) {
    const prog = el("div", "d-section");
    prog.append(el("h3", "", "Your progress"), progressRow(e), el("p", "msg"));
    prog.lastChild.id = "detail-msg";
    main.append(prog);
    if (canEdit) main.append(settingsSection(e));
    else {
      main.append(
        el("p", "msg", "Saved in this browser only. Chapter counts come from MangaBaka (plus AniList and Kitsu once a series has finished). Break alerts and release dates need the daily bot, which tracks the sample library.")
      );
    }
  }

  // What each source said
  const list = e.sources || [];
  if ((list.length && !e.pending) || wiki) {
    const src = list.length && !e.pending ? sourcesLine(e) : null;
    const sec = el("div", "d-section");
    sec.append(el("h3", "", "Sources"));
    if (src) sec.append(el("div", `src-line ${src.kind === "agree" ? "src-agree" : src.kind === "conflict" ? "src-conflict" : ""}`, src.text));
    (e.pending ? [] : list).forEach((s) => {
      const row = el("div", "src-row" + (s.plausible ? "" : " bad"));
      const dot = el("span", `dot ${s.plausible ? (s.name === e.source ? "ok" : "") : "bad"}`);
      const kind = s.kind === "count" ? "count" : "dated";
      row.append(
        dot,
        el("span", "nm", s.name),
        el("span", "ch", `ch. ${s.chapter ?? "?"} · ${s.publishedAt ? fmtShort(new Date(s.publishedAt)) : kind}`),
        el("span", "note", s.plausible ? (s.name === e.source ? "used" : "also fits") : "doesn't fit")
      );
      sec.append(row);
    });
    if (wiki) {
      const row = el("div", "src-row");
      const name = wiki.url ? el("a", "nm", "Wikipedia") : el("span", "nm", "Wikipedia");
      if (wiki.url) {
        name.href = wiki.url;
        name.target = "_blank";
        name.rel = "noopener noreferrer";
      }
      row.append(el("span", "dot"), name, el("span", "ch", `ch. ${wiki.count} · chapter list`), el("span", "note", "reference only"));
      sec.append(row);
    }
    main.append(sec);
  }

  // Actions + links
  const actions = el("div", "d-actions");
  if (personal()) {
    const rm = el("button", "btn danger", "Remove from my list");
    rm.type = "button";
    rm.addEventListener("click", () => {
      // Removing is hard to undo by hand, so ask first. The toast afterwards offers an Undo.
      const ask = el("div", "confirm", `Remove ${e.title} from your list?`);
      const yes = el("button", "btn danger", "Yes, remove");
      yes.type = "button";
      const no = el("button", "btn ghost", "Cancel");
      no.type = "button";
      no.addEventListener("click", () => actions.replaceChildren(rm));
      yes.addEventListener("click", async () => {
        const removed = canEdit ? await removeSeries(e.id) : removeMine(e.id);
        document.getElementById("detail").close();
        if (removed) toast(`Removed ${removed.title}`, () => (canEdit ? restoreSeries(removed) : restoreMine(removed)));
      });
      actions.replaceChildren(ask, yes, no);
      no.focus(); // the safe choice is the one under your thumb
    });
    actions.append(rm);
  }
  const links = el("div", "d-links");
  const link = (href, label) => {
    const a = el("a", "", label);
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    links.append(a);
  };
  link(`https://anilist.co/manga/${e.id}`, "AniList");
  if (e.mal) link(`https://myanimelist.net/manga/${e.mal}`, "MyAnimeList");
  if (actions.children.length) main.append(actions);
  main.append(links);

  body.replaceChildren(hero, main);
}

// ---------- filter + sort (all in the browser) ----------

const VIEW_KEY = "manga-tracker:view";

function behindBy(e) {
  if (!e.read || !e.chapter) return 0;
  const b = Math.floor(parseFloat(e.chapter) - e.read);
  return b > 0 ? b : 0;
}
// "+N behind" on a cover, the chip, the filter and the sort only mean something for a series
// that is still running: one that is on break, finished or has no usable data has nothing new
// to catch up on. (The detail panel still shows your exact numbers for every series.)
function isRunning(e) {
  return e.status === "RELEASING" && !isOnBreak(e) && !isDormant(e) && !isLimited(e);
}
function behindShown(e) {
  return personal() && isRunning(e) ? behindBy(e) : 0;
}
// Dormant = years without a chapter (the rule lives in server.dormant_days; the page only reads it).
// It is not "on break": a break is a pause we expect to end.
function isDormant(e) {
  return e.dormant === true && !isLimited(e);
}
function isOnBreak(e) {
  return !isDormant(e) && (quietDays(e) > 0 || onHiatus(e));
}
function isNew(e) {
  return Boolean(e.publishedAt) && Date.now() - new Date(e.publishedAt).getTime() < 7 * DAY;
}
function nextTime(e) {
  // Only series that are really running have a "next chapter" to sort by.
  if (!e.expectedNext || e.status !== "RELEASING" || isOnBreak(e) || isLimited(e)) return Infinity;
  return parseDay(e.expectedNext).getTime();
}
function newestTime(e) {
  return e.publishedAt ? new Date(e.publishedAt).getTime() : -Infinity;
}

const SHOW = {
  all: () => true,
  releasing: (e) => e.status === "RELEASING",
  onbreak: isOnBreak,
  dormant: isDormant,
  new: isNew,
  behind: (e) => behindShown(e) > 0,
  disagree: hasConflict,
  count: (e) => e.countOnly === true,
  finished: (e) => e.status === "FINISHED",
};
// "[Oshi no Ko]" should sort under O, so leading symbols are ignored.
const sortKey = (e) => e.title.replace(/^[^a-z0-9]+/i, "");
const byTitle = (a, b) => sortKey(a).localeCompare(sortKey(b));
const SORT = {
  title: byTitle,
  behind: (a, b) => behindShown(b) - behindShown(a) || byTitle(a, b),
  newest: (a, b) => newestTime(b) - newestTime(a) || byTitle(a, b),
  next: (a, b) => nextTime(a) - nextTime(b) || byTitle(a, b),
  onbreak: (a, b) => Number(isOnBreak(b)) - Number(isOnBreak(a)) || byTitle(a, b),
};

function visibleEntries() {
  const text = document.getElementById("filter-text").value.trim().toLowerCase();
  const show = SHOW[document.getElementById("filter-show").value] || SHOW.all;
  const sort = SORT[document.getElementById("sort-by").value] || SORT.title;
  return entries.filter((e) => show(e) && (!text || e.title.toLowerCase().includes(text))).sort(sort);
}

function saveView() {
  try {
    localStorage.setItem(
      VIEW_KEY,
      JSON.stringify({
        text: document.getElementById("filter-text").value,
        show: document.getElementById("filter-show").value,
        sort: document.getElementById("sort-by").value,
      })
    );
  } catch {
    /* private mode: the page works without remembering */
  }
}

function restoreView() {
  try {
    const v = JSON.parse(localStorage.getItem(VIEW_KEY) || "{}");
    if (v.text) document.getElementById("filter-text").value = v.text;
    const setIfOption = (id, value) => {
      const select = document.getElementById(id);
      if ([...select.options].some((o) => o.value === value)) select.value = value;
    };
    if (SHOW[v.show]) setIfOption("filter-show", v.show);
    if (SORT[v.sort]) setIfOption("sort-by", v.sort);
  } catch {
    /* ignore */
  }
}

// The quick-filter chips under the header: a count you can click to filter the grid.
function renderStats() {
  if (loadProblem?.level === "bad" && viewMode === "sample") {
    document.getElementById("stats").replaceChildren(); // "0 tracked" would be a lie when loading failed
    return;
  }
  const defs = [
    { key: "all", label: "tracked", n: entries.length, cls: "" },
    { key: "new", label: "new this week", n: entries.filter(isNew).length, cls: "fresh" },
    { key: "onbreak", label: "on break", n: entries.filter(isOnBreak).length, cls: "warm" },
  ];
  const dormantCount = entries.filter(isDormant).length;
  if (dormantCount) defs.splice(3, 0, { key: "dormant", label: "dormant", n: dormantCount, cls: "" });
  if (personal()) defs.push({ key: "behind", label: "to catch up", n: entries.filter((e) => behindShown(e) > 0).length, cls: "" });
  const current = document.getElementById("filter-show").value;
  document.getElementById("stats").replaceChildren(
    ...defs.map((d) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = `stat ${d.cls}${current === d.key ? " active" : ""}`;
      b.setAttribute("aria-pressed", String(current === d.key));
      b.append(el("b", "", String(d.n)), el("span", "", d.label));
      b.addEventListener("click", () => {
        // Clicking the active chip again goes back to showing everything.
        document.getElementById("filter-show").value = current === d.key ? "all" : d.key;
        saveView();
        renderList();
      });
      return b;
    })
  );
}

function renderList() {
  const msg = document.getElementById("list-msg");
  const problem = document.getElementById("load-error");
  problem.hidden = !loadProblem || viewMode === "mine";
  if (loadProblem) {
    problem.className = `banner ${loadProblem.level}`;
    problem.textContent = loadProblem.text;
  }
  document.getElementById("mylist").removeAttribute("aria-busy");
  msg.replaceChildren();
  if (!entries.length && !(loadProblem?.level === "bad" && viewMode === "sample")) {
    if (viewMode === "mine") {
      msg.append("Your list is empty. Find manga to add in ");
      const go = el("button", "link-btn", "Discover");
      go.type = "button";
      go.addEventListener("click", () => selectTab("discover", true));
      msg.append(go, ".");
    } else {
      msg.textContent = canEdit
        ? "Nothing here yet. Search a manga above, or open Discover, and press Add."
        : "This library is empty.";
    }
  }
  if (viewMode === "mine" && fillLeft > 0 && entries.length) msg.textContent = `Looking up chapter counts... ${fillLeft} left`;
  else if (viewMode === "mine") {
    const failed = entries.filter((e) => e.lookupFailed);
    if (failed.length) {
      msg.replaceChildren(`${failed.length} chapter count${failed.length > 1 ? "s" : ""} couldn't load (the source is busy). `);
      const retry = el("button", "link-btn", "Try again");
      retry.type = "button";
      retry.addEventListener("click", () => {
        blockedUntil = 0;
        failed.forEach((e) => ((e.lookupFailed = false), (e.mbAt = null)));
        fillMine(failed, { basics: false });
        renderList();
      });
      msg.append(retry);
    }
  }
  document.getElementById("library-title").textContent = canEdit
    ? "Your library"
    : viewMode === "mine"
      ? "My list"
      : "Sample library";
  document.getElementById("checked-at").textContent = checkedAt
    ? `Bot checked ${new Date(checkedAt).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`
    : "";

  renderStats();

  // "New this week": the freshest chapters as a row of covers
  const fresh = entries.filter(isNew).sort((a, b) => newestTime(b) - newestTime(a)).slice(0, 14);
  document.getElementById("fresh-block").hidden = fresh.length === 0;
  document.getElementById("fresh").replaceChildren(...fresh.map((e) => makeTile(e, { compact: true })));

  const visible = visibleEntries();
  document.getElementById("list-count").textContent =
    entries.length && visible.length !== entries.length
      ? `Showing ${visible.length} of ${entries.length}` + (visible.length ? "" : ": nothing matches this filter")
      : "";
  document.getElementById("mylist").replaceChildren(...visible.map((e) => makeTile(e)));
}

// ---------- a visitor's own list ("My list", kept in this browser) ----------
// A static site has no server, so a visitor's list is saved with localStorage. Chapter data comes
// from AniList and MangaBaka, the two sources a browser may call directly (MangaDex and
// MangaUpdates block browsers). MangaBaka only has a chapter COUNT and a hiatus flag, so My list
// has no release dates and no rhythm-based break detection: that needs the bot.

const MANGABAKA = "https://api.mangabaka.org/v1";
const KITSU = "https://kitsu.io/api/edge";
const MB_FRESH_MS = 12 * 60 * 60 * 1000; // ask the sources again after 12 hours
const MAX_MINE = 500; // a bigger list would take hours to look up (MangaBaka allows only a few calls in a row)
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const aniListCover = (v) => (typeof v === "string" && /^https:\/\/s\d+\.anilist\.co\/[\w\-./]+$/.test(v) ? v : null);

// Whatever is in localStorage is untrusted (old versions, hand edits, other pages on this address):
// keep only the fields we know, with the right types and sane sizes.
function cleanMine(x) {
  if (!x || !Number.isInteger(x.id) || x.id <= 0 || typeof x.title !== "string" || !x.title.trim()) return null;
  const num = (v, max) => (isNum(v) && v >= 0 && v <= max ? Math.floor(v) : null);
  const e = {
    id: x.id,
    title: x.title.trim().slice(0, 200),
    read: num(x.read, 100000) || 0,
    names: Array.isArray(x.names) ? x.names.filter((n) => typeof n === "string").slice(0, 6).map((n) => n.slice(0, 200)) : undefined,
    year: num(x.year, 3000) || null,
    cover: aniListCover(x.cover),
    status: Object.prototype.hasOwnProperty.call(STATUS_LABEL, x.status) ? x.status : undefined,
    alChapters: num(x.alChapters, 100000) || null,
    chapter: typeof x.chapter === "string" && /^\d{1,6}(\.\d{1,2})?$/.test(x.chapter) ? x.chapter : null,
    hiatus: x.hiatus === true,
    countOnly: x.countOnly === true,
    agreement: ["single", "counts"].includes(x.agreement) ? x.agreement : undefined,
    source: typeof x.source === "string" ? x.source.slice(0, 30) : undefined,
    url: typeof x.url === "string" && /^https:\/\//.test(x.url) ? x.url.slice(0, 300) : `https://anilist.co/manga/${x.id}`,
    mbAt: isNum(x.mbAt) ? x.mbAt : null,
  };
  if (Array.isArray(x.sources)) {
    e.sources = x.sources
      .filter((s) => s && typeof s.name === "string")
      .slice(0, 6)
      .map((s) => ({ name: s.name.slice(0, 30), kind: "count", chapter: typeof s.chapter === "string" ? s.chapter.slice(0, 10) : null, publishedAt: null, plausible: s.plausible === true }));
  }
  return e;
}

function loadMine() {
  try {
    const v = JSON.parse(localStorage.getItem(MINE_KEY) || "[]");
    if (!Array.isArray(v)) return [];
    const seen = new Set();
    return v
      .map(cleanMine)
      .filter((e) => e && !seen.has(e.id) && seen.add(e.id))
      .slice(0, MAX_MINE);
  } catch {
    return [];
  }
}

function saveMine() {
  try {
    const keep = ["id", "title", "read", "names", "year", "cover", "status", "alChapters", "chapter", "hiatus", "countOnly", "agreement", "source", "sources", "url", "mbAt"];
    localStorage.setItem(MINE_KEY, JSON.stringify(mineEntries.map((e) => Object.fromEntries(keep.map((k) => [k, e[k]])))));
  } catch {
    /* private mode or full storage: the list just won't survive a reload */
  }
}

// fetch that waits and retries when a source answers 429 "too many requests" (MangaBaka does
// after a handful of calls in a row, and says how long to wait in Retry-After: 10).
async function politeFetch(url, tries = 3) {
  for (let i = 1; ; i++) {
    const res = await fetch(url);
    if (res.status !== 429 || i >= tries) return res;
    await sleep(Math.min(15, parseFloat(res.headers.get("Retry-After")) || 10) * 1000);
  }
}

// The MangaBaka record of a series, matched exactly by its AniList id (same rule as server.py).
async function mangabakaFor(e) {
  for (const term of (e.names || [e.title]).slice(0, 3)) {
    const res = await politeFetch(`${MANGABAKA}/series/search?${new URLSearchParams({ q: term, limit: 10 })}`);
    if (res.status === 429) throw new Error("rate limited");
    if (!res.ok) continue;
    const found = (await res.json()).data || [];
    const hit = found.find((d) => (d.source?.anilist || {}).id === e.id);
    if (hit) return hit;
  }
  return null;
}

// Kitsu (no key, browsers allowed) knows the chapter count of FINISHED series, matched by AniList id.
async function kitsuCount(e) {
  const q = new URLSearchParams({
    "filter[externalSite]": "anilist/manga",
    "filter[externalId]": e.id,
    include: "item",
    "fields[manga]": "chapterCount",
  });
  const res = await fetch(`${KITSU}/mappings?${q}`);
  if (!res.ok) return null;
  const item = ((await res.json()).included || [])[0];
  return item?.attributes?.chapterCount > 0 ? item.attributes.chapterCount : null;
}

// Same idea as reconcile() in server.py for counts: take the lower median; a count more than 5%
// away from it is outvoted. A lone count (a series still running) is just "1 source".
function applyCounts(e, counts, hiatus) {
  const values = counts.map((c) => c.n).sort((a, b) => a - b);
  const mid = values.length ? values[Math.floor((values.length - 1) / 2)] : 0;
  e.sources = counts.map((c) => ({ name: c.name, kind: "count", chapter: String(c.n), publishedAt: null, plausible: Math.abs(c.n - mid) <= 0.05 * mid }));
  e.chapter = mid > 0 ? String(mid) : null;
  e.countOnly = mid > 0;
  e.hiatus = hiatus;
  e.agreement = counts.length > 1 ? "counts" : counts.length === 1 ? "single" : undefined;
  e.source = (e.sources.find((s) => s.plausible) || {}).name;
  e.url = e.source === "MangaBaka" && e.mbUrl ? e.mbUrl : `https://anilist.co/manga/${e.id}`;
}

async function lookUp(e) {
  const mb = await mangabakaFor(e); // may throw "rate limited": the caller stops and tries later
  const counts = [];
  const total = mb ? parseFloat(mb.total_chapters) : NaN;
  if (total > 0) counts.push({ name: "MangaBaka", n: total });
  if (e.status === "FINISHED") {
    if (e.alChapters) counts.push({ name: "AniList", n: e.alChapters });
    try {
      const k = await kitsuCount(e);
      if (k) counts.push({ name: "Kitsu", n: k });
    } catch {
      /* Kitsu is optional */
    }
  }
  if (!counts.length && e.chapter) return; // a lookup that finds nothing must not erase what we already knew
  e.mbUrl = mb?.canonical_url || null;
  applyCounts(e, counts, mb?.status === "hiatus");
}

// One queue for the whole page: lookups run one at a time, paced, so adding 40 series at once
// doesn't trip the rate limit. When MangaBaka keeps refusing, stop and try again on the next visit.
let fillChain = Promise.resolve();
let fillLeft = 0;
let blockedUntil = 0; // after a rate limit, nobody asks MangaBaka again for a minute

function fillMine(list, { basics = true } = {}) {
  const todo = list.filter((e) => !e.queued);
  todo.forEach((e) => (e.queued = true));
  fillLeft += todo.length;
  const run = async () => {
    if (basics) await fillBasics(todo);
    for (const e of todo) {
      const stale = !e.mbAt || Date.now() - e.mbAt > MB_FRESH_MS;
      if (stale && Date.now() >= blockedUntil) {
        try {
          await lookUp(e);
          e.mbAt = Date.now();
          e.lookupFailed = false;
        } catch {
          blockedUntil = Date.now() + 60000; // rate limited (or offline): the rest waits for the next visit
          e.lookupFailed = true;
        }
        await sleep(700);
      } else if (stale) {
        e.lookupFailed = true;
      }
      e.pending = false;
      e.queued = false;
      fillLeft -= 1;
      if (viewMode === "mine") renderList();
    }
    saveMine();
    if (viewMode === "mine") renderList();
  };
  fillChain = fillChain.then(run, run);
  return fillChain;
}

function updateModeSwitch() {
  document.getElementById("mine-count").textContent = mineEntries.length ? `(${mineEntries.length})` : "";
  document.querySelectorAll("#mode-switch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === viewMode)));
}

function setMode(mode) {
  viewMode = mode === "mine" && !canEdit ? "mine" : "sample";
  entries = viewMode === "mine" ? mineEntries : sampleEntries;
  try {
    if (!canEdit) localStorage.setItem(MODE_KEY, viewMode);
  } catch {
    /* ignore */
  }
  // The "behind" options and the "+12" legend only mean something for a personal list.
  document.querySelectorAll(".owner-only").forEach((n) => (n.hidden = !personal()));
  document.querySelectorAll("option[data-owner]").forEach((o) => {
    o.hidden = o.disabled = !personal();
    if (o.disabled && o.selected) o.parentElement.value = o.parentElement.id === "sort-by" ? "title" : "all";
  });
  document.getElementById("about").hidden = canEdit || viewMode === "mine";
  document.getElementById("mine-tools").hidden = canEdit || viewMode !== "mine";
  updateModeSwitch();
  renderList();
  if (viewMode === "mine") fillMine(mineEntries.filter((e) => e.pending || !e.mbAt || Date.now() - e.mbAt > MB_FRESH_MS), { basics: false });
}

// Returns the removed entry (so Undo can put it back), like removeSeries.
function removeMine(id) {
  const removed = mineEntries.find((e) => e.id === id) || null;
  if (!removed) return null;
  mineEntries = mineEntries.filter((e) => e.id !== id);
  if (viewMode === "mine") entries = mineEntries;
  saveMine();
  updateModeSwitch();
  renderList();
  renderDiscover();
  return removed;
}

function restoreMine(entry) {
  if (mineEntries.some((e) => e.id === entry.id)) return;
  mineEntries.push(entry);
  if (viewMode === "mine") entries = mineEntries;
  saveMine();
  updateModeSwitch();
  renderList();
  renderDiscover();
}

function addToMine(m, btn) {
  if (mineEntries.some((e) => e.id === m.id)) return;
  const e = { ...m, read: 0, pending: true };
  mineEntries.push(e);
  if (viewMode === "mine") entries = mineEntries;
  saveMine();
  updateModeSwitch();
  if (btn) {
    btn.textContent = "Added";
    btn.disabled = true;
  }
  toast(`Added ${m.title} to My list`, () => removeMine(m.id));
  if (viewMode === "mine") renderList();
  fillMine([e], { basics: false });
}

// ---------- My list: back it up, bring a list in ----------

function downloadMine() {
  const msg = document.getElementById("mine-msg");
  if (mineEntries.length === 0) {
    msg.textContent = "Your list is empty: nothing to export yet.";
    return;
  }
  const blob = new Blob([JSON.stringify(buildMineExport(mineEntries, new Date().toISOString()), null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "manga-tracker-my-list.json";
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
  msg.textContent = `Exported ${mineEntries.length} series. Keep the file somewhere safe.`;
}

// A file exported from here (.json) or a MyAnimeList manga export (.xml), read in the browser.
async function importIntoMine(file) {
  const msg = document.getElementById("mine-msg");
  if (file.size > 20 * 1024 * 1024) {
    msg.textContent = "That file is too big to be a list.";
    return;
  }
  let text;
  try {
    text = await file.text();
  } catch {
    msg.textContent = "Couldn't read that file.";
    return;
  }
  let items;
  let unmatched = 0;
  let invalid = 0;
  if (/^\s*<\?xml|^\s*<myanimelist/i.test(text) || /\.xml$/i.test(file.name)) {
    const wanted = [...document.querySelectorAll(".mine-status:checked")].map((c) => c.value);
    if (wanted.length === 0) {
      msg.textContent = "Tick at least one status to import.";
      return;
    }
    const parsed = parseMalExport(text, wanted);
    if (parsed.error) {
      msg.textContent = parsed.error;
      return;
    }
    let found;
    try {
      found = await matchMalOnAniList(parsed.rows, (done, total) => (msg.textContent = `Matching ${done} of ${total} on AniList...`));
    } catch (err) {
      msg.textContent = `AniList lookup failed: ${err.message}`;
      return;
    }
    items = parsed.rows.filter((r) => found.has(r.mal)).map((r) => ({ id: found.get(r.mal).id, title: found.get(r.mal).title.english || found.get(r.mal).title.romaji, read: r.read }));
    unmatched = parsed.rows.length - items.length;
  } else {
    const parsed = parseMineFile(text);
    if (parsed.error) {
      msg.textContent = parsed.error;
      return;
    }
    items = parsed.items;
    invalid = parsed.invalid;
  }
  const { added, already, skipped } = mergeIntoMine(mineEntries, items, MAX_MINE);
  mineEntries.push(...added);
  if (viewMode === "mine") entries = mineEntries;
  saveMine();
  updateModeSwitch();
  renderList();
  const bits = [`Added ${added.length}`];
  if (already) bits.push(`${already} already in your list`);
  if (unmatched) bits.push(`${unmatched} not found on AniList`);
  if (invalid) bits.push(`${invalid} unusable entries skipped`);
  if (skipped) bits.push(`${skipped} left out: a list holds at most ${MAX_MINE}`);
  msg.textContent = bits.join(", ") + (added.length ? ". Looking up their chapters..." : ".");
  fillMine(added); // covers and chapter counts, one by one (see fillMine)
}

// ---------- Discover: browse and search, then add ----------

const DISCOVER_SORT = {
  popular: { label: "Popular", sort: "POPULARITY_DESC" },
  trending: { label: "Trending", sort: "TRENDING_DESC" },
  rated: { label: "Top rated", sort: "SCORE_DESC" },
  newest: { label: "New releases", sort: "START_DATE_DESC" },
};
const GENRES = ["Action", "Adventure", "Comedy", "Drama", "Fantasy", "Horror", "Mystery", "Psychological", "Romance", "Sci-Fi", "Slice of Life", "Sports", "Supernatural", "Thriller"];
const disc = { by: "popular", genre: "", text: "", page: 0, items: [], more: false, busy: false, error: null, seq: 0 };

async function discoverLoad(reset) {
  if (disc.busy && !reset) return;
  if (reset) {
    disc.page = 0;
    disc.items = [];
  }
  const mine = ++disc.seq; // a newer query makes an older answer irrelevant
  disc.busy = true;
  disc.error = null;
  renderDiscover();
  try {
    const q = disc.text.trim();
    const vars = { page: disc.page + 1, sort: q ? "SEARCH_MATCH" : DISCOVER_SORT[disc.by].sort };
    if (q) vars.search = q;
    if (disc.genre) vars.genre = disc.genre;
    const data = await anilist(
      `query ($page: Int, $sort: [MediaSort], $search: String, $genre: String) {
         Page(page: $page, perPage: 24) {
           pageInfo { hasNextPage }
           media(type: MANGA, isAdult: false, format_not_in: [NOVEL], sort: $sort, search: $search, genre: $genre) { ${FIELDS} }
         } }`,
      vars
    );
    if (mine !== disc.seq) return;
    disc.page += 1;
    disc.items = disc.items.concat(data.Page.media.map(fromAniList));
    disc.more = data.Page.pageInfo.hasNextPage;
  } catch (err) {
    if (mine !== disc.seq) return;
    disc.error = err.message;
  } finally {
    if (mine === disc.seq) {
      disc.busy = false;
      renderDiscover();
    }
  }
}

function inLibrary(id) {
  return (canEdit ? entries : mineEntries).some((e) => e.id === id);
}

function addToLibrary(m, btn) {
  if (canEdit) addSeries(m, btn).then(renderDiscover);
  else addToMine(m, btn);
}

function renderDiscover() {
  const grid = document.getElementById("discover-grid");
  if (!grid) return;
  grid.setAttribute("aria-busy", String(disc.busy));
  grid.replaceChildren(
    ...disc.items.map((m) => {
      const btn = el("button", "", inLibrary(m.id) ? "Added" : "Add");
      btn.type = "button";
      btn.disabled = inLibrary(m.id);
      btn.addEventListener("click", () => addToLibrary(m, btn));
      return makeTile(m, { button: btn });
    })
  );
  const msg = document.getElementById("discover-msg");
  msg.replaceChildren();
  if (disc.error) {
    msg.append(`Couldn't load manga (${disc.error}). `);
    const retry = el("button", "link-btn", "Try again");
    retry.type = "button";
    retry.addEventListener("click", () => discoverLoad(!disc.items.length));
    msg.append(retry);
  } else msg.textContent = disc.busy && !disc.items.length
      ? "Loading..."
      : !disc.busy && !disc.items.length && disc.page
        ? "No results."
        : "";
  const more = document.getElementById("discover-more");
  more.hidden = !disc.more || Boolean(disc.error);
  more.disabled = disc.busy;
  more.textContent = disc.busy ? "Loading..." : "Show more";
  document.getElementById("discover-where").textContent = canEdit
    ? "Adding saves to series.json, so the daily bot tracks it."
    : "Adding saves to My list, which stays in this browser.";
}

function initDiscover() {
  const chips = document.getElementById("discover-sorts");
  Object.entries(DISCOVER_SORT).forEach(([key, v]) => {
    const b = el("button", "stat", v.label);
    b.type = "button";
    b.dataset.by = key;
    b.addEventListener("click", () => {
      disc.by = key;
      document.getElementById("discover-text").value = "";
      disc.text = "";
      syncDiscoverControls();
      discoverLoad(true);
    });
    chips.append(b);
  });
  const genre = document.getElementById("discover-genre");
  GENRES.forEach((g) => genre.append(new Option(g, g)));
  genre.addEventListener("change", () => {
    disc.genre = genre.value;
    discoverLoad(true);
  });
  let t;
  document.getElementById("discover-text").addEventListener("input", (ev) => {
    clearTimeout(t);
    t = setTimeout(() => {
      disc.text = ev.target.value;
      syncDiscoverControls();
      discoverLoad(true);
    }, 400);
  });
  document.getElementById("discover-more").addEventListener("click", () => discoverLoad(false));
  syncDiscoverControls();
}

function syncDiscoverControls() {
  document.querySelectorAll("#discover-sorts button").forEach((b) => {
    const on = !disc.text.trim() && b.dataset.by === disc.by;
    b.classList.toggle("active", on);
    b.setAttribute("aria-pressed", String(on));
  });
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

// Returns the removed entry (so it can be restored), or null if it failed.
async function removeSeries(id) {
  const removed = entries.find((e) => e.id === id) || null;
  const res = await fetch(`api/series?id=${id}`, { method: "DELETE" });
  if (!res.ok) return null;
  entries = entries.filter((e) => e.id !== id);
  renderList();
  renderResults();
  renderDiscover();
  document.getElementById("list-msg").textContent =
    "Removed from series.json. Commit and push it to stop the bot tracking it.";
  return removed;
}

// "Undo" for a removal: write the entry back to series.json and to the page.
async function restoreSeries(entry) {
  const body = { id: entry.id, title: entry.title };
  if (entry.mal != null) body.mal = entry.mal;
  if (entry.read != null) body.read = entry.read;
  const res = await fetch("api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) return toast("Could not restore it.");
  entries.push(entry);
  renderList();
  renderResults();
  renderDiscover();
  document.getElementById("list-msg").textContent = `Restored ${entry.title}.`;
}

// A small message at the bottom of the screen, with an optional Undo, gone after 8 seconds.
let toastTimer;
function toast(message, undo) {
  const box = document.getElementById("toast");
  box.replaceChildren(el("span", "", message));
  if (undo) {
    const b = el("button", "link-btn", "Undo");
    b.type = "button";
    b.addEventListener("click", async () => {
      clearTimeout(toastTimer);
      box.hidden = true;
      await undo();
    });
    box.append(b);
  }
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), 8000);
}

async function refreshNow() {
  const btn = document.getElementById("refresh");
  btn.disabled = true;
  document.getElementById("list-msg").textContent = "Checking all the sources...";
  entries.forEach((e) => (e.pending = true));
  await fillLive(entries);
  checkedAt = new Date().toISOString();
  renderList();
  document.getElementById("list-msg").textContent =
    "Updated live. The daily bot is what writes state.json.";
  btn.disabled = false;
}

// ---------- import a MyAnimeList export ----------
// On MAL: Profile > Export > Manga list gives an .xml file. We read it HERE in the
// browser; only titles, MAL ids and chapters-read are sent on to series.json.
// AniList can look up a manga by its MAL id, so each match is exact.

// MyAnimeList ids -> AniList entries (MAL id, AniList id, title), 50 at a time (AniList's page limit).
async function matchMalOnAniList(rows, onProgress) {
  const found = new Map();
  for (let i = 0; i < rows.length; i += 50) {
    onProgress(Math.min(i + 50, rows.length), rows.length);
    const data = await anilist(
      `query ($ids: [Int]) { Page(perPage: 50) {
         media(idMal_in: $ids, type: MANGA) { id idMal title { romaji english } } } }`,
      { ids: rows.slice(i, i + 50).map((r) => r.mal) }
    );
    data.Page.media.forEach((m) => found.set(m.idMal, m));
  }
  return found;
}

async function importMal(file) {
  const msg = document.getElementById("import-msg");
  const wanted = [...document.querySelectorAll(".import-status:checked")].map((c) => c.value);
  if (wanted.length === 0) {
    msg.textContent = "Tick at least one status to import.";
    return;
  }

  msg.textContent = "Reading the file...";
  const parsed = parseMalExport(await file.text(), wanted); // mylist.js (tested)
  if (parsed.error) {
    msg.textContent = parsed.error;
    return;
  }
  const rows = parsed.rows;

  let found;
  try {
    found = await matchMalOnAniList(rows, (done, total) => (msg.textContent = `Matching ${done} of ${total} on AniList...`));
  } catch (err) {
    msg.textContent = `AniList lookup failed: ${err.message}`;
    return;
  }

  const items = rows
    .filter((r) => found.has(r.mal))
    .map((r) => {
      const m = found.get(r.mal);
      return { id: m.id, title: m.title.english || m.title.romaji, mal: r.mal, read: r.read };
    });
  const res = await fetch("api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(items),
  });
  if (!res.ok) {
    msg.textContent = "Could not save to series.json.";
    return;
  }

  const unmatched = rows.length - items.length;
  msg.textContent =
    `Imported ${items.length} series` +
    (unmatched ? ` (${unmatched} not found on AniList)` : "") +
    ". Loading chapter info...";
  await loadAll((done, total) => {
    document.getElementById("list-msg").textContent = `Loading chapter info ${done}/${total}...`;
    renderList();
  });
  renderList();
  renderResults();
  msg.textContent = `Imported ${items.length} series${unmatched ? ` (${unmatched} not found on AniList)` : ""}.`;
  document.getElementById("list-msg").textContent =
    "Saved to series.json. Commit and push it so the daily bot tracks them.";
}

// ---------- Activity tab (the bot's history log) ----------
// history.jsonl = one JSON event per line, appended by check.py whenever something changes.

let historyEvents = [];

async function loadHistory() {
  try {
    const res = await fetch("history.jsonl");
    if (!res.ok) return;
    historyEvents = (await res.text())
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        try {
          return JSON.parse(line);
        } catch {
          return null; // skip a damaged line instead of failing the whole tab
        }
      })
      .filter(Boolean);
  } catch {
    historyEvents = [];
  }
}

function describeEvent(ev) {
  const status = (s) => STATUS_LABEL[s] || s;
  switch (ev.type) {
    case "chapter":
      return { icon: "📖", text: `${ev.title}: chapter ${ev.chapter}`, note: ev.backfilled ? "" : "new, seen by the bot" };
    case "first_seen":
      return { icon: "✨", text: `Started tracking ${ev.title} at chapter ${ev.chapter ?? "?"}`, note: "" };
    case "break_start":
      return { icon: "⏸️", text: `${ev.title} looks on break`, note: ev.reason === "hiatus" ? "listed as hiatus" : `${Math.floor((ev.quietDays || 0) / 7)} weeks quiet` };
    case "break_end":
      return { icon: "▶️", text: `${ev.title} is back`, note: "" };
    case "dormant":
      return { icon: "💤", text: `${ev.title} marked dormant`, note: `no chapter for over ${Math.floor((ev.dormantDays || 0) / 365)} years` };
    case "revived":
      return { icon: "🌱", text: `${ev.title} is back after years of silence`, note: "" };
    case "status":
      return { icon: "ℹ️", text: `${ev.title}: ${status(ev.previous)} to ${status(ev.status)}`, note: "AniList status" };
    case "source_change":
      return { icon: "🔁", text: `${ev.title}: now using ${ev.source} (ch. ${ev.previous} to ${ev.chapter})`, note: "data correction, not a new chapter", muted: true };
    default:
      return { icon: "•", text: `${ev.title}: ${ev.type}`, note: "", muted: true };
  }
}

function renderActivity() {
  // "run" lines are bookkeeping for the Health tab, not news
  const events = historyEvents.filter((e) => e.type !== "run").sort((a, b) => b.t.localeCompare(a.t));
  const weekAgo = Date.now() - 7 * DAY;
  const recent = events.filter((e) => new Date(e.t).getTime() >= weekAgo);
  const count = (type) => recent.filter((e) => e.type === type).length;
  document.getElementById("activity-summary").textContent = events.length
    ? `Last 7 days: ${count("chapter")} chapters, ${count("break_start")} series started a break, ${count("break_end")} came back.`
    : "No history yet. The daily bot writes history.jsonl; run devtools.py backfill once for older chapters.";

  const shown = events.slice(0, 60);
  document.getElementById("activity").replaceChildren(
    ...shown.map((ev) => {
      const d = describeEvent(ev);
      const when = fmtShort(new Date(ev.publishedAt || ev.t));
      const item = li(span("when", when), `${d.icon} ${d.text}`, " ", span("note", d.note));
      if (d.muted) item.classList.add("muted");
      return item;
    })
  );
  document.getElementById("activity-more").textContent =
    events.length > shown.length ? `Showing the newest ${shown.length} of ${events.length} events.` : "";
}

// ---------- Health tab: can I trust what I see? ----------
// The bot saves how each source behaved on its last run (state.json "health") and one
// "run" line per day in history.jsonl. This tab turns that into: is the bot alive, did
// the sources answer, and which series rest on a single source.

const SOURCE_ROLE = {
  MangaDex: "chapters + dates",
  MangaUpdates: "chapters + dates",
  MangaBaka: "chapter count + hiatus + ids",
  Webtoon: "official episodes + dates",
  Kitsu: "chapter count + end date (finished)",
  Shikimori: "status (paused / discontinued) + last chapter date, not a count",
  AniList: "chapter count (finished)",
  "MangaDex (any language)": "fallback for a running series with no English chapters",
};
const STALE_HOURS = 36; // the bot runs daily; more than this = something is wrong

function fmtDuration(sec) {
  return sec >= 60 ? `${Math.floor(sec / 60)}m ${sec % 60}s` : `${sec}s`;
}

// ok / warn / bad for one source's counters from a run
function sourceState(s) {
  const total = s.ok + s.none + s.failed;
  if (s.failed && total && s.failed / total >= 0.5) return "bad";
  return s.failed || s.rateLimited ? "warn" : "ok";
}

function botAgeHours() {
  return checkedAt ? (Date.now() - new Date(checkedAt).getTime()) / 3600000 : null;
}

// Overall state, also used for the dot on the tab.
function healthState() {
  const age = botAgeHours();
  if (age === null) return "unknown";
  if (age > STALE_HOURS) return "bad";
  if (!health) return "ok";
  const states = Object.values(health.sources).map(sourceState);
  if (states.includes("bad")) return "bad";
  if (states.includes("warn") || health.lookupFailed > 0) return "warn";
  return "ok";
}

function updateHealthDot() {
  const tab = document.getElementById("tabbtn-health");
  tab.classList.remove("warn", "bad");
  const state = healthState();
  if (state === "warn" || state === "bad") tab.classList.add(state);
}

function renderHealth() {
  // Banner: the loudest problem first
  const banner = document.getElementById("health-banner");
  const age = botAgeHours();
  const state = healthState();
  if (age !== null && age > STALE_HOURS) {
    banner.className = "banner bad";
    banner.textContent = `The daily bot hasn't run for ${Math.floor(age)} hours (last: ${new Date(checkedAt).toLocaleString()}). What you see may be out of date.`;
    banner.hidden = false;
  } else if (state === "bad" || state === "warn") {
    const down = Object.entries(health.sources)
      .filter(([, s]) => sourceState(s) === "bad")
      .map(([name]) => name);
    banner.className = `banner ${state}`;
    banner.textContent =
      state === "bad"
        ? `${down.join(" and ")} ${down.length > 1 ? "were" : "was"} mostly down on the last run. Results that depend on ${down.length > 1 ? "them" : "it"} may be stale.`
        : "Some requests failed or were rate-limited on the last run. See the sources below.";
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }

  // Last run + per-source cards
  const summary = document.getElementById("health-summary");
  const list = document.getElementById("health-sources");
  if (!health) {
    summary.textContent = checkedAt
      ? "No health data yet. The next daily run records it."
      : "No bot run found (state.json is missing).";
    list.replaceChildren();
  } else {
    summary.textContent =
      `Finished ${new Date(health.finishedAt).toLocaleString()}, took ${fmtDuration(health.durationSec)}, ` +
      `${health.series} series` +
      (health.lookupFailed ? `, ${health.lookupFailed} with no answer from any source` : "");
    list.replaceChildren(
      ...Object.entries(health.sources).map(([name, s]) => {
        const total = s.ok + s.none + s.failed;
        const item = document.createElement("li");
        const dot = span(`dot ${sourceState(s)}`, "");
        const head = document.createElement("div");
        head.append(dot, span("when", name), span("note", SOURCE_ROLE[name] || ""));
        const stats =
          `answered ${s.ok} of ${total || health.series}` +
          (s.none ? ` · no entry for ${s.none}` : "") +
          (s.failed ? ` · FAILED ${s.failed}` : "") +
          (s.rateLimited ? ` · rate-limited ${s.rateLimited}x` : "") +
          (s.cached ? ` · cached ${s.cached}` : "") +
          ` · ${s.seconds}s`;
        const line = document.createElement("div");
        line.className = "line2";
        line.textContent = stats;
        item.append(head, line);
        (s.errors || []).slice(0, 3).forEach((msg) => {
          const err = document.createElement("div");
          err.className = "line2";
          err.textContent = `error: ${msg}`;
          item.append(err);
        });
        return item;
      })
    );
  }

  // Rules that changed an answer on the last run (the bot saves the counts and their labels)
  const ruleList = document.getElementById("health-rules");
  if (!health || !health.rules) {
    ruleList.replaceChildren(li(span("note", "Recorded from the next daily run.")));
  } else {
    const rules = Object.entries(health.rules).sort((a, b) => b[1] - a[1]);
    ruleList.replaceChildren(
      ...(rules.length
        ? rules.map(([name, n]) => li(span("when", String(n)), ` ${(health.ruleLabels || {})[name] || name}`))
        : [li(span("note", "No rule changed an answer."))])
    );
  }

  // Coverage: how many sources back each series (from the data on the page)
  const usable = (e) => (e.sources || []).filter((s) => s.plausible).length;
  const known = botEntries().filter((e) => (e.sources || []).length > 0);
  const bucket = (test) => known.filter((e) => test(usable(e)));
  const rows = [
    ["3 or more usable sources", bucket((n) => n >= 3).length],
    ["2 usable sources", bucket((n) => n === 2).length],
    ["1 usable source", bucket((n) => n === 1).length],
    ["No usable source", bucket((n) => n === 0).length],
  ];
  const single = bucket((n) => n === 1).map((e) => e.title);
  document.getElementById("health-coverage").replaceChildren(
    ...(known.length
      ? [
          ...rows.map(([label, n]) => li(span("when", String(n)), label)),
          li(
            span("note", single.length
              ? `Resting on a single source (most fragile): ${single.slice(0, 10).join(", ")}${single.length > 10 ? ` and ${single.length - 10} more` : ""}`
              : "No series rests on a single source.")
          ),
        ]
      : [li(span("note", "No source data yet."))])
  );

  // Recent runs from the history log
  const runs = historyEvents.filter((e) => e.type === "run").sort((a, b) => b.t.localeCompare(a.t)).slice(0, 14);
  document.getElementById("health-runs").replaceChildren(
    ...(runs.length
      ? runs.map((r) => {
          const bad = Object.entries(r.sources || {}).filter(([, v]) => v.failed || v.rateLimited);
          const problems = bad.length
            ? bad.map(([n, v]) => `${n}: ${v.failed ? v.failed + " failed" : ""}${v.failed && v.rateLimited ? ", " : ""}${v.rateLimited ? v.rateLimited + " rate-limited" : ""}`).join("; ")
            : "all sources ok";
          return li(
            span("when", fmtShort(new Date(r.t))),
            `${r.series} series, ${fmtDuration(r.durationSec)} · `,
            span("note", problems)
          );
        })
      : [li(span("note", "No runs recorded yet. The next daily run adds the first one."))])
  );
}

// ---------- rendering: Calendar ----------

// One list of calendar events: past releases, plus projected ones from each
// series' usual gap. Quiet series are NOT projected (they are listed as overdue).
function buildEvents() {
  const today = startOfDay(new Date());
  const horizon = addDays(today, 120);
  const events = [];

  for (const e of botEntries()) {
    if (isLimited(e)) continue; // no source can be trusted: keep it off the calendar
    for (const h of e.history || []) {
      events.push({ date: localDay(h.date), kind: "released", id: e.id, title: e.title, label: `${e.title} ch. ${h.chapter}` });
    }
    if (e.status !== "RELEASING" || !e.expectedNext || !e.typicalGapDays || quietDays(e) || onHiatus(e)) continue;

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
  const overdue = botEntries().filter((e) => !isDormant(e) && (quietDays(e) || onHiatus(e)));
  document.getElementById("overdue-block").hidden = overdue.length === 0;
  document.getElementById("overdue").replaceChildren(
    ...overdue.map((e) => {
      const usual = e.typicalGapDays ? `, usually every ${Math.round(e.typicalGapDays)} days` : "";
      const q = quietDays(e);
      return li(
        span("when", e.title),
        span("note", q ? `last chapter ${e.chapter}, ${fmtQuiet(q)} ago${usual}` : `listed as on hiatus (last chapter ${e.chapter})`)
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

const TAB_ORDER = ["list", "discover", "calendar", "activity", "health"];

function selectTab(name, focus = false) {
  TAB_ORDER.forEach((t) => {
    const btn = document.getElementById(`tabbtn-${t}`);
    const on = t === name;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", String(on));
    btn.tabIndex = on ? 0 : -1; // only the open tab is in the tab order; arrow keys move between them
    document.getElementById(`tab-${t}`).hidden = !on;
    if (on && focus) btn.focus();
  });
  if (name === "discover" && !disc.page && !disc.busy) discoverLoad(true);
  if (name === "calendar") renderCalendar();
  if (name === "activity") renderActivity();
  if (name === "health") renderHealth();
}

TAB_ORDER.forEach((t, i) => {
  const btn = document.getElementById(`tabbtn-${t}`);
  btn.addEventListener("click", () => selectTab(t));
  btn.addEventListener("keydown", (ev) => {
    const n = TAB_ORDER.length;
    const next = { ArrowRight: (i + 1) % n, ArrowLeft: (i + n - 1) % n, Home: 0, End: n - 1 }[ev.key];
    if (next !== undefined) {
      ev.preventDefault();
      selectTab(TAB_ORDER[next], true);
    }
  });
});

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
document.getElementById("import-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (file) await importMal(file);
  ev.target.value = ""; // lets you pick the same file again later
});
document.getElementById("refresh").addEventListener("click", refreshNow);
document.getElementById("mine-export").addEventListener("click", downloadMine);
document.getElementById("mine-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (file) await importIntoMine(file);
  ev.target.value = ""; // lets you pick the same file again
});
document.querySelectorAll("#mode-switch button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

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

// Detail panel: close on a click on the dimmed backdrop; forget the open entry when it closes.
const detailDialog = document.getElementById("detail");
detailDialog.addEventListener("click", (ev) => {
  if (ev.target === detailDialog) detailDialog.close();
});
detailDialog.addEventListener("close", () => (detailEntry = null));

for (const id of ["filter-text", "filter-show", "sort-by"]) {
  document.getElementById(id).addEventListener(id === "filter-text" ? "input" : "change", () => {
    saveView();
    renderList();
  });
}

// Grey placeholder tiles while the data loads, so the page is never blank.
function showSkeleton() {
  const grid = document.getElementById("mylist");
  grid.setAttribute("aria-busy", "true");
  grid.replaceChildren(
    ...Array.from({ length: 12 }, () => {
      const li = el("li", "tile skeleton");
      li.append(el("span", "cover"), el("span", "meta"));
      li.lastChild.append(el("span", "t"), el("span", "s"));
      return li;
    })
  );
}

async function init() {
  showSkeleton();
  // Is server.py running? (On a static host this 404s and we are a read-only VISITOR view.)
  try {
    const res = await fetch("api/ping");
    canEdit = res.ok && (await res.json()).edit === true;
  } catch {
    canEdit = false;
  }
  // Owner mode (server.py) can add, import, edit settings and see its own reading progress.
  // Visitor mode (the published site) shows the library, an about box and nothing personal.
  document.getElementById("add-section").hidden = !canEdit;
  document.getElementById("import-section").hidden = !canEdit; // the import lives at the bottom of the list
  document.getElementById("refresh").hidden = !canEdit;
  document.getElementById("mode-switch").hidden = canEdit; // visitors choose: sample library or My list
  initDiscover();
  restoreView();

  await loadAll();
  sampleEntries = entries;
  mineEntries = loadMine().map((x) => ({ ...x, pending: !x.mbAt }));
  let mode = "sample";
  if (!canEdit) {
    try {
      if (localStorage.getItem(MODE_KEY) === "mine") mode = "mine";
    } catch {
      /* ignore */
    }
  }
  setMode(mode); // also draws the list, the legend and the options for that mode
  await loadHistory();
  updateHealthDot();
}

init();
