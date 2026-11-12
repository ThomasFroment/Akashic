"""Fit C5 thresholds (pure contrast) and the logistic fusion on MINING features only.

Reads C5/mining_feats.json, writes C5/params.json and prints the mining diagnostics.
Selection: 2-fold by source group; reject thresholds leave zero out-of-fold mining false rejections
among segments today keeps non-empty (with a margin), keep/unclear thresholds aim at >= 92% mining
recall so the test 90% target has some slack.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness  # noqa: E402
from C5_feat import derive, score_S  # noqa: E402

OUT = HERE / "C5"
RECALL_TARGET = 0.92
R_MARGIN = 0.5  # nats below the lowest real-speech S that could be rejected

FEATS = ["S_P", "S_0", "lpA", "lpB", "lpC", "lpD", "minA", "p_ns", "avg_lp", "cr", "silero_ratio",
         "ghost", "cover", "log_words", "duration", "S_P_min_win", "lift_X", "lift_0"]


def load() -> list[dict]:
    rows = json.loads((OUT / "mining_feats.json").read_text(encoding="utf-8"))
    groups = {cc["id"]: lab["group"] for cc, lab in harness.load_cache() if lab["split"] == "mining"}
    rows = [derive(r) for r in rows]
    for r in rows:
        r["group"] = groups[r["id"]]
        r["fold"] = int(hashlib.sha1(r["group"].encode()).hexdigest(), 16) % 2
        r["S"] = score_S(r) if not r.get("empty") else float("-inf")
    return rows


def second_signal(r: dict, p_ns: float = 0.6, cover: float = 0.6) -> bool:
    return r["p_ns"] > p_ns or bool(r["ghost"]) or r["cover"] >= cover


def contrast_status(r: dict, tk: float, tr: float) -> str:
    if r.get("empty") or r["today"] == "reject":
        return "reject"
    if r["S"] >= tk:
        return "keep"
    if r["S"] < tr and r["n_words"] >= 3 and second_signal(r):
        return "reject"
    return "unclear"


def report(rows: list[dict], status: list[str], tag: str) -> dict:
    hall = [s for r, s in zip(rows, status) if r["label"]["halluc"]]
    real = [(r, s) for r, s in zip(rows, status) if r["label"]["role"] != "nonspeech"]
    rec = sum(s != "keep" for s in hall) / len(hall)
    rej = sum(s == "reject" for s in hall) / len(hall)
    frr = sum(s == "reject" for _, s in real)
    frr_new = sum(s == "reject" and r["today"] != "reject" for r, s in real)
    unc = sum(s == "unclear" for _, s in real) / len(real)
    unc_short = [s == "unclear" for r, s in real if r["label"]["subset"] == "short"]
    out = {"recall": rec, "reject_recall": rej, "n_hall": len(hall), "frr": frr / len(real),
           "frr_new": frr_new, "n_real": len(real), "unclear": unc,
           "unclear_short": float(np.mean(unc_short))}
    print(f"{tag:28s} recall={rec:.3f} rej={rej:.3f} (n={len(hall)}) FRR={frr}/{len(real)} "
          f"(new {frr_new}) unclear={unc:.3f} short_unclear={out['unclear_short']:.3f}")
    return out


def fit_contrast(rows: list[dict]) -> tuple[float, float]:
    live = [r for r in rows if not r.get("empty") and r["today"] != "reject"]
    real = [r for r in live if r["label"]["role"] != "nonspeech"]
    hall = [r for r in live if r["label"]["halluc"]]
    n_hall_all = sum(r["label"]["halluc"] for r in rows)
    today_rej = n_hall_all - len(hall)
    # tau_k: smallest keep threshold reaching the recall target
    hs = np.sort([r["S"] for r in hall])
    need = math.ceil(RECALL_TARGET * n_hall_all) - today_rej
    tk = float(hs[need - 1] + 1e-6) if need > 0 else float(hs[0])
    # tau_r: below every real-speech S that the reject clause could touch
    eligible = [r["S"] for r in real if r["n_words"] >= 3 and second_signal(r)]
    tr = (min(eligible) - R_MARGIN) if eligible else tk
    return tk, min(tr, tk)


# ---------------------------------------------------------------- fusion
def matrix(rows: list[dict], mu=None, sd=None):  # noqa: ANN001, ANN201
    X = np.array([[float(r.get(k, 0.0)) for k in FEATS] for r in rows])
    if mu is None:
        mu, sd = X.mean(0), X.std(0) + 1e-9
    return np.hstack([np.ones((len(rows), 1)), (X - mu) / sd]), mu, sd


def irls(X: np.ndarray, y: np.ndarray, lam: float = 1.0, iters: int = 50) -> np.ndarray:
    w = np.zeros(X.shape[1])
    reg = lam * np.eye(X.shape[1]); reg[0, 0] = 0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(X @ w, -30, 30)))
        W = p * (1 - p) + 1e-9
        H = X.T @ (X * W[:, None]) + reg
        g = X.T @ (p - y) + reg @ w
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
    return w


def fit_fusion(rows: list[dict]) -> dict:
    live = [r for r in rows if not r.get("empty") and r["today"] != "reject"]
    y = np.array([float(r["label"]["halluc"]) for r in live])
    oof = np.zeros(len(live))
    for k in (0, 1):
        tr = [i for i, r in enumerate(live) if r["fold"] != k]
        te = [i for i, r in enumerate(live) if r["fold"] == k]
        Xtr, mu, sd = matrix([live[i] for i in tr])
        w = irls(Xtr, y[tr])
        Xte, _, _ = matrix([live[i] for i in te], mu, sd)
        oof[te] = 1 / (1 + np.exp(-(Xte @ w)))
    for r, p in zip(live, oof):
        r["p_oof"] = float(p)
    real_p = np.array([r["p_oof"] for r in live if r["label"]["role"] != "nonspeech"])
    hall_p = np.sort([r["p_oof"] for r in live if r["label"]["halluc"]])[::-1]
    n_hall_all = sum(r["label"]["halluc"] for r in rows)
    today_rej = n_hall_all - len(hall_p)
    need = math.ceil(RECALL_TARGET * n_hall_all) - today_rej
    t_unc = float(hall_p[need - 1] - 1e-9) if need > 0 else 1.0
    # reject threshold: above every real-speech OOF posterior, on the logit scale with a margin
    top = float(real_p.max())
    t_rej = float(1 / (1 + math.exp(-(math.log(top / (1 - top)) + 1.0))))
    X, mu, sd = matrix(live)
    w = irls(X, y)
    # reliability (OOF, deciles)
    order = np.argsort(oof)
    gaps = [abs(oof[c].mean() - y[c].mean()) for c in np.array_split(order, 10)]
    print("fusion weights:", {k: round(float(v), 3) for k, v in zip(["bias"] + FEATS, w)})
    print(f"fusion t_unc={t_unc:.4f} t_rej={t_rej:.4f} (max real OOF p {top:.4f}) "
          f"reliability max decile gap={max(gaps):.3f}")
    return {"w": w.tolist(), "mu": mu.tolist(), "sd": sd.tolist(), "feats": FEATS,
            "t_unc": t_unc, "t_rej": t_rej, "max_decile_gap": float(max(gaps))}


def fusion_status(r: dict, t_unc: float, t_rej: float) -> str:
    if r.get("empty") or r["today"] == "reject":
        return "reject"
    p = r.get("p_oof", 0.0)
    return "reject" if p >= t_rej else "unclear" if p >= t_unc else "keep"


def main() -> None:
    rows = load()
    report(rows, ["reject" if r.get("empty") or r["today"] == "reject" else
                  "unclear" if r["today"] == "unclear" else "keep" for r in rows], "today (mining)")
    real = [r for r in rows if r["label"]["role"] != "nonspeech" and not r.get("empty")]
    hall = [r for r in rows if r["label"]["halluc"] and not r.get("empty")]
    for name, sel in (("real", real), ("hall", hall)):
        s = np.array([r["S"] for r in sel])
        print(f"S {name}: n={len(s)} p1={np.percentile(s, 1):.2f} p5={np.percentile(s, 5):.2f} "
              f"p50={np.median(s):.2f} p95={np.percentile(s, 95):.2f}")
    by = {}
    for r in real:
        by.setdefault(r["label"]["subset"] + ("_opus" if r["label"]["opus"] else ""), []).append(r["S"])
    for k, v in sorted(by.items()):
        print(f"  real {k:16s} n={len(v):4d} min={min(v):.2f} p5={np.percentile(v, 5):.2f}")
    byh = {}
    for r in hall:
        byh.setdefault(r["label"]["subset"].split("/")[0], []).append(r["S"])
    for k, v in sorted(byh.items()):
        print(f"  hall {k:16s} n={len(v):4d} p50={np.median(v):.2f} p90={np.percentile(v, 90):.2f} max={max(v):.2f}")

    # 2-fold check of the contrast thresholds
    for k in (0, 1):
        tk, tr = fit_contrast([r for r in rows if r["fold"] != k])
        held = [r for r in rows if r["fold"] == k]
        report(held, [contrast_status(r, tk, tr) for r in held], f"contrast fold{k} tk={tk:.2f} tr={tr:.2f}")
    tk, tr = fit_contrast(rows)
    report(rows, [contrast_status(r, tk, tr) for r in rows], f"contrast all tk={tk:.2f} tr={tr:.2f}")
    fus = fit_fusion(rows)
    report(rows, [fusion_status(r, fus["t_unc"], fus["t_rej"]) for r in rows], "fusion OOF")
    full = list(FEATS)
    FEATS[:] = ["p_ns", "avg_lp", "cr", "log_words", "duration"]
    fus_dec = fit_fusion(rows)
    fus_dec["feats"] = list(FEATS)
    report(rows, [fusion_status(r, fus_dec["t_unc"], fus_dec["t_rej"]) for r in rows], "ablation decode-only OOF")
    FEATS[:] = full
    fus["feats"] = full
    params = {"contrast": {"tau_k": tk, "tau_r": tr}, "fusion": fus, "fusion_decode": fus_dec}
    (OUT / "params.json").write_text(json.dumps(params, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
