// Client of one harness page. The server stamps every report with wall_ms and mono_ns.
const Q = new URLSearchParams(location.search), W = Q.get('w'), S = Q.get('s'), CELL = 8;
const post = (o) => fetch('/report', {method: 'POST', keepalive: true, body: JSON.stringify({w: W, s: S, ...o})}).then(r => r.json());
const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
const cfg = JSON.parse(document.getElementById('cfg').textContent);
const tagCanvas = document.getElementById('tag');
const video = document.querySelector('video');

// A visual change: ask for a tag id, draw it with the change, report once it is on screen.
async function change(kind, apply) {
  const {tag} = await post({type: 'tag_request'});
  apply();
  drawTag(tagCanvas, tag, CELL);
  await frame();
  post({type: 'change', kind, tag});
}

function lineEl(l) { const p = document.createElement('p'); p.className = 'line'; p.id = l.id; p.textContent = l.text; return p; }

// Control centres in viewport CSS px; the driver maps them to screen pixels with the window's own client rectangle.
let lastRect = '';
function reportRect() {
  const controls = {};
  for (const id of ['play', 'pip', 'sel']) {
    const e = document.getElementById(id);
    if (e) { const r = e.getBoundingClientRect(); controls[id] = [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)]; }
  }
  const rect = {dpr: devicePixelRatio, iw: innerWidth, ih: innerHeight, controls};
  const key = JSON.stringify(rect);
  if (key !== lastRect) { lastRect = key; post({type: 'rect', ...rect}); }
}

const seen = new Set();
const io = new IntersectionObserver((entries) => {
  for (const e of entries) e.isIntersecting ? seen.add(e.target.id) : seen.delete(e.target.id);
  clearTimeout(io.t); io.t = setTimeout(() => post({type: 'visible', ids: [...seen].sort()}), 250);
});
function watch(p) { io.observe(p); }

document.body.className = cfg.theme;
document.body.style.fontSize = cfg.font_px + 'px';
document.title = cfg.title;
const main = document.getElementById('lines');
for (const l of cfg.lines) { const p = lineEl(l); main.appendChild(p); watch(p); }
post({type: 'lines', lines: cfg.lines, title: cfg.title, chrome: [...document.querySelectorAll('header, aside')].map(e => e.innerText)});
change('load', () => {});
reportRect(); setInterval(reportRect, 2000);

let counter = 0;
if (cfg.period_ms) setInterval(() => {
  const l = cfg.more[counter++ % cfg.more.length];
  const line = {id: `${cfg.page}-N${counter}`, text: l};
  change('line', () => { const p = lineEl(line); main.appendChild(p); watch(p); if (!video) p.scrollIntoView({block: 'end'}); });
  post({type: 'lines', lines: [line]});
}, cfg.period_ms);

// commands from the driver: navigation and title change
setInterval(async () => {
  const r = await (await fetch('/poll?w=' + encodeURIComponent(W))).json();
  for (const c of r) {
    if (c.cmd === 'goto') location.href = c.url;
    else if (c.cmd === 'title') { change('title', () => { document.title = c.title; }); post({type: 'title', title: c.title}); }
  }
}, 250);

if (video) {
  const send = (ev) => post({type: 'media', ev, t: video.currentTime, paused: video.paused});
  for (const ev of ['play', 'pause', 'seeked', 'ended']) video.addEventListener(ev, () => send(ev));
  let last = -1;
  video.addEventListener('timeupdate', () => { const s = Math.floor(video.currentTime); if (s !== last) { last = s; send('tick'); } });
  document.getElementById('play').onclick = () => video.play().catch(e => post({type: 'refused', name: e.name}));
  const pip = document.getElementById('pip');
  pip.onclick = () => video.requestPictureInPicture().catch(e => post({type: 'refused_pip', name: e.name}));
  video.addEventListener('enterpictureinpicture', () => post({type: 'pip', state: 'enter'}));
  video.addEventListener('leavepictureinpicture', () => post({type: 'pip', state: 'leave'}));
  if (cfg.media) navigator.mediaSession.metadata = new MediaMetadata(cfg.media);
}
const sel = document.getElementById('sel');
if (sel) sel.addEventListener('change', () => post({type: 'select', value: sel.value}));
