"""Profile C2's preprocessing on replay crops (numbers only)."""
import cProfile, pstats, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness")); sys.path.insert(0, str(HERE))
import c2, replay
from shared import Ctx, score
run_id = replay.RUNS[1]
_p, _k, conn, media = score.open_store(run_id)
rows, _ = replay.select(run_id, conn, 20); cache = {}
crops = [replay._crop(conn, media, r, cache)[0] for r in rows[:20]]
conn.close()
pr = cProfile.Profile(); pr.enable()
for c in crops: c2.mosaic(c, Ctx("replay", 1.0, None, None, {"force_s": 2.0}))
pr.disable()
pstats.Stats(pr).sort_stats("tottime").print_stats(14)
