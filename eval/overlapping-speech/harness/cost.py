"""Cost of a candidate on the fixed cost window, in a fresh process (VRAM peak is per process).

    uv run python eval/overlapping-speech/harness/cost.py <candidate.py> [reps]

Fixed window: the 6 minutes of the standard run with the most overlapped system speech (the
scorer's truth utterances), all stored segments starting in it, both streams. One warm-up call
(loads the models), then `reps` timed calls, each with a fresh context (fresh speaker profiles).
Prints one JSON line: GPU seconds per hour of recorded time (median), per hour of segment audio,
peak VRAM taken (device used memory minus at start: nothing else may load the GPU meanwhile), capture-side CPU in % of one core (median), CPU seconds
of the speaker step per hour.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

WINDOW_MS = 6 * 60_000


def cost_window(run: core.Run) -> tuple[int, int]:
    utts = [u for u in run.truth.utterances() if u["stream"] == "system"]
    utts.sort(key=lambda u: u["t0"])
    ov = []
    for i, a in enumerate(utts):
        for b in utts[i + 1:]:
            if b["t0"] >= a["t1"]:
                break
            ov.append((b["t0"], min(a["t1"], b["t1"])))
    best, start = -1, run.truth.t0
    for t in range(run.truth.t0, run.truth.t1 - WINDOW_MS, 30_000):
        v = sum(max(0, min(b, t + WINDOW_MS) - max(a, t)) for a, b in ov)
        if v > best:
            best, start = v, t
    return start, start + WINDOW_MS


def main() -> int:
    cand = core.load_candidate(Path(sys.argv[1]))
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 9
    models = core.load_models()
    run = core.Run(core.RUNS[0])
    a, b = cost_window(run)
    segs = [s for s in run.segments if a <= s.start_ms < b]
    seg_h = sum(s.end_ms - s.start_ms for s in segs) / 3.6e6
    gpu, cap, spk = [], [], []
    with core.VramPeak() as vp:
        for i in range(reps + 1):
            ctx = core.Ctx(run, *models)
            cand.process(segs, ctx)
            if i:
                gpu.append(ctx.gpu_s)
                cap.append(ctx.cpu_capture_s)
                spk.append(ctx.cpu_speakers_s)
    win_h = WINDOW_MS / 3.6e6
    print(json.dumps({"window": [a, b], "segments": len(segs), "segment_audio_h": seg_h, "reps": reps,
                      "gpu_s_per_h": statistics.median(gpu) / win_h, "gpu_s_per_segment_audio_h": statistics.median(gpu) / seg_h,
                      "gpu_s_runs": gpu, "vram_peak_mib": vp.peak_mib,
                      "capture_cpu_pct": statistics.median(cap) / (WINDOW_MS / 1000) * 100,
                      "speaker_cpu_s_per_h": statistics.median(spk) / win_h}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
