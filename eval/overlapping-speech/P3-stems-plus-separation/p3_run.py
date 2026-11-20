"""P3 accuracy driver (numbers only). One replay per run with the harness's own replay and scoring
(harness/run.py functions, unchanged), P3_SHADOW=1 so the same replay also yields P1's rows on the
same capture front end. Per run it writes into detail.json:

- runs[name]           P3 as the harness scores it (gpu_s = the decodes P3 used)
- runs_nopause[name]   P3 with the truth utterances spoken while Akashic was paused removed (P1's rule:
                       >= 50 % of the utterance inside a driver pause/resume span); ovl_local is in it
- shadow_p1[...]       P1 (strict exclusion, same front end) scored both ways
- gate[name]           gate recall / precision against the truth overlap present IN each output stream
                       (both talkers' processes among the stream's full-level sources), split same-app /
                       cross-app and by stream kind; plus P2's definition (all system truth overlap)
- mixed[name]          the mixed-language groups (M9.1 'mixed') broken down for today, P1, P3
- p3[name]             candidate counters, decode list (start ms, audio s) and separation jobs
- game_front[name]     foreground spans of game apps (for the CPU-only-when-no-game backlog estimate)
Synthetic: harness score_set for P3 and for the shadow P1, plus the gate on the synthetic mixes.

    uv run python eval/overlapping-speech/P3-stems-plus-separation/p3_run.py <out> [--runs std,day] [--no-syn]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

os.environ["P3_SHADOW"] = "1"
HERE = Path(__file__).resolve().parent
HARNESS = HERE.parent / "harness"
sys.path.insert(0, str(HARNESS))
import core  # noqa: E402
import errors  # noqa: E402
import score  # noqa: E402

hr = core.load_candidate(HARNESS / "run.py")  # harness/run.py (eval/quality has its own run.py)


def _num(v):
    return v.item() if hasattr(v, "item") else str(v)


# --- scoring variants ----------------------------------------------------------------------------


def nopause(run: core.Run) -> tuple[list[dict], int]:
    """P1's rule (p1_run.py): drop truth utterances >= 50 % inside a driver pause span."""
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


def excl_spans(run) -> list:
    return score.merge([(a + run.lag(k), a + run.lag(k) + len(x) * 1000 // core.SR) for k, t in run.tracks().items()
                        if t.exe in core.EXCLUDED for a, x, _ in t.items])


def score_both(run, ts, keep, removed) -> tuple[dict, dict]:
    res = hr.score_run(run, ts)
    run.truth.utterances = lambda keep=keep: [dict(u) for u in keep]  # as p1_run.py does (instance attribute)
    try:
        r2 = hr.score_run(run, ts)
    finally:
        del run.truth.utterances
    r2["paused_words_removed"] = removed
    ex = excl_spans(run)
    res["excl_overlap_s_by_kind"] = {}
    for t in ts:
        if t.stream == "system" and t.text:
            ov = score.overlap_ms(ex, [(t.start_ms, t.end_ms)]) / 1000
            if ov:
                k = getattr(t, "p1_kind", "?")
                res["excl_overlap_s_by_kind"][k] = res["excl_overlap_s_by_kind"].get(k, 0.0) + ov
    return res, r2


# --- gate report -----------------------------------------------------------------------------------


def truth_pairs(run) -> list[tuple[int, int, str, str]]:
    """Overlapping voiced spans of two system truth utterances: (lo, hi, proc a, proc b)."""
    utts = [u for u in run.truth.utterances() if u["stream"] == "system" and u["source"] != "elsewhere"]
    for u in utts:
        u["proc"] = run.utterance_proc(u)
    utts.sort(key=lambda u: u["t0"])
    out = []
    for i, a in enumerate(utts):
        for b in utts[i + 1:]:
            if b["t0"] >= a["t1"]:
                break
            for x0, x1 in a["spans"]:
                for y0, y1 in b["spans"]:
                    lo, hi = max(x0, y0), min(x1, y1)
                    if hi > lo:
                        out.append((lo, hi, a["proc"], b["proc"]))
    return out


def syn_pairs(sset) -> list[tuple[int, int, str, str]]:
    out = []
    for m in sset.mixes:
        if len(m.talkers) < 2:
            continue
        ea, eb = (t.get("exe", "?").removesuffix(".exe") for t in m.talkers[:2])
        for lo, hi in intersect(score.merge(m.talkers[0]["spans"]), score.merge(m.talkers[1]["spans"])):
            out.append((lo, hi, ea, eb))
    return out


def intersect(a, b):
    out = []
    for x0, x1 in a:
        for y0, y1 in b:
            lo, hi = max(x0, y0), min(x1, y1)
            if hi > lo:
                out.append((lo, hi))
    return score.merge(out)


def gate_report(pairs, records) -> dict:
    """records: (key, kind, a, b, sources, gated spans) per gated-or-not utterance of an output stream."""
    acc = defaultdict(lambda: [0, 0])  # name -> [hit ms, truth ms]
    g_ms = g_in = g_any = 0
    allv = score.merge([(lo, hi) for lo, hi, _, _ in pairs])
    for key, kind, a, b, srcs, gated in records:
        src = {s.removesuffix(".exe") for s in srcs}
        gm = score.merge([tuple(x) for x in gated])
        mine = {"same_app": [], "cross_app": []}
        for lo, hi, pa, pb in pairs:
            if pa in src and pb in src and hi > a and lo < b:
                mine["same_app" if pa == pb else "cross_app"].append((max(lo, a), min(hi, b)))
        both = score.merge(mine["same_app"] + mine["cross_app"])
        for name, v in (("same_app", score.merge(mine["same_app"])), ("cross_app", score.merge(mine["cross_app"])), ("all", both),
                        (f"kind_{kind}", both)):
            tot = sum(y - x for x, y in v)
            acc[name][0] += score.overlap_ms(v, gm)
            acc[name][1] += tot
        gs = sum(y - x for x, y in gm)
        g_ms += gs
        g_in += score.overlap_ms(gm, both)
        g_any += score.overlap_ms(gm, allv)
    out = {k: {"recall": h / t if t else None, "truth_s": t / 1000} for k, (h, t) in sorted(acc.items())}
    out["precision_in_stream"] = g_in / g_ms if g_ms else None
    out["precision_any_overlap"] = g_any / g_ms if g_ms else None  # P2's definition
    out["gated_s"] = g_ms / 1000
    # P2's recall definition: all truth overlap inside the streams' spans vs the union of gated spans
    spans = score.merge([(a, b) for _, _, a, b, _, _ in records])
    seen = intersect(allv, spans)
    gated_all = score.merge([tuple(x) for r in records for x in r[5]])
    tot = sum(y - x for x, y in seen)
    out["p2_def_recall_all_overlap"] = score.overlap_ms(seen, gated_all) / tot if tot else None
    out["p2_def_truth_s"] = tot / 1000
    return out


# --- mixed-language groups ------------------------------------------------------------------------


def rows_of(ts) -> tuple[list[tuple], dict]:
    rows, meta = [], {}
    for i, t in enumerate(sorted(ts, key=lambda t: (t.start_ms, t.stream))):
        rows.append((i, t.stream, t.start_ms, t.end_ms, t.speech_ms, t.text or None, t.speaker_id, t.language))
        meta[i] = {"kind": getattr(t, "p1_kind", "?"), "sources": tuple(t.sources), "sep": bool(getattr(t, "p3_sep", False)),
                   "prompted": bool(getattr(t, "p3_prompted", False)), "continued": bool(getattr(t, "p3_continued", False)), "lsplit": bool(getattr(t, "p3_lsplit", False))}
    return rows, meta


def rows_today(run) -> tuple[list[tuple], dict]:
    rows, meta = [], {}
    for r in run.conn.execute("SELECT id, stream, start_ms, end_ms, speech_ms, transcript, speaker_id, language, sources FROM audio_segments"):
        rows.append(tuple(r[:8]))
        meta[r[0]] = {"kind": "mic" if r[1] == "mic" else "today_system", "sources": tuple(json.loads(r[8] or "[]")), "sep": False}
    return rows, meta


def length_bucket(ms: int) -> str:
    s = ms / 1000
    return "<3s" if s < 3 else "3-6s" if s < 6 else "6-10s" if s < 10 else "10-20s" if s < 20 else ">=20s"


def mixed_records(run, rows, meta) -> list[dict]:
    """One record per M9.1 group whose language is 'mixed' (scorer grouping), numbers only."""
    utts = run.truth.utterances()
    for u in utts:
        u["proc"] = run.utterance_proc(u)
    out = []
    for stream in ("mic", "system"):
        for g in errors.groups(utts, rows, stream):
            us = [x[3] for x in g if x[0] == "u"]
            if not us:
                continue
            langs = {u["lang"] for u in us if u.get("kind") != "tts"}
            lang = "tts" if not langs else (langs.pop() if len(langs) == 1 else "multi")
            if lang != "mixed":
                continue
            ss = sorted((x for x in g if x[0] == "s"), key=lambda x: x[1])
            ref = score.fold(" ".join(u["text"] for u in us)).split()
            hyp_parts = [score.fold(x[3]).split() for x in ss]
            hyp = [w for p in hyp_parts for w in p]
            ops, ins, dist = errors.align(ref, hyp)
            byrow = {x[4]: x for x in ss}
            row_lang = [next((r[7] for r in rows if r[0] == x[4]), None) for x in ss]
            kinds = [meta[x[4]]["kind"] for x in ss]
            procs = {u["proc"] for u in us}
            foreign_words = sum(len(p) for x, p in zip(ss, hyp_parts, strict=True)
                                if meta[x[4]]["kind"] not in ("mic", "residual", "fallback", "today_system")
                                and not ({s.removesuffix(".exe") for s in meta[x[4]]["sources"]} & procs))
            u0, u1 = min(u["t0"] for u in us), max(u["t1"] for u in us)
            cut_inside = sum(1 for x in ss for e in (x[1], x[2]) if u0 + 300 < e < u1 - 300)
            out.append({
                "stream": stream, "overlap": any(us[i]["t1"] > us[j]["t0"] for i in range(len(us)) for j in range(i + 1, len(us))),
                "ref": len(ref), "hyp": len(hyp), "dist": dist, "S": ops.count("S"), "D": ops.count("D"), "I": ins,
                "n_utts": len(us), "n_segs": len(ss), "n_empty_segs": sum(1 for p in hyp_parts if not p),
                "kinds": "+".join(sorted(set(kinds))) or "none", "langs": "+".join(sorted({str(x) for x, p in zip(row_lang, hyp_parts, strict=True) if p})) or "none",
                "sep": any(meta[x[4]]["sep"] for x in ss), "foreign_row_words": foreign_words, "cuts_inside_utt": cut_inside,
                "utt_len": length_bucket(u1 - u0), "proc": "+".join(sorted(procs)),
                "seg_len": length_bucket(max((x[2] - x[1] for x in ss), default=0)),
                "seg_ms_total": sum(x[2] - x[1] for x in ss), "utt_ms": u1 - u0, "t0": u0,
                "hyp_by_kind": dict(Counter(k for x, p, k in zip(ss, hyp_parts, kinds, strict=True) for _ in p)),
                "prompted": "+".join(sorted({str(meta[x[4]].get("prompted", "na")) for x in ss})) or "none",
                "continued": any(meta[x[4]].get("continued", False) for x in ss),
                "empty_rows": sum(1 for p in hyp_parts if not p),
                "lsplit": any(meta[x[4]].get("lsplit", False) for x in ss),
            })
            del byrow
    return out


FEATURES = ("stream", "overlap", "kinds", "langs", "n_segs", "sep", "utt_len", "seg_len", "proc", "cuts_inside_utt", "prompted", "continued", "empty_rows", "lsplit")


def breakdown(recs: list[dict]) -> dict:
    def agg(sel):
        ref = sum(r["ref"] for r in sel)
        d = sum(r["dist"] for r in sel)
        return {"groups": len(sel), "ref": ref, "errors": d, "wer": d / ref if ref else None, "S": sum(r["S"] for r in sel), "D": sum(r["D"] for r in sel),
                "I": sum(r["I"] for r in sel), "foreign_row_words": sum(r["foreign_row_words"] for r in sel)}
    out = {"all": agg(recs)}
    for f in FEATURES:
        vals = sorted({str(min(r[f], 3) if f in ("n_segs", "cuts_inside_utt") else r[f]) for r in recs})
        out[f] = {v: agg([r for r in recs if str(min(r[f], 3) if f in ("n_segs", "cuts_inside_utt") else r[f]) == v]) for v in vals}
    return out


def paired(a: list[dict], b: list[dict]) -> dict:
    """Groups matched by their first utterance start (same truth utterance chain): error delta b - a,
    summed by b's features. Unmatched groups (the chains differ) counted apart."""
    ia = {r["t0"]: r for r in a}
    out = {"matched": 0, "unmatched_b": 0, "delta_errors": 0, "delta_ref": 0, "by": {}}
    for r in b:
        o = ia.get(r["t0"])
        if o is None:
            out["unmatched_b"] += 1
            continue
        out["matched"] += 1
        de = r["dist"] - o["dist"]
        out["delta_errors"] += de
        out["delta_ref"] += r["ref"] - o["ref"]
        for f in ("stream", "kinds", "langs", "n_segs", "sep", "utt_len", "seg_len", "cuts_inside_utt", "prompted", "continued", "lsplit"):
            v = str(min(r[f], 3) if f in ("n_segs", "cuts_inside_utt") else r[f])
            ov = str(min(o[f], 3) if f in ("n_segs", "cuts_inside_utt") else o[f])
            k = out["by"].setdefault(f, {}).setdefault(f"{ov}->{v}", {"groups": 0, "delta_errors": 0, "dS": 0, "dD": 0, "dI": 0})
            k["groups"] += 1
            k["delta_errors"] += de
            k["dS"] += r["S"] - o["S"]
            k["dD"] += r["D"] - o["D"]
            k["dI"] += r["I"] - o["I"]
    # the groups that got worse, worst first (numbers only)
    worse = sorted(((r["dist"] - ia[r["t0"]]["dist"], r) for r in b if r["t0"] in ia), key=lambda x: -x[0])
    out["worst"] = [{"delta": d, "ref": r["ref"], "kinds": r["kinds"], "langs": r["langs"], "was_langs": ia[r["t0"]]["langs"], "n_segs": r["n_segs"],
                     "was_n_segs": ia[r["t0"]]["n_segs"], "dS": r["S"] - ia[r["t0"]]["S"], "dD": r["D"] - ia[r["t0"]]["D"], "dI": r["I"] - ia[r["t0"]]["I"],
                     "foreign_row_words": r["foreign_row_words"], "utt_len": r["utt_len"], "stream": r["stream"]} for d, r in worse[:12]]
    out["n_worse"] = sum(1 for d, _ in worse if d > 0)
    out["n_better"] = sum(1 for d, _ in worse if d < 0)
    return out


# --- per run ---------------------------------------------------------------------------------------


def game_front(run) -> list[tuple[int, int]]:
    import scenario as sc

    return score.merge([(r["t0"], r["t1"]) for r in run.truth.fg_runs() if r["app"] and sc.APPS.get(r["app"], (None, None, None))[2] == "game"])


def p3_counters(ctx) -> dict:
    st = dict(ctx.p3)
    st.pop("records", None)
    st["sep_spans"] = [(a, b) for a, b, _ in st["sep_spans"]]
    return st


def analyse_run(run, ts, ctx, wall) -> dict:
    keep, removed = nopause(run)
    res, r2 = score_both(run, ts, keep, removed)
    p3be = ctx.p3["backends"]["p3"]
    res.update({"gpu_s": p3be["gpu_s"], "gpu_s_ctx_total_with_shadow": ctx.gpu_s, "wall_s": wall, "cpu_speakers_s": ctx.cpu_speakers_s,
                "cpu_capture_s": ctx.cpu_capture_s, "audio_s_transcribed": sum(p3be["decode_audio_s"].values()),
                "recorded_h": (run.truth.t1 - run.truth.t0) / 3.6e6})
    out = {"run": res, "nopause": r2}
    sh = getattr(ctx, "p3_shadow", None)
    if sh is not None:
        s1, s2 = score_both(run, sh, keep, removed)
        p1be = ctx.p3["backends"]["p1"]
        s1.update({"gpu_s": p1be["gpu_s"], "audio_s_transcribed": sum(p1be["decode_audio_s"].values())})
        out["shadow"] = {"run": s1, "nopause": s2}
    out["gate"] = gate_report(truth_pairs(run), ctx.p3["records"])
    out["gate_by_kind_counts"] = {"gated_by_kind": ctx.p3["gated_by_kind"], "sep_used_by_kind": ctx.p3["sep_used_by_kind"]}
    mixed = {}
    rt, mt = rows_today(run)
    mixed["today"] = mixed_records(run, rt, mt)
    r3, m3 = rows_of(ts)
    mixed["p3"] = mixed_records(run, r3, m3)
    if sh is not None:
        r1, m1 = rows_of(sh)
        mixed["p1"] = mixed_records(run, r1, m1)
    out["mixed"] = {"breakdown": {k: breakdown(v) for k, v in mixed.items()},
                    "paired": {f"today->{k}": paired(mixed["today"], v) for k, v in mixed.items() if k != "today"}}
    if "p1" in mixed:
        out["mixed"]["paired"]["p1->p3"] = paired(mixed["p1"], mixed["p3"])
    # all-language view of the same question: per kind of row, no-overlap WER of the groups (lang en/fr too)
    out["p3"] = p3_counters(ctx)
    out["game_front"] = game_front(run) if hasattr(run, "truth") and hasattr(run.truth, "fg_runs") else []
    out["baseline_decodes"] = [(s.start_ms, (s.end_ms - s.start_ms) / 1000) for s in run.segments]
    return out


def analyse_syn(sset, ts, ctx, wall) -> dict:
    import synthetic

    out = synthetic.score_set(sset, ts)
    out["gpu_s"] = ctx.p3["backends"]["p3"]["gpu_s"]
    out["audio_s"] = sum(len(x) for x in sset.mixed.values()) / core.SR
    out["wall_s"] = wall
    out["p3_gate"] = gate_report(syn_pairs(sset), ctx.p3["records"])
    out["p3"] = p3_counters(ctx)
    sh = getattr(ctx, "p3_shadow", None)
    if sh is not None:
        s = synthetic.score_set(sset, sh)
        out["shadow_p1_summary"] = s["summary"]
    return out


def store(detail, name, a) -> None:
    detail["runs"][name] = a["run"]
    detail["runs_nopause"][name] = a["nopause"]
    if "shadow" in a:
        detail["shadow_p1"]["runs"][name] = a["shadow"]["run"]
        detail["shadow_p1"]["runs_nopause"][name] = a["shadow"]["nopause"]
    for k in ("gate", "gate_by_kind_counts", "mixed", "p3", "game_front", "baseline_decodes"):
        detail.setdefault(k, {})[name] = a[k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--runs", default="std,day")
    ap.add_argument("--no-syn", action="store_true")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    cand = core.load_candidate(HERE / "candidate.py")
    models = core.load_models()
    path = a.out / "detail.json"
    detail = {"candidate": "P3-stems-plus-separation/candidate.py", "runs": {}, "runs_nopause": {}, "shadow_p1": {"runs": {}, "runs_nopause": {}}, "uses_sim": False}
    wanted = {n.strip() for n in a.runs.split(",") if n.strip()}
    for run_id in core.RUNS:
        name = core.SHORT[run_id]
        if name not in wanted:
            continue
        run = core.Run(run_id)
        ts, ctx, wall = hr.replay(cand, run, models)
        an = analyse_run(run, ts, ctx, wall)
        store(detail, name, an)
        detail["uses_sim"] |= ctx.used_sim
        path.write_text(json.dumps(detail, indent=1, default=_num), encoding="utf-8")
        r, r2 = an["run"], an["nopause"]
        sh = an.get("shadow", {}).get("run", {})
        print(f"{name}: P3 ovl {r['M9.1']['overlap']['wer']:.3f} (np {r2['M9.1']['overlap']['wer']:.3f} local {r2['overlap_local_wer']:.3f}) clean {r['M9.1']['no_overlap']['wer']:.3f} "
              f"mixed {r['M9.1'].get('mixed', {}).get('wer')} der {r['M8.2'].get('der')} excl {r['excluded_s']:.1f} | shadow P1 ovl {sh.get('M9.1', {}).get('overlap', {}).get('wer')} "
              f"mixed {sh.get('M9.1', {}).get('mixed', {}).get('wer')} | gate recall {an['gate']['all']['recall']} prec {an['gate']['precision_in_stream']} | "
              f"gpu {r['gpu_s']:.0f} s wall {wall:.0f} s sep {an['p3']['sep'].get('sep_wall_s', 0):.0f} s", flush=True)
    if not a.no_syn:
        import synthetic

        sset = synthetic.Set()
        ts, ctx, wall = hr.replay(cand, sset, models)
        detail["synthetic"] = analyse_syn(sset, ts, ctx, wall)
        detail["uses_sim"] |= ctx.used_sim
        sm = detail["synthetic"]["summary"]
        print(f"synthetic: wer {sm['wer']:.3f} cpwer {sm['cpwer']:.3f} der {sm['der']:.3f} quiet50 {sm['talker_any_wer_quiet_ov50']:.3f} wall {wall:.0f} s", flush=True)
    path.write_text(json.dumps(detail, indent=1, default=_num), encoding="utf-8")
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
