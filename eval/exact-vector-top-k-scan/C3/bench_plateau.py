"""Plateau fixture: passages chunks whose 4,000 rows all score the same value above T.

Compares C3's run-level selection (cand.select_passages: reduceat run max + first argmax,
partition top-k runs, merge) against a plain survivor lexsort (lexsort((seq, -s, o)) over the
survivors, first per owner, then the same partition/merge), over 25 chunks (100k rows), for
several limits and score shapes. Both must give identical retained sets after every chunk.
Times are per scanned row for the selection only (scores and owners given), min of 15 runs.
"""

import importlib.util
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("c3cand", HERE / "cand.py")
cand = importlib.util.module_from_spec(spec)
sys.modules["c3cand"] = cand
spec.loader.exec_module(cand)

CH, N_CHUNKS = 4000, 25


def lexsort_select(s, o, limit, r_o, r_s):
    """Plain survivor lexsort: same survivor rule, per-owner first max via lexsort."""
    sel = np.flatnonzero(s >= r_s[-1]) if r_s.size == limit else np.flatnonzero(s == s)
    if not sel.size:
        return None
    ss, so_ = s[sel], o[sel]
    order = np.lexsort((sel, -ss, so_))
    oo = so_[order]
    first = np.ones(oo.size, np.bool_)
    first[1:] = oo[1:] != oo[:-1]
    pick = order[first]
    pos, ro = sel[pick], so_[pick]
    rs = s[pos]
    ch = cand._top_owners(rs, ro, limit)
    nr = r_o.size
    all_o = np.concatenate((r_o, ro[ch]))
    all_s = np.concatenate((r_s, rs[ch].astype(np.float64)))
    all_seq = np.concatenate((np.full(nr, -1, np.int64), pos[ch]))
    neg = -all_s
    order = np.lexsort((all_seq, neg, all_o))
    so = all_o[order]
    first = np.ones(so.size, np.bool_)
    first[1:] = so[1:] != so[:-1]
    u = order[first]
    fin = u[np.lexsort((all_o[u], neg[u]))][:limit]
    return all_o[fin], all_s[fin], np.where(fin < nr, fin, -1), np.where(fin < nr, -1, all_seq[fin])


def fixture(shape: str, rng):
    """(scores, owners) per chunk; owners in contiguous runs of 1-30 rows. Ids increase along
    the scan, or are a random permutation of the runs ("_shuffled", runs still contiguous)."""
    lens = rng.integers(1, 31, CH * N_CHUNKS)
    ids = np.arange(1, lens.size + 1)
    if shape.endswith("_shuffled"):
        ids = rng.permutation(ids)
        shape = shape.removesuffix("_shuffled")
    owner = np.repeat(ids, lens)[: CH * N_CHUNKS].astype(np.int64)
    if shape == "plateau":  # 4,000 identical vectors per chunk: one score, above any T
        s = np.full(owner.size, np.float32(0.8125), np.float32)
    elif shape == "plateau_runs":  # identical vectors in every 2nd owner, others lower
        s = np.where(owner % 2 == 0, np.float32(0.8125), rng.random(owner.size).astype(np.float32) * 0.8)
        s = s.astype(np.float32)
    else:  # realistic: continuous scores
        s = (rng.standard_normal(owner.size) * 0.1 + 0.3).astype(np.float32)
    return [(s[k * CH:(k + 1) * CH], owner[k * CH:(k + 1) * CH]) for k in range(N_CHUNKS)]


def run(fn, chunks, limit):
    r_o, r_s = cand._NO_I, cand._NO_F
    trace = []
    for s, o in chunks:
        got = fn(s, o, limit, r_o, r_s)
        if got is not None:
            r_o, r_s = got[0], got[1]
            trace.append((got[0].tobytes(), got[1].tobytes(), got[2].tobytes(), got[3].tobytes()))
        else:
            trace.append(None)
    return trace


def bench(fn, chunks, limit, reps=15):
    best = 1e9
    for _ in range(reps):
        t = time.perf_counter()
        run(fn, chunks, limit)
        best = min(best, time.perf_counter() - t)
    return best / (CH * N_CHUNKS) * 1e9


def main() -> None:
    rng = np.random.default_rng(7)
    lines = []
    for shape in ("plateau", "plateau_shuffled", "plateau_runs", "plateau_runs_shuffled",
                  "continuous", "continuous_shuffled"):
        chunks = fixture(shape, rng)
        for limit in (30, 100, 200):
            same = run(cand.select_passages, chunks, limit) == run(lexsort_select, chunks, limit)
            a = bench(cand.select_passages, chunks, limit)
            b = bench(lexsort_select, chunks, limit)
            surv = "all rows survive" if shape.startswith("plateau") and "runs" not in shape else ""
            lines.append(f"{shape:22s} limit={limit:3d} C3_runlevel={a:6.1f} ns/row"
                         f"  survivor_lexsort={b:6.1f} ns/row  ratio={b / a:4.1f}x"
                         f"  identical={same} {surv}")  # fmt: skip
            print(lines[-1], flush=True)
    # The fallback path (an owner split into two runs inside a chunk) on the plateau.
    chunks = fixture("plateau_shuffled", rng)
    split = [(s, np.concatenate((o[2000:], o[:2000]))) for s, o in chunks]
    same = run(cand.select_passages, split, 100) == run(lexsort_select, split, 100)
    lines.append(f"plateau, owners re-ordered so runs repeat (fallback lexsort path) limit=100:"
                 f" C3={bench(cand.select_passages, split, 100):6.1f} ns/row"
                 f" lexsort={bench(lexsort_select, split, 100):6.1f} ns/row identical={same}")  # fmt: skip
    print(lines[-1])
    (HERE / "plateau_bench.txt").write_text("\n".join(lines) + "\n", "utf-8")


if __name__ == "__main__":
    main()
