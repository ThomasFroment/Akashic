"use strict";
// No framework, no build step. All text from the database goes through textContent / text nodes.

const HL_START = "\x02";
const HL_END = "\x03";
const PAGE = 30;

// ---- session: the tray opens /open?code=<one-time code>, which sets an HttpOnly cookie ------
// No secret ever lives in this page, the address bar or the browser history.
const REOPEN = "Open Akashic from its tray icon.";

// Theme: "system" follows Windows; "light" and "dark" are set in Settings and kept in config.toml.
// The last one is also remembered for this tab, so a reload does not flash the wrong colours.
function applyTheme(mode) {
  if (mode === "light" || mode === "dark") document.documentElement.dataset.theme = mode;
  else delete document.documentElement.dataset.theme;
  try { sessionStorage.setItem("akashic-theme", mode || "system"); } catch { /* storage blocked */ }
}
try { applyTheme(sessionStorage.getItem("akashic-theme") || "system"); } catch { /* storage blocked */ }

async function api(path, options = {}) {
  const res = await fetch(path, options);  // same origin: the session cookie goes along
  if (!res.ok) {
    const err = new Error("HTTP " + res.status);
    err.status = res.status;
    throw err;
  }
  return res;
}
const getJson = async (path) => (await api(path)).json();
// POST; without a body, no body and no Content-Type go along.
const sendJson = (path, body) => api(path, body === undefined ? { method: "POST" } : {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

// ---- small DOM helpers ----------------------------------------------------------------------
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v !== null && v !== undefined) el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c !== null && c !== undefined && c !== false) el.append(c);
  }
  return el;
}

// An icon from the <template id="icons"> of index.html.
const ICONS = document.getElementById("icons").content;
function icon(name, cls = "") {
  const found = ICONS.querySelector(`[data-icon="${name}"]`);
  const svg = found ? found.cloneNode(true) : document.createElement("span");
  svg.setAttribute("class", ("icon " + cls).trim());
  svg.setAttribute("aria-hidden", "true");
  svg.removeAttribute("data-icon");
  return svg;
}
for (const slot of document.querySelectorAll("[data-i]")) slot.replaceWith(icon(slot.dataset.i, slot.dataset.class || ""));

// A button with an icon and an accessible name; text is optional.
function iconButton(name, label, opts = {}) {
  const { text = "", cls = "ghost", onclick = null, disabled = false } = opts;
  return h("button", {
    type: "button", class: (text ? "" : "icon-btn ") + cls, "aria-label": text ? null : label, title: label,
    onclick, disabled,
  }, icon(name), text || null);
}

function helpIcon(message) {
  return h("span", { class: "help-icon", tabindex: "0", role: "note", "aria-label": message, "data-help": message }, "?");
}

// The header every page starts with: title, then any controls on the same line.
function pageHead(title, ...extras) {
  return h("div", { class: "page-head" }, h("h1", {}, title), ...extras);
}

function highlighted(snippet) {
  const out = [];
  let rest = snippet.replaceAll(/\s+/g, " ");
  while (rest) {
    const s = rest.indexOf(HL_START);
    if (s < 0) { out.push(rest); break; }
    const e = rest.indexOf(HL_END, s);
    if (e < 0) { out.push(rest.replace(HL_START, "")); break; }
    out.push(rest.slice(0, s), h("mark", {}, rest.slice(s + 1, e)));
    rest = rest.slice(e + 1);
  }
  return out;
}

const fmtTime = (ms) => new Date(ms).toLocaleString([], {
  weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit",
});
const fmtClock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const appName = (p) => (p || "").replace(/\.exe$/i, "");
// "Monitor N · name" by id, N counting left to right, then top to bottom: the order capture/screen_writer.py
// uses for the numbers in health problems.
const monitorLabels = (monitors) => new Map([...monitors]
  .sort((a, b) => a.rect[0] - b.rect[0] || a.rect[1] - b.rect[1])
  .map((m, i) => [m.id, m.name ? `Monitor ${i + 1} · ${m.name}` : `Monitor ${i + 1}`]));
const fmtAge = (ms) => (ms < 10000 ? "just now" : ms < 120000 ? Math.round(ms / 1000) + " s ago" : ms < 7200000 ? Math.round(ms / 60000) + " min ago" : Math.round(ms / 3600000) + " h ago");
// Whisper was unsure of this line (pipeline/transcribe.py UNCLEAR_BELOW): shown greyed out.
const UNCLEAR_BELOW = 0.55;
const unclearAttrs = (confidence) => (confidence !== null && confidence !== undefined && confidence < UNCLEAR_BELOW
  ? { class: "row-snippet unclear", title: "Low-confidence transcription" } : { class: "row-snippet" });
const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
// Meta line parts, separated by dots that never end up alone at the start of a line.
const metaLine = (...parts) => {
  const kept = parts.filter(Boolean);
  return h("div", { class: "row-meta" }, kept.flatMap((p, i) => (i ? [h("span", { class: "sep", "aria-hidden": "true" }), h("span", {}, p)] : [h("span", {}, p)])));
};

// ---- images: plain lazy <img>, the session cookie authorizes them -----------------------------
function lazyImg(cls, path, alt = "") {
  return h("img", { class: cls, alt, src: path, loading: "lazy", onerror: (e) => e.target.classList.add("none") });
}

function thumb(hit) {
  if (!hit.has_image) return h("div", { class: "thumb none" }, "no image");
  return lazyImg("thumb", `/api/frames/${hit.id}/image?window=${hit.window_id}&w=480`);
}

// ---- audio: one clip at a time --------------------------------------------------------------
// A play button shows a play or stop icon; a citation chip keeps its number and is highlighted.
function setPlaying(btn, on) {
  btn.classList.toggle("playing", on);
  if (!btn.classList.contains("cite")) btn.replaceChildren(icon(on ? "stop" : "play"));
  btn.setAttribute("aria-pressed", String(on));
}
function playButton(label = "Play", cls = "") {
  return h("button", { type: "button", class: ("play " + cls).trim(), "aria-label": label, title: label, "aria-pressed": "false" }, icon("play"));
}

// ---- views ----------------------------------------------------------------------------------
const view = document.getElementById("view");
const shell = document.getElementById("shell");
let cleanup = () => {};

// Focus and scroll do nothing while the shell is hidden (until recording is confirmed paused):
// run them now if it shows, else when it is revealed. A new route drops the old page's ones.
let onReveal = [];
function whenShown(fn) {
  if (shell.hidden) onReveal.push(fn);
  else fn();
}

function setActive(route) {
  for (const a of document.querySelectorAll(".nav-link")) {
    a.classList.toggle("active", a.dataset.route === route);
  }
  markRecent(null);
}

// In-app history, so "Back" returns to the page you came from (a conversation, a search…) and
// never leaves the app. Falls back to a sensible page when opened directly.
const visited = [];
function trackHistory() {
  const here = location.hash || "#/";
  if (visited.length > 1 && visited[visited.length - 2] === here) visited.pop();
  else if (visited[visited.length - 1] !== here) visited.push(here);
}
function goBack(fallback) {
  if (visited.length > 1) history.back();
  else goTo(fallback);
}
function backButton(fallback, label = "Back") {
  return iconButton("arrow-left", label, { text: label, cls: "ghost back", onclick: () => goBack(fallback) });
}

// The address of the timeline at one moment, optionally with a monitor enlarged and a window outlined.
function momentHref(ts, monitor = null, windowId = null) {
  const p = new URLSearchParams();
  if (monitor !== null) p.set("monitor", monitor);
  if (windowId !== null) p.set("window", windowId);
  const query = p.toString();
  return `#/moment/${ts}` + (query ? "?" + query : "");
}

function resultRow(hit) {
  return h("a", { class: "row", href: momentHref(hit.ts_ms, hit.monitor_id, hit.window_id) },
    thumb(hit),
    h("div", { class: "row-body" },
      metaLine(fmtTime(hit.ts_ms), hit.process ? appName(hit.process) : ""),
      h("div", { class: "row-title" }, hit.title || hit.url || "(untitled)"),
      hit.snippet ? h("div", { class: "row-snippet clamp" }, ...highlighted(hit.snippet)) : null));
}

function searchField(placeholder, label, value = "") {
  const input = h("input", { type: "search", placeholder, autocomplete: "off", "aria-label": label, value });
  return { input, el: h("div", { class: "searchbox" }, icon("search"), input) };
}

// Search: one page, two tabs. Screen = text and titles of screenshots; Speech = transcripts.
function searchView(apps, tab) {
  setActive("search");
  let chosen = tab;
  if (!chosen) {
    try { chosen = sessionStorage.getItem("akashic-search-tab") || "screen"; } catch { chosen = "screen"; }
  }
  try { sessionStorage.setItem("akashic-search-tab", chosen); } catch { /* storage blocked */ }
  const tabs = h("div", { class: "tabs", role: "tablist" },
    [["screen", "On screen"], ["speech", "Speech"]].map(([key, label]) =>
      h("a", { role: "tab", href: `#/search?tab=${key}`, class: key === chosen ? "active" : "", "aria-selected": key === chosen }, label)));
  const body = h("div");
  view.replaceChildren(h("div", { class: "column" }, pageHead("Search"), tabs, body));
  if (chosen === "speech") speechSearch(body);
  else screenSearch(body, apps, lastSearch);
}

const WORDS_ONLY = "Use Ask for questions in your own words: it searches by meaning across episodes and speech.";

function screenSearch(root, apps, saved) {
  const state = saved || { q: "", process: "", from: "", to: "" };
  let offset = 0;
  let timer = 0;
  let seq = 0;

  const { input, el: box } = searchField("Search what you have seen…", "Search screen text", state.q);
  const appSel = h("select", { "aria-label": "App" },
    h("option", { value: "" }, "All apps"), apps.map((a) => h("option", { value: a }, appName(a))));
  appSel.value = state.process;
  const from = h("input", { type: "date", "aria-label": "From", value: state.from });
  const to = h("input", { type: "date", "aria-label": "To", value: state.to });
  const results = h("div", { class: "results", "aria-live": "polite" });
  const more = h("button", { class: "more", type: "button", hidden: true }, "Show more");
  root.replaceChildren(box,
    h("div", { class: "filters" }, appSel, h("label", {}, "From", from), h("label", {}, "To", to)),
    h("div", { class: "search-hint muted small" }, "Exact words in screen text, window titles and addresses.", helpIcon(WORDS_ONLY)),
    results, more);
  whenShown(() => input.focus());

  async function run(append) {
    const mine = ++seq;
    if (!append) offset = 0;
    const p = new URLSearchParams({ q: input.value, limit: PAGE, offset });
    if (appSel.value) p.set("process", appSel.value);
    if (from.value) p.set("start_ms", new Date(from.value + "T00:00:00").getTime());
    if (to.value) p.set("end_ms", new Date(to.value + "T00:00:00").getTime() + 86400000);
    let hits;
    try { hits = await getJson("/api/search?" + p); } catch { return; }
    if (mine !== seq) return;
    if (!append) results.replaceChildren();
    if (!hits.length && !append) {
      results.append(h("div", { class: "empty" }, input.value ? "Nothing matches." : "Nothing captured yet."));
    }
    for (const hit of hits) results.append(resultRow(hit));
    offset += hits.length;
    more.hidden = hits.length < PAGE;
    lastSearch = { q: input.value, process: appSel.value, from: from.value, to: to.value };
  }
  const schedule = () => { clearTimeout(timer); timer = setTimeout(() => run(false), 250); };
  input.addEventListener("input", schedule);
  for (const el of [appSel, from, to]) el.addEventListener("change", () => run(false));
  more.addEventListener("click", () => run(true));
  cleanup = () => clearTimeout(timer);
  run(false);
}
let lastSearch = null;

// Speech: what was said, searchable, each with its recording to play back.
function speechSearch(root) {
  const { input, el: box } = searchField("Search what was said…", "Search transcripts");
  const date = h("input", { type: "date", "aria-label": "Day" });
  const results = h("div", { class: "results", "aria-live": "polite" });
  const more = h("button", { class: "more", type: "button", hidden: true }, "Show more");
  root.replaceChildren(box,
    h("div", { class: "filters" }, h("label", {}, "Day", date)),
    h("div", { class: "search-hint muted small" }, "Exact words in speech transcripts.", helpIcon(WORDS_ONLY)),
    results, more);
  whenShown(() => input.focus());
  let offset = 0;
  let seq = 0;
  let timer = 0;

  function row(u) {
    const btn = playButton();
    btn.addEventListener("click", () => playClip(btn, u.id, 0, Number.MAX_SAFE_INTEGER));
    const who = u.speaker || (u.stream === "mic" ? "You" : "Heard");
    const via = u.sources.length ? u.sources.map(appName).join(", ") : "";
    return h("div", { class: "row" }, btn,
      h("div", { class: "row-body" },
        metaLine(fmtTime(u.start_ms), who, u.tag, u.language || "", via),
        h("div", unclearAttrs(u.confidence), ...highlighted(u.snippet)),
        h("a", { class: "small muted", href: `#/settings?tab=models&segment=${u.id}` }, "Compare speech models on this clip")));
  }

  async function run(append) {
    const mine = ++seq;
    if (!append) offset = 0;
    const p = new URLSearchParams({ q: input.value, limit: PAGE, offset });
    if (date.value) {
      const start = new Date(date.value + "T00:00:00").getTime();
      p.set("start_ms", start);
      p.set("end_ms", start + 86400000);
    }
    let found;
    try { found = await getJson("/api/transcripts?" + p); } catch { return; }
    if (mine !== seq) return;
    if (!append) results.replaceChildren();
    if (!found.length && !append) {
      results.append(h("div", { class: "empty" }, input.value ? "Nothing matches." : "No transcripts yet."));
    }
    for (const u of found) results.append(row(u));
    offset += found.length;
    more.hidden = found.length < PAGE;
  }
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => run(false), 250); });
  date.addEventListener("change", () => run(false));
  more.addEventListener("click", () => run(true));
  cleanup = () => { clearTimeout(timer); stopClip(); };
  run(false);
}

const TEXT_SOURCE_LABELS = {
  uia: "app (UI Automation)",
  ocr: "screen reading (OCR)",
  ocr_deferred: "screen reading after the game (OCR)",
};

async function frameView(id) {
  setActive("timeline");
  let f;
  try { f = await getJson(`/api/frames/${id}`); } catch {
    view.replaceChildren(h("div", { class: "column empty" }, "That moment no longer exists."));
    return;
  }
  // Earlier / Later replace this moment in the history, so Back still leaves the viewer at once.
  const go = (target) => {
    if (!target) return;
    visited.pop();
    location.replace(`#/frame/${target}`);
  };
  const fallback = momentHref(f.ts_ms, f.monitor_id);
  const shot = f.has_image
    ? lazyImg("shot", `/api/frames/${f.id}/image`, "Screenshot")
    : h("div", { class: "noshot" }, "No screenshot was kept for this moment.");
  const row = (k, v) => (v ? [h("dt", {}, k), h("dd", {}, v)] : []);
  const windowBlock = (w) => h("section", { class: "window" },
    h("dl", { class: "meta" },
      row("App", appName(w.process)), row("Window", w.title), row("Address", w.url),
      row("Text from", TEXT_SOURCE_LABELS[w.text_source])),
    w.text ? h("div", { class: "text-block" }, w.text) : h("div", { class: "muted" }, "No text was read"));
  view.replaceChildren(h("div", { class: "column wide" },
    h("div", { class: "viewer-bar" },
      backButton(fallback),
      h("div", { class: "spacer" }),
      h("span", { class: "muted small" }, fmtTime(f.ts_ms)),
      iconButton("chev-left", "Earlier moment (←)", { cls: "", disabled: !f.prev_id, onclick: () => go(f.prev_id) }),
      iconButton("chev-right", "Later moment (→)", { cls: "", disabled: !f.next_id, onclick: () => go(f.next_id) })),
    shot,
    h("dl", { class: "meta" }, row("When", fmtTime(f.ts_ms))),
    ...f.windows.filter((w) => w.role === "app").map(windowBlock)));
  view.focus();
  const onKey = (e) => {
    if (e.target.matches("input, select, textarea")) return;
    if (e.key === "ArrowLeft") go(f.prev_id);
    else if (e.key === "ArrowRight") go(f.next_id);
  };
  document.addEventListener("keydown", onKey);
  cleanup = () => document.removeEventListener("keydown", onKey);
}

// ---- routing --------------------------------------------------------------------------------
async function route() {
  cleanup();
  cleanup = () => {};
  onReveal = [];
  trackHistory();
  shell.classList.remove("nav-open");
  view.classList.remove("chat-mode");
  view.scrollTop = 0;
  const hash = location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = hash.split("?");
  const params = new URLSearchParams(query);
  const mine = ++routeSeq;
  const frame = /^\/frame\/(\d+)$/.exec(path);
  const episode = /^\/episode\/(\d+)$/.exec(path);
  const chat = /^\/ask\/(\d+)$/.exec(path);
  const moment = /^\/moment\/(\d+)$/.exec(path);
  const appList = () => getJson("/api/apps").catch(() => []);  // the filters only; empty if unreadable
  if (!signedIn) {
    view.replaceChildren(h("div", { class: "column empty" }, REOPEN));
    return;
  }
  if (frame) await frameView(Number(frame[1]));
  else if (moment) {
    const apps = await appList();
    if (mine !== routeSeq) return;
    timelineView(apps, Number(moment[1]), params.get("monitor"), params.get("window"));
  }
  else if (path === "/timeline") {
    // Recording is paused while this page shows, so "now" is never recorded: open on the latest moment.
    const [apps, latest] = await Promise.all([appList(), getJson(`/api/stage?ts=${Date.now()}`).catch(() => ({}))]);
    if (mine !== routeSeq) return;
    timelineView(apps, latest.prev_ms ?? Date.now(), null, null);
  }
  else if (path === "/search") {
    const apps = await appList();
    if (mine !== routeSeq) return;
    searchView(apps, params.get("tab"));
  }
  else if (path === "/speakers") speakersView();
  else if (episode) await episodeView(Number(episode[1]));
  else if (chat) await chatView(Number(chat[1]));
  else if (path === "/settings") settingsView(params.get("tab"));
  else memoryView();
}

let routeSeq = 0;

// ---- mobile menu ----------------------------------------------------------------------------
const menuBtn = document.getElementById("menu-btn");
menuBtn.addEventListener("click", () => {
  const open = shell.classList.toggle("nav-open");
  menuBtn.setAttribute("aria-expanded", String(open));
});
document.getElementById("scrim").addEventListener("click", () => shell.classList.remove("nav-open"));

// ---- status in the sidebar ------------------------------------------------------------------
const dot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const statusCount = document.getElementById("status-count");
const pauseBtn = document.getElementById("pause-btn");
let paused = false;
const STATUS_LABELS = {
  ready: "Recording", gaming: "Recording (game mode)", degraded: "Recording (with problems)",
  starting: "Starting…", crashed: "Restarting…", paused: "Paused",
  ui_paused: "Not recording while Akashic is open",
};
const DOT_CLASSES = { ready: "on", gaming: "on", degraded: "warn", paused: "paused" };
const PROBLEM_WORDS = {
  audio_unavailable: "audio recording could not start",
  mic_unavailable: "the microphone could not be read for a minute",
  system_audio_unavailable: "system audio could not be read for a minute",
  mic_segments_dropped: "some microphone speech could not be saved in the last minute",
  system_segments_dropped: "some system audio could not be saved in the last minute",
  voice_elsewhere: "voice chat played in the last minute on an audio device that is not recorded",
  screen_images_failing: (where) => `screenshots are failing${where}`,
  screen_frames_dropped: (where) => `screenshots${where} are being dropped because saving cannot keep up`,
  screen_disk_full: (where) => `screenshots${where} can't be saved because the disk is full`,
  window_events_down: "window changes are not being detected, so screenshots may come late and some browser windows may stay blank",
};
// A screen problem arrives as "<code>@<monitor label>".
function problemWords(problem) {
  const [code, monitor] = problem.split(/@(.*)/s);
  const words = PROBLEM_WORDS[code];
  return typeof words === "function" ? words(monitor ? ` on ${monitor}` : "") : words;
}
function problemSentence(codes) {
  const words = (codes || []).map(problemWords).filter(Boolean);
  const what = words.length ? words.join("; ") : "part of the recording is not working";
  return `Recording has problems: ${what}. The Debug page has details.`;
}

const processingLineEl = document.getElementById("processing-line");
const processingText = document.getElementById("processing-text");
const KIND_WORDS = {
  transcribe: "transcription", speakers: "speaker recognition", embed: "search indexing",
  embed_query: "search", ask: "answering questions", ocr_window: "text recognition",
  stt_compare: "speech comparison", retention: "cleanup",
};
const recordings = (n) => `${n} recording${n === 1 ? "" : "s"}`;

// The processing problem to show under the capture status, as {text, detail}, or null.
function processingLine(s) {
  const p = s.processing;
  const failed = p?.failed.transcribe ?? 0;
  const failedSentence = failed ? ` ${recordings(failed)} could not be transcribed.` : "";
  const more = " The Debug page has details.";
  if (s.attention === "processing_stopped") {
    return { text: "Processing stopped", detail: `The processing worker is not running.${failedSentence}${more}` };
  }
  if (s.attention === "needs_attention") {
    if (p.blocked.length) {
      const what = p.blocked.map((k) => KIND_WORDS[k] ?? k).join(", ");
      return { text: "Needs attention", detail: `${what[0].toUpperCase()}${what.slice(1)} can't run: something needed is missing. See Settings › Models.${failedSentence}${more}` };
    }
    return { text: "Needs attention", detail: `Transcription made no progress for over an hour.${failedSentence}${more}` };
  }
  if (failed) return { text: `${recordings(failed)} could not be transcribed`, detail: `${failedSentence.trim()}${more}` };
  return null;
}

let signedIn = true;  // until the server says otherwise (401)
async function refreshStatus() {
  try {
    const s = await getJson("/api/status");
    const wasSignedOut = !signedIn;
    signedIn = true;
    paused = s.paused;
    applyTheme(s.theme);
    const capture = s.processes.capture;
    dot.className = "dot " + (DOT_CLASSES[capture] ?? "");
    statusText.textContent = STATUS_LABELS[capture] ?? "Stopped";
    const gameReason = s.game_process ? `Game mode is on because ${appName(s.game_process)} is a fullscreen window. Check Settings › Capture to adjust game detection.`
      : "Game mode is on because a fullscreen window was detected. Check Settings › Capture to adjust game detection.";
    const detail = capture === "gaming" ? gameReason : capture === "degraded" ? problemSentence(s.capture_problems) : "";
    statusText.title = detail;
    statusText.setAttribute("aria-label", detail || statusText.textContent);
    const line = processingLine(s);
    processingLineEl.hidden = !line;
    if (line) {
      processingText.textContent = line.text;
      processingText.title = line.detail;
      processingText.setAttribute("aria-label", `${line.text}. ${line.detail}`);
    }
    statusCount.textContent = `${s.frames.toLocaleString()} moments` + (s.processes.capture === "gaming" ? ` · ${s.game_process ? appName(s.game_process) : "fullscreen app"} detected` : "");
    pauseBtn.hidden = false;
    pauseBtn.textContent = paused ? "Resume" : "Pause";
    if (wasSignedOut) { loadRecents(); route(); }
  } catch (e) {
    dot.className = "dot";
    processingLineEl.hidden = true;
    if (e.status === 401) signedIn = false;
    statusText.textContent = e.status === 401 ? "Reopen from the tray" : "Not reachable";
    pauseBtn.hidden = true;
  }
}
pauseBtn.addEventListener("click", async () => {
  await sendJson("/api/pause?paused=" + !paused).catch(() => {});
  setTimeout(refreshStatus, 400);
});

window.addEventListener("hashchange", route);
// A link to the page already shown starts it afresh (e.g. "New question" from an unsaved chat).
document.addEventListener("click", (e) => {
  const link = e.target.closest("a[href^='#']");
  if (!link || e.ctrlKey || e.metaKey || e.shiftKey) return;
  if ((location.hash || "#/") === link.getAttribute("href")) { e.preventDefault(); route(); }
});
function goTo(hash) {
  if ((location.hash || "#/") === hash) route();
  else location.hash = hash;
}
window.addEventListener("DOMContentLoaded", () => {
  refreshStatus().then(() => { loadRecents(); route(); });
});
setInterval(refreshStatus, 5000);

// ---- presence: recording stops while this page is visible ------------------------------------
// The page shows nothing recorded until the server confirms that recording is paused. It says
// so every 2 s while visible (every 250 ms until confirmed); the server drops it after 6 s.
// Renewed at every hide, so a late reply cannot affect the next visible period.
let pageId = crypto.randomUUID();
const CONFIRMED_EVERY_MS = 2000;
const UNCONFIRMED_EVERY_MS = 250;
const waitNote = document.getElementById("wait-note");
let confirmed = false;
let presenceTimer = 0;

function postPresence(visible, keepalive) {
  return api("/api/presence", {
    method: "POST", headers: { "Content-Type": "application/json" }, keepalive,
    body: JSON.stringify({ page: pageId, visible }),
  });
}

function conceal() {
  confirmed = false;
  shell.hidden = true;
  waitNote.hidden = false;
  stopClip();
  for (const el of document.querySelectorAll("audio")) el.pause();
}

async function reportPresence() {
  clearTimeout(presenceTimer);
  if (document.hidden) return;
  const id = pageId;
  let next = UNCONFIRMED_EVERY_MS;
  try {
    const { recording_paused } = await (await postPresence(true, false)).json();
    if (id !== pageId) return;
    if (recording_paused) {
      if (shell.hidden) refreshStatus();  // the footer says at once that recording is paused
      confirmed = true;
      shell.hidden = false;
      waitNote.hidden = true;
      for (const fn of onReveal.splice(0)) fn();
      next = CONFIRMED_EVERY_MS;
    } else if (confirmed) {
      conceal();
    }
  } catch (e) {
    if (id !== pageId) return;
    conceal();
    if (e.status === 401) { waitNote.textContent = REOPEN; return; }
  }
  presenceTimer = setTimeout(reportPresence, next);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) { reportPresence(); return; }
  clearTimeout(presenceTimer);
  conceal();
  postPresence(false, true).catch(() => {});
  pageId = crypto.randomUUID();
});
window.addEventListener("DOMContentLoaded", reportPresence);
