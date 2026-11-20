"""fig-misread-count.png: misread tokens search and Ask can return, today vs P1 vs P2 (counts only)."""
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

STUDY = Path(sys.argv[1])
OUT = Path(sys.argv[2])
RUNS = ("smoke", "standard", "day")
p1 = json.loads((STUDY / "P1-line-store-lossless/details.json").read_text(encoding="utf-8"))
v2 = {k: json.loads((STUDY / f"P2-line-store-consensus/v2_{k}.json").read_text(encoding="utf-8")) for k in ("baseline", "p1", "p2")}
a = {"today": [p1[r]["p1"]["tnoise"]["occ_bad"][1] for r in RUNS], "P1": [p1[r]["p1"]["tnoise"]["occ_bad"][0] for r in RUNS]}
b = {"today": [v2["baseline"][r]["noise_occ_n"] for r in RUNS], "P1": [v2["p1"][r]["noise_occ_n"] for r in RUNS],
     "P2": [v2["p2"][r]["noise_occ_n"] for r in RUNS]}
COL = {"today": "#6b6a64", "P1": "#3987e5", "P2": "#2f9e75"}
LIMIT, INK, MUTED, SURF, PLANE, GRID = "#e0a030", "#f0efec", "#898781", "#1a1a19", "#0d0d0d", "#2c2c2a"

plt.style.use("dark_background")
fig, axes = plt.subplots(1, 2, figsize=(16, 6.2), dpi=100, facecolor=PLANE)
for ax, data, title in ((axes[0], a, "run_p1.py counting (P1 prototype)"), (axes[1], b, "score_v2.py counting (P2 prototype, also scores P1)")):
    ax.set_facecolor(SURF)
    names = list(data)
    w = 0.8 / len(names)
    for i, n in enumerate(names):
        xs = [j + (i - (len(names) - 1) / 2) * w for j in range(3)]
        ax.bar(xs, data[n], width=w - 0.03, color=COL[n], label=n, zorder=3)
        for x, v, t in zip(xs, data[n], data["today"]):
            lab = f"{v:,}" if n == "today" else f"{v:,}\n{v / t:.2f}x"
            ax.text(x, v * 1.12, lab, ha="center", va="bottom", fontsize=9, color=INK)
    for j in range(3):
        half = data["today"][j] / 2
        ax.hlines(half, j - 0.45, j + 0.45, colors=LIMIT, linestyles="--", linewidth=1.5, zorder=4,
                  label="must: half of today" if j == 0 else None)
    ax.set_yscale("log")
    ax.set_ylim(20, 300000)
    ax.set_xticks(range(3), RUNS, color=INK, fontsize=11)
    ax.tick_params(axis="y", colors=MUTED)
    ax.grid(axis="y", color=GRID, zorder=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, color=INK, fontsize=12, loc="left")
    ax.legend(loc="upper left", frameon=False, fontsize=10, ncol=4)
fig.suptitle("Misread tokens search and Ask can return (count, log scale; must <= half of today)", color=INK, fontsize=15, x=0.01, ha="left")
fig.tight_layout()
fig.savefig(OUT, facecolor=PLANE)
print(a, b)
