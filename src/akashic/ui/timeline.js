"use strict";
// Timeline: the stage (every monitor as it was at one moment) above a strip of the day's app bands.
// Sizes are set through CSSOM, never a style attribute (the CSP forbids it).

const MIN_SPAN_MS = 60000;
const ZOOM_STEP = 1.25;
const WHEEL_NOTCH_PARTS = 20;  // one wheel notch moves the moment by a twentieth of the strip
const SETTLE_MS = 120;  // after the last move: fetch the stage and update the address
const IMAGE_STEP_PX = 240;
const MAX_IMAGE_PX = 1920;
const STATE_TEXT = { hidden: "Hidden", dropped: "Dropped", failed: "Failed", not_recording: "Not recording" };
let zoomSpan = Infinity;  // the strip's span, kept from one moment to the next

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const idOf = (v) => (/^\d+$/.test(v ?? "") ? Number(v) : null);
const imageWidth = (cssPx) => clamp(Math.ceil((cssPx * devicePixelRatio) / IMAGE_STEP_PX) * IMAGE_STEP_PX, IMAGE_STEP_PX, MAX_IMAGE_PX);

function processColor(process) {
  let hash = 0;
  for (const ch of process || "") hash = (hash * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${hash} 55% 52%)`;
}

// Place el over the part of `within` ([l, t, r, b]) that `rect` covers, in percent; hide it when none.
function placeRect(el, rect, within) {
  const [l, t, r, b] = [Math.max(rect[0], within[0]), Math.max(rect[1], within[1]), Math.min(rect[2], within[2]), Math.min(rect[3], within[3])];
  el.hidden = r <= l || b <= t;
  if (el.hidden) return;
  const w = within[2] - within[0];
  const hgt = within[3] - within[1];
  el.style.left = `${(100 * (l - within[0])) / w}%`;
  el.style.top = `${(100 * (t - within[1])) / hgt}%`;
  el.style.width = `${(100 * (r - l)) / w}%`;
  el.style.height = `${(100 * (b - t)) / hgt}%`;
}

// One monitor: its picture (or why there is none), a label, and the list of its windows on hover.
function monitorTile(onPick) {
  const shot = h("div", { class: "mon-shot" });
  const note = h("div", { class: "mon-note", hidden: true });
  const outline = h("div", { class: "window-outline", hidden: true });
  const name = h("span", { class: "mon-name" });
  const age = h("span", { class: "mon-age" });
  const mark = h("span", { class: "focus-mark", hidden: true }, "Focused");
  const list = h("ul", { class: "window-list" });
  const el = h("div", { class: "monitor", role: "group" }, shot, note, outline, h("div", { class: "mon-label" }, name, age, mark), list);
  let id = null;
  let rect = [0, 0, 1, 1];
  let restWindow = null;  // the window outlined when nothing is hovered
  let shownUrl = null;

  const setNote = (text) => { note.textContent = text; note.hidden = !text; };
  function showOutline(windowRect) {
    if (windowRect) placeRect(outline, windowRect, rect);
    else outline.hidden = true;
  }
  // The old picture stays until the new one has loaded.
  function setPicture(frameId, width, emptyNote, alt) {
    if (frameId === null) { shownUrl = null; shot.replaceChildren(); setNote(emptyNote); return; }
    const url = `/api/frames/${frameId}/image?w=${width}`;
    if (url === shownUrl) return;
    shownUrl = url;
    setNote("");
    const loader = new Image();
    loader.alt = alt;
    loader.onload = () => { if (shownUrl === url) shot.replaceChildren(loader); };
    loader.onerror = () => { if (shownUrl === url) { shot.replaceChildren(); setNote("No picture"); } };
    loader.src = url;
  }

  if (onPick) el.insertBefore(h("button", { type: "button", class: "mon-hit", "aria-label": "Show enlarged", onclick: () => onPick(id) }), note);

  function update(m, label, ts, widthCss, windowId) {
    id = m.id;
    rect = m.rect;
    const shown = m.state === "shown";
    el.dataset.state = m.state;
    el.classList.toggle("focused", m.focused);
    name.textContent = label;
    age.textContent = m.checked_ms !== null && m.checked_ms < ts ? `checked ${fmtAge(ts - m.checked_ms)}` : "";
    mark.hidden = !m.focused;
    const stateText = shown ? "" : STATE_TEXT[m.state];
    el.setAttribute("aria-label", [label, stateText, m.focused ? "focused" : ""].filter(Boolean).join(", "));
    setPicture(shown ? m.frame_id : null, imageWidth(widthCss), shown ? "No picture" : stateText, `Screen of ${label}`);
    restWindow = m.windows.find((w) => w.id === windowId)?.rect ?? null;
    showOutline(restWindow);
    list.replaceChildren(...m.windows.map((w) => {
      const hover = {
        onmouseenter: () => showOutline(w.rect), onfocus: () => showOutline(w.rect),
        onmouseleave: () => showOutline(restWindow), onblur: () => showOutline(restWindow),
      };
      const label = [h("span", { class: "win-app" }, appName(w.process)), h("span", { class: "win-title" }, w.title || "")];
      return onPick
        ? h("li", {}, h("button", { type: "button", onclick: () => onPick(id, w.id), ...hover }, ...label))
        : h("li", { tabindex: "0", ...hover }, ...label);
    }));
  }
  return { el, update };
}

// The newest recorded moment: recording is paused while a page shows, so "now" never is one.
async function latestMoment() {
  try {
    return (await getJson(`/api/stage?ts=${Date.now()}`)).prev_ms ?? Date.now();
  } catch {
    return Date.now();
  }
}

function timelineView(apps, ts, monitorParam, windowParam) {
  setActive("timeline");
  let moment = clamp(ts, 0, Date.now());
  let monitorId = idOf(monitorParam);
  let windowId = idOf(windowParam);
  let day = isoDay(new Date(moment));
  let dayStart = 0;
  let dayEnd = 0;
  let viewStart = 0;
  let bands = [];
  let stage = null;
  let fetching = false;
  let stripSeq = 0;
  let settleTimer = 0;
  let scrubbing = false;

  const date = h("input", { type: "date", "aria-label": "Day", max: isoDay(new Date()) });
  const appSel = h("select", { "aria-label": "Highlight app" },
    h("option", { value: "" }, "All apps"), apps.map((a) => h("option", { value: a }, appName(a))));
  const shiftDay = (n) => () => {
    const d = new Date(moment);
    d.setDate(d.getDate() + n);
    moveTo(d.getTime(), true);
  };
  const stageEl = h("div", { class: "stage" });
  const wrap = h("div", { class: "stage-wrap" }, stageEl);
  const status = h("div", { class: "muted small", role: "status" });
  const bigName = h("h2", { class: "big-name" });
  const bigTile = monitorTile(null);
  const bigBox = h("section", { class: "big", hidden: true },
    h("div", { class: "big-head" }, bigName, iconButton("x", "Show the whole desk (Esc)", { onclick: () => closeBig() })), bigTile.el);
  const clockLabel = h("div", { class: "strip-clock" });
  const axisStart = h("span");
  const axisEnd = h("span");
  const cursor = h("div", { class: "strip-cursor" });
  const track = h("div", { class: "strip-track" });
  const strip = h("div", {
    class: "strip", tabindex: "0", role: "slider", "aria-label": "Day strip: arrows jump between moments, plus and minus zoom",
  }, track);
  view.replaceChildren(h("div", { class: "column wide" },
    pageHead("Timeline", h("div", { class: "spacer" }),
      iconButton("chev-left", "Previous day", { cls: "", onclick: shiftDay(-1) }), date,
      iconButton("chev-right", "Next day", { cls: "", onclick: shiftDay(1) }),
      h("button", { type: "button", onclick: () => latestMoment().then((ts) => moveTo(ts, true)) }, "Today"), appSel),
    status, wrap, bigBox,
    h("div", { class: "strip-bar" }, clockLabel, h("div", { class: "spacer" }),
      iconButton("minus", "Zoom out (-)", { cls: "", onclick: () => zoomAtCursor(ZOOM_STEP) }),
      iconButton("plus", "Zoom in (+)", { cls: "", onclick: () => zoomAtCursor(1 / ZOOM_STEP) })),
    strip, h("div", { class: "strip-axis" }, axisStart, axisEnd)));

  // ---- the stage ----
  const tiles = new Map();
  const fitScale = (rect, maxHeight) => Math.min(wrap.clientWidth / (rect[2] - rect[0]), maxHeight / (rect[3] - rect[1]));

  function renderStage() {
    if (wrap.clientWidth === 0) return;  // the shell is hidden; the observer renders when it shows
    const ms = stage.monitors;
    const labels = monitorLabels(ms);
    for (const [id, tile] of tiles) {
      if (!ms.some((m) => m.id === id)) { tile.el.remove(); tiles.delete(id); }
    }
    const union = ms.length ? [Math.min(...ms.map((m) => m.rect[0])), Math.min(...ms.map((m) => m.rect[1])),
      Math.max(...ms.map((m) => m.rect[2])), Math.max(...ms.map((m) => m.rect[3]))] : [0, 0, 1, 1];
    const big = ms.find((m) => m.id === monitorId);
    const scale = fitScale(union, innerHeight * (big ? 0.3 : 0.55));
    stageEl.style.width = `${(union[2] - union[0]) * scale}px`;
    stageEl.style.height = `${(union[3] - union[1]) * scale}px`;
    status.textContent = ms.length ? "" : "No monitor layout was recorded at this moment.";
    for (const m of ms) {
      let tile = tiles.get(m.id);
      if (!tile) {
        tile = monitorTile((id, windowId) => { location.hash = momentHref(moment, id, windowId); });
        tiles.set(m.id, tile);
        stageEl.append(tile.el);
      }
      placeRect(tile.el, m.rect, union);
      tile.update(m, labels.get(m.id), stage.ts_ms, (m.rect[2] - m.rect[0]) * scale);
    }
    bigBox.hidden = !big;
    if (!big) return;
    const bigScale = fitScale(big.rect, innerHeight * 0.6);
    bigTile.el.style.width = `${(big.rect[2] - big.rect[0]) * bigScale}px`;
    bigTile.el.style.height = `${(big.rect[3] - big.rect[1]) * bigScale}px`;
    bigName.textContent = labels.get(big.id);
    bigTile.update(big, labels.get(big.id), stage.ts_ms, (big.rect[2] - big.rect[0]) * bigScale, windowId);
  }

  // One request at a time; asks again when the moment moved meanwhile.
  async function fetchStage() {
    if (fetching) return;
    fetching = true;
    try {
      let asked;
      do {
        asked = moment;
        stage = await getJson(`/api/stage?ts=${asked}`);
        renderStage();
      } while (asked !== moment);
    } catch {
      status.textContent = "The desk at this moment could not be loaded.";
    } finally {
      fetching = false;
    }
  }

  function syncAddress() {
    history.replaceState(null, "", momentHref(moment, monitorId, windowId));
    visited[visited.length - 1] = location.hash;
  }

  function closeBig() {
    monitorId = null;
    windowId = null;
    syncAddress();
    renderStage();
  }

  // ---- the strip ----
  const spanNow = () => Math.min(zoomSpan, dayEnd - dayStart);
  const fraction = (ms) => (ms - viewStart) / spanNow();

  function applyFilter() {
    const wanted = appSel.value.toLowerCase();
    for (const el of track.querySelectorAll(".band")) {
      const match = wanted !== "" && el.dataset.process === wanted;
      el.classList.toggle("match", match);
      el.classList.toggle("dim", wanted !== "" && !match);
    }
  }

  function placeCursor() {
    cursor.style.left = `${100 * fraction(moment)}%`;
    clockLabel.textContent = fmtTime(moment);
    strip.setAttribute("aria-valuenow", String(moment));
    strip.setAttribute("aria-valuetext", fmtTime(moment));
  }

  function renderStrip() {
    const span = spanNow();
    const viewEnd = viewStart + span;
    const clock = { hour: "2-digit", minute: "2-digit", second: span < 600000 ? "2-digit" : undefined };
    axisStart.textContent = new Date(viewStart).toLocaleTimeString([], clock);
    axisEnd.textContent = new Date(viewEnd).toLocaleTimeString([], clock);
    track.replaceChildren(...bands.filter((b) => b.end_ms >= viewStart && b.start_ms < viewEnd).map((b) => {
      const el = h("div", { class: "band", title: `${appName(b.process)}${b.title ? " · " + b.title : ""} · ${fmtClock(b.start_ms)}–${fmtClock(b.end_ms)}` });
      const left = fraction(Math.max(b.start_ms, viewStart));
      el.dataset.process = (b.process || "").toLowerCase();
      el.style.left = `${100 * left}%`;
      el.style.width = `${100 * (fraction(Math.min(b.end_ms, viewEnd)) - left)}%`;
      el.style.background = processColor(b.process);
      return el;
    }), cursor);
    applyFilter();
    placeCursor();
  }

  function setView(start, span) {
    viewStart = clamp(start, dayStart, dayEnd - span);
    renderStrip();
  }

  async function loadStrip() {
    const mine = ++stripSeq;
    let data;
    try { data = await getJson(`/api/strip?day=${day}`); } catch { data = { bands: [] }; }
    if (mine !== stripSeq) return;
    bands = data.bands;
    renderStrip();
  }

  function setDay(next) {
    day = next;
    const [y, m, d] = day.split("-").map(Number);
    dayStart = new Date(y, m - 1, d).getTime();
    dayEnd = new Date(y, m - 1, d + 1).getTime();
    date.value = day;
    strip.setAttribute("aria-valuemin", String(dayStart));
    strip.setAttribute("aria-valuemax", String(dayEnd));
    bands = [];
    loadStrip();
  }

  function moveTo(ms, immediately = false) {
    moment = clamp(Math.round(ms), 0, Date.now());
    windowId = null;
    const changedDay = isoDay(new Date(moment)) !== day;
    if (changedDay) setDay(isoDay(new Date(moment)));
    const span = spanNow();
    if (changedDay || moment < viewStart || moment >= viewStart + span) setView(moment - span / 2, span);
    else placeCursor();
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => { fetchStage(); syncAddress(); }, immediately ? 0 : SETTLE_MS);
  }

  // Zoom so the time at `anchor` (0..1 across the strip) stays where it is.
  function zoom(factor, anchor) {
    const span = spanNow();
    const length = dayEnd - dayStart;
    const next = clamp(span * factor, MIN_SPAN_MS, length);
    zoomSpan = next >= length ? Infinity : next;
    setView(viewStart + anchor * span - anchor * next, next);
  }
  const zoomAtCursor = (factor) => zoom(factor, clamp(fraction(moment), 0, 1));
  const timeAt = (clientX) => {
    const box = track.getBoundingClientRect();
    const span = spanNow();
    return viewStart + clamp(((clientX - box.left) / box.width) * span, 0, span - 1);
  };

  strip.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (e.ctrlKey) {
      const box = track.getBoundingClientRect();
      zoom(e.deltaY < 0 ? 1 / ZOOM_STEP : ZOOM_STEP, clamp((e.clientX - box.left) / box.width, 0, 1));
    } else {
      moveTo(moment + ((e.deltaY / 100) * spanNow()) / WHEEL_NOTCH_PARTS);
    }
  }, { passive: false });
  strip.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    scrubbing = true;
    strip.setPointerCapture(e.pointerId);
    moveTo(timeAt(e.clientX));
  });
  strip.addEventListener("pointermove", (e) => { if (scrubbing) moveTo(timeAt(e.clientX)); });
  for (const type of ["pointerup", "pointercancel"]) strip.addEventListener(type, () => { scrubbing = false; });
  appSel.addEventListener("change", applyFilter);
  date.addEventListener("change", () => {
    if (!date.value) return;
    const [y, m, d] = date.value.split("-").map(Number);
    const t = new Date(moment);
    moveTo(new Date(y, m - 1, d, t.getHours(), t.getMinutes(), t.getSeconds(), t.getMilliseconds()).getTime(), true);
  });

  const jump = (target) => { if (target !== null) moveTo(target, true); };
  const onKey = (e) => {
    if (e.target.matches("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (stage?.ts_ms === moment) jump(e.key === "ArrowLeft" ? stage.prev_ms : stage.next_ms);
    }
    else if (e.key === "+" || e.key === "=") zoomAtCursor(1 / ZOOM_STEP);
    else if (e.key === "-") zoomAtCursor(ZOOM_STEP);
    else if (e.key === "Escape" && monitorId !== null) closeBig();
    else return;
    e.preventDefault();
  };
  const resizeObserver = new ResizeObserver(() => { if (stage) renderStage(); });
  resizeObserver.observe(wrap);
  document.addEventListener("keydown", onKey);
  cleanup = () => {
    clearTimeout(settleTimer);
    document.removeEventListener("keydown", onKey);
    resizeObserver.disconnect();
  };

  setDay(day);
  setView(moment - spanNow() / 2, spanNow());
  fetchStage();
}
