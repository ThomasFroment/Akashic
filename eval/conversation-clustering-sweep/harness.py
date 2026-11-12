"""Measure a conversation-clustering candidate against the contract oracle.

    python harness.py <candidate.py> <outdir> [--reps 9] [--fuzz 10000] [--replay 1000]

candidate.py defines clusters(rows, games, horizon) -> (final, growing_start):
  final          list[list[int]]: member ids per final cluster, members sorted by (start, id),
                 clusters in voice-cluster order
  growing_start  int | None: earliest member start among growing clusters

Runs (a) exact equality vs oracle.py on seeded fuzz cases and every edge set in fixtures.py
(plus the realistic 8 h fixture, counted with the edges), (b) a multi-pass replay, (c) timing
of the whole post-fetchall pass at N = 20,000, and writes <outdir>/metrics.txt and side.png.

Replay model (b): segments become visible once they end (end_ms <= now). Every 30 s of `now`:
  horizon = min(now - 300 s, start - 1 of any segment in progress (start < now < end)),
  i.e. a recorded-but-not-transcribed segment holds the horizon back like a pending job;
  rows = visible, not yet linked, end_ms > since; final clusters are linked; since is advanced
  exactly like build_conversations. The union of finals over all passes must equal one call
  on all rows with an unbounded horizon.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import statistics
import sys
import time
import traceback

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixtures  # noqa: E402

GAP = 300_000
MARGIN = 30_000
STEP = 30_000
BUDGET_MS = 50.0


def load(path):
    path = pathlib.Path(path).resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"cand_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


def norm(result):
    final, growing = result
    return [list(c) for c in final], growing


def run(fn, rows, games, horizon):
    try:
        return norm(fn(list(rows), list(games), horizon)), None
    except Exception:  # noqa: BLE001 - a crash is a mismatch
        return None, traceback.format_exc(limit=3)


def describe(name, rows, games, horizon, want, got, err):
    lines = [f"--- first mismatch: {name}", f"horizon={horizon} games={games}"]
    if len(rows) <= 60:
        lines += [f"  {r[:5]} {r[5]}" for r in rows]
    else:
        lines.append(f"  ({len(rows)} rows)")
    if err:
        lines.append(f"candidate raised:\n{err}")
        return "\n".join(lines)
    wf, wg = want
    gf, gg = got
    lines.append(f"growing_start want={wg} got={gg}")
    lines.append(f"final clusters want={len(wf)} got={len(gf)}")
    for i in range(max(len(wf), len(gf))):
        a = wf[i] if i < len(wf) else None
        b = gf[i] if i < len(gf) else None
        if a != b:
            sa = set(a or [])
            sb = set(b or [])
            lines.append(
                f"  cluster {i}: want {len(sa)} ids, got {len(sb)} ids;"
                f" missing={sorted(sa - sb)[:20]} extra={sorted(sb - sa)[:20]}"
                + (" (same ids, different order)" if sa == sb else "")
            )
    return "\n".join(lines)


# --- (a) equality -------------------------------------------------------------------------


def check_equality(cand, oracle, n_fuzz):
    first = None
    fuzz_bad = 0
    for seed in range(n_fuzz):
        rows, games, horizon = fixtures.random_case(seed)
        want, _ = run(oracle.clusters, rows, games, horizon)
        got, err = run(cand.clusters, rows, games, horizon)
        if err or got != want:
            fuzz_bad += 1
            if first is None:
                first = describe(f"fuzz seed={seed}", rows, games, horizon, want, got, err)
    edges = {}
    for name, (rows, games, horizon) in fixtures.edge_sets().items():
        want, _ = run(oracle.clusters, rows, games, horizon)
        got, err = run(cand.clusters, rows, games, horizon)
        bad = bool(err or got != want)
        edges[name] = int(bad)
        if bad:
            print(describe(f"edge {name}", rows, games, horizon, want, got, err), flush=True)
    return fuzz_bad, edges, first


def check_realistic(cand, oracle, rows, games, horizon, cand_result):
    want, _ = run(oracle.clusters, rows, games, horizon)
    got, err = cand_result if cand_result is not None else run(cand.clusters, rows, games, horizon)
    bad = bool(err or got != want)
    if bad:
        print(describe("realistic_8h", rows, games, horizon, want, got, err), flush=True)
    return int(bad)


# --- (b) replay ---------------------------------------------------------------------------


def replay(fn, rows, games):
    """Returns None when consistent, else a short reason."""
    if not rows:
        return None
    max_end = max(r[3] for r in rows)
    one, err = run(fn, rows, games, max_end + 10**12)
    if err:
        return f"one-shot raised: {err}"
    expected = sorted(tuple(c) for c in one[0])
    linked: set[int] = set()
    got: list[tuple[int, ...]] = []
    since = 0
    now = rows[0][2]
    while now <= max_end + 2 * GAP + 2 * STEP:
        horizon = now - GAP
        for r in rows:
            if r[2] < now < r[3]:
                horizon = min(horizon, r[2] - 1)
        visible = [r for r in rows if r[3] <= now and r[0] not in linked and r[3] > since]
        res, err = run(fn, visible, games, horizon)
        if err:
            return f"pass now={now} raised: {err}"
        final, growing = res
        for c in final:
            if linked.intersection(c):
                return f"pass now={now}: segment linked twice {sorted(linked.intersection(c))}"
            linked.update(c)
            got.append(tuple(c))
        new_since = horizon - GAP - MARGIN
        if growing is not None:
            new_since = min(new_since, growing - MARGIN)
        since = max(since, new_since)
        now += STEP
    got.sort()
    if got != expected:
        return f"union of finals {got} != one-shot {expected}"
    return None


def check_replay(cand, n_replay):
    bad = 0
    first = None
    for seed in range(n_replay):
        rows, games = fixtures.replay_case(seed)
        reason = replay(cand.clusters, rows, games)
        if reason:
            bad += 1
            if first is None:
                first = f"--- first replay mismatch: seed={seed} games={games}\n" + "\n".join(
                    f"  {r[:5]} {r[5]}" for r in rows
                ) + f"\n{reason}"
    return bad, first


# --- (c) timing ---------------------------------------------------------------------------


def time_it(cand, rows, games, horizon, reps):
    res, err = run(cand.clusters, rows, games, horizon)  # warm-up (and the result for (a))
    samples = []
    for _ in range(reps):
        r = list(rows)
        t = time.perf_counter()
        cand.clusters(r, games, horizon)
        samples.append((time.perf_counter() - t) * 1e3)
    samples.sort()
    p95 = samples[min(len(samples) - 1, max(0, -(-95 * len(samples) // 100) - 1))]
    return statistics.median(samples), p95, (res, err)


def today_ms():
    path = HERE / "ref" / "metrics.txt"
    if not path.exists():
        return None
    fields = dict(kv.split("=") for kv in path.read_text().split())
    return float(fields["median_ms"])


# --- plot ---------------------------------------------------------------------------------


def plot(out, label, median, p95, today, scenarios):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.style.use("dark_background")
    ink, muted, accent, bad, base = "#e6e6e6", "#8a8a8a", "#5b9bd5", "#e5534b", "#7a7a7a"
    fig, (ax0, ax1) = plt.subplots(
        1, 2, figsize=(14, 6.5), gridspec_kw={"width_ratios": [1, 1.4]}, facecolor="#111111"
    )
    for ax in (ax0, ax1):
        ax.set_facecolor("#111111")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.spines["left"].set_color(muted)
        ax.spines["bottom"].set_color(muted)
        ax.tick_params(colors=muted)

    names = [label, "today"]
    vals = [median, today if today is not None else float("nan")]
    ax0.barh([0, 1], vals, height=0.5, color=[accent, base])
    ax0.errorbar([median], [0], xerr=[[0], [max(0.0, p95 - median)]], fmt="none", ecolor=ink,
                 capsize=4, lw=1)
    ax0.axvline(BUDGET_MS, color=ink, ls="--", lw=1)
    ax0.text(BUDGET_MS, 1.45, " 50 ms budget", color=ink, va="center", fontsize=9)
    ax0.set_xscale("log")
    finite = [v for v in vals + [BUDGET_MS, p95] if v == v and v > 0]
    ax0.set_xlim(1, 50_000)  # fixed so every candidate's plot reads the same
    ax0.set_ylim(-0.6, 1.7)
    ax0.set_yticks([0, 1], names, color=ink)
    ax0.invert_yaxis()
    for y, v in enumerate(vals):
        txt = "n/a (run ref first)" if v != v else f" {v:,.1f} ms"
        ax0.text(v if v == v else min(finite), y, txt, va="center", color=ink, fontsize=9)
    ax0.set_xlabel("median ms per pass, N = 20,000 (log); whisker = p95", color=muted)
    ax0.set_title("Timing: whole post-fetchall pass", color=ink, loc="left")

    keys = list(scenarios)
    counts = [scenarios[k][0] for k in keys]
    ys = range(len(keys))
    ax1.barh(ys, [max(c, 0) for c in counts], height=0.6,
             color=[bad if c else muted for c in counts])
    ax1.set_xscale("symlog", linthresh=1)
    ax1.set_xlim(0, max([30_000, *counts]))
    ax1.set_yticks(list(ys), [f"{k} (/{scenarios[k][1]})" for k in keys], fontsize=8, color=ink)
    ax1.invert_yaxis()
    for y, c in zip(ys, counts, strict=True):
        ax1.text(max(c, 0) + 0.05, y, f" {c}", va="center", fontsize=8,
                 color=bad if c else muted)
    total = sum(counts)
    ax1.set_title(f"Mismatches vs contract oracle (total {total})", color=ink, loc="left")
    ax1.set_xlabel("mismatching cases (symlog)", color=muted)
    fig.suptitle(f"conversation clustering: {label}", color=ink, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(out / "side.png", dpi=90, facecolor=fig.get_facecolor())
    plt.close(fig)


# --- main ---------------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate")
    ap.add_argument("outdir")
    ap.add_argument("--reps", type=int, default=9)
    ap.add_argument("--fuzz", type=int, default=10_000)
    ap.add_argument("--replay", type=int, default=1_000)
    args = ap.parse_args()
    out = pathlib.Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    cand = load(args.candidate)
    oracle = load(HERE / "oracle.py")
    label = pathlib.Path(args.candidate).stem

    t0 = time.perf_counter()
    fuzz_bad, edges, first = check_equality(cand, oracle, args.fuzz)
    print(f"(a) fuzz mismatches {fuzz_bad}/{args.fuzz}; edge mismatches "
          f"{sum(edges.values())}/{len(edges)} [{time.perf_counter() - t0:.1f} s]", flush=True)
    if first:
        print(first, flush=True)

    t0 = time.perf_counter()
    replay_bad, rfirst = check_replay(cand, args.replay)
    print(f"(b) replay mismatches {replay_bad}/{args.replay} [{time.perf_counter() - t0:.1f} s]",
          flush=True)
    if rfirst:
        print(rfirst, flush=True)

    rows, games, horizon = fixtures.realistic()
    t0 = time.perf_counter()
    median, p95, res = time_it(cand, rows, games, horizon, args.reps)
    print(f"(c) N={len(rows)} median {median:.2f} ms p95 {p95:.2f} ms "
          f"[{time.perf_counter() - t0:.1f} s]", flush=True)
    real_bad = check_realistic(cand, oracle, rows, games, horizon, res)
    edges_bad = sum(edges.values()) + real_bad

    today = median if label == "ref_today" else today_ms()
    line = (f"mismatches_fuzz={fuzz_bad} mismatches_edges={edges_bad} "
            f"mismatches_replay={replay_bad} median_ms={median:.3f} p95_ms={p95:.3f}")
    (out / "metrics.txt").write_text(line + "\n")
    print(line)
    if edges_bad:
        print("edge mismatches:", ", ".join(
            [k for k, v in edges.items() if v] + (["realistic_8h"] if real_bad else [])))

    scenarios = {"fuzz": (fuzz_bad, args.fuzz), "replay": (replay_bad, args.replay),
                 "realistic_8h": (real_bad, 1)}
    scenarios.update({k: (v, 1) for k, v in edges.items()})
    plot(out, label, median, p95, today, scenarios)


if __name__ == "__main__":
    main()
