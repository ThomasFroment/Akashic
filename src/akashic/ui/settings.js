"use strict";
// Settings: every option of config.toml worth changing from here, saved back into that file.
// Uses the helpers of app.js (h, api, getJson, view, setActive, cleanup, applyTheme).
// A live microphone meter sits with the voice settings so the energy floor can be set by looking
// at your real voice level instead of guessing.

const SETTINGS_TABS = [
  ["general", "General"], ["capture", "Capture"], ["audio", "Audio"],
  ["retention", "Retention"], ["models", "Models"], ["debug", "Debug"],
];
const settingsDraft = new Map();

function settingsTabs(current) {
  return h("nav", { class: "tabs settings-tabs", "aria-label": "Settings sections" },
    SETTINGS_TABS.map(([key, label]) => h("a", {
      href: "#/settings?tab=" + key, class: key === current ? "active" : "",
      "aria-current": key === current ? "page" : null,
    }, label)));
}

function settingsView(tab = "general") {
  setActive("settings");
  if (tab === "debug" || tab === "overview" || tab === "logs") { debugView(tab === "debug" ? "overview" : tab); return; }
  if (!SETTINGS_TABS.some(([key]) => key === tab)) tab = "general";
  const root = h("div", { class: "column settings wide" }, h("div", { class: "muted" }, "Loading…"));
  view.replaceChildren(root);
  let timer = 0;
  cleanup = () => clearInterval(timer);
  getJson("/api/settings").then((data) => { timer = build(root, data, tab); cleanup = () => clearInterval(timer); })
    .catch(() => root.replaceChildren(h("div", { class: "empty" }, "Cannot read the settings.")));
}

function build(root, data, tab) {
  const controls = new Map(); // key -> { field, read(), row }
  const saved = new Map(data.fields.map((f) => [f.key, JSON.stringify(f.value)]));
  const status = h("span", { class: "msg muted small" });
  const saveBtn = h("button", { type: "button", class: "primary", disabled: true }, "Save");
  const restartBtn = h("button", { type: "button", hidden: true }, "Restart recording now");
  const meter = micMeter(() => Number(controls.get("audio.energy_floor")?.read() ?? 0));
  const storage = storagePanel();
  const deletePanel = dataDeletionPanel();
  const modelDownloads = tab === "models" ? modelsPanel() : null;
  const speechTests = tab === "models" ? speechComparisonPanel() : null;

  function control(field) {
    const id = "set-" + field.key;
    const common = { id, "aria-describedby": id + "-help" };
    const value = settingsDraft.has(field.key) ? settingsDraft.get(field.key) : field.value;
    switch (field.kind) {
      case "bool": {
        const el = h("input", { ...common, type: "checkbox", checked: value });
        return { el, read: () => el.checked };
      }
      case "int":
      case "number": {
        const el = h("input", { ...common, type: "number", min: field.min, max: field.max, step: field.step ?? "any", value });
        return { el, read: () => (el.value === "" ? NaN : Number(el.value)) };
      }
      case "days": { // empty means forever
        const el = h("input", { ...common, type: "number", min: field.min, max: field.max, step: 1, placeholder: "forever", value: value ?? "" });
        return { el, read: () => (el.value === "" ? null : Number(el.value)) };
      }
      case "choice": {
        const el = h("select", common, field.choices.map((c) => h("option", { value: c }, field.choice_labels?.[c] || c)));
        if (!field.choices.includes(value)) el.append(h("option", { value }, value + " (custom)"));
        el.value = value;
        return { el, read: () => el.value };
      }
      case "device": {
        const names = (data.devices && data.devices[field.key === "audio.mic_device" ? "mic" : "system"]) || [];
        const el = h("select", common, h("option", { value: "" }, "Windows default"),
          names.map((n) => h("option", { value: n }, n)));
        if (value && !names.includes(value)) el.append(h("option", { value }, value + " (not found now)"));
        el.value = value;
        return { el, read: () => el.value };
      }
      case "lines": {
        const el = h("textarea", { ...common, rows: Math.max(3, value.length + 1), spellcheck: "false" });
        el.value = value.join("\n");
        return { el, read: () => el.value.split("\n").map((s) => s.trim()).filter(Boolean) };
      }
      default: {
        const el = h("input", { ...common, type: "text", value });
        return { el, read: () => el.value };
      }
    }
  }

  const sections = [];
  const groups = new Map();
  for (const field of data.fields) {
    const group = field.group;
    let box = groups.get(group);
    if (!box) {
      box = h("div", {});
      groups.set(group, box);
      const section = h("section", { class: "settings-section", "data-group": group }, h("h2", {}, group), box);
      sections.push(section);
      if (group === "Microphone and voice") box.append(meter.el);
      if (group === "Retention") box.append(storage.el);
    }
    const c = control(field);
    const row = h("div", { class: "setting" },
      h("label", { class: "name", for: "set-" + field.key }, field.label), c.el,
      field.help ? h("div", { class: "help", id: "set-" + field.key + "-help" }, field.help) : null);
    controls.set(field.key, { field, read: c.read, row });
    c.el.addEventListener("input", refresh);
    c.el.addEventListener("change", refresh);
    box.append(row);
  }

  function changedKeys() {
    return [...controls].filter(([key, c]) => JSON.stringify(c.read()) !== saved.get(key)).map(([key]) => key);
  }
  function refresh() {
    const changed = new Set(changedKeys());
    for (const [key, c] of controls) {
      c.row.classList.toggle("changed", changed.has(key));
      if (changed.has(key)) settingsDraft.set(key, c.read());
      else settingsDraft.delete(key);
    }
    saveBtn.disabled = changed.size === 0;
    const theme = controls.get("theme");
    if (theme) applyTheme(theme.read());
    if (changed.size) { status.textContent = `${changed.size} unsaved change(s)`; status.className = "msg muted small"; }
  }

  saveBtn.addEventListener("click", async () => {
    const values = Object.fromEntries(changedKeys().map((key) => [key, controls.get(key).read()]));
    // A shorter limit deletes data for good, so ask before saving one.
    const shorter = Object.entries(values).filter(([key, v]) => {
      if (!key.startsWith("retention.") || v === null) return false;
      const before = JSON.parse(saved.get(key));
      return before === null || v < before;
    });
    if (shorter.length) {
      const list = shorter.map(([key, v]) => `${controls.get(key).field.label}: ${v} days`).join("\n");
      if (!confirm(`This permanently deletes recordings older than:\n\n${list}\n\nIt happens after you restart recording, and cannot be undone. Save?`)) return;
    }
    saveBtn.disabled = true;
    let res;
    try {
      res = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      });
    } catch { status.textContent = "Cannot reach the app."; status.className = "msg bad"; refresh(); return; }
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      status.textContent = typeof detail.detail === "string" ? detail.detail : "Could not save (" + res.status + ").";
      status.className = "msg bad";
      refresh();
      return;
    }
    const out = await res.json();
    if (modelDownloads) modelDownloads.load();
    for (const key of Object.keys(values)) saved.set(key, JSON.stringify(values[key]));
    refresh();
    const notes = [];
    if (out.restart_recording) notes.push("Restart recording to apply.");
    if (out.restart_app) notes.push("Restart the whole app for the log level.");
    status.textContent = "Saved. " + notes.join(" ");
    status.className = "msg small";
    restartBtn.hidden = !out.restart_recording;
  });

  restartBtn.addEventListener("click", async () => {
    restartBtn.disabled = true;
    try { await sendJson("/api/settings/restart"); status.textContent = "Restarting the recording and worker processes… this takes a few seconds."; }
    catch { status.textContent = "Could not restart."; status.className = "msg bad"; }
    setTimeout(() => { restartBtn.hidden = true; restartBtn.disabled = false; }, 4000);
  });

  const groupTab = (name) => name === "Model selection" ? "models" : name === "Retention" ? "retention"
    : ["Microphone and voice", "System sound", "Transcription"].includes(name) ? "audio"
      : ["Screen", "Privacy"].includes(name) ? "capture" : "general";
  for (const section of sections) section.hidden = groupTab(section.dataset.group) !== tab;
  const savebar = h("div", { class: "savebar" }, saveBtn, restartBtn, status);
  root.replaceChildren(...[
    pageHead("Settings"),
    settingsTabs(tab),
    tab === "general" ? autostartBlock() : null,
    ...sections,
    tab === "retention" ? deletePanel : null,
    tab === "models" ? h("section", { class: "block", id: "models" }, h("h2", {}, "Downloads for selected models"), modelDownloads.el) : null,
    speechTests?.el,
    savebar].filter(Boolean));
  refresh();
  return tab === "audio" ? meter.start() : speechTests ? speechTests.start() : 0;
}

const STORAGE_KIND_LABELS = { screenshots: "Screenshots", frame_text: "Screen text", raw_audio: "Recorded audio", transcripts: "Transcripts", media_sessions: "Media sessions", app_sessions: "App sessions", episodes: "Episodes", embeddings: "Search vectors" };

function dataDeletionPanel() {
  const status = h("div", { class: "muted small" });
  async function remove(kind) {
    if (!confirm(`Permanently delete all ${STORAGE_KIND_LABELS[kind]}? This cannot be undone.`)) return;
    try {
      await api(`/api/settings/storage/${kind}`, { method: "DELETE" });
      status.textContent = `${STORAGE_KIND_LABELS[kind]} deleted.`;
    } catch { status.textContent = "Could not delete that data."; }
  }
  return h("section", { class: "block" }, h("h2", {}, "Delete data"),
    h("p", { class: "muted" }, "Choose a category to permanently remove all of it."),
    h("div", { class: "button-row" }, Object.keys(STORAGE_KIND_LABELS).map((k) => { const b = h("button", { type: "button", class: "danger" }, `Delete ${STORAGE_KIND_LABELS[k]}`); b.addEventListener("click", () => remove(k)); return b; })),
    (() => { const b = h("button", { type: "button", class: "danger solid" }, "Delete all recorded data"); b.addEventListener("click", async () => {
      if (!confirm("Permanently delete all captured data, transcripts, episodes, speakers and past questions? App settings and downloaded models will remain. Recording will briefly stop, then return to its current state. This cannot be undone.")) return;
      b.disabled = true;
      status.textContent = "Deleting recorded data…";
      try { await api("/api/settings/storage", { method: "DELETE" }); status.textContent = "Recorded data deleted. New activity will be recorded if recording is active."; }
      catch { status.textContent = "Could not complete deletion. Some data may remain; close programs using the files and retry."; }
      finally { b.disabled = false; }
    }); return b; })(), status);
}

// Start with Windows: applied at once (the user's Run registry value), not part of config.toml.
function autostartBlock() {
  const box = h("input", { type: "checkbox", id: "set-autostart" });
  const note = h("span", { class: "msg muted small" });
  const block = h("section", { class: "block", hidden: true }, h("h2", {}, "Startup"),
    h("label", { class: "autostart-row", for: "set-autostart" }, box, " Start Akashic when I sign in to Windows (it asks for the passphrase first)"), note);
  getJson("/api/settings/autostart").then((a) => {
    if (!a.available) return;
    box.checked = a.enabled;
    block.hidden = false;
  }).catch(() => {});
  box.addEventListener("change", async () => {
    try {
      const res = await api("/api/settings/autostart", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: box.checked }),
      });
      box.checked = (await res.json()).enabled;
      note.textContent = " Saved.";
    } catch { note.textContent = " Could not change it."; box.checked = !box.checked; }
  });
  return block;
}

// The downloads the current settings need. Nothing is fetched until a button is pressed; files
// are pinned and checked (pipeline/models.py). Polls while something is downloading.
function modelsPanel() {
  const el = h("div", { class: "meter-box" }, h("div", { class: "muted small" }, "Checking…"));
  const mb = (n) => n == null || !Number.isFinite(Number(n)) ? "Size unknown" : (n >= 1000 ? (n / 1000).toFixed(1) + " GB" : n + " MB");
  let timer = 0;
  async function start(path) {
    try { await sendJson(path); } catch { /* busy or gone: the list says why */ }
    load();
  }
  async function load() {
    clearTimeout(timer);
    if (!el.isConnected && timer) return;
    let rows;
    try { rows = await getJson("/api/models"); } catch {
      el.replaceChildren(h("div", { class: "muted small" }, "Cannot read the model list."));
      return;
    }
    const busy = rows.some((r) => r.status === "downloading" || r.status === "queued");
    const missing = rows.filter((r) => r.status !== "ready");
    const state = (r) => {
      if (r.status === "ready") return h("span", { class: "muted" }, "ready");
      if (r.status === "queued") return h("span", { class: "muted" }, "waiting…");
      if (r.status === "downloading") {
        const pct = r.total ? Math.floor((100 * r.done) / r.total) : 0;
        return h("span", {}, h("progress", { max: 100, value: pct }), ` ${pct} %`);
      }
      const btn = h("button", { type: "button", disabled: busy }, r.status === "failed" ? "Retry" : "Download");
      btn.addEventListener("click", () => start(`/api/models/${encodeURIComponent(r.key)}/download`));
      return h("span", {}, btn, r.error ? h("span", { class: "msg bad small" }, " " + r.error) : null);
    };
    const all = h("button", { type: "button", class: "primary", disabled: busy }, `Download all missing (${mb(missing.reduce((s, r) => s + r.size_mb, 0))})`);
    all.addEventListener("click", () => start("/api/models/download-all"));
    el.replaceChildren(...[
      h("table", { class: "storage" }, rows.map((r) => h("tr", {},
        h("td", {}, r.label || "Unnamed model"), h("td", { class: "muted" }, r.needed_for || "—"), h("td", { class: "num" }, mb(r.size_mb)), h("td", {}, state(r))))),
      missing.length && !busy ? all : null,
      h("div", { class: "muted small" }, "Downloads come from Hugging Face and GitHub, pinned to exact versions and checked; nothing about you is sent.")].filter(Boolean));
    if (busy) timer = setTimeout(load, 1000);
  }
  load();
  return { el, load };
}

// What the recordings take on disk, per kind, and how fast it grows: the numbers to choose limits by.
function storagePanel() {
  const el = h("div", { class: "meter-box" }, h("div", { class: "muted small" }, "Measuring…"));
  const size = (b) => b >= 1e9 ? (b / 1e9).toFixed(1) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.max(0, Math.round(b / 1e3)) + " KB";
  getJson("/api/settings/storage").then((s) => {
    const rows = s.types.map((t) => h("tr", {}, h("td", {}, STORAGE_KIND_LABELS[t.kind] || t.kind), h("td", { class: "num" }, size(t.bytes)),
      h("td", { class: "num muted" }, "+" + size(t.bytes_7d) + " this week"), h("td", { class: "muted" }, t.days === null ? "kept forever" : "kept " + t.days + " days")));
    const free = s.free_bytes === null ? "unknown" : size(s.free_bytes);
    el.replaceChildren(...[
      h("table", { class: "storage" }, rows),
      h("div", { class: "muted small" }, `Total on disk ${size(s.total_bytes)} (database ${size(s.database_bytes)}, which holds the texts). Free on the data drive: ${free}.`),
      s.low_space ? h("div", { class: "msg bad small" }, `Less than ${s.warn_below_gb} GB free. Nothing is deleted automatically: shorten a limit below, or pause screenshots.`) : null].filter(Boolean));
  }).catch(() => el.replaceChildren(h("div", { class: "muted small" }, "Cannot measure the disk use.")));
  return { el };
}

// The live voice level, from the capture process (about once a second).
function micMeter(getFloor) {
  const loudFill = h("div", { class: "fill" });
  const voiceFill = h("div", { class: "fill" });
  const voiceMark = h("div", { class: "mark", title: "Energy floor" });
  const loudText = h("span", { class: "mono" });
  const voiceText = h("span", { class: "mono" });
  const hint = h("div", { class: "muted small" }, "Waiting for the recorder…");
  const row = (label, fill, text, mark) => h("div", { class: "meter-row" }, h("span", {}, label),
    h("div", { class: "meter" }, fill, mark || null), text);
  const el = h("div", { class: "meter-box", "aria-live": "off" },
    row("Loudest moment", loudFill, loudText),
    row("Your voice", voiceFill, voiceText, voiceMark), hint);
  // Levels are small numbers: a square-root scale makes quiet speech visible.
  const pos = (v) => Math.min(100, Math.sqrt(Math.max(v, 0) / 0.4) * 100) + "%";

  async function poll() {
    let r;
    try { r = await getJson("/api/settings/levels"); } catch { return; }
    const mic = r.audio && r.audio.streams && r.audio.streams.mic;
    if (r.age_ms === null || r.age_ms > 6000 || !mic) {
      hint.textContent = "The recorder is not reporting (stopped, paused or restarting).";
      return;
    }
    const floor = getFloor();
    loudFill.style.width = pos(mic.level_max);
    loudText.textContent = mic.level_max.toFixed(3);
    voiceFill.style.width = pos(mic.speech_level ?? 0);
    voiceText.textContent = mic.speech_level === null ? "–" : mic.speech_level.toFixed(3);
    voiceMark.style.left = pos(floor);
    if (mic.speech_level === null) hint.textContent = `Speak normally. No speech heard in the last second. The marker is the energy floor (${floor}); your voice must reach past it. Gain and sensitivity changes show here after restarting the recorder.`;
    else if (mic.speech_level < floor) hint.textContent = `Your voice (${mic.speech_level.toFixed(3)}) is below the energy floor (${floor}): it would be dropped. Lower the floor or raise the gain.`;
    else hint.textContent = `Your voice (${mic.speech_level.toFixed(3)}) is above the energy floor (${floor}): it is kept.`;
  }
  return { el, start() { poll(); return setInterval(poll, 1000); } };
}
