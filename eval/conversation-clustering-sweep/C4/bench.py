"""C4 side bench: memo vs no memo, low- vs high-cardinality `sources`, tuples vs _Segment.

    ..\\.venv\\Scripts\\python.exe C4\\bench.py   (from conversation-clustering-sweep)
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys
import time
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import fixtures  # noqa: E402
import oracle  # noqa: E402
from ref_today import _Segment  # noqa: E402


def load_variant(name, replace=None):
    src = (HERE / "cand.py").read_text()
    if replace:
        assert replace[0] in src
        src = src.replace(*replace)
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    exec(compile(src, name, "exec"), mod.__dict__)
    return mod


def bench(fn, rows, games, horizon, reps=15):
    fn(list(rows), games, horizon)
    s = []
    for _ in range(reps):
        r = list(rows)
        t = time.perf_counter()
        fn(r, games, horizon)
        s.append((time.perf_counter() - t) * 1e3)
    s.sort()
    return statistics.median(s), s[int(0.95 * (len(s) - 1) + 0.999)]


def high_cardinality(rows):
    """Same rows, each sources list gets a unique non-voice, non-game process: same semantics,
    every raw string distinct."""
    out = []
    for r in rows:
        src = json.loads(r[5]) + [f"proc{r[0]}.exe"]
        out.append((*r[:5], json.dumps(src), r[6]))
    return out


def segment_build(rows):
    """Cost of materialising _Segment objects that share the memoised tuple."""
    memo = {}
    segs = []
    for r in rows:
        src = memo.get(r[5])
        if src is None:
            src = memo[r[5]] = tuple(str(x) for x in json.loads(r[5]))
        segs.append(_Segment(r[0], r[1], r[2], r[3], r[4], src, r[6]))
    return segs


def main():
    memo_mod = load_variant("c4_memo")
    nomemo_mod = load_variant("c4_nomemo", ("memo[raw] = m", "pass"))
    rows, games, horizon = fixtures.realistic()
    hi = high_cardinality(rows)
    lines = []
    for label, data in (("low_card", rows), ("high_card", hi)):
        want = oracle.clusters(data, games, horizon)
        for vname, mod in (("memo", memo_mod), ("nomemo", nomemo_mod)):
            got = mod.clusters(list(data), games, horizon)
            ok = got == want
            med, p95 = bench(mod.clusters, data, games, horizon)
            distinct = len({r[5] for r in data})
            lines.append(
                f"{label} distinct_sources={distinct} variant={vname} "
                f"median_ms={med:.3f} p95_ms={p95:.3f} equals_oracle={ok}"
            )
    seg_med, seg_p95 = bench(lambda r, g, h: segment_build(r), rows, games, horizon)
    lines.append(f"extra_cost_if_rows_become_Segment_objects median_ms={seg_med:.3f} "
                 f"p95_ms={seg_p95:.3f} (low_card, shared tuples)")
    print("\n".join(lines))
    (HERE / "bench.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
