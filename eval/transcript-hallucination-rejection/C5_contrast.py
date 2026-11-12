"""C5 pure contrast: keep if S >= tau_k; reject if S < tau_r, >= 3 words and a second signal
(p_ns > 0.6, known-hallucination substring, prompt 3-gram copy coverage >= 0.6); else unclear.
Thresholds come from C5/params.json, fitted by C5_fit.py on the mining split only."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import C5_feat  # noqa: E402

OUT = HERE / "C5"
P: dict = {}
MODE = "contrast"


def fit(mining, helpers) -> None:  # noqa: ANN001
    P.update(json.loads((OUT / "params.json").read_text(encoding="utf-8")))


def _second(f: dict) -> bool:
    return f["p_ns"] > 0.6 or bool(f["ghost"]) or f["cover"] >= 0.6


def decide(f: dict) -> str:
    if MODE == "contrast":
        c = P["contrast"]
        s = C5_feat.score_S(f)
        if s >= c["tau_k"]:
            return "keep"
        if s < c["tau_r"] and f["n_words"] >= 3 and _second(f):
            return "reject"
        return "unclear"
    fu = P["fusion_decode" if MODE == "decode" else "fusion"]
    f = C5_feat.derive(f)
    z = fu["w"][0] + sum(w * (float(f.get(k, 0.0)) - m) / s
                         for w, k, m, s in zip(fu["w"][1:], fu["feats"], fu["mu"], fu["sd"]))
    p = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
    return "reject" if p >= fu["t_rej"] else "unclear" if p >= fu["t_unc"] else "keep"


def judge(cc: dict, helpers) -> dict:  # noqa: ANN001
    text = cc["text"]
    if not text or helpers.repo.is_hallucination(text) or not cc["parts"]:
        return {"status": "reject", "text": "", "extra_decodes": 0, "extra_ms": 0.0}
    if MODE == "decode":
        status = decide(C5_feat.decode_stats(cc, helpers.repo.normalize))
        return {"status": status, "text": text if status != "reject" else "", "extra_decodes": 0,
                "extra_ms": 0.0}
    f = C5_feat.features(cc, helpers)
    status = decide(f)
    with (OUT / f"timing_{MODE}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": cc["id"], "enc_ms": f["enc_ms"], "align_ms": f["align_ms"],
                             "total_ms": f["total_ms"], "decode_ms": cc["decode_ms"],
                             "n_win": f["n_win"]}) + "\n")
    return {"status": status, "text": text if status != "reject" else "", "extra_decodes": 0,
            "extra_ms": f["total_ms"]}
