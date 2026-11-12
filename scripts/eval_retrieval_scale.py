"""Measure old-evidence recall and search cost on an invented encrypted episode history.

uv run python scripts/eval_retrieval_scale.py --episodes 10000 --repeats 7
No real history, model inference, audio devices or network connections are used.
"""

from __future__ import annotations

import argparse
import json
import secrets
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from akashic.search.hybrid import search_memory
from akashic.search.planner import Plan
from akashic.search.timeparse import TimeRange
from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection

DIMS = 256
DAY_MS = 86_400_000


def populate(conn: Connection, count: int) -> None:
    """The oldest episode is uniquely relevant; every episode involves the same person."""
    near = np.zeros(DIMS, dtype=np.float16)
    near[0] = 1
    far = np.zeros(DIMS, dtype=np.float16)
    far[1] = 1
    with db.transaction(conn):
        conn.execute("INSERT INTO speakers (id, name, created_ms) VALUES (1, 'Minlo', 0)")
        conn.executemany(
            "INSERT INTO episodes (id, kind, ref, start_ms, end_ms, title, body)"
            " VALUES (?, 'conversation', ?, ?, ?, ?, '')",
            [
                (
                    i,
                    str(i),
                    i * DAY_MS // count,
                    i * DAY_MS // count + 1000,
                    "survie survival" if i == 1 else "unrelated",
                )
                for i in range(1, count + 1)
            ],
        )
        conn.executemany(
            "INSERT INTO episode_people VALUES (?, 1, 1000)",
            [(i,) for i in range(1, count + 1)],
        )
        conn.executemany(
            "INSERT INTO embeddings VALUES ('episode', ?, 'synthetic', ?, ?, ?, 0)",
            [
                (i, DIMS, (near if i == 1 else far).tobytes(), i * DAY_MS // count)
                for i in range(1, count + 1)
            ],
        )


def measure(conn: Connection, mode: str, repeats: int) -> dict[str, object]:
    query = np.zeros(DIMS, dtype=np.float32)
    query[0] = 1
    plan = Plan(
        time=TimeRange(0, DAY_MS + 2000, "synthetic history"),
        people=("Minlo",),
        kinds=("conversation",),
        keywords=("survie",) if mode != "vectors" else (),
    )
    durations: list[float] = []
    recall: list[int] = []
    for _ in range(repeats):
        start = time.perf_counter()
        found = search_memory(conn, plan, query if mode != "words" else None, "synthetic", limit=10)
        durations.append((time.perf_counter() - start) * 1000)
        ids = [e.id for e in found.episodes]
        recall.append(ids.index(1) + 1 if 1 in ids else 0)
    # Instrument allocations separately: tracing significantly changes timings.
    tracemalloc.start()
    search_memory(conn, plan, query if mode != "words" else None, "synthetic", limit=10)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "mode": mode,
        "recall_at_10": sum(r > 0 for r in recall) / repeats,
        "mrr_at_10": sum(1 / r if r else 0 for r in recall) / repeats,
        "median_ms": round(statistics.median(durations), 2),
        "max_ms": round(max(durations), 2),
        "python_peak_mib": round(peak / 2**20, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episodes", type=int, default=10000)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if args.episodes < 1 or args.repeats < 1:
        parser.error("episodes and repeats must be positive")
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "scale.db", secrets.token_bytes(32))
        try:
            db.migrate(conn)
            populate(conn, args.episodes)
            print(
                json.dumps(
                    {
                        "episodes": args.episodes,
                        "repeats": args.repeats,
                        "results": [
                            measure(conn, mode, args.repeats)
                            for mode in ("words", "vectors", "hybrid")
                        ],
                    },
                    indent=2,
                )
            )
        finally:
            conn.close()


if __name__ == "__main__":
    main()
