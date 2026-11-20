"""P3 report files from detail.json (+ cost-same-session.json when present): metrics.txt (harness
line + P2's gate keys + P3 cost keys), side.png (harness draw.side, the layout P1 and P2 used),
fig-mixed-day.png, fig-overlap-pairs.png, and detail["musts"] / detail["cost_model"] / detail["backlog"].
Numbers only.

    uv run python eval/overlapping-speech/P3-stems-plus-separation/finalize.py [<out>]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
sys.path.insert(0, str(STUDY / "harness"))
import core  # noqa: E402
import draw  # noqa: E402

hr = core.load_candidate(STUDY / "harness" / "run.py")
P1C = (170, 120, 230)


def load(p: Path):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def g(d, *keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


# --- cost --------------------------------------------------------------------------------------------


def cost_model(d: dict, cs: dict | None, base: dict) -> dict:
    """gpu = a * decode calls + b * decoded audio s, solved on the two same-session cost windows
    (baseline and P3), applied to whole runs and to each clock hour of the day run."""
    if not cs or "counts" not in cs:
        return {}
    cb, cc = cs["counts"]["baseline"], cs["counts"]["candidate"]
    A = np.array([[cb["calls"], cb["audio_s"]], [cc["calls"], cc["audio_s"]]], float)
    # GPU s per 6-min window; the candidate's language-detection passes (counted apart in its counting
    # pass) are taken out so a and b describe decodes only, then added back per run from ls-census.json
    y = np.array([cs["baseline"]["gpu_s_per_h"] * 0.1, cs["candidate"]["gpu_s_per_h"] * 0.1 - cc.get("ls_gpu_s", 0.0)])
    try:
        a, b = np.linalg.solve(A, y)
    except np.linalg.LinAlgError:
        return {"error": "singular"}
    census = load(HERE / "ls-census.json") or {}
    out = {"model": f"gpu = {a:.3f} s per decode call + {b:.4f} s per audio s (solved on the two same-session cost windows) "
                    "+ the language-split detection GPU measured on the whole run (ls-census.json)", "a": a, "b": b, "census": census}
    for r in ("std", "day"):
        x, bx = g(d, "p3", r), g(base, "runs", r)
        if not x or not bx:
            continue
        be = x["backends"]["p3"]
        ls = g(census, r, "ls_gpu_s") or 0.0
        c = a * be["n_decode"] + b * sum(be["decode_audio_s"].values()) + ls
        out[f"ls_share_{r}"] = ls / c if c else None
        t = a * bx["n_out"] + b * bx["audio_s_transcribed"]
        out[f"gpu_x_{r}"] = c / t
    # per clock hour of the day run
    x = g(d, "p3", "day")
    bd = g(d, "baseline_decodes", "day")
    if x and bd:
        t0 = min(t for t, _ in bd)
        scale = base["runs"]["day"]["audio_s_transcribed"] / max(1e-9, sum(s for _, s in bd))
        hc, hb = {}, {}
        ls = g(census, "day", "ls_gpu_s") or 0.0
        sys_audio = sum(s for _t, s, k in x["decodes"] if k != "mic") or 1e-9
        for t, s, k in x["decodes"]:  # detection GPU spread over the hours by system audio decoded
            h = (t - t0) // 3_600_000
            hc[h] = hc.get(h, 0.0) + a + b * s + (ls * s / sys_audio if k != "mic" else 0.0)
        for t, s in bd:
            h = (t - t0) // 3_600_000
            hb[h] = hb.get(h, 0.0) + a + b * s * scale
        per = {int(h): hc.get(h, 0.0) / hb[h] for h in sorted(hb) if hb[h] >= 30}
        out["day_hour_ratio"] = per
        out["gpu_x_worst_hour"] = max(per.values()) if per else None
    return out


def backlog(d: dict, name: str = "day") -> dict:
    """CPU separation only while no game is in front: FIFO queue of the separation jobs (wall s as
    measured in the replay, all cores), served at 1 s per s outside game-in-front spans."""
    x = g(d, "p3", name)
    gf = g(d, "game_front", name) or []
    if not x:
        return {}
    jobs = sorted((t, w) for t, w, _c, _a in x["sep_jobs"])
    t0 = int(min(t for t, _ in g(d, "baseline_decodes", name))) if g(d, "baseline_decodes", name) else (jobs[0][0] if jobs else 0)
    rec_h = g(d, "runs", name, "recorded_h") or 0
    t1 = t0 + int(rec_h * 3.6e6)
    gm = [(a, b) for a, b in gf]
    def in_game(t):
        return any(a <= t < b for a, b in gm)
    q, mx, i, waits, pend = 0.0, 0.0, 0, [], []
    t = t0
    end = t1 + 12 * 3_600_000
    while t < end and (i < len(jobs) or q > 1e-9):
        while i < len(jobs) and jobs[i][0] <= t:
            q += jobs[i][1]
            pend.append([jobs[i][0], jobs[i][1]])
            i += 1
        if not in_game(t) and q > 0:
            step = min(1.0, q)
            q -= step
            while step > 1e-12 and pend:
                take = min(step, pend[0][1])
                pend[0][1] -= take
                step -= take
                if pend[0][1] <= 1e-12:
                    waits.append((t + 1000 - pend[0][0]) / 1000)
                    pend.pop(0)
        mx = max(mx, q)
        t += 1000
    game_s = sum(min(b, t1) - max(a, t0) for a, b in gm if b > t0 and a < t1) / 1000
    work = sum(w for _, w in jobs)
    return {"jobs": len(jobs), "work_wall_s": work, "work_cpu_core_s": sum(c for _t, _w, c, _a in x["sep_jobs"]),
            "sep_audio_s": sum(a for _t, _w, _c, a in x["sep_jobs"]), "recorded_s": rec_h * 3600, "game_front_s": game_s,
            "free_s": rec_h * 3600 - game_s, "load_vs_free": work / max(1e-9, rec_h * 3600 - game_s), "max_backlog_s": mx,
            "max_wait_s": max(waits) if waits else None, "median_wait_s": float(np.median(waits)) if waits else None,
            "left_at_run_end_s": None}


# --- musts ---------------------------------------------------------------------------------------------


def musts(d: dict, base: dict) -> dict:
    p1 = load(STUDY / "P1-per-process-streams" / "detail.json") or {}
    bnp = (load(STUDY / "P1-per-process-streams" / "baseline-rescore" / "detail.json") or {}).get("runs_nopause", {})
    ways = {
        "harness": {"today": base.get("runs", {}), "P1": p1.get("runs", {}), "P1_strict": p1.get("runs_strict_excl", {}),
                    "P1_shadow": g(d, "shadow_p1", "runs") or {}, "P3_no_langsplit": g(d, "p3_no_langsplit", "runs") or {}, "P3": d.get("runs", {})},
        "no_paused": {"today": bnp, "P1": p1.get("runs_nopause", {}), "P1_strict": p1.get("runs_nopause_strict_excl", {}),
                      "P1_shadow": g(d, "shadow_p1", "runs_nopause") or {}, "P3_no_langsplit": g(d, "p3_no_langsplit", "runs_nopause") or {},
                      "P3": d.get("runs_nopause", {})},
    }
    out = {}
    for way, srcs in ways.items():
        for r in ("std", "day"):
            row = {}
            for who, runs in srcs.items():
                x = runs.get(r)
                if not x:
                    continue
                m = x["M9.1"]
                row[who] = {"overlap": g(m, "overlap", "wer"), "ovl_local": x.get("overlap_local_wer"), "clean": g(m, "no_overlap", "wer"),
                            "en": g(m, "en", "wer"), "fr": g(m, "fr", "wer"), "mixed": g(m, "mixed", "wer"), "der": g(x, "M8.2", "der"),
                            "wrong": g(x, "M8.3", "wrong"), "unknown": g(x, "M8.3", "unknown"), "excl_s": x.get("excluded_s")}
            out[f"{way}/{r}"] = row
    return out


# --- metrics -----------------------------------------------------------------------------------------


def metrics(d: dict, base_cost: dict, cm: dict, bl: dict) -> str:
    line = hr.metrics_line(d, base_cost)
    f = lambda v, n=3: "nan" if v is None else f"{v:.{n}f}"
    ex = []
    for r in ("std", "day"):
        x, gt, npr = g(d, "p3", r), g(d, "gate", r), g(d, "runs_nopause", r)
        if not x:
            continue
        sysa = x["gate_audio_s"] or 1e-9
        ex += [f"gate_{r}={f(x['gated_audio_s'] / sysa)}", f"sep_share_{r}={f(x['sep']['sep_audio_s'] / sysa)}",
               f"recall_{r}={f(g(gt, 'all', 'recall'))}", f"recall_xapp_{r}={f(g(gt, 'cross_app', 'recall'))}", f"recall_same_{r}={f(g(gt, 'same_app', 'recall'))}",
               f"prec_{r}={f(gt.get('precision_in_stream'))}", f"recall_p2def_{r}={f(gt.get('p2_def_recall_all_overlap'))}",
               f"np_ovl_{r}={f(g(npr, 'M9.1', 'overlap', 'wer'))}", f"np_ovl_local_{r}={f(g(npr, 'overlap_local_wer'))}", f"np_clean_{r}={f(g(npr, 'M9.1', 'no_overlap', 'wer'))}"]
        rh = g(d, "runs", r, "recorded_h") or 1e-9
        ex += [f"sep_rtf_cpu_{r}={f(x['sep']['sep_wall_s'] / max(1e-9, x['sep']['sep_audio_s']))}", f"sep_cpu_s_h_{r}={f(x['sep']['sep_cpu_s'] / rh, 1)}",
               f"sep_wall_s_h_{r}={f(x['sep']['sep_wall_s'] / rh, 1)}", f"gate_cpu_s_h_{r}={f(x['gate_cpu_s'] / rh, 1)}"]
    for k in ("gpu_x_std", "gpu_x_day", "gpu_x_worst_hour"):
        if cm.get(k) is not None:
            ex.append(f"{k.replace('gpu_x_', 'gpu_x_avg_') if k != 'gpu_x_worst_hour' else k}={f(cm[k], 3)}")
    if bl:
        ex += [f"sep_load_vs_free_day={f(bl['load_vs_free'])}", f"sep_backlog_max_s_day={f(bl['max_backlog_s'], 0)}", f"sep_wait_max_s_day={f(bl['max_wait_s'], 0)}"]
    return " ".join([line, *ex])


# --- figures -----------------------------------------------------------------------------------------


def fig_mixed(d: dict, path: Path, name: str = "day") -> None:
    bd = g(d, "mixed", name, "breakdown") or {}
    W, H = 1500, 980
    img = Image.new("RGB", (W, H), draw.BG)
    dr = ImageDraw.Draw(img)
    dr.text((20, 12), f"M9.1 'mixed' groups, {name} run: WER by feature, today grey / P1 purple / P3 blue (numbers only)", fill=draw.FG, font=draw.FT)
    panels = [("stream", "by stream"), ("kinds", "by row kind in the group"), ("langs", "by language Whisper chose (rows with text)"),
              ("n_segs", "by rows in the group (3 = 3+)"), ("cuts_inside_utt", "by row edges inside the utterance (3 = 3+)"), ("utt_len", "by utterance chain length")]
    for i, (f, title) in enumerate(panels):
        keys = sorted(set().union(*[set((bd.get(w) or {}).get(f, {})) for w in ("today", "p1", "p3")]))
        vals = lambda w: [g(bd, w, f, k, "wer") for k in keys]
        x0 = 80 + (i % 2) * 730
        y0 = 90 + (i // 2) * 300
        labs = [f"{k} ({g(bd, 'p1', f, k, 'ref') or g(bd, 'today', f, k, 'ref') or 0}w)" for k in keys]
        draw.bars(dr, (x0, y0, x0 + 640, y0 + 200), title, labs, [(draw.BASE, vals("today")), (P1C, vals("p1")), (draw.CAND, vals("p3"))], 1.0, [0, 0.25, 0.5, 0.75, 1.0])
    a = [g(bd, w, "all") or {} for w in ("today", "p1", "p3")]
    dr.text((20, H - 40), "all mixed: " + "   ".join(f"{w}: wer {x.get('wer') or 0:.3f} S {x.get('S')} D {x.get('D')} I {x.get('I')} ref {x.get('ref')}"
                                               for w, x in zip(("today", "P1", "P3"), a, strict=True)), fill=draw.FG, font=draw.F)
    img.save(path)


def fig_pairs(d: dict, path: Path) -> None:
    base = load(STUDY / "baseline" / "detail.json") or {}
    p1 = load(STUDY / "P1-per-process-streams" / "detail.json") or {}
    W, H = 1500, 800
    img = Image.new("RGB", (W, H), draw.BG)
    dr = ImageDraw.Draw(img)
    dr.text((20, 12), "Overlap WER (M9.1 groups) per pair of overlapping sources: today grey / P1 purple / P3 blue; red = 0.25", fill=draw.FG, font=draw.FT)
    for j, r in enumerate(("std", "day")):
        srcs = [g(base, "runs", r, "overlap_by_pair") or {}, g(p1, "runs", r, "overlap_by_pair") or {}, g(d, "runs", r, "overlap_by_pair") or {}]
        keys = sorted(set().union(*map(set, srcs)))
        labs = [f"{k} ({(srcs[2].get(k) or srcs[0].get(k) or {}).get('ref_words', 0)}w)" for k in keys]
        lim = {i: (0.25, "") for i in range(len(keys))}
        y0 = 90 + j * 360
        draw.bars(dr, (80, y0, 1440, y0 + 260), f"{r} run", labs, [(c, [g(s, k, "wer") for k in keys]) for s, c in zip(srcs, (draw.BASE, P1C, draw.CAND), strict=True)], 1.0, [0, 0.25, 0.5, 0.75, 1.0], lim)
    img.save(path)


def main(out: Path = HERE, check: bool = False) -> None:
    d = load(out / "detail.json")
    base = load(STUDY / "baseline" / "detail.json")
    cs = load(out / "cost-same-session.json")
    if cs:
        d["cost"] = cs["candidate"]
        base_cost = dict(base, cost=cs["baseline"])  # gpu_x against the baseline re-measured in the same lock
    else:
        base_cost = base
    cm = cost_model(d, cs, base)
    bl = backlog(d) if g(d, "p3", "day") else (backlog(d, "std") if g(d, "p3", "std") else {})
    d["cost_model"], d["backlog"], d["musts"] = cm, bl, musts(d, base)
    line = metrics(d, base_cost, cm, bl)
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    draw.side(d, base_cost, out / "side.png")
    if g(d, "mixed", "day") or g(d, "mixed", "std"):
        fig_mixed(d, out / "fig-mixed-day.png", "day" if g(d, "mixed", "day") else "std")
    fig_pairs(d, out / "fig-overlap-pairs.png")
    (out / "detail.json").write_text(json.dumps(d, indent=1, default=lambda v: v.item() if hasattr(v, "item") else str(v)), encoding="utf-8")
    print(line)
    for k, row in d["musts"].items():
        print(k, {w: {m: (None if v is None else round(v, 3) if isinstance(v, float) else v) for m, v in x.items()} for w, x in row.items()})
    if check:
        print("finalize ok", sorted(p.name for p in out.iterdir()))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE)
