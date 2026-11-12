"""Time today's scans and episode builders on invented encrypted stores (the #100-#104 baseline).

uv run python scripts/bench_baseline.py --rows 100000 --repeats 5 --output baseline.json

Builds one SQLCipher store in a temporary folder with invented rows only: ``--rows`` segment
passages (256-dimension float16 unit vectors, 1-5 per segment), ``--episodes`` episode vectors
spread over a history of 50 episodes a day, an 8-hour call of ``--segments`` audio segments (half
system voice, half microphone) and ``--visits`` app sessions. Then it times, each the median of
``--repeats`` runs:

- ``passages_top``: ``passages.top`` over every passage, limit 100 (#100).
- ``embeddings_top``: ``embeddings.top`` over every episode vector, limit 100 (#100).
- ``windowed_episode_vectors``: the episode vector step of a one-day question, as
  ``hybrid._ranked_episodes`` runs it (candidates by overlap, then ``embeddings.top``) (#104);
  ``windowed_ranked_episodes`` is the whole ranking.
- ``conversation_clusters``: one ``_conversation_clusters`` pass over the call (#102).
- ``app_runs``: one ``build_app_runs`` pass over every visit, episode writes included (#103).
- ``passage_publish``: ``passages.publish`` of 200 segments of ~2,000 characters (#101).

No model, GPU, audio device, network or real history is used. Numbers depend on the machine
and its load; compare runs made on the same PC.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import secrets
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

import numpy as np

from akashic.pipeline import episodes
from akashic.search import hybrid
from akashic.search.planner import Plan
from akashic.search.timeparse import TimeRange
from akashic.storage import db, embeddings, passages

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

DIMS: Final = 256
MODEL: Final = "bench"
DAY_MS: Final = 86_400_000
T0: Final = 1_700_000_000_000
EPISODES_PER_DAY: Final = 50
PUBLISH_OWNERS: Final = 200
PUBLISH_CHARS: Final = 2_000
FAR: Final = 2**62
SWITCH_CHANCE: Final = 0.1  # a visit changes program


def unit_rows(rng: np.random.Generator, count: int) -> NDArray[np.float16]:
    vectors = rng.standard_normal((count, DIMS), dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors.astype(np.float16)


def unit_query(rng: np.random.Generator) -> Vector:
    query = rng.standard_normal(DIMS, dtype=np.float32)
    return (query / np.linalg.norm(query)).astype(np.float32)


def populate_passages(conn: Connection, rng: np.random.Generator, rows: int) -> int:
    """``rows`` passages over segments of 1-5 passages each; returns the segment count."""
    counts: list[int] = []
    while sum(counts) < rows:
        counts.append(min(int(rng.integers(1, 6)), rows - sum(counts)))
    vectors = unit_rows(rng, rows)
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO audio_segments (id, stream, start_ms, end_ms, speech_ms, status,"
            " transcript) VALUES (?, 'system', ?, ?, 4000, 'done', 'texte inventé')",
            [(n, T0 + n * 5_000, T0 + n * 5_000 + 4_000) for n in range(1, len(counts) + 1)],
        )
        at = 0
        batch: list[tuple[int, str, int, int, int, bytes]] = []
        for owner, count in enumerate(counts, start=1):
            for index in range(count):
                start = index * passages.STEP
                batch.append(
                    (owner, MODEL, start, start + passages.CHARS, DIMS, vectors[at].tobytes())
                )
                at += 1
        conn.executemany("INSERT INTO segment_passages VALUES (?, ?, ?, ?, ?, ?)", batch)
    return len(counts)


def populate_episodes(conn: Connection, rng: np.random.Generator, count: int) -> None:
    """``count`` episodes, ``EPISODES_PER_DAY`` a day, 1-90 minutes long, each with a vector."""
    vectors = unit_rows(rng, count)
    rows: list[tuple[int, str, int, int]] = []
    for n in range(1, count + 1):
        start = T0 + (n - 1) * DAY_MS // EPISODES_PER_DAY
        rows.append((n, f"video:{n}", start, start + int(rng.integers(1, 91)) * 60_000))
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO episodes (id, kind, ref, start_ms, end_ms, title, body)"
            " VALUES (?, 'video', ?, ?, ?, 'Titre', '')",
            rows,
        )
        conn.executemany(
            "INSERT INTO embeddings VALUES ('episode', ?, ?, ?, ?, ?, 0)",
            [(n, MODEL, DIMS, vectors[n - 1].tobytes(), start) for n, _, start, _ in rows],
        )


def populate_call(conn: Connection, rng: random.Random, count: int) -> int:
    """An 8-hour Discord evening: system voice and microphone speech of 0.4-30 s, three breaks
    over five minutes. Ids follow ``--rows`` segments; returns a horizon after the call."""
    base = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM audio_segments").fetchone()[0])
    start = T0 + 400 * DAY_MS
    span = 8 * 3_600_000
    breaks = sorted(rng.sample(range(span), 3))
    rows: list[tuple[int, str, int, int, str]] = []
    for n in range(count):
        stream = "system" if n % 2 == 0 else "mic"
        at = start + n * span // count + rng.randrange(0, 1_000)
        at += sum(10 * 60_000 for b in breaks if b < at - start)
        length = int(min(30_000, max(400, rng.lognormvariate(7.6, 0.6))))
        sources = '["discord.exe"]' if stream == "system" else "[]"
        rows.append((base + n + 1, stream, at, at + length, sources))
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO audio_segments (id, stream, start_ms, end_ms, speech_ms, sources,"
            " status, transcript) VALUES (?, ?, ?, ?, 1000, ?, 'done', 'on parle du jeu')",
            rows,
        )
    return max(r[3] for r in rows) + 60_000


def populate_visits(conn: Connection, rng: random.Random, count: int) -> None:
    """``count`` app sessions of 1 s to 10 min, mostly back to back with an occasional idle gap
    over five minutes; the program changes on one visit in ten (browser or work)."""
    processes = ("zen.exe", "chrome.exe", "code.exe", "windowsterminal.exe", "explorer.exe")
    titles = [f"Fenêtre {n}" for n in range(200)]
    t = T0
    process = processes[0]
    rows: list[tuple[int, str, str, str | None, int, int]] = []
    for n in range(1, count + 1):
        t += rng.choices((0, 1_000, 30_000, 120_000, 400_000), weights=(30, 40, 20, 9, 1))[0]
        length = rng.choice((1_000, 20_000, 60_000, 300_000, 600_000))
        if rng.random() < SWITCH_CHANCE:
            process = rng.choice(processes)
        url = f"https://site{n % 50}.test" if process in {"zen.exe", "chrome.exe"} else None
        rows.append((n, process, rng.choice(titles), url, t, t + length))
        t += length
    with db.transaction(conn):
        conn.executemany(
            "INSERT INTO app_sessions (id, process, window_title, url, start_ms, end_ms,"
            " frame_count, last_frame_id, closed) VALUES (?, ?, ?, ?, ?, ?, 1, 0, 1)",
            rows,
        )


def timed(
    run: Callable[[], object], repeats: int, reset: Callable[[], object] | None = None
) -> list[float]:
    """Milliseconds of each of ``repeats`` runs after one warm-up run."""
    durations: list[float] = []
    for index in range(repeats + 1):
        if reset is not None:
            reset()
        began = time.perf_counter()
        run()
        if index:
            durations.append((time.perf_counter() - began) * 1_000)
    return durations


def summary(durations: list[float], rows: int) -> dict[str, object]:
    median = statistics.median(durations)
    return {
        "median_ms": round(median, 3),
        "runs_ms": [round(d, 3) for d in durations],
        "rows": rows,
        "ns_per_row": round(median * 1e6 / rows, 1) if rows else None,
    }


def measure(conn: Connection, args: argparse.Namespace) -> dict[str, dict[str, object]]:
    rng = np.random.default_rng(args.seed)
    py_rng = random.Random(args.seed)  # noqa: S311 - invented data, not cryptography
    results: dict[str, dict[str, object]] = {}
    query = unit_query(rng)

    def passage_scan() -> object:
        return passages.top(conn, MODEL, query, start_ms=0, end_ms=FAR, limit=100)

    results["passages_top"] = summary(timed(passage_scan, args.repeats), args.rows)

    def episode_scan() -> object:
        return embeddings.top(conn, "episode", MODEL, query, limit=100)

    results["embeddings_top"] = summary(timed(episode_scan, args.repeats), args.episodes)

    middle = T0 + (args.episodes // EPISODES_PER_DAY // 2) * DAY_MS
    plan = Plan(time=TimeRange(middle, middle + DAY_MS, "one day"))
    scope = hybrid._Scope(plan, query, MODEL, middle, middle + DAY_MS, frozenset())  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    candidates = hybrid._candidates(conn, scope)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    def window_vectors() -> object:
        allowed = set(hybrid._candidates(conn, scope))  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
        return embeddings.top(
            conn, "episode", MODEL, query, only=allowed, limit=hybrid.VECTOR_DEPTH
        )

    def window_ranking() -> object:
        return hybrid._ranked_episodes(conn, scope)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    results["windowed_episode_vectors"] = summary(
        timed(window_vectors, args.repeats), args.episodes
    )
    results["windowed_episode_vectors"]["candidates"] = len(candidates)
    results["windowed_ranked_episodes"] = summary(
        timed(window_ranking, args.repeats), args.episodes
    )

    horizon = populate_call(conn, py_rng, args.segments)

    def clusters() -> object:
        return episodes._conversation_clusters(conn, horizon, 0)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    results["conversation_clusters"] = summary(timed(clusters, args.repeats), args.segments)
    final, growing = episodes._conversation_clusters(conn, horizon, 0)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    results["conversation_clusters"]["final_clusters"] = len(final)
    results["conversation_clusters"]["growing"] = growing is not None

    populate_visits(conn, py_rng, args.visits)

    def forget_runs() -> object:
        with db.transaction(conn):
            return conn.execute("DELETE FROM episodes WHERE kind IN ('browsing', 'work')")

    def app_runs() -> object:
        return episodes.build_app_runs(conn, FAR)

    results["app_runs"] = summary(timed(app_runs, args.repeats, forget_runs), args.visits)
    results["app_runs"]["episodes_built"] = int(
        conn.execute("SELECT count(*) FROM episodes WHERE kind IN ('browsing', 'work')").fetchone()[
            0
        ]
    )

    text = ("Le texte inventé d'une page web assez longue pour plusieurs passages. " * 40)[
        :PUBLISH_CHARS
    ]
    with db.transaction(conn):
        conn.executemany(
            "UPDATE audio_segments SET transcript = ? WHERE id = ?",
            [(text, n) for n in range(1, PUBLISH_OWNERS + 1)],
        )
    windows = len(passages.windows(text))
    vectors = [list(unit_rows(rng, windows).astype(np.float32)) for _ in range(PUBLISH_OWNERS)]

    def publish() -> object:
        return [
            passages.publish(conn, owner, MODEL, text, vectors[owner - 1])
            for owner in range(1, PUBLISH_OWNERS + 1)
        ]

    results["passage_publish"] = summary(timed(publish, args.repeats), PUBLISH_OWNERS * windows)
    results["passage_publish"]["owners"] = PUBLISH_OWNERS
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rows", type=int, default=100_000, help="segment passages")
    parser.add_argument("--episodes", type=int, default=40_000, help="episode vectors")
    parser.add_argument("--segments", type=int, default=20_000, help="audio segments in the call")
    parser.add_argument("--visits", type=int, default=50_000, help="app sessions")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, help="write the JSON report here too")
    args = parser.parse_args()
    if min(args.rows, args.episodes, args.segments, args.visits, args.repeats) < 1:
        parser.error("every count must be positive")
    if args.rows < PUBLISH_OWNERS * 5:
        parser.error(f"--rows must be at least {PUBLISH_OWNERS * 5}")
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "bench.db", secrets.token_bytes(32))
        try:
            db.migrate(conn)
            began = time.perf_counter()
            rng = np.random.default_rng(args.seed + 1)
            populate_passages(conn, rng, args.rows)
            populate_episodes(conn, rng, args.episodes)
            built_s = time.perf_counter() - began
            results = measure(conn, args)
        finally:
            conn.close()
    report = {
        "params": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "environment": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "platform": platform.platform(),
            "chunk_rows": embeddings.CHUNK_ROWS,
            "dims": DIMS,
            "store_build_s": round(built_s, 2),
        },
        "results": results,
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
