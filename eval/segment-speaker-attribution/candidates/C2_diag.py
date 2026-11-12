"""Dev diagnosis: in-fold wrong-author segments at the strictest settings."""
import json, pickle, sys
from collections import Counter
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
from C2_decide import Params, decide
from C2_fit import folds, seg_turns, unit, DEST, NB
from C2_lib import EDGES, balanced_weights, bucket, irls_logistic
from common import FIXTURE
index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
cache = pickle.loads((DEST / "cache.pkl").read_bytes())
fold = folds(index)
enrolled = sorted(s for s, r in index["roles"].items() if r == "enrolled")
cent = {s: unit(np.sum([e for _, e in cache["enrol"][s]], axis=0)) for s in enrolled}
calib = json.loads((DEST / "calib.json").read_text()) if (DEST/"calib.json").exists() else None
consts = {0: [[16.56, -3.57], [28.73, -9.61], [27.64, -11.15], [31.01, -14.25], [30.14, -15.19]],
          1: [[20.63, -4.24], [23.53, -7.31], [27.88, -10.77], [34.1, -15.47], [36.28, -18.0]]}
p = Params(*[float(a) for a in sys.argv[1:5]]) if len(sys.argv) > 4 else Params(6, 4, 2, 2)
for f in (0, 1):
    names = [s for s in enrolled if fold[s] == f]
    C = np.stack([cent[n] for n in names]); K = np.tile(np.array(consts[f])[None], (len(names), 1, 1))
    cnt = Counter(); n = 0
    for e in index["segments"]:
        if e["kind"] == "drift": continue
        spk = set(e["labels"]) | ({e["single"]} if e["single"] else set())
        if not spk or not all(fold.get(s) == f for s in spk): continue
        n += 1
        turns = seg_turns(cache["segs"][e["id"]])
        k, info = decide(turns, C, K, p)
        who = None if k is None else names[k]
        if who is not None and (e["single"] is None or who != e["single"]):
            cnt[tuple(e["subsets"])] += 1
            tl = [(w, c, ms, None if em is None else round(float(info['llr'][info['emb_idx'].index(i), k]), 1)) for i, (w, c, ms, em) in enumerate(turns)]
            print(f, e["id"], e["subsets"], e["length_ms"], "single", e["single"], "->", who, "sum %.1f m %.1f" % (info["sum"], info["margin"]), tl, {s: v for s, v in e["speech_ms_after_lead"].items()})
    print("fold", f, n, dict(cnt))
