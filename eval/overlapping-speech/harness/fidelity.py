"""How faithful the simulated per-process streams are, measured on the runs.

    uv run python eval/overlapping-speech/harness/fidelity.py

1. Re-render vs stored audio (from the calibration): share of the stored band energy the re-render
   explains, per stream; per-source lag spread; gains.
2. ASR: on system segments where a single harness source was sounding (one recorded track active,
   no bed, no Outer Wilds), Whisper (production decode) on the stored audio vs on the simulated
   process stream over the same span; WER of each against the truth utterances whose midpoint
   falls inside. Up to N segments per source per run, evenly spaced.
Writes ../fidelity.json; prints numbers only.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import score  # noqa: E402

N = 40


def main() -> int:
    tr_model, voices = core.load_models()
    res = {}
    for run_id in core.RUNS:
        run = core.Run(run_id)
        cal = run.calibration()
        utts = run.truth.utterances()
        aud = score.audible_spans(run.truth)
        others = score.merge([s for exe, sp in aud.items() if exe.lower() in ("outerwilds.exe", "qualitygame.exe", "spotify.exe") for s in sp])
        by_src = defaultdict(list)
        for seg in run.segments:
            if seg.stream != "system":
                continue
            act = [t for t in run.recorded_tracks() if t.active(seg.start_ms - 500, seg.end_ms + 500)]
            if len(act) != 1 or score.overlap_ms([(seg.start_ms, seg.end_ms)], others):
                continue
            by_src[act[0].exe].append(seg)
        ctx = core.Ctx(run, tr_model, voices)
        out = {}
        for exe, segs in by_src.items():
            pick = [segs[i] for i in np.linspace(0, len(segs) - 1, min(N, len(segs))).astype(int)] if segs else []
            e_st = e_sim = e_x = refw = 0
            for seg in pick:
                ref = score.fold(" ".join(u["text"] for u in utts if u["stream"] == "system" and seg.start_ms <= (u["t0"] + u["t1"]) / 2 < seg.end_ms)).split()
                x = ctx.audio(seg)
                y = ctx.process_stream(exe, seg.start_ms, seg.start_ms + len(x) * 1000 // core.SR)
                a = ctx.decode(x)
                b = ctx.decode(y)
                ha = score.fold(a.text if a else "").split()
                hb = score.fold(b.text if b else "").split()
                e_st += score.lev(ref, ha)
                e_sim += score.lev(ref, hb)
                e_x += score.lev(ha, hb)
                refw += len(ref)
            out[exe] = {"segments": len(pick), "ref_words": refw, "wer_stored": e_st / max(1, refw), "wer_simulated": e_sim / max(1, refw),
                        "stored_vs_simulated_wer": e_x / max(1, refw)}
            print(core.SHORT[run_id], exe, out[exe], flush=True)
        ex = defaultdict(list)
        for x in cal["segments"]:
            if x["explained"] is not None:
                ex[x["stream"]].append(x["explained"])
        res[core.SHORT[run_id]] = {"asr": out, "gain": cal["gain"], "lag_ms": cal["lag"], "lag_mad_ms": cal["lag_mad"],
                                   "explained_band_energy_p10_p50_p90": {k: [float(np.percentile(v, q)) for q in (10, 50, 90)] for k, v in ex.items()}}
    (core.STUDY / "fidelity.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
