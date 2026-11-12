r"""QA-R2-C2 gate fit, 2-fold over speakers as C2_fit: on top of C1's calibration (calib.json:
bucket constants, merge rule, span calibration), one logistic gate per fold over R2span_gate.FEATS,
fitted on fold-f segments with fold-f profiles (y = the loose k* is the expected author), its
threshold the in-fold point of max authorship with wrong <= 0.8% of fold-f segments.
At runtime a profile of fold f uses the other fold's gate.  -> out\R2span_C1\gate.json
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from C2_decide import Params, decide  # noqa: E402
from C2_fit import folds  # noqa: E402
from C2_lib import balanced_weights  # noqa: E402
from common import FIXTURE, OUT  # noqa: E402
from R2span_core import irls  # noqa: E402
from R2span_fit import DEST, loo, unit, view  # noqa: E402
from R2span_gate import FEATS, ed_max, lsame_max, score, vector  # noqa: E402

WRONG_GATE = 0.008
RIDGE = 1e-3  # weights sum to 1 (balanced), so the ridge must be small


def main() -> None:
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    cache = pickle.loads((DEST / "cache.pkl").read_bytes())["segs"]
    c2 = pickle.loads((OUT / "C2_calib" / "cache.pkl").read_bytes())
    cal = json.loads((DEST / "calib.json").read_text(encoding="utf-8"))
    fold = folds(index)
    enrolled = sorted(s for s, r in index["roles"].items() if r == "enrolled")
    cent = {s: unit(np.sum([e for _, e in c2["enrol"][s]], axis=0)) for s in enrolled}
    segs = {e["id"]: e for e in index["segments"] if e["kind"] != "drift"}
    loose = Params(-1e9, -1e9, 1e9, -1e9)

    def seg_fold(e):
        spk = set(e["labels"]) | ({e["single"]} if e["single"] else set())
        fs = {fold.get(s) for s in spk}
        return fs.pop() if len(fs) == 1 and spk else None

    gate = {}
    for f in (0, 1):
        names = [s for s in enrolled if fold[s] == f]
        C = np.stack([cent[n] for n in names])
        K = np.tile(np.array(cal["consts"][str(f)])[None], (len(names), 1, 1))
        wp, tm, ws = np.array(cal["w_pair"][str(f)]), cal["tau_m"][str(f)], np.array(cal["w_span"][str(f)])
        X, AU, WR = [], [], []
        n_all = n_exp = 0
        for sid, e in segs.items():
            if seg_fold(e) != f:
                continue
            n_all += 1
            n_exp += e["expected"] is not None
            cs = cache[sid]
            groups, turns, ovl = view(cs, wp, tm)
            k, info = decide(turns, C, K, loose)
            if k is None:
                continue
            s1, s2 = loo(cs, groups, turns, k, C, K, ws)
            me, md = ed_max(cs["logits"])
            v = vector(info, k, turns, ovl, lsame_max(cs["chans"], groups, wp), me, md, s1, s2)
            who = names[k]
            X.append(v)
            AU.append(who == e["expected"] and e["expected"] is not None)
            WR.append(e["single"] is None or who != e["single"])
        X, AU, WR = np.array(X), np.array(AU), np.array(WR)
        mu, sd = X.mean(0), X.std(0) + 1e-9
        y = AU.astype(float)
        w = irls((X - mu) / sd, y, balanced_weights(y), ridge=RIDGE)
        g = {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist()}
        sc = np.array([score(v, g) for v in X])
        best = None
        for thr in np.unique(sc):
            acc = sc >= thr
            wr = (acc & WR).sum() / n_all
            au = (acc & AU).sum() / n_exp
            if wr <= WRONG_GATE + 1e-12 and (best is None or au > best[1]):
                best = (float(thr), float(au), float(wr))
        g["thr"], g["fit_auth"], g["fit_wrong"] = best
        g["n"] = [n_all, int(AU.sum()), int(WR.sum())]
        gate[f] = g
        print(f"fold {f}: {n_all} segs, {int(AU.sum())} right / {int(WR.sum())} wrong k*, thr {best[0]:.3f} auth {best[1]:.3f} wrong {best[2]:.4f}")
        print("   weights", dict(zip(FEATS + ["b"], np.round(w, 2).tolist(), strict=True)))
    (DEST / "gate.json").write_text(json.dumps(gate, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
