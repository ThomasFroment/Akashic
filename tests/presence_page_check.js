"use strict";
// Runs the presence block of the real app.js over a tiny DOM port.
// Usage: node presence_page_check.js <path to app.js>
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");

const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("// ---- presence");
assert.ok(start >= 0, "app.js no longer has the presence block");
const code = source.slice(start);

let ids = 0;
let statusRefreshes = 0;
const timers = [];
const posts = [];
const listeners = { document: {}, window: {} };
const shell = { hidden: true };
const waitNote = { hidden: false, textContent: "" };
const doc = {
  hidden: false,
  getElementById: () => waitNote,
  querySelectorAll: () => [],
  addEventListener: (name, fn) => { listeners.document[name] = fn; },
};
const context = vm.createContext({
  crypto: { randomUUID: () => `page-${++ids}` },
  document: doc,
  window: { addEventListener: (name, fn) => { listeners.window[name] = fn; } },
  shell, REOPEN: "reopen", stopClip() {}, onReveal: [], refreshStatus() { statusRefreshes += 1; },
  setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  clearTimeout() {},
  api: (path, init) => new Promise((resolve) => {
    posts.push({ path, body: JSON.parse(init.body), resolve });
  }),
});
vm.runInContext(code, context);
const reply = (post, recording_paused) => post.resolve({ json: async () => ({ recording_paused }) });
const settle = () => new Promise((r) => setImmediate(r));

(async () => {
  listeners.window.DOMContentLoaded();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, "/api/presence");
  assert.equal(posts[0].body.visible, true);
  const first = posts[0].body.page;

  // Nothing recorded is shown until the server says recording is paused.
  reply(posts[0], false);
  await settle();
  assert.equal(shell.hidden, true);
  assert.equal(timers.at(-1).ms, 250);
  timers.at(-1).fn();
  const revealed = [];
  context.onReveal.push(() => revealed.push("focus"));  // a page's focus, deferred while hidden
  reply(posts[1], true);
  await settle();
  assert.equal(shell.hidden, false);
  assert.deepEqual(revealed, ["focus"]);
  assert.equal(context.onReveal.length, 0);
  assert.equal(statusRefreshes, 1);  // the footer stops saying "Recording" at once
  assert.equal(waitNote.hidden, true);
  assert.equal(timers.at(-1).ms, 2000);

  // Recording is no longer paused: the content goes away again.
  timers.at(-1).fn();
  reply(posts[2], false);
  await settle();
  assert.equal(shell.hidden, true);

  // A late answer of a hidden period never shows anything in the next visible period.
  timers.at(-1).fn();
  const stale = posts[3];
  doc.hidden = true;
  listeners.document.visibilitychange();
  const hide = posts.at(-1);
  assert.equal(hide.body.visible, false);
  assert.equal(hide.body.page, first);
  hide.resolve({ json: async () => ({}) });
  doc.hidden = false;
  listeners.document.visibilitychange();
  const shown = posts.at(-1);
  assert.equal(shown.body.visible, true);
  assert.notEqual(shown.body.page, first, "each visible period has its own page id");
  reply(stale, true);
  await settle();
  assert.equal(shell.hidden, true, "a stale response is ignored");
  reply(shown, true);
  await settle();
  assert.equal(shell.hidden, false);
  console.log("presence page verified");
})().catch((e) => { console.error(e); process.exit(1); });
