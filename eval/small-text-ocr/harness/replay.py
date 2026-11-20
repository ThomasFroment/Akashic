"""Replay a candidate on the stored frames of real quality runs. Numbers only.

Rows are chosen exactly like score.m3 (M3.1): app windows with stored text, mapped to a harness
window by hwnd, whose page reported shown truth lines at the frame's time; the font bucket is the
page's font_px times the monitor scale. For each sampled row the stored (WebP q70) monitor frame is
decoded, the window rectangle is cropped out (FrameScan's mapping), and the candidate reads the crop
with ctx.path = "replay". CER uses score's rule; a wrap-tolerant variant also lets a truth line match
2-3 consecutive OCR lines joined (the harness pages wrap long lines, which score's rule counts as errors).
The stored text of the same rows is scored too (that is M3.1 on the sample). Rows whose window is
covered by a window in front of it (> 2 % of its area) are skipped: live OCR read the window's own
capture, the frame shows the composite. Captured pixels and text stay in memory, never printed.
"""

from __future__ import annotations

import bisect
import io
import random
import time
from collections import defaultdict
from urllib.parse import parse_qs, urlsplit

import numpy as np
from PIL import Image

from shared import QUALITY, Ctx, best_line_cer, bucket, engines, hyp_of, joined, score

RUNS = ("20261119-standard-s21-target-1818", "20261119-day-s31-target-0547")
MAX_COVERED = 0.02


def _truth_rows(run_id: str, conn):
    tr = score.Truth(QUALITY / "results" / run_id)
    lines = {}
    for e in tr.pages:
        if e.get("type") == "lines" and "#private" not in e["w"]:
            for ln in e["lines"]:
                lines[ln["id"]] = {**ln, "w": e["w"], "s": e["s"]}
    visible = score.visible_reports(tr)
    report_times = {wid: [t for t, _ in reports] for wid, reports in visible.items()}
    focus_params = {s["id"]: s["params"] for s in tr.scn["steps"] if s["kind"] == "focus"}
    for d in tr.driver:
        if d["action"] == "page_load" and d.get("url"):
            q = {k: v[0] for k, v in parse_qs(urlsplit(d["url"]).query).items()}
            if "s" in q:
                focus_params[q["s"]] = {"lang": q.get("lang"), "theme": q.get("theme"), "font_px": int(q.get("px", 16))}
    rows = conn.execute(
        "SELECT w.id, w.hwnd, f.id, f.ts_ms, f.image_path, w.x0, w.y0, w.x1, w.y1, w.z, w.text, w.text_source "
        "FROM frame_windows w JOIN frames f ON f.id = w.frame_id WHERE w.role = 'app' AND w.text IS NOT NULL ORDER BY f.ts_ms").fetchall()
    out = []
    for wrow, h, fid, ts, path, x0, y0, x1, y1, z, text, src in rows:
        wid = tr.hwnd_to_window.get(h)
        i = bisect.bisect_right(report_times.get(wid, []), ts) - 1
        shown = [lines[x] for x in visible[wid][i][1] if x in lines] if i >= 0 else []
        if not shown or path is None:
            continue
        params = focus_params.get(shown[-1]["s"], {})
        mon = score.monitor_of(tr, wid)
        device_px = params.get("font_px", 16) * mon.get("scale", 1)
        out.append({"fid": fid, "path": path, "rect": (x0, y0, x1, y1), "z": z, "text": text, "src": src,
                    "shown": [ln["text"] for ln in shown], "res": f"{mon.get('w', '?')}x{mon.get('h', '?')}",
                    "scale": mon.get("scale", 1), "bucket": bucket(device_px), "lang": params.get("lang", "?"),
                    "theme": params.get("theme", "?")})
    return out


def _covered(conn, fid: int, rect, z: int) -> float:
    x0, y0, x1, y1 = rect
    w, h = max(1, (x1 - x0) // 8), max(1, (y1 - y0) // 8)
    mask = np.zeros((h, w), dtype=bool)
    for a0, b0, a1, b1 in conn.execute("SELECT x0, y0, x1, y1 FROM frame_windows WHERE frame_id = ? AND z < ?", (fid, z)):
        l, t = max(0, (a0 - x0) // 8), max(0, (b0 - y0) // 8)
        r, b = min(w, -(-(a1 - x0) // 8)), min(h, -(-(b1 - y0) // 8))
        if r > l and b > t:
            mask[t:b, l:r] = True
    return float(mask.mean())


def select(run_id: str, conn, per_res: int, seed: int = 0) -> tuple[list[dict], dict]:
    rows = _truth_rows(run_id, conn)
    stats = {"matched_rows": len(rows), "covered_skipped": 0}
    by_res = defaultdict(list)
    for r in rows:
        by_res[r["res"]].append(r)
    picked = []
    for res, rs in sorted(by_res.items()):
        rng = random.Random(f"{seed}/{run_id}/{res}")
        order = rs[:]
        rng.shuffle(order)
        kept = []
        for r in order:
            if len(kept) >= per_res:
                break
            if _covered(conn, r["fid"], r["rect"], r["z"]) > MAX_COVERED:
                stats["covered_skipped"] += 1
                continue
            kept.append(r)
        picked += kept
    picked.sort(key=lambda r: r["fid"])
    return picked, stats


def _crop(conn, media, r, cache: dict) -> Image.Image:
    fid = r["fid"]
    if cache.get("fid") != fid:
        with Image.open(io.BytesIO(media.read(r["path"]))) as im:
            img = im.convert("RGB")
        ox, oy, mon_w = conn.execute(
            "SELECT lm.x0, lm.y0, lm.x1 - lm.x0 FROM frames f JOIN layout_monitors lm ON lm.monitor_id = f.monitor_id "
            "WHERE f.id = ? AND lm.layout_id = (SELECT id FROM monitor_layouts WHERE start_ms <= f.ts_ms ORDER BY start_ms DESC LIMIT 1)", (fid,)).fetchone()
        cache.update(fid=fid, img=img, origin=(ox, oy), k=img.width / mon_w)
    img, (ox, oy), k = cache["img"], cache["origin"], cache["k"]
    x0, y0, x1, y1 = r["rect"]
    box = (max(0, round((x0 - ox) * k)), max(0, round((y0 - oy) * k)), min(img.width, round((x1 - ox) * k)), min(img.height, round((y1 - oy) * k)))
    return img.crop(box), k


def run(cand, per_res: int = 100, runs=RUNS, log=print) -> list[dict]:
    """One record per replayed row: numbers and split labels only."""
    fr, en = engines()
    out = []
    for run_id in runs:
        _paths, _key, conn, media = score.open_store(run_id)
        try:
            rows, stats = select(run_id, conn, per_res)
            log(f"  replay {run_id}: {stats['matched_rows']} matched rows, {len(rows)} sampled, {stats['covered_skipped']} covered skipped")
            cache: dict = {}
            t0 = time.perf_counter()
            for r in rows:
                crop, k = _crop(conn, media, r, cache)
                ctx = Ctx("replay", r["scale"], fr, en)
                t = time.perf_counter()
                hyp = hyp_of(cand.read(crop, ctx))
                ms = (time.perf_counter() - t) * 1000
                stored = hyp_of(r["text"])
                hj, sj = joined(hyp), joined(stored)
                out.append({"run": run_id, "res": r["res"], "bucket": r["bucket"], "lang": r["lang"], "theme": r["theme"], "src": r["src"],
                            "frame_scale": round(k, 4), "ms": ms,
                            "cer": float(np.mean([best_line_cer(t_, hyp) for t_ in r["shown"]])),
                            "cer_join": float(np.mean([best_line_cer(t_, hj) for t_ in r["shown"]])),
                            "stored_cer": float(np.mean([best_line_cer(t_, stored) for t_ in r["shown"]])),
                            "stored_cer_join": float(np.mean([best_line_cer(t_, sj) for t_ in r["shown"]]))})
            log(f"  replay {run_id}: {time.perf_counter() - t0:.0f} s")
        finally:
            conn.close()
    return out
