"use strict";
// Runs processingLine() of the real app.js.
// Usage: node processing_line_check.js <path to app.js>
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");

const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("const KIND_WORDS");
const end = source.indexOf("let signedIn");
assert.ok(start >= 0 && end > start, "app.js no longer has the processing line block");
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
const line = (attention, processing) =>
  context.processingLine({ attention, processes: {}, processing });
const proc = (over = {}) => ({
  state: "ready", blocked: [], failed: {}, oldest_pending_ms: {}, horizon_ms: 0,
  transcribe_stalled_ms: 0, ...over,
});

const stopped = line("processing_stopped", null);
assert.equal(stopped.text, "Processing stopped");
assert.ok(stopped.detail.endsWith("The Debug page has details."));

const blocked = line("needs_attention", proc({ blocked: ["transcribe", "embed"] }));
assert.equal(blocked.text, "Needs attention");
assert.match(blocked.detail, /Transcription, search indexing can.t run/);
assert.match(blocked.detail, /Settings › Models/);
const stalled = line("needs_attention", proc({ transcribe_stalled_ms: 3_600_000 }));
assert.equal(stalled.text, "Needs attention");
assert.match(stalled.detail, /no progress for over an hour/);

// No verdict shows nothing, but failures still show.
assert.equal(line(null, proc()), null);
assert.equal(line(null, null), null);

const failed = line(null, proc({ failed: { transcribe: 1 } }));
assert.equal(failed.text, "1 recording could not be transcribed");
assert.equal(line(null, proc({ failed: { transcribe: 3 } })).text, "3 recordings could not be transcribed");
assert.equal(line(null, proc({ failed: { embed: 3 } })), null);

// The failure count is its own sentence on a problem line.
const both = line("processing_stopped", proc({ failed: { transcribe: 2 } }));
assert.match(both.detail, / 2 recordings could not be transcribed\. The Debug page has details\.$/);
console.log("processing line verified");
