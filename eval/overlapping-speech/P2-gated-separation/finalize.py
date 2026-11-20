"""metrics.txt (harness line + P2 fields), side.png (harness draw.py) and fig-*.png from detail.json,
bench.json (separation cost) and cost-*.json (harness cost.py, candidate and re-measured baseline).

    uv run python eval/overlapping-speech/P2-gated-separation/finalize.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import draw  # noqa: E402
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("harness_run", HERE.parent / "harness" / "run.py")
hr = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(hr)


def load(name):
    p = HERE / name
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def main() -> None:
    d = load("detail.json")
    base = json.loads((HERE.parent / "baseline" / "detail.json").read_text(encoding="utf-8"))
    cc, cb, bench = load("cost-candidate.json"), load("cost-baseline.json"), load("bench.json")
    if cc:
        d["cost"] = cc
    if cb:
        base = dict(base, cost=cb)  # gpu_x against the baseline re-measured in the same lock
    line = hr.metrics_line(d, base)
    f = lambda v, n=3: "nan" if v is None else f"{v:.{n}f}"
    extra = []
    for r in ("std", "day"):
        x = d["runs"].get(r)
        if not x:
            continue
        st, g, ex = x["p2_stats"], x["p2_gate"], x["p2_extras"]["no_paused"]
        extra += [f"gate_{r}={f(st['gate_rate_audio'])}", f"sep_share_{r}={f(st['sep_share_of_system_audio'])}",
                  f"recall_{r}={f(g['all']['recall'])}", f"recall_xapp_{r}={f(g.get('cross_app', {}).get('recall'))}",
                  f"recall_same_{r}={f(g.get('same_app', {}).get('recall'))}",
                  f"np_ovl_{r}={f(ex['M9.1']['overlap']['wer'])}", f"np_ovl_local_{r}={f(ex['overlap_local_wer'])}",
                  f"np_clean_{r}={f(ex['M9.1']['no_overlap']['wer'])}"]
    if bench:
        extra += [f"sep_rtf_cpu_8s={f(bench['cpu_rtf']['8'])}" if "cpu_rtf" in bench else "",
                  f"sep_rtf_gpu_8s={f(bench['cuda_rtf']['8'], 4)}" if "cuda_rtf" in bench else "",
                  f"sep_vram_gb={f(bench['cuda_vram_delta_mib'] / 1024, 2)}" if "cuda_vram_delta_mib" in bench else ""]
    cg = load("cost-candidate-gpu.json")
    if cg and cb:
        extra += [f"gpu_x_gpusep={f(cg['gpu_s_per_h'] / cb['gpu_s_per_h'], 2)}", f"vram_gb_gpusep={f(cg['vram_peak_mib'] / 1024, 2)}"]
    day = d["runs"].get("day")
    if day:
        bd = base["runs"]["day"]
        # the accuracy replays shared the GPU with P1's replays, so their wall-clock GPU seconds are
        # inflated; the daily average is estimated from what Whisper decoded (audio and calls)
        extra.append(f"gpu_x_day_avg_audio={f(day['audio_s_transcribed'] / bd['audio_s_transcribed'], 3)}")
        extra.append(f"calls_x_day={f(day['p2_stats']['whisper_calls'] / bd['n_out'], 3)}")
    std = d["runs"].get("std")
    if std:
        extra.append(f"calls_x_std={f(std['p2_stats']['whisper_calls'] / base['runs']['std']['n_out'], 3)}")
    (HERE / "metrics.txt").write_text(" ".join([line, *[e for e in extra if e]]) + "\n", encoding="utf-8")
    draw.side(d, base, HERE / "side.png")
    figs(d)
    musts(d, base)
    print(" ".join([line, *[e for e in extra if e]]))


def musts(d, base) -> None:
    """musts.json: each must as the harness scores it and with paused-speech words removed, P2 vs today."""
    out = {}
    for r in ("std", "day"):
        x = d["runs"].get(r)
        if not x:
            continue
        b = base["runs"][r]
        np_c, np_t = x["p2_extras"]["no_paused"], d["p2"]["today_extras"][r]["no_paused"]
        row = {}
        for way, cm, tm, cd, td, cl, tl in (("harness", x["M9.1"], b["M9.1"], x["M8.2"], b["M8.2"], x["overlap_local_wer"], b.get("overlap_local_wer")),
                                           ("no_paused", np_c["M9.1"], np_t["M9.1"], np_c["M8.2"], np_t["M8.2"], np_c["overlap_local_wer"], np_t["overlap_local_wer"])):
            row[way] = {"overlap_wer": [tm["overlap"]["wer"], cm["overlap"]["wer"]], "overlap_local_wer": [tl, cl],
                        **{f"clean_{g}": [(tm.get(k) or {}).get("wer"), (cm.get(k) or {}).get("wer")] for g, k in (("all", "no_overlap"), ("en", "en"), ("fr", "fr"), ("mixed", "mixed"))},
                        "der": [td.get("der"), cd.get("der")]}
        out[r] = row
    out["_format"] = "[today, P2]; must: overlap_wer <= 0.25; clean_* and der not above today"
    (HERE / "musts.json").write_text(json.dumps(out, indent=1), encoding="utf-8")


def figs(d) -> None:
    today = d["p2"]["today_extras"]
    W, H = 1500, 560
    # per-language overlap WER
    img = Image.new("RGB", (W, H), draw.BG)
    dr = ImageDraw.Draw(img)
    dr.text((20, 12), "P2 overlap WER per group language: today (grey) vs P2 (blue); left harness scoring, right paused words removed", fill=draw.FG, font=draw.FT)
    for col, (key, title) in enumerate((("overlap_by_lang", "harness scoring"), ("no_paused", "paused-speech words removed"))):
        labs, bv, cv, lim = [], [], [], {}
        for r in ("std", "day"):
            if r not in d["runs"]:
                continue
            c = d["runs"][r]["p2_extras"]
            t = today[r]
            cl = (c["no_paused"]["overlap_by_lang"] if key == "no_paused" else c["overlap_by_lang"])
            tl = (t["no_paused"]["overlap_by_lang"] if key == "no_paused" else t["overlap_by_lang"])
            for lang in sorted(set(cl) | set(tl)):
                lim[len(labs)] = (0.25, "")
                labs.append(f"{r} {lang}")
                bv.append((tl.get(lang) or {}).get("wer"))
                cv.append((cl.get(lang) or {}).get("wer"))
        x0 = 70 + col * 740
        draw.bars(dr, (x0, 90, x0 + 640, 480), title + " (red: 0.25)", labs, [(draw.BASE, bv), (draw.CAND, cv)], 1.0, [0, 0.25, 0.5, 0.75, 1.0], lim)
    img.save(HERE / "fig-overlap-lang.png")
    # gate recall vs truth overlap
    img = Image.new("RGB", (W, H), draw.BG)
    dr = ImageDraw.Draw(img)
    dr.text((20, 12), "P2 gate: share of truth overlap time flagged (pyannote >= 2 speakers >= 0.5 s); blue gated, green separated; grey precision", fill=draw.FG, font=draw.FT)
    labs, gv, sv, pv = [], [], [], []
    for r in ("std", "day"):
        if r not in d["runs"]:
            continue
        g = d["runs"][r]["p2_gate"]
        for k in ("cross_app", "same_app", "all"):
            if k in g:
                labs.append(f"{r} {k.replace('_app', '')}")
                gv.append(g[k]["recall"])
                sv.append(g[k]["recall_separated"])
                pv.append(g["precision"] if k == "all" else None)
    draw.bars(dr, (70, 90, 700, 480), "runs: recall by overlap kind", labs, [(draw.CAND, gv), ((110, 190, 140), sv), (draw.BASE, pv)], 1.0, [0, 0.25, 0.5, 0.75, 1.0])
    s = (d.get("synthetic") or {}).get("p2_gate")
    if s:
        keys = [k for k in ("ov0.25", "ov0.5", "ov0.75", "ov1.0", "procs_two", "procs_one", "bed_none", "bed_music", "bed_game") if k in s]
        draw.bars(dr, (800, 90, 1440, 480), "synthetic: recall by overlap ratio / layout / bed", [k.replace("procs_", "").replace("bed_", "") for k in keys],
                  [(draw.CAND, [s[k]["recall"] for k in keys])], 1.0, [0, 0.25, 0.5, 0.75, 1.0])
    img.save(HERE / "fig-gate-recall.png")
    # music-bed groups apart, and per pair kind (cross-app pairs vs the same app)
    base = json.loads((HERE.parent / "baseline" / "detail.json").read_text(encoding="utf-8"))
    img = Image.new("RGB", (W, H), draw.BG)
    dr = ImageDraw.Draw(img)
    dr.text((20, 12), "P2 overlap WER: music-bed groups apart (left, harness / no-paused) and per app pair (right, harness); grey today, blue P2", fill=draw.FG, font=draw.FT)
    labs, bv, cv = [], [], []
    for r in ("std", "day"):
        if r not in d["runs"]:
            continue
        c, t = d["runs"][r]["p2_extras"], today[r]
        for key, src in (("h", lambda z: z["overlap_music_split"]), ("np", lambda z: z["no_paused"]["overlap_music_split"])):
            for bed in ("music_bed", "no_music_bed"):
                labs.append(f"{r} {key} {'music' if bed == 'music_bed' else 'none'}")
                bv.append(src(t)[bed]["wer"])
                cv.append(src(c)[bed]["wer"])
    draw.bars(dr, (70, 90, 700, 480), "music bed vs none", labs, [(draw.BASE, bv), (draw.CAND, cv)], 1.0, [0, 0.25, 0.5, 0.75, 1.0])
    labs, bv, cv = [], [], []
    for r in ("std", "day"):
        if r not in d["runs"]:
            continue
        cp, bp = d["runs"][r]["overlap_by_pair"], base["runs"][r].get("overlap_by_pair", {})
        for k in sorted(set(cp) | set(bp)):
            labs.append(f"{r} " + "+".join(p[:3] for p in k.split("+")))
            bv.append((bp.get(k) or {}).get("wer"))
            cv.append((cp.get(k) or {}).get("wer"))
    draw.bars(dr, (800, 90, 1440, 480), "per app pair", labs, [(draw.BASE, bv), (draw.CAND, cv)], 1.0, [0, 0.25, 0.5, 0.75, 1.0])
    img.save(HERE / "fig-music-pairs.png")


if __name__ == "__main__":
    main()
