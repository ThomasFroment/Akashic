"""App-time-between-frames harness: replay a candidate on the five harness runs and the synthetic
timelines, time it, and write <out>/side.png, <out>/metrics.txt and <out>/detail.json.

    uv run python eval/app-time-between-frames/harness/run.py <candidate.py> [<out_dir>] [--runs smoke|all]

Run from the repo root. <out_dir> defaults to the candidate's folder when the folder is named after
the module (baseline/baseline.py -> baseline/), else to a sibling folder named after the module
(explored/ref_cap60.py -> explored/ref_cap60/). The baseline (today's `sessions.app_bands`) is
always replayed in the same process for the side-by-side.

Real runs: `sessions.app_bands` is patched with the candidate and the unchanged `score.m5` scores
it, as eval/quality/spikes/session_hold_replay.py does. Only numbers are printed or written (exe
names of the harness's own apps appear in the per-app errors, as in scores.json).

Metrics (metrics.txt, one line): holes_pct_* = M5.1 seconds / truth app seconds (%); overlap_max_s =
worst M5.2; err_* = M5.3; past_end_s = band seconds past the stored end of recording (must be 0);
syn_away_counted_s = synthetic seconds counted while away or not recording (must be ~0);
syn_present_missed_s = synthetic present seconds not covered by a band of the right app;
ms_per_day = median of 9 timed calls over the day run's range, scaled to 24 h; uses_sim = which
simulated records the candidate read (fg, idle, fg+idle, no). detail.json has per-run, per-app
and per-scenario numbers, and the sim record stats.

Limits:
- The stores cannot show idle, away, lock, sleep or long static screens: no run has them (one
  pause per run only). The synthetic timelines cover them with a simplified capture model.
- The input-idle record is EMPTY on the real runs (no run has idle; whether the driver's synthetic
  input would register in GetLastInputInfo is unknown), so idle cuts are only exercised on the
  synthetic timelines.
- The foreground record is built from the observer's own WinEvent log, the same source as the
  scorer's truth (fg_runs): a candidate that uses it is scored against nearly its own input, so
  its real-run M5.1/M5.3 are an upper bound. It assumes the hook sees every switch at its exact
  time and that Akashic names windows as it did in its stored rows. Windows the observer could not
  name and Akashic never stored are 'unknown'; user-configured title exclusions and the built-in
  password-manager/banking exclusions cannot be identified from the log (the runs open none).
- M5.3 compares each truth run with the longest overlapping same-app band, unclipped: a band
  spanning several truth runs of one app (split by unnamed 'foreign' windows) counts in full
  against each, so holding a band longer can raise M5.3 even when it covers the truth better.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import core  # noqa: E402
from core import Ctx, Usage, install_sim, merge, overlap_ms  # noqa: E402

import score  # noqa: E402  (eval/quality, put on sys.path by core)
from akashic.pipeline import sessions  # noqa: E402

RUNS = {
    "smoke": ["20261118-smoke-s11-target-2220", "20261118-smoke-s11-target-2241"],
    "std": ["20261119-standard-s21-target-0315", "20261119-standard-s21-target-1818"],
    "day": ["20261119-day-s31-target-0547"],
}
TIMING_N = 9
DAY_MS = 86_400_000
BUDGET_MS_PER_DAY = 50.0


# --- simulated records from the observer's OS log ---------------------------------------------


def sim_fg_rows(tr, conn, now_ms: int) -> tuple[list, dict]:
    """Foreground changes as a WinEvent hook would log them (truth/os.jsonl `fg`), with Akashic's
    exclusions applied in memory: private windows and the vault page (driver window ids with
    '#private') and qualityvault.exe become 'excluded' rows without hwnd or process. A window
    Akashic stored as focused gets the process name Akashic gave it (UWP hosts resolved); a window
    the observer could not name ('foreign') and Akashic never stored becomes 'unknown'. Nothing is
    written inside a stored `all` gap; its end repeats the foreground of that moment."""
    fgs = sorted((e for e in tr.os if e["t"] == "fg"), key=lambda e: e["wall_ms"])
    all_gaps = [(g.start_ms, g.end_ms) for g in score_overlapping(conn, 0, now_ms, now_ms, ("all",))]
    rows, last, stats = [], None, {"events": len(fgs), "excluded": 0, "unknown": 0, "in_gap_dropped": 0, "gap_repeats": 0,
                                   "named_as_store": 0}
    # The process name Akashic gives a window (UWP hosts resolved, etc.), from its own focused rows:
    # a hook inside Akashic would name a window the same way, so the record must not carry the
    # observer's raw image name where the scorer's truth uses it.
    as_store: dict[int, tuple[str, int]] = {}
    for hwnd, proc, n in conn.execute("SELECT hwnd, process, COUNT(*) FROM frame_windows WHERE focused = 1"
                                      " AND process IS NOT NULL GROUP BY hwnd, process"):
        if n > as_store.get(hwnd, ("", 0))[1]:
            as_store[hwnd] = (proc, n)

    def row(e):
        wid = tr.hwnd_to_window.get(e["hwnd"]) or ""
        exe = e.get("exe")
        if "#private" in wid or (exe or "").lower() in core.EXCLUDED_PROCESSES:
            return (e["wall_ms"], None, None, "excluded")
        if e["hwnd"] in as_store:
            stats["named_as_store"] += as_store[e["hwnd"]][0].lower() != (exe or "").lower()
            return (e["wall_ms"], e["hwnd"], as_store[e["hwnd"]][0], "app")
        if e["kind"] == "foreign" or not exe:
            return (e["wall_ms"], None, None, "unknown")
        return (e["wall_ms"], e["hwnd"], exe, "app")

    for e in fgs:
        if e["hwnd"] == last:
            continue
        last = e["hwnd"]
        if any(g0 <= e["wall_ms"] < g1 for g0, g1 in all_gaps):
            stats["in_gap_dropped"] += 1
            stats["in_gap_dropped_before_t1"] = stats.get("in_gap_dropped_before_t1", 0) + (e["wall_ms"] < tr.t1)
            continue
        rows.append(row(e))
    for g0, g1 in all_gaps:  # capture restart: the foreground at the gap's end
        before = [e for e in fgs if e["wall_ms"] < g1]
        if before and g1 < now_ms:
            r = row(before[-1])
            rows.append((g1, *r[1:]))
            stats["gap_repeats"] += 1
    rows.sort(key=lambda r: r[0])
    stats["excluded"] = sum(r[3] == "excluded" for r in rows)
    stats["unknown"] = sum(r[3] == "unknown" for r in rows)
    stored = {p.lower() for (p,) in conn.execute("SELECT DISTINCT process FROM frame_windows WHERE focused = 1 AND process IS NOT NULL")}
    named = [r for r in rows if r[3] == "app"]
    stats["app_rows_whose_process_is_in_store"] = round(sum(r[2].lower() in stored for r in named) / max(1, len(named)), 3)
    stats["rows_per_hour"] = round(len(rows) / max(1e-9, (tr.t1 - tr.t0) / 3_600_000), 1)
    return rows, stats


def score_overlapping(conn, a, b, now, kinds):
    from akashic.storage.gaps import overlapping
    return overlapping(conn, a, b, now, kinds)


# --- one candidate on one store ---------------------------------------------------------------


def as_app_bands(fn, now_ms: int, usage: Usage):
    def app_bands(conn, a, b, process):
        out = fn(conn, a, b, now_ms, Ctx(conn, a, b, now_ms, usage))
        if process is not None:
            out = [x for x in out if (x.process or "").lower() == process.lower()]
        return out
    return app_bands


def timed(fn, conn, a, b, now_ms) -> float:
    """Median ms of TIMING_N calls after one warm-up call, a fresh ctx each call."""
    fn(conn, a, b, now_ms, Ctx(conn, a, b, now_ms))
    ts = []
    for _ in range(TIMING_N):
        t = time.perf_counter()
        fn(conn, a, b, now_ms, Ctx(conn, a, b, now_ms))
        ts.append((time.perf_counter() - t) * 1000)
    return statistics.median(ts)


def replay_run(rid: str, fns: dict, usages: dict) -> dict:
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _media = score.open_store(rid)
    now_ms = int(time.time() * 1000)
    fg_rows, fg_stats = sim_fg_rows(tr, conn, now_ms)
    install_sim(conn, fg_rows, [])  # input-idle: no run has idle periods -> empty = always active
    rec_end = Ctx(conn, tr.t0, tr.t1, now_ms).rec_end_ms
    runs = [r for r in tr.fg_runs() if r["kind"] != "foreign"]
    truth_by_exe: dict[str, int] = {}
    for r in runs:
        truth_by_exe[r["exe"] or "?"] = truth_by_exe.get(r["exe"] or "?", 0) + r["t1"] - r["t0"]
    out = {"truth_s": sum(r["t1"] - r["t0"] for r in runs) / 1000, "data_s": (tr.t1 - tr.t0) / 1000,
           "rec_end_minus_t1_s": (rec_end - tr.t1) / 1000, "sim_fg": fg_stats}
    gaps = merge([(g.start_ms, g.end_ms) for g in score_overlapping(conn, tr.t0, tr.t1, now_ms, ("all", "screen"))])
    for name, fn in fns.items():
        sessions.app_bands = as_app_bands(fn, now_ms, usages[name])
        try:
            m = score.m5(tr, conn, now_ms)
            bands = sessions.app_bands(conn, tr.t0, tr.t1, None)
        finally:
            sessions.app_bands = core.ORIGINAL_APP_BANDS
        per_app = m["M5.3"].get("per_app", {}) if isinstance(m["M5.3"], dict) else {}
        top = sorted(((k, v * truth_by_exe.get(k, 0) / 1000, v) for k, v in per_app.items()), key=lambda x: -x[1])[:5]
        ms = timed(fn, conn, tr.t0, tr.t1, now_ms)
        out[name] = {
            "M5.1_hole_s": round(m["M5.1"]["seconds"], 1),
            "M5.1_pct": round(100 * m["M5.1"]["seconds"] / m["M5.1"]["truth_seconds"], 2),
            "M5.2_overlap_s": round(m["M5.2"].get("seconds", 0.0), 1),
            "M5.3_error": round(m["M5.3"]["total_abs_error_ratio"], 3),
            "past_end_s": round(sum(max(0, x.end_ms - max(rec_end, x.start_ms)) for x in bands) / 1000, 1),
            "into_gap_s": round(overlap_ms([(x.start_ms, x.end_ms) for x in bands], gaps) / 1000, 1),
            "n_bands": len(bands),
            "top_app_errors": [{"exe": k, "abs_err_s": round(s, 1), "ratio": round(v, 3)} for k, s, v in top],
            "ms": round(ms, 3),
            "ms_per_day": round(ms * DAY_MS / (tr.t1 - tr.t0), 2),
        }
    conn.close()
    return out


# --- synthetic --------------------------------------------------------------------------------


def replay_synthetic(fns: dict, usages: dict) -> dict:
    import synthetic
    out = {}
    for sc in synthetic.build_all():
        conn, a, b, now = sc["conn"], sc["start"], sc["end"], sc["now"]
        res = {"present_s": sum(t1 - t0 for t0, t1, lab, _ in sc["truth"] if lab == "present") / 1000,
               "must_not_s": sum(t1 - t0 for t0, t1, lab, _ in sc["truth"] if lab != "present") / 1000}
        for name, fn in fns.items():
            bands = fn(conn, a, b, now, Ctx(conn, a, b, now, usages[name]))
            spans = [(x.start_ms, x.end_ms) for x in bands]
            away = overlap_ms(spans, [(t0, t1) for t0, t1, lab, _ in sc["truth"] if lab != "present"])
            missed = wrong = 0
            for t0, t1, lab, proc in sc["truth"]:
                if lab != "present":
                    continue
                right = overlap_ms([(t0, t1)], [s for s, x in zip(spans, bands) if (x.process or "").lower() == proc.lower()])
                anyb = overlap_ms([(t0, t1)], spans)
                missed += (t1 - t0) - right
                wrong += anyb - right
            res[name] = {"away_counted_s": round(away / 1000, 1), "present_missed_s": round(missed / 1000, 1),
                         "wrong_app_s": round(wrong / 1000, 1)}
        out[sc["name"]] = res
        conn.close()
    return out


# --- output -----------------------------------------------------------------------------------


def pair(xs):
    return "/".join(f"{x:g}" for x in xs)


def metrics_line(d: dict, who: str) -> str:
    def per(group, key):
        return [d["runs"][r][who][key] for r in RUNS[group] if r in d["runs"]]
    syn = d["synthetic"].values()
    parts = []
    for g in ("smoke", "std", "day"):
        if per(g, "M5.1_pct"):
            parts.append(f"holes_pct_{g}={pair(per(g, 'M5.1_pct'))}")
    parts.append(f"overlap_max_s={max(r[who]['M5.2_overlap_s'] for r in d['runs'].values()):g}")
    for g in ("smoke", "std", "day"):
        if per(g, "M5.3_error"):
            parts.append(f"err_{g}={pair(per(g, 'M5.3_error'))}")
    parts.append(f"past_end_s={max(r[who]['past_end_s'] for r in d['runs'].values()):g}")
    parts.append(f"syn_away_counted_s={sum(s[who]['away_counted_s'] for s in syn):g}")
    parts.append(f"syn_present_missed_s={sum(s[who]['present_missed_s'] for s in syn):g}")
    longest = max(d["runs"].values(), key=lambda r: r["data_s"])  # short runs scale a fixed per-call cost up
    parts.append(f"ms_per_day={longest[who]['ms_per_day']:g}")
    parts.append(f"uses_sim={d['uses_sim'][who]}")
    return " ".join(parts)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_smoke = "--runs" in sys.argv and sys.argv[sys.argv.index("--runs") + 1] == "smoke"
    args = [a for a in args if a not in ("smoke", "all")]
    cand_path = Path(args[0]).resolve()
    out = Path(args[1]).resolve() if len(args) > 1 else (cand_path.parent if cand_path.parent.name == cand_path.stem else cand_path.parent / cand_path.stem)
    out.mkdir(parents=True, exist_ok=True)
    cand = core.load(cand_path)
    base = core.load(HERE.parent / "baseline" / "baseline.py")
    fns = {"baseline": base.bands, "cand": cand.bands}
    usages = {"baseline": Usage(), "cand": Usage()}
    t_start = time.perf_counter()
    run_ids = RUNS["smoke"] if only_smoke else [r for g in RUNS.values() for r in g]
    rel = cand_path.relative_to(core.REPO) if cand_path.is_relative_to(core.REPO) else cand_path
    d = {"candidate": rel.as_posix(), "runs": {}, "synthetic": {}}
    for rid in run_ids:
        d["runs"][rid] = replay_run(rid, fns, usages)
        r = d["runs"][rid]
        print(f"{rid}: truth {r['truth_s']:.0f} s")
        for who in fns:
            x = r[who]
            print(f"  {who:<8} holes {x['M5.1_hole_s']:>7.1f} s ({x['M5.1_pct']:5.2f} %)  overlap {x['M5.2_overlap_s']:6.1f}  err {x['M5.3_error']:.3f}"
                  f"  past_end {x['past_end_s']:.1f}  into_gap {x['into_gap_s']:.1f}  {x['ms']:.2f} ms ({x['ms_per_day']:.1f} ms/day)")
    d["synthetic"] = replay_synthetic(fns, usages)
    for name, s in d["synthetic"].items():
        print(f"  syn {name:<17} present {s['present_s']:>5.0f} must_not {s['must_not_s']:>5.0f} | " + "  ".join(
            f"{who}: away+{s[who]['away_counted_s']:g} missed {s[who]['present_missed_s']:g} wrong {s[who]['wrong_app_s']:g}" for who in fns))
    d["uses_sim"] = {k: u.label() for k, u in usages.items()}
    d["runtime_s"] = round(time.perf_counter() - t_start, 1)
    d["metrics"] = {who: metrics_line(d, who) for who in fns}
    (out / "metrics.txt").write_text(d["metrics"]["cand"] + "\n", encoding="utf-8")
    (out / "detail.json").write_text(json.dumps(d, indent=1), encoding="utf-8")
    import draw
    draw.side(d, out / "side.png", title=cand_path.stem)
    print("baseline:", d["metrics"]["baseline"])
    print("cand:    ", d["metrics"]["cand"])
    print(f"runtime {d['runtime_s']} s -> {out}")


if __name__ == "__main__":
    main()
