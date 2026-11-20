"""Driver for P1 (numbers only): replay the candidate once per run with the harness's own replay and
scoring (harness/run.py functions, unchanged), and score the same transcripts a second way:
truth utterances spoken while Akashic was paused (>= 50 % of the utterance inside a driver
pause/resume span) removed. Writes <out>/detail.json, metrics.txt, side.png like run.py, plus
detail["runs_nopause"] and detail["p1"] (candidate counters). Cost: run harness/run.py --only-cost
afterwards into the same <out>.

    uv run python eval/overlapping-speech/P1-per-process-streams/p1_run.py <candidate.py> <out> [--runs std,day] [--no-syn]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
import core  # noqa: E402
import score  # noqa: E402

hr = core.load_candidate(HARNESS / "run.py")  # harness/run.py (eval/quality has its own run.py)


def nopause(run: core.Run) -> tuple[list[dict], int]:
    utts = core.score.Truth.utterances(run.truth)
    pauses = score.truth_gap_kinds(run.truth).get("pause", [])
    keep, removed = [], 0
    for u in utts:
        inside = sum(max(0, min(u["t1"], b) - max(u["t0"], a)) for a, b in pauses)
        if inside >= 0.5 * (u["t1"] - u["t0"]):
            removed += len(score.fold(u["text"]).split())
        else:
            keep.append(u)
    return keep, removed


def excl_spans(run: core.Run) -> list:
    return score.merge([(a + run.lag(k), a + run.lag(k) + len(x) * 1000 // core.SR) for k, t in run.tracks().items()
                        if t.exe in core.EXCLUDED for a, x, _ in t.items])


def excl_split(run: core.Run, ts: list) -> tuple[list, dict]:
    """The harness's excl_s counts any system transcript overlapping an excluded app's sound. Only the
    residual (endpoint mix) can hold that sound; a per-app stem cannot. Returns the transcripts with
    every system row overlapping excluded sound dropped (strict, as today's capture does), and the
    seconds per row kind."""
    ex = excl_spans(run)
    keep, secs = [], {}
    for t in ts:
        ov = score.overlap_ms(ex, [(t.start_ms, t.end_ms)]) / 1000 if t.stream == "system" and t.text else 0.0
        if ov > 0:
            k = getattr(t, "p1_kind", "?")
            secs[k] = secs.get(k, 0.0) + ov
            continue
        keep.append(t)
    return keep, secs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--runs", default="std,day")
    ap.add_argument("--no-syn", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    cand = core.load_candidate(a.candidate.resolve())
    models = core.load_models()
    detail = {"candidate": a.candidate.name, "runs": {}, "runs_nopause": {}, "uses_sim": False, "p1": {}}
    wanted = {n.strip() for n in a.runs.split(",") if n.strip()}
    for run_id in core.RUNS:
        name = core.SHORT[run_id]
        if name not in wanted:
            continue
        run = core.Run(run_id)
        ts, ctx, wall = hr.replay(cand, run, models)
        res = hr.score_run(run, ts)
        res.update({"gpu_s": ctx.gpu_s, "wall_s": wall, "cpu_speakers_s": ctx.cpu_speakers_s, "cpu_capture_s": ctx.cpu_capture_s,
                    "audio_s_transcribed": ctx.audio_s_decoded, "recorded_h": (run.truth.t1 - run.truth.t0) / 3.6e6})
        detail["runs"][name] = res
        detail["uses_sim"] |= ctx.used_sim
        detail["p1"][name] = getattr(ctx, "p1", None)
        strict_ts, secs = excl_split(run, ts)
        r3 = hr.score_run(run, strict_ts)
        r3["excl_overlap_s_by_kind"] = secs
        r3["rows_dropped"] = len(ts) - len(strict_ts)
        detail.setdefault("runs_strict_excl", {})[name] = r3
        keep, removed = nopause(run)
        run.truth.utterances = lambda keep=keep: [dict(u) for u in keep]
        r2 = hr.score_run(run, ts)
        r2["paused_words_removed"] = removed
        detail["runs_nopause"][name] = r2
        r4 = hr.score_run(run, strict_ts)
        r4["paused_words_removed"] = removed
        detail.setdefault("runs_nopause_strict_excl", {})[name] = r4
        print(f"{name}: overlap WER {res['M9.1']['overlap']['wer']:.3f} (no pause {r2['M9.1']['overlap']['wer']:.3f}, local {r2['overlap_local_wer']:.3f}) "
              f"no_overlap {res['M9.1']['no_overlap']['wer']:.3f} DER {res['M8.2'].get('der')} gpu {ctx.gpu_s:.0f} s wall {wall:.0f} s", flush=True)
        (a.out / "detail.json").write_text(json.dumps(detail, indent=1, default=str), encoding="utf-8")
    if not a.no_syn:
        import synthetic

        sset = synthetic.Set()
        ts, ctx, wall = hr.replay(cand, sset, models)
        detail["synthetic"] = synthetic.score_set(sset, ts)
        detail["synthetic"]["gpu_s"] = ctx.gpu_s
        detail["synthetic"]["audio_s"] = sum(len(x) for x in sset.mixed.values()) / core.SR
        detail["uses_sim"] |= ctx.used_sim
        detail["p1"]["synthetic"] = getattr(ctx, "p1", None)
        sm = detail["synthetic"]["summary"]
        print(f"synthetic: wer {sm['wer']:.3f} cpwer {sm['cpwer']:.3f} der {sm['der']:.3f} quiet50 {sm['talker_any_wer_quiet_ov50']:.3f}", flush=True)
    base = json.loads((core.STUDY / "baseline" / "detail.json").read_text(encoding="utf-8"))
    (a.out / "detail.json").write_text(json.dumps(detail, indent=1, default=str), encoding="utf-8")
    line = hr.metrics_line(detail, base)
    (a.out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    import draw

    draw.side(detail, base, a.out / "side.png")
    print(line)
    np_line = hr.metrics_line({"runs": detail["runs_nopause"]}, base)
    print("nopause:", np_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
