"""Windows OCR on rendered screen-like text: recognizer language, size, theme, upscaling.
Ground truth is text written for this spike. Numbers only."""

from __future__ import annotations

import asyncio
import ctypes
import json
import sys
import time
import unicodedata

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from winrt.windows.globalization import Language
from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.security.cryptography import CryptographicBuffer

ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)

FR = [
    "Réunion déplacée à jeudi après-midi, salle du deuxième étage",
    "Tu as vu la nouvelle mise à jour ? Les graphismes sont très améliorés",
    "Paramètres avancés : désactiver la synchronisation à l'ouverture",
    "Ça marche, on se retrouve à 21 h sur le serveur habituel",
    "Évènements récents : téléchargement terminé, échec de la connexion",
    "Le projet « Forêt noire » a été archivé par Élodie hier soir",
    "Où est passé le fichier coûts_prévisionnels_2026.xlsx ?",
    "Mode hors ligne activé — les modifications seront envoyées plus tard",
    "Quête terminée : récupérer l'épée du chevalier déchu",
    "Bientôt disponible : sous-titres français et doublage québécois",
]
EN = [
    "Meeting moved to Thursday afternoon, second floor room",
    "Did you see the new update? The graphics are much better now",
    "Advanced settings: disable synchronization on startup",
    "Pull request #4821 merged into main by a reviewer",
    "Recent events: download finished, connection failed",
    "The Minecraft Rust project was renamed to ferrocraft yesterday",
    "Where did the projected_costs_2026.xlsx file go?",
    "Offline mode enabled - changes will be sent later",
    "Quest complete: retrieve the fallen knight's sword",
    "Coming soon: French subtitles and Quebec dubbing",
]
CODE = [
    "def compute_score(items: list[int]) -> float:",
    "    return sum(x * 0.5 for x in items if x > 0)",
    "for (let i = 0; i < rows.length; i++) { total += rows[i]; }",
    "SELECT id, start_ms FROM audio_segments WHERE status = 'done';",
]
SETS = {"fr": FR, "en": EN, "code": CODE}
THEMES = {"light": ((255, 255, 255), (20, 20, 20)), "dark": ((32, 33, 36), (225, 225, 225))}
SIZES = [11, 12, 13, 15, 18]  # px: ~100% UI (11-13), 125% (15), 150% (18)


def render(lines, size, theme, mono=False):
    bg, fg = THEMES[theme]
    font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf" if mono else "C:/Windows/Fonts/segoeui.ttf", size)
    h = int(size * 1.6)
    img = Image.new("RGB", (int(size * 0.62 * max(map(len, lines))) + 40, h * len(lines) + 20), bg)
    d = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        d.text((20, 10 + i * h), line, font=font, fill=fg)
    return img


_loop = asyncio.new_event_loop()


def ocr(engine, image):
    lim = OcrEngine.max_image_dimension
    if max(image.size) > lim:
        image = image.copy(); image.thumbnail((lim, lim))
    bgra = np.asarray(image.convert("RGBA"))[:, :, [2, 1, 0, 3]].tobytes()
    bmp = SoftwareBitmap.create_copy_from_buffer(CryptographicBuffer.create_from_byte_array(bgra),
                                                 BitmapPixelFormat.BGRA8, image.width, image.height,
                                                 BitmapAlphaMode.PREMULTIPLIED)
    res = _loop.run_until_complete(engine.recognize_async(bmp))
    return [ln.text for ln in res.lines]


def lev(a, b):
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def score(truth, got):
    t = "\n".join(truth); g = "\n".join(got)
    acc_truth = [c for c in t if unicodedata.decomposition(c)]
    acc_got = sum(1 for c in acc_truth if c in g)
    return lev(t, g) / max(1, len(t)), (acc_got / len(acc_truth)) if acc_truth else None


def variants(img, theme):
    yield "raw", img
    yield "x1.5", img.resize((int(img.width * 1.5), int(img.height * 1.5)), Image.LANCZOS)
    yield "x2", img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    if theme == "dark":
        from PIL import ImageOps
        yield "invert", ImageOps.invert(img)


if __name__ == "__main__":
    engines = {tag: OcrEngine.try_create_from_language(Language(tag)) for tag in ("fr-FR", "en-US")}
    rows = []
    for set_name, lines in SETS.items():
        for size in SIZES:
            for theme in THEMES:
                img = render(lines, size, theme, mono=set_name == "code")
                for vname, v in variants(img, theme):
                    for tag, eng in engines.items():
                        t0 = time.perf_counter()
                        got = ocr(eng, v)
                        ms = (time.perf_counter() - t0) * 1000
                        cer, acc = score(lines, got)
                        rows.append(dict(set=set_name, px=size, theme=theme, variant=vname, rec=tag,
                                         cer=round(cer, 4), accents=None if acc is None else round(acc, 3),
                                         ms=round(ms, 1)))
    json.dump(rows, open(sys.argv[1], "w"), indent=0)
    # summary
    import collections
    agg = collections.defaultdict(list)
    for r in rows:
        agg[(r["set"], r["rec"], r["variant"])].append(r["cer"])
    for k in sorted(agg):
        print(k, "mean CER", round(float(np.mean(agg[k])), 4))
