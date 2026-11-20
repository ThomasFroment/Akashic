"""Pre-flight for evaluate.py: replay the candidate on the first N gated system segments of the
standard run (plus nothing else), then run every scoring step evaluate.py runs on the result, so a
scoring crash shows in minutes, not after an hour-long replay. Prints numbers only.

    uv run python eval/overlapping-speech/P2-gated-separation/test_score.py [N]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evaluate as ev  # noqa: E402

core, hr = ev.core, ev.hr


class Sub:
    def __init__(self, run, segs):
        self._run, self.segments = run, segs

    def __getattr__(self, k):
        return getattr(self._run, k)


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    cand = core.load_candidate(HERE / "candidate.py")
    models = core.load_models()
    run = core.Run(core.RUNS[0])
    ctx = core.Ctx(run, *models)
    gated = [s for s in run.segments if s.stream == "system" and cand.gate(cand.activity(ctx, run.audio(s)))][:n]
    cand.reset_stats()
    ts, ctx, wall = hr.replay(cand, Sub(run, gated), models)
    res = hr.score_run(run, ts)
    conn = core.scoring_db(ts)
    res["p2_extras"] = ev.extras(run, conn)
    res["p2_stats"] = ev.stats_summary(cand.STATS)
    res["p2_gate"] = ev.gate_report(run, cand.STATS)
    s = json.dumps(res, default=ev._num)
    print("ok", len(s), {k: v for k, v in res["p2_stats"].items() if isinstance(v, (int, float))}, "wall", round(wall, 1))


if __name__ == "__main__":
    main()
