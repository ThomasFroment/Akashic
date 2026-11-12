"""Vector storage and brute-force search."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from memory_data import NOW, add_episode, add_segment
from store_reads import embedding_count

from akashic.storage import embeddings
from akashic.storage.retention import expire_embeddings

if TYPE_CHECKING:
    from sqlite3 import Connection

    import pytest


def vec(*values: float) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
    v = np.array(values, dtype=np.float32)
    return v / np.linalg.norm(v)


def put(conn: Connection, owner_id: int, v: object, start: int = 0, kind: str = "episode") -> None:
    embeddings.put(conn, kind, owner_id, "m", np.asarray(v, dtype=np.float32), start, NOW)


def test_vectors_are_stored_as_float16(conn: Connection) -> None:
    put(conn, 1, vec(*range(1, 257)))
    blob, dim = conn.execute("SELECT vec, dim FROM embeddings").fetchone()
    assert (len(blob), dim) == (2 * 256, 256)
    back = embeddings.get(conn, "episode", 1, "m")
    assert back is not None
    assert back.dtype == np.float32
    np.testing.assert_allclose(back, vec(*range(1, 257)), atol=1e-3)


def test_round_trip_and_replace(conn: Connection) -> None:
    put(conn, 1, vec(1, 0))
    first = embeddings.get(conn, "episode", 1, "m")
    assert first is not None
    np.testing.assert_allclose(first, [1, 0])
    put(conn, 1, vec(0, 1))  # replaces
    second = embeddings.get(conn, "episode", 1, "m")
    assert second is not None
    np.testing.assert_allclose(second, [0, 1])
    assert embedding_count(conn, "episode", "m") == 1
    assert embeddings.get(conn, "episode", 1, "other") is None
    assert embeddings.get(conn, "segment", 1, "m") is None


def test_top_orders_by_cosine_similarity(conn: Connection) -> None:
    put(conn, 1, vec(1, 0))
    put(conn, 2, vec(1, 1))
    put(conn, 3, vec(0, 1))
    found = embeddings.top(conn, "episode", "m", vec(1, 0.2), limit=2)
    assert [s.owner_id for s in found] == [1, 2]
    assert found[0].score > found[1].score > 0.5


def test_top_respects_time_window_models_kinds_and_only(conn: Connection) -> None:
    put(conn, 1, vec(1, 0), start=100)
    put(conn, 2, vec(1, 0), start=200)
    put(conn, 3, vec(1, 0), start=300)
    put(conn, 4, vec(1, 0), kind="segment")
    embeddings.put(conn, "episode", 5, "other", vec(1, 0), 200, NOW)
    q = vec(1, 0)
    assert {
        s.owner_id for s in embeddings.top(conn, "episode", "m", q, start_ms=150, end_ms=300)
    } == {2}
    assert {s.owner_id for s in embeddings.top(conn, "episode", "m", q)} == {1, 2, 3}
    assert {s.owner_id for s in embeddings.top(conn, "episode", "m", q, only={1, 3, 99})} == {1, 3}
    assert embeddings.top(conn, "episode", "m", q, only=set()) == []


def test_top_scans_in_chunks_and_skips_vectors_of_another_size(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embeddings, "CHUNK_ROWS", 2)
    for i in range(1, 8):
        put(conn, i, vec(1, i))
    put(conn, 99, vec(1, 0, 0))  # three numbers where the query has two: never compared
    found = embeddings.top(conn, "episode", "m", vec(1, 7), limit=3)
    assert [s.owner_id for s in found] == [7, 6, 5]
    only_skipped = embeddings.top(conn, "episode", "m", vec(1, 0), only={99}, limit=3)
    assert only_skipped == []


def test_query_ids_are_stable_and_depend_on_the_model() -> None:
    a = embeddings.query_id("hello", "m")
    assert a == embeddings.query_id("hello", "m")
    assert a != embeddings.query_id("hello", "n")
    assert a != embeddings.query_id("hello!", "m")
    assert 0 <= a < 2**62


def test_old_questions_are_dropped_but_not_other_vectors(conn: Connection) -> None:
    embeddings.put(
        conn,
        "query",
        1,
        "m",
        vec(1, 0),
        NOW - 2 * embeddings.QUERY_KEEP_MS,
        NOW - 2 * embeddings.QUERY_KEEP_MS,
    )
    embeddings.put(conn, "query", 2, "m", vec(1, 0), NOW, NOW)
    put(conn, 3, vec(1, 0))
    assert embeddings.delete_old_queries(conn, NOW) == 1
    assert embedding_count(conn, "query", "m") == 1
    assert embedding_count(conn, "episode", "m") == 1


def test_a_deleted_or_retranscribed_segment_loses_its_vector(conn: Connection) -> None:
    keep = add_segment(conn, 1_000, text="keep this one")
    redo = add_segment(conn, 2_000, text="old words")
    gone = add_segment(conn, 3_000, text="to be deleted")
    for s in (keep, redo, gone):
        put(conn, s, vec(1, 0), kind="segment")
    conn.execute("UPDATE audio_segments SET transcript = 'new words' WHERE id = ?", (redo,))
    conn.execute("DELETE FROM audio_segments WHERE id = ?", (gone,))
    assert {r[0] for r in conn.execute("SELECT owner_id FROM embeddings")} == {keep}


def test_deleting_an_episode_deletes_its_vector(conn: Connection) -> None:
    episode = add_episode(conn, "game", 0, 10, "G")
    put(conn, episode, vec(1, 0))
    conn.execute("DELETE FROM episodes WHERE id = ?", (episode,))
    assert embedding_count(conn, "episode", "m") == 0


def test_the_embeddings_retention_step_spares_questions(conn: Connection) -> None:
    put(conn, 1, vec(1, 0), start=10)
    put(conn, 2, vec(1, 0), start=1_000)
    embeddings.put(conn, "query", 3, "m", vec(1, 0), 10, NOW)
    removed = expire_embeddings(conn, None, 500)  # type: ignore[arg-type]  # media is unused
    assert removed == 1
    assert embedding_count(conn, "episode", "m") == 1
    assert embedding_count(conn, "query", "m") == 1
