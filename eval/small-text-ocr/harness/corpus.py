"""Synthetic window corpus: harness-like pages rendered off-screen, with ground-truth lines and boxes.

Renderers (same text, same layout):
  chromium       Edge headless (--headless=new --screenshot --dump-dom), default anti-aliasing
                 (on this PC: LCD/subpixel, i.e. ClearType-like colour fringes)
  chromium_gray  same, with --disable-lcd-text (grayscale anti-aliasing)
  pillow         Pillow + segoeui.ttf at the same boxes and sizes (grayscale AA, the earlier research)
Scales 1.0 (the 2560x1440 side monitors) and 1.25 (the 4K primary). No window is shown or focused:
headless Edge with its own throwaway profile, file:// pages, nothing fetched from the network.

    uv run python eval/small-text-ocr/harness/corpus.py [--rebuild]
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared import CORPUS, QUALITY  # noqa: E402

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
SIZES = [9, 10, 11, 12, 13, 14, 16, 18]
THEMES = {"dark": ("#16181d", "#d8dbe0"), "light": ("#fafafa", "#1d1f23"), "color": ("#2b5a8a", "#f2f4f7")}
LANGS = ("en", "fr", "mixed")
SCALES = (1.0, 1.25)
RENDERERS = ("chromium", "chromium_gray", "pillow")
WINDOW_CSS = (1400, 720)
N_LINES = 10
MAX_SENTENCE = 95
TITLES = {"en": "The long road to the quiet harbour", "fr": "Le long chemin vers le port tranquille", "mixed": "Harbour notes, notes du port"}
SYLLABLES = ("zor", "vex", "kal", "mun", "tir", "bol", "qua", "dren", "fyl", "gro", "hesk", "nim", "pax", "wul", "jek", "rho",
             "sab", "tov", "uln", "yem", "cid", "lop", "bav", "nur", "dax", "fel", "gim", "hok", "reb", "sil", "tay", "vun")
FONTS = {False: "C:/Windows/Fonts/segoeui.ttf", True: "C:/Windows/Fonts/segoeuib.ttf"}

CSS = """body{margin:0;font-family:Segoe UI,Arial,sans-serif;line-height:1.45}
body.dark{background:#16181d;color:#d8dbe0}body.light{background:#fafafa;color:#1d1f23}body.color{background:#2b5a8a;color:#f2f4f7}
header{padding:10px 20px;border-bottom:1px solid #8884}header h1{margin:0;font-size:1.5em}
.wrap{display:flex;gap:24px;padding:16px 20px}main{flex:1;min-width:0}
aside{width:18%;font-size:.9em;border-left:1px solid #8884;padding-left:16px}
.line{margin:0 0 .7em;white-space:nowrap;overflow:hidden}"""

JS = """const out=[];for(const e of document.querySelectorAll('h1, aside b, aside li, .line')){
const g=document.createRange();g.selectNodeContents(e);const r=g.getBoundingClientRect();const cs=getComputedStyle(e);
out.push({k:e.classList.contains('line')?'line':'chrome',id:e.id||'',text:e.textContent,box:[r.left,r.top,r.right,r.bottom],
fs:parseFloat(cs.fontSize),bold:parseInt(cs.fontWeight)>=600,fits:e.scrollWidth<=e.clientWidth&&r.right<=innerWidth&&r.bottom<=innerHeight});}
document.getElementById('boxes').textContent=JSON.stringify({dpr:devicePixelRatio,iw:innerWidth,ih:innerHeight,els:out});"""


def nonce(*parts: object) -> str:
    bits = int.from_bytes(hashlib.sha256("/".join(map(str, parts)).encode()).digest()[:6], "big")
    return "".join(SYLLABLES[(bits >> (5 * i)) & 31] for i in range(8))


def truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:")


def page_specs() -> list[dict]:
    sentences = json.loads((QUALITY / "assets" / "sentences.json").read_text(encoding="utf-8"))
    specs = []
    for lang in LANGS:
        for theme in THEMES:
            for px in SIZES:
                pid = f"{lang}-{theme}-{px}"
                rng = random.Random(pid)
                lines = [{"id": f"L{i}", "text": f"{truncate(rng.choice(sentences[lang]), MAX_SENTENCE)} {nonce(pid, i)}"} for i in range(N_LINES)]
                side = [truncate(t, 60) for t in sentences["en"][:6]]
                specs.append({"id": pid, "lang": lang, "theme": theme, "px": px, "lines": lines, "side": side})
    return specs


def page_html(spec: dict) -> str:
    lines = "".join(f"<p class='line' id='{ln['id']}'>{html.escape(ln['text'])}</p>" for ln in spec["lines"])
    side = "".join(f"<li>{html.escape(t)}</li>" for t in spec["side"])
    return (f"<!doctype html><html lang='{spec['lang']}' translate='no'><meta charset='utf-8'><style>{CSS}</style>"
            f"<body class='{spec['theme']}' style='font-size:{spec['px']}px'><header><h1>{html.escape(TITLES[spec['lang']])}</h1></header>"
            f"<div class='wrap'><main>{lines}</main><aside><b>Related</b><ul>{side}</ul></aside></div>"
            f"<div id='boxes' style='display:none'></div><script>{JS}</script></body></html>")


def scale_dir(scale: float) -> str:
    return f"s{round(scale * 100)}"


def edge_shot(html_path: Path, png: Path, scale: float, gray: bool, profile: Path) -> dict:
    args = [str(EDGE), "--headless=new", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--disable-sync", "--disable-background-networking", "--disable-component-update", "--disable-extensions",
            "--hide-scrollbars", f"--force-device-scale-factor={scale}", f"--window-size={WINDOW_CSS[0]},{WINDOW_CSS[1]}",
            f"--screenshot={png}", "--dump-dom"]
    if gray:
        args.append("--disable-lcd-text")
    args.append(html_path.as_uri())
    for attempt in range(3):
        out = subprocess.run(args, capture_output=True, timeout=120, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
        dom = out.stdout.decode("utf-8", "replace")
        start = dom.find("<div id=\"boxes\" style=\"display:none\">")
        if start >= 0 and png.is_file():
            body = dom[start:].split(">", 1)[1].split("</div>", 1)[0]
            return json.loads(html.unescape(body))
        time.sleep(0.5 + attempt)
    raise RuntimeError(f"edge failed on {html_path.name}")


def ink_stats(img: Image.Image, bg: str, boxes: list[list[int]]) -> tuple[float, float]:
    """(share of ink pixels inside the text boxes, share of ink pixels with colour fringes)."""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    bgc = np.array([int(bg[i : i + 2], 16) for i in (1, 3, 5)])
    ink = np.abs(a - bgc).max(axis=2) > 40
    inside = np.zeros_like(ink)
    for l, t, r, b in boxes:
        inside[max(t - 2, 0) : b + 2, max(l - 2, 0) : r + 2] = True
    n = int(ink.sum())
    chroma = (a.max(axis=2) - a.min(axis=2)) > 24
    # Only meaningful on the gray themes: on the coloured theme, plain blending of fg and bg is chroma too.
    return (float((ink & inside).sum()) / n if n else 1.0, float((ink & chroma).sum()) / n if n else 0.0)


def device_box(box: list[float], s: float) -> list[int]:
    return [int(np.floor(box[0] * s)), int(np.floor(box[1] * s)), int(np.ceil(box[2] * s)), int(np.ceil(box[3] * s))]


def pillow_page(spec: dict, layout: dict, s: float) -> Image.Image:
    bg, fg = THEMES[spec["theme"]]
    img = Image.new("RGB", (round(WINDOW_CSS[0] * s), round(WINDOW_CSS[1] * s)), bg)
    d = ImageDraw.Draw(img)
    for e in layout["els"]:
        font = ImageFont.truetype(FONTS[e["bold"]], e["fs"] * s)
        d.text((e["box"][0] * s, e["box"][1] * s), e["text"], font=font, fill=fg, anchor="la")
    return img


def build(rebuild: bool = False) -> dict:
    index = CORPUS / "pages.json"
    if index.is_file() and not rebuild:
        return json.loads(index.read_text(encoding="utf-8"))
    if CORPUS.exists():
        shutil.rmtree(CORPUS)
    (CORPUS / "html").mkdir(parents=True)
    specs = page_specs()
    jobs = []
    for spec in specs:
        hp = CORPUS / "html" / f"{spec['id']}.html"
        hp.write_text(page_html(spec), encoding="utf-8")
        for s in SCALES:
            for r in ("chromium", "chromium_gray"):
                (CORPUS / r / scale_dir(s)).mkdir(parents=True, exist_ok=True)
                jobs.append((spec, hp, s, r))
    t0 = time.perf_counter()
    workers = 6
    profiles = [CORPUS / "_profiles" / f"w{i}" for i in range(workers)]

    def run(i_job):
        i, (spec, hp, s, r) = i_job
        png = CORPUS / r / scale_dir(s) / f"{spec['id']}.png"
        return (spec["id"], s, r), edge_shot(hp, png, s, r == "chromium_gray", profiles[i % workers])

    with ThreadPoolExecutor(workers) as ex:
        layouts = dict(ex.map(run, enumerate(jobs)))
    shutil.rmtree(CORPUS / "_profiles", ignore_errors=True)
    pages = []
    aa: dict[str, list[float]] = {}
    coverage = []
    for spec in specs:
        for s in SCALES:
            layout = layouts[(spec["id"], s, "chromium")]
            gray = layouts[(spec["id"], s, "chromium_gray")]
            assert abs(layout["dpr"] - s) < 1e-6 and layout["iw"] == gray["iw"], (spec["id"], s)
            assert [e["box"] for e in layout["els"]] == [e["box"] for e in gray["els"]], (spec["id"], s)
            (CORPUS / "pillow" / scale_dir(s)).mkdir(parents=True, exist_ok=True)
            pillow_page(spec, layout, s).save(CORPUS / "pillow" / scale_dir(s) / f"{spec['id']}.png")
            texts = {ln["id"]: ln["text"] for ln in spec["lines"]}
            lines = [{"id": e["id"], "text": texts[e["id"]], "box": device_box(e["box"], s), "fits": e["fits"]}
                     for e in layout["els"] if e["k"] == "line"]
            boxes = [device_box(e["box"], s) for e in layout["els"]]
            size = None
            for r in RENDERERS:
                with Image.open(CORPUS / r / scale_dir(s) / f"{spec['id']}.png") as im:
                    size = size or im.size
                    assert im.size == size, (r, spec["id"], im.size, size)
                    inside, fringe = ink_stats(im, THEMES[spec["theme"]][0], boxes)
                coverage.append(inside)
                if spec["theme"] != "color":
                    aa.setdefault(f"{r}/{spec['theme']}", []).append(fringe)
            pages.append({"id": spec["id"], "lang": spec["lang"], "theme": spec["theme"], "px": spec["px"], "scale": s,
                          "dir": scale_dir(s), "size": list(size), "viewport_css": [layout["iw"], layout["ih"]], "lines": lines})
    out = {"renderers": list(RENDERERS), "pages": pages,
           "fringe_share_of_ink": {k: round(float(np.mean(v)), 4) for k, v in sorted(aa.items())},
           "ink_inside_boxes_min": round(min(coverage), 4), "ink_inside_boxes_mean": round(float(np.mean(coverage)), 4),
           "lines_fitting": sum(ln["fits"] for p in pages for ln in p["lines"]), "lines_total": sum(len(p["lines"]) for p in pages),
           "edge": subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Item '{EDGE}').VersionInfo.ProductVersion"],
                                  capture_output=True, text=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip(),
           "build_s": round(time.perf_counter() - t0, 1)}
    index.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    c = build(rebuild="--rebuild" in sys.argv)
    print({k: v for k, v in c.items() if k != "pages"})
    print("pages per renderer:", len(c["pages"]), "sizes:", sorted({tuple(p["size"]) for p in c["pages"]}))
