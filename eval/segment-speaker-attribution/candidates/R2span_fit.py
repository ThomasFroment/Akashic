r"""QA-R2-C1 calibration, 2-fold over speakers exactly as C2_fit (folds, in-fold fitting, a profile
of fold f uses the other fold's numbers at runtime), from out\R2span_C1\cache.pkl (+ C2's crops and
enrolment embeddings, which are the same model outputs) -> out\R2span_C1\calib.json.

Per fold f (fold-f speakers and segments only):
1. per-bucket LLR constants and the update threshold tau_u: C2_fit's trials and IRLS, with this
   candidate's turns;
2. split-channel merge: same-voice LLR of a channel pair in one window, logistic on
   [cos, ln min dur, ln max dur], targets = pairs whose channels belong to one true speaker;
   tau_m = the point where at most 0.5% of different-voice pairs merge;
3. span change test: same-voice LLR z of a span vs the rest of its group, logistic on
   [cos, ln d_span, ln d_rest]; targets = spans of the group's own speaker, non-targets = spans
   owned by another speaker (the merged voices in mixed segments);
4. decision thresholds: C2's grid (tau_a, margin, tau_v, tau_c, ovl_ms) x (tau_s, tau_k) for the
   new vetoes (NULL if max -z > tau_s or max Delta > tau_k), the two new vetoes together nulling at
   most 1.5% of fold-f single-speaker segments; max authorship s.t. wrong <= cap (C2's rule).
"""

from __future__ import annotations

import itertools
import json
import pickle
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from C2_decide import VETO_MS, Params, decide  # noqa: E402
from C2_fit import FLOOR_SLACK, GRID, OVL_GRID, WRONG_CAP, AUTH_TIE, folds  # noqa: E402
from C2_lib import EDGES, balanced_weights, bucket, irls_logistic  # noqa: E402
from common import FIXTURE, OUT  # noqa: E402
from R2span_core import MERGE_MIN_MS, STEP_S, irls, logit, pair_feats, span_feats  # noqa: E402

DEST = OUT / "R2span_C1"
NB = len(EDGES)
VETO_BUDGET = 0.015
MERGE_FA = 0.005
NAME = sys.argv[1] if len(sys.argv) > 1 else "calib.json"


def unit(v):
    return v / np.linalg.norm(v)


def overlap(spans, a, b):
    return sum(max(0, min(y, b) - max(x, a)) for x, y in spans)


def owner_of(e, pieces, lead):
    """(speaker, fraction of the labelled speech in these body-ms pieces) or (None, 0)."""
    tot = Counter()
    for a, b in pieces:
        for spk, sp in e["spans"].items():
            tot[spk] += overlap(sp, a + lead, b + lead)
    s = sum(tot.values())
    if s <= 0:
        return None, 0.0, 0
    spk, v = tot.most_common(1)[0]
    return spk, v / s, s


# --- a cached segment under given merge numbers ------------------------------------------------


def view(cs, w_pair, tau_m):
    """-> (groups, turns for decide, ovl per window, group keys)."""
    ch = cs["chans"]
    parent = list(range(len(ch)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if w_pair is not None:
        by = defaultdict(list)
        for i, c in enumerate(ch):
            if c["emb"] is not None:
                by[c["win"]].append(i)
        for ix in by.values():
            for p, q in itertools.combinations(ix, 2):
                a, b = ch[p], ch[q]
                if min(a["ms"], b["ms"]) < MERGE_MIN_MS:
                    continue
                if float(logit(pair_feats(float(a["emb"] @ b["emb"]), a["ms"], b["ms"]), w_pair)) >= tau_m:
                    parent[find(q)] = find(p)
    gs = defaultdict(list)
    for i in range(len(ch)):
        gs[find(i)].append(i)
    groups = [tuple(sorted(g)) for g in gs.values()]
    turns, gid_of = [], {}
    for gi, g in enumerate(groups):
        for i in g:
            gid_of[i] = gi
        c0 = ch[g[0]]
        emb = cs["groups"][g]["emb"] if c0["emb"] is not None else None
        turns.append((c0["win"], gi, sum(ch[i]["ms"] for i in g), None if emb is None else unit(emb.astype(np.float64))))
    ovl = []
    for wi, po in enumerate(cs["pair_ovl"]):
        ovl.append(sum(po.values()))  # iteration 2: merged channels' overlap still counts
    return groups, turns, ovl


def spans_of(cs, g):
    """(eS, cos, n, nrest, kind, t) of a group from the cache, or None."""
    gr = cs["groups"].get(g)
    if gr is None or gr["cos"] is None:
        return None
    parts = [cs["eS"][i] for i in g if i in cs["eS"] and len(cs["eS"][i][1])]
    if not parts:
        return None
    eS = np.concatenate([p[0] for p in parts]).astype(np.float64)
    n = np.concatenate([p[1] for p in parts])
    kind = np.concatenate([p[2] for p in parts])
    t = np.concatenate([p[3] for p in parts])
    return eS, gr["cos"].astype(np.float64), n, gr["nr"], kind, t


def loo(cs, groups, turns, k, C, K, w_span):
    s1, s2 = -np.inf, -np.inf
    for g, t in zip(groups, turns, strict=True):
        if t[3] is None:
            continue
        sp = spans_of(cs, g)
        if sp is None:
            continue
        eS, cos, n, nr, _kind, _t = sp
        z = logit(span_feats(cos, n, nr), w_span)
        s1 = max(s1, float((-z).max()))
        if len(C) > 1:
            bk = np.array([bucket(x * STEP_S * 1000) for x in n])
            llr = K[:, bk, 0].T * (eS @ C.T) + K[:, bk, 1].T
            s2 = max(s2, float((np.delete(llr, k, 1).max(1) - llr[:, k]).max()))
    return s1, s2


def main() -> None:
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    cache = pickle.loads((DEST / "cache.pkl").read_bytes())["segs"]
    c2 = pickle.loads((OUT / "C2_calib" / "cache.pkl").read_bytes())
    fold = folds(index)
    roles = index["roles"]
    enrolled = sorted(s for s, r in roles.items() if r == "enrolled")
    cent = {s: unit(np.sum([e for _, e in c2["enrol"][s]], axis=0)) for s in enrolled}
    segs = {e["id"]: e for e in index["segments"] if e["kind"] != "drift"}

    def seg_fold(e):
        spk = set(e["labels"]) | ({e["single"]} if e["single"] else set())
        fs = {fold.get(s) for s in spk}
        return fs.pop() if len(fs) == 1 and spk else None

    calib = {"folds": fold, "consts": {}, "tau_u": {}, "w_pair": {}, "tau_m": {}, "w_span": {},
             "params": {}, "report": {}}  # fmt: skip

    # 1. bucket constants (C2_fit trials, this candidate's single-channel turns)
    trials = {0: [], 1: []}
    for sid, spk, ms, emb in c2["crops"]:
        if sid in segs:
            trials[fold[spk]].append((spk, ms, unit(emb), "crop"))
    for sid, e in segs.items():
        if e["kind"] == "mixed" or e["single"] is None:
            continue
        for c in cache[sid]["chans"]:
            if c["emb"] is not None:
                trials[fold[e["single"]]].append((e["single"], c["ms"], unit(c["emb"].astype(np.float64)), "turn"))
    for s in enrolled:
        for ms, emb in c2["enrol"][s]:
            trials[fold[s]].append((s, ms, unit(emb), "enrol"))
    prof = {f: [s for s in enrolled if fold[s] == f] for f in (0, 1)}
    for f in (0, 1):
        names = prof[f]
        Cf = np.stack([cent[n] for n in names])
        by_b = {b: ([], []) for b in range(NB)}
        for spk, ms, emb, src in trials[f]:
            for n_, c in zip(names, Cf @ emb, strict=True):
                if n_ == spk:
                    if src != "enrol":
                        by_b[bucket(ms)][0].append(c)
                else:
                    by_b[bucket(ms)][1].append(c)
        consts, tau_u = [], []
        for b in range(NB):
            tar, non = map(np.array, by_b[b])
            s = np.concatenate([tar, non])
            y = np.concatenate([np.ones(len(tar)), np.zeros(len(non))])
            a, c = irls_logistic(s, y, balanced_weights(y))
            consts.append([a, c])
            tau_u.append(float(np.quantile(a * non + c, 0.999)))
        calib["consts"][f], calib["tau_u"][f] = consts, tau_u
        print(f"fold {f} consts {np.round(consts, 2).tolist()}")

    # 2. split-channel pairs
    pairs = {0: [], 1: []}  # (features, same)
    pair_rows = []
    for sid, e in segs.items():
        f = seg_fold(e)
        cs = cache[sid]
        ch = cs["chans"]
        by = defaultdict(list)
        for i, c in enumerate(ch):
            if c["emb"] is not None:
                by[c["win"]].append(i)
        for ix in by.values():
            for p, q in itertools.combinations(ix, 2):
                a, b = ch[p], ch[q]
                if min(a["ms"], b["ms"]) < MERGE_MIN_MS:
                    continue
                oa, fa, _ = owner_of(e, a["pieces"], e["lead_ms"])
                ob, fb, _ = owner_of(e, b["pieces"], e["lead_ms"])
                if oa is None or ob is None:
                    continue
                # iteration 2: "same" only for pure channels (>= 90% one voice); a channel holding
                # another voice is a different-voice pair even when its majority is the main voice
                if oa == ob and min(fa, fb) < 0.9:
                    oa = oa + "/impure"
                x = pair_feats(float(a["emb"].astype(np.float64) @ b["emb"].astype(np.float64)), a["ms"], b["ms"])
                pair_rows.append((sid, f, x, oa == ob))
                if f is not None:
                    pairs[f].append((x, oa == ob))
    # iteration 2: the 3-feature pair logistic overfit ~20-28 different-voice pairs per fold
    # (7/48 out-of-fold different-voice merges), so the rule is cosine only, one threshold for both
    # folds: the larger in-fold maximum different-voice cosine, rounded up to 0.05 (the stricter
    # fold's number; not strictly out of fold for that fold's segments)
    top = []
    for f in (0, 1):
        X = np.stack([p[0] for p in pairs[f]])
        y = np.array([p[1] for p in pairs[f]], float)
        top.append(float(X[y == 0, 0].max()))
    tau_cos = float(np.ceil(max(top) / 0.05 + 1e-9) * 0.05)
    for f in (0, 1):
        calib["w_pair"][f], calib["tau_m"][f] = [1.0, 0.0, 0.0, 0.0], tau_cos
        print(f"fold {f} pairs: merge iff cos >= {tau_cos:.2f} (in-fold max different-voice cos {top[f]:.3f})")
    # out-of-fold merge report
    rep = {"same": [0, 0], "diff": [0, 0]}
    for sid, f, x, same in pair_rows:
        if f is None:
            continue
        g = 1 - f
        m = float(logit(x, np.array(calib["w_pair"][g]))) >= calib["tau_m"][g]
        key = "same" if same else "diff"
        rep[key][0] += m
        rep[key][1] += 1
    calib["report"]["merge_oof"] = rep
    print("out-of-fold merges: same-voice", rep["same"], "different-voice", rep["diff"])

    # 3. span calibration (in-fold, unmerged channel groups)
    sp_rows = {0: ([], []), 1: ([], [])}
    rng = np.random.default_rng(3)
    nontarget_n = Counter()
    for sid, e in segs.items():
        f = seg_fold(e)
        if f is None:
            continue
        cs = cache[sid]
        for i, c in enumerate(cs["chans"]):
            if c["emb"] is None:
                continue
            sp = spans_of(cs, (i,))
            if sp is None:
                continue
            own, frac, _ = owner_of(e, c["pieces"], e["lead_ms"])
            if own is None:
                continue
            _eS, cos, n, nr, _kind, t = sp
            X = span_feats(cos, n, nr)
            for j in range(len(n)):
                so, sf, tot = owner_of(e, [(t[j, 0], t[j, 1])], e["lead_ms"])
                if so is None or sf < 0.6 or tot < 150:
                    continue
                if so == own:
                    if rng.random() < 0.25:
                        sp_rows[f][0].append((X[j], 1.0))
                else:
                    sp_rows[f][1].append((X[j], 0.0))
                    nontarget_n[f] += 1
    for f in (0, 1):
        rows = sp_rows[f][0] + sp_rows[f][1]
        X = np.stack([r[0] for r in rows])
        y = np.array([r[1] for r in rows])
        w = irls(X, y, balanced_weights(y))
        calib["w_span"][f] = w.tolist()
        print(f"fold {f} spans: {int(y.sum())} target / {int((1 - y).sum())} non-target, w {np.round(w, 2).tolist()}")
    calib["report"]["span_trials"] = {f: [len(sp_rows[f][0]), len(sp_rows[f][1])] for f in (0, 1)}

    # 4. decision thresholds per fold
    loose = Params(-1e9, -1e9, 1e9, -1e9)
    calib["stats"] = {}
    for f in (0, 1):
        names = prof[f]
        Cf = np.stack([cent[n] for n in names])
        K = np.tile(np.array(calib["consts"][f])[None], (len(names), 1, 1))
        wp, tm, ws = np.array(calib["w_pair"][f]), calib["tau_m"][f], np.array(calib["w_span"][f])
        rows, solo_flags = [], []
        n_exp = n_all = 0
        for sid, e in segs.items():
            if seg_fold(e) != f:
                continue
            n_all += 1
            n_exp += e["expected"] is not None
            cs = cache[sid]
            groups, turns, ovl = view(cs, wp, tm)
            k, info = decide(turns, Cf, K, loose)
            if k is None:
                continue
            llr = info["llr"][:, k]
            pos = {i: j for j, i in enumerate(info["emb_idx"])}
            by = defaultdict(list)
            for i, t in enumerate(turns):
                if t[2] >= VETO_MS:
                    by[t[0]].append(i)
            cmin = min((llr[pos[i]] for ix in by.values() if len(ix) >= 2 for i in ix if i in pos), default=np.inf)
            if any(len(ix) >= 2 and any(i not in pos for i in ix) for ix in by.values()):
                cmin = -np.inf
            s1, s2 = loo(cs, groups, turns, k, Cf, K, ws)
            who = names[k]
            solo = e["single"] is not None and len(e["labels"]) == 1
            rows.append((info["sum"], info["margin"], float(llr.min()), cmin, max(ovl, default=0.0), s1, s2,
                         who == e["expected"] and e["expected"] is not None,
                         e["single"] is None or who != e["single"], solo, sid))  # fmt: skip
        st = np.array([r[:10] for r in rows], float)
        S, MG, MN, CM, OV, Z, D, AU, WR, SO = st.T
        n_solo = sum(1 for sid, e in segs.items() if seg_fold(e) == f and e["single"] is not None and len(e["labels"]) == 1)
        zs = np.quantile(Z[SO > 0], [0.985, 0.99, 0.9925, 0.995, 0.9975, 1.0])
        ds = np.quantile(D[SO > 0][np.isfinite(D[SO > 0])], [0.985, 0.99, 0.9925, 0.995, 0.9975, 1.0])
        zgrid = sorted(set(np.round(zs, 4).tolist())) + [np.inf]
        dgrid = sorted(set(np.round(ds, 4).tolist())) + [np.inf]
        out = []
        for ts, tk in itertools.product(zgrid, dgrid):
            veto = (Z > ts) | (D > tk)
            if (veto & (SO > 0)).sum() / n_solo > VETO_BUDGET + 1e-12:
                continue
            for ta, m, tv, tc, ov in itertools.product(GRID["tau_a"], GRID["margin"], GRID["tau_v"], GRID["tau_c"], OVL_GRID):
                acc = (S >= ta) & (MG >= m) & (MN >= -tv) & (CM >= tc) & (OV < ov) & ~veto
                out.append((float((acc & (AU > 0)).sum() / n_exp), float((acc & (WR > 0)).sum() / n_all),
                            ta, m, tv, tc, ov, ts, tk))  # fmt: skip
        cap = max(WRONG_CAP, min(r[1] for r in out) + FLOOR_SLACK)
        ok = [r for r in out if r[1] <= cap + 1e-12]
        top = max(r[0] for r in ok)
        best = min((r for r in ok if r[0] >= top - AUTH_TIE), key=lambda r: (r[1], r[6], -r[5], r[7], r[8], -r[0]))
        calib["params"][f] = {"tau_a": best[2], "margin": best[3], "tau_v": best[4], "tau_c": best[5],
                              "ovl_ms": best[6], "tau_s": best[7], "tau_k": best[8], "fit_auth": best[0],
                              "fit_wrong": best[1], "cap": cap, "n_segs": n_all}  # fmt: skip
        floor_wo = min(r[1] for r in out if r[7] == np.inf and r[8] == np.inf)
        print(f"fold {f}: {n_all} segs, cap {cap:.4f}, wrong floor {min(r[1] for r in out):.4f} (no new veto {floor_wo:.4f}), best {best[:2]} {best[2:]}")
        calib["stats"][f] = [list(map(float, r[:10])) + [r[10]] for r in rows]

    (DEST / NAME).write_text(json.dumps(calib, indent=1, default=float), encoding="utf-8")
    print("wrote", DEST / NAME)


if __name__ == "__main__":
    main()
