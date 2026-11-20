"""M3.1 on frames rebuilt from P2's line store (user answer 3): each frame row keeps its ordered line
ids and digit slots, so its text is the canonical reading of each line with that frame's own digits.
Scored with the harness's M3.1 code (same sample as eval/quality/score.py). Numbers only.

    uv run python .../m31_rebuilt.py [runs] -> prints and writes m31_rebuilt.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import replay as R  # noqa: E402
import shared as S  # noqa: E402
import cand  # noqa: E402


def main() -> None:
    runs = (sys.argv[1] if len(sys.argv) > 1 else "smoke,standard,day").split(",")
    S.init_corpus()
    res = {}
    for key in runs:
        truth = S.RunTruth(S.RUNS[key])
        store = S.load_store(S.RUNS[key], truth)
        sh = cand.prepare(R.fresh_windows([(w.history, w.ctx) for w in store.windows]))
        out = {}
        for h, frames in sh["rebuild"].items():
            out[h] = S.StoredText([S.KeptLine(ln, 0, ts, ts, None) for ts, lines in frames for ln in lines])
        m = R.m31(store, truth, out, {})
        res[key] = {k: v for k, v in m.items()}
        store.conn.close()
        print(key, {k: round(v["cer"], 4) for k, v in m.items()}, flush=True)
    (HERE / "m31_rebuilt.json").write_text(json.dumps(res, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
