import json, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ocr_spike import render, ocr, score, FR, EN, OcrEngine, Language
from PIL import Image
eng = OcrEngine.try_create_from_language(Language("fr-FR"))
lines = (FR + EN) * 4  # 80 lines
out = {}
for px, w in ((12, 2560), (13, 3840)):
    img = render(lines, px, "light")
    canvas = Image.new("RGB", (w, max(img.height, 1400)), (255, 255, 255)); canvas.paste(img, (0, 0))
    canvas = canvas.crop((0, 0, w, min(canvas.height, 2160 if w == 3840 else 1440)))
    shown = [l for i, l in enumerate(lines) if 10 + i * int(px * 1.6) + px < canvas.height]
    for f in (1.0, 1.5, 2.0):
        v = canvas if f == 1 else canvas.resize((int(w * f), int(canvas.height * f)), Image.LANCZOS)
        ts = []
        for _ in range(3):
            t0 = time.perf_counter(); got = ocr(eng, v); ts.append((time.perf_counter() - t0) * 1000)
        r0 = time.perf_counter(); canvas.resize((int(w * f), int(canvas.height * f)), Image.LANCZOS); rs = (time.perf_counter() - r0) * 1000
        out[f"{w}px_text{px}_x{f}"] = {"ocr_ms_median": round(sorted(ts)[1]), "resize_ms": round(rs), "cer": round(score(shown, got)[0], 4), "lines": len(shown)}
print(json.dumps(out, indent=1))
