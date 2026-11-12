"use strict";
// Local, hand-corrected comparison clips. No transcription replaces the memory's original.
function speechComparisonPanel() {
  const el = h("section", { class: "block" });
  const choices = h("div");
  const samples = h("div");
  const results = h("div");
  const note = h("p", { class: "muted small" });
  const reference = h("textarea", { rows: 4, placeholder: "Type what you hear from scratch. Leave empty for no speech.", "aria-label": "Correct reference transcript" });
  const draft = h("textarea", { rows: 2, readOnly: true, "aria-label": "Existing draft (read only)" });
  const draftLabel = h("p", { class: "muted small" });
  const language = h("select", { "aria-label": "Reference language" }, ...["fr", "en", "mixed", "none"].map((v) => h("option", { value: v }, v)));
  const condition = h("input", { placeholder: "Condition: quiet, game, short, overlap…", "aria-label": "Recording condition" });
  const derived = h("input", { type: "checkbox", "aria-label": "Reference derived from draft" });
  const previous = h("textarea", { rows: 2, placeholder: "Fixed preceding reference for prompt (if applicable)", "aria-label": "Preceding reference" });
  const overlap = h("textarea", { rows: 2, placeholder: "Fixed continuation predecessor reference for overlap trim (if applicable)", "aria-label": "Continuation reference" });
  const terms = h("input", { type: "text", placeholder: "Important terms, separated by commas", "aria-label": "Important terms" });
  const recording = h("select", { "aria-label": "Recording to compare" });
  const audio = h("audio", { controls: true, preload: "none" });
  const save = h("button", { type: "button" }, "Save corrected reference");
  const compare = h("button", { type: "button", class: "primary" }, "Compare selected models");
  let clips = [];
  let lastState = "";
  let loading = false;
  const checked = new Set();
  el.append(h("h2", {}, "Compare speech models"),
    h("p", { class: "muted small" }, "Use 6–12 short clips covering French, English, mixed speech, quiet speech and troublesome words. Correct the reference by listening; do not trust the old transcript as the answer. All clips, references and results stay local and encrypted."),
    recording, audio, draftLabel, draft, reference, language, condition,
    h("label", {}, derived, " I used the draft to create this reference"), previous, overlap, terms, save, note, samples, choices, compare,
    h("p", { class: "muted small" }, "Runs one model at a time, waits during games or heavy load, and releases models between comparisons. Earlier memories are unchanged. First-clip time includes model loading. Word scores ignore accents/case/punctuation; inspect important words and the full text too. Canary compares French/English decodes and does not use vocabulary hints."), results);

  async function request(path, method, body) {
    const response = await api(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    return response.json();
  }
  function chooseClip() {
    const clip = clips.find((c) => String(c.segment_id) === recording.value);
    reference.value = "";
    draft.value = clip?.transcript || "";
    draftLabel.textContent = `Existing draft · model: ${clip?.draft_model || "unknown"}`;
    derived.checked = false;
    previous.value = overlap.value = "";
    condition.value = "";
    terms.value = "";
    audio.src = clip ? `/api/audio/${clip.segment_id}` : "";
    save.disabled = !clip;
  }
  recording.addEventListener("change", chooseClip);
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await request("/api/stt/samples", "POST", { segment_id: Number(recording.value), reference: reference.value, reference_language: language.value, condition: condition.value, draft_derived: derived.checked, previous_reference: previous.value, overlap_reference: overlap.value, terms: terms.value.split(",").map((t) => t.trim()).filter(Boolean) });
      note.textContent = "Reference saved. Important terms must appear in this reference.";
      lastState = ""; await load();
    } catch { note.textContent = "Could not save. Check the reference and important terms (maximum 12 clips)."; }
    save.disabled = false;
  });
  compare.addEventListener("click", async () => {
    compare.disabled = true;
    try { await request("/api/stt/runs", "POST", { models: [...checked] }); note.textContent = "Comparison queued; you can keep using the app."; lastState = ""; }
    catch { note.textContent = "Could not start. Download the models and save a corrected reference first; only one run can be active."; }
    await load();
  });
  async function load() {
    if (loading || !el.isConnected) return;
    loading = true;
    try {
      const data = await getJson("/api/stt");
      const state = JSON.stringify(data);
      if (state === lastState) return;
      lastState = state;
      const busy = data.runs.some((r) => ["queued", "running"].includes(r.state));
      choices.replaceChildren(...data.models.map((m) => {
        const box = h("input", { type: "checkbox", disabled: !m.ready || busy, checked: checked.has(m.key) });
        box.addEventListener("change", () => { box.checked ? checked.add(m.key) : checked.delete(m.key); compare.disabled = !checked.size || busy || !data.samples.length; });
        return h("label", { class: "autostart-row" }, box, ` ${m.label}${m.ready ? "" : " — select above, save and download first"}`);
      }));
      samples.replaceChildren(h("p", { class: "muted small" }, `${data.samples.length}/12 corrected clips`), ...data.samples.map((s) => {
        const remove = h("button", { type: "button", disabled: busy }, "Remove");
        remove.onclick = async () => { await request(`/api/stt/samples/${s.segment_id}`, "DELETE"); lastState = ""; load(); };
        return h("div", { class: "row" }, h("div", { class: "row-body" }, h("span", { class: "small muted" }, `${fmtTime(s.start_ms)} · ${s.reference_language} · ${s.condition} · draft model: ${s.draft_model} · ${s.draft_derived ? "draft-derived" : "independently typed"}`), h("div", {}, s.reference)), remove);
      }));
      compare.disabled = busy || !checked.size || !data.samples.length;
      results.replaceChildren(...data.runs.map((run) => {
        return h("details", { open: run === data.runs[0] }, h("summary", {}, `Comparison ${run.id}: ${run.state} · ${run.processing_version}`),
          h("p", { class: "muted small" }, "Failed decodes count as full deletions; pending clips are excluded and counted. Empty-reference insertions are retained. WER/CER are undefined at zero denominator. FR/EN numbers 0–99 and hyphens normalize symmetrically; CER includes spaces. Inspect names and negations separately."),
          ...run.scores.map((s) => h("p", { class: "small" }, `${s.model} · ${s.dimension}: ${s.label} · clips ${s.clips}, completed ${s.completed}, failed ${s.failed}, pending ${s.pending}; WER ${s.words ? (100 * s.errors / s.words).toFixed(1) + "%" : "undefined"} (${s.errors}/${s.words}); CER ${s.chars ? (100 * s.char_errors / s.chars).toFixed(1) + "%" : "undefined"} (${s.char_errors}/${s.chars}); terms ${s.term_hits}/${s.term_total}`)),
          ...run.results.map((r) => h("div", { class: "row" }, h("div", { class: "row-body" },
            h("div", { class: "small muted" }, `${r.model} · clip ${r.segment_id}${r.elapsed_ms == null ? "" : ` · ${(r.elapsed_ms / 1000).toFixed(1)} s`}`),
            h("div", {}, "Reference: ", r.reference), h("div", {}, r.error ? `Failed: ${r.error}` : r.prediction === null ? "Waiting…" : `Heard: ${r.prediction}`)))))
      }));
    } catch { note.textContent = "Cannot read speech comparison data."; }
    finally { loading = false; }
  }
  return { el, start() {
    const segment = new URLSearchParams(location.hash.split("?")[1] || "").get("segment");
    getJson("/api/stt/recordings" + (segment ? `?segment_id=${encodeURIComponent(segment)}` : "")).then((data) => {
      clips = data;
      recording.replaceChildren(...data.map((c) => h("option", { value: c.segment_id }, `${fmtTime(c.start_ms)} · ${c.stream === "mic" ? "You" : "Heard"} · ${c.transcript.slice(0, 70)}`)));
      chooseClip(); if (!data.length) note.textContent = "No recorded clips available. Use Search > Speech to choose one when recordings exist.";
    }).catch(() => { note.textContent = "Cannot list recorded clips."; });
    load(); return setInterval(load, 2000);
  } };
}
