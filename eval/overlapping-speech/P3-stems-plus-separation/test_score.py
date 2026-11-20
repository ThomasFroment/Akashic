"""Pre-flight for p3_run.py (P2's test_score.py idea): replay P3 (shadow on) on a short slice of the
standard run (the harness cost window, where overlap is densest) and on the first mixtures of the
synthetic set, then run every analysis and scoring step p3_run.py and finalize.py run, into a
throwaway folder. Prints numbers only.

    uv run python eval/overlapping-speech/P3-stems-plus-separation/test_score.py <tmp_out> [minutes]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import p3_run as pr  # noqa: E402  (sets P3_SHADOW=1)

core, hr = pr.core, pr.hr


class Sub:
    def __init__(self, src, segs):
        self._src, self.segments = src, segs

    def __getattr__(self, k):
        return getattr(self._src, k)


def main() -> None:
    out = Path(sys.argv[1])
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 3
    out.mkdir(parents=True, exist_ok=True)
    import cost

    cand = core.load_candidate(HERE / "candidate.py")
    models = core.load_models()
    run = core.Run(core.RUNS[0])
    a, _ = cost.cost_window(run)
    same = sorted(lo for lo, hi, pa, pb in pr.truth_pairs(run) if pa == pb and hi - lo >= 500)
    if same:  # the separation path needs same-app overlap inside one stream: start before the first one
        a = same[0] - 30_000
    b = a + int(minutes * 60_000)
    segs = [s for s in run.segments if a <= s.start_ms < b]
    t = time.perf_counter()
    ts, ctx, wall = hr.replay(cand, Sub(run, segs), models)
    an = pr.analyse_run(run, ts, ctx, wall)
    detail = {"candidate": "test", "runs": {}, "runs_nopause": {}, "shadow_p1": {"runs": {}, "runs_nopause": {}}, "uses_sim": True}
    pr.store(detail, "std", an)
    p3 = an["p3"]
    print("std slice: segs", len(segs), "rows", len(ts), "shadow rows", len(ctx.p3_shadow), "gated", p3["gated_utts"], "/", p3["gate_utts"],
          "sep used", p3["sep_used_utts"], "fallback", p3["sep_fallback_utts"], "pieces", p3["pieces"], "sep wall", round(p3["sep"]["sep_wall_s"], 1),
          "sep cpu", round(p3["sep"]["sep_cpu_s"], 1), "excl", an["run"]["excluded_s"], "dropped", p3["front"]["dropped"],
          "gpu p3", round(an["run"]["gpu_s"], 1), "ctx", round(ctx.gpu_s, 1), "p1", round(ctx.p3["backends"]["p1"]["gpu_s"], 1), "wall", round(wall, 1), flush=True)
    print("gate", {k: v for k, v in an["gate"].items() if not isinstance(v, dict)}, {k: v["recall"] for k, v in an["gate"].items() if isinstance(v, dict)})
    print("mixed groups", {k: v["all"]["groups"] for k, v in an["mixed"]["breakdown"].items()})
    # synthetic: the first mixtures only
    import synthetic

    sset = synthetic.Set()
    i0 = next(i for i, m in enumerate(sset.mixes) if m.procs == "one" and m.overlap >= 0.5)
    m0, m_end = sset.mixes[i0].t0, sset.mixes[min(len(sset.mixes) - 1, i0 + 3)].t0
    ssegs = [s for s in sset.segments if m0 <= s.start_ms < m_end]
    ts2, ctx2, wall2 = hr.replay(cand, Sub(sset, ssegs), models)
    detail["synthetic"] = pr.analyse_syn(sset, ts2, ctx2, wall2)
    print("syn slice: rows", len(ts2), "gate", detail["synthetic"]["p3_gate"].get("all"), "wall", round(wall2, 1))
    (out / "detail.json").write_text(json.dumps(detail, indent=1, default=pr._num), encoding="utf-8")
    import finalize

    finalize.main(out, check=True)
    print("preflight ok", round(time.perf_counter() - t, 1), "s")


if __name__ == "__main__":
    main()
