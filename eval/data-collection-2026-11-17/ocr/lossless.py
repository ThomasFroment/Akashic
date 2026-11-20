import io, json, time, sys, os, ctypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
from PIL import Image
from ocr_spike import render, ocr, score, FR, EN, OcrEngine, Language
lines = (FR + EN) * 4
img = render(lines, 12, "dark"); canvas = Image.new("RGB", (2560, 1440), (32, 33, 36)); canvas.paste(img, (0, 0))
shown = [l for i, l in enumerate(lines) if 10 + i * 19 + 12 < 1440]
eng = OcrEngine.try_create_from_language(Language("fr-FR"))
def up(v): return v.resize((v.width * 2, v.height * 2), Image.LANCZOS)
out = {}
for name, kw in {"webp_q70_m2 (today)": dict(format="WEBP", quality=70, method=2),
                 "webp_lossless_m0": dict(format="WEBP", lossless=True, quality=0, method=0),
                 "png_level1": dict(format="PNG", compress_level=1)}.items():
    t0 = time.perf_counter(); b = io.BytesIO(); canvas.save(b, **kw); enc = (time.perf_counter() - t0) * 1000
    back = Image.open(io.BytesIO(b.getvalue())).convert("RGB")
    out[name] = {"kb": round(len(b.getvalue()) / 1024), "encode_ms": round(enc),
                 "cer_x2": round(score(shown, ocr(eng, up(back)))[0], 4)}
out["original_x2"] = {"cer_x2": round(score(shown, ocr(eng, up(canvas)))[0], 4)}
print(json.dumps(out, indent=1))
