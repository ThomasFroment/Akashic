"""Windows.Media.Ocr CER vs upscale factor and filter on synthetic UI text (in memory, no window).

Text: FR/EN/mixed UI-like lines in Segoe UI, anti-aliased, dark and light themes, 9-16 px font sizes.
For each font size x theme x language, renders a block of lines, then OCRs it at scale 1, 1.5, 2, 3, 4
with bilinear / bicubic / Lanczos, records CER (vs rendered text) and OCR milliseconds.
"""

import asyncio
import json
import statistics
import sys
import time
import unicodedata

from PIL import Image, ImageDraw, ImageFont
import winrt.windows.globalization as glob
import winrt.windows.graphics.imaging as gi
import winrt.windows.media.ocr as wocr
import winrt.windows.storage.streams as ws

LINES = {
    "en": [
        "Settings saved. Restart the app to apply the new theme.",
        "Download complete: 3 files, 128 MB in 42 seconds",
        "Reply to Minlo in #general about the raid tonight",
        "Error 404: the requested page could not be found",
    ],
    "fr": [
        "Paramètres enregistrés. Redémarrez l'application pour appliquer le thème.",
        "Téléchargement terminé : 3 fichiers, 128 Mo en 42 secondes",
        "Répondre à Minlo dans #général à propos du raid ce soir",
        "Erreur 404 : la page demandée est introuvable",
    ],
    "mixed": [
        "On se retrouve sur le serveur, check le channel avant le match",
        "J'ai push le fix sur la branche main, tu peux faire la review ?",
        "Le build a fail encore, je relance le pipeline demain matin",
        "Merci pour le feedback, je mets à jour le ticket tout de suite",
    ],
}
SIZES = [9, 10, 11, 12, 14, 16]
SCALES = [1.0, 1.5, 2.0, 3.0, 4.0]
FILTERS = {"bilinear": Image.BILINEAR, "bicubic": Image.BICUBIC, "lanczos": Image.LANCZOS}
THEMES = {"light": ((255, 255, 255), (30, 30, 30)), "dark": ((32, 33, 36), (230, 230, 230))}
FONT = "C:/Windows/Fonts/segoeui.ttf"


def fold(s: str) -> str:
    s = unicodedata.normalize("NFC", s).casefold()
    return " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in s).split())


def lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def render(lines: list[str], px: int, theme: str) -> Image.Image:
    bg, fg = THEMES[theme]
    font = ImageFont.truetype(FONT, px)
    line_h = round(px * 1.6)
    w = max(round(font.getlength(t)) for t in lines) + 24
    img = Image.new("RGB", (w, line_h * len(lines) + 16), bg)
    d = ImageDraw.Draw(img)
    for i, t in enumerate(lines):
        d.text((12, 8 + i * line_h), t, font=font, fill=fg)
    return img


async def ocr(engine, img: Image.Image) -> str:
    rgba = img.convert("RGBA")
    raw = rgba.tobytes("raw", "BGRA")
    writer = ws.DataWriter()
    writer.write_bytes(raw)
    buf = writer.detach_buffer()
    bmp = gi.SoftwareBitmap.create_copy_from_buffer(buf, gi.BitmapPixelFormat.BGRA8, rgba.width, rgba.height, gi.BitmapAlphaMode.PREMULTIPLIED)
    res = await engine.recognize_async(bmp)
    return "\n".join(line.text for line in res.lines)


async def main() -> None:
    engines = {tag: wocr.OcrEngine.try_create_from_language(glob.Language(tag)) for tag in ("fr-FR", "en-US")}
    rows = []
    for lang, lines in LINES.items():
        engine = engines["en-US" if lang == "en" else "fr-FR"]
        ref = fold(" ".join(lines))
        for px in SIZES:
            for theme in THEMES:
                base = render(lines, px, theme)
                for scale in SCALES:
                    for fname, f in (FILTERS.items() if scale != 1.0 else [("none", None)]):
                        img = base if f is None else base.resize((round(base.width * scale), round(base.height * scale)), f)
                        t0 = time.perf_counter()
                        hyp = fold(await ocr(engine, img))
                        ms = (time.perf_counter() - t0) * 1000
                        rows.append({"lang": lang, "px": px, "theme": theme, "scale": scale, "filter": fname,
                                     "cer": lev(ref, hyp) / len(ref), "ms": ms, "mpix": img.width * img.height / 1e6})
    json.dump(rows, open(sys.argv[1], "w"), indent=0)
    # summary: CER by px x (scale, filter), averaged over lang and theme
    keys = sorted({(r["scale"], r["filter"]) for r in rows})
    print("px  " + "  ".join(f"{s}x{f[:4]}" for s, f in keys))
    for px in SIZES:
        cells = [statistics.mean(r["cer"] for r in rows if r["px"] == px and (r["scale"], r["filter"]) == k) for k in keys]
        print(f"{px:<3} " + "  ".join(f"{c:7.3f}" for c in cells))
    print("ms/Mpix by scale:", {s: round(statistics.mean(r["ms"] / r["mpix"] for r in rows if r["scale"] == s), 1) for s in SCALES})
    print("ms by scale:", {s: round(statistics.median(r["ms"] for r in rows if r["scale"] == s), 1) for s in SCALES})
    for lang in LINES:
        print(lang, {f"{s}x{f[:4]}": round(statistics.mean(r["cer"] for r in rows if r["lang"] == lang and r["px"] <= 11 and (r["scale"], r["filter"]) == (s, f)), 3) for s, f in keys})


asyncio.run(main())
