"use strict";
// Runs the frame page and the result row from the real app.js over a tiny DOM port.
// Usage: node frame_page_check.js <path to app.js>
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");

const source = fs.readFileSync(process.argv[2], "utf8");
const grab = (start, end) => {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `app.js no longer has ${start}`);
  return source.slice(a, b);
};
const code = [
  grab("function resultRow(", "function searchField("),
  grab("const TEXT_SOURCE_LABELS", "// ---- routing"),
].join("\n");

function h(tag, attrs = {}, ...children) {
  return { tag, attrs: attrs || {}, children: children.flat(Infinity).filter((c) => c !== null) };
}
const text = (node) => (typeof node === "string" ? node : (node.children || []).map(text).join(" "));
const find = (node, pred, out = []) => {
  if (typeof node === "string") return out;
  if (pred(node)) out.push(node);
  for (const c of node.children) find(c, pred, out);
  return out;
};

let rendered = null;
let frame = null;
const view = { replaceChildren: (...items) => { rendered = items; }, focus() {} };
const context = vm.createContext({
  h, view, fmtTime: (ms) => `at ${ms}`, appName: (p) => p, thumb: () => null,
  metaLine: (...parts) => h("div", {}, ...parts), highlighted: (s) => [s],
  iconButton: (_i, label) => h("button", {}, label), backButton: () => h("button", {}, "Back"),
  lazyImg: () => h("img"), setActive() {}, visited: [], location: { replace() {} },
  document: { addEventListener() {}, removeEventListener() {} },
  getJson: async () => { if (!frame) throw new Error("gone"); return frame; },
  encodeURIComponent, cleanup: () => {},
});
vm.runInContext(code, context);

(async () => {
  // Search, timeline and memory rows show the window title, falling back to the address.
  const row = vm.runInContext("resultRow", context);
  const hit = { id: 4, window_id: 9, ts_ms: 1, process: "code.exe", title: "main.py", url: null, snippet: "x" };
  assert.ok(text(row(hit, "search")).includes("main.py"));
  assert.ok(text(row({ ...hit, title: null, url: "https://a.test" }, "search")).includes("https://a.test"));

  frame = {
    id: 4, ts_ms: 1700000000000, monitor_id: 1, has_image: false, prev_id: null, next_id: 5,
    windows: [
      { id: 1, role: "app", process: "chrome.exe", title: "Docs", url: "https://docs.test", z: 0,
        focused: true, painted: true, text: "front text", text_source: "uia", rect: [0, 0, 1, 1] },
      { id: 2, role: "hidden", z: 1, rect: [0, 0, 1, 1] },
      { id: 3, role: "app", process: "code.exe", title: "main.py", url: null, z: 2,
        focused: false, painted: false, text: null, text_source: null, rect: [0, 0, 1, 1] },
      { id: 4, role: "app", process: "game.exe", title: "Game", url: null, z: 3,
        focused: false, painted: true, text: "later words", text_source: "ocr_deferred", rect: [0, 0, 1, 1] },
      { id: 5, role: "app", process: "zen.exe", title: "Page", url: null, z: 4,
        focused: false, painted: true, text: "seen words", text_source: "ocr", rect: [0, 0, 1, 1] },
      { id: 6, role: "shell", z: 5, rect: [0, 0, 1, 1] },
    ],
  };
  await vm.runInContext("frameView(4, 'search')", context);
  const page = text(h("div", {}, ...rendered));
  const sections = find(h("div", {}, ...rendered), (n) => n.attrs.class === "window");
  assert.equal(sections.length, 4, "one block per app window, no hidden or shell rows");
  const blocks = sections.map(text);
  assert.ok(blocks[0].includes("chrome.exe") && blocks[0].includes("Docs"));
  assert.ok(blocks[0].includes("https://docs.test"));
  assert.ok(blocks[0].includes("app (UI Automation)") && blocks[0].includes("front text"));
  assert.ok(blocks[1].includes("code.exe") && blocks[1].includes("No text was read"));
  assert.ok(!blocks[1].includes("Text from"));
  assert.ok(blocks[2].includes("screen reading after the game (OCR)") && blocks[2].includes("later words"));
  assert.ok(blocks[3].includes("screen reading (OCR)") && !blocks[3].includes("after the game"));
  assert.ok(page.indexOf("front text") < page.indexOf("later words"), "front to back");
  assert.ok(page.includes(`at ${frame.ts_ms}`), "when the frame was taken");
  console.log("frame page verified");
})().catch((err) => { console.error(err); process.exit(1); });
