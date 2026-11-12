"""Dev: what segmentation decodes at the hidden voice's frames in C2's 24 merge cases."""
import json, pickle, sys
from collections import Counter
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
from C2_lib import PER_FRAME, SR, window_starts
from common import FIXTURE, OUT
ix = {e["id"]: e for e in json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))["segments"]}
L = pickle.loads((OUT / "C2_calib" / "logits.pkl").read_bytes())
det = json.loads((OUT / "C2_calib" / "details.json").read_text(encoding="utf-8"))
merge = [r for r in det["segments"] if r["wrong"] and r["single"] is None and ix[r["id"]].get("cont_mode") != "switch_early"]
tot = Counter()
for r in merge:
    e = ix[r["id"]]; lead = e["lead_ms"]
    n_body = (e["length_ms"] - lead) * SR // 1000
    starts = window_starts(n_body)
    c = Counter()
    for spk, spans in e["spans"].items():
        if spk == r["author"]: continue
        for a, b in spans:
            for (valid, lg), st in zip(L[r["id"]], starts):
                lg = lg.astype(np.float32); best = lg.argmax(1)
                fa = int(((a - lead) * SR / 1000 - st) / PER_FRAME); fb = int(((b - lead) * SR / 1000 - st) / PER_FRAME)
                fa, fb = max(fa, 0), min(fb, int(valid / PER_FRAME))
                if fb <= fa: continue
                for f in range(fa, fb): c[int(best[f])] += 1
    # main channel = most solo frames
    print(r["id"], r["subsets"], "hidden ms", {s: v for s, v in e["speech_ms_after_lead"].items() if s != r["author"]}, dict(sorted(c.items())))
    tot.update(c)
print("classes over hidden-voice frames (0 none, 1-3 solo, 4-6 pair):", dict(sorted(tot.items())))
