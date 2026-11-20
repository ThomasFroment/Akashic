"""Smoke test: the candidate on the first N system segments of the standard run; prints stats only.

    uv run python eval/overlapping-speech/P2-gated-separation/smoke.py [N]
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    cand = core.load_candidate(HERE / "candidate.py")
    run = core.Run(core.RUNS[0])
    # gate only over every system segment first (cheap): which segments would be separated
    ctx = core.Ctx(run, *core.load_models())
    gated = []
    for s in run.segments:
        if s.stream == "system":
            sp = cand.gate(cand.activity(ctx, run.audio(s)))
            if sp:
                gated.append(s)
    sysn = sum(1 for s in run.segments if s.stream == "system")
    print(f"system segments {sysn}, gated {len(gated)}")
    cand.reset_stats()
    out = cand.process(gated[:n], ctx)
    st = {k: v for k, v in cand.STATS.items() if not isinstance(v, list)}
    print(st, "rows", len(out), "rows with text", sum(1 for t in out if t.text), "gpu_s", round(ctx.gpu_s, 1))


if __name__ == "__main__":
    main()
