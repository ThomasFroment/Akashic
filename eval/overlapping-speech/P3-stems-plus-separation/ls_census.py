"""GPU census of the language split on whole runs (numbers only): P3's capture front end, then
candidate.lang_split on every kept system utterance (FR/EN detection passes only, no decode).
Gives the detection calls and GPU seconds per run that p3_run's back-end GPU figure leaves out.
Writes ls-census.json.

    uv run python eval/overlapping-speech/P3-stems-plus-separation/ls_census.py [std,day]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402

cand = core.load_candidate(HERE / "candidate.py")


def main() -> None:
    names = (sys.argv[1] if len(sys.argv) > 1 else "std,day").split(",")
    models = core.load_models()
    out = {}
    for run_id in core.RUNS:
        name = core.SHORT[run_id]
        if name not in names:
            continue
        run = core.Run(run_id)
        ctx = core.Ctx(run, *models)
        ctx.p3 = {}
        front = cand.Front(ctx)
        rows = splits = 0
        for w0, w1 in cand.capture_windows(run.segments, run):
            streams: dict = {}
            for c0 in range(w0, w1, cand.CHUNK_MS):
                c1 = min(w1, c0 + cand.CHUNK_MS)
                outs = front.chunk(c0, c1, streams)
                if c1 == w1:
                    for key in list(streams):
                        outs += [(key, u) for u in streams.pop(key).finish()]
                for key, u in outs:
                    if front.classify(key, u)[0] is None:
                        continue
                    parts, _ = cand.lang_split(ctx, u.samples)
                    rows += 1
                    splits += len(parts) > 1
        out[name] = {"system_rows": rows, "splits": splits, "detect_calls": ctx.p3.get("ls_detect_calls", 0), "ls_gpu_s": ctx.p3.get("ls_gpu_s", 0.0),
                     "recorded_h": (run.truth.t1 - run.truth.t0) / 3.6e6}
        print(name, out[name], flush=True)
    (HERE / "ls-census.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
