"use strict";
// The Debug console: everything needed to find out why something does not work, in one place.
// Uses the helpers of app.js (h, api, getJson, view, setActive, fmtTime, cleanup). Tabs:
// overview (what looks wrong) and logs (all of them, by level).
// Text from the database goes through textContent only, like the rest of the UI.

const DEBUG_TABS = [
  ["overview", "Overview"], ["logs", "Logs"],
];

function debugView(tab) {
  setActive("settings");
  const current = DEBUG_TABS.some(([key]) => key === tab) ? tab : "overview";
  const body = h("div", {});
  view.replaceChildren(h("div", { class: "column debug wide" },
    pageHead("Settings"),
    settingsTabs("debug"),
    h("div", { class: "bar" }, h("h2", {}, "Debug"),
      h("nav", { class: "tabs", "aria-label": "Debug sections" },
        DEBUG_TABS.map(([key, label]) =>
          h("a", { href: "#/settings?tab=" + key, class: key === current ? "active" : "" }, label)))),
    body));
  const tabs = { overview: debugOverview, logs: debugLogs };
  cleanup = tabs[current](body) || (() => {});
}

// ---- shared bits ----------------------------------------------------------------------------
const fmtAge = (ms) => (ms < 10000 ? "just now" : ms < 120000 ? Math.round(ms / 1000) + " s ago" : ms < 7200000 ? Math.round(ms / 60000) + " min ago" : Math.round(ms / 3600000) + " h ago");
const dbgCell = (v, col) => {
  if (v === null || v === undefined) return h("td", { class: "mono null" }, "–");
  if (typeof v === "number" && /_ms$/.test(col || "") && v > 1e11) return h("td", { class: "mono", title: String(v) }, fmtTime(v));
  return h("td", { class: "mono" }, String(v));
};
const dbgKv = (rows) => h("table", { class: "kv" }, rows.map(([k, v]) => h("tr", {}, h("th", {}, k), dbgCell(v))));
const dbgGrid = (heads, rows) => h("table", { class: "kv grid-table" },
  h("tr", {}, heads.map((t) => h("th", {}, t))),
  rows.map((r) => h("tr", {}, r.map((v, c) => dbgCell(v, heads[c])))));
const dbgNote = (kind, text) => h("div", { class: "note " + kind }, text);
const dbgSelect = (label, options, value) => {
  const el = h("select", { "aria-label": label }, options.map(([v, t]) => h("option", { value: v }, t)));
  el.value = value;
  return el;
};

// ---- overview -------------------------------------------------------------------------------
function debugOverview(root) {
  const diag = h("div", { class: "diag" });
  const actions = h("div", { class: "bar" });
  const body = h("div", {});
  const retryMsg = h("span", { class: "muted small" });
  root.replaceChildren(h("h2", {}, "What looks wrong"), diag, actions, body);
  actions.append(
    h("button", { type: "button", onclick: async () => {
      const r = await sendJson("/api/debug/jobs/retry").then((res) => res.json()).catch(() => null);
      retryMsg.textContent = r ? `${r.retried} job(s) made runnable.` : "Failed.";
      refresh();
    } }, "Run waiting jobs now"),
    h("button", { type: "button", class: "ghost", onclick: () => { document.getElementById("pause-btn").click(); } }, "Pause / resume capture"),
    retryMsg, h("span", { class: "muted small" }, "updates every 2 s"));

  function diagnose(o) {
    const out = [];
    const w = o.info.worker;
    const c = o.info.capture;
    if (!w) out.push(dbgNote("bad", "The worker has never reported. It may not have started: see the worker log."));
    else if (w.age_ms > 15000) out.push(dbgNote("bad", `The worker last reported ${fmtAge(w.age_ms)}: it is stuck or has stopped.`));
    else {
      const d = w.data;
      if (d.deferred_reason) {
        const s = d.scheduler;
        const detail = s && s.cpu !== null ? ` (CPU ${s.cpu}%, GPU ${s.gpu ?? "n/a"}%; limits ${d.limits.cpu_busy_percent}% and ${d.limits.gpu_busy_percent}%)` : "";
        out.push(dbgNote("bad", `${d.pending_jobs} job(s) are waiting because: ${d.deferred_reason}${detail}.`));
      } else if (Object.keys(d.blocked).length) {
        out.push(dbgNote("bad", "A job type cannot run: " + Object.entries(d.blocked).map(([k, v]) => `${k} (${v})`).join(", ") + "."));
      } else if (d.pending_jobs > 0) {
        const retrying = o.jobs.open.filter((j) => j.wait_ms > 0).length;
        out.push(dbgNote(retrying ? "" : "bad", retrying
          ? `${d.pending_jobs} job(s) pending, ${retrying} waiting out a retry delay after a failure. See the queue below.`
          : `${d.pending_jobs} job(s) pending but nothing blocks them; the worker should pick them up within seconds.`));
      } else out.push(dbgNote("ok", "The worker is running and the queue is empty."));
    }
    const failed = o.jobs.counts.filter((j) => j.state === "failed").reduce((a, j) => a + j.n, 0);
    if (failed) out.push(dbgNote("bad", `${failed} job(s) failed for good. “Run waiting jobs now” retries them; the Logs tab says why they failed.`));
    if (!c) out.push(dbgNote("bad", "The capture process has never reported."));
    else if (c.age_ms > 15000) out.push(dbgNote("bad", `Capture last reported ${fmtAge(c.age_ms)}.`));
    else if (c.data.audio) {
      const a = c.data.audio;
      const levels = Object.values(a.streams).flatMap((s) => s.drops.filter((d) => d.reason === "quiet").map((d) => d.level));
      if (a.stats.dropped_quiet) out.push(dbgNote("bad", `${a.stats.dropped_quiet} segment(s) dropped as too quiet (floor ${a.energy_floor}). Recent levels: ${levels.slice(-8).join(", ") || "none kept"}. If that was real speech, set [audio] energy_floor in config.toml below those levels and restart.`));
      if (a.stats.dropped_short) out.push(dbgNote("", `${a.stats.dropped_short} segment(s) dropped as under 0.4 s of speech (coughs, clicks, single words).`));
      if (a.stats.write_failed || a.stats.dropped_backlog) out.push(dbgNote("bad", "Some audio segments could not be stored: see the capture log."));
      for (const [kind, s] of Object.entries(a.streams)) if (!s.device) out.push(dbgNote("bad", `No ${kind} audio device is open.`));
    } else out.push(dbgNote("bad", "Audio recording is not running (disabled, or it failed to start): see the capture log."));
    if (o.disk.free_gb !== null && o.disk.free_gb < 50) out.push(dbgNote("bad", `Only ${o.disk.free_gb} GB free on the data drive.`));
    return out;
  }

  function segmentTable(o) {
    if (!o.audio.recent.length) return h("div", { class: "muted" }, "No segments yet.");
    const rows = o.audio.recent;
    const table = dbgGrid(["id", "stream", "when", "speech", "sources", "status", "lang"],
      rows.map((s) => [s.id, s.stream, fmtTime(s.start_ms), (s.speech_ms / 1000).toFixed(1) + " s", JSON.parse(s.sources).join(", ") || "–", s.status, s.language]));
    table.rows[0].append(h("th", {}, ""));
    rows.forEach((s, i) => {
      const play = playButton("Play", "small-play");
      play.addEventListener("click", () => playClip(play, s.id, 0, Number.MAX_SAFE_INTEGER));
      const again = h("button", { type: "button", class: "ghost" }, "re-transcribe");
      again.addEventListener("click", async () => {
        const queued = await sendJson(`/api/debug/segments/${s.id}/retranscribe`).then((res) => res.json()).catch(() => null);
        again.textContent = queued ? "queued" : "failed";
      });
      table.rows[i + 1].append(h("td", {}, play, again));
    });
    return table;
  }

  function render(o) {
    diag.replaceChildren(...diagnose(o));
    const w = o.info.worker;
    const c = o.info.capture;
    const parts = [];
    parts.push(h("h2", {}, "Processes"), dbgKv(Object.entries(o.processes).map(([k, v]) =>
      [k, v + (o.info[k] ? " · reported " + fmtAge(o.info[k].age_ms) : "")])));
    if (w) {
      const d = w.data;
      const s = d.scheduler;
      parts.push(h("h2", {}, "Worker"), dbgKv([
        ["State", d.state], ["Last step", d.last_outcome], ["Pending jobs", d.pending_jobs],
        ["Waiting because", d.deferred_reason || "nothing"],
        ["Scheduler", s ? `${s.allowed ? "allowed" : "refused: " + s.reason} · CPU ${s.cpu ?? "n/a"}% · GPU ${s.gpu ?? "n/a"}%` : "not asked yet"],
        ["Limits", `CPU ${d.limits.cpu_busy_percent}% · GPU ${d.limits.gpu_busy_percent}%`],
        ["Blocked handlers", Object.keys(d.blocked).length ? JSON.stringify(d.blocked) : "none"]]));
    }
    parts.push(h("h2", {}, "Job queue"));
    parts.push(o.jobs.counts.length ? dbgKv(o.jobs.counts.map((j) => [`${j.kind} · ${j.state}`, j.n])) : h("div", { class: "muted" }, "No jobs."));
    if (o.jobs.open.length) {
      parts.push(dbgGrid(["id", "kind", "state", "tries", "heard", "retry in", "error"],
        o.jobs.open.map((j) => [j.id, j.kind, j.state, j.attempts, fmtTime(j.data_ms), j.wait_ms ? Math.round(j.wait_ms / 1000) + " s" : "–", j.error])));
    }
    if (c && c.data.audio) {
      const a = c.data.audio;
      parts.push(h("h2", {}, "Audio capture"), dbgKv([["Energy floor", a.energy_floor], ...Object.entries(a.stats)]));
      for (const [kind, s] of Object.entries(a.streams)) {
        parts.push(h("h3", {}, kind), dbgKv([
          ["Device", s.device || "not open"],
          ["Loudest 32 ms window (last second)", s.level_max],
          ["Voice level (last second)", s.speech_level],
          ["Best speech probability (last second)", s.speech_probability_max],
          ["Recent drops", s.drops.length ? s.drops.slice(-8).map((d) => `${d.reason}: level ${d.level}, ${d.speech_ms} ms`).join("\n") : "none"]]));
      }
    }
    parts.push(h("h2", {}, "Recent audio segments"), segmentTable(o));
    parts.push(h("h2", {}, "Screen and storage"), dbgKv([
      ["Frames", `${o.frames.total.toLocaleString()} total · ${o.frames.last_minute} in the last minute`],
      ["Latest frame", o.frames.latest_ts_ms ? fmtAge(o.now_ms - o.frames.latest_ts_ms) : "none"],
      ["Screen outcomes", c ? JSON.stringify(c.data.screen) : null],
      ["Media outcomes", c ? JSON.stringify(c.data.media) : null],
      ["Database", `${o.disk.db_mb ?? "?"} MB · schema v${o.schema}`], ["Free disk", `${o.disk.free_gb ?? "?"} GB`]]));
    parts.push(h("h2", {}, "Settings in effect"), dbgKv(Object.entries(o.settings)));
    body.replaceChildren(...parts);
  }

  async function refresh() {
    try { render(await getJson("/api/debug/overview")); } catch (e) {
      diag.replaceChildren(dbgNote("bad", e.status === 404 ? "The debug routes are not enabled." : "Cannot reach the app."));
    }
  }
  const timer = setInterval(refresh, 2000);
  refresh();
  return () => { clearInterval(timer); stopClip(); };
}

// ---- logs -----------------------------------------------------------------------------------
function debugLogs(root) {
  const fileSel = h("select", { "aria-label": "Log file" }, h("option", { value: "all" }, "All logs, merged"));
  const levelSel = dbgSelect("Minimum level", [["DEBUG", "All levels"], ["INFO", "Info and above"], ["WARNING", "Warnings and above"], ["ERROR", "Errors only"]], "DEBUG");
  const search = h("input", { type: "search", placeholder: "Contains…", "aria-label": "Search the logs" });
  const limitSel = dbgSelect("Entries", [["300", "300 entries"], ["1000", "1000 entries"], ["5000", "5000 entries"]], "300");
  const live = h("input", { type: "checkbox", id: "log-live", checked: true });
  const info = h("div", { class: "muted small" });
  const box = h("div", { class: "logbox", tabindex: "0" }, "…");
  root.replaceChildren(
    h("div", { class: "bar" }, fileSel, levelSel, search, limitSel,
      h("label", { class: "small muted", for: "log-live" }, live, " live")),
    info, box);

  let last = "";
  async function loadFiles() {
    try {
      const files = await getJson("/api/debug/logs/files");
      const chosen = fileSel.value;
      fileSel.replaceChildren(h("option", { value: "all" }, "All logs, merged"),
        files.map((f) => h("option", { value: f.name }, `${f.name} (${Math.round(f.size / 1024)} KB, ${fmtAge(Date.now() - f.modified_ms)})`)));
      fileSel.value = chosen;
    } catch { /* keep the old list */ }
  }
  async function load() {
    const p = new URLSearchParams({ name: fileSel.value, level: levelSel.value, q: search.value, limit: limitSel.value });
    let r;
    try { r = await getJson("/api/debug/logs?" + p); } catch { box.textContent = "Cannot read the logs."; return; }
    const stamp = `${p}|${r.total}|${r.entries.length}|${r.entries.at(-1)?.ts}`;
    if (stamp === last) return;
    last = stamp;
    info.textContent = `${r.entries.length} of ${r.total} entries shown`;
    const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    const frag = document.createDocumentFragment();
    for (const e of r.entries) {
      frag.append(h("div", { class: "logline " + e.level.toLowerCase() },
        h("span", { class: "ts" }, e.ts.slice(5, 23)), h("span", { class: "src" }, e.src),
        h("span", { class: "lvl" }, e.level), h("span", { class: "msg" }, `${e.logger}: ${e.msg}`)));
    }
    box.replaceChildren(r.entries.length ? frag : h("div", { class: "muted" }, "Nothing matches."));
    if (atEnd) box.scrollTop = box.scrollHeight;
  }
  let timer = 0;
  const schedule = () => { clearTimeout(timer); timer = setTimeout(load, 250); };
  for (const el of [fileSel, levelSel, limitSel]) el.addEventListener("change", load);
  search.addEventListener("input", schedule);
  const tick = setInterval(() => { if (live.checked) { load(); } }, 2000);
  const filesTick = setInterval(loadFiles, 10000);
  loadFiles().then(load);
  return () => { clearInterval(tick); clearInterval(filesTick); clearTimeout(timer); };
}
