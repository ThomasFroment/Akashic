"""Dev: remaining wrong authors of iteration 2 (cross-fitted, static profiles), by mechanism."""
import json, pickle, sys
from collections import Counter
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
from C2_decide import Params, decide
from C2_fit import seg_turns, unit, DEST
from common import FIXTURE
ix = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
cache = pickle.loads((DEST / "cache.pkl").read_bytes())
c = json.loads((DEST / "calib_iter2.json").read_text())
fold = c["folds"]; ovl = {int(k): v for k, v in c["ovl"].items()}
en = sorted(s for s, r in ix["roles"].items() if r == "enrolled")
cent = {s: unit(np.sum([e for _, e in cache["enrol"][s]], axis=0)) for s in en}
C = np.stack([cent[n] for n in en]); K = np.stack([np.array(c["consts"][str(1 - fold[n])]) for n in en])
P = {f: Params(**{k: c["params"][str(f)][k] for k in ("tau_a", "margin", "tau_v", "tau_c", "ovl_ms")}) for f in (0, 1)}
mech = Counter()
for e in ix["segments"]:
    if e["kind"] == "drift": continue
    t = seg_turns(cache["segs"][e["id"]])
    k, info = decide(t, C, K, lambda j: P[1 - fold[en[j]]], [ovl[e["id"]]])
    who = None if k is None else en[k]
    if who is None or (e["single"] is not None and who == e["single"]): continue
    others = {s: v for s, v in e["speech_ms_after_lead"].items() if s != who and v >= 300}
    if e["single"] is not None:
        m = "impostor->enrolled"
    else:
        nch = max(Counter(w for w, ch, ms, em in t if ms >= 300).values())
        o_ms = max(others.values())
        m = ("2nd voice >=1 s" if o_ms >= 1000 else "2nd voice <1 s") + (", seg saw 2 channels" if nch >= 2 else ", 1 channel") + (", ovl>0" if ovl[e["id"]] > 0 else "")
    mech[(e["kind"] + ("/" + e.get("cont_mode", "") if e["kind"] == "cont_first" else ""), m)] += 1
    if "mixed_reply" in e["subsets"]:
        print(e["id"], who, others, [(w, ch, ms, None if em is None else round(float(info['llr'][info['emb_idx'].index(i), k]), 1)) for i, (w, ch, ms, em) in enumerate(t)], "ovl", ovl[e["id"]])
for k_, v in sorted(mech.items()): print(v, k_)
