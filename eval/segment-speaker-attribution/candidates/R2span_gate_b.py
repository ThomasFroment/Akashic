r"""Gate (b) for QA-R2-C2: can segmentation posteriors propose the span of a merged second voice?

From the cached C2 logits (out\C2_calib\logits.pkl, the body windows of C2_lib.window_starts):
per frame decoded as one voice m alone,
  E_t = max over k != m of a_k(t)   (a_k = softmax mass of the classes containing k)
  D_t = 1 - p({m}) - p(empty)        (residual mass off "m alone")
both 0 elsewhere, each box-averaged over 18 frames (~306 ms); s_t = max(boxE, boxD).
Proposals = peaks of s_t (non-max suppression 18 frames) >= tau, with a separate tau for frames in
the first/last 0.5 s of a window (edge) and the interior.

Positives: the 24 merge cases (C2_calib's wrong-author mixed segments). A merge case is recalled
when a proposal box overlaps a true span of a non-author voice. Negatives: single-speaker segments.
Thresholds fitted 2-fold over speakers (in-fold: max recall s.t. mean proposals per segment <= 8
on in-fold segments), recall reported out of fold. Also segment-level AUC of max s.
Keep the proposal step only if recall >= 0.95 at <= 8 proposals per segment.
-> out\R2span_gates\gate_b.json
"""

from __future__ import annotations

import json
import pickle
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from C2_fit import folds  # noqa: E402
from C2_lib import PER_FRAME, SR, window_starts  # noqa: E402
from common import FIXTURE, OUT  # noqa: E402

BOX = 18
EDGE_F = int(0.5 * SR / PER_FRAME)
_PS = ((), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2))
M = np.zeros((7, 3))
for c, mem in enumerate(_PS):
    for j in mem:
        M[c, j] = 1


def frame_scores(valid: int, lg: np.ndarray):
    lg = lg.astype(np.float32)
    real = min(len(lg), int(valid / PER_FRAME))
    lg = lg[:real]
    p = np.exp(lg - lg.max(1, keepdims=True))
    p /= p.sum(1, keepdims=True)
    best = lg.argmax(1)
    solo = (best >= 1) & (best <= 3)
    m = np.clip(best - 1, 0, 2)
    a = p @ M  # (F, 3)
    a_other = a.copy()
    a_other[np.arange(real), m] = -1
    E = np.where(solo, a_other.max(1), 0.0)
    D = np.where(solo, 1 - p[np.arange(real), best] - p[:, 0], 0.0)
    k = np.ones(BOX) / BOX
    bE = np.convolve(E, k, "same")
    bD = np.convolve(D, k, "same")
    s = np.maximum(bE, bD)
    edge = np.zeros(real, bool)
    edge[:EDGE_F] = True
    edge[-EDGE_F:] = True
    return s, edge


def proposals(s: np.ndarray, edge: np.ndarray, te: float, ti: float) -> list[int]:
    thr = np.where(edge, te, ti)
    cand = np.flatnonzero(s >= thr)
    order = cand[np.argsort(-s[cand])]
    taken: list[int] = []
    for f in order:
        if all(abs(f - g) >= BOX for g in taken):
            taken.append(int(f))
    return taken


def main() -> None:
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    L = pickle.loads((OUT / "C2_calib" / "logits.pkl").read_bytes())
    det = json.loads((OUT / "C2_calib" / "details.json").read_text(encoding="utf-8"))
    by = {e["id"]: e for e in index["segments"]}
    merge = [r["id"] for r in det["segments"] if r["wrong"] and r["single"] is None
             and by[r["id"]].get("cont_mode") != "switch_early"]  # fmt: skip
    author = {r["id"]: r["author"] for r in det["segments"]}
    fold = folds(index)
    # per segment: frame scores in body time
    feats = {}
    for sid, ws in L.items():
        e = by[sid]
        n_body = (e["length_ms"] - e["lead_ms"]) * SR // 1000
        starts = window_starts(n_body)
        items = []
        for (valid, lg), st in zip(ws, starts, strict=True):
            s, edge = frame_scores(valid, lg)
            items.append((st, s, edge))
        feats[sid] = items
    neg = [sid for sid, e in by.items() if sid in L and e["single"] is not None and len(e["labels"]) == 1]
    pos = merge

    # greedy NMS once at the lowest threshold; a (te, ti) pair keeps the peaks above their own
    # threshold (exact for one threshold; with two, an edge peak could have suppressed an interior one)
    peaks = {}
    for sid, items in feats.items():
        pk = []
        for st, s, edge in items:
            for f in proposals(s, edge, 0.05, 0.05):
                c = (st + (f + 0.5) * PER_FRAME) * 1000 / SR
                h = BOX / 2 * PER_FRAME * 1000 / SR
                pk.append((float(s[f]), bool(edge[f]), c - h, c + h))
        peaks[sid] = pk

    def props_ms(sid, te, ti):
        return [(a, b) for v, ed, a, b in peaks[sid] if v >= (te if ed else ti)]

    def hit(sid, pr):
        e = by[sid]
        lead = e["lead_ms"]
        for spk, spans in e["spans"].items():
            if spk == author[sid]:
                continue
            for a, b in spans:
                a, b = a - lead, b - lead
                if any(min(b, y) - max(a, x) > 0 for x, y in pr):
                    return True
        return False

    seg_fold = {}
    for sid in pos + neg:
        e = by[sid]
        spk = set(e["labels"])
        fs = {fold[s] for s in spk}
        seg_fold[sid] = fs.pop() if len(fs) == 1 else -1

    # segment-level AUC of max s
    def smax(sid):
        return max(float(s.max()) if len(s) else 0.0 for _, s, _ in feats[sid])

    sp = np.array([smax(s) for s in pos])
    sn = np.array([smax(s) for s in neg])
    auc = float(np.mean(sp[:, None] > sn[None, :]) + 0.5 * np.mean(sp[:, None] == sn[None, :]))

    grid = np.round(np.arange(0.05, 0.96, 0.025), 3)
    # pooled curve
    curve = []
    for te in grid:
        for ti in grid:
            npn = np.mean([len(props_ms(s, te, ti)) for s in neg])
            rec = np.mean([hit(s, props_ms(s, te, ti)) for s in pos])
            curve.append((float(te), float(ti), float(rec), float(npn)))
    ok = [c for c in curve if c[3] <= 8]
    best_pooled = max(ok, key=lambda c: (c[2], -c[3])) if ok else None
    # 2-fold
    oof = {}
    fit = {}
    for f in (0, 1):
        pin = [s for s in pos if seg_fold[s] == f]
        nin = [s for s in neg if seg_fold[s] == f]
        rows = []
        for te in grid:
            for ti in grid:
                npn = np.mean([len(props_ms(s, te, ti)) for s in nin + pin])
                if npn > 8:
                    continue
                rec = np.mean([hit(s, props_ms(s, te, ti)) for s in pin]) if pin else 0
                rows.append((float(rec), -float(npn), float(te), float(ti)))
        b = max(rows)
        fit[f] = {"te": b[2], "ti": b[3], "rec_in": b[0], "props_in": -b[1], "n_pos": len(pin)}
    for s in pos + neg:
        f = seg_fold[s]
        g = fit[1 - f] if f in (0, 1) else {"te": max(fit[0]["te"], fit[1]["te"]), "ti": max(fit[0]["ti"], fit[1]["ti"])}
        pr = props_ms(s, g["te"], g["ti"])
        oof[s] = (len(pr), hit(s, pr) if s in pos else None)
    rec_oof = float(np.mean([oof[s][1] for s in pos]))
    props_neg = np.array([oof[s][0] for s in neg])
    props_pos = np.array([oof[s][0] for s in pos])
    res = {
        "n_pos": len(pos), "n_neg": len(neg), "auc_segment_max": auc,
        "pos_max_s_q": np.percentile(sp, [0, 10, 50]).tolist(),
        "neg_max_s_q": np.percentile(sn, [50, 90, 99]).tolist(),
        "pooled_best_at_8": best_pooled,
        "fit": fit, "recall_oof": rec_oof,
        "props_per_seg_oof_neg": {"mean": float(props_neg.mean()), "p95": float(np.percentile(props_neg, 95)), "max": int(props_neg.max())},
        "props_per_seg_oof_pos": {"mean": float(props_pos.mean()), "max": int(props_pos.max())},
        "missed": [s for s in pos if not oof[s][1]],
        "keep_proposals": bool(rec_oof >= 0.95 and props_neg.mean() <= 8),
        "recall_at_mean_props": sorted({round(c[3]): c[2] for c in curve}.items())[:20],
    }  # fmt: skip
    # recall vs mean proposals frontier (pooled)
    fr = {}
    for te, ti, rec, npn in curve:
        k = int(np.ceil(npn))
        if k not in fr or rec > fr[k][0]:
            fr[k] = (rec, te, ti)
    res["frontier_pooled"] = {k: fr[k] for k in sorted(fr)[:25]}
    (OUT / "R2span_gates" / "gate_b.json").write_text(json.dumps(res, indent=1, default=float), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "recall_at_mean_props"}, indent=1, default=float))


if __name__ == "__main__":
    main()
