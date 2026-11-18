"use strict";
// Runs the real meta-line code of the speech search row and the memory line row over a tiny DOM port.
// Usage: node speech_tag_ui_check.js <app.js> <memory.js>
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");

const read = (file) => fs.readFileSync(file, "utf8").split("\r\n").join("\n");
const app = read(process.argv[2]);
const memory = read(process.argv[3]);
const slice = (source, from, to) => {
  const a = source.indexOf(from);
  assert.ok(a >= 0, `missing ${from}`);
  const b = source.indexOf(to, a);
  assert.ok(b > a, `missing end of ${from}`);
  return source.slice(a, b + to.length);
};
const metaCode = slice(app, "const metaLine", "\n};\n");
const speechRow = slice(app, "  function row(u) {", "\n  }\n");
const lineRow = slice(memory, "function lineRow(line) {", "\n}\n");

function h(tag, attrs = {}, ...children) {
  return { tag, attrs, children: children.flat(), addEventListener() {} };
}
const texts = (el) => (typeof el === "string" ? [el] : (el?.children ?? []).flatMap(texts));
const meta = (el) => {
  const found = [];
  const walk = (n) => {
    if (n && typeof n === "object") {
      if (n.attrs?.class === "row-meta") found.push(n);
      n.children.forEach(walk);
    }
  };
  walk(el);
  return texts(found[0]);
};
const context = vm.createContext({
  h, fmtTime: () => "10:00", appName: (p) => p, playButton: () => h("button"), playClip() {},
  unclearAttrs: () => ({}), highlighted: (s) => [s],
});
vm.runInContext(metaCode, context);
const speech = vm.runInContext(`(${speechRow.replace("function row", "function")})`, context);
const line = vm.runInContext(`(${lineRow})`, context);

const u = { id: 1, stream: "system", start_ms: 0, speaker: null, sources: [], language: "en", snippet: "x", confidence: null };
const l = { segment_id: 1, stream: "system", start_ms: 0, speaker: null, episode_id: null, text: "x", confidence: null };
for (const tag of ["you", "voice chat", "game", "media"]) {
  assert.ok(meta(speech({ ...u, tag })).includes(tag), `speech row shows ${tag}`);
  assert.ok(meta(line({ ...l, tag })).includes(tag), `line row shows ${tag}`);
}
assert.deepEqual(meta(speech({ ...u, tag: "" })), ["10:00", "Heard", "en"]);
assert.deepEqual(meta(line({ ...l, tag: "" })), ["10:00", "Heard"]);
console.log("speech tag shown in the speech search and memory line rows, nothing when empty");
