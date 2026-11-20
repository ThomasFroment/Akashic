"""P2 accuracy driver: replays candidate.py on the standard and day runs and the synthetic set with the
harness's own replay and scoring (run.replay / run.score_run / synthetic.score_set: the same numbers
run.py writes), then adds what the report needs:
- the musts with paused-speech words removed from the truth (words whose estimated time falls in a
  truth no-recording gap), M9.1 and ovl_local;
- overlap WER per group language and per bed (music-bed groups apart), per pair kind;
- gate recall / precision against the truth overlap (cross-app vs same-app) and on the synthetic set;
- the same extras for today's stored transcripts (the baseline replay reproduces them).
Numbers only: no transcript is printed or written.

    uv run python eval/overlapping-speech/P2-gated-separation/evaluate.py [--runs std,day] [--no-syn] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402
import errors  # noqa: E402
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("harness_run", HERE.parent / "harness" / "run.py")
hr = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(hr)
import score  # noqa: E402
from diagnose import beds_of, talker_levels  # noqa: E402


class NoPaused:
    """The run's truth with the words said while Akashic was not recording removed."""

    def __init__(self, tr) -> None:
        self._tr = tr
        self._u = None

    def __getattr__(self, k):
        return getattr(self._tr, k)

    def utterances(self):
        if self._u is None:
            gaps = score.merge(score.truth_gaps(self._tr))
            out = []
            for u in self._tr.utterances():
                w = score.fold(u["text"]).split()
                ts = errors.word_times(u, len(w))
                keep = [x for x, t in zip(w, ts, strict=True) if not errors.inside(t, gaps)]
                if len(keep) == len(w):
                    out.append(u)
                    continue
                if not keep:
                    continue
                spans = subtract(u["spans"] or [(u["t0"], u["t1"])], gaps) or u["spans"]
                v = dict(u, text=" ".join(keep), spans=spans, t0=min(a for a, _ in spans), t1=max(b for _, b in spans))
                out.append(v)
            self._u = out
        return self._u


def subtract(spans, gaps):
    out = []
    for a, b in spans:
        cur = [(a, b)]
        for g0, g1 in gaps:
            nxt = []
            for x, y in cur:
                if g1 <= x or g0 >= y:
                    nxt.append((x, y))
                    continue
                if x < g0:
                    nxt.append((x, g0))
                if g1 < y:
                    nxt.append((g1, y))
            cur = nxt
        out += cur
    return [(a, b) for a, b in out if b > a]


def seg_rows(conn):
    return conn.execute("SELECT id, stream, start_ms, end_ms, speech_ms, transcript, speaker_id, language FROM audio_segments").fetchall()


def extras(run: core.Run, conn) -> dict:
    """Extra scoring of one set of rows (candidate scoring db, or the store itself for today)."""
    tr = run.truth
    utts = tr.utterances()
    for u in utts:
        u["proc"] = run.utterance_proc(u)
    rows = seg_rows(conn)
    levels = talker_levels(run, utts)
    recs = errors.analyse(utts, rows, levels, beds_of(tr))
    ov = [r for r in recs if r["overlap"]]
    T = errors.totals
    by_lang = {k: T(ov, lambda r, k=k: r["lang"] == k) for k in sorted({r["lang"] for r in ov})}
    by_bed = {k: T(ov, lambda r, k=k: "+".join(r["bed"] or ["none"]) == k) for k in sorted({"+".join(r["bed"] or ["none"]) for r in ov})}
    music = {"music_bed": T(ov, lambda r: "music" in r["bed"]), "no_music_bed": T(ov, lambda r: "music" not in r["bed"])}
    # paused words removed
    np_tr = NoPaused(tr)
    m = score.m8_m9(np_tr, conn)
    np_utts = np_tr.utterances()
    np_recs = errors.analyse(np_utts, rows, talker_levels(run, np_utts), beds_of(tr))
    np_ov = [r for r in np_recs if r["overlap"]]
    ref = sum(r["ref"] for r in np_ov)
    return {"overlap_by_lang": by_lang, "overlap_by_bed": by_bed, "overlap_music_split": music,
            "no_paused": {"M9.1": m["M9.1"], "M8.2": m["M8.2"], "overlap_local_wer": sum(r["local_errors"] for r in np_ov) / ref if ref else None,
                          "overlap_by_lang": {k: T(np_ov, lambda r, k=k: r["lang"] == k) for k in sorted({r["lang"] for r in np_ov})},
                          "overlap_music_split": {"music_bed": T(np_ov, lambda r: "music" in r["bed"]), "no_music_bed": T(np_ov, lambda r: "music" not in r["bed"])}}}


def truth_overlap(run: core.Run):
    """Truth overlap on the system stream, split by route: cross-app (two processes) and same-app."""
    utts = [u for u in run.truth.utterances() if u["stream"] == "system" and u["source"] != "elsewhere"]
    for u in utts:
        u["proc"] = run.utterance_proc(u)
    utts.sort(key=lambda u: u["t0"])
    out = defaultdict(list)
    for i, a in enumerate(utts):
        for b in utts[i + 1:]:
            if b["t0"] >= a["t1"]:
                break
            for x0, x1 in a["spans"]:
                for y0, y1 in b["spans"]:
                    lo, hi = max(x0, y0), min(x1, y1)
                    if hi > lo:
                        out["same_app" if a["proc"] == b["proc"] else "cross_app"].append((lo, hi))
    return {k: score.merge(v) for k, v in out.items()}


def gate_report(run: core.Run, stats: dict) -> dict:
    segs = score.merge(stats["seg_spans"])
    gated = score.merge(stats["gated_spans"])
    used = score.merge(stats["sep_spans"])
    out = {}
    tov = truth_overlap(run)
    allv = score.merge([s for v in tov.values() for s in v])
    for k, v in {**tov, "all": allv}.items():
        seen = intersect(v, segs)
        tot = sum(b - a for a, b in seen)
        out[k] = {"truth_overlap_s_in_segments": tot / 1000, "recall": score.overlap_ms(seen, gated) / tot if tot else None,
                  "recall_separated": score.overlap_ms(seen, used) / tot if tot else None}
    g = sum(b - a for a, b in gated)
    out["precision"] = score.overlap_ms(gated, allv) / g if g else None
    out["gated_s"] = g / 1000
    return out


def intersect(a, b):
    out, j = [], 0
    for x0, x1 in a:
        for y0, y1 in b:
            lo, hi = max(x0, y0), min(x1, y1)
            if hi > lo:
                out.append((lo, hi))
    return score.merge(out)


def stats_summary(st: dict) -> dict:
    calls = st["sep_calls"]
    rtfs = sorted(dt / a for a, dt in calls if a > 0)
    return {k: v for k, v in st.items() if not isinstance(v, list)} | {
        "gate_rate_segments": st["gated_segments"] / max(1, st["system_segments"]),
        "gate_rate_audio": st["gated_audio_s"] / max(1e-9, st["system_audio_s"]),
        "sep_share_of_system_audio": st["sep_audio_s"] / max(1e-9, st["system_audio_s"]),
        "sep_rtf_overall": st["sep_wall_s"] / max(1e-9, st["sep_audio_s"]),
        "sep_rtf_median_call": rtfs[len(rtfs) // 2] if rtfs else None, "sep_n_calls": len(calls)}


def syn_gate(sset, st: dict) -> dict:
    gated = score.merge(st["gated_spans"])
    by = defaultdict(lambda: [0, 0])
    for m in sset.mixes:
        if len(m.talkers) < 2:
            continue
        ov = intersect(score.merge(m.talkers[0]["spans"]), score.merge(m.talkers[1]["spans"]))
        t = sum(b - a for a, b in ov)
        for key in (f"ov{m.overlap}", f"procs_{m.procs}", f"bed_{m.bed}", "all"):
            by[key][0] += score.overlap_ms(ov, gated)
            by[key][1] += t
    return {k: {"recall": a / b if b else None, "truth_s": b / 1000} for k, (a, b) in sorted(by.items())}


def _num(v):
    """numpy scalars from the stats (json cannot write them)."""
    return v.item() if hasattr(v, "item") else str(v)


def guarded(name, fn, *a):
    """One extra's failure is reported as a string instead of losing the replay."""
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        return f"failed: {type(e).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="std,day")
    ap.add_argument("--no-syn", action="store_true")
    ap.add_argument("--out", type=Path, default=HERE)
    args = ap.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    cand = core.load_candidate(HERE / "candidate.py")
    models = core.load_models()
    path = out / "detail.json"
    detail = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"candidate": "P2-gated-separation/candidate.py", "runs": {}, "uses_sim": False}
    detail.setdefault("p2", {})
    wanted = {n.strip() for n in args.runs.split(",") if n.strip()}
    for run_id in core.RUNS:
        name = core.SHORT[run_id]
        if name not in wanted:
            continue
        run = core.Run(run_id)
        cand.reset_stats()
        ts, ctx, wall = hr.replay(cand, run, models)
        res = hr.score_run(run, ts)
        res.update({"gpu_s": ctx.gpu_s, "wall_s": wall, "cpu_speakers_s": ctx.cpu_speakers_s, "cpu_capture_s": ctx.cpu_capture_s,
                    "audio_s_transcribed": ctx.audio_s_decoded, "recorded_h": (run.truth.t1 - run.truth.t0) / 3.6e6})
        conn = core.scoring_db(ts)
        res["p2_extras"] = guarded("extras", extras, run, conn)
        res["p2_stats"] = stats_summary(cand.STATS)
        res["p2_gate"] = guarded("gate", gate_report, run, cand.STATS)
        detail["runs"][name] = res
        detail["uses_sim"] |= ctx.used_sim
        detail["p2"].setdefault("today_extras", {})[name] = guarded("today", extras, run, run.conn)
        detail["p2"].setdefault("today_gate_truth", {})[name] = {k: sum(b - a for a, b in v) / 1000 for k, v in truth_overlap(run).items()}
        path.write_text(json.dumps(detail, indent=1, default=_num), encoding="utf-8")
        print(f"{name}: ovl {res['M9.1']['overlap']['wer']:.3f} clean {res['M9.1']['no_overlap']['wer']:.3f} der {res['M8.2'].get('der')} "
              f"gate {res['p2_stats']['gate_rate_audio']:.3f} recall {res['p2_gate']['all']['recall'] if isinstance(res['p2_gate'], dict) else res['p2_gate']} rtf {res['p2_stats']['sep_rtf_overall']:.3f} "
              f"gpu {ctx.gpu_s:.0f} s wall {wall:.0f} s", flush=True)
    if not args.no_syn:
        import synthetic

        sset = synthetic.Set()
        cand.reset_stats()
        ts, ctx, wall = hr.replay(cand, sset, models)
        detail["synthetic"] = synthetic.score_set(sset, ts)
        detail["synthetic"]["gpu_s"] = ctx.gpu_s
        detail["synthetic"]["audio_s"] = sum(len(x) for x in sset.mixed.values()) / core.SR
        detail["synthetic"]["p2_stats"] = stats_summary(cand.STATS)
        detail["synthetic"]["p2_gate"] = syn_gate(sset, cand.STATS)
        detail["uses_sim"] |= ctx.used_sim
        sm = detail["synthetic"]["summary"]
        print(f"synthetic: wer {sm['wer']:.3f} cpwer {sm['cpwer']:.3f} der {sm['der']:.3f} quiet50 {sm['talker_any_wer_quiet_ov50']:.3f} "
              f"solo {sm['wer_solo']:.3f}", flush=True)
    path.write_text(json.dumps(detail, indent=1, default=_num), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
