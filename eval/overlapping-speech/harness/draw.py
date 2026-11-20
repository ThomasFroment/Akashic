"""side.png with Pillow (no matplotlib in the repo venv): the same six panels for every candidate,
the baseline in grey beside the candidate in blue."""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

BG, FG, DIM, GRID = (18, 18, 22), (225, 225, 230), (120, 120, 130), (50, 50, 58)
BASE, CAND, BAD, WARN = (120, 120, 135), (90, 170, 255), (240, 95, 95), (240, 180, 70)
STACK = [(240, 95, 95), (240, 150, 90), (240, 200, 90), (150, 120, 220), (110, 190, 140), (90, 170, 255), (180, 180, 190)]
W, H = 1500, 960


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


F, FS, FT = font(15), font(12), font(19)


def axis(d, box, vmax, ticks, fmt="{:.2f}"):
    x0, y0, x1, y1 = box
    for t in ticks:
        y = y1 - (y1 - y0) * t / vmax
        d.line((x0, y, x1, y), fill=GRID)
        d.text((x0 - 40, y - 7), fmt.format(t), fill=DIM, font=FS)


def bars(d, box, title, labels, series, vmax, ticks, limits=None, fmt="{:.2f}"):
    """series: [(color, [values])]; limits: {label index: (value, text)} drawn as red ticks."""
    x0, y0, x1, y1 = box
    d.text((x0 - 40, y0 - 26), title, fill=FG, font=F)
    axis(d, box, vmax, ticks, fmt)
    n, k = len(labels), len(series)
    gw = (x1 - x0) / max(n, 1)
    bw = gw * 0.75 / k
    for i, lab in enumerate(labels):
        gx = x0 + i * gw + gw * 0.125
        for j, (col, vals) in enumerate(series):
            v = vals[i]
            if v is None:
                continue
            h = (y1 - y0) * min(v, vmax) / vmax
            d.rectangle((gx + j * bw, y1 - h, gx + (j + 1) * bw - 2, y1), fill=col)
            d.text((gx + j * bw, y1 - h - 13), fmt.format(v), fill=FG, font=FS)
        d.text((gx, y1 + 4), lab, fill=DIM, font=FS)
        if limits and i in limits:
            lv, lt = limits[i]
            y = y1 - (y1 - y0) * min(lv, vmax) / vmax
            d.line((gx - 3, y, gx + gw * 0.75 + 3, y), fill=BAD, width=2)


def lines(d, box, title, xs, series, vmax, ticks, xfmt="{:g}"):
    """series: [(color, dashed, label, [values])]"""
    x0, y0, x1, y1 = box
    d.text((x0 - 40, y0 - 26), title, fill=FG, font=F)
    axis(d, box, vmax, ticks)
    px = lambda i: x0 + (x1 - x0) * i / max(1, len(xs) - 1)
    for i, x in enumerate(xs):
        d.text((px(i) - 8, y1 + 4), xfmt.format(x), fill=DIM, font=FS)
    ly = y0
    for col, dashed, label, vals in series:
        pts = [(px(i), y1 - (y1 - y0) * min(v, vmax) / vmax) for i, v in enumerate(vals) if v is not None]
        for (a, b), (c, e) in zip(pts, pts[1:], strict=False):
            if dashed:
                for t in range(0, 10, 2):
                    d.line((a + (c - a) * t / 10, b + (e - b) * t / 10, a + (c - a) * (t + 1) / 10, b + (e - b) * (t + 1) / 10), fill=col, width=2)
            else:
                d.line((a, b, c, e), fill=col, width=3)
        for p in pts:
            d.ellipse((p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3), fill=col)
        d.text((x1 - 170, ly), label, fill=col, font=FS)
        ly += 15


def side(cand: dict, base: dict | None, path) -> None:
    base = base or cand
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((20, 12), f"overlapping speech: {cand.get('candidate', '?')} (blue) vs baseline (grey)", fill=FG, font=FT)
    g = lambda dd, run, *keys: _get(dd, ["runs", run, *keys])
    # 1. M9.1
    labs, bv, cv, lim = [], [], [], {}
    for run in ("std", "day"):
        for grp in ("overlap", "no_overlap", "en", "fr", "mixed"):
            if g(cand, run, "M9.1", grp, "wer") is None and g(base, run, "M9.1", grp, "wer") is None:
                continue
            if grp == "overlap":
                lim[len(labs)] = (0.25, "must")
            labs.append(f"{run} {grp.replace('no_overlap', 'cln').replace('overlap', 'ovl')}")
            bv.append(g(base, run, "M9.1", grp, "wer"))
            cv.append(g(cand, run, "M9.1", grp, "wer"))
    bars(d, (70, 80, 720, 300), "M9.1 WER per group (red: must <= 0.25 on overlap)", labs, [(BASE, bv), (CAND, cv)], 0.8, [0, 0.2, 0.4, 0.6, 0.8], lim)
    # 2. DER and authors
    labs = ["DER std", "DER day"]
    bars(d, (810, 80, 1060, 300), "M8.2 DER (must not rise)", labs,
         [(BASE, [g(base, r, "M8.2", "der") for r in ("std", "day")]), (CAND, [g(cand, r, "M8.2", "der") for r in ("std", "day")])], 0.5, [0, 0.25, 0.5])
    y = 80
    d.text((1120, y - 26), "M8.3 authors  base -> cand", fill=FG, font=F)
    for r in ("std", "day"):
        for k in ("correct", "wrong", "unknown"):
            d.text((1120, y), f"{r} {k}: {g(base, r, 'M8.3', k)} -> {g(cand, r, 'M8.3', k)}", fill=FG if k != "wrong" else WARN, font=F)
            y += 20
    for r in ("std", "day"):
        d.text((1120, y), f"{r} leak s: {_f(g(base, r, 'M7.1', 'leak_seconds_other_than_self'))} -> {_f(g(cand, r, 'M7.1', 'leak_seconds_other_than_self'))}", fill=FG, font=F)
        y += 20
    d.text((1120, y), f"excluded s: {_f(sum(v.get('excluded_s', 0) for v in base.get('runs', {}).values()))} -> "
                      f"{_f(sum(v.get('excluded_s', 0) for v in cand.get('runs', {}).values()))}", fill=FG, font=F)
    # 3. overlap error types (stacked, errors per overlap ref word)
    cats = ["D:overlap_quieter", "D:overlap_louder", "D:clean", "D:uncovered", "D:boundary", "S", "I"]
    x0, y0, x1, y1 = 70, 380, 720, 600
    d.text((x0 - 40, y0 - 26), "M9.1 overlap groups: errors per ref word by type", fill=FG, font=F)
    axis(d, (x0, y0, x1, y1), 0.8, [0, 0.2, 0.4, 0.6, 0.8])
    cols = [("base std", base, "std"), ("cand std", cand, "std"), ("base day", base, "day"), ("cand day", cand, "day")]
    bw = (x1 - x0) / len(cols)
    for i, (lab, dd, run) in enumerate(cols):
        et = g(dd, run, "overlap_error_types") or {}
        ref = g(dd, run, "M9.1", "overlap", "ref_words") or 1
        acc = 0.0
        for c, colr in zip(cats, STACK, strict=True):
            v = sum(n for k, n in et.items() if (k == c or (c in ("S", "I") and k.split(":")[0] == c))) / ref
            h0, h1 = y1 - (y1 - y0) * min(acc, 0.8) / 0.8, y1 - (y1 - y0) * min(acc + v, 0.8) / 0.8
            d.rectangle((x0 + i * bw + 15, h1, x0 + (i + 1) * bw - 15, h0), fill=colr)
            acc += v
        d.text((x0 + i * bw + 15, y1 + 4), lab, fill=DIM, font=FS)
    for j, (c, colr) in enumerate(zip(cats, STACK, strict=True)):
        d.text((x1 + 5, y0 + j * 16), c, fill=colr, font=FS)
    # 4. synthetic: quiet / loud talker WER vs overlap
    sb, sc = _get(base, ["synthetic", "summary"]) or {}, _get(cand, ["synthetic", "summary"]) or {}
    xs = ["0.0", "0.25", "0.5", "0.75", "1.0"]
    ser = []
    for col, dash, name, sm in ((BASE, True, "base", sb), (CAND, False, "cand", sc)):
        if sm:
            ser.append((col, dash, f"{name} quieter talker", [sm.get("talker_any_wer_quiet_by_overlap", {}).get(x) for x in xs]))
            ser.append(((col[0] // 2 + 60, col[1] // 2 + 60, col[2] // 2 + 60), dash, f"{name} louder talker", [sm.get("talker_any_wer_loud_by_overlap", {}).get(x) for x in xs]))
    lines(d, (900, 380, 1440, 600), "synthetic: talker WER (words found anywhere) vs overlap ratio", [0, 0.25, 0.5, 0.75, 1.0], ser, 1.0, [0, 0.25, 0.5, 0.75, 1.0])
    # 5. synthetic cpWER by SIR / process layout / bed
    labs = ["SIR -6", "SIR 0", "SIR +6", "2 procs", "1 proc", "no bed", "music", "game", "solo", "DER"]
    pick = lambda sm: ([sm.get("cpwer_by_sir", {}).get(k) for k in ("-6", "0", "6")] + [sm.get("cpwer_by_procs", {}).get(k) for k in ("two", "one")]
                       + [sm.get("cpwer_by_bed", {}).get(k) for k in ("none", "music", "game")] + [sm.get("wer_solo"), sm.get("der")]) if sm else [None] * 10
    bars(d, (70, 680, 900, 900), "synthetic cpWER by SIR (A over B), process layout, bed; solo WER; DER", labs, [(BASE, pick(sb)), (CAND, pick(sc))], 1.0, [0, 0.25, 0.5, 0.75, 1.0])
    # 6. cost against budget
    cb, cc = base.get("cost") or {}, cand.get("cost") or {}
    gx = (cc.get("gpu_s_per_h") or 0) / cb["gpu_s_per_h"] if cb.get("gpu_s_per_h") else None
    vr = [None if not x.get("vram_peak_mib") else x["vram_peak_mib"] / 1024 for x in (cb, cc)]
    bars(d, (1000, 680, 1180, 900), "GPU x baseline", ["gpu"], [(BASE, [1.0 if cb else None]), (CAND, [gx])], 2.0, [0, 0.5, 1.0, 1.5, 2.0], {0: (1.3, "")})
    bars(d, (1240, 680, 1440, 900), "VRAM GB / cap CPU %", ["vram", "cpu%"], [(BASE, [vr[0], cb.get("capture_cpu_pct")]), (CAND, [vr[1], cc.get("capture_cpu_pct")])], 8.0,
         [0, 2, 4, 6, 8], {0: ((vr[0] or 0) + 1.5, ""), 1: ((cb.get("capture_cpu_pct") or 0) + 5, "")})
    d.text((20, H - 26), "uses simulated per-process streams: " + ("yes" if cand.get("uses_sim") else "no"), fill=DIM, font=FS)
    img.save(path)


def _f(v):
    return "-" if v is None else f"{v:.1f}"


def _get(d, keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d
