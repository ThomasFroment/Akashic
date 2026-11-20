"""fig-*.png for what side.png hides, from details.json (numbers only). Same Pillow style as the
harness plot: gray = today, blue = P1, amber = limit.

    uv run python .../P1-line-store-lossless/figs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import plot as PL  # noqa: E402

D = json.loads((HERE / "details.json").read_text(encoding="utf-8"))
B = json.loads((HERE.parent / "baseline" / "details.json").read_text(encoding="utf-8"))
RUNS = [("s", "smoke"), ("st", "standard"), ("d", "day")]


def canvas(title: str, n: int, w: int = 1600, h: int = 470):
    img = Image.new("RGB", (w, h), PL.PLANE)
    d = ImageDraw.Draw(img)
    d.text((16, 10), title, font=PL.f(20, True), fill=PL.INK)
    x = 16 + d.textlength(title, font=PL.f(20, True)) + 30
    for lab, col in (("today", PL.BASE), ("P1", PL.CAND), ("limit", PL.LIMIT)):
        d.rounded_rectangle((x, 17, x + 12, 29), radius=3, fill=col)
        d.text((x + 18, 13), lab, font=PL.f(13), fill=PL.INK2)
        x += 40 + d.textlength(lab, font=PL.f(13))
    pw = (w - 16 * (n + 1)) / n
    boxes = [(16 + i * (pw + 16), 50, 16 + i * (pw + 16) + pw, h - 16) for i in range(n)]
    return img, d, boxes


def noise_fig() -> None:
    img, d, bx = canvas("Misread tokens (round-2 noise) P1 vs today; stronger garbage settings on the right (must <= 0.5x)", 4)
    for box, (name, key) in zip(bx[:3], [("per document: distinct tokens once", "doc"), ("Ask evidence of top-5 hits", "ask"),
                                         ("indexed token occurrences", "occ")], strict=True):
        groups = [(s, 1.0, D[k]["p1"]["tnoise"][key + "_x"]) for s, k in RUNS]
        PL.panel(d, box, name, groups, 1.6, [0, 0.5, 1, 1.5], 0.5, "{:.2f}", "x")
    sw = json.loads((HERE / "sweep.json").read_text(encoding="utf-8"))
    short = {"token guard off": "guard off", "minority readings": "minority", "all guards off, lm 5%": "all off"}
    groups = [(f"final {100 * D['day']['drop']['all']['rate']:.1f}%", 1.0, D["day"]["p1"]["tnoise"]["ask_x"])]
    for n, v in sw.items():
        groups.append((f"{short.get(n, n)} {100 * v['day']['drop']:.1f}%", 1.0, v["day"]["mis_ask_x"]))
    PL.panel(d, bx[3], "day, Ask reading (content lost %)", groups, 1.6, [0, 0.5, 1, 1.5], 0.5, "{:.2f}", "x")
    img.save(HERE / "fig-noise-views.png")


def content_fig() -> None:
    img, d, bx = canvas("Harness content lines dropped or down-ranked, by cause (counts; must <= 1 % of captured)", 3)
    for box, (s, k) in zip(bx, RUNS, strict=True):
        cd = D[k]["p1"]["content_down"]
        groups = [(f"{c} ({cd['captured']} cap.)" if c == "chrome" else c, None, float(cd.get(c, 0))) for c in ("chrome", "garbage", "other")]
        for key, v in sorted(cd["by_app"].items()):
            groups.append((key.replace("|", " "), None, float(v)))
        PL.panel(d, box, f"{k}: by cause, then app|cause", groups, max(10.0, 0.01 * cd["captured"] * 1.2),
                 [0, 0.01 * cd["captured"]], 0.01 * cd["captured"], "{:.0f}")
    img.save(HERE / "fig-content-down.png")


def m32_fig() -> None:
    img, d, bx = canvas("M3.2 by app type: today vs P1 FTS documents (lines once per page-day); physically stored rows = 0", 3)
    for box, (s, k) in zip(bx, RUNS, strict=True):
        by = D[k]["p1"]["m32_by_app"]
        apps = sorted(by, key=lambda a: -by[a]["chars"])
        diag = json.loads((HERE.parent / "diagnosis" / f"{B[k]['run']}.json").read_text(encoding="utf-8")) if (HERE.parent / "diagnosis" / f"{B[k]['run']}.json").is_file() else {}
        groups = []
        for a in apps:  # today: exact repeats (diagnosis classes a + b_exact = M3.2 share)
            cl = diag.get("classes", {}).get(f"app={a}")
            groups.append((a[:8], cl["a"] + cl["b_exact"] if cl else None, by[a]["m32"]))
        PL.panel(d, box, f"{k} (today all apps {B[k]['m32']:.2f}, P1 {D[k]['m32']:.3f})", groups, 1.0, [0, 0.3, 0.6, 1.0], 0.3, "{:.2f}")
    img.save(HERE / "fig-m32-by-app.png")


def unknown_fig() -> None:
    img, d, bx = canvas("Unknown tokens P1 still indexes: where they are (share; harness labels, harness 'garbled')", 3)
    for box, (s, k) in zip(bx, RUNS, strict=True):
        comp = D[k]["p1"]["unknown_composition"]
        tot = sum(comp.values()) or 1
        agg = {}
        for key, v in comp.items():
            parts = dict(p.split("=") for p in key.split("|"))
            name = f"{parts['line']} {'garb' if parts['garbled'] == 'True' else 'word'}"
            agg[name] = agg.get(name, 0) + v
        groups = [(n, None, v / tot) for n, v in sorted(agg.items(), key=lambda x: -x[1])]
        PL.panel(d, box, f"{k}: {tot} unknown tokens", groups, 0.6, [0, 0.2, 0.4, 0.6], None, "{:.2f}")
    img.save(HERE / "fig-unknown-left.png")


if __name__ == "__main__":
    noise_fig()
    content_fig()
    m32_fig()
    unknown_fig()
    print("ok")
