"""Seam wrapper vs its base: per-subset deltas, the switch_early firsts, deferral share, seam
outcomes and failure classes; draws out/R2seam_<base>_t0/seam_vs_base.png.

    ..\\.venv\\Scripts\\python.exe candidates\\R2seam_compare.py C2_calib
"""

import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import FIXTURE, OUT  # noqa: E402

SUBS = ["solo", "solo_10k", "solo_10_0", "cont_first", "cont_second", "cont_same", "cont_switch",
        "cont_switch_early", "similar", "mixed", "impostor"]  # fmt: skip


def load(name: str):
    p = OUT / name / "details.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def f(v) -> str:
    return "  na  " if v is None else f"{v * 100:6.2f}"


def main() -> None:
    base = sys.argv[1]
    index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
    mode = {e["id"]: e.get("cont_mode") for e in index["segments"]}
    for e in index["segments"]:  # a second's mode = its first's
        if e["kind"] == "cont_second":
            mode[e["id"]] = mode[e["continues"]]
    runs = {"base": load(base)}
    for v in ("t0_norule", "t1_norule", "t0_i1", "t1_i1", "t0", "t1"):
        d = load(f"R2seam_{base}_{v}")
        if d:
            runs[v] = d
    names = list(runs)
    print(f"== {base}: authorship / wrong (%) per subset; deltas vs base in pts")
    print(f"{'subset':20s}" + "".join(f"{n:>22s}" for n in names))
    for s in ["ALL"] + SUBS:
        row = f"{s:20s}"
        for n in names:
            sm = runs[n]["summary"]
            a, w = (sm["authorship"][0], sm["wrong"][0]) if s == "ALL" else (
                sm["subsets"][s]["authorship"][0], sm["subsets"][s]["wrong"][0])
            b = runs["base"]["summary"]
            ba, bw = (b["authorship"][0], b["wrong"][0]) if s == "ALL" else (
                b["subsets"][s]["authorship"][0], b["subsets"][s]["wrong"][0])
            da = "" if n == "base" or a is None else f"{(a - ba) * 100:+.1f}"
            dw = "" if n == "base" or w is None else f"{(w - bw) * 100:+.1f}"
            row += f"  {f(a)}{da:>5s}/{f(w)}{dw:>5s}"
        print(row)
    print("\n== cont_first by mode: n, authored, wrong, NULL (count)")
    for n in names:
        rows = [r for r in runs[n]["segments"] if r["kind"] == "cont_first"]
        out = []
        for m in ("same", "switch", "switch_early"):
            rr = [r for r in rows if mode[r["id"]] == m]
            out.append(f"{m}: n={len(rr)} auth={sum(r['authored'] for r in rr)} "
                       f"wrong={sum(r['wrong'] for r in rr)} null={sum(r['author'] is None for r in rr)}")
        rows2 = [r for r in runs[n]["segments"] if r["kind"] == "cont_second"]
        out2 = []
        for m in ("same", "switch", "switch_early"):
            rr = [r for r in rows2 if mode[r["id"]] == m]
            out2.append(f"{m}: auth={sum(r['authored'] for r in rr)}/{sum(r['expected'] is not None for r in rr)} "
                        f"wrong={sum(r['wrong'] for r in rr)}")
        print(f"  {n:10s} first  " + " | ".join(out))
        print(f"  {'':10s} second " + " | ".join(out2))
    for n in names:
        d = runs[n]
        if "extras" not in d or not d["extras"].get("deferred"):
            continue
        segs = d["segments"]
        dfr = [r for r in segs if r.get("deferred")]
        authored_any = [r for r in segs if r["author"] is not None or r.get("deferred")]
        print(f"\n== {n}: deferred {len(dfr)} of {len(segs)} segments ({len(dfr) / len(segs):.1%}); "
              f"of segments with a (provisional) author {len(dfr) / len(authored_any):.1%}")
        seam = d["extras"]["seam"]
        tab = Counter()
        for r in dfr:
            why = seam.get(str(r["id"]), "?").split(":")[0]
            prov_ok = r.get("provisional") is not None and r.get("provisional") == r["single"]
            tab[(mode[r["id"]], why, "prov_right" if prov_ok else "prov_wrong",
                 "kept" if r["author"] is not None else "null")] += 1
        for k, v in sorted(tab.items()):
            print(f"  {k}: {v}")
        short = [seam[str(r["id"])] for r in dfr if seam.get(str(r["id"]), "").startswith("short")]
        if short:
            print("  short_evidence ms:", sorted(int(s.split(":")[1]) for s in short))
    t0 = runs.get("t0")
    if t0:
        figure(base, runs, mode)


def figure(base: str, runs: dict, mode: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    BG, SURF, INK, INK2, MUTED, GRID, AXIS = (
        "#0d0d0d", "#1a1a19", "#f0efec", "#c3c2b7", "#898781", "#2c2c2a", "#383835")  # fmt: skip
    series = [("seam, trimmed unknown (t0)", "t0", "#3987e5"), ("base", "base", "#d95926"),
              ("seam, trimmed=1 (t1)", "t1", "#199e70"), ("lead as body, no seam (control)", "t0_norule", "#c98500")]  # fmt: skip
    series = [s for s in series if s[1] in runs]
    plt.rcParams.update({
        "figure.facecolor": BG, "axes.facecolor": SURF, "axes.edgecolor": AXIS,
        "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "font.size": 9,
        "axes.spines.top": False, "axes.axisbelow": True, "axes.spines.right": False, "legend.frameon": False,
    })  # fmt: skip
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.8), dpi=110)

    def per_mode(d, kind, key):
        vals = []
        for m in ("same", "switch", "switch_early"):
            rr = [r for r in d["segments"] if r["kind"] == kind and mode[r["id"]] == m]
            if key == "authored":
                rr = [r for r in rr if r["expected"] is not None]
            vals.append(np.mean([r[key] for r in rr]) if rr else None)
        return vals

    panels = [
        (ax[0], "Authorship, continuation pairs",
         [("first same", "cont_first", 0), ("first switch", "cont_first", 1),
          ("second same", "cont_second", 0), ("second switch", "cont_second", 1),
          ("second sw_early", "cont_second", 2)], "authored", 1.0),
        (ax[1], "Wrong-author rate, continuation pairs",
         [("first same", "cont_first", 0), ("first switch", "cont_first", 1),
          ("first sw_early", "cont_first", 2), ("second switch", "cont_second", 1),
          ("second sw_early", "cont_second", 2)], "wrong", None),
    ]  # fmt: skip
    for a, title, cols, key, ylim in panels:
        w = 0.8 / len(series)
        allv = []
        for j, (label, k, color) in enumerate(series):
            vals = [per_mode(runs[k], kind, key)[mi] for _, kind, mi in cols]
            allv += [v or 0 for v in vals]
            xs = np.arange(len(cols)) + (j - (len(series) - 1) / 2) * w
            a.bar(xs, [v or 0 for v in vals], width=w - 0.02, color=color, label=label,
                  edgecolor=SURF, linewidth=2)  # fmt: skip
            for x, v in zip(xs, vals, strict=True):
                a.text(x, (v or 0) + 0.01, "na" if v is None else f"{v:.0%}", ha="center",
                       va="bottom", fontsize=7, color=INK2)  # fmt: skip
        top = ylim or max(0.1, max(allv) * 1.25)
        a.set_ylim(0, top * 1.15)
        a.set_xticks(range(len(cols)), [c[0] for c in cols], rotation=20, ha="right")
        a.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        a.set_title(title, loc="left", color=INK)
        a.grid(axis="x", visible=False)
        a.legend(loc="upper right", labelcolor=INK2, fontsize=7)
    fig.suptitle(f"QA-R2-C3 seam reconciliation over {base}: 40 same / 50 switch / 30 switch_early pairs",
                 color=INK, x=0.01, ha="left", fontsize=11)  # fmt: skip
    fig.tight_layout()
    p = OUT / f"R2seam_{base}_t0" / "seam_vs_base.png"
    fig.savefig(p, facecolor=BG)
    plt.close(fig)
    print("figure:", p)


if __name__ == "__main__":
    main()
