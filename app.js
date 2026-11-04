// Manga Tracker, step 1: search AniList, save a list in the browser.
// No backend yet. Your list lives in localStorage on this device.

const API = "https://graphql.anilist.co";
const STORAGE_KEY = "manga-tracker:list";

// What we ask AniList for. Keep it small: AniList allows ~30 requests/minute.
const FIELDS = `
  id
  title { romaji english }
  status
  chapters
  coverImage { medium }
`;

const STATUS_LABEL = {
  RELEASING: "Releasing",
  HIATUS: "On break",
  FINISHED: "Finished",
  CANCELLED: "Cancelled",
  NOT_YET_RELEASED: "Not yet released",
};

// ---------- "On break?" detection ----------
// AniList keeps saying "Releasing" for series that are really paused, so we
// look at the date of the last chapter instead: if a Releasing series has had
// nothing new for N weeks, we flag it as "On break?". N is yours to tune.

const THRESHOLD_KEY = "manga-tracker:break-weeks";
const DEFAULT_BREAK_WEEKS = 6;

function getBreakWeeks() {
  const n = Number(localStorage.getItem(THRESHOLD_KEY));
  return n > 0 ? n : DEFAULT_BREAK_WEEKS;
}

function weeksSince(iso) {
  return Math.floor((Date.now() - new Date(iso).getTime()) / (7 * 24 * 3600 * 1000));
}

// Returns how many weeks it has been quiet, or 0 if it doesn't look on break.
function quietWeeks(m) {
  if (m.status !== "RELEASING" || !m.latest) return 0;
  const w = weeksSince(m.latest.publishedAt);
  return w >= getBreakWeeks() ? w : 0;
}

// ---------- Saved list (localStorage) ----------

function loadList() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || [];
  } catch {
    return [];
  }
}

function saveList(list) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
}

// ---------- AniList ----------

async function anilist(query, variables) {
  const res = await fetch(API, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  if (!res.ok) throw new Error(`AniList error ${res.status}`);
  const json = await res.json();
  if (json.errors) throw new Error(json.errors[0].message);
  return json.data;
}

// Latest chapter comes from MangaDex, through our own server.py
// (MangaDex refuses calls made directly from a web page).
// Returns {chapter, publishedAt, url} or null if unknown / server not running.
async function fetchLatest(m) {
  const params = new URLSearchParams({ al: m.id });
  if (m.title.english) params.append("title", m.title.english);
  if (m.title.romaji) params.append("alt", m.title.romaji);
  try {
    const res = await fetch(`/api/latest?${params}`);
    if (!res.ok) return null;
    const data = await res.json();
    return data.found && data.chapter ? data : null;
  } catch {
    return null;
  }
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

async function searchManga(text) {
  const data = await anilist(
    `query ($s: String) {
       Page(perPage: 8) {
         media(search: $s, type: MANGA, sort: SEARCH_MATCH) { ${FIELDS} }
       }
     }`,
    { s: text }
  );
  return data.Page.media;
}

async function fetchManyByIds(ids) {
  if (ids.length === 0) return [];
  const data = await anilist(
    `query ($ids: [Int]) {
       Page(perPage: 50) {
         media(id_in: $ids, type: MANGA) { ${FIELDS} }
       }
     }`,
    { ids }
  );
  return data.Page.media;
}

// ---------- Rendering ----------
// We build elements with textContent (not innerHTML) so a weird title can't inject HTML.

function displayTitle(m) {
  return m.title.english || m.title.romaji;
}

function makeCard(m, button) {
  const li = document.createElement("li");
  li.className = "card";

  const img = document.createElement("img");
  img.src = m.coverImage?.medium || "";
  img.alt = "";
  img.loading = "lazy";

  const info = document.createElement("div");
  info.className = "info";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = displayTitle(m);

  const sub = document.createElement("div");
  sub.className = "sub";
  if (m.latest) {
    // Link to the official reader when MangaDex knows one.
    const a = document.createElement("a");
    a.href = m.latest.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = `Ch. ${m.latest.chapter}`;
    sub.append(a, ` · ${formatDate(m.latest.publishedAt)}`);
  } else if (m.chapters) {
    sub.textContent = `${m.chapters} ${m.chapters === 1 ? "chapter" : "chapters"}`;
  } else {
    sub.textContent = "Ongoing";
  }

  const badge = document.createElement("span");
  const quiet = quietWeeks(m);
  if (quiet) {
    badge.className = "badge QUIET";
    badge.textContent = `On break? ${quiet} wks quiet`;
    badge.title = `AniList says Releasing, but the last chapter was ${quiet} weeks ago.`;
  } else {
    badge.className = `badge ${m.status}`;
    badge.textContent = STATUS_LABEL[m.status] || m.status;
  }

  info.append(title, sub, badge);
  li.append(img, info, button);
  return li;
}

function renderResults(items) {
  const ul = document.getElementById("results");
  const saved = new Set(loadList().map((m) => m.id));
  ul.replaceChildren(
    ...items.map((m) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = saved.has(m.id) ? "Added" : "Add";
      btn.disabled = saved.has(m.id);
      btn.addEventListener("click", () => {
        addToList(m);
        btn.textContent = "Added";
        btn.disabled = true;
      });
      return makeCard(m, btn);
    })
  );
}

function renderList() {
  const list = loadList();
  const ul = document.getElementById("mylist");
  document.getElementById("list-msg").textContent = list.length
    ? ""
    : "Nothing here yet. Search a manga above and press Add.";

  ul.replaceChildren(
    ...list.map((m) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost";
      btn.textContent = "Remove";
      btn.addEventListener("click", () => removeFromList(m.id));
      return makeCard(m, btn);
    })
  );
}

// ---------- Actions ----------

async function addToList(m) {
  const list = loadList();
  if (list.some((x) => x.id === m.id)) return;
  list.push(m);
  saveList(list);
  renderList();

  // Fetch the latest chapter in the background, then update the saved copy.
  const latest = await fetchLatest(m);
  if (!latest) return;
  saveList(loadList().map((x) => (x.id === m.id ? { ...x, latest } : x)));
  renderList();
}

function removeFromList(id) {
  saveList(loadList().filter((m) => m.id !== id));
  renderList();
  // Re-draw search results so the "Added" button resets if it's visible.
  if (lastResults.length) renderResults(lastResults);
}

async function refreshStatuses() {
  const btn = document.getElementById("refresh");
  const msg = document.getElementById("list-msg");
  const list = loadList();
  if (list.length === 0) return;

  btn.disabled = true;
  msg.textContent = "Checking AniList...";
  try {
    const fresh = await fetchManyByIds(list.map((m) => m.id));
    const byId = new Map(fresh.map((m) => [m.id, m]));
    const changes = [];

    // One at a time, so we stay polite to MangaDex's rate limit.
    const updated = [];
    for (const old of list) {
      const now = byId.get(old.id) || old;
      if (now.status !== old.status) {
        changes.push(`${displayTitle(now)}: ${STATUS_LABEL[old.status]} -> ${STATUS_LABEL[now.status]}`);
      }
      // If the lookup fails, keep what we knew before.
      const latest = (await fetchLatest(now)) || old.latest;
      const next = { ...now, latest };
      if (latest && old.latest && latest.chapter !== old.latest.chapter) {
        // A new chapter after a quiet stretch means it's back from break.
        changes.push(
          quietWeeks(old)
            ? `${displayTitle(now)}: back from break, chapter ${latest.chapter}`
            : `${displayTitle(now)}: new chapter ${latest.chapter}`
        );
      } else if (!quietWeeks(old) && quietWeeks(next)) {
        changes.push(`${displayTitle(now)}: ${quietWeeks(next)} weeks without a chapter, maybe on break`);
      }
      updated.push(next);
    }

    saveList(updated);
    renderList();
    msg.textContent = changes.length ? `Changed: ${changes.join("; ")}` : "No status changes.";
  } catch (err) {
    msg.textContent = `Could not refresh: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

// ---------- Search box ----------

let lastResults = [];
let timer;

document.getElementById("search").addEventListener("input", (e) => {
  clearTimeout(timer);
  const text = e.target.value.trim();
  const msg = document.getElementById("search-msg");

  if (text.length < 2) {
    lastResults = [];
    renderResults([]);
    msg.textContent = "";
    return;
  }

  // Wait 400 ms after the last keystroke so we don't spam the API.
  timer = setTimeout(async () => {
    msg.textContent = "Searching...";
    try {
      lastResults = await searchManga(text);
      msg.textContent = lastResults.length ? "" : "No results.";
      renderResults(lastResults);
    } catch (err) {
      msg.textContent = `Search failed: ${err.message}`;
    }
  }, 400);
});

document.getElementById("refresh").addEventListener("click", refreshStatuses);

// Threshold picker: how many quiet weeks before we call it "On break?".
const breakSelect = document.getElementById("break-weeks");
breakSelect.value = String(getBreakWeeks());
breakSelect.addEventListener("change", () => {
  localStorage.setItem(THRESHOLD_KEY, breakSelect.value);
  renderList();
});

renderList();
