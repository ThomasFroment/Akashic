"""Diagnosis of today's overlap failures on the stored transcripts of the standard and day runs.

    uv run python eval/overlapping-speech/harness/diagnose.py

Rebuilds score.m8_m9's M9.1 groups, aligns every reference word, and attributes each error to an
overlap kind, a stream, an error type and a fix route. Writes ../diagnosis.json; prints numbers only.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import errors  # noqa: E402
import score  # noqa: E402



def pair_route(kind: str) -> str:
    a, b = kind.split("+")
    if "elsewhere" in (a, b):
        return "not_recorded"
    return "per_process" if a != b else "speech_separation"


def talker_levels(run: core.Run, utts: list[dict]) -> dict[int, float]:
    """RMS of each utterance's own source over its voiced spans, at the calibrated gain."""
    tracks = run.tracks()
    out = {}
    for u in utts:
        labels = {"call": ["call"], "own": ["own"], "video": [k for k in tracks if k.startswith("video:")]}.get(u["source"], [])
        if not labels:
            out[id(u)] = 0.0
            continue
        sq = n = 0.0
        for a, b in u["spans"]:
            x = np.zeros((b - a) * core.SR // 1000, np.float32)
            for lab in labels:
                lag = run.lag(lab)
                tracks[lab].render(a - lag, b - lag, x, run.gain(lab))
            sq += float(np.sum(x.astype(np.float64) ** 2))
            n += len(x)
        out[id(u)] = (sq / n) ** 0.5 if n else 0.0
    return out


def beds_of(tr) -> dict[str, list]:
    aud = score.audible_spans(tr)
    names = {"qualitygame.exe": "game", "spotify.exe": "music", "outerwilds.exe": "outer_wilds"}
    out = defaultdict(list)
    for exe, spans in aud.items():
        if exe.lower() in names:
            out[names[exe.lower()]] += spans
    return {k: score.merge(v) for k, v in out.items()}


def route(rec: dict) -> str:
    routes = {pair_route(k) for k in rec["pairs"]}
    return "none" if not routes else (routes.pop() if len(routes) == 1 else "mixed:" + "+".join(sorted(routes)))


def diagnose(run_id: str) -> dict:
    run = core.Run(run_id)
    tr = run.truth
    utts = tr.utterances()
    for u in utts:
        u["proc"] = run.utterance_proc(u)
    canary = score.merge([(a - 1000, a + len(x) * 1000 // core.SR + 1000) for k, t in run.tracks().items() if k.startswith("canary") for a, x, _ in t.items])
    gaps = score.merge(score.truth_gaps(tr))
    segs = run.conn.execute("SELECT id, stream, start_ms, end_ms, speech_ms, transcript, speaker_id, language FROM audio_segments").fetchall()
    recs = errors.analyse(utts, segs, talker_levels(run, utts), beds_of(tr), [("paused", gaps), ("excluded_app_audible", canary)])
    ov = [r for r in recs if r["overlap"]]
    T = errors.totals
    out = {"check": {"overlap": T(recs, lambda r: r["overlap"]), "no_overlap": T(recs, lambda r: not r["overlap"]), "all": T(recs, lambda r: True)}}
    # 1. overlap groups by kind (set of overlapping pair kinds) and stream
    by_kind = defaultdict(list)
    for r in ov:
        by_kind["|".join(sorted(r["pairs"]))].append(r)
    out["overlap_by_kind"] = {k: T(v, lambda r: True) for k, v in sorted(by_kind.items(), key=lambda kv: -sum(r["dist"] for r in kv[1]))}
    out["overlap_by_stream"] = {st: T(ov, lambda r, st=st: r["stream"] == st) for st in ("mic", "system")}
    out["overlap_by_bed"] = {b: T(ov, lambda r, b=b: (r["bed"] or ["none"]) == b.split("+")) for b in sorted({"+".join(r["bed"] or ["none"]) for r in ov})}
    # 2. the brief's views, on every system group (M9.1 'overlap' or not)
    sysg = [r for r in recs if r["stream"] == "system"]
    has = lambda r, s: s in r["sources"]
    out["views"] = {
        "mic_over_call": T(sysg, lambda r: has(r, "call") and r["mic_over"]),
        "call_without_mic": T(sysg, lambda r: has(r, "call") and not r["mic_over"]),
        "mic_over_call_no_system_overlap": T(sysg, lambda r: has(r, "call") and r["mic_over"] and not r["overlap"]),
        "call_over_video": T(sysg, lambda r: any("discord" in k and k != "discord+discord" for k in r["pairs"])),
        "call_over_game": T(sysg, lambda r: has(r, "call") and "game" in r["bed"]),
        "call_over_game_no_speech_overlap": T(sysg, lambda r: has(r, "call") and "game" in r["bed"] and not r["overlap"]),
        "call_over_music_no_speech_overlap": T(sysg, lambda r: has(r, "call") and "music" in r["bed"] and not r["overlap"]),
        "video_over_music_no_speech_overlap": T(sysg, lambda r: has(r, "video") and "music" in r["bed"] and not r["overlap"]),
        "call_no_bed_no_overlap": T(sysg, lambda r: has(r, "call") and not r["bed"] and not r["overlap"]),
        "video_over_music": T(sysg, lambda r: has(r, "video") and "music" in r["bed"]),
        "video_no_bed_no_overlap": T(sysg, lambda r: has(r, "video") and not r["bed"] and not r["overlap"]),
        "two_videos_two_browsers": T(sysg, lambda r: any(k in ("chrome+msedge", "chrome+zen", "msedge+zen") for k in r["pairs"])),
        "two_system_sources_any": T(sysg, lambda r: any(pair_route(k) == "per_process" for k in r["pairs"])),
        "two_talkers_one_process": T(sysg, lambda r: any(pair_route(k) == "speech_separation" for k in r["pairs"])),
        "outer_wilds_bed": T(sysg, lambda r: "outer_wilds" in r["bed"]),
        "elsewhere": T(sysg, lambda r: has(r, "elsewhere")),
        "mic_stream_all": T(recs, lambda r: r["stream"] == "mic"),
    }
    # 3. error types in overlap groups
    regions = errors.merge_counts(ov, "ops_by_region")
    out["overlap_error_types"] = {**regions, "I": sum(r["I"] for r in ov)}
    out["overlap_words_by_region"] = errors.merge_counts(ov, "words_by_region")
    out["overlap_errors_in_concurrent_words_by_pair"] = errors.merge_counts(ov, "err_by_pair")
    out["overlap_uncovered_by_cause"] = errors.merge_counts(ov, "uncovered_ops")
    out["all_uncovered_by_cause"] = errors.merge_counts(recs, "uncovered_ops")
    nov = [r for r in sysg if not r["overlap"]]
    mc = {k: sum(r["mic_concurrent"][k] for r in nov) for k in ("words", "errors", "other_words", "other_errors")}
    out["system_no_overlap_words_vs_mic_speech"] = {**mc, "err_rate_with_mic": mc["errors"] / max(1, mc["words"]), "err_rate_without_mic": mc["other_errors"] / max(1, mc["other_words"])}
    pr = defaultdict(int)
    for k, v in errors.merge_counts(ov, "err_by_pair").items():
        pr[pair_route(k)] += v
    out["overlap_errors_in_concurrent_words_by_route"] = dict(pr)
    out["overlap_local_errors"] = {"scorer_errors": sum(r["dist"] for r in ov), "per_talker_best_substring_SD": sum(r["local_errors"] for r in ov),
                                   "scorer_SD": sum(r["S"] + r["D"] for r in ov)}
    # 4. fix routes (group level)
    routes = defaultdict(list)
    for r in ov:
        routes[route(r)].append(r)
    tot = sum(r["dist"] for r in ov) or 1
    out["fix_routes_group_level"] = {k: {**T(v, lambda r: True), "share_of_overlap_errors": sum(r["dist"] for r in v) / tot} for k, v in routes.items()}
    clean = out["check"]["no_overlap"]["wer"]
    # 5. word-level fix routes: every scorer error of an overlap group gets one route
    #    - by_design: words played while Akashic was paused or on another endpoint (never recorded)
    #    - per_process: words concurrent with another process's talker; words lost because an
    #      excluded app was audible (the whole mixed segment is dropped today)
    #    - speech_separation: words concurrent with a talker of the same process
    #    - the rest of the group's errors (non-concurrent words Whisper dropped with the other
    #      talker, insertions, VAD drops) follow the group's concurrent errors pro rata
    attributed = defaultdict(float)
    words = defaultdict(float)
    for r in ov:
        unc = r["uncovered_ops"]
        by_design = sum(v for k, v in unc.items() if k.endswith((":paused", ":other_endpoint")))
        excl = sum(v for k, v in unc.items() if k.endswith(":excluded_app_audible"))
        conc = defaultdict(float)
        for k, v in r["err_by_pair"].items():
            conc[pair_route(k)] += v
        conc["per_process"] += excl
        rest = r["dist"] - by_design - sum(conc.values())
        tot_c = sum(conc.values())
        share = {k: v / tot_c for k, v in conc.items()} if tot_c else {k: 1 / len(r["pairs"]) for k in (pair_route(p) for p in r["pairs"])}
        attributed["by_design_not_recorded"] += by_design
        by_design_words = r["words_by_region"].get("uncovered", 0) if by_design else 0
        for k, v in conc.items():
            attributed[k] += v
        for k, v in share.items():
            attributed[k] += rest * v
            words[k] += (r["ref"] - by_design_words) * v
        words["by_design_not_recorded"] += by_design_words
    tot = sum(r["dist"] for r in ov) or 1
    out["fix_routes_word_level"] = {k: {"errors": round(v, 1), "share_of_overlap_errors": v / tot, "ref_words": round(words[k], 1)} for k, v in attributed.items()}
    ref = out["check"]["overlap"]["ref_words"]
    if ref:
        def proj(fixed):
            e = tot - sum(attributed[k] for k in fixed) + sum(words[k] for k in fixed) * clean
            return e / ref
        out["projection"] = {
            "fix_per_process_to_no_overlap_wer": proj(["per_process"]),
            "fix_per_process_and_speech_separation": proj(["per_process", "speech_separation"]),
            "floor_by_design_not_recorded": attributed["by_design_not_recorded"] / ref,
            "no_overlap_wer": clean,
        }
    # stored M9.1 (for the check)
    s = json.loads((run.dir / "scores.json").read_text(encoding="utf-8"))
    out["stored_M9.1"] = find(s, "M9.1")
    out["calibration"] = {k: run.calibration()[k] for k in ("gain", "lag", "lag_mad", "n")}
    ex = defaultdict(list)
    for x in run.calibration()["segments"]:
        if x["explained"] is not None:
            ex[x["stream"]].append(x["explained"])
    out["calibration"]["explained_band_energy_p10_p50_p90"] = {k: [float(np.percentile(v, q)) for q in (10, 50, 90)] for k, v in ex.items()}
    return out


def find(d, k):
    if isinstance(d, dict):
        for kk, v in d.items():
            if kk == k:
                return v
            r = find(v, k)
            if r is not None:
                return r
    return None


def main() -> int:
    res = {core.SHORT[r]: diagnose(r) for r in core.RUNS}
    (core.STUDY / "diagnosis.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    for name, d in res.items():
        print(f"== {name}")
        c = d["check"]
        print("  check  overlap wer %.3f (stored %.3f, ref %d/%d)  no_overlap %.3f (stored %.3f)" % (
            c["overlap"]["wer"], d["stored_M9.1"]["overlap"]["wer"], c["overlap"]["ref_words"], d["stored_M9.1"]["overlap"]["ref_words"],
            c["no_overlap"]["wer"], d["stored_M9.1"]["no_overlap"]["wer"]))
        for sec in ("overlap_by_kind", "views", "fix_routes_group_level"):
            print("  " + sec)
            for k, v in d[sec].items():
                extra = f" share={v['share_of_overlap_errors']:.2f}" if "share_of_overlap_errors" in v else ""
                w = "-" if v["wer"] is None else f"{v['wer']:.3f}"
                print(f"    {k:40s} groups={v['groups']:4d} ref={v['ref_words']:6d} err={v['errors']:5d} wer={w} S={v['S']} D={v['D']} I={v['I']} local={v['local_errors']}{extra}")
        for sec in ("overlap_error_types", "overlap_words_by_region", "overlap_errors_in_concurrent_words_by_pair", "overlap_errors_in_concurrent_words_by_route",
                    "overlap_uncovered_by_cause", "all_uncovered_by_cause", "system_no_overlap_words_vs_mic_speech", "overlap_local_errors", "fix_routes_word_level", "projection"):
            print(f"  {sec}: {json.dumps(d.get(sec))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
