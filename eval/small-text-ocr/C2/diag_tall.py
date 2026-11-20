"""Numbers only: geometry of tall strips (h > 60) in replay <= 11 px crops."""
import sys
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness")); sys.path.insert(0, str(HERE))
import c2, replay
from shared import score
run_id = replay.RUNS[0]
_p, _k, conn, media = score.open_store(run_id)
rows, _ = replay.select(run_id, conn, 100)
rows = [r for r in rows if r["res"] == "2560x1440" and r["bucket"] == "<=11"][:12]
cache = {}
seen = 0
for r in rows:
    crop, k = replay._crop(conn, media, r, cache)
    g = np.asarray(crop.convert("L")).astype(np.int16)
    mask = np.zeros(g.shape, bool); mask[:, 1:] = np.abs(np.diff(g, axis=1)) > c2.EDGE_T
    for x0, y0, x1, y1, h in c2.strips(g.astype(np.uint8)):
        if h > 60 and seen < 3:
            seen += 1
            sub = mask[y0:y1, x0:x1]
            colfrac = sub.mean(axis=0)
            rowany = sub.any(axis=1)
            print(f"box x {x0}-{x1} y {y0}-{y1} (crop {crop.size}); rows with ink {rowany.mean():.2f}; "
                  f"cols with ink in >50% rows: {np.flatnonzero(colfrac > 0.5)[:20].tolist()} n={int((colfrac > 0.5).sum())}")
            # longest empty-row gap and the row profile when the busiest columns are removed
            busy = colfrac > 0.5
            rp = sub[:, ~busy].any(axis=1)
            print(f"   without busy cols: rows with ink {rp.mean():.2f}, runs {len(c2._runs(rp, 1))}")
conn.close()
