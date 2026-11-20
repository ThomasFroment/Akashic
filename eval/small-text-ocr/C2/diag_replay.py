"""Why does C2 not help on the replay <= 11 px rows? Numbers only (no pixels or text are written or printed).

Per sampled replay row at 2560x1440 in the <= 11 bucket: crop size, strips, small strips, run-height
quantiles, chosen s and the budget figures.

    uv run python eval/small-text-ocr/C2/diag_replay.py [run_index] [max_rows]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
sys.path.insert(0, str(HERE))
import c2  # noqa: E402
import replay  # noqa: E402
from shared import Ctx, score  # noqa: E402


def main() -> None:
    run_id = replay.RUNS[int(sys.argv[1]) if len(sys.argv) > 1 else 0]
    cap = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    _p, _k, conn, media = score.open_store(run_id)
    try:
        rows, _ = replay.select(run_id, conn, 100)
        rows = [r for r in rows if r["res"] == "2560x1440" and r["bucket"] == "<=11"][:cap]
        cache: dict = {}
        for r in rows:
            crop, k = replay._crop(conn, media, r, cache)
            gray = np.asarray(crop.convert("L"))
            boxes = c2.strips(gray)
            h = np.array([b[4] for b in boxes]) if boxes else np.array([0])
            stats: dict = {}
            im, info = c2.mosaic(crop, Ctx("replay", r["scale"], None, None, {"stats": stats}))
            print(f"crop={crop.size} k={k:.2f} strips={len(boxes)} small={int((h <= c2.SMALL_H).sum())} "
                  f"h p10/50/90={np.percentile(h, [10, 50, 90]).round(0).tolist()} s={info.get('s')} "
                  f"today={info.get('today_ms', 0):.0f} pred={info.get('pred_ms', 0):.0f} out={im.size}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
