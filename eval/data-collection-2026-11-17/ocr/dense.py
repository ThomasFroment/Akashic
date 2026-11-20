"""Dense 1440p window: four columns of 12 px text, fr-FR, raw / 1.5x / 2x."""
import json, sys, os, time, ctypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
from PIL import Image
from ocr_spike import render, ocr, score, FR, EN, OcrEngine, Language
eng = OcrEngine.try_create_from_language(Language("fr-FR"))
lines = (FR + EN) * 4
col = render(lines, 12, "light")
canvas = Image.new("RGB", (2560, 1440), (255, 255, 255))
for k in range(4): canvas.paste(col.crop((0, 0, 620, 1440)), (k * 640, 0))
truth = []
for i, l in enumerate(lines):
    if 10 + i * 19 + 12 < 1440: truth.append(l)
out = {}
for f in (1.0, 1.5, 2.0):
    t0 = time.perf_counter(); v = canvas if f == 1 else canvas.resize((int(2560 * f), int(1440 * f)), Image.LANCZOS); rs = time.perf_counter() - t0
    ts = []
    for _ in range(3):
        t0 = time.perf_counter(); got = ocr(eng, v); ts.append(time.perf_counter() - t0)
    words = sum(len(x.split()) for x in got)
    out[f"x{f}"] = {"ocr_ms": round(sorted(ts)[1] * 1000), "resize_ms": round(rs * 1000), "words_read": words,
                    "cer_vs_4_columns": round(score(truth * 4, sorted(got))[0], 3)}
print(json.dumps(out))
