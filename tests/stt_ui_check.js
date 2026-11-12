"use strict";
// Tiny DOM port for the real local panel; no browser/package dependency or captured material.
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const elements = [];
function h(tag, attrs = {}, ...children) {
  const el = { tag, attrs, children: children.flat(), isConnected: true, value: attrs.value || "",
    disabled: false, checked: false, events: {}, textContent: "", ...attrs,
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; if (this.tag === "select") this.value = String(items[0]?.value || ""); },
    addEventListener(name, handler) { this.events[name] = handler; } };
  elements.push(el);
  return el;
}
const writes = [];
const data = { models: [{ key: "fake", label: "Fake", ready: true }], samples: [], runs: [{
  id: 1, state: "done", processing_version: "production-boundary-v1", results: [],
  scores: [{model: "fake", dimension: "condition", label: "quiet", clips: 2, completed: 1,
    failed: 1, pending: 0, errors: 2, words: 4, char_errors: 5, chars: 20, term_hits: 0, term_total: 1}]
}] };
const context = vm.createContext({ h, URLSearchParams, location: { hash: "" }, fmtTime: String,
  getJson: async (url) => url.startsWith("/api/stt/recordings") ? [{ segment_id: 3,
    start_ms: 1, stream: "system", transcript: "incumbent draft", draft_model: "unknown" }] : data,
  api: async (url, body) => { writes.push({url, body: JSON.parse(body.body)}); return {json: async () => ({})}; },
  setInterval: () => 1,
});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
const panel = vm.runInContext("speechComparisonPanel()", context);
const find = (label) => elements.find((el) => el.attrs["aria-label"] === label);
const flush = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  panel.start(); await flush();
  assert.equal(find("Correct reference transcript").value, "");
  assert.equal(find("Existing draft (read only)").value, "incumbent draft");
  assert.equal(find("Existing draft (read only)").readOnly, true);
  find("Correct reference transcript").value = ""; // an independently labelled no-speech clip
  find("Recording condition").value = "silence";
  find("Reference language").value = "none";
  const save = elements.find((el) => el.tag === "button" && el.children.includes("Save corrected reference"));
  await save.events.click();
  assert.equal(writes[0].body.reference, "");
  assert.equal(writes[0].body.condition, "silence");
  assert.equal(writes[0].body.reference_language, "none");
  assert.equal(writes[0].body.draft_derived, false);
  const text = JSON.stringify(panel.el);
  assert.match(text, /failed 1/);
  assert.match(text, /50.0%/);
  assert.match(text, /25.0%/);
  console.log("STT panel: empty reference, readonly draft, tags/provenance and WER/CER denominators verified");
})().catch((error) => { console.error(error); process.exitCode = 1; });
