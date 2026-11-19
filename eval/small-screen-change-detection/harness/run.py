"""Run one change-detection candidate through the synthetic harness.

    uv run python eval/small-screen-change-detection/harness/run.py <candidate.py> <outdir>
        [--quick] [--workers N] [--no-seq]

Writes <outdir>/metrics.txt (one line), details.json and side.png. Synthetic content only.
The candidate contract is in hcommon.py. Parts:
  sweep      every case x 64x64 placements x {4k, 1440p} x {dark, light}; pair = page without
             the mark (ref = prev) vs with it (cur), fresh state per pair (--quick: 16x16
             stratified placements, every residue mod 4 and reduced offsets 0/7/8/15 mod 16)
  superset   baseline True but candidate False, over the sweep and the extra pairs
  timing     median of 31 runs (signature 9) after a warm-up, interleaved with the baseline,
             4K reduced frames, fresh state per run
  sequences  4K check streams at 5 s, one hour each, store simulation (ref = last stored)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcommon import HERE, MONITORS, THEMES, Candidate, baseline, digest, readonly, today_reduce  # noqa: E402
from scenes import (  # noqa: E402
    CASE_BY_NAME, CASES, FULL_PLACEMENTS, N_CHECKS, QUICK_PLACEMENTS, INTERVAL_S, Scene, Sweep,
    extra_pairs, now_at, page, streams,
)

MAX_DELAY = 6  # checks: a meaningful state stored later than this counts as missed
CACHE = HERE / "cache"

# --- sweep (worker side) ----------------------------------------------------------------------

_W: dict = {}


def _sweep_task(task):
    path, monitor, theme, name, quick, with_base = task
    placements = QUICK_PLACEMENTS if quick else FULL_PLACEMENTS
    out = {}
    jobs = [("cand", path)] + ([("base", str(HERE / "baseline.py"))] if with_base else [])
    for who, p in jobs:
        cand = _W.get(p) or _W.setdefault(p, Candidate(p))
        sw = _W.get((p, monitor, theme)) or _W.setdefault((p, monitor, theme), Sweep(cand.reduce, monitor, theme))
        ref = readonly(sw.ref)
        res = np.empty(len(placements), dtype=bool)
        for i, (_, _, cur) in enumerate(sw.frames(CASE_BY_NAME[name], placements)):
            res[i] = cand.decide(cand.make_state(), ref, cur, ref, monitor)
        out[who] = res
    return f"{monitor}|{theme}|{name}", out


def _cache_file(quick: bool) -> Path:
    h = hashlib.sha256()
    for f in ("hcommon.py", "scenes.py", "baseline.py"):
        h.update((HERE / f).read_bytes())
    return CACHE / f"base_sweep_{'quick' if quick else 'full'}_{h.hexdigest()[:12]}.npz"


def run_sweep(cand: Candidate, quick: bool, workers: int) -> tuple[dict, dict]:
    cache = _cache_file(quick)
    is_base = cand.path == (HERE / "baseline.py").resolve()
    base = dict(np.load(cache)) if cache.exists() else None
    with_base = base is None and not is_base
    tasks = [(str(cand.path), m, t, c.name, quick, with_base) for m in MONITORS for t in THEMES for c in CASES]
    res, new_base = {}, {}
    t0 = time.perf_counter()
    with mp.Pool(workers) as pool:
        for k, (key, out) in enumerate(pool.imap_unordered(_sweep_task, tasks)):
            res[key] = out["cand"]
            if "base" in out:
                new_base[key] = out["base"]
            if (k + 1) % 40 == 0:
                print(f"  sweep {k + 1}/{len(tasks)} {time.perf_counter() - t0:.0f} s", flush=True)
    if is_base:
        new_base = res
    if base is None:
        CACHE.mkdir(exist_ok=True)
        np.savez_compressed(cache, **new_base)
        base = new_base
    return res, base


# --- extra pairs ------------------------------------------------------------------------------


def run_extra(cand: Candidate, base: Candidate) -> list[dict]:
    rows = []
    for m in MONITORS:
        for t in THEMES:
            ref = page(m, t)
            rb, rc = readonly(today_reduce(ref)), readonly(cand.reduce(ref))
            for name, img in extra_pairs(m, t):
                cb, cc = readonly(today_reduce(img)), readonly(cand.reduce(img))
                rows.append({"monitor": m, "theme": t, "pair": name,
                             "base": base.decide(base.make_state(), rb, cb, rb, m),
                             "cand": cand.decide(cand.make_state(), rc, cc, rc, m)})
    return rows


# --- timing -----------------------------------------------------------------------------------


def _bench_pair(fb, fc, runs: int) -> tuple[float, float]:
    """Median ns of fb() and fc(), interleaved run by run after one warm-up call each, so both
    see the same clock and cache conditions. fb/fc take a run index (state prepared outside)."""
    fb(-1)
    fc(-1)
    tb, tc = [], []
    for i in range(runs):
        for f, ts in ((fb, tb), (fc, tc)) if i % 2 == 0 else ((fc, tc), (fb, tb)):
            t = time.perf_counter_ns()
            f(i)
            ts.append(time.perf_counter_ns() - t)
    return statistics.median(tb) / 1e6, statistics.median(tc) / 1e6


def _decider(c: Candidate, ref, cur, runs: int):
    ref, cur = readonly(ref), readonly(cur)
    states = [c.make_state() for _ in range(runs)] + [c.make_state()]  # index -1 = warm-up
    return lambda i: c.decide(states[i], ref, cur, ref, "4k")


def _timing_frames(c: Candidate) -> dict:
    sw = Sweep(c.reduce, "4k", "dark")
    word = next(sw.frames(CASE_BY_NAME["word:ok:14:full"], [(0, 0)]))[2].copy()
    caret = next(sw.frames(CASE_BY_NAME["bar2"], [(0, 0)]))[2].copy()
    large = next(c.reduce(img) for name, img in extra_pairs("4k", "dark") if name == "scroll40")
    return {"identical": (sw.ref, sw.ref.copy()), "word": (sw.ref, word), "caret": (sw.ref, caret), "large": (sw.ref, large)}


def run_timing(cand: Candidate, base: Candidate, runs: int = 31) -> dict:
    fb, fc = _timing_frames(base), _timing_frames(cand)
    out = {}
    for k in fb:
        out[f"t_{k}_base_ms"], out[f"t_{k}_ms"] = _bench_pair(
            _decider(base, *fb[k], runs), _decider(cand, *fc[k], runs), runs)
    img = page("4k", "dark")
    sig = lambda f: (lambda i: digest(f(img)))  # noqa: E731
    out["t_sig_base_ms"], out["t_sig_ms"] = _bench_pair(sig(today_reduce), sig(cand.reduce), 9)
    if not cand.overrides_reduce:
        out["t_sig_ms"] = out["t_sig_base_ms"]
    sig_extra = out["t_sig_ms"] - out["t_sig_base_ms"]
    out["extra_ms_worst"] = max(out[f"t_{k}_ms"] - out[f"t_{k}_base_ms"] for k in fb) + sig_extra
    return out


# --- sequences --------------------------------------------------------------------------------


class Sim:
    def __init__(self, c: Candidate) -> None:
        self.c, self.state = c, c.make_state()
        self.ref = self.prev = None
        self.stores: list[int] = []
        self.ns = 0
        self.calls = 0

    def step(self, i: int, gray: np.ndarray) -> None:
        gray = readonly(gray)
        if self.ref is None:
            self.ref = gray
            self.stores.append(i)
        else:
            t = time.perf_counter_ns()
            fire = self.c.decide(self.state, self.ref, gray, self.prev, "4k", now=now_at(i))
            self.ns += time.perf_counter_ns() - t
            self.calls += 1
            if fire:
                self.ref = gray
                self.stores.append(i)
        self.prev = gray


def _score(stream, stores: list[int]) -> dict:
    hours = N_CHECKS * INTERVAL_S / 3600
    out = {"stores_h": (len(stores) - 1) / hours}
    s = np.array(stores)
    for ev in stream.events:
        hit = s[(s >= ev["t"]) & (s < min(ev["end"], ev["t"] + MAX_DELAY + 1))]
        ev = dict(ev, delay=int(hit[0] - ev["t"]) if hit.size else None)
        out.setdefault("events", []).append(ev)
    return out


def run_sequences(cand: Candidate, base: Candidate) -> dict:
    reducers = {"today": today_reduce}
    if cand.overrides_reduce:
        reducers["cand"] = cand.reduce
    ck = "cand" if cand.overrides_reduce else "today"
    res = {}
    ns = calls = 0
    for st in streams():
        scene = Scene(page("4k", "dark"), reducers)
        sims = {"base": Sim(base), "cand": Sim(cand)}
        for i in range(N_CHECKS):
            for key, item in st.ops[i]:
                scene.set(key, item)
            g = scene.render()
            sims["base"].step(i, g["today"])
            sims["cand"].step(i, g[ck])
        res[st.name] = {who: _score(st, sim.stores) for who, sim in sims.items()}
        ns += sims["cand"].ns
        calls += sims["cand"].calls
    res["t_seq_ms"] = ns / max(calls, 1) / 1e6
    return res


# --- summary ----------------------------------------------------------------------------------


def summarize(sweep: dict, base_sweep: dict, extra: list[dict], timing: dict, seq: dict | None) -> tuple[dict, str]:
    groups: dict = {}
    per_case: dict = {}
    by_mon: dict = {}
    viol = 0
    for key, res in sweep.items():
        m, t, name = key.split("|")
        c = CASE_BY_NAME[name]
        bad = int((~res).sum() if c.positive else res.sum())
        g = groups.setdefault(c.group, {"bad": 0, "n": 0, "by_monitor": {}})
        g["bad"] += bad
        g["n"] += res.size
        bm = g["by_monitor"].setdefault(m, [0, 0])
        bm[0] += bad
        bm[1] += res.size
        pc = per_case.setdefault(name, {"bad": 0, "n": 0})
        pc["bad"] += bad
        pc["n"] += res.size
        cm = by_mon.setdefault(m, {}).setdefault(name, [0, 0])
        cm[0] += bad
        cm[1] += res.size
        viol += int((base_sweep[key] & ~res).sum())
    extra_viol = [r for r in extra if r["base"] and not r["cand"]]
    viol += len(extra_viol)
    f = lambda k: f"{groups[k]['bad']}/{groups[k]['n']}"  # noqa: E731
    parts = [f"miss_words={f('word_full')}", f"miss_words_d64={f('word_d64')}", f"miss_words_d40={f('word_d40')}",
             f"miss_badge={f('badge')}", f"miss_sentence={f('sentence')}", f"fire_caret={f('caret')}",
             f"fire_lines={f('line')}", f"fire_block={f('block')}", f"superset_viol={viol}",
             f"extra_fires={sum(r['cand'] for r in extra)}/{len(extra)}(base {sum(r['base'] for r in extra)})"]
    for k in ("identical", "word", "caret", "large", "sig"):
        parts.append(f"t_{k}_ms={timing[f't_{k}_ms']:.3f}")
    parts.append(f"extra_ms_worst={timing['extra_ms_worst']:+.3f}")
    if seq is not None:
        c = {k: v["cand"] for k, v in seq.items() if k != "t_seq_ms"}
        for k in ("clock", "spinner", "block", "minclock", "typing"):
            parts.append(f"seq_{k}_stores_h={c[k]['stores_h']:.0f}")
        miss = lambda evs: sum(e["delay"] is None for e in evs)  # noqa: E731
        rq, rb = c["replies_quiet"]["events"], c["replies_busy"]["events"]
        parts.append(f"seq_replies_missed={miss(rb)}/{len(rb)}")
        parts.append(f"seq_replies_missed_quiet={miss(rq)}/{len(rq)}")
        near = [e for e in rb if e["near_clock"]]
        parts.append(f"seq_replies_near_clock_missed={miss(near)}/{len(near)}")
        delays = [e["delay"] for e in rb + rq if e["delay"] is not None]
        parts.append(f"seq_reply_delay_max={max(delays) if delays else 'na'}")
        be = c["badge"]["events"]
        parts.append(f"seq_badge_missed={miss(be)}/{len(be)}")
        de = c["typing"]["events"]
        parts.append(f"seq_draft_missed={miss(de)}/{len(de)}")
        parts.append(f"t_seq_ms={seq['t_seq_ms']:.3f}")
    line = " ".join(parts)
    wrapped, cur = [], ""
    for p in parts:
        if len(cur) + len(p) > 95:
            wrapped.append(cur)
            cur = ""
        cur += p + " "
    wrapped.append(cur)
    details = {"groups": groups, "per_case": per_case, "cases_by_monitor": by_mon, "superset_viol": viol,
               "metrics_wrapped": wrapped, "extra": extra, "extra_superset_violations": extra_viol,
               "timing": timing, "sequences": seq}
    return details, line


def self_check(cand: Candidate) -> None:
    try:
        from akashic.capture import imaging
    except ImportError:
        imaging = None
    img = page("4k", "dark")
    if imaging is not None and not np.array_equal(imaging.gray_small(img), today_reduce(img)):
        raise AssertionError("today_reduce no longer matches akashic.capture.imaging.gray_small")
    for c in {id(x): x for x in (baseline(), cand)}.values():
        for m in MONITORS:
            for t in THEMES:
                Sweep(c.reduce, m, t).check()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate")
    ap.add_argument("outdir")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--no-seq", action="store_true")
    a = ap.parse_args()
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    cand, base = Candidate(a.candidate), baseline()
    t0 = time.perf_counter()
    self_check(cand)
    print(f"self-check ok {time.perf_counter() - t0:.0f} s", flush=True)
    timing = run_timing(cand, base)
    print("timing", {k: round(v, 3) for k, v in timing.items()}, flush=True)
    t1 = time.perf_counter()
    sweep, base_sweep = run_sweep(cand, a.quick, a.workers)
    t_sweep = time.perf_counter() - t1
    print(f"sweep {sum(v.size for v in sweep.values())} pairs in {t_sweep:.0f} s", flush=True)
    extra = run_extra(cand, base)
    seq = None if a.no_seq else run_sequences(cand, base)
    details, line = summarize(sweep, base_sweep, extra, timing, seq)
    details["run"] = {"candidate": str(cand.path), "quick": a.quick, "workers": a.workers,
                      "sweep_pairs": sum(v.size for v in sweep.values()), "sweep_s": round(t_sweep, 1),
                      "total_s": round(time.perf_counter() - t0, 1)}
    (out / "metrics.txt").write_text(line + "\n", encoding="utf-8")
    (out / "details.json").write_text(json.dumps(details, indent=1, default=int), encoding="utf-8")
    import plot

    plot.side(details, cand.name, out / "side.png")
    print(line)
    print(f"total {time.perf_counter() - t0:.0f} s")


if __name__ == "__main__":
    main()
