"""Vectors for semantic search: storage, and the brute-force search over them (numpy).

Vectors are unit-length float16 BLOBs (256 numbers, see ``pipeline.llm.EMBED_DIMS``), so a dot
product is the cosine similarity. One vector per (owner, model); vectors made by different
models are never compared. With a time window the search reads only the vectors inside it;
without one it streams through all of them in chunks. Matrix memory is bounded by chunk size;
latency and eligible-ID bookkeeping still grow with history. Measure before adding an ANN index.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

from akashic.storage import db

if TYPE_CHECKING:
    from collections.abc import Iterable
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

CHUNK_ROWS: Final = 4_000
STORED: Final = np.dtype(np.float16)
QUERY_KEEP_MS: Final = 24 * 3_600_000


@dataclass(frozen=True, slots=True)
class Scored:
    owner_id: int
    score: float


def unit(vector: Vector) -> Vector:
    """``vector`` scaled to length 1 (float32); a zero vector stays as it is."""
    norm = float(np.linalg.norm(vector))
    return vector if norm == 0.0 else (vector / norm).astype(np.float32)


def query_id(text: str, model: str) -> int:
    """Stable 62-bit id of a question's text, so asking twice reuses the vector."""
    digest = hashlib.sha256(f"{model}\x00{text}".encode()).digest()
    return int.from_bytes(digest[:8], "big") >> 2


def put(  # noqa: PLR0913, PLR0917 - one vector and where it belongs
    conn: Connection,
    owner_kind: str,
    owner_id: int,
    model: str,
    vector: Vector,
    start_ms: int,
    now_ms: int,
    *,
    expected_episode_text: str | None = None,
) -> None:
    stored = np.ascontiguousarray(vector, dtype=np.float16)
    with db.transaction(conn):
        if expected_episode_text is not None and conn.execute(
            "SELECT title || char(10) || body FROM episodes WHERE id = ?", (owner_id,)
        ).fetchone() != (expected_episode_text,):
            return  # deletion/rebuilding during inference invalidated this result
        conn.execute(
            "INSERT OR REPLACE INTO embeddings"
            " (owner_kind, owner_id, model, dim, vec, start_ms, created_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner_kind, owner_id, model, len(stored), stored.tobytes(), start_ms, now_ms),
        )


def get(conn: Connection, owner_kind: str, owner_id: int, model: str) -> Vector | None:
    row = conn.execute(
        "SELECT vec FROM embeddings WHERE owner_kind = ? AND owner_id = ? AND model = ?",
        (owner_kind, owner_id, model),
    ).fetchone()
    return (
        None if row is None else np.frombuffer(bytes(row[0]), dtype=np.float16).astype(np.float32)
    )


def top(  # noqa: PLR0913 - a query vector, where to look and how many
    conn: Connection,
    owner_kind: str,
    model: str,
    query: Vector,
    *,
    start_ms: int = 0,
    end_ms: int = 2**62,
    only: Iterable[int] | None = None,
    exclude: Iterable[int] = (),
    limit: int = 30,
) -> list[Scored]:
    """The ``limit`` owners most similar to ``query``, best first.

    ``only`` restricts the search to those owner ids (e.g. the segments of the episodes already
    selected); an empty collection finds nothing.
    """
    allowed = None if only is None else set(only)
    excluded = set(exclude)
    if allowed is not None and not allowed:
        return []
    cursor = conn.execute(
        "SELECT owner_id, vec FROM embeddings WHERE owner_kind = ? AND model = ?"
        " AND start_ms >= ? AND start_ms < ?",
        (owner_kind, model, start_ms, end_ms),
    )
    unit_query = np.ascontiguousarray(query, dtype=np.float32)
    wanted_bytes = len(unit_query) * STORED.itemsize
    best: list[Scored] = []
    while True:
        rows = cursor.fetchmany(CHUNK_ROWS)
        if not rows:
            break
        # Only vectors of the query's size are comparable: a different model under the same name
        # (or a damaged row) is skipped, never compared.
        rows = [
            r
            for r in rows
            if len(r[1]) == wanted_bytes
            and (allowed is None or r[0] in allowed)
            and r[0] not in excluded
        ]
        if not rows:
            continue
        ids = [r[0] for r in rows]
        matrix = np.vstack([np.frombuffer(bytes(r[1]), dtype=STORED) for r in rows]).astype(
            np.float32
        )
        scores = matrix @ unit_query
        best.extend(Scored(i, float(s)) for i, s in zip(ids, scores, strict=True))
        best.sort(key=lambda s: s.score, reverse=True)
        del best[limit:]
    return best


def delete_old_queries(conn: Connection, now_ms: int) -> int:
    with db.transaction(conn):
        return conn.execute(
            "DELETE FROM embeddings WHERE owner_kind = 'query' AND created_ms < ?",
            (now_ms - QUERY_KEEP_MS,),
        ).rowcount
