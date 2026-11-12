"""Equivalence + timing harness for build_app_runs candidates.

    python harness.py <candidate.py> <outdir> [--cases N] [--cap-s 60] [--skip-timing]

candidate.py defines build_app_runs(rows, games, horizon, add_episode) -> int, where
add_episode(**kw) takes kind, ref, start_ms, end_ms, title, app, url, body, items and returns a
bool (the recorder returns False on some calls, like INSERT OR IGNORE on a duplicate).

(a) Fuzz: >= N cases (default 10,000 + 2 full-size) per PYTHONHASHSEED in {0, 1, 2, random},
    each seed in its own subprocess; ref_today and the candidate run in the same interpreter
    and the repr of (recorded calls in a fixed key order, return value) must be identical.
(b) Timing: median of 9 after one warm-up, compute only (recorder appends kwargs), for the
    realistic and fragmented shapes at V=50,000, G=500, plus scaling V=25k/50k/100k (G=500) and
    G=50/500/5000 (V=50k) on the realistic shape. If warm-up x 10 would exceed --cap-s, the
    point is measured at V/2^k and extrapolated linearly in V (marked "~" / hollow markers).
(c) Writes metrics.txt (one line), timings.json, fuzz.json and side.png in <outdir>.
"""

from __future__ import annotations

import argparse
import gc
import importlib.util
import json
import os
import pathlib
import statistics
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402

KEYS = ("kind", "ref", "start_ms", "end_ms", "title", "app", "url", "body", "items")
SEEDS = ("0", "1", "2", "random")
BUDGET_MS = 20.0
V_POINTS = (25_000, 50_000, 100_000)
G_POINTS = (50, 500, 5000)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look the module up
    parent = str(pathlib.Path(path).resolve().parent)
    if parent not in sys.path:
        sys.path.append(parent)  # candidate's own helper modules
    spec.loader.exec_module(mod)
    return mod


def recorder():
    calls = []

    def add_episode(**kw):
        if set(kw) != set(KEYS):
            raise TypeError(f"add_episode keys {sorted(kw)}")
        calls.append(tuple(list(kw[k]) if k == "items" else kw[k] for k in KEYS))
        return len(calls) % 7 != 3

    return calls, add_episode


def outcome(fn, rows, games, horizon):
    calls, add = recorder()
    try:
        ret = fn(list(rows), list(games), horizon, add)
    except Exception as e:  # noqa: BLE001 - compared, not hidden
        return repr((calls, "raise", type(e).__name__, str(e)[:200]))
    return repr((calls, ret))


def _first_diff(a, b):
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    return i, a[max(0, i - 60) : i + 200], b[max(0, i - 60) : i + 200]


def fuzz_worker(candidate, n):
    ref = load(HERE / "ref_today.py", "ref_today_fuzz")
    cand = load(candidate, "candidate_fuzz")
    counts = {c: [0, 0] for c in (*fixtures.CATEGORIES, "full")}  # [cases, mismatches]
    examples = []
    cases = [("case", i) for i in range(n)] + [("full", s) for s in fixtures.SHAPES]
    for tag, x in cases:
        if tag == "case":
            cat, rows, games, horizon = fixtures.fuzz_case(x)
        else:
            cat = "full"
            rows, games, horizon = fixtures.SHAPES[x](1)
        a = outcome(ref.build_app_runs, rows, games, horizon)
        b = outcome(cand.build_app_runs, rows, games, horizon)
        counts[cat][0] += 1
        if a != b:
            counts[cat][1] += 1
            if sum(1 for e in examples if e["cat"] == cat) < 3:
                i, ea, eb = _first_diff(a, b)
                examples.append({"cat": cat, "case": x, "at_char": i, "ref": ea, "cand": eb})
    json.dump(
        {"seed": os.environ.get("PYTHONHASHSEED"), "counts": counts, "examples": examples},
        sys.stdout,
    )


def run_fuzz(candidate, n):
    procs = {}
    for seed in SEEDS:
        env = dict(os.environ, PYTHONHASHSEED=seed)
        procs[seed] = subprocess.Popen(
            [sys.executable, str(HERE / "harness.py"), "--fuzz-worker", str(candidate), str(n)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
    res = {}
    for seed, p in procs.items():
        out, err = p.communicate()
        if p.returncode != 0:
            res[seed] = {"error": err[-2000:]}
        else:
            res[seed] = json.loads(out)
    return res


# --- timing --------------------------------------------------------------------------------


def _time_once(fn, rows, games, horizon):
    calls, add = recorder()
    r, g = list(rows), list(games)
    gc.collect()
    t = time.perf_counter()
    fn(r, g, horizon, add)
    return (time.perf_counter() - t) * 1000


def measure(fn, shape, V, G, cap_s, reps=9):
    rows, games, horizon = fixtures.SHAPES[shape](1, V, G)
    warm = _time_once(fn, rows, games, horizon)
    Vm = V
    if warm * (reps + 1) / 1000 > cap_s:
        while warm * (Vm / V) * (reps + 1) / 1000 > cap_s and Vm > 1000:
            Vm //= 2
        rows, games, horizon = fixtures.SHAPES[shape](1, Vm, G)
        _time_once(fn, rows, games, horizon)
    ts = [_time_once(fn, rows, games, horizon) for _ in range(reps)]
    p50 = statistics.median(ts)
    return {
        "shape": shape, "V": V, "G": G, "V_measured": Vm, "G_actual": len(games),
        "rows_measured": len(rows), "p50_ms": p50 * V / Vm, "measured_p50_ms": p50,
        "min_ms": min(ts), "max_ms": max(ts), "extrapolated": Vm != V,
    }  # fmt: skip


def run_timing(candidate, cap_s):
    cand = load(candidate, "candidate_time")
    fn = cand.build_app_runs
    res = {"shapes": {}, "V": {}, "G": {}}
    for shape in fixtures.SHAPES:
        res["shapes"][shape] = measure(fn, shape, 50_000, 500, cap_s)
        print(f"  {shape}: {res['shapes'][shape]['p50_ms']:.2f} ms", file=sys.stderr, flush=True)
    for V in V_POINTS:
        res["V"][V] = res["shapes"]["realistic"] if V == 50_000 else measure(fn, "realistic", V, 500, cap_s)
        print(f"  V={V}: {res['V'][V]['p50_ms']:.2f} ms", file=sys.stderr, flush=True)
    for G in G_POINTS:
        res["G"][G] = res["shapes"]["realistic"] if G == 500 else measure(fn, "realistic", 50_000, G, cap_s)
        print(f"  G={G}: {res['G'][G]['p50_ms']:.2f} ms", file=sys.stderr, flush=True)
    return res


# --- output --------------------------------------------------------------------------------


def _fmt(m):
    return ("~" if m["extrapolated"] else "") + f"{m['p50_ms']:.2f}"


def metrics_line(fuzz, timing):
    mism = []
    cat_note = []
    for seed in SEEDS:
        r = fuzz.get(seed, {})
        if "error" in r:
            mism.append(f"{seed}:ERROR")
            continue
        mism.append(f"{seed}:{sum(v[1] for v in r['counts'].values())}")
        cat_note.append(f"{seed}:" + "/".join(f"{k}{v[1]}" for k, v in r["counts"].items()))
    cases = next((sum(v[0] for v in r["counts"].values()) for r in fuzz.values() if "counts" in r), 0)
    parts = [f"mismatches_by_seed={','.join(mism)}", f"cases_per_seed={cases}"]
    if cat_note:
        parts.append(f"mismatches_by_category={';'.join(cat_note)}")
    if timing:
        s, V, G = timing["shapes"], timing["V"], timing["G"]
        parts += [
            f"realistic_p50_ms={_fmt(s['realistic'])}",
            f"fragmented_p50_ms={_fmt(s['fragmented'])}",
            f"budget_ms={BUDGET_MS:g}",
            "V25k/50k/100k_ms=" + "/".join(_fmt(V[v]) for v in V_POINTS),
            f"ratio_V50/25={V[50_000]['p50_ms'] / V[25_000]['p50_ms']:.2f}",
            f"ratio_V100/50={V[100_000]['p50_ms'] / V[50_000]['p50_ms']:.2f}",
            "G50/500/5000_ms=" + "/".join(_fmt(G[g]) for g in G_POINTS),
            f"ratio_G500/50={G[500]['p50_ms'] / G[50]['p50_ms']:.2f}",
            f"ratio_G5000/500={G[5000]['p50_ms'] / G[500]['p50_ms']:.2f}",
        ]
        if any(m["extrapolated"] for d in (s, V, G) for m in d.values()):
            parts.append("note=~_marks_points_measured_at_smaller_V_and_extrapolated_linearly")
    return " ".join(parts)


def plot(timing, today, name, path):
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    plt.style.use("dark_background")
    fig = plt.figure(figsize=(13, 5))
    gs = fig.add_gridspec(2, 2, width_ratios=[1, 1.3])
    ax0 = fig.add_subplot(gs[:, 0])
    shapes = list(fixtures.SHAPES)
    x = range(len(shapes))
    w = 0.38
    tv = [today["shapes"][s]["p50_ms"] for s in shapes]
    cv = [timing["shapes"][s]["p50_ms"] for s in shapes]
    b1 = ax0.bar([i - w / 2 for i in x], tv, w, color="#8a8f98", label="today (ref_today)")
    b2 = ax0.bar([i + w / 2 for i in x], cv, w, color="#4fa3ff", label=name)
    for bars, src in ((b1, today), (b2, timing)):
        for bar, s in zip(bars, shapes, strict=True):
            m = src["shapes"][s]
            ax0.annotate(_fmt(m), (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                         ha="center", va="bottom", fontsize=8)  # fmt: skip
            if m["extrapolated"]:
                bar.set_hatch("//")
    ax0.axhline(BUDGET_MS, color="#ff6b6b", ls="--", lw=1, label="budget 20 ms")
    ax0.set_yscale("log")
    ax0.set_ylim(min(*tv, *cv, BUDGET_MS) / 4, max(*tv, *cv, BUDGET_MS) * 8)
    ax0.set_xticks(list(x), [f"{s}\nV=50k G=500" for s in shapes])
    ax0.set_ylabel("p50 ms per pass (log)")
    ax0.set_title("p50 per shape (hatched = extrapolated)")
    ax0.legend(fontsize=8, loc="upper left")
    for row, (key, pts, label) in enumerate((("V", V_POINTS, "V visits (G=500)"),
                                            ("G", G_POINTS, "G games (V=50k)"))):  # fmt: skip
        ax = fig.add_subplot(gs[row, 1])
        for src, color, ls, lab in ((today, "#8a8f98", "--", "today"), (timing, "#4fa3ff", "-", name)):
            d = src[key]
            ys = [d[p]["p50_ms"] for p in pts]
            ax.plot(pts, ys, ls=ls, color=color, label=lab)
            for p, y in zip(pts, ys, strict=True):
                ext = d[p]["extrapolated"]
                ax.plot([p], [y], "o", color=color, mfc="none" if ext else color)
        ax.axhline(BUDGET_MS, color="#ff6b6b", ls="--", lw=1)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel(label)
        ax.set_ylabel("p50 ms")
        ax.legend(fontsize=7, loc="upper left")
    fig.suptitle(f"build_app_runs: {name}  (realistic shape for scaling)", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=90)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate")
    ap.add_argument("outdir")
    ap.add_argument("--cases", type=int, default=10_000)
    ap.add_argument("--cap-s", type=float, default=60.0)
    ap.add_argument("--skip-timing", action="store_true")
    args = ap.parse_args()
    cand = pathlib.Path(args.candidate).resolve()
    out = pathlib.Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    print(f"python {sys.version.split()[0]}; fuzz {args.cases}+2 cases x seeds {SEEDS}", file=sys.stderr)
    t = time.perf_counter()
    fuzz = run_fuzz(cand, args.cases)
    print(f"fuzz done in {time.perf_counter() - t:.0f} s", file=sys.stderr, flush=True)
    (out / "fuzz.json").write_text(json.dumps(fuzz, indent=1), encoding="utf-8")
    timing = None
    if not args.skip_timing:
        timing = run_timing(cand, args.cap_s)
        (out / "timings.json").write_text(json.dumps(timing, indent=1), encoding="utf-8")
        ref_t = HERE / "ref" / "timings.json"
        today = json.loads(ref_t.read_text(encoding="utf-8")) if ref_t.exists() else timing
        for d in ("V", "G"):  # json keys are strings
            today[d] = {int(k): v for k, v in today[d].items()}
        plot(timing, today, cand.stem, out / "side.png")
    line = metrics_line(fuzz, timing)
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    print(line)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--fuzz-worker":
        fuzz_worker(pathlib.Path(sys.argv[2]), int(sys.argv[3]))
    else:
        main()
