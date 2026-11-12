"""C2 calibration, 2-fold over speakers, from out\\C2_calib\\cache.pkl -> out\\C2_calib\\calib.json.

Folds: enrolled speakers (and impostors) split in two halves, stratified by language and
F0-gender. For fold f, everything is fitted on fold f's speakers only:
- per-bucket LLR constants (IRLS logistic regression, balanced prior) from target/non-target
  trials: duration crops and natural turns of fold-f single-speaker fixture segments, plus
  enrolment turns (non-target only), all scored against fold-f enrolment centroids;
- the update threshold tau_u per bucket >= 2 s: the 0.1% false-accept point of fold-f
  non-target LLRs;
- the decision thresholds (tau_a, margin, tau_v, tau_c) by grid search on fold-f segments
  (only segments whose every speaker is in fold f), max authorship s.t. wrong <= WRONG_CAP.
At runtime a profile of fold f uses the constants and thresholds fitted on the OTHER fold, so no
evaluated segment is ever scored with numbers fitted on its own speakers.
"""

from __future__ import annotations

import itertools
import json
import pickle
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from C2_decide import VETO_MS, Params, decide  # noqa: E402
from C2_lib import overlap_ms  # noqa: E402
from C2_lib import EDGES, balanced_weights, bucket, cllr, eer, irls_logistic, min_cllr  # noqa: E402
from common import FIXTURE, OUT  # noqa: E402

DEST = OUT / "C2_calib"
NB = len(EDGES)
WRONG_CAP = 0.005
FLOOR_SLACK = 0.005
ITER = int(sys.argv[1]) if len(sys.argv) > 1 else 2  # 1: spec C2; 2: + overlap-frame veto
AUTH_TIE = 0.01
OVL_GRID = [1e9, 400, 300, 200, 150, 100, 50]  # when the cap is out of reach: allow this much above the floor
GRID = {
    "tau_a": [0, 1, 2, 3, 4, 5, 6, 8, 10, 12],
    "margin": [0, 1, 2, 3, 4, 6, 8, 10],
    "tau_v": [0.5, 1, 2, 3, 5, 1e9],
    "tau_c": [0, 1, 2, 3, 5, 1e9],
}


def unit(v):
    return v / np.linalg.norm(v)


def folds(index) -> dict[str, int]:
    rng = np.random.default_rng(20261110)
    out = {}
    for role in ("enrolled", "impostor"):
        groups: dict = {}
        for spk, r in sorted(index["roles"].items()):
            if r == role:
                m = index["meta"][spk]
                groups.setdefault((m["lang"], m["gender"]), []).append(spk)
        k = 0
        for g in sorted(groups):
            spks = list(groups[g])
            rng.shuffle(spks)
            for s in spks:
                out[s] = k % 2
                k += 1
    return out


def seg_turns(cache_seg):
    return [(w, c, ms, None if e is None else unit(e)) for w, c, a, b, ms, e in cache_seg["turns"]]


def main() -> None:
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    cache = pickle.loads((DEST / "cache.pkl").read_bytes())
    fold = folds(index)
    roles = index["roles"]
    enrolled = sorted(s for s, r in roles.items() if r == "enrolled")
    cent = {s: unit(np.sum([e for _, e in cache["enrol"][s]], axis=0)) for s in enrolled}
    segs = {e["id"]: e for e in index["segments"] if e["kind"] != "drift"}

    # --- trials per fold ---
    trials = {0: [], 1: []}  # (speaker, ms, emb, source)
    for sid, spk, ms, emb in cache["crops"]:
        if sid in segs:
            trials[fold[spk]].append((spk, ms, unit(emb), "crop"))
    for sid, e in segs.items():
        if e["kind"] == "mixed" or e["single"] is None:
            continue
        for w, c, a, b, ms, emb in cache["segs"][sid]["turns"]:
            if emb is not None:
                trials[fold[e["single"]]].append((e["single"], ms, unit(emb), "turn"))
    for s in enrolled:
        for ms, emb in cache["enrol"][s]:
            trials[fold[s]].append((s, ms, unit(emb), "enrol"))

    def score(f_trials, f_profiles):
        """-> per bucket: (cos_tar, cos_non) against the given profiles."""
        names = sorted(f_profiles)
        C = np.stack([cent[n] for n in names])
        out = {b: ([], []) for b in range(NB)}
        for spk, ms, emb, src in f_trials:
            cs = C @ emb
            b = bucket(ms)
            for n, c in zip(names, cs, strict=True):
                if n == spk:
                    if src != "enrol":  # an enrolment turn is inside its own centroid
                        out[b][0].append(c)
                else:
                    out[b][1].append(c)
        return {b: (np.array(t), np.array(n)) for b, (t, n) in out.items()}

    prof = {f: [s for s in enrolled if fold[s] == f] for f in (0, 1)}
    calib = {"iter": ITER, "folds": fold, "consts": {}, "tau_u": {}, "params": {}, "trials": {}, "report": {}}
    scored = {f: score(trials[f], prof[f]) for f in (0, 1)}
    for f in (0, 1):
        consts, tau_u, counts = [], [], []
        for b in range(NB):
            tar, non = scored[f][b]
            s = np.concatenate([tar, non])
            y = np.concatenate([np.ones(len(tar)), np.zeros(len(non))])
            a, c = irls_logistic(s, y, balanced_weights(y))
            consts.append([a, c])
            llr_non = a * non + c
            tau_u.append(float(np.quantile(llr_non, 0.999)) if len(non) else 1e9)
            counts.append([len(tar), len(non)])
        calib["consts"][f] = consts
        calib["tau_u"][f] = tau_u
        calib["trials"][f] = counts
        print(f"fold {f}: consts {np.round(consts, 2).tolist()}")
        print(f"         tau_u {np.round(tau_u, 2).tolist()} trials {counts}")

    # --- Cllr per bucket on held-out folds (fold f trials, other fold's constants) ---
    rep = {}
    for b in range(NB):
        lt, ln, ct, cn = [], [], [], []
        for f in (0, 1):
            a, c = calib["consts"][1 - f][b]
            tar, non = scored[f][b]
            lt.append(a * tar + c)
            ln.append(a * non + c)
            ct.append(tar)
            cn.append(non)
        lt, ln, ct, cn = map(np.concatenate, (lt, ln, ct, cn))
        rep[b] = {
            "n_tar": len(lt), "n_non": len(ln),
            "cllr_cal": cllr(lt, ln), "cllr_cos": cllr(ct, cn),
            "cllr_cos_rule": cllr(20 * (ct - 0.5), 20 * (cn - 0.5)),
            "min_cllr": min_cllr(ct, cn), "eer": eer(ct, cn),
            "fa_at_0.5": float(np.mean(cn >= 0.5)), "miss_at_0.5": float(np.mean(ct < 0.5)),
        }  # fmt: skip
        print(f"bucket {b}: " + " ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                                         for k, v in rep[b].items()))  # fmt: skip
    calib["report"] = rep

    # --- decision thresholds per fold (in-fold segments, in-fold constants) ---
    logits = pickle.loads((DEST / "logits.pkl").read_bytes())
    ovl_of = {sid: max((overlap_ms(lg.astype(np.float32), v) for v, lg in ws), default=0.0)
              for sid, ws in logits.items()}  # fmt: skip
    calib["ovl"] = ovl_of
    grid = dict(GRID, ovl_ms=OVL_GRID if ITER == 2 else [1e9])
    loose = Params(-1e9, -1e9, 1e9, -1e9)

    def stats(e, turns, C, K, names):
        """Everything the thresholds act on, from one permissive decide() call."""
        k, info = decide(turns, C, K, loose)
        if k is None:
            return None
        llr = info["llr"][:, k]
        pos = {i: j for j, i in enumerate(info["emb_idx"])}
        by: dict = {}
        for i, t in enumerate(turns):
            if t[2] >= VETO_MS:
                by.setdefault(t[0], []).append(i)
        cmin = min((llr[pos[i]] for ix in by.values() if len(ix) >= 2 for i in ix),
                   default=np.inf)  # fmt: skip
        who = names[k]
        return (info["sum"], info["margin"], float(llr.min()), cmin, ovl_of[e["id"]],
                who == e["expected"] and e["expected"] is not None,
                e["single"] is None or who != e["single"])  # fmt: skip

    for f in (0, 1):
        names = prof[f]
        C = np.stack([cent[n] for n in names])
        K = np.tile(np.array(calib["consts"][f])[None], (len(names), 1, 1))
        fs = []
        for sid, e in segs.items():
            spk = set(e["labels"]) | ({e["single"]} if e["single"] else set())
            if spk and all(fold.get(s) == f for s in spk):
                fs.append((e, stats(e, seg_turns(cache["segs"][sid]), C, K, names)))
        n_exp = sum(e["expected"] is not None for e, _ in fs)
        st = np.array([r for _, r in fs if r is not None], float)
        S, MG, MN, CM, OV, AU, WR = st.T
        rows = []
        for ta, m, tv, tc, ov in itertools.product(*grid.values()):
            acc = (S >= ta) & (MG >= m) & (MN >= -tv) & (CM >= tc) & (OV < ov)
            rows.append((float((acc & (AU > 0)).sum() / n_exp), float((acc & (WR > 0)).sum() / len(fs)),
                         ta, m, tv, tc, ov))  # fmt: skip
        # max authorship s.t. wrong <= WRONG_CAP; if no setting reaches it, the floor (the
        # least wrong any setting reaches) + FLOOR_SLACK is the cap, still maximising authorship
        cap = max(WRONG_CAP, min(r[1] for r in rows) + FLOOR_SLACK)
        # in-fold multi-speaker counts are small (~50), so near-ties are noise: among settings
        # within AUTH_TIE of the best authorship, take the least wrong, then the strictest
        # overlap veto, then the strictest two-channel confirmation
        ok = [r for r in rows if r[1] <= cap + 1e-12]
        top = max(r[0] for r in ok)
        best = min((r for r in ok if r[0] >= top - AUTH_TIE), key=lambda r: (r[1], r[6], -r[5], -r[0]))
        calib["params"][f] = {"tau_a": best[2], "margin": best[3], "tau_v": best[4],
                              "tau_c": best[5], "ovl_ms": best[6], "fit_auth": best[0],
                              "fit_wrong": best[1], "n_segs": len(fs), "cap": cap}  # fmt: skip
        print(f"fold {f}: {len(fs)} segs ({n_exp} with an expected author), cap {cap:.4f}, best {best}")
        frontier = {}
        for r in rows:
            key = round(r[1], 3)
            if key not in frontier or r[0] > frontier[key][0]:
                frontier[key] = r
        for key in sorted(frontier)[:8]:
            print("    frontier", frontier[key])

    (DEST / f"calib_iter{ITER}.json").write_text(json.dumps(calib, indent=1, default=float), encoding="utf-8")
    preview(index, cache, calib, cent, enrolled, segs, ovl_of)


def preview(index, cache, calib, cent, enrolled, segs, ovl_of) -> None:
    """Cross-fitted preview with static enrolment profiles (no updates, no new profiles)."""
    from collections import Counter, defaultdict

    fold = calib["folds"]
    C = np.stack([cent[n] for n in enrolled])
    K = np.stack([np.array(calib["consts"][1 - fold[n]]) for n in enrolled])
    P = {f: Params(**{k: calib["params"][f][k] for k in ("tau_a", "margin", "tau_v", "tau_c", "ovl_ms")})
         for f in (0, 1)}  # fmt: skip
    stats = defaultdict(lambda: [0, 0, 0, 0])  # n_exp, auth, n, wrong
    reasons = defaultdict(Counter)
    for sid, e in segs.items():
        turns = seg_turns(cache["segs"][sid])
        k, info = decide(turns, C, K, lambda j: P[1 - fold[enrolled[j]]], [ovl_of[sid]])
        who = None if k is None else enrolled[k]
        a = e["expected"] is not None and who == e["expected"]
        w = who is not None and (e["single"] is None or who != e["single"])
        for s in ["all", *e["subsets"]]:
            st = stats[s]
            st[0] += e["expected"] is not None
            st[1] += a
            st[2] += 1
            st[3] += w
            if e["expected"] is not None and not a:
                reasons[s][info["reason"] or ("wrong_k" if who else "?")] += 1
            if w:
                reasons[s]["WRONG:" + (info["reason"] or "accepted")] += 1
    for s, (ne, au, n, wr) in sorted(stats.items()):
        print(f"  {s:20s} auth {au}/{ne} = {au / max(ne, 1):.3f}  wrong {wr}/{n} = {wr / n:.3f}"
              f"  {dict(reasons[s])}")  # fmt: skip


if __name__ == "__main__":
    main()
