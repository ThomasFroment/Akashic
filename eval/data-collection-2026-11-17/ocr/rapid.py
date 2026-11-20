import json, sys, os, time, ctypes, collections, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
import numpy as np
from ocr_spike import render, score, SETS, THEMES
from rapidocr import RapidOCR, LangRec, OCRVersion, ModelType
configs = {
 "v5_latin_mobile": {"Rec.lang_type": LangRec.LATIN, "Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.model_type": ModelType.MOBILE, "Det.lang_type": LangRec.CH, "Det.ocr_version": OCRVersion.PPOCRV5, "Det.model_type": ModelType.MOBILE},
 "v6_small": {},
 "v6_medium": {"Rec.model_type": ModelType.MEDIUM, "Det.model_type": ModelType.MEDIUM},
}
res = {}
for name, params in configs.items():
    try:
        eng = RapidOCR(params={**params, "Global.log_level": "critical"})
    except Exception as e:
        res[name] = repr(e)[:300]; continue
    agg = collections.defaultdict(list); acc = []; ms = []
    for set_name, lines in SETS.items():
        for px in (11, 12, 13, 15, 18):
            for theme in THEMES:
                img = np.asarray(render(lines, px, theme, mono=set_name == "code"))
                t0 = time.perf_counter(); out = eng(img); ms.append((time.perf_counter() - t0) * 1000)
                got = list(out.txts) if out.txts else []
                # order lines by box top
                if out.boxes is not None:
                    order = np.argsort([b[0][1] for b in out.boxes]); got = [got[i] for i in order]
                cer, a = score(lines, got)
                agg[set_name].append(cer)
                if set_name == "fr" and a is not None: acc.append(a)
    res[name] = {"cer": {k: round(st.mean(v), 4) for k, v in agg.items()}, "fr_accent_recall": round(st.mean(acc), 3), "ms_median": round(st.median(ms))}
print(json.dumps(res, indent=1))
