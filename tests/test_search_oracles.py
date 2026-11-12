"""Exact vector searches, the windowed episode scan and identical screen text, against oracles.

Today's ``passages.top``, ``embeddings.top`` and ``hybrid._ranked_episodes`` read rows from an
encrypted test store; the oracles in ``oracles.py`` read the same rows with plain SQL and rank
them with a float64 brute force. A rewrite of the scans (#100, #104) or of screen passage
storage (#101) must keep every passing test here green.

Fuzz vectors use dyadic components (multiples of 1/4 in [-1, 1], 16 dimensions): every product
and partial sum is exact in float16, float32 and float64 whatever the summation order, so scores
match bit for bit and ties are real ties. A separate case uses realistic random unit vectors with
a tolerance.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

import numpy as np
import pytest
from defects import known_defect
from oracles import HashEmbedder, Ranked, embedding_ranking, passage_ranking, ranking_problems

from akashic.pipeline.embed import EMBED, EmbedHandler, enqueue_missing
from akashic.pipeline.jobs import JobQueue
from akashic.search import hybrid
from akashic.search.hybrid import VECTOR_DEPTH, search_memory
from akashic.search.planner import Plan
from akashic.search.timeparse import TimeRange
from akashic.storage import embeddings, passages
from akashic.storage.media import MediaStore
from akashic.storage.retention import expire_embeddings, expire_frame_text

if TYPE_CHECKING:
    from sqlite3 import Connection

    from numpy.typing import NDArray

    from akashic.config import Paths

    type Vector = NDArray[np.float32]

DIM: Final = 16
LEVELS: Final = np.array([-1, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1], dtype=np.float32)
PROCESSES: Final = ("zen.exe", "ZEN.EXE", "chrome.exe", "discord.exe", None)
NAN: Final = float("nan")


def dyadic(rng: np.random.Generator, dim: int = DIM) -> Vector:
    return rng.choice(LEVELS, size=dim).astype(np.float32)


def blob(vector: Vector | list[float]) -> bytes:
    return np.asarray(vector, dtype=np.float16).tobytes()


def add_owner(  # noqa: PLR0913 - a source row of either kind
    conn: Connection,
    kind: str,
    owner: int,
    ts: int,
    *,
    text: str | None = "texte de la source",
    process: str | None = "zen.exe",
    title: str = "Window",
    url: str | None = None,
) -> None:
    if kind == "segment":
        conn.execute(
            "INSERT INTO audio_segments (id, stream, start_ms, end_ms, speech_ms, status,"
            " transcript) VALUES (?, 'system', ?, ?, 1000, 'done', ?)",
            (owner, ts, ts + 1000, text),
        )
    else:
        conn.execute(
            "INSERT INTO frames (id, ts_ms, monitor, process, window_title, url, text,"
            " text_source, trigger) VALUES (?, ?, 0, ?, ?, ?, ?, 'uia', 'idle')",
            (owner, ts, process, title, url, text),
        )


def add_passage(  # noqa: PLR0913 - one passages row
    conn: Connection, kind: str, owner: int, start_char: int, vector: Vector, *, model: str = "m"
) -> None:
    table = "segment_passages" if kind == "segment" else "frame_passages"
    conn.execute(
        f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)",  # noqa: S608 - literal table
        (owner, model, start_char, start_char + 400, len(vector), blob(vector)),
    )


def add_vector(  # noqa: PLR0913 - one embeddings row
    conn: Connection,
    owner_kind: str,
    owner: int,
    vector: Vector,
    start_ms: int,
    *,
    model: str = "m",
) -> None:
    conn.execute(
        "INSERT INTO embeddings VALUES (?, ?, ?, ?, ?, ?, 0)",
        (owner_kind, owner, model, len(vector), blob(vector), start_ms),
    )


def matches(found: list[passages.Match]) -> list[tuple[int, float, int, int]]:
    return [(m.owner_id, m.score, m.start_char, m.end_char) for m in found]


def expected(full: list[Ranked], limit: int) -> list[tuple[int, float, int, int]]:
    return [(r.owner_id, r.score, r.start_char, r.end_char) for r in full[:limit]]


# --- passages: per-owner max top-k -----------------------------------------------------------


def passage_store(conn: Connection, rng: np.random.Generator, kind: str) -> list[int]:
    """40 owners with 0-6 passages each: shared vectors within and across owners (ties), rows of
    another size and another model, sources without text, times on the window edges."""
    owners = [int(o) for o in rng.choice(np.arange(1, 10_000), size=40, replace=False)]
    pool = [dyadic(rng) for _ in range(4)]
    for owner in owners:
        ts = int(rng.choice([0, 4_999, 5_000, 9_999, int(rng.integers(0, 10_000))]))
        add_owner(
            conn, kind, owner, ts,
            text=None if rng.random() < 0.1 else "texte",
            process=PROCESSES[int(rng.integers(len(PROCESSES)))],
        )  # fmt: skip
        own = dyadic(rng)
        for index in range(int(rng.integers(0, 7))):
            draw = rng.random()
            if draw < 0.08:
                vector = dyadic(rng, DIM // 2)
            elif draw < 0.25:
                vector = own
            elif draw < 0.45:
                vector = pool[int(rng.integers(len(pool)))]
            else:
                vector = dyadic(rng)
            add_passage(
                conn, kind, owner, index * 300, vector, model="other" if draw > 0.95 else "m"
            )
    return owners


@pytest.mark.parametrize("kind", ["segment", "frame"])
def test_passage_top_matches_brute_force(
    conn: Connection, monkeypatch: pytest.MonkeyPatch, kind: passages.Kind
) -> None:
    mismatches: list[str] = []
    busy = 0
    for seed in range(80):
        rng = np.random.default_rng(seed)
        for table in ("segment_passages", "frame_passages", "audio_segments", "frames"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 - literal table names
        owners = passage_store(conn, rng, kind)
        monkeypatch.setattr(embeddings, "CHUNK_ROWS", int(rng.choice([1, 2, 5, 4_000])))
        query = dyadic(rng)
        start = int(rng.choice([0, 5_000, int(rng.integers(0, 10_000))]))
        end = int(rng.choice([10_000, 5_000, start, int(rng.integers(start, 10_001))]))
        only = None if rng.random() < 0.5 else {*rng.choice(owners, size=15).tolist(), 10_001}
        limit = int(rng.choice([1, 3, 10, 100]))
        apps: tuple[str, ...] = ()
        if kind == "frame":
            apps = [(), ("zen",), ("ZEN.EXE",), ("chrome", "Discord.exe")][int(rng.integers(4))]
        got = passages.top(
            conn, "m", query, start_ms=start, end_ms=end, only=only, limit=limit, kind=kind,
            apps=apps,
        )  # fmt: skip
        full = passage_ranking(
            conn, "m", query, start_ms=start, end_ms=end, only=only, kind=kind, apps=apps
        )
        busy += len(got) >= 2
        if matches(got) != expected(full, limit):
            mismatches.append(f"seed {seed}: {matches(got)} != {expected(full, limit)}")
    assert not mismatches, mismatches[:2]
    assert busy >= 15  # the fuzz is not vacuous


def test_passage_top_on_realistic_unit_vectors(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """256-dimension random unit vectors: float16 storage, float32 scores, chunk boundaries."""
    rng = np.random.default_rng(7)
    for owner in range(1, 801):
        add_owner(conn, "segment", owner, owner)
        for index in range(int(rng.integers(1, 6))):
            vector = rng.standard_normal(256).astype(np.float32)
            add_passage(conn, "segment", owner, index * 300, vector / np.linalg.norm(vector))
    query = rng.standard_normal(256).astype(np.float32)
    query /= np.linalg.norm(query)
    for chunk in (4_000, 701):
        monkeypatch.setattr(embeddings, "CHUNK_ROWS", chunk)
        for limit in (1, 30, 100):
            got = passages.top(conn, "m", query, start_ms=0, end_ms=10_000, limit=limit)
            full = passage_ranking(conn, "m", query, start_ms=0, end_ms=10_000)
            problems = ranking_problems([(m.owner_id, m.score) for m in got], full, limit, tol=1e-5)
            assert not problems, (chunk, limit, problems[:3])


def test_passage_edges_empty_store_limits_and_unknown_owners(conn: Connection) -> None:
    query = np.ones(DIM, dtype=np.float32)
    assert passages.top(conn, "m", query, start_ms=0, end_ms=10) == []
    add_owner(conn, "segment", 1, 5)
    add_passage(conn, "segment", 1, 0, np.full(DIM, 0.25, dtype=np.float32))
    assert passages.top(conn, "m", query, start_ms=0, end_ms=10, only=[]) == []
    assert passages.top(conn, "m", query, start_ms=0, end_ms=10, only=[2, 3]) == []
    assert passages.top(conn, "m", query, start_ms=0, end_ms=5) == []  # end is exclusive
    found = passages.top(conn, "m", query, start_ms=5, end_ms=6, limit=1_000)
    assert matches(found) == [(1, 4.0, 0, 400)]


def test_passage_best_window_of_an_owner_survives_pruning_across_chunks(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With one row per chunk and limit 1, an owner whose first passages lose every chunk and
    whose last passage wins must still be returned with that passage's offsets."""
    monkeypatch.setattr(embeddings, "CHUNK_ROWS", 1)
    query = np.zeros(DIM, dtype=np.float32)
    query[0] = 1
    for owner, scores in ((1, (0.5,)), (2, (0.25, 0.25, 0.75))):
        add_owner(conn, "segment", owner, owner)
        for index, score in enumerate(scores):
            vector = np.zeros(DIM, dtype=np.float32)
            vector[0] = score
            add_passage(conn, "segment", owner, index * 300, vector)
    found = passages.top(conn, "m", query, start_ms=0, end_ms=10, limit=1)
    assert matches(found) == [(2, 0.75, 600, 1000)]


def test_equal_scores_rank_by_owner_and_keep_the_first_passage(conn: Connection) -> None:
    query = np.ones(DIM, dtype=np.float32)
    same = np.full(DIM, 0.5, dtype=np.float32)
    for owner in (30, 10, 20):
        add_owner(conn, "frame", owner, owner)
        add_passage(conn, "frame", owner, 0, same)
        add_passage(conn, "frame", owner, 300, same)
    found = passages.top(conn, "m", query, start_ms=0, end_ms=100, limit=2, kind="frame")
    assert matches(found) == [(10, 8.0, 0, 400), (20, 8.0, 0, 400)]


def test_float16_storage_rounds_scores(conn: Connection) -> None:
    """Scores come from the stored float16 values, not from the float32 vector published."""
    text = "x" * 50
    add_owner(conn, "segment", 1, 1, text=text)
    vector = np.full(DIM, 0.1, dtype=np.float32)
    assert passages.publish(conn, 1, "m", text, [vector])
    query = np.ones(DIM, dtype=np.float32)
    (hit,) = passages.top(conn, "m", query, start_ms=0, end_ms=10)
    stored = float(np.float16(0.1)) * DIM
    assert hit.score == pytest.approx(stored, abs=1e-6)
    assert abs(hit.score - 0.1 * DIM) > 1e-4


def test_rows_of_another_size_or_model_are_never_compared(conn: Connection) -> None:
    add_owner(conn, "segment", 1, 1)
    add_passage(conn, "segment", 1, 0, np.ones(DIM // 2, dtype=np.float32))
    add_passage(conn, "segment", 1, 300, np.ones(DIM, dtype=np.float32), model="other")
    add_vector(conn, "segment", 1, np.ones(DIM // 2, dtype=np.float32), 1)
    query = np.ones(DIM, dtype=np.float32)
    assert passages.top(conn, "m", query, start_ms=0, end_ms=10) == []
    assert embeddings.top(conn, "segment", "m", query) == []


@known_defect(100, "study finding: a NaN first passage hides the owner's finite passages")
def test_a_nan_passage_does_not_hide_the_owners_best_passage(conn: Connection) -> None:
    query = np.ones(DIM, dtype=np.float32)
    add_owner(conn, "segment", 1, 1)
    add_passage(conn, "segment", 1, 0, np.full(DIM, NAN, dtype=np.float32))
    add_passage(conn, "segment", 1, 300, np.full(DIM, 0.5, dtype=np.float32))
    add_owner(conn, "segment", 2, 2)
    add_passage(conn, "segment", 2, 0, np.full(DIM, 0.25, dtype=np.float32))
    found = passages.top(conn, "m", query, start_ms=0, end_ms=10)
    assert matches(found) == [(1, 8.0, 300, 700), (2, 4.0, 0, 400)]


@known_defect(100, "study finding: a NaN score in scope corrupts the order of finite scores")
def test_a_nan_vector_does_not_corrupt_the_ranking(conn: Connection) -> None:
    query = np.ones(DIM, dtype=np.float32)
    for owner, value in enumerate((0.25, NAN, 0.75, 0.5), start=1):
        add_vector(conn, "episode", owner, np.full(DIM, value, dtype=np.float32), owner)
    found = embeddings.top(conn, "episode", "m", query)
    finite = [s.owner_id for s in found if math.isfinite(s.score)]
    assert finite == [3, 4, 1]


# --- embeddings: one vector per owner --------------------------------------------------------


def test_embedding_top_matches_brute_force(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mismatches: list[str] = []
    busy = 0
    for seed in range(60):
        rng = np.random.default_rng(1_000 + seed)
        conn.execute("DELETE FROM embeddings")
        pool = [dyadic(rng) for _ in range(3)]
        owners = [int(o) for o in rng.choice(np.arange(1, 10_000), size=60, replace=False)]
        for owner in owners:
            kind = str(rng.choice(["episode", "segment", "query"], p=[0.6, 0.3, 0.1]))
            draw = rng.random()
            vector = (
                dyadic(rng, DIM // 2) if draw < 0.05
                else pool[int(rng.integers(3))] if draw < 0.4
                else dyadic(rng)
            )  # fmt: skip
            start = int(rng.choice([0, 4_999, 5_000, int(rng.integers(0, 10_000))]))
            add_vector(
                conn, kind, owner, vector, start, model="other" if rng.random() < 0.05 else "m"
            )
        monkeypatch.setattr(embeddings, "CHUNK_ROWS", int(rng.choice([1, 3, 7, 4_000])))
        query = dyadic(rng)
        start = int(rng.choice([0, 5_000]))
        end = int(rng.choice([2**62, 5_000, 10_000]))
        only = None if rng.random() < 0.5 else set(rng.choice(owners, size=30).tolist())
        exclude = set(rng.choice(owners, size=int(rng.integers(0, 10))).tolist())
        limit = int(rng.choice([1, 5, 30, 100]))
        kind = str(rng.choice(["episode", "segment"]))
        got = embeddings.top(
            conn, kind, "m", query, start_ms=start, end_ms=end, only=only, exclude=exclude,
            limit=limit,
        )  # fmt: skip
        full = embedding_ranking(
            conn, kind, "m", query, start_ms=start, end_ms=end, only=only, exclude=exclude
        )
        problems = ranking_problems([(s.owner_id, s.score) for s in got], full, limit, tol=0.0)
        busy += len(got) >= 2
        if problems:
            mismatches.append(f"seed {seed}: {problems[:3]}")
    assert not mismatches, mismatches[:2]
    assert busy >= 15


def test_embedding_edges(conn: Connection) -> None:
    query = np.ones(DIM, dtype=np.float32)
    assert embeddings.top(conn, "episode", "m", query) == []
    add_vector(conn, "episode", 1, np.full(DIM, 0.5, dtype=np.float32), 100)
    add_vector(conn, "episode", 2, np.full(DIM, 0.25, dtype=np.float32), 200)
    assert embeddings.top(conn, "episode", "m", query, only=()) == []
    assert embeddings.top(conn, "episode", "m", query, exclude=(1, 2)) == []
    assert [s.owner_id for s in embeddings.top(conn, "episode", "m", query, limit=500)] == [1, 2]
    assert [s.owner_id for s in embeddings.top(conn, "episode", "m", query, end_ms=200)] == [1]
    assert [s.owner_id for s in embeddings.top(conn, "episode", "m", query, start_ms=200)] == [2]
    assert embeddings.top(conn, "segment", "m", query) == []


# --- the windowed episode vector step (#104) -------------------------------------------------


def add_episode(conn: Connection, episode: int, start: int, end: int) -> None:
    conn.execute(
        "INSERT INTO episodes (id, kind, ref, start_ms, end_ms, title, body)"
        " VALUES (?, 'video', ?, ?, ?, 'Title', '')",
        (episode, f"video:{episode}", start, end),
    )


def ranked_episodes(
    conn: Connection, start: int, end: int, query: Vector
) -> list[tuple[int, float]]:
    plan = Plan(time=TimeRange(start, end, "window"))
    scope = hybrid._Scope(plan, query, "m", start, end, frozenset())
    return hybrid._ranked_episodes(conn, scope)


def episode_problems(
    conn: Connection, start: int, end: int, query: Vector, got: list[tuple[int, float]]
) -> list[str]:
    """Candidates overlap the window (``start < end_w and end > start_w``); the vector-ranked
    ones come first with reciprocal-rank scores, the rest follow oldest first."""
    rows = conn.execute("SELECT id, start_ms, end_ms FROM episodes").fetchall()
    overlap = {int(i): (int(s), int(i)) for i, s, e in rows if s < end and e > start}
    full = embedding_ranking(conn, "episode", "m", query, only=set(overlap))
    scored = [(i, s) for i, s in got if s > 0]
    oracle_scores = {r.owner_id: r.score for r in full}
    problems = ranking_problems(
        [(i, oracle_scores.get(i, math.nan)) for i, _ in scored],
        full, VECTOR_DEPTH, tol=0.0,
    )  # fmt: skip
    if [s for _, s in scored] != [1 / (hybrid.RRF_K + n) for n in range(1, len(scored) + 1)]:
        problems.append("fused scores are not reciprocal ranks")
    rest = sorted(set(overlap) - {i for i, _ in scored}, key=lambda i: overlap[i])
    if [i for i, s in got if s == 0] != rest:
        problems.append(f"unscored {[i for i, s in got if s == 0]} != {rest}")
    return problems


def test_windowed_episode_scan_uses_overlap_at_every_edge(conn: Connection) -> None:
    window = (10_000, 20_000)
    layout = {
        1: (5_000, 12_000),  # starts before, ends inside
        2: (15_000, 25_000),  # starts inside, ends after
        3: (5_000, 25_000),  # spans the window
        4: (12_000, 13_000),  # inside
        5: (5_000, 10_000),  # ends exactly at the start: out
        6: (20_000, 25_000),  # starts exactly at the end: out
        7: (10_000, 11_000),  # starts exactly at the start
        8: (19_000, 20_000),  # ends exactly at the end
        9: (1_000, 2_000),  # before
        10: (30_000, 31_000),  # after
        11: (10_000, 10_000),  # zero length at the start: out
        12: (0, 10**9),  # began long before, still open long after
    }
    query = np.zeros(DIM, dtype=np.float32)
    query[0] = 1
    for episode, (start, end) in layout.items():
        add_episode(conn, episode, start, end)
        vector = np.zeros(DIM, dtype=np.float32)
        vector[0] = episode / 16
        add_vector(conn, "episode", episode, vector, start)
    add_episode(conn, 13, 11_000, 12_000)  # no vector yet: a candidate without a score
    add_vector(conn, "episode", 14, np.ones(DIM, dtype=np.float32), 11_000)  # deleted episode
    got = ranked_episodes(conn, *window, query)
    assert [i for i, _ in got] == [12, 8, 7, 4, 3, 2, 1, 13]
    assert not episode_problems(conn, *window, query, got)


def test_windowed_episode_scan_matches_the_oracle(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    mismatches: list[str] = []
    busy = 0
    for seed in range(30):
        rng = np.random.default_rng(2_000 + seed)
        conn.execute("DELETE FROM episodes")
        conn.execute("DELETE FROM embeddings")
        pool = [dyadic(rng) for _ in range(3)]
        edges: list[int] = []
        for episode in range(1, 181):
            start = int(rng.integers(0, 100_000))
            end = start + int(rng.choice([0, 1, 1_000, 10_000, 50_000]))
            edges += [start, end]
            add_episode(conn, episode, start, end)
            draw = rng.random()
            if draw < 0.1:
                continue
            vector = pool[int(rng.integers(3))] if draw < 0.4 else dyadic(rng)
            add_vector(
                conn, "episode", episode, vector, start, model="other" if draw > 0.95 else "m"
            )
        monkeypatch.setattr(embeddings, "CHUNK_ROWS", int(rng.choice([1, 7, 4_000])))
        start = int(rng.choice([*rng.choice(edges, size=2), 0]))
        end = int(rng.choice([start + 1, int(rng.choice(edges)) + 1, 200_000]))
        end = max(end, start + 1)
        query = dyadic(rng)
        got = ranked_episodes(conn, start, end, query)
        problems = episode_problems(conn, start, end, query, got)
        busy += sum(score > 0 for _, score in got) >= 2
        if problems:
            mismatches.append(f"seed {seed}: {problems[:3]}")
    assert not mismatches, mismatches[:2]
    assert busy >= 10


# --- identical frame text (#101) -------------------------------------------------------------

SHARED: Final = (
    "Lecture en cours. Patch notes saison 4: nouvelle carte, équilibrage des armes. " * 12
)


def identical_frames(conn: Connection, count: int = 4) -> list[int]:
    """``count`` frames with byte-identical text at 1000, 2000, ... ms, embedded by the worker
    with the deterministic embedder."""
    for n in range(1, count + 1):
        add_owner(conn, "frame", n, n * 1_000, text=SHARED, title="Player", url="https://v.test")
    queue = JobQueue(conn)
    handler = EmbedHandler(conn, embedder := HashEmbedder(), lambda: True)
    while enqueue_missing(conn, queue, embedder.model, 1):
        job = queue.claim([EMBED], 1)
        assert job is not None
        handler.run(job)
        queue.complete(job.id, 1)
    return list(range(1, count + 1))


def frame_passages(conn: Connection) -> dict[int, list[tuple[int, int, bytes]]]:
    out: dict[int, list[tuple[int, int, bytes]]] = {}
    for frame, start, end, vec in conn.execute(
        "SELECT frame_id, start_char, end_char, vec FROM frame_passages"
        " ORDER BY frame_id, start_char"
    ):
        out.setdefault(int(frame), []).append((int(start), int(end), bytes(vec)))
    return out


def found_frames(conn: Connection, start: int, end: int) -> list[tuple[int, int]]:
    query = HashEmbedder().vector("patch notes saison carte")
    plan = Plan(time=TimeRange(start, end, "window"))
    return [(f.id, f.ts_ms) for f in search_memory(conn, plan, query, HashEmbedder.model).frames]


def test_every_frame_with_identical_text_is_findable_at_its_own_time(conn: Connection) -> None:
    frames = identical_frames(conn)
    stored = frame_passages(conn)
    windows = passages.windows(SHARED)
    assert set(stored) == set(frames)
    assert all([(s, e) for s, e, _ in stored[f]] == windows for f in frames)
    for frame in frames:
        assert found_frames(conn, frame * 1_000, frame * 1_000 + 1) == [(frame, frame * 1_000)]
    assert sorted(found_frames(conn, 0, 10_000)) == [(f, f * 1_000) for f in frames]


@pytest.mark.parametrize("removal", ["delete-row", "change-text", "expire-text", "expire-vectors"])
def test_removing_one_identical_frame_removes_exactly_its_passages(
    conn: Connection, paths: Paths, removal: str
) -> None:
    frames = identical_frames(conn)
    before = frame_passages(conn)
    media = MediaStore(paths.media_dir, bytes(32))
    if removal == "delete-row":
        conn.execute("DELETE FROM frames WHERE id = 1")
    elif removal == "change-text":
        conn.execute("UPDATE frames SET text = 'autre chose' WHERE id = 1")
    elif removal == "expire-text":
        expire_frame_text(conn, media, 1_001)
    else:
        expire_embeddings(conn, media, 1_001)
    after = frame_passages(conn)
    assert after == {f: before[f] for f in frames[1:]}
    assert found_frames(conn, 0, 1_001) == []
    assert sorted(found_frames(conn, 0, 10_000)) == [(f, f * 1_000) for f in frames[1:]]


def test_identical_frames_tie_in_frame_id_order_today(conn: Connection) -> None:
    """Characterises today's tie order (``hybrid.py`` 628): equal semantic scores keep the
    passage order, which breaks ties by frame id. A collapse (#101) may change this."""
    identical_frames(conn, 3)
    assert found_frames(conn, 0, 10_000) == [(1, 1_000), (2, 2_000), (3, 3_000)]


@known_defect(101, "identical frame text fills the five frame slots, crowding out other evidence")
def test_identical_frames_do_not_crowd_out_other_screen_evidence(conn: Connection) -> None:
    query = np.zeros(DIM, dtype=np.float32)
    query[0] = 1
    for frame in range(1, 7):
        add_owner(conn, "frame", frame, frame, text=SHARED, title="Player", url="https://v.test")
        add_passage(conn, "frame", frame, 0, query)
    other = "Les sauvegardes sont sur un disque externe."
    add_owner(conn, "frame", 7, 7, text=other, title="Notes")
    add_passage(conn, "frame", 7, 0, np.array([0.75, 0.5, *([0] * (DIM - 2))], dtype=np.float32))
    found = search_memory(conn, Plan(), query, "m").frames
    groups = [(f.window_title, f.url) for f in found]
    assert 7 in [f.id for f in found]
    assert len(groups) == len(set(groups))
