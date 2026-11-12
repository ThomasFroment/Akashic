"""Summarise C5 test runs: per-subset FRR/ub95, recall by source, unclear, time ratios incl. encoder reuse."""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "C5"
REF = Path(__file__).resolve().parent / "ref" / "details.json"


def main() -> None:
    ref = json.loads(REF.read_text(encoding="utf-8"))["metrics"]
    for name, mode in (("C5_contrast", "contrast"), ("C5_fusion", "fusion"), ("C5_decode", None)):
        p = OUT / name / "details.json"
        if not p.exists():
            continue
        m = json.loads(p.read_text(encoding="utf-8"))["metrics"]
        print((OUT / name / "metrics.txt").read_text(encoding="utf-8").strip())
        print("  FRR/ub95:", {k: f"{v['k']}/{v['n']} {v['rate']:.4f}/{v['ub95']:.4f}" for k, v in m["frr"].items()})
        print("  FRR among today non-empty:", m["frr_among_today_nonempty"])
        print("  recall by source:", {k: f"{v['k']}/{v['n']}" for k, v in m["nonspeech_recall_by_source"].items()})
        print("  unclear:", {k: f"{v['rate']:.3f} (today {ref['unclear'][k]['rate']:.3f})" for k, v in m["unclear"].items()})
        if mode:
            t = [json.loads(l) for l in (OUT / f"timing_{mode}.jsonl").open(encoding="utf-8")]
            prim = m["primary_decode_ms_total"]
            enc = sum(r["enc_ms"] for r in t); al = sum(r["align_ms"] for r in t); tot = sum(r["total_ms"] for r in t)
            print(f"  time: judge/primary={m['extra_time_ratio']:.3f}  features total={tot / prim:.3f}  "
                  f"encode={enc / prim:.3f}  align(4 or 2 teacher-forced passes/window)={al / prim:.3f}  "
                  f"=> with encoder reuse ~{(tot - enc) / prim:.3f}x; windows={sum(r['n_win'] for r in t)} segs={len(t)}")
            short = [r for r in t if r["decode_ms"] < 500]
            if short:
                print(f"  short segments (<500 ms decode, n={len(short)}): features/decode="
                      f"{sum(r['total_ms'] for r in short) / sum(r['decode_ms'] for r in short):.2f}, "
                      f"align only {sum(r['align_ms'] for r in short) / sum(r['decode_ms'] for r in short):.2f}")


if __name__ == "__main__":
    main()
