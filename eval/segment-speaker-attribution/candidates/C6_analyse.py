"""C6: why authored segments fail, per run, and timing vs the concurrent reference rerun.

    ..\\.venv\\Scripts\\python.exe candidates\\C6_analyse.py C6_a_resnet34 C6_b_dual ...

Failure classes for a segment with an expected author and no correct author (first match wins):
  lead      - a continuation second with a turn starting inside the 1.984 s lead whose id is not
              the expected profile (the previous speaker, or NULL);
  short     - a NULL turn whose span is < 1 s (stored unembedded, under MIN_IDENTIFY_MS);
  unmatched - a NULL turn of >= 1 s span (no profile >= threshold, and < 2 s so no new one);
  other     - a turn attributed to another profile (incl. a new one created from the segment);
  wrong     - the segment author is set but not the expected speaker.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parents[1]
OUT = HERE / "out"
LEAD = 1984


def load(name: str) -> dict:
    return json.loads((OUT / name / "details.json").read_text(encoding="utf-8"))


def classify(r: dict, own: set) -> str:
    if r["author"] is not None:
        return "wrong"
    turns = r["turns"]
    if "cont_second" in r["subsets"] and any(a < LEAD and s not in own for a, b, s in turns):
        return "lead"
    if any(s is None and b - a < 1000 for a, b, s in turns):
        return "short"
    if any(s is None for a, b, s in turns):
        return "unmatched"
    return "other" if turns else "noturns"


def main() -> None:
    names = ["reference", "C6_ref_rerun", *sys.argv[1:]]
    runs = {n: load(n) for n in names if (OUT / n / "details.json").is_file()}
    ref = runs["reference"]
    base = runs.get("C6_ref_rerun", ref)
    for n, d in runs.items():
        segs = d["segments"]
        exp = [r for r in segs if r["expected"] is not None]
        fails = [r for r in exp if not r["authored"]]
        # profile ids of the expected speaker are not in details; a turn's id is "own" if it
        # equals the id on any segment that run authored correctly for that speaker
        own_ids: dict[str, set] = {}
        for r in exp:
            if r["authored"]:
                own_ids.setdefault(r["expected"], set()).update(
                    t[2] for t in r["turns"] if t[2] is not None
                )
        cls = Counter(classify(r, own_ids.get(r["expected"], set())) for r in fails)
        t = d["summary"]["timing"]
        bt = base["summary"]["timing"]
        print(f"\n== {n}: authored {len(exp) - len(fails)}/{len(exp)}, failures {dict(cls)}")
        print(
            f"   p50 {t['p50_ms']:.1f} ({t['p50_ms'] / bt['p50_ms']:.2f}x rerun)  p95 "
            f"{t['p95_ms']:.1f} ({t['p95_ms'] / bt['p95_ms']:.2f}x)  mean {t['mean_ms']:.1f} "
            f"({t['mean_ms'] / bt['mean_ms']:.2f}x)  emb fwd/seg {t['emb_forwards_mean']:.2f}"
            f" ({t['emb_ms_mean']:.1f} ms)  seg fwd/seg {t['seg_forwards_mean']:.2f}"
            f" ({t['seg_ms_mean']:.1f} ms)"
        )
        if n != "reference":
            by = {r["id"]: r for r in ref["segments"]}
            gained = sum(1 for r in exp if r["authored"] and not by[r["id"]]["authored"])
            lost = sum(1 for r in exp if not r["authored"] and by[r["id"]]["authored"])
            nw = sum(1 for r in segs if r["wrong"] and not by[r["id"]]["wrong"])
            fw = sum(1 for r in segs if not r["wrong"] and by[r["id"]]["wrong"])
            print(f"   vs reference: +{gained} / -{lost} authored, wrong +{nw} / -{fw}")
        sub = d["summary"]["subsets"]
        print("   " + "  ".join(
            f"{k}={sub[k]['authorship'][0]:.3f}" for k in
            ("solo", "solo_10k", "solo_10_0", "cont_first", "cont_second", "cont_same",
             "cont_switch", "cont_switch_early", "similar") if sub[k]["authorship"][0] is not None
        ))  # fmt: skip
        print("   wrong: " + "  ".join(
            f"{k}={sub[k]['wrong'][0]:.3f}" for k in
            ("solo", "similar", "mixed", "mixed_backchannel", "mixed_lastwin", "mixed_reply",
             "impostor", "cont_first", "cont_second")
        ))  # fmt: skip
        if d.get("drift"):
            dr = d["drift"]
            print(f"   drift wrong_all={dr['wrong_all']:.4f} auth_first100={dr['auth_first100']}"
                  f" auth_last100={dr['auth_last100']} cos_end={dr['cos_end']}")  # fmt: skip
    _ = np


if __name__ == "__main__":
    main()
