"""Mining-only diagnostics: per-feature separation (AUC), high-S hallucinations, fusion ablations."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import C5_fit as F  # noqa: E402


def auc(pos, neg) -> float:
    pos, neg = np.asarray(pos), np.asarray(neg)
    return float(((pos[:, None] > neg[None, :]).mean() + 0.5 * (pos[:, None] == neg[None, :]).mean()))


rows = F.load()
live = [r for r in rows if not r.get("empty") and r["today"] != "reject"]
hall = [r for r in live if r["label"]["halluc"]]
real = [r for r in live if r["label"]["role"] != "nonspeech"]
print(f"live hall={len(hall)} real={len(real)}")
for k in F.FEATS + ["S", "conf"]:
    a = auc([r.get(k) or 0 for r in hall], [r.get(k) or 0 for r in real])
    print(f"  AUC(hall>real) {k:14s} {a:.3f}   hall med={np.median([r.get(k) or 0 for r in hall]):.2f} "
          f"real med={np.median([r.get(k) or 0 for r in real]):.2f}")
print("high-S hallucinations:")
for r in sorted(hall, key=lambda r: -r["S"])[:15]:
    print(f"  S={r['S']:.2f} lpA={r['lpA']:.2f} lpB={r['lpB']:.2f} minA={r['minA']:.2f} p_ns={r['p_ns']:.2f} "
          f"cr={r['cr']:.2f} sil={r['silero_ratio']:.2f} {r['label']['subset']:24s} {r['text'][:60]!r}")
print("low-S real speech:")
for r in sorted(real, key=lambda r: r["S"])[:10]:
    print(f"  S={r['S']:.2f} lpA={r['lpA']:.2f} lpB={r['lpB']:.2f} {r['label']['subset']} {r['text'][:50]!r} | ref {r['label']['ref_text'][:40]!r}")

base = list(F.FEATS)
for name, feats in [("all", base),
                    ("no contrast cells", [f for f in base if f not in ("S_P", "S_0", "lpA", "lpB", "lpC", "lpD", "minA", "S_P_min_win")]),
                    ("decode stats + conf only", ["p_ns", "avg_lp", "cr", "log_words", "duration"]),
                    ("cells only", ["S_P", "S_0", "lpA", "lpB", "lpC", "lpD", "minA"]),
                    ("decode stats + lifts", ["p_ns", "avg_lp", "cr", "log_words", "duration", "lift_X", "lift_0"]),
                    ("decode + S_P + lifts + minA", ["p_ns", "avg_lp", "cr", "log_words", "duration", "S_P",
                                                     "lift_X", "lift_0", "minA"])]:
    F.FEATS[:] = feats
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        fus = F.fit_fusion(rows)
    F.report(rows, [F.fusion_status(r, fus["t_unc"], fus["t_rej"]) for r in rows], f"fusion[{name}]")
F.FEATS[:] = base
