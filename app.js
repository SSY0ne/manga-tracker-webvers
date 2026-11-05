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
const FIELDS = `id title { romaji english } synonyms startDate { year } status coverImage { medium }`;

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
let health = null;
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
    e.agreement === "agree"
      ? `Sources agree (${names.join(" + ")})`
      : e.agreement === "conflict"
        ? `Sources disagree, using ${used} (fresher)`
        : e.agreement === "single"
          ? `1 source: ${used}`
          : e.agreement === "count"
            ? `Count only (${used}): no release feed fits`
            : "No reliable source";
  const detail = list
    .map((s) => {
      const when = s.publishedAt ? fmtShort(new Date(s.publishedAt)) : "count";
      return `${s.name}: ch. ${s.chapter ?? "?"} (${when})${s.plausible ? "" : " ✗ doesn't fit"}`;
    })
    .join(" · ");
  return { text, detail, kind: e.agreement };
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

// Fill entries with live data (AniList for covers/status, server.py for chapters).
async function fillLive(list, onProgress) {
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

async function loadAll(onProgress) {
  const series = (await fetchJSON("series.json")) || [];
  const raw = (await fetchJSON("state.json")) || {};
  const state = raw.series || raw; // the first version of state.json had no wrapper
  checkedAt = raw.checkedAt || null;
  health = raw.health || null; // how the last bot run went (see the Health tab)

  // An entry from the OLD state.json format has no history/cover: treat it as pending too,
  // so the page fills it live until the bot rewrites state.json in the new format.
  // On a read-only host, progress you set in this browser wins over series.json.
  const overrides = canEdit ? {} : loadReadOverrides();
  entries = series.map((s) => {
    const known = state[s.id];
    // mal / read (chapters you've read) come from series.json, not from the bot's state
    return {
      id: s.id,
      title: s.title,
      ...known,
      mal: s.mal,
      read: overrides[s.id] ?? s.read,
      alerts: s.alerts, // per-series settings from series.json (see the panel)
      gapDays: s.gapDays,
      pending: !known || !known.history,
    };
  });
  await fillLive(entries.filter((e) => e.pending), onProgress);
}

// ---------- rendering: My list ----------

// ---------- reading progress: "I'm up to date" / "I'm actually at chapter N" ----------
// With server.py running, the new number is written to series.json (the bot reads it too).
// On a read-only host it is kept in THIS browser only (localStorage) and applied on load.

const READ_KEY = "manga-tracker:read";

function loadReadOverrides() {
  try {
    return JSON.parse(localStorage.getItem(READ_KEY) || "{}");
  } catch {
    return {};
  }
}

async function setRead(e, n) {
  n = Math.max(0, Math.floor(n));
  if (canEdit) {
    const res = await fetch("api/series", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: e.id, title: e.title, read: n }),
    });
    if (!res.ok) return false;
  } else {
    try {
      const overrides = loadReadOverrides();
      overrides[e.id] = n;
      localStorage.setItem(READ_KEY, JSON.stringify(overrides));
    } catch {
      return false; // private mode: can't remember it
    }
  }
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
  if (isLimited(e)) return { cls: "limited", text: "Limited data" };
  if (isOnBreak(e)) return { cls: "break", text: "On break" };
  if (isNew(e)) return { cls: "new", text: "New" };
  if (e.status === "FINISHED") return { cls: "finished", text: "Finished" };
  return null;
}

function tileSubtitle(e) {
  if (e.chapter) {
    // For a series on break the date is old news: say WHY it is flagged instead.
    const q = quietDays(e);
    const why = q ? `${fmtQuiet(q)} quiet` : onHiatus(e) ? "listed as hiatus" : null;
    if (why) return `Ch. ${e.chapter} · ${why}`;
    return `Ch. ${e.chapter}` + (e.publishedAt ? ` · ${fmtShort(new Date(e.publishedAt))}` : "");
  }
  return e.pending ? "Checking..." : STATUS_LABEL[e.status] || "";
}

// opts.button = a search result with an Add button (not clickable);
// opts.compact = the small "new this week" strip.
function makeTile(e, opts = {}) {
  const li = document.createElement("li");
  li.className = "tile" + (opts.button ? " static" : "");

  const cover = document.createElement("div");
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
    const behind = behindShown(e);
    if (behind > 0) {
      const p = document.createElement("span");
      p.className = "behind-pill";
      p.textContent = `+${behind}`;
      p.title = `${behind} chapters behind`;
      cover.append(p);
    }
    if (e.read && e.chapter) {
      const pct = Math.max(0, Math.min(100, Math.round((100 * e.read) / parseFloat(e.chapter))));
      const bar = document.createElement("div");
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

  const meta = document.createElement("div");
  meta.className = "meta";
  const t = document.createElement("div");
  t.className = "t";
  t.textContent = e.title;
  const s = document.createElement("div");
  s.className = "s";
  s.textContent = tileSubtitle(e);
  meta.append(t, s);
  li.append(cover, meta);

  if (!opts.button) {
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.setAttribute("aria-label", `Open ${e.title}`);
    li.addEventListener("click", () => openDetail(e));
    li.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        openDetail(e);
      }
    });
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
async function saveSettings(e, alerts, gapDays) {
  const res = await fetch("api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: e.id, title: e.title, alerts: alerts === "all" ? null : alerts, gapDays: gapDays || null }),
  });
  if (!res.ok) return false;
  e.alerts = alerts === "all" ? undefined : alerts;
  e.gapDays = gapDays || undefined;
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
    const ok = await saveSettings(e, sel.value, days);
    renderList();
    if (detailEntry === e) renderDetail(e);
    const note = document.getElementById("settings-msg");
    if (note) note.textContent = ok ? "Saved to series.json. Commit and push it so the daily bot uses it." : "Could not save.";
  });
  sec.append(alertsRow, gapRow, hint, save, msg);
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
  if (lead && lead.cls !== "finished") chips.append(el("span", `chip ${lead.cls}`, lead.text));
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
    latest.append(a, e.publishedAt ? ` · ${fmtShort(new Date(e.publishedAt))}` : " · count only, no date");
    facts.append(fact("Latest", latest));
  } else {
    facts.append(fact("Latest", e.pending ? "Checking..." : "No reliable chapter data"));
  }
  if (e.typicalGapDays) facts.append(fact("Usually every", `${Math.round(e.typicalGapDays)} days`));
  if (e.expectedNext && e.status === "RELEASING" && !isOnBreak(e) && !isLimited(e)) {
    facts.append(fact("Next expected", `around ${fmtShort(parseDay(e.expectedNext))}`));
  }
  if (quiet) facts.append(fact("On break?", `${fmtQuiet(quiet)} without a chapter`));
  else if (onHiatus(e)) facts.append(fact("On break?", "listed as hiatus"));
  main.append(facts);

  // Your progress (the editor from before)
  const prog = el("div", "d-section");
  prog.append(el("h3", "", "Your progress"), progressRow(e), el("p", "msg"));
  prog.lastChild.id = "detail-msg";
  main.append(prog);

  // Alerts and rhythm: settings for THIS series, saved in series.json (so only with server.py)
  const mine = [];
  if (e.alerts) mine.push(e.alerts === "off" ? "muted" : "break alerts only");
  if (e.gapDays) mine.push(`rhythm set to every ${e.gapDays} days`);
  if (canEdit) {
    main.append(settingsSection(e));
  } else if (mine.length) {
    const sec = el("div", "d-section");
    sec.append(el("h3", "", "Alerts and rhythm"), el("p", "msg", `This series: ${mine.join(", ")}.`));
    main.append(sec);
  }

  // What each source said
  const list = e.sources || [];
  if (list.length && !e.pending) {
    const src = sourcesLine(e);
    const sec = el("div", "d-section");
    sec.append(el("h3", "", "Sources"));
    if (src) sec.append(el("div", `src-line ${src.kind === "agree" ? "src-agree" : src.kind === "conflict" ? "src-conflict" : ""}`, src.text));
    list.forEach((s) => {
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
    main.append(sec);
  }

  // Actions + links
  const actions = el("div", "d-actions");
  if (canEdit) {
    const rm = el("button", "btn danger", "Remove from my list");
    rm.type = "button";
    rm.addEventListener("click", () => {
      // Removing edits series.json, so ask first. The toast afterwards offers an Undo.
      const ask = el("div", "confirm", `Remove ${e.title} from your list?`);
      const yes = el("button", "btn danger", "Yes, remove");
      yes.type = "button";
      const no = el("button", "btn ghost", "Cancel");
      no.type = "button";
      no.addEventListener("click", () => actions.replaceChildren(rm));
      yes.addEventListener("click", async () => {
        const removed = await removeSeries(e.id);
        document.getElementById("detail").close();
        if (removed) toast(`Removed ${removed.title}`, () => restoreSeries(removed));
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
  return e.status === "RELEASING" && !isOnBreak(e) && !isLimited(e);
}
function behindShown(e) {
  return isRunning(e) ? behindBy(e) : 0;
}
function isOnBreak(e) {
  return quietDays(e) > 0 || onHiatus(e);
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
  new: isNew,
  behind: (e) => behindShown(e) > 0,
  disagree: (e) => e.agreement === "conflict",
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
    if (SHOW[v.show]) document.getElementById("filter-show").value = v.show;
    if (SORT[v.sort]) document.getElementById("sort-by").value = v.sort;
  } catch {
    /* ignore */
  }
}

// The quick-filter chips under the header: a count you can click to filter the grid.
function renderStats() {
  const defs = [
    { key: "all", label: "tracked", n: entries.length, cls: "" },
    { key: "new", label: "new this week", n: entries.filter(isNew).length, cls: "fresh" },
    { key: "onbreak", label: "on break", n: entries.filter(isOnBreak).length, cls: "warm" },
    { key: "behind", label: "to catch up", n: entries.filter((e) => behindShown(e) > 0).length, cls: "" },
  ];
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
  msg.textContent = entries.length
    ? ""
    : canEdit
      ? "Nothing here yet. Search a manga below and press Add."
      : "series.json is empty.";
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
  document.getElementById("list-msg").textContent = "Checking AniList and MangaDex...";
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

async function importMal(file) {
  const msg = document.getElementById("import-msg");
  const wanted = [...document.querySelectorAll(".import-status:checked")].map((c) => c.value);
  if (wanted.length === 0) {
    msg.textContent = "Tick at least one status to import.";
    return;
  }

  msg.textContent = "Reading the file...";
  const doc = new DOMParser().parseFromString(await file.text(), "application/xml");
  if (doc.querySelector("parsererror") || !doc.querySelector("myanimelist")) {
    msg.textContent = "That doesn't look like a MyAnimeList export (.xml).";
    return;
  }
  const text = (m, tag) => (m.querySelector(tag)?.textContent || "").trim();
  const rows = [...doc.querySelectorAll("manga")]
    .map((m) => ({
      mal: Number(text(m, "manga_mangadb_id")),
      status: text(m, "my_status"),
      read: Number(text(m, "my_read_chapters")) || 0,
    }))
    .filter((r) => r.mal && wanted.includes(r.status));
  if (rows.length === 0) {
    msg.textContent = "No manga with those statuses in this file (is it the manga export, not anime?).";
    return;
  }

  // MAL id -> AniList id, 50 at a time (AniList's page limit)
  const found = new Map();
  try {
    for (let i = 0; i < rows.length; i += 50) {
      msg.textContent = `Matching ${Math.min(i + 50, rows.length)} of ${rows.length} on AniList...`;
      const data = await anilist(
        `query ($ids: [Int]) { Page(perPage: 50) {
           media(idMal_in: $ids, type: MANGA) { id idMal title { romaji english } } } }`,
        { ids: rows.slice(i, i + 50).map((r) => r.mal) }
      );
      data.Page.media.forEach((m) => found.set(m.idMal, m));
    }
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
  const tab = document.getElementById("health-tab");
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

  // Coverage: how many sources back each series (from the data on the page)
  const usable = (e) => (e.sources || []).filter((s) => s.plausible).length;
  const known = entries.filter((e) => (e.sources || []).length > 0);
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

  for (const e of entries) {
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
  const overdue = entries.filter((e) => quietDays(e) || onHiatus(e));
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

document.querySelectorAll(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    document.getElementById("tab-list").hidden = btn.dataset.tab !== "list";
    document.getElementById("tab-calendar").hidden = btn.dataset.tab !== "calendar";
    document.getElementById("tab-activity").hidden = btn.dataset.tab !== "activity";
    document.getElementById("tab-health").hidden = btn.dataset.tab !== "health";
    if (btn.dataset.tab === "calendar") renderCalendar();
    if (btn.dataset.tab === "activity") renderActivity();
    if (btn.dataset.tab === "health") renderHealth();
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
document.getElementById("import-file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (file) await importMal(file);
  ev.target.value = ""; // lets you pick the same file again later
});
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

async function init() {
  restoreView();
  // Is server.py running? (On a static host this 404s and we stay read-only.)
  try {
    const res = await fetch("api/ping");
    canEdit = res.ok && (await res.json()).edit === true;
  } catch {
    canEdit = false;
  }
  document.getElementById("add-section").hidden = !canEdit;
  document.getElementById("import-section").hidden = !canEdit; // the import lives at the bottom of the list
  document.getElementById("refresh").hidden = !canEdit;

  await loadAll();
  renderList();
  await loadHistory();
  updateHealthDot();
}

init();
