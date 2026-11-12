"""Evaluation harness for thread follow-up field inheritance (scope QC).

Usage:
    python harness.py <candidate_dir | candidate.py> [--reps 20]
    python harness.py --baseline today|never [--reps 20]
    python harness.py --check            (print each pair's standalone and earlier plans)
    add --pairs <file.jsonl> to any of these to score another pairs file (e.g. heldout2.jsonl);
    its outputs go to <candidate dir or baselines/<name>>/<file stem>/, with metrics over that
    file (all, per split, language and category). A pair may carry its own "names" list.

A candidate is a module (``<candidate_dir>/candidate.py``, or the .py file given) exposing

    plan_thread(question: str, earlier_question: str, earlier_plan: Plan,
                standalone_plan: Plan, names: tuple[str, ...], now: datetime) -> Plan

``Plan`` is ``akashic.search.planner.Plan``. ``earlier_plan`` and ``standalone_plan`` are the
repo's ``heuristic_plan`` of the earlier and of the new question, each planned alone with the
same fixed clock: ``now`` is 2026-11-10 14:00 Europe/Paris (an aware datetime; a candidate that
needs the planner's arguments uses ``int(now.timestamp() * 1000)`` and ``now.tzinfo``).
Names are the known speakers Minlo, Sacha and Léa.

Scoring, per pair and per field (people, kinds, keywords, time). Values are compared as sets
for people/kinds/keywords (order ignored) and as (start_ms, end_ms) for time; S is the
standalone value, E the earlier value, C the candidate's:
    expected   gold "inherit" -> C == E;  gold "own" -> C == S;
               gold "none"    -> C is empty or C == S (a self-contained question plans as if
                              asked alone; for "what else" the topic may also be dropped).
    class      reported decision of the candidate: "none" if C is empty, else "own" if
               C == S (this includes S == E), else "inherit" if C == E, else "other".
Correctness is decided by value (so a field where S == E is right whichever way it was
reached). Accuracy per field is reported over all pairs, per split, language and category.
Target errors = field errors on the two target pairs (category "target").
Gate timing: perf_counter around ``plan_thread`` only (both heuristic_plan calls are done by
the harness beforehand), 1 warm-up pass then --reps passes over all pairs; p50/p99 in µs.
An exception in the candidate counts as "other" on all four fields.

Outputs, in the candidate dir (or baselines/<name>/): metrics.txt (one line), details.json,
side.png (held-out per-field accuracy: candidate vs the two built-in baselines, 95 % line).
Baselines: "today" = planner.follow_up(standalone, earlier); "never" = standalone only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
REPO_SRC = HERE.parents[1] / "src"
sys.path.insert(0, str(REPO_SRC))

from akashic.search import planner  # noqa: E402
from akashic.search.planner import Plan  # noqa: E402

FIELDS = ("people", "kinds", "keywords", "time")
NAMES: tuple[str, ...] = ("Minlo", "Sacha", "Léa")
TZ = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 11, 10, 14, 0, tzinfo=TZ)
NOW_MS = int(NOW.timestamp() * 1000)
BUDGET = 0.95
RULE_BUDGET_US = 5000.0


def load_pairs(path: Path | None = None) -> list[dict[str, Any]]:
    lines = (path or HERE / "pairs.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def names_of(pair: dict[str, Any]) -> tuple[str, ...]:
    """Known names for a pair: its own ``names`` list if it has one (heldout2.jsonl), else NAMES."""
    return tuple(pair.get("names", NAMES))


def plan_alone(question: str, names: tuple[str, ...] = NAMES) -> Plan:
    return planner.heuristic_plan(question, NOW_MS, names, TZ)


def value(plan: Plan, field: str) -> object:
    if field == "time":
        return None if plan.time is None else (plan.time.start_ms, plan.time.end_ms)
    return frozenset(getattr(plan, field))


def empty(v: object) -> bool:
    return v is None or v == frozenset()


def show(v: object) -> object:
    if v is None or isinstance(v, tuple):
        return v
    return sorted(v)  # type: ignore[arg-type]


def classify(c: object, s: object, e: object) -> str:
    if empty(c):
        return "none"
    if c == s:
        return "own"
    if c == e:
        return "inherit"
    return "other"


def correct(gold: str, c: object, s: object, e: object) -> bool:
    if gold == "inherit":
        return c == e
    if gold == "own":
        return c == s
    return empty(c) or c == s


# --- candidates ------------------------------------------------------------------------------


def baseline_today(question, earlier_question, earlier_plan, standalone_plan, names, now):  # noqa: ANN001, ANN201, ARG001, PLR0913
    return planner.follow_up(standalone_plan, earlier_plan)


def baseline_never(question, earlier_question, earlier_plan, standalone_plan, names, now):  # noqa: ANN001, ANN201, ARG001, PLR0913
    return standalone_plan


BASELINES = {"today": baseline_today, "never": baseline_never}


def load_candidate(path: Path) -> tuple[str, Any, Path]:
    file = path / "candidate.py" if path.is_dir() else path
    out = path if path.is_dir() else path.parent
    spec = importlib.util.spec_from_file_location(f"candidate_{abs(hash(file))}", file)
    assert spec is not None and spec.loader is not None, file
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(file.parent))
    spec.loader.exec_module(module)
    return path.name if path.is_dir() else file.stem, module.plan_thread, out


# --- evaluation ------------------------------------------------------------------------------


def prepare(pairs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Plan, Plan]]:
    return [
        (p, plan_alone(p["prev_question"], names_of(p)), plan_alone(p["question"], names_of(p)))
        for p in pairs
    ]


def evaluate(fn: Any, prepared: list[tuple[dict[str, Any], Plan, Plan]]) -> dict[str, Any]:
    rows = []
    exceptions = 0
    for p, earlier, alone in prepared:
        try:
            out = fn(p["question"], p["prev_question"], earlier, alone, names_of(p), NOW)
            if not isinstance(out, Plan):
                msg = f"plan_thread returned {type(out).__name__}"
                raise TypeError(msg)  # noqa: TRY301
            error = None
        except Exception as exc:  # noqa: BLE001 - a broken candidate is scored, not fatal
            out, error = None, f"{type(exc).__name__}: {exc}"
            exceptions += 1
        fields = {}
        for f in FIELDS:
            s, e = value(alone, f), value(earlier, f)
            c = None if out is None else value(out, f)
            gold = p["gold"][f]
            ok = out is not None and correct(gold, c, s, e)
            fields[f] = {
                "gold": gold,
                "got": "other" if out is None else classify(c, s, e),
                "ok": ok,
                "standalone": show(s),
                "earlier": show(e),
                "candidate": None if out is None else show(c),
            }
        rows.append({k: p[k] for k in ("id", "lang", "split", "category")} | {
            "prev_question": p["prev_question"], "question": p["question"],
            "error": error, "fields": fields,
        })  # fmt: skip
    return {"rows": rows, "exceptions": exceptions}


def accuracy(rows: list[dict[str, Any]], key: str | None = None) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups["all" if key is None else r[key]].append(r)
    out = {}
    for name, rs in sorted(groups.items()):
        per = {f: sum(r["fields"][f]["ok"] for r in rs) / len(rs) for f in FIELDS}
        out[name] = per | {"min": min(per.values()), "n": len(rs)}
    return out


def timing(fn: Any, prepared: list[tuple[dict[str, Any], Plan, Plan]], reps: int) -> dict[str, float]:
    samples: list[float] = []
    for rep in range(reps + 1):
        for p, earlier, alone in prepared:
            t = time.perf_counter()
            try:
                fn(p["question"], p["prev_question"], earlier, alone, names_of(p), NOW)
            except Exception:  # noqa: BLE001, S112 - scored in evaluate()
                continue
            dt = time.perf_counter() - t
            if rep:
                samples.append(dt * 1e6)
    if not samples:
        return {"p50_us": float("nan"), "p99_us": float("nan"), "n": 0}
    samples.sort()
    return {
        "p50_us": statistics.median(samples),
        "p99_us": samples[min(len(samples) - 1, int(0.99 * len(samples)))],
        "n": len(samples),
    }


def summarize(name: str, fn: Any, prepared: list[Any], reps: int) -> dict[str, Any]:
    result = evaluate(fn, prepared)
    rows = result["rows"]
    target = [r for r in rows if r["category"] == "target"]
    return {
        "name": name,
        "all": accuracy(rows)["all"],
        "split": accuracy(rows, "split"),
        "lang": accuracy(rows, "lang"),
        "category": accuracy(rows, "category"),
        "target_errors": sum(not r["fields"][f]["ok"] for r in target for f in FIELDS),
        "exceptions": result["exceptions"],
        "timing": timing(fn, prepared, reps),
        "errors": [
            {"id": r["id"], "prev": r["prev_question"], "question": r["question"],
             "error": r["error"],
             "wrong": {f: v for f, v in r["fields"].items() if not v["ok"]}}
            for r in rows if not all(v["ok"] for v in r["fields"].values())
        ],
        "rows": rows,
    }  # fmt: skip


def metrics_line(s: dict[str, Any]) -> str:
    a, h, d = s["all"], s["split"]["heldout"], s["split"]["dev"]
    ok = (
        a["min"] >= BUDGET
        and h["min"] >= BUDGET
        and s["target_errors"] == 0
        and s["exceptions"] == 0
    )
    parts = [f"all_min={a['min']:.3f}"]
    parts += [f"all_{f}={a[f]:.3f}" for f in FIELDS]
    parts += [f"heldout_min={h['min']:.3f}"] + [f"heldout_{f}={h[f]:.3f}" for f in FIELDS]
    parts += [f"dev_min={d['min']:.3f}"]
    parts += [f"{lang}_min={s['lang'][lang]['min']:.3f}" for lang in ("en", "fr", "mixed")]
    parts += [
        f"target_errors={s['target_errors']}",
        f"exceptions={s['exceptions']}",
        f"gate_p50_us={s['timing']['p50_us']:.1f}",
        f"gate_p99_us={s['timing']['p99_us']:.1f}",
        f"accuracy_budget_ok={'yes' if ok else 'no'}",
        f"rule_cost_ok={'yes' if s['timing']['p99_us'] <= RULE_BUDGET_US else 'no'}",
        f"n={a['n']}",
    ]
    return " ".join(parts)


def pairs_metrics_line(s: dict[str, Any]) -> str:
    """Metrics for an alternative pairs file (--pairs): all pairs, then per split, language and
    category. The accuracy budget is every per-field accuracy >= 0.95 over the file."""
    a = s["all"]
    ok = a["min"] >= BUDGET and s["target_errors"] == 0 and s["exceptions"] == 0
    parts = [f"all_min={a['min']:.3f}"] + [f"all_{f}={a[f]:.3f}" for f in FIELDS]
    parts += [f"{k}_min={v['min']:.3f}" for k, v in s["split"].items()]
    parts += [f"{k}_min={v['min']:.3f}" for k, v in s["lang"].items()]
    parts += [f"cat_{k}_min={v['min']:.3f}" for k, v in s["category"].items()]
    parts += [
        f"target_errors={s['target_errors']}",
        f"exceptions={s['exceptions']}",
        f"gate_p50_us={s['timing']['p50_us']:.1f}",
        f"gate_p99_us={s['timing']['p99_us']:.1f}",
        f"accuracy_budget_ok={'yes' if ok else 'no'}",
        f"rule_cost_ok={'yes' if s['timing']['p99_us'] <= RULE_BUDGET_US else 'no'}",
        f"n={a['n']}",
    ]
    return " ".join(parts)


def side_png(
    out: Path, cand: dict[str, Any], bases: dict[str, dict[str, Any]], split: str = "heldout"
) -> None:
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    surface, ink, muted = "#1a1a19", "#f0efec", "#c3c2b7"
    series = [
        (f"candidate: {cand['name']}", cand, "#3987e5"),
        ("today (follow_up)", bases["today"], "#d95926"),
        ("never inherit", bases["never"], "#199e70"),
    ]
    fig, ax = plt.subplots(figsize=(8, 4.2), facecolor=surface)
    ax.set_facecolor(surface)
    width = 0.26
    for k, (label, s, color) in enumerate(series):
        held = s["split"][split]
        xs = [i + (k - 1) * (width + 0.02) for i in range(len(FIELDS))]
        ys = [held[f] for f in FIELDS]
        ax.bar(xs, ys, width, color=color, label=label, zorder=2)
        for x, y in zip(xs, ys, strict=True):
            inside = y >= 0.12  # noqa: PLR2004
            ax.text(
                x, y - 0.02 if inside else y + 0.01, f"{y:.2f}", ha="center",
                va="top" if inside else "bottom", color=ink, fontsize=7, zorder=4,
            )  # fmt: skip
    ax.axhline(BUDGET, color=ink, linestyle="--", linewidth=1, zorder=3)
    ax.set_xlim(-0.5, len(FIELDS) - 0.5)
    ax.text(len(FIELDS) - 0.48, BUDGET, " 95 %", color=ink, fontsize=8, ha="left",
            va="center", clip_on=False)  # fmt: skip
    ax.set_xticks(range(len(FIELDS)), FIELDS, color=ink)
    ax.set_ylim(0, 1.08)
    label = "held-out" if split == "heldout" else split
    ax.set_ylabel(f"{label} accuracy", color=muted)
    ax.tick_params(colors=muted)
    ax.grid(axis="y", color="#383835", linewidth=0.6, zorder=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    n = cand["split"][split]["n"]
    fig.suptitle(
        f"Per-field {label} accuracy (n={n}) — target errors: candidate "
        f"{cand['target_errors']}, today {bases['today']['target_errors']}, "
        f"never {bases['never']['target_errors']}",
        color=ink, fontsize=9,
    )  # fmt: skip
    legend = ax.legend(
        loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3, fontsize=8, frameon=False
    )
    for text in legend.get_texts():
        text.set_color(ink)
    fig.tight_layout()
    fig.savefig(out / "side.png", dpi=110, facecolor=surface)
    plt.close(fig)


def check(prepared: list[tuple[dict[str, Any], Plan, Plan]]) -> None:
    """Print the standalone and earlier plans per pair and flag labels the rule planner cannot
    express (gold own with an empty standalone value, gold inherit with an empty earlier one)."""
    flags = 0
    for p, earlier, alone in prepared:
        print(f"[{p['id']}] {p['split']}  {p['prev_question']!r} -> {p['question']!r}")
        for f in FIELDS:
            s, e, g = value(alone, f), value(earlier, f), p["gold"][f]
            note = ""
            if g == "own" and empty(s):
                note = "  <- FLAG own but standalone empty"
            elif g == "inherit" and empty(e):
                note = "  (inherit of an empty earlier value)"
            elif g == "none" and not empty(s):
                note = "  (none, standalone has a value)"
            flags += note.startswith("  <-")
            print(f"    {f:8} gold={g:7} S={show(s)} E={show(e)}{note}")
    print(f"{flags} flag(s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", nargs="?", type=Path)
    ap.add_argument("--baseline", choices=sorted(BASELINES))
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--check", action="store_true")
    ap.add_argument(
        "--pairs", type=Path, help="score another pairs file; outputs go to <out>/<file stem>/"
    )
    args = ap.parse_args()
    pairs_file = args.pairs.resolve() if args.pairs else None
    prepared = prepare(load_pairs(pairs_file))
    if args.check:
        check(prepared)
        return
    bases = {n: summarize(n, fn, prepared, args.reps) for n, fn in BASELINES.items()}
    if args.baseline:
        name, out = args.baseline, HERE / "baselines" / args.baseline
        cand = bases[name]
    elif args.candidate is not None:
        name, fn, out = load_candidate(args.candidate.resolve())
        cand = summarize(name, fn, prepared, args.reps)
    else:
        ap.error("give a candidate dir/file or --baseline")
    split = "heldout"
    if pairs_file is not None:
        out = out / pairs_file.stem
        split = max(cand["split"], key=lambda k: cand["split"][k]["n"])
    out.mkdir(parents=True, exist_ok=True)
    line = metrics_line(cand) if pairs_file is None else pairs_metrics_line(cand)
    (out / "metrics.txt").write_text(line + "\n", "utf-8")
    details = {
        "candidate": cand,
        "baselines": {n: {k: v for k, v in b.items() if k != "rows"} for n, b in bases.items()},
        "now": NOW.isoformat(),
        "names": NAMES,
        "metrics": line,
    }
    if pairs_file is not None:
        details["names"] = sorted({n for p, _, _ in prepared for n in names_of(p)})
        details["pairs"] = str(pairs_file)
    (out / "details.json").write_text(
        json.dumps(details, indent=1, ensure_ascii=False, default=str), "utf-8"
    )
    side_png(out, cand, bases, split)
    print(line)
    print(f"-> {out}")


if __name__ == "__main__":
    main()
