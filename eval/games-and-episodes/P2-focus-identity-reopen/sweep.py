"""Sweep P2 over the pulsetime P and the two end modes; write sweep.json and fig-*.png.

    uv run --with matplotlib python eval/games-and-episodes/P2-focus-identity-reopen/sweep.py

Uses the harness's own scoring (harness.score_run, harness.detection) with REPS=1 (timings here
are not the reported ones: metrics.txt from harness.py is). Prints numbers only.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import ge  # noqa: E402
import harness  # noqa: E402

harness.REPS = 1
PS = (60, 300, 900, 3600)
MODES = ("front", "alive")


def agg(results: dict, det: dict) -> dict:
    per = [x for r in results.values() for x in r["rows_per_truth"]]
    st = [x for r in results.values() for x in r["m6_start_err_s"]]
    en = [x for r in results.values() for x in r["m6_end_err_s"]]
    return {
        "rows_per_truth_mean": statistics.mean(per), "rows_per_truth_max": max(per),
        "rows_per_truth_mean_by_run": {k[-4:]: statistics.mean(r["rows_per_truth"]) for k, r in results.items()},
        "m6_start_med_s": statistics.median(st), "m6_start_max_s": max(st),
        "m6_end_med_s": statistics.median(en), "m6_end_max_s": max(en),
        "m6_end_over_15s": sum(1 for x in en if x > 15), "m6_n": len(en),
        "f1_video_min": min(r["f1_video"] for r in results.values()), "f1_game_min": min(r["f1_game"] for r in results.values()),
        "f1_by_run": {k[-4:]: (round(r["f1_video"], 2), round(r["f1_game"], 2)) for k, r in results.items()},
        "title_acc_min": min(r["title_acc"] for r in results.values() if r["title_acc"] is not None),
        "useless_max": max(r["useless"] for r in results.values() if r["useless"] is not None),
        "false_game": det["false_game"], "missed_game": det["missed_game"], "excluded_written": det["excluded_written"],
    }


def main() -> None:
    mod = harness.load_module(HERE / "candidate.py")
    runs = {rid: ge.load(rid) for rid in ge.RUNS}
    det = harness.detection(mod)
    out = {}
    for mode in MODES:
        for p in PS:
            mod.PULSE_S, mod.END_MODE = p, mode
            results = {}
            for rid, run in runs.items():
                res = harness.score_run(run, mod)
                results[rid] = {k: v for k, v in res.items() if not k.startswith("_")}
                results[rid]["truth"] = [(t.process, rid[-4:]) for t in run.games]
            a = agg(results, det)
            out[f"{mode}-{p}"] = {"mode": mode, "pulse_s": p, "aggregate": a, "runs": results}
            print(f"{mode:5} P={p:5}: rows/truth mean {a['rows_per_truth_mean']:.2f} max {a['rows_per_truth_max']}"
                  f" {a['rows_per_truth_mean_by_run']} | start med {a['m6_start_med_s']:.1f} max {a['m6_start_max_s']:.1f}"
                  f" | end med {a['m6_end_med_s']:.1f} max {a['m6_end_max_s']:.1f} over15 {a['m6_end_over_15s']}/{a['m6_n']}"
                  f" | F1 {a['f1_by_run']} | titles {a['title_acc_min']:.2f} | useless {a['useless_max']:.2f}", flush=True)
    ge.write_json(HERE / "sweep.json", out)
    figures(out, runs)


def figures(out: dict, runs: dict) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.style.use("dark_background")
    # rows per truth vs P
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.2))
    for rid in runs:
        for mode, ls in (("front", "-"), ("alive", "--")):
            ys = [statistics.mean(out[f"{mode}-{p}"]["runs"][rid]["rows_per_truth"]) for p in PS]
            mx = [max(out[f"{mode}-{p}"]["runs"][rid]["rows_per_truth"]) for p in PS]
            ax[0].plot(PS, ys, ls, marker="o", label=f"{rid[-4:]} {mode}")
            ax[1].plot(PS, mx, ls, marker="o", label=f"{rid[-4:]} {mode}")
    for a, t in zip(ax, ("rows per truth session: mean", "rows per truth session: max"), strict=True):
        a.set_xscale("log")
        a.set_xticks(PS, [str(p) for p in PS])
        a.axhline(1.5, color="#e05050", lw=1, ls=":")
        a.set_xlabel("pulsetime P (s)")
        a.set_title(t)
        a.legend(fontsize=7)
    ax[0].text(PS[0], 1.55, "must <= 1.5", color="#e05050", fontsize=8)
    fig.tight_layout()
    fig.savefig(HERE / "fig-rows-vs-P.png", dpi=90)
    plt.close(fig)
    # end error per truth vs P
    fig, ax = plt.subplots(1, 2, figsize=(12, 4.6), sharey=True)
    for k, mode in enumerate(MODES):
        for rid in runs:
            games = runs[rid].games
            for i, t in enumerate(games):
                ys = [max(out[f"{mode}-{p}"]["runs"][rid]["m6_end_err_s"][i], 0.05) for p in PS]
                col = "#5aaafa" if t.process == "qualitygame.exe" else "#faaa46"
                ax[k].plot(PS, ys, marker="o", color=col, alpha=0.8, lw=1)
        ax[k].set_xscale("log")
        ax[k].set_yscale("log")
        ax[k].set_xticks(PS, [str(p) for p in PS])
        ax[k].axhline(15, color="#e05050", lw=1, ls=":")
        ax[k].set_xlabel("pulsetime P (s)")
        ax[k].set_title(f"M6.1 end error per truth session, end = {mode}")
    ax[0].set_ylabel("|row end - process exit| (s)")
    ax[0].text(PS[0], 17, "must <= 15 s", color="#e05050", fontsize=8)
    ax[0].plot([], [], color="#5aaafa", label="Quality Game")
    ax[0].plot([], [], color="#faaa46", label="Outer Wilds")
    ax[0].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(HERE / "fig-end-error-vs-P.png", dpi=90)
    plt.close(fig)


if __name__ == "__main__":
    main()
