"""Noise share of P2's index and its floors (counts only; the harness lexicon is used for analysis).

For each run: the indexed tokens' noise share, and what it would be if an oracle garbage detector
removed (a) every 'garbled' unknown token (not word-like), (b) every unknown OCR token. Shows how
much of today's noise is real but unlisted text (names, UI words) that no garbage rule may remove.

    uv run python .../noise_floor.py [runs] -> noise_floor.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import replay as R  # noqa: E402
import shared as S  # noqa: E402
import cand  # noqa: E402

INDEXED = ("content", "chrome")


def main() -> None:
    runs = (sys.argv[1] if len(sys.argv) > 1 else "smoke,standard,day").split(",")
    base = json.loads((S.STUDY / "baseline" / "details.json").read_text(encoding="utf-8"))
    S.init_corpus()
    res = {}
    for key in runs:
        truth = S.RunTruth(S.RUNS[key])
        store = S.load_store(S.RUNS[key], truth)
        lx = S.Lexicon(truth.strings)
        sh = cand.prepare(R.fresh_windows([(w.history, w.ctx) for w in store.windows]))
        src = {(h, o.frame_id): o.source for h, o in store.order}
        c: Counter = Counter()
        for h, st in sh["out"].items():
            for kl in st.lines:
                if kl.kind not in INDEXED:
                    continue
                s = "uia" if src.get((h, kl.frame_id)) == "uia" else "ocr"
                for t in S.tokens(kl.text):
                    c["tok"] += 1
                    if lx.classify(t) == "unknown":
                        g = lx.garbled(t)
                        c["unk"] += 1
                        c[f"unk_{s}"] += 1
                        c[f"unk_{s}_garbled"] += g
        b = base[key]["noise"]["share"]
        share = c["unk"] / c["tok"]
        no_garb = (c["unk"] - c["unk_ocr_garbled"] - c["unk_uia_garbled"]) / (c["tok"] - c["unk_ocr_garbled"] - c["unk_uia_garbled"])
        no_ocr = c["unk_uia"] / (c["tok"] - c["unk_ocr"])
        res[key] = {"counts": dict(c), "share": share, "x": share / b, "oracle_no_garbled_x": no_garb / b, "oracle_no_ocr_unknown_x": no_ocr / b,
                    "baseline_share": b}
        print(key, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in res[key].items() if k != "counts"}, dict(c), flush=True)
        store.conn.close()
    (HERE / "noise_floor.json").write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
