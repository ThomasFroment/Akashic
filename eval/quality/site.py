"""Local site of the harness: pages, report endpoint, videos and the vault, on 127.0.0.1 only.

Pages load nothing from another host. Every report is stamped with wall_ms and mono_ns and written
to truth/pages.jsonl. Page content is a pure function of (seed, window, step), so it can be
rebuilt, but the shown line texts are logged too.
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import numpy as np

import assets
import tags
from common import ASSETS, CANARY_RGB, CANARY_TEXT, QUALITY, JsonlWriter

SITE_DIR = QUALITY / "site"
PERIOD_MS = 20_000
TERM_SHARE = 0.2  # of the speech clips of a video: names and terms read by a TTS voice
VIDEO_SLACK_S = 60  # the video outlasts its span: late stops
# 32 syllables, 8 per nonce: 40 bits, so a day of lines and titles does not collide
SYLLABLES = ("zor", "vex", "kal", "mun", "tir", "bol", "qua", "dren", "fyl", "gro", "hesk", "nim", "pax", "wul", "jek", "rho",
             "sab", "tov", "uln", "yem", "cid", "lop", "bav", "nur", "dax", "fel", "gim", "hok", "reb", "sil", "tay", "vun")
NONCE_SYLLABLES = 8
TITLES = {
    "article": ("The long road to the quiet harbour", "Le long chemin vers le port tranquille", "Harbour notes, notes du port"),
    "forum": ("Thread: which build survives the late game", "Fil : quel build tient en fin de partie", "Thread / fil: late game"),
    "docs_code": ("Docs: parsing frames in a worker", "Docs : analyser des trames dans un worker", "Docs / guide: worker frames"),
    "chat_growing": ("#general live chat", "#general discussion en direct", "#general chat / direct"),
    "assistant": ("Assistant: ask your memory", "Assistant : interroge ta mémoire", "Assistant / assistant"),
    "select": ("Settings: pick a profile", "Réglages : choisis un profil", "Settings / réglages"),
}
CODE = "def window_text(frame):\n    lines = [row.strip() for row in frame.rows]\n    return '\\n'.join(l for l in lines if l)\n"
SERIES = ("Quilvane", "Mornhollow", "Estrel")


def nonce(*parts: object) -> str:
    bits = int.from_bytes(hashlib.sha256("/".join(map(str, parts)).encode()).digest()[:NONCE_SYLLABLES * 5 // 8 + 1], "big")
    return "".join(SYLLABLES[(bits >> (5 * i)) & 31] for i in range(NONCE_SYLLABLES))


class Site:
    def __init__(self, truth_dir: Path, run_dir: Path, seed: int, vault_port: int, port: int = 0) -> None:
        self.log = JsonlWriter(truth_dir / "pages.jsonl")
        self.seed = seed
        self.run_dir = run_dir
        self.sentences = json.loads((ASSETS / "sentences.json").read_text(encoding="utf-8"))
        self.manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
        self.commands: dict[str, list[dict]] = {}
        self.next_tag = 1
        self.lock = threading.Lock()
        self.videos: dict[str, dict] = {}
        self.rects: dict[str, dict] = {}
        self.issued: dict[str, str] = {}
        self.taken: set[str] = set()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.vault = ThreadingHTTPServer(("127.0.0.1", vault_port), self._vault_handler())
        self.port, self.vault_port = self.server.server_address[1], self.vault.server_address[1]

    def start(self) -> None:
        for s in (self.server, self.vault):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def stop(self) -> None:
        for s in (self.server, self.vault):
            s.shutdown()
            s.server_close()
        self.log.close()

    def command(self, window: str, **cmd: object) -> None:
        with self.lock:
            self.commands.setdefault(window, []).append(cmd)

    def alloc_tags(self, n: int) -> int:
        with self.lock:
            base, self.next_tag = self.next_tag, self.next_tag + n
        return base

    def unique_nonce(self, *parts: object) -> str:
        """The nonce of `parts`, salted until no other line or title of the run has the same one."""
        key = "/".join(map(str, parts))
        with self.lock:
            if key not in self.issued:
                salt = 0
                while (n := nonce(*parts, salt) if salt else nonce(*parts)) in self.taken:
                    salt += 1
                self.issued[key] = n
                self.taken.add(n)
            return self.issued[key]

    def video_query(self, step: dict) -> dict[str, str]:
        v = step["params"]["video"]
        return {"v": v["title_id"], "sec": str(v["span_s"] + VIDEO_SLACK_S), "ep": str(int(v["episode"])), "se": str(v["season"]), "no": str(v["number"])}

    def url(self, window: str, step: dict) -> str:
        p = step["params"]
        kind = p.get("page", "assistant" if p.get("app") == "assistant" else "article")
        q = f"w={quote(window, safe='')}&s={quote(step['id'], safe='')}&k={kind}&lang={p.get('lang', 'en')}&px={p.get('font_px', 16)}&theme={p.get('theme', 'dark')}"
        if p.get("popups"):
            q += "&select=1"
        if p.get("video"):
            q += "&" + "&".join(f"{k}={v}" for k, v in self.video_query(step).items())
        return f"http://127.0.0.1:{self.port}/p?{q}"

    def prepare_video(self, step: dict) -> None:
        """Build the video of a step now, so no page request waits for it."""
        self.video(self.video_query(step))

    # --- content ---

    def lines(self, window: str, step: str, lang: str, n: int) -> list[dict]:
        rng = random.Random(f"{self.seed}/{window}/{step}")
        pool = self.sentences[lang if lang in self.sentences else "en"]
        return [{"id": f"{step}-L{i}", "text": f"{rng.choice(pool)} {self.unique_nonce(self.seed, window, step, i)}"} for i in range(n)]

    def page(self, q: dict[str, str]) -> bytes:
        w, s, kind, lang = q["w"], q["s"], q["k"], q["lang"]
        theme_title = TITLES.get(kind, TITLES["article"])[{"en": 0, "fr": 1}.get(lang, 2)]
        title = f"{self.unique_nonce(self.seed, w, s, 'title')} {theme_title}"
        cfg = {
            "page": s, "theme": q["theme"], "font_px": int(q["px"]), "title": title,
            "lines": self.lines(w, s, lang, 14), "more": [x["text"] for x in self.lines(w, s + "m", lang, 60)],
            "period_ms": PERIOD_MS,
        }
        side = "".join(f"<li>{html.escape(t[:60])}</li>" for t in self.sentences["en"][:6])
        body = {"article": "<h2>Chapter</h2>", "forum": f"<div class='post'>Reply by user_{nonce(s)}</div>", "docs_code": f"<pre>{html.escape(CODE)}</pre>",
                "chat_growing": "", "assistant": "<div class='post'>Ask anything about your day.</div>", "select": ""}.get(kind, "")
        extra = ""
        if q.get("select"):
            extra = "<select id='sel'><option>profile-a</option><option>profile-b</option><option>profile-c</option></select>"
        if "v" in q:
            video = self.video(q)
            cfg["media"] = video["media"]
            extra += (f"<video src='/media/{video['file']}' preload='auto'></video><button id='play'>Play</button><button id='pip'>Picture in picture</button>"
                      f"<h3>{html.escape(video['media']['title'])}</h3><p>{html.escape(video['media']['artist'])}</p>"
                      f"<p>Description: {html.escape(self.lines(w, s + 'd', lang, 1)[0]['text'])}</p><div class='post'>Comment: {html.escape(self.lines(w, s + 'c', lang, 1)[0]['text'])}</div>")
        cls = "chat" if kind == "chat_growing" else ""
        return (f"<!doctype html><meta charset='utf-8'><link rel='stylesheet' href='/static/page.css'><title>{html.escape(title)}</title>"
                f"<header><h1>{html.escape(theme_title)}</h1></header><div class='wrap'><main class='{cls}'>{body}{extra}<div id='lines'></div></main>"
                f"<aside><b>Related</b><ul>{side}</ul></aside></div><canvas id='tag'></canvas>"
                f"<script id='cfg' type='application/json'>{json.dumps(cfg)}</script><script src='/static/tags.js'></script>"
                f"<script src='/static/page.js'></script>").encode()

    def video(self, q: dict[str, str]) -> dict:
        vid = q["v"]
        with self.lock:
            if vid in self.videos:
                return self.videos[vid]
        seconds = int(q["sec"])
        episode = q["ep"] == "1"
        series = SERIES[int(q["se"]) % len(SERIES)]
        title_nonce = self.unique_nonce(self.seed, "video", vid)
        title = f"{series} S{int(q['se']):02d}E{int(q['no']):02d} {title_nonce}" if episode else f"{title_nonce} {series} walkthrough"
        file = f"{vid}.mp4"
        base = self.alloc_tags(seconds + 1)
        plan = build_video(self.run_dir / "media" / file, seconds, base, title, self.manifest, self.seed)
        info = {"file": file, "tag_base": base, "seconds": seconds,
                "media": {"title": title, "artist": f"Channel {self.unique_nonce(self.seed, 'ch', vid)}", "album": series if episode else ""}}
        self.log.write(type="video_built", video=vid, tag_base=base, seconds=seconds, title=title, episode=episode, plan=plan,
                       season=int(q["se"]) if episode else None, number=int(q["no"]) if episode else None)
        with self.lock:
            self.videos[vid] = info
        return info

    # --- http ---

    def _handler(self):
        site = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D102
                pass

            def _send(self, code: int, data: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                if u.path == "/p":
                    self._send(200, site.page(q), "text/html; charset=utf-8")
                elif u.path == "/static/page.js":
                    self._send(200, (SITE_DIR / "page.js").read_bytes(), "text/javascript")
                elif u.path == "/static/page.css":
                    self._send(200, (SITE_DIR / "page.css").read_bytes(), "text/css")
                elif u.path == "/static/tags.js":
                    self._send(200, tags.JS_SOURCE.encode(), "text/javascript")
                elif u.path == "/poll":
                    with site.lock:
                        cmds = site.commands.pop(q["w"], [])
                    self._send(200, json.dumps(cmds).encode(), "application/json")
                elif u.path.startswith("/media/"):
                    self._media(site.run_dir / "media" / Path(u.path).name)
                else:
                    self._send(404, b"", "text/plain")

            def _media(self, path: Path) -> None:
                data = path.read_bytes()
                start, end = 0, len(data) - 1
                rng = self.headers.get("Range")
                if rng:
                    a, _, b = rng.removeprefix("bytes=").partition("-")
                    start, end = int(a or 0), int(b) if b else len(data) - 1
                self.send_response(206 if rng else 200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                self.wfile.write(data[start:end + 1])

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                if body.get("type") == "tag_request":
                    self._send(200, json.dumps({"tag": site.alloc_tags(1)}).encode(), "application/json")
                    return
                if body.get("type") == "rect":
                    site.rects[body["w"]] = body
                site.log.write(**body)
                self._send(200, b"{}", "application/json")

        return H

    def _vault_handler(self):
        class V(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                swatch = "#%02x%02x%02x" % CANARY_RGB
                page = (f"<!doctype html><title>{CANARY_TEXT} vault</title><body style='font-size:28px'>"
                        f"<p>{CANARY_TEXT}</p><div style='width:300px;height:300px;background:{swatch}'></div></body>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

        return V


def build_video(path: Path, seconds: int, tag_base: int, title: str, manifest: dict, seed: int) -> list[dict]:
    """1 fps H.264 video whose frame at second t carries tag tag_base + t, with narrator speech and
    now and then a spoken name or term. Returns the speech plan (clip id and start in ms)."""
    import av
    from PIL import Image, ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"{seed}/{title}")
    narrators = [c for c in manifest["clips"] if c["kind"] == "speech" and manifest["speakers"][c["speaker"]]["role"] == "video"]
    terms = [c for c in manifest["clips"] if c["kind"] == "tts"]
    banks: dict[str, np.ndarray] = {}
    speech, plan = [], []
    while sum(len(x) for x in speech) < seconds * assets.SR:
        c = rng.choice(terms if rng.random() < TERM_SHARE else narrators)
        banks.setdefault(c["bank"], assets.load_bank(c["bank"]))
        plan.append({"id": c["id"], "t_ms": round(sum(len(x) for x in speech) * 1000 / assets.SR)})
        speech += [assets.clip_samples(c, banks), np.zeros(assets.SR // 2, np.float32)]
    audio = np.concatenate(speech)[: seconds * assets.SR]
    with av.open(str(path), "w", format="mp4") as out:
        v = out.add_stream("libx264", rate=2)
        v.width, v.height, v.pix_fmt = 640, 360, "yuv420p"
        v.options = {"crf": "18", "preset": "veryfast", "g": "2"}
        a = out.add_stream("aac", rate=assets.SR, layout="mono")
        for t in range(seconds):
            img = Image.new("RGB", (640, 360), (30 + (t * 37) % 120, 60, 90 + (t * 11) % 100))
            ImageDraw.Draw(img).text((20, 150), f"{title}  t={t}s", fill=(255, 255, 255))
            frame = np.asarray(img).copy()
            tags.paste(frame, tag_base + t, 520, 250, cell=8)
            for _ in range(2):
                f = av.VideoFrame.from_ndarray(frame, format="rgb24")
                out.mux(v.encode(f))
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=assets.SR, frame_size=1024)
        for i in range(0, len(pcm), 1024):
            af = av.AudioFrame.from_ndarray(pcm[i:i + 1024][None, :], format="s16", layout="mono")
            af.sample_rate = assets.SR
            for f in resampler.resample(af):
                out.mux(a.encode(f))
        out.mux(v.encode(None))
        out.mux(a.encode(None))
    return plan
