import json, sys, os, time, ctypes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
import numpy as np
from PIL import Image
from ocr_spike import render, score, FR, EN
from rapidocr import RapidOCR
lines = (FR + EN) * 4
img = render(lines, 12, "light"); canvas = Image.new("RGB", (2560, 1440), (255,255,255)); canvas.paste(img, (0,0))
shown = [l for i, l in enumerate(lines) if 10 + i * int(12 * 1.6) + 12 < 1440]
out = {}
for threads in (2, 4):
    eng = RapidOCR(params={"Global.log_level": "critical", "EngineConfig.onnxruntime.intra_op_num_threads": threads})
    ts = []
    for _ in range(3):
        t0 = time.perf_counter(); r = eng(np.asarray(canvas)); ts.append((time.perf_counter() - t0) * 1000)
    got = [r.txts[i] for i in np.argsort([b[0][1] for b in r.boxes])]
    out[f"cpu_threads_{threads}"] = {"ms": [round(t) for t in ts], "cer": round(score(shown, got)[0], 4)}
print(json.dumps(out))
