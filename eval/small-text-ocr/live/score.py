"""Score baseline, C1 (c1_r2a) and C2 on the live WGC captures written by capture.py.

    uv run python eval/small-text-ocr/live/score.py [--repeats 9]

Per capture (raw RGB from WgcCaptures, saved losslessly): ClearType fringe energy of every strip as
C2 computes it (c2.strips on the find_text_box crop, c2.coverage), split into page strips (inside the
viewport) and browser-UI strips; the corpus' ink-pixel fringe share (corpus.ink_stats) on the
viewport; then each candidate's read() on the live path (ctx.path='live', ctx.scale = the monitor's
scale), scored against the page's truth lines that fit the real viewport, with best_line_cer
(single OCR line, as the corpus metrics) and wrap-tolerant (joined OCR lines, as the replay metric).
Timing: the harness' TIMING pages (9/12/16 px, dark/light, mixed) of each capture set, one warm-up
call per candidate, then N interleaved repeats; wall ms and process CPU ms per call.
Writes live/results.json (numbers only, no OCR text), live/summary.md and live/side.png.
"""

from __future__ import annotations

import argparse
import atexit
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

LIVE = Path(__file__).resolve().parent
ROOT = LIVE.parent
sys.path.insert(0, str(ROOT / "harness"))
import corpus as corpus_mod  # noqa: E402
from shared import Candidate, Ctx, best_line_cer, bucket, engines, hyp_of, joined  # noqa: E402

from akashic.capture import imaging  # noqa: E402

CANDS = {"baseline": ROOT / "baseline.py", "C1": ROOT / "C1" / "c1_r2a.py", "C2": ROOT / "C2" / "c2.py"}
HEADLESS = {"baseline": ROOT / "baseline" / "metrics.json", "C1": ROOT / "C1" / "c1_r2a" / "metrics.json",
            "C2": ROOT / "C2" / "c2" / "metrics.json"}
TIMING = {"px": (9, 12, 16), "theme": ("dark", "light"), "lang": ("mixed",)}
SC = {1.0: "1440", 1.25: "4k"}
BUCKETS = ("<=11", "12-15", ">=16")


def clock(fn, *args):
    w, c = time.perf_counter(), time.process_time()
    out = fn(*args)
    return out, (time.perf_counter() - w) * 1000, (time.process_time() - c) * 1000


def load_cands() -> dict[str, Candidate]:
    cands = {k: Candidate(p) for k, p in CANDS.items()}
    atexit.unregister(cands["C1"].mod._dump_stats)  # never write into C1/ (frozen study files)
    return cands


def fringe_of(img: Image.Image, vp_top: int, vp_left: int, c2) -> list[dict]:
    """Per strip of the live crop: region (page / ui), height, fringe energy (None when no ink)."""
    box = imaging.find_text_box(img)
    if box is None:
        return []
    region = img.crop((box.left, box.top, box.right, box.bottom))
    rgb, gray = np.asarray(region), np.asarray(region.convert("L"))
    out = []
    for x0, y0, x1, y1, h in c2.strips(gray):
        cov = c2.coverage(rgb[y0:y1, x0:x1])
        out.append({"where": "page" if box.top + y0 >= vp_top and box.left + x0 >= vp_left else "ui", "h": int(h), "w": int(x1 - x0),
                    "fringe": None if cov is None else round(cov[2], 4)})
    return out


def score_set(meta: dict, cands: dict[str, Candidate], specs: dict, log) -> tuple[list[dict], list[dict], list[dict]]:
    fr, en = engines()
    tag = f"{meta['browser']}-{meta['monitor']}"
    scale = meta["scale"]
    c2 = cands["C2"].mod
    c1 = cands["C1"].mod
    recs, strips_out, calls = [], [], []
    for pg in meta["pages"]:
        spec = specs[pg["id"]]
        img = Image.open(LIVE / "captures" / tag / f"{pg['id']}.png").convert("RGB")
        vp_h = round(pg["viewport_css"][1] * pg["dpr"])
        vp_top = img.height - vp_h - 2
        vp_left = img.width - round(pg["viewport_css"][0] * pg["dpr"]) - 12  # Zen: sidebar on the left
        base = {"browser": meta["browser"], "monitor": meta["monitor"], "scale": SC[scale], "page": pg["id"],
                "lang": spec["lang"], "theme": spec["theme"], "px": spec["px"], "bucket": bucket(spec["px"] * scale)}
        for s in fringe_of(img, vp_top, vp_left, c2):
            strips_out.append({**base, **s})
        if spec["theme"] != "color":
            vp = img.crop((max(0, vp_left + 12), max(0, vp_top), img.width, img.height))
            _, share = corpus_mod.ink_stats(vp, corpus_mod.THEMES[spec["theme"]][0], [])
            strips_out.append({**base, "where": "pixels", "pixel_fringe_share": round(share, 4)})
        truth = [ln["text"] for ln, f in zip(spec["lines"], pg["fits"], strict=True) if f == "1"]
        for name, cand in cands.items():
            st: dict = {}
            ctx = Ctx("live", scale, fr, en, extra={"stats": st} if name == "C2" else {})
            n1 = len(c1.STATS)
            text, ms, cpu = clock(cand.read, img, ctx)
            hyp = hyp_of(text)
            hj = joined(hyp)
            call = {**base, "cand": name, "ms": ms, "cpu": cpu, "n_truth": len(truth)}
            if name == "C2":
                c = (st.get("calls") or [{}])[-1]
                call |= {"s": c.get("s", 1.0), "strips": c.get("strips"), "small": c.get("small"), "cleartype": c.get("cleartype"),
                         "order": (st.get("order") or [None])[-1]}
            if name == "C1" and len(c1.STATS) > n1:
                c = c1.STATS[-1]
                call |= {"s": c.get("s"), "mode": c.get("mode")}
            calls.append(call)
            for t in truth:
                recs.append({**base, "cand": name, "cer": best_line_cer(t, hyp), "cer_join": best_line_cer(t, hj)})
        c1.STATS.clear()
    log(f"  {tag}: {len(meta['pages'])} pages scored")
    return recs, strips_out, calls


def timing(metas: list[dict], cands: dict[str, Candidate], specs: dict, repeats: int, log) -> list[dict]:
    fr, en = engines()
    out = []
    for meta in metas:
        tag = f"{meta['browser']}-{meta['monitor']}"
        for pg in meta["pages"]:
            spec = specs[pg["id"]]
            if spec["px"] not in TIMING["px"] or spec["theme"] not in TIMING["theme"] or spec["lang"] not in TIMING["lang"]:
                continue
            img = Image.open(LIVE / "captures" / tag / f"{pg['id']}.png").convert("RGB")
            ctx = Ctx("live", meta["scale"], fr, en)
            for cand in cands.values():
                cand.read(img, ctx)
            for _ in range(repeats):
                for name, cand in cands.items():
                    _, ms, cpu = clock(cand.read, img, ctx)
                    out.append({"browser": meta["browser"], "scale": SC[meta["scale"]], "page": pg["id"], "px": spec["px"],
                                "cand": name, "ms": ms, "cpu": cpu})
            cands["C1"].mod.STATS.clear()
        log(f"  timing {tag} done")
    return out


def mean(xs) -> float | None:
    xs = list(xs)
    return float(np.mean(xs)) if xs else None


def headless() -> dict:
    """Headless-render corpus CER (single-line, chromium renderer, live path) per px and bucket, from the study."""
    out = {}
    for name, p in HEADLESS.items():
        m = json.loads(p.read_text(encoding="utf-8"))
        corp = m["corpus"]
        out[name] = {k.split("/", 2)[2]: v["cer"] for k, v in corp.items() if k.startswith("chromium/live/")}
    return out


def summarize(recs, strips, calls, tim) -> dict:
    res: dict = {"cer": {}, "fringe": {}, "c2_order": {}, "c2_mode": {}, "c1_mode": {}, "timing": {}, "per_call_all_pages": {}}
    sets = sorted({(r["browser"], r["scale"]) for r in recs})
    for br, sc in sets:
        key = f"{br}/{sc}"
        R = [r for r in recs if r["browser"] == br and r["scale"] == sc]
        d: dict = {}
        for cand in CANDS:
            rc = [r for r in R if r["cand"] == cand]
            d[cand] = {"all": {"cer": mean(r["cer"] for r in rc), "cer_join": mean(r["cer_join"] for r in rc), "n": len(rc)}}
            for b in BUCKETS:
                rb = [r for r in rc if r["bucket"] == b]
                if rb:
                    d[cand][f"bucket={b}"] = {"cer": mean(r["cer"] for r in rb), "cer_join": mean(r["cer_join"] for r in rb), "n": len(rb)}
            for px in sorted({r["px"] for r in rc}):
                rp = [r for r in rc if r["px"] == px]
                d[cand][f"px={px}"] = {"cer": mean(r["cer"] for r in rp), "cer_join": mean(r["cer_join"] for r in rp), "n": len(rp)}
            for th in sorted({r["theme"] for r in rc}):
                rt = [r for r in rc if r["theme"] == th]
                d[cand][f"theme={th}"] = {"cer": mean(r["cer"] for r in rt), "cer_join": mean(r["cer_join"] for r in rt), "n": len(rt)}
        res["cer"][key] = d
        S = [s for s in strips if s["browser"] == br and s["scale"] == sc]
        f: dict = {}
        for where in ("page", "ui"):
            ink = [s for s in S if s["where"] == where and s["fringe"] is not None]
            small = [s for s in ink if s["h"] <= 15]
            f[where] = {
                "strips": len(ink), "share_fringed": mean(s["fringe"] >= 0.05 for s in ink) if ink else None,
                "small_strips": len(small), "small_share_fringed": mean(s["fringe"] >= 0.05 for s in small) if small else None,
                "fringe_median": float(np.median([s["fringe"] for s in ink])) if ink else None,
                "fringe_p10": float(np.percentile([s["fringe"] for s in ink], 10)) if ink else None,
                "gray_strips": sum(s["fringe"] < 0.05 for s in ink),
                "gray_by_h": {str(h): sum(1 for s in ink if s["fringe"] < 0.05 and s["h"] == h)
                              for h in sorted({s["h"] for s in ink if s["fringe"] < 0.05})},
            }
            if where == "page":
                for th in ("dark", "light", "color"):
                    sub = [s for s in small if s["theme"] == th]
                    f[where][f"small_share_fringed_{th}"] = mean(s["fringe"] >= 0.05 for s in sub) if sub else None
        px_share = [s["pixel_fringe_share"] for s in S if s["where"] == "pixels"]
        for th in ("dark", "light"):
            f[f"pixel_fringe_share_{th}"] = mean(s["pixel_fringe_share"] for s in S if s["where"] == "pixels" and s["theme"] == th)
        f["pixel_fringe_share"] = mean(px_share)
        res["fringe"][key] = f
        C2c = [c for c in calls if c["browser"] == br and c["scale"] == sc and c["cand"] == "C2"]
        orders = [c["order"] for c in C2c if c.get("order")]
        res["c2_order"][key] = {"rgb": orders.count("rgb"), "bgr": orders.count("bgr"), "calls_voting": len(orders), "calls": len(C2c)}
        res["c2_mode"][key] = {"mosaic_calls": sum(1 for c in C2c if (c.get("s") or 1.0) > 1.0),
                               "s_median": mean([c["s"] for c in C2c if (c.get("s") or 1.0) > 1.0] or [1.0]),
                               "cleartype_strips": sum(c.get("cleartype") or 0 for c in C2c),
                               "small_strips": sum(c.get("small") or 0 for c in C2c)}
        C1c = [c for c in calls if c["browser"] == br and c["scale"] == sc and c["cand"] == "C1"]
        res["c1_mode"][key] = {"mosaic_calls": sum(1 for c in C1c if c.get("mode") == "mosaic"), "calls": len(C1c),
                               "s_median": mean([c["s"] for c in C1c if c.get("mode") == "mosaic"] or [1.0])}
        # one call per page over every page (not repeated)
        pc = {}
        for cand in CANDS:
            cc = [c for c in calls if c["browser"] == br and c["scale"] == sc and c["cand"] == cand]
            pc[cand] = {"ms_p50": float(np.percentile([c["ms"] for c in cc], 50)), "ms_p95": float(np.percentile([c["ms"] for c in cc], 95)),
                        "cpu_mean": mean(c["cpu"] for c in cc)}
        res["per_call_all_pages"][key] = pc
    groups = {"all": tim} | {f"{b}/{s}": [t for t in tim if t["browser"] == b and t["scale"] == s] for b, s in sets}
    for g, T in groups.items():
        if not T:
            continue
        d = {}
        for cand in CANDS:
            tt = [t for t in T if t["cand"] == cand]
            d[cand] = {"ms_p50": float(np.percentile([t["ms"] for t in tt], 50)), "ms_p95": float(np.percentile([t["ms"] for t in tt], 95)),
                       "cpu_mean": mean(t["cpu"] for t in tt), "n": len(tt)}
        for cand in ("C1", "C2"):
            d[cand]["cpu_ratio"] = d[cand]["cpu_mean"] / d["baseline"]["cpu_mean"]
            d[cand]["dms_p95"] = d[cand]["ms_p95"] - d["baseline"]["ms_p95"]
            d[cand]["dms_p50"] = d[cand]["ms_p50"] - d["baseline"]["ms_p50"]
        res["timing"][g] = d
    hl = headless()
    res["headless_single_line_cer"] = {c: {k: v for k, v in hl[c].items() if "/px=" in k or "/bucket=" in k} for c in hl}
    return res


def fmt(v, nd=3) -> str:
    return "na" if v is None else f"{v:.{nd}f}"


def write_summary(res: dict, metas: list[dict], path: Path) -> None:
    L = ["# Live WGC check of #190 (small-text OCR)", "",
         "Generated by `live/score.py` from captures made by `live/capture.py` (real windowed browsers, "
         "WgcCaptures raw frames, corpus pages with truth lines). Numbers only; no OCR text.", "",
         "## Capture sets", "", "| set | pages | window (device px) | viewport CSS | dpr |", "| :- | -: | :- | :- | :- |"]
    for m in metas:
        vps = sorted({tuple(p["viewport_css"]) for p in m["pages"]})
        dprs = sorted({p["dpr"] for p in m["pages"]})
        r = m.get("window_rect")
        L.append(f"| {m['browser']}-{m['monitor']} (scale {m['scale']}) | {len(m['pages'])} | {r[2] - r[0]}x{r[3] - r[1]} | "
                 f"{', '.join(f'{a}x{b}' for a, b in vps)} | {', '.join(map(str, dprs))} |")
    L += ["", "## ClearType fringes on real captures", "",
          "Fringe energy = C2's mean |a_R - a_B| per strip (threshold 0.05). Headless renders: ClearType on 81-85 % of ink "
          "pixels (corpus `fringe_share_of_ink`: dark 0.806, light 0.849); fringe energy 0.31-0.43 per strip.", "",
          "| set | page strips fringed | small (<= 15 px) page strips fringed | page fringe median / p10 | pixel share dark / light | UI strips fringed |",
          "| :- | -: | -: | :- | :- | -: |"]
    for k, f in res["fringe"].items():
        p, u = f["page"], f["ui"]
        L.append(f"| {k} | {fmt(p['share_fringed'])} ({p['strips']}) | {fmt(p['small_share_fringed'])} ({p['small_strips']}) | "
                 f"{fmt(p['fringe_median'])} / {fmt(p['fringe_p10'])} | {fmt(f['pixel_fringe_share_dark'])} / {fmt(f['pixel_fringe_share_light'])} | "
                 f"{fmt(u['share_fringed'])} ({u['strips']}) |")
    L += ["", "## CER by device-px bucket (wrap-tolerant; single-line in brackets)", "",
          "| set | cand | <=11 | 12-15 | >=16 | all |", "| :- | :- | -: | -: | -: | -: |"]
    for k, d in res["cer"].items():
        for cand in CANDS:
            cells = []
            for b in (*BUCKETS, None):
                e = d[cand].get(f"bucket={b}") if b else d[cand]["all"]
                cells.append("" if not e else f"{fmt(e['cer_join'])} ({fmt(e['cer'])})")
            L.append(f"| {k} | {cand} | " + " | ".join(cells) + " |")
    L += ["", "Headless reference (single-line, chromium, live path): "
          + "; ".join(f"{c} <=11 1440 {fmt(res['headless_single_line_cer'][c].get('1440/bucket=<=11'))}, "
                      f"<=11 4k {fmt(res['headless_single_line_cer'][c].get('4k/bucket=<=11'))}" for c in CANDS), ""]
    L += ["## CER by CSS px (wrap-tolerant)", ""]
    pxs = sorted({int(k.split("=")[1]) for d in res["cer"].values() for k in d["baseline"] if k.startswith("px=")})
    L += ["| set | cand | " + " | ".join(f"{p}" for p in pxs) + " |", "| :- | :- | " + " | ".join("-:" for _ in pxs) + " |"]
    for k, d in res["cer"].items():
        for cand in CANDS:
            L.append(f"| {k} | {cand} | " + " | ".join(fmt(d[cand].get(f"px={p}", {}).get("cer_join")) for p in pxs) + " |")
    L += ["", "## C2 subpixel order vote and mosaic use", "", "| set | RGB votes | BGR votes | calls | mosaic calls | ClearType strips / small strips | C1 mosaic calls |",
          "| :- | -: | -: | -: | -: | :- | -: |"]
    for k in res["c2_order"]:
        o, m, c1 = res["c2_order"][k], res["c2_mode"][k], res["c1_mode"][k]
        L.append(f"| {k} | {o['rgb']} | {o['bgr']} | {o['calls']} | {m['mosaic_calls']} | {m['cleartype_strips']} / {m['small_strips']} | {c1['mosaic_calls']} |")
    L += ["", "## Timing (TIMING pages, warm-up then interleaved repeats)", "",
          "| group | baseline p50 / p95 ms | C1 p50 / p95 | C2 p50 / p95 | C1 CPU ratio, +p95 | C2 CPU ratio, +p95 |", "| :- | :- | :- | :- | :- | :- |"]
    for g, d in res["timing"].items():
        b, c1, c2 = d["baseline"], d["C1"], d["C2"]
        L.append(f"| {g} | {b['ms_p50']:.0f} / {b['ms_p95']:.0f} | {c1['ms_p50']:.0f} / {c1['ms_p95']:.0f} | {c2['ms_p50']:.0f} / {c2['ms_p95']:.0f} | "
                 f"{c1['cpu_ratio']:.2f}, {c1['dms_p95']:+.0f} | {c2['cpu_ratio']:.2f}, {c2['dms_p95']:+.0f} |")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def side(res: dict, path: Path) -> None:
    import plot
    from plot import BASE, CAND, INK, INK2, MUTED, PLANE, STORED, SURFACE, bars, f, legend
    from PIL import ImageDraw

    C1C = "#d08a2e"
    W, H = 1500, 900
    img = Image.new("RGB", (W, H), PLANE)
    d = ImageDraw.Draw(img)
    d.text((20, 14), "Live WGC captures: baseline vs C1 vs C2 on real Edge / Chrome / Zen windows", font=f(20, True), fill=INK)
    legend(d, 20, 46, [("baseline", BASE), ("C1 (c1_r2a)", C1C), ("C2", CAND), ("C2 headless (study)", STORED)])
    hl = res["headless_single_line_cer"]["C2"]
    groups = []
    for k, dd in res["cer"].items():
        br, sc = k.split("/")
        if br == "zen":
            continue
        for b in BUCKETS:
            e = [dd[c].get(f"bucket={b}", {}).get("cer_join") for c in CANDS]
            groups.append((f"{b}\n{br} {sc}", [*e, hl.get(f"{sc}/bucket={b}")]))
    bars(d, (20, 76, W - 20, 400), "Live CER (wrap-tolerant) by device-px bucket, Chrome and Edge; ^ = clipped",
         groups, [BASE, C1C, CAND, STORED], 0.8, [0, 0.2, 0.4, 0.6, 0.8], label_series=2)
    zg = []
    for k, dd in res["cer"].items():
        if not k.startswith("zen/"):
            continue
        for b in BUCKETS:
            zg.append((f"{b}\nzen {k.split('/')[1]}", [dd[c].get(f"bucket={b}", {}).get("cer_join") for c in CANDS]))
    if zg:
        bars(d, (20, 412, 640, 660), "Zen (Firefox engine), CER wrap-tolerant", zg, [BASE, C1C, CAND], 0.8, [0, 0.2, 0.4, 0.6, 0.8], label_series=2)
    fg = []
    for k, ff in res["fringe"].items():
        fg.append((k.replace("/", "\n"), [ff["page"]["small_share_fringed"], ff["pixel_fringe_share"], ff["ui"]["share_fringed"]]))
    fg.append(("headless\nEdge", [None, (0.806 + 0.8486) / 2, None]))
    bars(d, (652, 412, W - 20, 660), "ClearType share: blue small page strips (C2 test), green ink pixels, gray UI strips",
         fg, [CAND, STORED, BASE], 1.0, [0, 0.25, 0.5, 0.75, 1.0], label_series=0)
    t = res["timing"]["all"]
    tg = [(f"{lab}", [t[c][m] for c in CANDS]) for lab, m in (("p50 ms", "ms_p50"), ("p95 ms", "ms_p95"))]
    for k, dd in res["timing"].items():
        if k != "all":
            tg.append((f"p95\n{k}", [dd[c]["ms_p95"] for c in CANDS]))
    bars(d, (20, 672, 900, H - 20), "OCR wall time per live call, ms (TIMING pages, warm-up + repeats)", tg, [BASE, C1C, CAND], 300,
         [0, 100, 200, 300], label_series=2)
    x, y = 912, 672
    d.rectangle((x, y, W - 20, H - 20), fill=SURFACE)
    d.text((x + 12, y + 8), "Budget ratios on live captures (vs baseline)", font=f(15, True), fill=INK)
    rows = [("C2 CPU per live call", t["C2"]["cpu_ratio"], "<= 2.0"), ("C2 p95 live call, +ms", t["C2"]["dms_p95"], "<= +50"),
            ("C1 CPU per live call", t["C1"]["cpu_ratio"], "<= 2.0"), ("C1 p95 live call, +ms", t["C1"]["dms_p95"], "<= +50")]
    orders = res["c2_order"]
    rgb = sum(o["rgb"] for o in orders.values())
    bgr = sum(o["bgr"] for o in orders.values())
    for i, (lab, v, budget) in enumerate(rows):
        yy = y + 38 + i * 31
        d.text((x + 12, yy), lab, font=f(14), fill=INK2)
        d.text((x + 300, yy), f"{v:.2f}", font=f(18, True), fill=INK)
        d.text((x + 400, yy + 2), budget, font=f(13), fill=MUTED)
    d.text((x + 12, y + 38 + 4 * 31), f"C2 order vote: RGB {rgb} / BGR {bgr} calls", font=f(14), fill=INK2)
    _ = plot
    img.save(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=9)
    ap.add_argument("--sets", default="")
    ap.add_argument("--plot-only", action="store_true", help="redraw side.png from results.json")
    a = ap.parse_args()
    if a.plot_only:
        side(json.loads((LIVE / "results.json").read_text(encoding="utf-8")), LIVE / "side.png")
        return
    log = lambda s: print(s, flush=True)  # noqa: E731
    metas = [json.loads(p.read_text(encoding="utf-8")) for p in sorted((LIVE / "captures").glob("*.json"))]
    if a.sets:
        metas = [m for m in metas if f"{m['browser']}-{m['monitor']}" in a.sets.split(",")]
    specs = {s["id"]: s for s in corpus_mod.page_specs()}
    cands = load_cands()
    recs, strips, calls = [], [], []
    for meta in metas:
        r, s, c = score_set(meta, cands, specs, log)
        recs += r
        strips += s
        calls += c
    tim = timing(metas, cands, specs, a.repeats, log)
    res = summarize(recs, strips, calls, tim)
    res["capture_sets"] = [{k: m[k] for k in ("browser", "monitor", "scale", "window_rect", "client_rect")} | {
        "pages": len(m["pages"]), "viewports_css": sorted({tuple(p["viewport_css"]) for p in m["pages"]}),
        "dpr": sorted({p["dpr"] for p in m["pages"]}), "all_foreground": all(p["foreground"] for p in m["pages"])} for m in metas]
    (LIVE / "results.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    (LIVE / "raw_records.json").write_text(json.dumps({"lines": recs, "strips": strips, "calls": calls, "timing": tim}), encoding="utf-8")
    write_summary(res, metas, LIVE / "summary.md")
    side(res, LIVE / "side.png")
    log("done")


if __name__ == "__main__":
    main()
