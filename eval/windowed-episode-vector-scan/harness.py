"""Correctness and latency harness for the windowed episode vector step.

Usage:  python harness.py <candidate.py> <outdir> [--trials N]

The candidate module defines
    episode_vector_step(conn, model, qvec, start_ms, end_ms, candidates, limit=VECTOR_DEPTH)
        -> list[(owner_id, score)]  (or objects with .owner_id/.score), best first
where candidates = [(episode_id, start_ms), ...] from today's overlap query, ordered
start_ms DESC, id DESC. Optional:
    prepare(conn)  additive schema changes/backfill; then the harness works on a copy of the
                   cached DB (<outdir>/work.db, same key), never on the cache itself.
    PLANS          list of (sql, params) to EXPLAIN QUERY PLAN (statements issued during the
                   day/straddle runs are also traced and explained automatically).

Correctness (>= 1,000 seeded trials, results of today cached in cache/): owner lists and order
must equal today's, except that owners with exactly equal scores (identical float64 dot
products of the stored float16 vector with the query, i.e. duplicated vectors) may come in any
order in today's list and must come in (start_ms, embeddings.rowid) order in the candidate's,
and a top-``limit`` cut through such a tie group must keep the group's smallest keys.
Any other difference is a mismatch; mismatches are broken down into hard / near-tie (positions
whose exact scores differ by < 1e-6, i.e. decided by float32 rounding) / tie-order.

Timing: the candidate call only (candidates are computed beforehand), warm median and p95 of
21 runs after one warm-up, plus 3 cold runs, each on a fresh connection opened like
akashic.storage.db.connect (OS file cache stays warm). Today's code is timed in the same run.

Outputs in <outdir>: metrics.txt (one line), details.json, plans.txt, side.png.
"""

from __future__ import annotations

import gc
import hashlib
import importlib.util
import json
import pickle
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_db  # noqa: E402
import ref_today  # noqa: E402

REPO = HERE.parents[1]
HYBRID = REPO / "src" / "akashic" / "search" / "hybrid.py"
VECTOR_DEPTH = int(re.search(r"^VECTOR_DEPTH: Final = (\d+)", HYBRID.read_text("utf-8"), re.M)[1])
DAY, HOUR = build_db.DAY_MS, build_db.HOUR_MS
FAR = 2**62
SCORE_TOL = 1e-6
BUDGET_MS = 5.0
WARM_RUNS, COLD_RUNS = 21, 3
SEED = 7
SCENARIOS = ("day", "month", "straddle", "unwindowed")


def load(path: str):
    spec = importlib.util.spec_from_file_location(Path(path).stem + "_candidate", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


def norm_result(res) -> list[tuple[int, float]]:
    out = []
    for r in res:
        if hasattr(r, "owner_id"):
            out.append((int(r.owner_id), float(r.score)))
        else:
            out.append((int(r[0]), float(r[1])))
    return out


# --- ground truth from the stored vectors ---------------------------------------------------


class Truth:
    """All main-model episode vectors of valid size, with their tie keys (start_ms, rowid)."""

    def __init__(self, conn, model: str) -> None:
        rows = conn.execute(
            "SELECT rowid, owner_id, start_ms, vec FROM embeddings"
            " WHERE owner_kind = 'episode' AND model = ?",
            (model,),
        ).fetchall()
        rows = [r for r in rows if len(r[3]) == build_db.DIMS * 2]
        self.ids = np.array([r[1] for r in rows], dtype=np.int64)
        self.key = {int(r[1]): (int(r[2]), int(r[0])) for r in rows}
        self.m16 = np.vstack([np.frombuffer(bytes(r[3]), dtype=np.float16) for r in rows])
        self.m64 = self.m16.astype(np.float64)
        self.pos = {int(i): n for n, i in enumerate(self.ids)}
        digest: dict[bytes, list[int]] = {}
        for r in rows:
            digest.setdefault(bytes(r[3]), []).append(int(r[1]))
        self.dup_of = {i: g for g in digest.values() if len(g) > 1 for i in g}

    def exact(self, ids, q: np.ndarray) -> dict[int, float]:
        """float64 dot of each float16 vector with the float32 query, one reduction per row
        (identical vectors give identical values: exact ties are well defined)."""
        ids = [i for i in ids if i in self.pos]
        if not ids:
            return {}
        rows = self.m64[[self.pos[i] for i in ids]]
        vals = (rows * q.astype(np.float64)).sum(axis=1)
        return dict(zip(ids, vals.tolist(), strict=True))


def compare(ref, cand, allowed_ids, truth: Truth, q, limit):  # noqa: C901, PLR0912
    """Returns (kind, max_abs_dscore, info). kind in ok|hard|near_tie|tie_order."""
    allowed_valid = [i for i in allowed_ids if i in truth.pos]
    ex = truth.exact(allowed_valid, q)
    r_ids = [i for i, _ in ref]
    c_ids = [i for i, _ in cand]
    rs = dict(ref)
    dmax = 0.0
    for i, s in cand:
        base = rs.get(i, ex.get(i))
        if base is None:
            return "hard", float("inf"), {"why": f"owner {i} not an allowed valid vector"}
        dmax = max(dmax, abs(s - base))
    if len(set(c_ids)) != len(c_ids):
        return "hard", dmax, {"why": "duplicate owners in candidate list"}
    if len(c_ids) != len(r_ids):
        return "hard", dmax, {"why": f"length {len(c_ids)} != today {len(r_ids)}"}
    re_ = [ex[i] for i in r_ids]
    ce = [ex[i] for i in c_ids]
    if re_ != ce:
        diff = [k for k in range(len(re_)) if re_[k] != ce[k]]
        gap = max(abs(re_[k] - ce[k]) for k in diff)
        same_multiset = sorted(re_) == sorted(ce)
        kind = "near_tie" if same_multiset and gap < SCORE_TOL else "hard"
        k0 = diff[0]
        return kind, dmax, {"why": "order of exact scores differs", "first_pos": k0,
                            "today": r_ids[k0], "cand": c_ids[k0], "max_exact_gap": gap,
                            "positions": len(diff)}  # fmt: skip
    # Same exact-score sequence: compare tie groups.
    full = len(c_ids) == limit
    k = 0
    while k < len(ce):
        j = k
        while j < len(ce) and ce[j] == ce[k]:
            j += 1
        cg, rg = c_ids[k:j], r_ids[k:j]
        keys = [truth.key[i] for i in cg]
        if keys != sorted(keys):
            return "tie_order", dmax, {"why": "tie group not in (start_ms, rowid) order",
                                       "group": cg}  # fmt: skip
        if j == len(ce) and full:  # the cut may pass through this tie group
            whole = sorted((truth.key[i], i) for i, v in ex.items() if v == ce[k])
            want = [i for _, i in whole[: j - k]]
            if cg != want:
                return "tie_order", dmax, {"why": "cut through tie group keeps wrong members",
                                           "group": cg, "want": want}  # fmt: skip
        elif set(cg) != set(rg):
            return "hard", dmax, {"why": "tie group members differ", "cand": cg, "today": rg}
        k = j
    return "ok", dmax, {}


# --- trials ---------------------------------------------------------------------------------


def unit(v: np.ndarray) -> np.ndarray:
    v = v.astype(np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32)


def make_trials(conn, meta, truth: Truth, n: int) -> list[dict]:  # noqa: C901, PLR0915
    rng = np.random.default_rng(SEED)
    t0, t1 = int(meta["t0"]), int(meta["t_end"])
    eps = conn.execute("SELECT id, start_ms, end_ms FROM episodes").fetchall()
    longs = [e for e in eps if e[2] - e[1] >= DAY]
    starts = np.array([e[1] for e in eps])
    vals, counts = np.unique(starts, return_counts=True)
    equal_starts = vals[counts > 1]
    kinds_all = ["video", "music", "conversation", "browsing", "work", "game"]
    apps_all = ["firefox", "Discord.exe", "Spotify", "zen.exe", "Code"]
    types = (["1h"] * 20 + ["1day"] * 20 + ["1week"] * 12 + ["1month"] * 8 + ["inside_long"] * 15
             + ["unwindowed"] * 8 + ["edge"] * 7 + ["empty"] * 2 + ["equal_start"] * 8)  # fmt: skip
    trials = []
    for t in range(n):
        typ = types[int(rng.integers(len(types)))]
        lengths = {"1h": HOUR, "1day": DAY, "1week": 7 * DAY, "1month": 30 * DAY}
        if typ in lengths:
            length = lengths[typ]
            if typ != "1h" and rng.random() < 0.6:  # calendar-aligned (UTC or UTC+1 midnight)
                d = int(rng.integers(0, (t1 - t0) // DAY))
                s = t0 + d * DAY - int(rng.integers(0, 2)) * HOUR
            else:
                s = int(rng.integers(t0 - DAY, t1))
            e = s + length
        elif typ == "inside_long":
            ep = longs[int(rng.integers(len(longs)))]
            length = [HOUR, DAY, 7 * DAY][int(rng.integers(3))]
            s = int(rng.integers(ep[1] + 1, max(ep[1] + 2, ep[2] - length // 2)))
            e = s + length
        elif typ == "unwindowed":
            s, e = 0, FAR
        elif typ == "edge":
            ep = eps[int(rng.integers(len(eps)))]
            length = int(rng.integers(HOUR, DAY))
            s, e = ((ep[2], ep[2] + length) if rng.random() < 0.5
                    else (ep[1] - length, ep[1]))  # fmt: skip
        elif typ == "empty":
            s = t0 - 400 * DAY if rng.random() < 0.5 else t1 + 300 * DAY
            e = s + DAY
        else:  # equal_start
            s = int(equal_starts[int(rng.integers(len(equal_starts)))])
            e = s + [HOUR, DAY][int(rng.integers(2))]
        filt: dict = {}
        r = rng.random()
        if r < 0.08:
            filt["kinds"] = tuple(rng.choice(kinds_all, int(rng.integers(1, 3)), replace=False))
        elif r < 0.12:
            filt["apps"] = list(rng.choice(apps_all, int(rng.integers(1, 3)), replace=False))
        elif r < 0.15:
            filt["people"] = frozenset(int(p) for p in rng.choice(np.arange(2, 13), 2,
                                                                  replace=False))  # fmt: skip
        cands = ref_today.candidates(conn, s, e, **filt)
        qr = rng.random()
        ids = [i for i, _ in cands if i in truth.pos]
        if qr < 0.40 or not ids:
            qtype, q = "random", unit(rng.standard_normal(build_db.DIMS))
        elif qr < 0.75:
            src = ids[int(rng.integers(len(ids)))]
            v = truth.m16[truth.pos[src]].astype(np.float32)
            qtype, q = "topic", unit(v + 0.07 * rng.standard_normal(build_db.DIMS))
        else:
            dups = [i for i in ids if i in truth.dup_of]
            src = (dups or ids)[int(rng.integers(len(dups or ids)))]
            qtype, q = "exact_dup", truth.m16[truth.pos[src]].astype(np.float32)
        trials.append({"n": t, "type": typ, "start": int(s), "end": int(e), "filter": filt,
                       "qtype": qtype, "q": q, "cands": cands})  # fmt: skip
    return trials


def ref_results(conn, meta, trials) -> list:
    db = Path(str(meta["db"]))
    tag = hashlib.sha256(
        f"{db.stat().st_size}:{db.stat().st_mtime_ns}:{SEED}:{len(trials)}:{VECTOR_DEPTH}".encode()
    ).hexdigest()[:16]
    path = build_db.CACHE / f"ref_results_{tag}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    out = []
    for tr in trials:
        out.append(ref_today.episode_vector_step(
            conn, meta["model"], tr["q"], tr["start"], tr["end"], list(tr["cands"]),
            limit=VECTOR_DEPTH))  # fmt: skip
    path.write_bytes(pickle.dumps(out))
    return out


# --- timing ---------------------------------------------------------------------------------


def scenario_windows(conn, meta) -> dict[str, tuple[int, int, list]]:
    t0 = int(meta["t0"])
    rng = np.random.default_rng(11)
    best = None
    for d in rng.integers(30, 700, 300):
        s = t0 + int(d) * DAY
        c = ref_today.candidates(conn, s, s + DAY)
        if best is None or abs(len(c) - 50) < abs(len(best[2]) - 50):
            best = (s, s + DAY, c)
    out = {"day": best}
    s = t0 + 365 * DAY
    out["month"] = (s, s + 30 * DAY, ref_today.candidates(conn, s, s + 30 * DAY))
    span = meta["forced_60day_spans"][1]
    s = (span[0] // DAY + 30) * DAY  # a calendar day 30 days into the 60-day episode
    out["straddle"] = (s, s + DAY, ref_today.candidates(conn, s, s + DAY))
    out["unwindowed"] = (0, FAR, ref_today.candidates(conn, 0, FAR))
    return out


def time_step(fn, conn_factory, conn, model, q, s, e, cands) -> dict:
    def once(c):
        arg = list(cands)
        t = time.perf_counter_ns()
        fn(c, model, q, s, e, arg, limit=VECTOR_DEPTH)
        return (time.perf_counter_ns() - t) / 1e6

    gc.collect()
    once(conn)  # warm-up
    warm = [once(conn) for _ in range(WARM_RUNS)]
    cold = []
    for _ in range(COLD_RUNS):
        fresh = conn_factory()
        cold.append(once(fresh))
        fresh.close()
    return {"p50": float(np.median(warm)), "p95": float(np.percentile(warm, 95)),
            "min": float(min(warm)), "cold": cold, "cold_p50": float(np.median(cold))}  # fmt: skip


# --- plans ----------------------------------------------------------------------------------


def explain(conn, sql, params=()) -> list[str]:
    try:
        return [f"{r[0]}:{r[1]} {r[3]}" for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]
    except Exception as exc:  # noqa: BLE001
        return [f"(not explainable: {exc})"]


def traced_plans(fn, conn, model, q, s, e, cands) -> list[tuple[str, list[str]]]:
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        fn(conn, model, q, s, e, list(cands), limit=VECTOR_DEPTH)
    finally:
        conn.set_trace_callback(None)
    out = []
    for sql in dict.fromkeys(seen):
        short = re.sub(r"x'[0-9a-fA-F]{64,}'", "x'…'", sql)
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            out.append((short, explain(conn, sql)))
        else:
            out.append((short, ["(not a query)"]))
    return out


# --- plot -----------------------------------------------------------------------------------


def plot(path: Path, name: str, cand_t: dict, today_t: dict, dscores: np.ndarray, mism: int):
    plt.style.use("dark_background")
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.6))
    x = np.arange(len(SCENARIOS))
    w = 0.38
    tp = [today_t[k]["p50"] for k in SCENARIOS]
    t95 = [today_t[k]["p95"] for k in SCENARIOS]
    cp = [cand_t[k]["p50"] for k in SCENARIOS]
    c95 = [cand_t[k]["p95"] for k in SCENARIOS]
    ax[0].bar(x - w / 2, tp, w, color="#8a8a8a", label="today p50 (bar) / p95 (whisker)",
              yerr=[[0] * 4, np.subtract(t95, tp)], capsize=3, ecolor="#cccccc")  # fmt: skip
    ax[0].bar(x + w / 2, cp, w, color="#4f9cf0", label=f"{name} p50 / p95",
              yerr=[[0] * 4, np.subtract(c95, cp)], capsize=3, ecolor="#cccccc")  # fmt: skip
    ax[0].scatter(x - w / 2, [today_t[k]["cold_p50"] for k in SCENARIOS], marker="x",
                  color="#ffffff", zorder=3, label="cold (fresh connection), median")  # fmt: skip
    ax[0].scatter(x + w / 2, [cand_t[k]["cold_p50"] for k in SCENARIOS], marker="x",
                  color="#ffffff", zorder=3)  # fmt: skip
    ax[0].axhline(BUDGET_MS, color="#f05a4f", ls="--", lw=1.2, label="5 ms budget")
    for xi, v in zip(x + w / 2, cp, strict=True):
        ax[0].text(xi, v * 1.15, f"{v:.2f}", ha="center", fontsize=8)
    for xi, v in zip(x - w / 2, tp, strict=True):
        ax[0].text(xi, v * 1.15, f"{v:.1f}", ha="center", fontsize=8, color="#bbbbbb")
    ax[0].set_yscale("log")
    ax[0].set_ylim(0.01, 2000)
    ax[0].set_xticks(x, ["1 day (~50 ep.)", "1 month", "1 day inside\n60-day episode",
                         "unwindowed"])  # fmt: skip
    ax[0].set_ylabel("episode vector step (ms, log)")
    ax[0].set_title("Latency per scenario, 40,000 episodes, SQLCipher")
    ax[0].legend(fontsize=7, loc="upper left")
    zeros = int((dscores == 0).sum())
    nz = dscores[dscores > 0]
    bins = np.logspace(-12, -3, 37)
    if nz.size:
        ax[1].hist(np.clip(nz, 1e-12, 1e-3), bins=bins, color="#4f9cf0")
    ax[1].set_xscale("log")
    ax[1].set_xlim(1e-12, 1e-3)
    ax[1].axvline(SCORE_TOL, color="#f05a4f", ls="--", lw=1.2, label="1e-6 tolerance")
    ax[1].set_xlabel("|candidate score - today's score| (non-zero only)")
    ax[1].set_ylabel("owners")
    mx = float(dscores.max()) if dscores.size else 0.0
    ax[1].set_title(f"|Δscore|: {zeros:,} exactly 0, {nz.size:,} non-zero, max {mx:.2e}")
    ax[1].text(0.02, 0.95, f"mismatching trials: {mism}", transform=ax[1].transAxes,
               fontsize=9, va="top")  # fmt: skip
    ax[1].legend(fontsize=7, loc="upper right")
    fig.suptitle(f"windowed-episode-vector-scan: {name}", fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=90, facecolor="#000000")
    plt.close(fig)


# --- main -----------------------------------------------------------------------------------


def main() -> None:  # noqa: C901, PLR0912, PLR0915
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    n_trials = 1200
    if "--trials" in args:
        k = args.index("--trials")
        n_trials = int(args[k + 1])
        del args[k : k + 2]
    cand_path, out = args[0], Path(args[1])
    out.mkdir(parents=True, exist_ok=True)
    meta = build_db.ensure()
    model = str(meta["model"])
    mod = load(cand_path)
    name = Path(cand_path).stem
    step = mod.episode_vector_step

    base = build_db.connect()
    truth = Truth(base, model)
    cand_db = build_db.DB_PATH
    prepare_ms = None
    if hasattr(mod, "prepare"):
        base.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cand_db = out / "work.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(cand_db) + suffix).unlink(missing_ok=True)
        shutil.copyfile(build_db.DB_PATH, cand_db)
        c = build_db.connect(cand_db)
        t = time.perf_counter_ns()
        mod.prepare(c)
        prepare_ms = (time.perf_counter_ns() - t) / 1e6
        c.close()
    cconn = build_db.connect(cand_db)

    print(f"VECTOR_DEPTH={VECTOR_DEPTH}; building {n_trials} trials …", flush=True)
    trials = make_trials(base, meta, truth, n_trials)
    t = time.perf_counter()
    refs = ref_results(base, meta, trials)
    print(f"today's results ready ({time.perf_counter() - t:.1f} s)", flush=True)

    kinds_count = {"ok": 0, "hard": 0, "near_tie": 0, "tie_order": 0, "error": 0}
    dscores: list[float] = []
    worst = None
    failures = []
    by_type: dict[str, list[int]] = {}
    for tr, ref in zip(trials, refs, strict=True):
        try:
            res = norm_result(step(cconn, model, tr["q"], tr["start"], tr["end"],
                                   list(tr["cands"]), limit=VECTOR_DEPTH))  # fmt: skip
        except Exception:  # noqa: BLE001
            kinds_count["error"] += 1
            failures.append({"trial": tr["n"], "kind": "error", "info": traceback.format_exc()})
            continue
        kind, dmax, info = compare(ref, res, [i for i, _ in tr["cands"]], truth, tr["q"],
                                   VECTOR_DEPTH)  # fmt: skip
        kinds_count[kind] += 1
        st = by_type.setdefault(tr["type"], [0, 0, 0])
        st[0] += 1
        st[1] += kind != "ok"
        st[2] += len(tr["cands"])
        rs = dict(ref)
        ex = truth.exact([i for i, _ in res], tr["q"])
        dscores.extend(abs(s - rs.get(i, ex.get(i, s))) for i, s in res)
        if worst is None or dmax > worst[0]:
            worst = (dmax, tr)
        if kind != "ok":
            failures.append({"trial": tr["n"], "type": tr["type"], "start": tr["start"],
                             "end": tr["end"], "filter": {k: list(v) for k, v in
                             tr["filter"].items()}, "qtype": tr["qtype"], "kind": kind,
                             "info": info})  # fmt: skip
    mismatches = sum(v for k, v in kinds_count.items() if k != "ok")
    d = np.array(dscores, dtype=np.float64)
    max_d = float(d.max()) if d.size else 0.0
    print(f"trials={len(trials)} {kinds_count} max_abs_dscore={max_d:.3e}")
    if worst is not None:
        tr = worst[1]
        print(f"worst |Δscore| {worst[0]:.3e}: trial {tr['n']} type={tr['type']} "
              f"window=[{tr['start']}, {tr['end']}) q={tr['qtype']} "
              f"candidates={len(tr['cands'])}")  # fmt: skip
    for f in failures[:5]:
        print("FAIL", json.dumps(f, default=str)[:400])

    windows = scenario_windows(base, meta)
    qtime = unit(truth.m16[truth.pos[windows["day"][2][0][0]]].astype(np.float32) + 0.05)
    cand_t, today_t = {}, {}
    for sc in SCENARIOS:
        s, e, cands = windows[sc]
        today_t[sc] = time_step(ref_today.episode_vector_step, build_db.connect, base, model,
                                qtime, s, e, cands)  # fmt: skip
        cand_t[sc] = time_step(step, lambda: build_db.connect(cand_db), cconn, model, qtime, s,
                               e, cands)  # fmt: skip
        print(f"{sc:>10}: candidates={len(cands):>6}  today p50={today_t[sc]['p50']:9.3f} ms "
              f"p95={today_t[sc]['p95']:9.3f} cold={today_t[sc]['cold_p50']:9.3f} | "
              f"{name} p50={cand_t[sc]['p50']:8.3f} p95={cand_t[sc]['p95']:8.3f} "
              f"cold={cand_t[sc]['cold_p50']:8.3f}", flush=True)  # fmt: skip

    lines = []
    for sc in ("day", "straddle", "unwindowed"):
        s, e, cands = windows[sc]
        lines.append(f"## {sc}: window [{s}, {e}), {len(cands)} candidates")
        for label, fn, c in (("today", ref_today.episode_vector_step, base), (name, step, cconn)):
            lines.append(f"# {label} (traced)")
            for sql, plan in traced_plans(fn, c, model, qtime, s, e, cands):
                lines.append(f"SQL: {sql[:600]}")
                lines.extend(f"    {p}" for p in plan)
    for entry in getattr(mod, "PLANS", []) or []:
        sql, params = (entry, ()) if isinstance(entry, str) else entry
        lines.append(f"# PLANS: {sql}")
        lines.extend(f"    {p}" for p in explain(cconn, sql, params))
    (out / "plans.txt").write_text("\n".join(lines) + "\n", "utf-8")

    metrics = {
        "mismatches": mismatches,
        "max_abs_dscore": max_d,
        "day_p50_ms": cand_t["day"]["p50"],
        "day_p95_ms": cand_t["day"]["p95"],
        "month_p50_ms": cand_t["month"]["p50"],
        "straddle_p50_ms": cand_t["straddle"]["p50"],
        "unwindowed_p50_ms": cand_t["unwindowed"]["p50"],
        "today_unwindowed_p50_ms": today_t["unwindowed"]["p50"],
    }
    line = " ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                    for k, v in metrics.items())  # fmt: skip
    (out / "metrics.txt").write_text(line + "\n", "utf-8")
    details = {
        "candidate": str(Path(cand_path).resolve()), "db": str(cand_db), "trials": len(trials),
        "vector_depth": VECTOR_DEPTH, "breakdown": kinds_count,
        "by_window_type": {k: {"trials": v[0], "mismatches": v[1],
                               "mean_candidates": v[2] / max(v[0], 1)}
                           for k, v in sorted(by_type.items())},
        "compared_owners": int(d.size), "dscore_zero": int((d == 0).sum()),
        "max_abs_dscore": max_d, "prepare_ms": prepare_ms,
        "scenarios": {sc: {"window": windows[sc][:2], "candidates": len(windows[sc][2]),
                           "today": today_t[sc], "candidate": cand_t[sc]} for sc in SCENARIOS},
        "failures": failures[:50], "metrics": metrics, "db_meta": meta,
    }  # fmt: skip
    (out / "details.json").write_text(json.dumps(details, indent=1, default=str), "utf-8")
    plot(out / "side.png", name, cand_t, today_t, d, mismatches)
    print(line)
    cconn.close()
    base.close()


if __name__ == "__main__":
    main()
