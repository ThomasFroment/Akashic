"""Run one candidate on the shared harness and write <out>/{metrics.txt, metrics.json, side.png}.

    uv run python eval/small-text-ocr/harness/run.py <candidate.py> [--out DIR] [--replay-rows N]
                  [--no-replay] [--renderers chromium,chromium_gray,pillow]

<candidate.py> exposes prepare(img, ctx) -> list[Image] (what Windows OCR gets for one window crop),
optionally merge(results, ctx) -> str, read(img, ctx) -> str (replaces prepare+OCR+merge, e.g. for a
two-pass method), store(img, ctx) -> bytes and load(bytes, ctx) -> Image (what deferred OCR keeps).
Anything left out falls back to baseline.py (today's path). ctx: path ("live" | "deferred" |
"replay"), scale (1.0 / 1.25), ocr (Akashic's engine, fr-FR), ocr_en (en-US), extra (dict).
Default out dir: next to the candidate, named after it (baseline.py -> baseline/).

Corpus: harness/corpus (built by corpus.py on first use). Live path = read(raw window); deferred
path = read(load(store(raw window))). Timing: 12 Edge pages (9/12/16 px, dark/light, mixed, both
scales), one warm-up call, then 9 interleaved repeats of baseline and candidate per page.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus as corpus_mod  # noqa: E402
import replay  # noqa: E402
from shared import CORPUS, ROOT, SCALES, Candidate, Ctx, best_line_cer, bucket, engines, hyp_of, short_bucket  # noqa: E402

from akashic.capture import imaging  # noqa: E402

TIMING = {"px": (9, 12, 16), "theme": ("dark", "light"), "lang": ("mixed",), "renderer": "chromium", "repeats": 9}
RENDER_TAG = {"chromium": "", "chromium_gray": "gray_", "pillow": "pil_"}


def clock(fn, *args):
    w, c = time.perf_counter(), time.process_time()
    out = fn(*args)
    return out, (time.perf_counter() - w) * 1000, (time.process_time() - c) * 1000


def corpus_eval(cand: Candidate, pages: list[dict], renderers: list[str], log) -> tuple[list[dict], dict]:
    fr, en = engines()
    recs = []
    nbytes = {"cand": 0, "base": 0}
    t0 = time.perf_counter()
    for r in renderers:
        for p in pages:
            img = Image.open(CORPUS / r / p["dir"] / f"{p['id']}.png").convert("RGB")
            truth = [ln["text"] for ln in p["lines"] if ln["fits"]]
            meta = {"renderer": r, "scale": SCALES[p["scale"]], "theme": p["theme"], "lang": p["lang"], "px": p["px"],
                    "bucket": bucket(p["px"] * p["scale"])}
            text, ms, cpu = clock(cand.read, img, Ctx("live", p["scale"], fr, en))
            hyp = hyp_of(text)
            recs += [{**meta, "path": "live", "cer": best_line_cer(t, hyp)} for t in truth]
            dctx = Ctx("deferred", p["scale"], fr, en)
            data = cand.store(img, dctx)
            if r == "chromium":
                nbytes["cand"] += len(data)
                nbytes["base"] += len(imaging.encode_webp(img))
            hyp = hyp_of(cand.read(cand.load(data, dctx), dctx))
            recs += [{**meta, "path": "deferred", "cer": best_line_cer(t, hyp)} for t in truth]
        log(f"  corpus {r}: {time.perf_counter() - t0:.0f} s")
    return recs, nbytes


def timing(cand: Candidate, base: Candidate, pages: list[dict], log) -> dict:
    fr, en = engines()
    sel = [p for p in pages if p["px"] in TIMING["px"] and p["theme"] in TIMING["theme"] and p["lang"] in TIMING["lang"]]
    same = cand.path.resolve() == base.path.resolve()
    who = {"base": base} if same else {"base": base, "cand": cand}
    samples = {k: defaultdict(list) for k in who}
    for p in sel:
        img = Image.open(CORPUS / TIMING["renderer"] / p["dir"] / f"{p['id']}.png").convert("RGB")
        live, dctx = Ctx("live", p["scale"], fr, en), Ctx("deferred", p["scale"], fr, en)
        stored = {k: c.store(img, dctx) for k, c in who.items()}
        for k, c in who.items():  # warm-up
            c.read(img, live)
            c.read(c.load(stored[k], dctx), dctx)
        for _ in range(TIMING["repeats"]):
            for k, c in who.items():
                _, ms, cpu = clock(c.read, img, live)
                samples[k]["live_ms"].append(ms)
                samples[k]["live_cpu"].append(cpu)
                _, ms_s, cpu_s = clock(c.store, img, dctx)
                im2, ms_l, cpu_l = clock(c.load, stored[k], dctx)
                _, ms_r, cpu_r = clock(c.read, im2, dctx)
                samples[k]["store_ms"].append(ms_s)
                samples[k]["def_ms"].append(ms_l + ms_r)
                samples[k]["def_cpu"].append(cpu_s + cpu_l + cpu_r)
    if same:
        samples["cand"] = samples["base"]
    log(f"  timing: {len(sel)} pages x {TIMING['repeats']} repeats")
    out = {}
    for k, s in samples.items():
        out[k] = {f"{m}_p50": float(np.percentile(v, 50)) for m, v in s.items()} | {f"{m}_p95": float(np.percentile(v, 95)) for m, v in s.items()}
        out[k] |= {"live_cpu_mean": float(np.mean(s["live_cpu"])), "def_cpu_mean": float(np.mean(s["def_cpu"])), "n": len(s["live_ms"])}
    return out


def mean_of(recs, key="cer", **flt) -> tuple[float | None, int]:
    xs = [r[key] for r in recs if all(r.get(k) == v for k, v in flt.items())]
    return (float(np.mean(xs)) if xs else None, len(xs))


def summarize(recs, rep, tim, nbytes, renderers) -> tuple[dict, dict]:
    """(flat headline metrics, nested breakdown)."""
    m: dict[str, float | None] = {}
    for r in renderers:
        tag = RENDER_TAG[r]
        for path, ptag in (("live", ""), ("deferred", "def_")):
            for sc in ("1440", "4k"):
                m[f"{tag}cer_{ptag}{sc}"] = mean_of(recs, renderer=r, path=path, scale=sc)[0]
                for b in ("<=11", "12-15", ">=16"):
                    m[f"{tag}cer_{ptag}{short_bucket(b)}_{sc}"] = mean_of(recs, renderer=r, path=path, scale=sc, bucket=b)[0]
    m["ms_p50"], m["ms_p95"] = tim["cand"]["live_ms_p50"], tim["cand"]["live_ms_p95"]
    m["dms_p95"] = tim["cand"]["live_ms_p95"] - tim["base"]["live_ms_p95"]
    m["ms_def_p50"], m["ms_def_p95"] = tim["cand"]["def_ms_p50"], tim["cand"]["def_ms_p95"]
    m["dms_def_p95"] = tim["cand"]["def_ms_p95"] - tim["base"]["def_ms_p95"]
    m["cpu_ratio"] = tim["cand"]["live_cpu_mean"] / tim["base"]["live_cpu_mean"]
    m["cpu_def_ratio"] = tim["cand"]["def_cpu_mean"] / tim["base"]["def_cpu_mean"]
    m["bytes_ratio"] = nbytes["cand"] / nbytes["base"] if nbytes["base"] else None
    res = {"1440": "2560x1440", "4k": "3840x2160"}
    if rep:
        for sc, rs in res.items():
            m[f"replay_cer_{sc}"] = mean_of(rep, res=rs)[0]
            m[f"replay_join_{sc}"] = mean_of(rep, "cer_join", res=rs)[0]
            m[f"replay_stored_{sc}"] = mean_of(rep, "stored_cer", res=rs)[0]
            m[f"replay_stored_join_{sc}"] = mean_of(rep, "stored_cer_join", res=rs)[0]
        m["replay_cer_le11"] = mean_of(rep, bucket="<=11")[0]
        m["replay_ms_p95"] = float(np.percentile([r["ms"] for r in rep], 95))
    nested: dict = {"corpus": {}, "replay": {}, "timing": tim, "bytes": nbytes}
    for r in renderers:
        for path in ("live", "deferred"):
            for sc in ("1440", "4k"):
                for dim in ("bucket", "theme", "lang", "px"):
                    vals = sorted({x[dim] for x in recs if x["renderer"] == r}, key=str)
                    for v in vals:
                        c, n = mean_of(recs, renderer=r, path=path, scale=sc, **{dim: v})
                        if n:
                            nested["corpus"][f"{r}/{path}/{sc}/{dim}={v}"] = {"cer": c, "n": n}
    for run_id in sorted({x["run"] for x in rep}):
        for rs in sorted({x["res"] for x in rep}):
            for dim in ("all", "bucket", "lang", "theme", "src"):
                vals = ["*"] if dim == "all" else sorted({str(x[dim]) for x in rep})
                for v in vals:
                    sub = [x for x in rep if x["run"] == run_id and x["res"] == rs and (dim == "all" or str(x[dim]) == v)]
                    if sub:
                        nested["replay"][f"{run_id}/{rs}/{dim}={v}"] = {
                            k: float(np.mean([x[k] for x in sub])) for k in ("cer", "cer_join", "stored_cer", "stored_cer_join")} | {
                            "n": len(sub), "ms_p50": float(np.percentile([x["ms"] for x in sub], 50)),
                            "frame_scale_min": min(x["frame_scale"] for x in sub)}
    return m, nested


def fmt(v) -> str:
    return "na" if v is None else f"{v:.3f}" if abs(v) < 100 else f"{v:.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--replay-rows", type=int, default=100, help="rows per resolution per run")
    ap.add_argument("--no-replay", action="store_true")
    ap.add_argument("--renderers", default=",".join(corpus_mod.RENDERERS))
    a = ap.parse_args()
    t_start = time.perf_counter()
    cand_path = a.candidate.resolve()
    out = a.out or cand_path.parent / cand_path.stem
    out.mkdir(parents=True, exist_ok=True)
    log = lambda s: print(s, flush=True)  # noqa: E731
    cand, base = Candidate(cand_path), Candidate(ROOT / "baseline.py")
    pages = corpus_mod.build()["pages"]
    renderers = a.renderers.split(",")
    log(f"{cand.name}: {len(pages)} pages x {len(renderers)} renderers -> {out}")
    recs, nbytes = corpus_eval(cand, pages, renderers, log)
    tim = timing(cand, base, pages, log)
    rep = [] if a.no_replay else replay.run(cand, a.replay_rows, log=log)
    m, nested = summarize(recs, rep, tim, nbytes, renderers)
    nested["runtime_s"] = round(time.perf_counter() - t_start, 1)
    nested["metrics"] = m
    (out / "metrics.json").write_text(json.dumps(nested, indent=1), encoding="utf-8")
    keys = ["cer_le11_1440", "cer_le11_4k", "cer_12_15_1440", "cer_ge16_1440", "cer_1440", "cer_4k",
            "cer_def_le11_1440", "cer_def_le11_4k", "cer_def_ge16_1440", "cer_def_1440", "cer_def_4k",
            "gray_cer_le11_1440", "gray_cer_1440", "gray_cer_def_1440", "pil_cer_le11_1440", "pil_cer_1440", "pil_cer_def_1440",
            "ms_p50", "ms_p95", "dms_p95", "ms_def_p50", "ms_def_p95", "dms_def_p95", "cpu_ratio", "cpu_def_ratio", "bytes_ratio",
            "replay_cer_1440", "replay_cer_4k", "replay_cer_le11", "replay_join_1440", "replay_join_4k",
            "replay_stored_1440", "replay_stored_4k", "replay_stored_join_1440", "replay_ms_p95"]
    line = " ".join(f"{k}={fmt(m.get(k))}" for k in keys if k in m) + f" runtime_s={nested['runtime_s']:.0f}"
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    import plot

    base_json = ROOT / "baseline" / "metrics.json"
    is_base = cand_path == (ROOT / "baseline.py").resolve()
    base_m = m if is_base or not base_json.is_file() else json.loads(base_json.read_text(encoding="utf-8"))["metrics"]
    plot.side(out / "side.png", cand.name, m, base_m, tim)
    if cand_path.parent != out and not (out / cand_path.name).exists():
        shutil.copy2(cand_path, out / cand_path.name)
    log(line)


if __name__ == "__main__":
    main()
