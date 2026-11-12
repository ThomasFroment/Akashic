"""Evaluate a time-window candidate against phrases.jsonl (and the retrieval fixture).

A candidate is a Python file exposing

    resolve_question(question: str, now: datetime, model_time_text: str | None)
        -> tuple[datetime, datetime] | None

``now`` is timezone-aware (Europe/Paris). The returned [start, end) may be aware or naive
(naive = local time in now's zone). Two modes are run:

- rules: model_time_text=None (no language model);
- model: model_time_text = the main anchor's time words copied exactly from the question (what
  the current planner prompt asks the model to do), or "" when the question names no time.

Optionally the candidate may also expose ``select_lines(conn, start_ms, end_ms, plan_kinds,
people=())`` and/or ``select_episodes(...)`` (same signature); see retrieval.py.

Run:
    python harness.py                         # baseline only -> out/baseline/
    python harness.py --candidate path/to/cand.py --name C1   # -> out/C1/
    python harness.py --phrases heldout2.jsonl --no-retrieval  # -> out/baseline_heldout2/
    python harness.py --phrases heldout2.jsonl --candidate path/to/cand.py --name C1 --no-retrieval
                                              # -> out/C1_heldout2/

Per case: covers = window contains the gold span with 30 min tolerance on each edge; tight =
window length <= 2x gold length; pass = covers and tight (gold null: pass = no window).
future = window start >= now; false filter = any window for a null gold.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import timeit
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Paris")
TOL = timedelta(minutes=30)
TARGET = 0.95


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PHRASES = HERE / "phrases.jsonl"  # --phrases replaces it (e.g. heldout2.jsonl)


def phrases() -> list[dict]:
    with PHRASES.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def _aware(x: datetime, tz) -> datetime:  # noqa: ANN001
    return x if x.tzinfo is not None else x.replace(tzinfo=tz)


def score_case(window, gold, now: datetime) -> dict:  # noqa: ANN001
    if gold is None:
        return {"covers": None, "tight": None, "future": window is not None and window[0] >= now,
                "false_filter": window is not None, "pass": window is None}  # fmt: skip
    if window is None:
        return {"covers": False, "tight": False, "future": False, "false_filter": False,
                "pass": False}  # fmt: skip
    ws, we = window
    gs, ge = gold
    covers = ws <= gs + TOL and we >= ge - TOL
    tight = (we - ws) <= 2 * (ge - gs)
    return {"covers": covers, "tight": tight, "future": ws >= now, "false_filter": False,
            "pass": covers and tight}  # fmt: skip


def evaluate(module: ModuleType) -> list[dict]:
    rows: list[dict] = []
    for rec in phrases():
        for lab in rec["labels"]:
            now = datetime.fromisoformat(lab["now"])
            gold = None if lab["gold"] is None else tuple(map(datetime.fromisoformat, lab["gold"]))
            for mode in ("rules", "model"):
                mtt = None if mode == "rules" else (rec["main_time_text"] or "")
                error = None
                try:
                    out = module.resolve_question(rec["question"], now, mtt)
                    window = None if out is None else (_aware(out[0], TZ), _aware(out[1], TZ))
                except Exception as exc:  # noqa: BLE001 - a crash is a failed case
                    window, error = None, type(exc).__name__
                s = score_case(window, gold, now)
                rows.append(
                    {
                        "id": rec["id"], "question": rec["question"], "lang": rec["lang"],
                        "category": rec["category"], "split": rec["split"],
                        "connector": rec["connector"], "ref_date": lab["ref_date"],
                        "now": lab["now_label"], "mode": mode, "model_time_text": mtt,
                        "gold": lab["gold"],
                        "window": None if window is None else [w.isoformat() for w in window],
                        "error": error, **s,
                    }
                )  # fmt: skip
    return rows


def timing(module: ModuleType) -> dict:
    per_call: list[float] = []
    for rec in phrases():
        for lab in rec["labels"][:5]:
            now = datetime.fromisoformat(lab["now"])
            q = rec["question"]
            best = min(
                timeit.repeat(lambda q=q, now=now: module.resolve_question(q, now, None),
                              number=5, repeat=3)
            )  # fmt: skip
            per_call.append(best / 5 * 1e6)
    per_call.sort()
    return {
        "p50_us": round(statistics.median(per_call), 1),
        "p99_us": round(per_call[min(len(per_call) - 1, int(0.99 * len(per_call)))], 1),
        "max_us": round(per_call[-1], 1),
        "n": len(per_call),
    }


def rate(rows: list[dict]) -> float:
    return round(sum(r["pass"] for r in rows) / len(rows), 4) if rows else float("nan")


def summarize(rows: list[dict]) -> dict:
    out: dict = {}
    for mode in ("rules", "model"):
        m = [r for r in rows if r["mode"] == mode]
        timed = [r for r in m if r["gold"] is not None]
        out[mode] = {
            "pass_rate": rate(m),
            "pass_rate_timed": rate(timed),
            "covers_rate_timed": round(sum(r["covers"] for r in timed) / len(timed), 4),
            "tight_rate_timed": round(sum(r["tight"] for r in timed) / len(timed), 4),
            "future_windows": sum(r["future"] for r in m),
            "false_filters": sum(r["false_filter"] for r in m),
            "errors": sum(r["error"] is not None for r in m),
            "n_cases": len(m),
        }
        for key in ("category", "now", "lang", "split", "connector"):
            groups: dict[str, list[dict]] = {}
            for r in m:
                if key == "connector" and r["category"] != "two_anchor":
                    continue
                groups.setdefault(str(r[key]), []).append(r)
            out[mode][f"by_{key}"] = {k: {"pass": rate(v), "n": len(v)} for k, v in sorted(groups.items())}
    return out


def plot(summary: dict, base: dict, name: str, path: Path) -> None:
    import matplotlib as mpl

    mpl.use("Agg")
    import matplotlib.pyplot as plt

    surface, ink, muted, grid = "#1a1a19", "#f0efec", "#898781", "#2c2c2a"
    c_base, c_cand = "#d95926", "#3987e5"
    cats = ["ALL", *sorted(base["rules"]["by_category"])]

    def vals(s: dict) -> list[float]:
        return [s["rules"]["pass_rate"]] + [s["rules"]["by_category"][c]["pass"] for c in cats[1:]]

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(13, 7.5), gridspec_kw={"width_ratios": [3, 2]}, facecolor=surface
    )
    for ax in (ax1, ax2):
        ax.set_facecolor(surface)
        ax.tick_params(colors=muted, labelcolor=ink)
        for spine in ax.spines.values():
            spine.set_visible(False)
    y = range(len(cats))
    h = 0.38
    ax1.barh([i - h / 2 for i in y], vals(base), height=h - 0.04, color=c_base, label="baseline (today)")
    ax1.barh([i + h / 2 for i in y], vals(summary), height=h - 0.04, color=c_cand, label=name)
    for i, (b, c) in enumerate(zip(vals(base), vals(summary), strict=True)):
        ax1.text(c + 0.01, i + h / 2, f"{c:.0%}", va="center", color=ink, fontsize=8)
        ax1.text(b + 0.01, i - h / 2, f"{b:.0%}", va="center", color=muted, fontsize=8)
    ax1.axvline(TARGET, color=ink, lw=1, ls="--")
    ax1.text(TARGET, -0.9, "95%", color=ink, ha="center", fontsize=8)
    ax1.set_yticks(list(y), cats)
    ax1.invert_yaxis()
    ax1.set_xlim(0, 1.12)
    ax1.xaxis.grid(True, color=grid, lw=0.6)
    ax1.set_axisbelow(True)
    ax1.set_title("covers & tight, rules mode (no model)", color=ink, loc="left", fontsize=11)
    ax1.legend(loc="lower right", facecolor=surface, edgecolor=grid, labelcolor=ink, fontsize=9)

    groups = ["future\n(rules)", "false filter\n(rules)", "future\n(model)", "false filter\n(model)"]

    def counts(s: dict) -> list[int]:
        return [s["rules"]["future_windows"], s["rules"]["false_filters"],
                s["model"]["future_windows"], s["model"]["false_filters"]]  # fmt: skip

    x = range(len(groups))
    ax2.bar([i - h / 2 for i in x], counts(base), width=h - 0.04, color=c_base)
    ax2.bar([i + h / 2 for i in x], counts(summary), width=h - 0.04, color=c_cand)
    for i, (b, c) in enumerate(zip(counts(base), counts(summary), strict=True)):
        ax2.text(i - h / 2, b, str(b), ha="center", va="bottom", color=muted, fontsize=8)
        ax2.text(i + h / 2, c, str(c), ha="center", va="bottom", color=ink, fontsize=8)
    ax2.set_xticks(list(x), groups)
    ax2.yaxis.grid(True, color=grid, lw=0.6)
    ax2.set_axisbelow(True)
    ax2.set_title("case counts (target 0)", color=ink, loc="left", fontsize=11)
    fig.suptitle(
        f"{name}: rules {summary['rules']['pass_rate']:.1%} | model "
        f"{summary['model']['pass_rate']:.1%}  (baseline {base['rules']['pass_rate']:.1%} | "
        f"{base['model']['pass_rate']:.1%})",
        color=ink, x=0.01, ha="left", fontsize=12,
    )  # fmt: skip
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=surface)
    plt.close(fig)


def metrics_line(name: str, s: dict, t: dict, retrieval: dict | None) -> str:
    r, m = s["rules"], s["model"]
    parts = [
        f"name={name}",
        f"rules_pass={r['pass_rate']:.4f}",
        *(f"rules_{k}={v['pass']:.4f}" for k, v in r["by_split"].items()),  # dev, heldout
        f"model_pass={m['pass_rate']:.4f}",
        f"future_rules={r['future_windows']}",
        f"future_model={m['future_windows']}",
        f"false_filter_rules={r['false_filters']}",
        f"false_filter_model={m['false_filters']}",
        f"errors={r['errors'] + m['errors']}",
        f"p50_us={t['p50_us']}",
        f"p99_us={t['p99_us']}",
        f"cases={r['n_cases']}",
    ]
    if retrieval is not None:
        parts += [f"retr_{k}={v}" for k, v in retrieval["summary"].items() if "_cand_" in k]
    return " ".join(parts)


def run(module: ModuleType, name: str) -> dict:
    rows = evaluate(module)
    return {"summary": summarize(rows), "rows": rows, "timing": timing(module)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", type=Path)
    ap.add_argument("--name")
    ap.add_argument("--no-retrieval", action="store_true")
    ap.add_argument("--phrases", type=Path, help="question file (default phrases.jsonl)")
    args = ap.parse_args()

    global PHRASES  # noqa: PLW0603
    suffix = ""
    if args.phrases is not None:
        p = args.phrases
        PHRASES = (p if p.is_absolute() or p.exists() else HERE / p).resolve()
        if PHRASES != (HERE / "phrases.jsonl").resolve():
            suffix = f"_{PHRASES.stem}"  # out/<name>_<stem>/, never overwrites out/<name>/

    import retrieval

    baseline = load(HERE / "baseline_today.py", "baseline_today")
    base = run(baseline, "baseline")
    if args.candidate is None:
        module, name, result = baseline, "baseline", base
    else:
        name = args.name or args.candidate.stem
        module = load(args.candidate.resolve(), f"cand_{name}")
        result = run(module, name)
    name += suffix

    retr = None if args.no_retrieval else retrieval.evaluate(module, baseline)
    out = HERE / "out" / name
    out.mkdir(parents=True, exist_ok=True)
    line = metrics_line(name, result["summary"], result["timing"], retr)
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    details = {"name": name, "summary": result["summary"], "timing": result["timing"],
               "baseline_summary": base["summary"], "retrieval": retr, "cases": result["rows"]}  # fmt: skip
    (out / "details.json").write_text(json.dumps(details, ensure_ascii=False, indent=1), "utf-8")
    plot(result["summary"], base["summary"], name, out / "side.png")
    print(line)
    for mode in ("rules", "model"):
        s = result["summary"][mode]
        print(f"[{mode}] by category:", {k: v["pass"] for k, v in s["by_category"].items()})
        print(f"[{mode}] by now:", {k: v["pass"] for k, v in s["by_now"].items()})
        print(f"[{mode}] by lang:", {k: v["pass"] for k, v in s["by_lang"].items()})
        print(f"[{mode}] by split:", {k: v["pass"] for k, v in s["by_split"].items()})
        print(f"[{mode}] two_anchor by connector:", {k: v["pass"] for k, v in s["by_connector"].items()})
        print(f"[{mode}] covers={s['covers_rate_timed']} tight={s['tight_rate_timed']} (timed cases)")
    if retr is not None:
        retrieval.print_table(retr)
    print(f"-> {out}")


if __name__ == "__main__":
    main()
