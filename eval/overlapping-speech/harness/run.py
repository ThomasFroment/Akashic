"""Overlapping-speech harness: replay a candidate on the standard and day runs and on the synthetic
set, score it with the quality scorer's own functions, time it, and write <out>/side.png,
<out>/metrics.txt and <out>/detail.json.

    uv run python eval/overlapping-speech/harness/run.py <candidate.py> [<out_dir>] [--runs std,day] [--no-syn] [--no-cost]

Run from the repo root. <out_dir> defaults to the candidate's folder. The baseline
(baseline/baseline.py = today's pipeline on the stored segments) is the reference: side.png puts
the candidate beside baseline/detail.json, so run the baseline first.

Real runs: the candidate's transcripts become rows of an in-memory SQLite with the store's column
names (audio_segments, speaker_turns), and the unchanged score.m7 and score.m8_m9 score them
against the run's truth (M9.1 WER per group, M8.2 DER, M8.3 authors, M7.1 leak). Only numbers are
printed or written.

metrics.txt (one line):
  ovl_std / ovl_day        M9.1 'overlap' WER (must <= 0.25)
  ovl_local_*              diagnostic, not a must: overlap groups' substitutions + deletions when each
                           truth utterance is matched to its best stretch of the group's text (order-
                           free, no insertions). M9.1 itself joins the references in utterance start
                           order and the transcripts in row start order, so two parallel transcripts
                           over one span (one per source) are scored as badly ordered text
  clean_std / clean_day    M9.1 'no_overlap' WER; en_*, fr_*, mixed_* the language groups (must not rise)
  der_std / der_day        M8.2 DER (must not rise); wrong_* / unk_* M8.3 wrong and unknown authors
  leak_std / leak_day      M7.1 leak seconds other than Akashic itself
  excl_s                   seconds of stored transcript overlapping an excluded app's sound, both runs
                           (must 0; today's capture drops such segments whole)
  syn_wer / syn_cpwer      synthetic scorer-style WER and cpWER; syn_q50 / syn_l50 the quieter and
                           louder talker's WER (words found anywhere) at overlap >= 50 %;
                           syn_solo = single-talker WER (clean reference), syn_ov0 = back-to-back talkers WER; syn_der multi-label DER
  gpu_s_h                  GPU seconds per hour of recorded time over the fixed cost window (median
                           of >= 9); gpu_x = ratio to the baseline (budget 1.30)
  vram_gb                  peak GPU memory of the process while transcribing (budget baseline + 1.5)
  cap_cpu                  capture-side CPU, % of one core (budget +5)
  uses_sim                 the candidate read simulated per-process streams
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import errors  # noqa: E402
import score  # noqa: E402

COST_REPS = 9


def score_run(run: core.Run, transcripts: list[core.Transcript]) -> dict:
    conn = core.scoring_db(transcripts)
    tr = run.truth
    m7 = score.m7(tr, conn)
    m89 = score.m8_m9(tr, conn)
    rows = conn.execute("SELECT id, stream, start_ms, end_ms, speech_ms, transcript, speaker_id, language FROM audio_segments").fetchall()
    utts = tr.utterances()
    for u in utts:
        u["proc"] = run.utterance_proc(u)
    from diagnose import talker_levels

    recs = errors.analyse(utts, rows, talker_levels(run, utts))
    ov = [r for r in recs if r["overlap"]]
    ref = sum(r["ref"] for r in ov)
    # excluded app: seconds of non-empty system transcript while an excluded process was sounding
    excl = score.merge([(a + run.lag(k), a + run.lag(k) + len(x) * 1000 // core.SR) for k, t in run.tracks().items()
                        if t.exe in core.EXCLUDED for a, x, _ in t.items])
    excl_s = score.overlap_ms(excl, score.merge([(t.start_ms, t.end_ms) for t in transcripts if t.stream == "system" and t.text])) / 1000
    excl_s += sum((t.end_ms - t.start_ms) / 1000 for t in transcripts if set(t.sources) & core.EXCLUDED)
    return {"M7.1": m7["M7.1"], "M8.2": m89["M8.2"], "M8.3": m89["M8.3"], "M9.1": m89["M9.1"], "M9.2": m89["M9.2"],
            "excluded_s": excl_s, "overlap_local_wer": sum(r["local_errors"] for r in ov) / ref if ref else None, "overlap_error_types": {**errors.merge_counts(ov, "ops_by_region"), "I": sum(r["I"] for r in ov)},
            "overlap_by_pair": {k: errors.totals([r for r in ov if k in r["pairs"]], lambda r: True) for k in sorted({p for r in ov for p in r["pairs"]})},
            "n_out": len(transcripts)}


def replay(cand, source, models) -> tuple[list[core.Transcript], core.Ctx, float]:
    ctx = core.Ctx(source, *models)
    t = time.perf_counter()
    out = cand.process(list(source.segments), ctx)
    return out, ctx, time.perf_counter() - t


def cost(cand_path: Path) -> dict:
    """Fresh process: VRAM peak is per process, and CTranslate2 keeps its allocator cache."""
    out = subprocess.run([sys.executable, str(Path(__file__).with_name("cost.py")), str(cand_path), str(COST_REPS)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def metrics_line(d: dict, base: dict | None) -> str:
    f = lambda v, n=3: "nan" if v is None else f"{v:.{n}f}"
    parts = []
    for name in ("std", "day"):
        r = d["runs"].get(name)
        if not r:
            continue
        m = r["M9.1"]
        parts += [f"ovl_{name}={f(m['overlap']['wer'])}", f"clean_{name}={f(m['no_overlap']['wer'])}",
                  f"en_{name}={f(m.get('en', {}).get('wer'))}", f"fr_{name}={f(m.get('fr', {}).get('wer'))}", f"mixed_{name}={f(m.get('mixed', {}).get('wer'))}",
                  f"ovl_local_{name}={f(r.get('overlap_local_wer'))}", f"der_{name}={f(r['M8.2'].get('der'))}", f"wrong_{name}={r['M8.3'].get('wrong')}", f"unk_{name}={r['M8.3'].get('unknown')}",
                  f"leak_{name}={f(r['M7.1'].get('leak_seconds_other_than_self'), 1)}"]
    if d["runs"]:
        parts.append(f"excl_s={f(sum(r['excluded_s'] for r in d['runs'].values()), 1)}")
    s = d.get("synthetic")
    if s:
        sm = s["summary"]
        parts += [f"syn_wer={f(sm['wer'])}", f"syn_cpwer={f(sm['cpwer'])}", f"syn_q50={f(sm['talker_any_wer_quiet_ov50'])}",
                  f"syn_l50={f(sm['talker_any_wer_loud_ov50'])}", f"syn_solo={f(sm['wer_solo'])}", f"syn_ov0={f(sm['wer_ov0'])}", f"syn_der={f(sm['der'])}"]
    c = d.get("cost")
    if c:
        bx = c["gpu_s_per_h"] / base["cost"]["gpu_s_per_h"] if base and base.get("cost") else 1.0
        parts += [f"gpu_s_h={f(c['gpu_s_per_h'], 1)}", f"gpu_x={f(bx, 2)}", f"vram_gb={f(c['vram_peak_mib'] / 1024, 2)}", f"cap_cpu={f(c['capture_cpu_pct'], 2)}"]
    parts.append(f"uses_sim={'yes' if d.get('uses_sim') else 'no'}")
    return " ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", type=Path)
    ap.add_argument("out", type=Path, nargs="?")
    ap.add_argument("--runs", default="std,day")
    ap.add_argument("--no-syn", action="store_true")
    ap.add_argument("--no-cost", action="store_true")
    ap.add_argument("--only-cost", action="store_true", help="re-measure the cost into an existing <out>/detail.json")
    args = ap.parse_args()
    out = args.out or args.candidate.resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    if args.only_cost:
        detail = json.loads((out / "detail.json").read_text(encoding="utf-8"))
        wanted, args.no_syn = set(), True
    else:
        cand = core.load_candidate(args.candidate.resolve())
        models = core.load_models()
        detail = {"candidate": args.candidate.name, "runs": {}, "uses_sim": False}
        wanted = {n.strip() for n in args.runs.split(",") if n.strip()}
    for run_id in core.RUNS:
        name = core.SHORT[run_id]
        if name not in wanted:
            continue
        run = core.Run(run_id)
        ts, ctx, wall = replay(cand, run, models)
        res = score_run(run, ts)
        res.update({"gpu_s": ctx.gpu_s, "wall_s": wall, "cpu_speakers_s": ctx.cpu_speakers_s, "cpu_capture_s": ctx.cpu_capture_s,
                    "audio_s_transcribed": ctx.audio_s_decoded, "recorded_h": (run.truth.t1 - run.truth.t0) / 3.6e6})
        detail["runs"][name] = res
        detail["uses_sim"] |= ctx.used_sim
        print(f"{name}: overlap WER {res['M9.1']['overlap']['wer']:.3f} no_overlap {res['M9.1']['no_overlap']['wer']:.3f} DER {res['M8.2'].get('der')} "
              f"gpu {ctx.gpu_s:.0f} s wall {wall:.0f} s", flush=True)
    if not args.no_syn:
        import synthetic

        sset = synthetic.Set()
        ts, ctx, wall = replay(cand, sset, models)
        detail["synthetic"] = synthetic.score_set(sset, ts)
        detail["synthetic"]["gpu_s"] = ctx.gpu_s
        detail["synthetic"]["audio_s"] = sum(len(x) for x in sset.mixed.values()) / core.SR
        detail["uses_sim"] |= ctx.used_sim
        sm = detail["synthetic"]["summary"]
        print(f"synthetic: wer {sm['wer']:.3f} cpwer {sm['cpwer']:.3f} der {sm['der']:.3f} quiet50 {sm['talker_any_wer_quiet_ov50']:.3f}", flush=True)
    if not args.no_cost:
        detail["cost"] = cost(args.candidate.resolve())
        print(f"cost: {detail['cost']}", flush=True)
    base_path = core.STUDY / "baseline" / "detail.json"
    base = json.loads(base_path.read_text(encoding="utf-8")) if base_path.is_file() and out.resolve() != base_path.parent.resolve() else None
    (out / "detail.json").write_text(json.dumps(detail, indent=1), encoding="utf-8")
    line = metrics_line(detail, base or detail)
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    import draw

    draw.side(detail, base, out / "side.png")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
