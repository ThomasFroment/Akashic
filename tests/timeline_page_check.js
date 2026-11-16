"use strict";
// Runs the real timeline.js, and the parts of app.js and memory.js that build moment links, over a
// small fake DOM. Not exercised: layout, real images, pointer capture, CSS, a real browser.
// Usage: node timeline_page_check.js <ui dir>
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const assert = require("node:assert/strict");

const ui = process.argv[2];
const read = (name) => fs.readFileSync(path.join(ui, name), "utf8");
const app = read("app.js");
const memory = read("memory.js");
const timelineSource = read("timeline.js");

const grab = (source, start, end) => {
  const a = source.indexOf(start);
  const b = end === undefined ? source.length : source.indexOf(end, a);
  assert.ok(a >= 0 && b > a, `source no longer has ${start}`);
  return source.slice(a, b);
};

// ---- the fake DOM ---------------------------------------------------------------------------
class El {
  constructor(tag) {
    this.tag = tag;
    this.attrs = {};
    this.children = [];
    this.parent = null;
    this.style = {};
    this.dataset = {};
    this.hidden = false;
    this.listeners = {};
    this.clientWidth = 0;
    this.value = "";
    this._classes = new Set();
    this._text = "";
    const self = this;
    this.classList = {
      toggle(n, force) {
        const on = force === undefined ? !self._classes.has(n) : !!force;
        if (on) self._classes.add(n); else self._classes.delete(n);
        return on;
      },
      add: (n) => self._classes.add(n),
      remove: (n) => self._classes.delete(n),
      contains: (n) => self._classes.has(n),
    };
  }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._classes].join(" "); }
  setAttribute(k, v) {
    if (k === "style") throw new Error("setAttribute('style') is forbidden by the CSP");
    this.attrs[k] = v;
    if (k === "hidden") this.hidden = true;
  }
  removeAttribute(k) { delete this.attrs[k]; }
  _adopt(n) {
    if (typeof n !== "string") {
      if (n.parent) n.parent.children = n.parent.children.filter((c) => c !== n);
      n.parent = this;
    }
    return n;
  }
  append(...nodes) { for (const n of nodes) this.children.push(this._adopt(n)); }
  replaceChildren(...nodes) {
    for (const c of this.children) if (typeof c !== "string") c.parent = null;
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }
  insertBefore(n, ref) {
    this._adopt(n);
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, n);
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join(""); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  fire(type, event = {}) { for (const fn of this.listeners[type] || []) if (fn) fn({ preventDefault() {}, ...event }); }
  querySelectorAll(sel) {
    assert.ok(sel.startsWith("."), "the fake supports class selectors only");
    return find(this, (n) => n._classes.has(sel.slice(1)));
  }
  getBoundingClientRect() { return { left: 0, width: 1000 }; }
  setPointerCapture() {}
  matches() { return false; }
}
function find(node, pred, out = []) {
  if (typeof node === "string") return out;
  for (const c of node.children) {
    if (typeof c === "string") continue;
    if (pred(c)) out.push(c);
    find(c, pred, out);
  }
  return out;
}
const hasClass = (n, c) => n._classes.has(c);
const settle = async () => { for (let i = 0; i < 3; i++) await new Promise((r) => setImmediate(r)); };

// ---- the page, per test ---------------------------------------------------------------------
const NOON = new Date(2026, 10, 8, 12).getTime();
const DAY_START = new Date(2026, 10, 8).getTime();
const DAY_END = new Date(2026, 10, 9).getTime();

function boot() {
  const env = { pending: [], requested: [], images: [], timers: [], replaced: [], resize: null, keys: null };
  const view = new El("div");
  const location = { hash: "" };
  class Img extends El {
    constructor() { super("img"); env.images.push(this); }
    set src(v) { this._src = v; env.requested.push(v); }
    get src() { return this._src; }
  }
  let timerIds = 0;
  const context = vm.createContext({
    document: {
      createElement: (tag) => new El(tag),
      addEventListener: (type, fn) => { if (type === "keydown") env.keys = fn; },
      removeEventListener: (type, fn) => { if (type === "keydown" && env.keys === fn) env.keys = null; },
    },
    Image: Img,
    URLSearchParams, view, location, visited: [], cleanup() {},
    devicePixelRatio: 1, innerHeight: 1000,
    history: { replaceState: (_s, _t, url) => { env.replaced.push(url); location.hash = url; } },
    setTimeout: (fn, ms) => { const id = ++timerIds; env.timers.push({ id, fn, ms }); return id; },
    clearTimeout: (id) => { env.timers = env.timers.filter((t) => t.id !== id); },
    ResizeObserver: class { constructor(fn) { env.resize = fn; } observe() {} disconnect() {} },
    getJson: (p) => new Promise((resolve, reject) => env.pending.push({ path: p, resolve, reject })),
    setActive() {},
    pageHead: (_title, ...extras) => { const d = new El("div"); d.append(...extras); return d; },
    iconButton: (_name, label, opts = {}) => {
      const b = new El("button");
      b.attrs.title = label;
      b.addEventListener("click", opts.onclick);
      return b;
    },
    fmtTime: (ms) => `t${ms}`,
  });
  const run = (code) => vm.runInContext(code, context);
  run([
    grab(app, "function h(tag", "// An icon from"),
    grab(app, "const fmtClock", "// Whisper"),
    grab(app, "const isoDay", "// Meta line"),
    grab(app, "function momentHref(", "function resultRow("),
    timelineSource,
  ].join("\n"));
  const calls = (prefix) => env.pending.filter((p) => p.path.startsWith(prefix));
  const answer = async (prefix, data) => {
    const i = env.pending.findIndex((p) => p.path.startsWith(prefix));
    assert.ok(i >= 0, `no pending request for ${prefix}`);
    const [req] = env.pending.splice(i, 1);
    req.resolve(data);
    await settle();
  };
  const runTimers = async () => {
    const due = env.timers;
    env.timers = [];
    for (const t of due) t.fn();
    await settle();
  };
  const press = (key) => env.keys({ key, target: { matches: () => false }, ctrlKey: false, metaKey: false, altKey: false, preventDefault() {} });
  const start = (opts = {}) => {
    context.args = [opts.apps || ["code.exe", "chrome.exe"], opts.ts ?? NOON, opts.monitor ?? null, opts.window ?? null];
    run("timelineView(...args)");
    const wrap = find(view, (n) => hasClass(n, "stage-wrap"))[0];
    wrap.clientWidth = opts.width ?? 0;
    return wrap;
  };
  const tiles = () => find(find(view, (n) => hasClass(n, "stage"))[0], (n) => hasClass(n, "monitor"));
  const stageCalls = () => calls("/api/stage");
  // Moves the moment to about `ms` through the strip, as a user would.
  const scrubTo = async (ms) => {
    const strip = find(view, (n) => n.attrs.role === "slider")[0];
    strip.fire("pointerdown", { button: 0, pointerId: 1, clientX: ((ms - DAY_START) / (DAY_END - DAY_START)) * 1000 });
    await runTimers();
  };
  const stageTs = (req) => Number(req.path.split("ts=")[1]);
  return { env, context, run, view, location, calls, answer, runTimers, press, start, tiles, stageCalls, stageTs, scrubTo };
}

const win = (id) => ({ id, process: "code.exe", title: `w${id}`, rect: [0, 0, 100, 100] });
function stage(ts, opts = {}) {
  const m = (id, name, rect, state, extra = {}) => ({
    id, name, rect, scale: 1, state, frame_id: null, checked_ms: ts - 1000, focused: false, windows: [], ...extra,
  });
  return {
    ts_ms: ts, prev_ms: "prev" in opts ? opts.prev : ts - 5000, next_ms: "next" in opts ? opts.next : ts + 5000,
    monitors: [
      m(1, "Left", [0, 0, 1920, 1080], opts.leftState || "shown",
        opts.leftState ? {} : { frame_id: opts.frame ?? 11, focused: true, windows: [win(101), win(102)] }),
      m(2, "Hid", [1920, 0, 3840, 1080], "hidden"),
      m(3, "Drop", [0, 1080, 1920, 2160], "dropped"),
      m(4, "Fail", [1920, 1080, 3840, 2160], "failed"),
      m(5, "Never", [3840, 0, 5760, 1080], "not_recording"),
    ],
  };
}
const STRIP_PATH = "/api/strip?day=2026-11-08";
const IMAGE = /^\/api\/frames\/(\d+)\/image\?w=(\d+)$/;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ---- routes and links -----------------------------------------------------------------------
async function routeTo(hash) {
  const calls = [];
  const ctx = vm.createContext({
    location: { hash }, shell: new El("div"), view: new El("div"), cleanup() {}, trackHistory() {},
    signedIn: true, URLSearchParams, timelineView: (...a) => calls.push(a),
    frameView() {}, searchView() {}, speakersView() {}, episodeView() {}, chatView() {},
    settingsView() {}, memoryView() {}, REOPEN: "",
    getJson: async (path) => (path.startsWith("/api/stage?ts=") ? { prev_ms: 42 } : []),
  });
  vm.runInContext(grab(app, "async function route()", "// ---- mobile menu"), ctx);
  await vm.runInContext("route()", ctx);
  return calls;
}

test("#/timeline and #/moment/<ts> route to the timeline", async () => {
  const [[apps, ts, monitor, windowId]] = await routeTo("#/timeline");
  assert.equal(JSON.stringify([apps, monitor, windowId]), "[[],null,null]");
  assert.equal(ts, 42);  // the latest recorded moment: "now" is inside the UI-open pause
  assert.equal(JSON.stringify(await routeTo("#/moment/123?monitor=2&window=5")), '[[[],123,"2","5"]]');
  assert.equal(JSON.stringify(await routeTo("#/moment/123")), "[[[],123,null,null]]");
});

function linkContext() {
  const view = new El("div");
  const ctx = vm.createContext({
    document: { createElement: (tag) => new El(tag) }, view, URLSearchParams, cleanup() {},
    fmtTime: (ms) => `t${ms}`, setActive() {}, playClip() {}, playButton: () => new El("button"),
    metaLine: () => new El("div"), kindBadge: () => new El("span"), people: () => [], fmtDuration: () => "1 min",
    section: () => null, lineRow: () => null, backButton: () => new El("button"), stopClip() {},
    appName: (p) => p, getJson: async () => ctx.episode,
  });
  vm.runInContext([
    grab(app, "function h(tag", "// An icon from"),
    grab(app, "function lazyImg(", "function thumb("),
    grab(app, "function momentHref(", "function resultRow("),
    grab(memory, "// A frame whose moment", "// The plain matches"),
    grab(memory, "async function episodeView("),
  ].join("\n"), ctx);
  return { ctx, view };
}

const source = (extra) => ({ n: 1, kind: "frame", id: 4, label: "L", start_ms: 1, window_id: 9, frame_ms: 1000, monitor_id: 2, ...extra });

test("frame citation chips and source images open the moment, omitting null params", async () => {
  const { ctx } = linkContext();
  const href = (s) => vm.runInContext("citeChip(s)", Object.assign(ctx, { s })) && ctx.s && vm.runInContext("citeChip(s)", ctx).attrs.href;
  assert.equal(href(source()), "#/moment/1000?monitor=2&window=9");
  assert.equal(href(source({ monitor_id: 0 })), "#/moment/1000?monitor=0&window=9");
  assert.equal(href(source({ monitor_id: null })), "#/moment/1000?window=9");
  assert.equal(href(source({ window_id: null })), "#/moment/1000?monitor=2");
  assert.equal(href(source({ window_id: null, monitor_id: null })), "#/moment/1000");
  assert.equal(href({ n: 2, kind: "episode", id: 3, label: "E", start_ms: 1 }), "#/episode/3");

  ctx.s = { n: 2, kind: "episode", id: 3, label: "E", start_ms: 1, frame_id: 12, window_id: 7, frame_ms: 5000, monitor_id: 2 };
  const link = vm.runInContext("sourceImage(s)", ctx);
  assert.equal(link.attrs.href, "#/moment/5000?monitor=2&window=7");
  assert.equal(link.children[0].attrs.src, "/api/frames/12/image?window=7&w=320");
  ctx.s = source();
  assert.equal(vm.runInContext("sourceImage(s)", ctx).attrs.href, "#/moment/1000?monitor=2&window=9");
});

test("the episode image links to its moment", async () => {
  const { ctx, view } = linkContext();
  ctx.episode = {
    id: 3, kind: "video", title: "T", start_ms: 1, end_ms: 2, app: "Zen", url: null, lines: [],
    frame_id: 12, window_id: 7, frame_ms: 5000, monitor_id: 2,
  };
  await vm.runInContext("episodeView(3)", ctx);
  const [link] = find(view, (n) => n.attrs.title === "Open this moment");
  assert.equal(link.attrs.href, "#/moment/5000?monitor=2&window=7");
  assert.equal(link.children[0].attrs.src, "/api/frames/12/image?window=7&w=960");
});

// ---- the stage ------------------------------------------------------------------------------
test("a non-shown monitor never shows an image", async () => {
  const t = boot();
  t.start({ width: 1000 });
  await t.answer("/api/stage", stage(NOON));
  assert.ok(t.env.requested.length > 0);
  assert.ok(t.env.requested.every((u) => IMAGE.exec(u)[1] === "11"), "only the shown monitor's frame is asked for");
  for (const img of t.env.images) img.onload();  // every picture arrives
  const states = t.tiles().map((tile) => [tile.dataset.state, find(tile, (n) => n.tag === "img").length]);
  assert.deepEqual(states, [["shown", 1], ["hidden", 0], ["dropped", 0], ["failed", 0], ["not_recording", 0]]);
  // A monitor that stops being shown loses its picture.
  await t.scrubTo(NOON + 5 * 60000);
  await t.answer("/api/stage", stage(t.stageTs(t.stageCalls()[0]), { leftState: "hidden" }));
  assert.equal(find(t.tiles()[0], (n) => n.tag === "img").length, 0);
});

test("an unchanged frame id at the same width step is not asked for again", async () => {
  const t = boot();
  const wrap = t.start({ width: 1000 });
  await t.answer("/api/stage", stage(NOON));
  const first = [...t.env.requested];
  assert.equal(first.length, 1);
  const step = Number(IMAGE.exec(first[0])[2]);
  assert.equal(step % 240, 0);
  wrap.clientWidth = 990;  // still the same 240 px step
  t.env.resize();
  await t.scrubTo(NOON + 5 * 60000);
  await t.answer("/api/stage", stage(t.stageTs(t.stageCalls()[0])));
  assert.deepEqual(t.env.requested, first);
  // A new frame, or another width step, is a new request.
  await t.scrubTo(NOON + 10 * 60000);
  await t.answer("/api/stage", stage(t.stageTs(t.stageCalls()[0]), { frame: 12 }));
  assert.equal(t.env.requested.length, 2);
  assert.ok(t.env.requested[1].startsWith("/api/frames/12/"));
  wrap.clientWidth = 500;
  t.env.resize();
  assert.equal(t.env.requested.length, 3);
  assert.notEqual(IMAGE.exec(t.env.requested[2])[2], IMAGE.exec(t.env.requested[1])[2]);
});

test("nothing is requested while the stage width is 0; it renders when the width appears", async () => {
  const t = boot();
  const wrap = t.start({ width: 0 });
  await t.answer("/api/stage", stage(NOON));
  assert.deepEqual(t.env.requested, []);
  assert.deepEqual(t.tiles(), []);
  wrap.clientWidth = 1000;
  t.env.resize();
  assert.equal(t.env.requested.length, 1);
  assert.equal(t.tiles().length, 5);
});

test("a tile is labelled with its left-to-right number, then its name", async () => {
  const t = boot();
  t.start({ width: 1000 });
  await t.answer("/api/stage", stage(NOON));
  const labels = t.tiles().map((tile) => find(tile, (n) => hasClass(n, "mon-name"))[0].textContent);
  assert.deepEqual(labels, ["Monitor 1 · Left", "Monitor 3 · Hid", "Monitor 2 · Drop", "Monitor 4 · Fail", "Monitor 5 · Never"]);
  assert.equal(t.tiles()[0].attrs["aria-label"], "Monitor 1 · Left, focused");
});

test("a listed window on a stage tile is a button that opens the moment with it outlined", async () => {
  const t = boot();
  t.start({ width: 1000 });
  await t.answer("/api/stage", stage(NOON));
  const items = find(t.tiles()[0], (n) => n.tag === "li");
  assert.equal(items.length, 2);
  const button = items[1].children[0];
  assert.equal(button.tag, "button");
  assert.equal(button.attrs.type, "button");
  button.fire("click");
  assert.equal(t.location.hash, `#/moment/${NOON}?monitor=1&window=102`);
  // The enlarged monitor keeps plain focusable items.
  const t2 = boot();
  t2.start({ width: 1000, monitor: "1" });
  await t2.answer("/api/stage", stage(NOON));
  const big = find(t2.view, (n) => hasClass(n, "big"))[0];
  const bigItems = find(big, (n) => n.tag === "li");
  assert.equal(bigItems.length, 2);
  assert.ok(bigItems.every((li) => li.attrs.tabindex === "0" && !li.children.some((c) => c.tag === "button")));
});

// ---- the strip and the moment ---------------------------------------------------------------
test("a scrub past the strip's right edge stays inside the day", async () => {
  const t = boot();
  t.start({ width: 1000 });
  await t.answer("/api/stage", stage(NOON));
  const strip = find(t.view, (n) => n.attrs.role === "slider")[0];
  strip.fire("pointerdown", { button: 0, pointerId: 1, clientX: 50000 });
  await t.runTimers();
  const [asked] = t.stageCalls();
  assert.ok(t.stageTs(asked) < DAY_END && t.stageTs(asked) >= DAY_END - 1000, asked.path);
  assert.equal(t.calls("/api/strip").length, 1, "still the same day: the strip is not fetched again");
  strip.fire("pointerdown", { button: 0, pointerId: 1, clientX: -50000 });
  await t.runTimers();
  assert.ok(t.stageTs(t.stageCalls()[0]) >= DAY_START);
});

test("arrows step from prev_ms and next_ms only when the stage matches the moment", async () => {
  const t = boot();
  t.start({ width: 1000 });
  await t.answer("/api/stage", stage(NOON));
  t.press("ArrowLeft");
  await t.runTimers();
  assert.equal(t.stageTs(t.stageCalls()[0]), NOON - 5000);
  t.press("ArrowRight");  // the stage is still the old moment's
  assert.deepEqual(t.env.timers, []);
  await t.answer("/api/stage", stage(NOON - 5000, { next: NOON, prev: null }));
  t.press("ArrowLeft");  // nothing before
  assert.deepEqual(t.env.timers, []);
  t.press("ArrowRight");
  await t.runTimers();
  assert.equal(t.stageTs(t.stageCalls()[0]), NOON);
});

test("only one stage fetch is in flight, and the last moment is fetched after it", async () => {
  const t = boot();
  t.start({ width: 1000 });
  const strip = find(t.view, (n) => n.attrs.role === "slider")[0];
  strip.fire("pointerdown", { button: 0, pointerId: 1, clientX: 100 });
  await t.runTimers();
  strip.fire("pointermove", { clientX: 300 });
  await t.runTimers();
  strip.fire("pointermove", { clientX: 600 });
  await t.runTimers();
  assert.equal(t.stageCalls().length, 1);
  const first = t.stageTs(t.stageCalls()[0]);
  await t.answer("/api/stage", stage(first));
  assert.equal(t.stageCalls().length, 1, "asked again for the moment it moved to");
  const latest = t.stageTs(t.stageCalls()[0]);
  assert.ok(latest > first);
  await t.answer("/api/stage", stage(latest));
  assert.equal(t.stageCalls().length, 0);
});

test("moving the moment replaces the address, keeping monitor and dropping window", async () => {
  const t = boot();
  t.start({ width: 1000, monitor: "1", window: "101" });
  await t.answer("/api/stage", stage(NOON));
  assert.deepEqual(t.env.replaced, []);
  const strip = find(t.view, (n) => n.attrs.role === "slider")[0];
  strip.fire("pointerdown", { button: 0, pointerId: 1, clientX: 400 });
  await t.runTimers();
  const ts = t.stageTs(t.stageCalls()[0]);
  assert.deepEqual(t.env.replaced, [`#/moment/${ts}?monitor=1`]);
  assert.ok(!t.env.replaced[0].includes("window"));

  const plain = boot();
  plain.start({ width: 1000 });
  find(plain.view, (n) => n.attrs.role === "slider")[0].fire("pointerdown", { button: 0, pointerId: 1, clientX: 400 });
  await plain.runTimers();
  assert.equal(plain.env.replaced.length, 1);
  assert.match(plain.env.replaced[0], /^#\/moment\/\d+$/);
});

test("the filter dims non-matching bands and hides none; positions go through el.style", async () => {
  const t = boot();
  t.start({ width: 1000, apps: ["code.exe", "chrome.exe"] });
  const band = (start, process) => ({ start_ms: NOON + start, end_ms: NOON + start + 600000, process, title: "x" });
  await t.answer("/api/strip", { bands: [band(0, "Code.EXE"), band(1000000, "chrome.exe"), band(2000000, "code.exe")] });
  const bands = () => find(t.view, (n) => hasClass(n, "band"));
  assert.equal(bands().length, 3);
  const select = find(t.view, (n) => n.tag === "select")[0];
  select.value = "code.exe";
  select.fire("change");
  assert.deepEqual(bands().map((b) => [hasClass(b, "match"), hasClass(b, "dim")]), [[true, false], [false, true], [true, false]]);
  assert.ok(bands().every((b) => !b.hidden && !("display" in b.style)), "none is hidden");
  select.value = "";
  select.fire("change");
  assert.ok(bands().every((b) => !hasClass(b, "match") && !hasClass(b, "dim")));
  for (const b of bands()) {
    assert.match(b.style.left, /%$/);
    assert.match(b.style.width, /%$/);
    assert.equal(b.attrs.style, undefined);
  }
  const cursor = find(t.view, (n) => hasClass(n, "strip-cursor"))[0];
  assert.match(cursor.style.left, /%$/);
  assert.equal(find(t.view, (n) => "style" in n.attrs).length, 0);  // setAttribute("style") would have thrown
});

let failed = 0;
(async () => {
  for (const [name, fn] of tests) {
    try { await fn(); console.log(`ok - ${name}`); } catch (err) { failed++; console.error(`FAIL - ${name}\n`, err); }
  }
  if (failed) process.exit(1);
  console.log("timeline page verified");
})();
