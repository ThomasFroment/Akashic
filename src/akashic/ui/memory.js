"use strict";
// Ask: questions in your own words, answered from your memory with links to the moments, as a
// conversation you can follow up on. Conversations are listed in the sidebar (Recents). Also the
// episode page. Uses the helpers of app.js and the audio player of speakers.js. Everything from
// the database goes in as text, never as markup.

const KIND_LABEL = {
  video: "Video", music: "Music", conversation: "Conversation", browsing: "Browsing", work: "Work", game: "Game",
};
const kindLabel = (k) => KIND_LABEL[k] || k;

const fmtDuration = (ms) => {
  const min = Math.max(1, Math.round(ms / 60000));
  return min >= 60 ? `${Math.floor(min / 60)} h ${String(min % 60).padStart(2, "0")} min` : `${min} min`;
};

// The same square badge for every kind, so the rows line up whatever the kind is called.
const kindBadge = (kind) => h("span", { class: `kind k-${kind}`, title: kindLabel(kind) }, icon("k-" + kind));
const kindChip = (kind) => h("span", { class: `chip kindchip k-${kind}` }, kindLabel(kind));
const people = (e) => [...e.people, ...(e.unnamed ? [`${e.unnamed} unknown voice${e.unnamed > 1 ? "s" : ""}`] : [])];

function episodeRow(e) {
  const who = people(e);
  return h("a", { class: "row episode", href: `#/episode/${e.id}` },
    kindBadge(e.kind),
    h("div", { class: "row-body" },
      h("div", { class: "row-title" }, e.title || "(untitled)"),
      metaLine(kindLabel(e.kind), `${fmtClock(e.start_ms)} · ${fmtDuration(e.end_ms - e.start_ms)}`,
        e.app ? appName(e.app) : "", who.join(", ")),
      e.snippet ? h("div", { class: "row-snippet clamp" }, e.snippet) : null));
}

function lineRow(line) {
  const btn = playButton();
  btn.addEventListener("click", () => playClip(btn, line.segment_id, 0, Number.MAX_SAFE_INTEGER));
  const who = line.speaker || (line.stream === "mic" ? "You" : "Heard");
  const link = line.episode_id ? h("a", { href: `#/episode/${line.episode_id}` }, "open episode") : "";
  return h("div", { class: "row" }, btn,
    h("div", { class: "row-body" }, metaLine(fmtTime(line.start_ms), who, link), h("div", unclearAttrs(line.confidence), line.text)));
}

function section(title, rows) {
  return rows.length ? h("section", { class: "block" }, h("h2", {}, title), h("div", { class: "results" }, rows)) : null;
}

// What the question was understood as, so a wrong guess is visible and can be rephrased.
function understood(plan, semantic) {
  const chips = [];
  if (plan.time) chips.push(h("span", { class: "chip plain" }, `${fmtTime(plan.time.start_ms)} → ${fmtTime(plan.time.end_ms)}`));
  for (const p of plan.people) chips.push(h("span", { class: "chip plain" }, p));
  for (const k of plan.kinds) chips.push(kindChip(k));
  for (const w of plan.keywords) chips.push(h("span", { class: "chip plain" }, w));
  if (!chips.length) chips.push(h("span", { class: "muted small" }, "any time, anyone, anything"));
  return h("div", { class: "understood" }, ...chips,
    semantic ? null : h("span", { class: "muted small" }, "· by words and filters only (meaning search is not ready)"));
}

// ---- answers: prose with [n] markers turned into chips that open the moment ----------------
const STEP_LABEL = { searching: "Searching your memory…", planning: "Understanding the question…", answering: "Writing the answer…" };

// A chip for source n: frames open the screenshot, episodes their page, a spoken line plays.
function citeChip(source) {
  const label = String(source.n);
  const title = `${source.label} · ${fmtTime(source.start_ms)}`;
  if (source.kind === "line") {
    const btn = h("button", { type: "button", class: "cite", title: "Play: " + title, "aria-label": `Source ${label}, play: ${title}` }, label);
    btn.addEventListener("click", () => playClip(btn, source.id, 0, Number.MAX_SAFE_INTEGER));
    return btn;
  }
  const href = source.kind === "frame" ? `#/frame/${source.id}?from=memory` : `#/episode/${source.id}`;
  return h("a", { class: "cite", href, title, "aria-label": `Source ${label}: ${title}` }, label);
}

// The answer as text nodes and chips. Anything that is not [n] stays text.
function answerBody(text, sources) {
  const byNumber = new Map((sources || []).map((s) => [s.n, s]));
  const parts = [];
  let last = 0;
  for (const m of text.matchAll(/\[(\d+)\]/g)) {
    parts.push(text.slice(last, m.index));
    const source = byNumber.get(Number(m[1]));
    parts.push(source ? citeChip(source) : m[0]);
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}

function sourceRow(s) {
  return h("div", { class: "row source-row" }, citeChip(s),
    h("div", { class: "row-body" }, metaLine(s.label, fmtTime(s.start_ms)),
      s.text ? h("div", { class: "row-snippet clamp" }, s.text) : null),
    s.kind === "frame" ? lazyImg("thumb", `/api/frames/${s.id}/image?w=320`) : null);
}

// The plain matches for a question (no model): episodes, lines, screen moments.
async function relatedMoments(text) {
  let found;
  try { found = await getJson("/api/memory/search?" + new URLSearchParams({ q: text.slice(0, 500) })); } catch {
    return [h("div", { class: "muted small" }, "Search failed.")];
  }
  const parts = [
    section("Episodes", found.episodes.map(episodeRow)),
    section("What was said", found.lines.map(lineRow)),
    section("On screen", found.frames.map((f) => resultRow(f, "memory"))),
  ].filter(Boolean);
  return parts.length ? parts : [h("div", { class: "muted small" }, "Nothing matches that.")];
}

// ---- the composer: a growing text box, Enter sends, Shift+Enter is a new line ----------------
function composer(placeholder, onSubmit) {
  const box = h("textarea", { rows: 1, placeholder, "aria-label": "Ask about your memory", maxlength: "500", autocomplete: "off" });
  const send = h("button", { type: "button", class: "send", "aria-label": "Send", title: "Send (Enter)", disabled: true }, icon("arrow-up"));
  const hint = h("span", { class: "hint" }, "Answers come only from what Akashic recorded on this PC.");
  let busy = false;
  const grow = () => { box.style.height = "auto"; box.style.height = Math.min(box.scrollHeight, 240) + "px"; };
  const sync = () => { send.disabled = busy || !box.value.trim(); };
  const submit = () => {
    const q = box.value.trim();
    if (!q || busy) return;
    box.value = "";
    grow();
    sync();
    onSubmit(q);
  };
  box.addEventListener("input", () => { grow(); sync(); });
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  send.addEventListener("click", submit);
  const el = h("div", { class: "composer" }, box, h("div", { class: "composer-foot" }, hint, send));
  return {
    el,
    focus: () => box.focus(),
    fill: (text) => { box.value = text; grow(); sync(); box.focus(); },
    setBusy(on, why = "") {
      busy = on;
      hint.textContent = on ? why : "Answers come only from what Akashic recorded on this PC.";
      sync();
    },
  };
}

function modelsBanner(where) {
  getJson("/api/models").then((rows) => {
    if (rows.some((r) => r.status !== "ready")) {
      where.before(h("div", { class: "banner" }, "Some models are not downloaded yet, so written answers or meaning search may not work.",
        h("a", { href: "#/settings?tab=models" }, "Settings › Models")));
    }
  }).catch(() => {});
}

// ---- Recents in the sidebar ----------------------------------------------------------------
const recentsBox = document.getElementById("recents");
let currentThread = null;

function markRecent(id) {
  currentThread = id;
  for (const r of recentsBox.querySelectorAll(".recent")) r.classList.toggle("active", Number(r.dataset.id) === id);
}

async function loadRecents() {
  let list;
  try { list = await getJson("/api/ask?limit=50"); } catch { return; }
  if (!list.length) {
    recentsBox.replaceChildren(h("div", { class: "recents-empty" }, "Your questions appear here."));
    return;
  }
  recentsBox.replaceChildren(...list.map((q) => {
    const forget = iconButton("x", "Forget this conversation", { cls: "ghost forget" });
    forget.addEventListener("click", () => forgetThread(q.id, q.text));
    return h("div", { class: "recent" + (q.id === currentThread ? " active" : ""), "data-id": q.id },
      h("a", { href: `#/ask/${q.id}`, title: q.text }, q.text), forget);
  }));
}

async function forgetThread(id, text) {
  if (!confirm(`Forget the conversation "${text}"? The questions and answers are deleted; your recordings are not.`)) return;
  try { await api(`/api/ask/${id}`, { method: "DELETE" }); } catch { return; }
  await loadRecents();
  if (currentThread === id) goTo("#/");
}

// ---- Ask home: greeting, composer, today's episodes -----------------------------------------
function greetingText() {
  const hour = new Date().getHours();
  return hour < 5 ? "Up late?" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

const SUGGESTIONS = [
  "What video did I watch yesterday afternoon?",
  "What did I talk about today?",
  "What was I working on this morning?",
];

function memoryView() {
  setActive("ask");
  const box = composer("What do you want to remember?", (q) => start(q));
  const below = h("div");
  const home = h("div", { class: "ask-home" },
    h("h1", { class: "greeting" }, h("span", { class: "brand-mark", "aria-hidden": "true" }), greetingText()),
    box.el,
    h("div", { class: "suggestions" }, SUGGESTIONS.map((s) => h("button", { type: "button", onclick: () => box.fill(s) }, s))),
    below);
  view.replaceChildren(home);
  box.focus();
  modelsBanner(box.el);
  let alive = true;
  cleanup = () => { alive = false; stopClip(); };

  async function start(q) {
    box.setBusy(true, "Asking…");
    let res;
    try { res = await (await sendJson("/api/ask", { q })).json(); } catch { res = { id: null, blocked: "the app could not be reached" }; }
    if (!alive) return;
    if (res.id) {
      loadRecents();
      location.hash = `#/ask/${res.thread || res.id}`;
      return;
    }
    // No conversation was stored (a greeting, or no model yet): show it here, as a chat.
    cleanup();
    mountChat(null, [{ id: null, text: q, state: "local", answer: res.answer || "", blocked: res.blocked }]);
  }

  (async () => {
    const begin = new Date(); begin.setHours(0, 0, 0, 0);
    let list;
    try { list = await getJson(`/api/episodes?start_ms=${begin.getTime()}&end_ms=${begin.getTime() + 86400000}`); } catch { return; }
    if (!alive) return;
    below.replaceChildren(list.length
      ? section("Today", list.slice().reverse().map(episodeRow))
      : h("div", { class: "empty" }, "No episodes yet today. They appear a few minutes after something ends."));
  })();
}

// ---- a conversation -------------------------------------------------------------------------
async function chatView(id) {
  setActive(null);
  markRecent(id);
  let turns;
  try { turns = await getJson(`/api/ask/thread/${id}`); } catch {
    view.replaceChildren(h("div", { class: "column empty" }, "That conversation no longer exists. ", h("a", { href: "#/" }, "Ask something new")));
    return;
  }
  if (turns.length && turns[0].id !== id) { location.replace(`#/ask/${turns[0].id}`); return; }
  mountChat(id, turns);
}

function mountChat(threadId, initial) {
  view.classList.add("chat-mode");
  const list = h("div", { class: "turns-list", "aria-live": "polite" });
  const box = composer("Ask a follow-up…", (q) => ask(q));
  const title = initial[0] ? initial[0].text : "New question";
  const head = h("div", { class: "chat-head" },
    iconButton("arrow-left", "Back", { onclick: () => goBack("#/") }),
    h("h1", { title }, title),
    threadId ? iconButton("trash", "Forget this conversation", { onclick: () => forgetThread(threadId, title) }) : null,
    iconButton("plus", "New question", { onclick: () => goTo("#/") }));
  view.replaceChildren(h("div", { class: "chat" }, head, list, h("div", { class: "chat-composer" }, box.el)));
  let alive = true;
  const timers = new Set();
  cleanup = () => { alive = false; for (const t of timers) clearTimeout(t); stopClip(); };

  const all = [];  // { q, el, render }
  const nearBottom = () => view.scrollHeight - view.scrollTop - view.clientHeight < 140;
  const toBottom = () => { view.scrollTop = view.scrollHeight; };
  const working = (q) => q.state === "queued" || q.state === "working";
  const refreshBusy = () => box.setBusy(all.some((t) => working(t.q)), "Wait for the answer to finish before following up.");

  function addTurn(q) {
    const index = all.length;
    // A follow-up alone ("and after that?") matches little: its related moments also use the question before it.
    const context = () => (index > 0 ? all[index - 1].q.text + " " : "") + q.text;
    const turn = { q, el: null, render: null };
    const bot = h("div", { class: "bot-msg" });
    turn.el = h("div", { class: "turn" }, h("div", { class: "user-msg" }, q.text), bot);
    const open = new Set();  // panels left open across re-renders
    turn.render = () => renderBot(bot, turn.q, open, context);
    turn.render();
    all.push(turn);
    list.append(turn.el);
    if (working(q)) follow(turn);
    return turn;
  }

  function follow(turn) {
    const step = async () => {
      let q;
      try { q = await getJson(`/api/ask/${turn.q.id}`); } catch { return; }
      if (!alive) return;
      const stick = nearBottom();
      turn.q = q;
      turn.render();
      if (stick) toBottom();
      if (working(q)) { const t = setTimeout(step, 400); timers.add(t); } else refreshBusy();
    };
    const t = setTimeout(step, 300);
    timers.add(t);
  }

  async function ask(q) {
    box.setBusy(true, "Asking…");
    let res;
    try { res = await (await sendJson("/api/ask", threadId ? { q, thread: threadId } : { q })).json(); } catch {
      res = { id: null, blocked: "the app could not be reached" };
    }
    if (!alive) return;
    if (res.id && !threadId) {  // the first stored question of what was a local chat
      loadRecents();
      location.hash = `#/ask/${res.thread || res.id}`;
      return;
    }
    if (res.id) {
      addTurn({ id: res.id, text: q, state: "queued", answer: "", sources: [] });
      loadRecents();
    } else {
      addTurn({ id: null, text: q, state: "local", answer: res.answer || "", blocked: res.blocked });
    }
    toBottom();
    refreshBusy();
  }

  for (const q of initial) addTurn(q);
  refreshBusy();
  requestAnimationFrame(toBottom);
  box.focus();
}

// One answer: progress while it is written, then the text, its sources and the tools around it.
function renderBot(bot, q, open, context) {
  const parts = [];
  const live = q.state === "queued" || q.state === "working";
  if (live && !q.answer) {
    parts.push(h("div", { class: "step" }, h("span", { class: "spinner" }),
      q.state === "queued" ? "Waiting for the model…" : STEP_LABEL[q.step] || "Working…"));
  }
  if (q.answer) parts.push(h("p", { class: "answer-text" + (live ? " caret" : "") }, ...answerBody(q.answer, q.sources)));
  if (q.state === "failed") parts.push(h("div", { class: "notice" }, "The model could not answer this one. The related moments below may still help."));
  if (q.state === "local" && q.blocked) {
    parts.push(h("div", { class: "notice" }, `No written answer: ${q.blocked}. Plain matches are shown below. `,
      h("a", { href: "#/settings?tab=models" }, "Settings › Models")));
  }
  if (live) { bot.replaceChildren(...parts); return; }

  const greeting = q.state === "local" && !q.blocked;
  if (greeting) { bot.replaceChildren(...parts); return; }
  if (q.state === "failed" || q.state === "local") open.add("related");

  const cited = new Set([...String(q.answer || "").matchAll(/\[(\d+)\]/g)].map((m) => Number(m[1])));
  const used = (q.sources || []).filter((s) => cited.has(s.n));
  const panels = h("div", { class: "disclosure" });
  const buttons = [];
  const panel = (key, iconName, label, fill) => {
    const btn = iconButton(iconName, label, { text: label });
    btn.setAttribute("aria-expanded", String(open.has(key)));
    const box = h("div", { class: "disclosure", hidden: !open.has(key) });
    let filled = false;
    const show = async (user = false) => {
      box.hidden = !open.has(key);
      btn.setAttribute("aria-expanded", String(open.has(key)));
      if (open.has(key) && !filled) { filled = true; box.replaceChildren(h("div", { class: "step" }, h("span", { class: "spinner" }), "Loading…")); box.replaceChildren(...await fill()); }
      if (open.has(key) && user) box.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    btn.addEventListener("click", () => { if (open.has(key)) open.delete(key); else open.add(key); show(true); });
    buttons.push(btn);
    panels.append(box);
    show();
  };
  if (used.length) panel("sources", "list", `Sources (${used.length})`, async () => [h("div", { class: "results" }, used.map(sourceRow))]);
  panel("related", "layers", "Related moments", () => relatedMoments(context()));
  if (q.plan) panel("plan", "filter", "How I searched", async () => [understood(q.plan, q.plan.semantic !== false)]);
  if (q.answer) {
    const copy = iconButton("copy", "Copy the answer");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(q.answer.replaceAll(/\s*\[\d+\]/g, "")); } catch { return; }
      copy.replaceChildren(icon("check"));
      setTimeout(() => copy.replaceChildren(icon("copy")), 1500);
    });
    buttons.push(copy);
  }
  bot.replaceChildren(...parts,
    h("div", { class: "bot-actions" }, ...buttons, q.model ? h("span", { class: "model", title: "Written on this PC" }, q.model) : null),
    panels);
}

// ---- an episode -----------------------------------------------------------------------------
async function episodeView(id) {
  setActive(null);
  let e;
  try { e = await getJson(`/api/episodes/${id}`); } catch {
    view.replaceChildren(h("div", { class: "column empty" }, "That episode no longer exists."));
    return;
  }
  const meta = h("dl", { class: "meta" });
  const add = (k, v) => { if (v) meta.append(h("dt", {}, k), h("dd", {}, v)); };
  add("When", `${fmtTime(e.start_ms)} · ${fmtDuration(e.end_ms - e.start_ms)}`);
  add("In", e.app ? appName(e.app) : "");
  add("Address", e.url || "");
  add("With", people(e).join(", "));
  const shot = e.frame_id ? h("a", { href: `#/frame/${e.frame_id}?from=memory`, title: "Open this moment" }, lazyImg("shot", `/api/frames/${e.frame_id}/image?w=960`, "Screenshot")) : null;
  view.replaceChildren(h("div", { class: "column" },
    h("div", { class: "viewer-bar" }, backButton("#/")),
    h("div", { class: "episode-head" }, kindBadge(e.kind), h("h1", {}, e.title || "(untitled)")),
    meta,
    shot,
    section(e.kind === "conversation" ? "What was said" : "Heard while it played", e.lines.map(lineRow))));
  cleanup = stopClip;
}
