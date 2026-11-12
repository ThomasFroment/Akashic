"""Mismatch breakdown for C3 by the longest non-voice mic segment in the case (<= 300 s vs > 300 s).

    python C3/breakdown.py   (from conversation-clustering-sweep)

Sets: harness fuzz seeds 0..9999, edge sets, realistic_8h, replay seeds (one-shot call),
extended fuzz seeds 10,000..209,999, and an adversarial generator that puts mic segments of
1..300,000 ms (and a control band 300,001..400,000 ms) across voice gaps of 300,001..330,000 ms.
"""

from __future__ import annotations

import json
import pathlib
import random
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import cand  # noqa: E402
import fixtures  # noqa: E402
import oracle  # noqa: E402
from ref_today import _is_voice, _segment  # noqa: E402

LIMIT = 300_000


def max_mic_len(rows, games):
    best = 0
    for r in rows:
        if r[1] == "mic" and not _is_voice(_segment(r), games):
            best = max(best, r[3] - r[2])
    return best


def same(rows, games, horizon):
    return oracle.clusters(list(rows), list(games), horizon) == cand.clusters(
        list(rows), list(games), horizon
    )


def tally(name, cases, stats):
    for rows, games, horizon in cases:
        bucket = "le300" if max_mic_len(rows, games) <= LIMIT else "gt300"
        key = (name, bucket)
        n, bad = stats.get(key, (0, 0))
        stats[key] = (n + 1, bad + (not same(rows, games, horizon)))


def adversarial(seed):
    rng = random.Random(seed)
    rows = []
    nid = 1
    t = fixtures.T0
    bounds = []
    for _ in range(rng.randint(2, 4)):
        vs = t
        for _ in range(rng.randint(1, 4)):
            d = rng.randint(1, 30_000)
            rows.append(fixtures._row(nid, "system", t, t + d, ["discord.exe"]))
            nid += 1
            t += d + rng.randint(0, 20_000)
        ve = max(r[3] for r in rows if r[2] >= vs)
        bounds.append((vs, ve))
        t = ve + rng.randint(300_001, 330_000)
    long_band = rng.random() < 0.2
    for _ in range(rng.randint(1, 6)):
        k = rng.randrange(len(bounds) - 1)
        ve, vs_next = bounds[k][1], bounds[k + 1][0]
        length = rng.randint(300_001, 400_000) if long_band else rng.randint(1, LIMIT)
        mid = rng.choice([(ve + vs_next) // 2, ve, vs_next, rng.randint(ve - 60_000, vs_next + 60_000)])
        start = mid - length // 2 + rng.choice([0, 1, -1, rng.randint(-40_000, 40_000)])
        rows.append(fixtures._row(nid, "mic", start, start + length, []))
        nid += 1
    rows.sort(key=lambda r: (r[2], r[0]))
    horizon = max(r[3] for r in rows) + rng.choice([0, 10 * fixtures.GAP])
    return rows, [], horizon


def main():
    stats: dict[tuple[str, str], tuple[int, int]] = {}
    tally("fuzz 0..9999", (fixtures.random_case(s) for s in range(10_000)), stats)
    tally("edges", fixtures.edge_sets().values(), stats)
    tally("realistic_8h", [fixtures.realistic()], stats)
    tally(
        "replay one-shot",
        ((r, g, 10**15) for r, g in (fixtures.replay_case(s) for s in range(1_000))),
        stats,
    )
    tally("fuzz 10k..210k", (fixtures.random_case(s) for s in range(10_000, 210_000)), stats)
    tally("adversarial 50k", (adversarial(s) for s in range(50_000)), stats)
    edge_bad = [
        n for n, c in fixtures.edge_sets().items() if not same(*c)
    ]
    lines = []
    for (name, bucket), (n, bad) in sorted(stats.items()):
        lines.append(f"{name:18s} mic<=300s: " if bucket == "le300" else f"{name:18s} mic>300s:  ")
        lines[-1] += f"{bad}/{n} mismatches"
    lines.append("edge mismatches: " + (", ".join(edge_bad) or "none"))
    print("\n".join(lines))
    print(json.dumps({f"{a}|{b}": v for (a, b), v in stats.items()}))


if __name__ == "__main__":
    main()
