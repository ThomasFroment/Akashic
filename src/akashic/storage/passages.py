"""Atomic, encrypted passage vectors and offsets into original speech or screen text."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

import numpy as np

from akashic.storage import db, embeddings

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

CHARS: Final = 400
STEP: Final = 300
type Kind = Literal["segment", "frame"]
# Only these literal identifiers can enter SQL. All captured values remain bound parameters.
_TABLES: Final = {
    "segment": ("segment_passages", "segment_id", "audio_segments", "transcript", "start_ms"),
    "frame": ("frame_passages", "frame_id", "frames", "text", "ts_ms"),
}


def windows(text: str) -> list[tuple[int, int]]:
    if not text:
        return []
    return [
        (start, min(start + CHARS, len(text)))
        for start in range(0, max(len(text) - CHARS, 0) + STEP, STEP)
    ]


def publish(  # noqa: PLR0913 - original owner/text/vectors and the source kind
    conn: Connection,
    owner_id: int,
    model: str,
    text: str,
    vectors: Sequence[Vector],
    *,
    kind: Kind = "segment",
) -> bool:
    """Publish a complete set only if the source text has not changed during inference."""
    table, column, source, text_column, _ = _TABLES[kind]
    offsets = windows(text)
    rows = [
        (
            owner_id,
            model,
            start,
            end,
            len(vector),
            np.ascontiguousarray(vector, dtype=np.float16).tobytes(),
        )
        for (start, end), vector in zip(offsets, vectors, strict=True)
    ]
    with db.transaction(conn):
        current = conn.execute(
            f"SELECT {text_column} FROM {source} WHERE id = ?",  # noqa: S608 - literal identifiers
            (owner_id,),
        ).fetchone()
        if current is None or current[0] != text:
            return False
        conn.execute(
            f"DELETE FROM {table} WHERE {column} = ? AND model = ?",  # noqa: S608
            (owner_id, model),
        )
        conn.executemany(f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)", rows)  # noqa: S608
        if kind == "segment":
            conn.execute(
                "DELETE FROM embeddings WHERE owner_kind = 'segment'"
                " AND owner_id = ? AND model = ?",
                (owner_id, model),
            )
    return True


@dataclass(frozen=True, slots=True)
class Match:
    owner_id: int
    score: float
    start_char: int
    end_char: int


def _order(hit: Match) -> tuple[float, int]:
    return -hit.score, hit.owner_id


def top(  # noqa: PLR0913 - same filters as the existing exact vector search
    conn: Connection,
    model: str,
    query: Vector,
    *,
    start_ms: int,
    end_ms: int,
    only: Iterable[int] | None = None,
    limit: int = 100,
    kind: Kind = "segment",
    apps: tuple[str, ...] = (),
) -> list[Match]:
    """Max-passage score per source; all filters precede the distinct-source limit."""
    allowed = None if only is None else set(only)
    if allowed is not None and not allowed:
        return []
    table, column, source, text_column, timestamp = _TABLES[kind]
    sql = (
        f"SELECT p.{column}, p.start_char, p.end_char, p.vec FROM {table} p"  # noqa: S608
        f" JOIN {source} s ON s.id = p.{column} WHERE p.model = :model"
        f" AND s.{timestamp} >= :start AND s.{timestamp} < :end"
        f" AND s.{text_column} IS NOT NULL"
    )
    params: dict[str, object] = {"model": model, "start": start_ms, "end": end_ms}
    if kind == "frame" and apps:
        sql += (
            " AND replace(lower(s.process), '.exe', '')"
            " IN (SELECT replace(lower(value), '.exe', '') FROM json_each(:apps))"
        )
        params["apps"] = json.dumps(apps)
    cursor = conn.execute(sql, params)
    best: dict[int, Match] = {}
    while rows := cursor.fetchmany(embeddings.CHUNK_ROWS):
        rows = [
            r
            for r in rows
            if len(r[3]) == len(query) * 2 and (allowed is None or int(r[0]) in allowed)
        ]
        if not rows:
            continue
        matrix = np.vstack([np.frombuffer(bytes(r[3]), dtype=np.float16) for r in rows]).astype(
            np.float32
        )
        for r, score in zip(rows, matrix @ query, strict=True):
            owner_id = int(r[0])
            hit = Match(owner_id, float(score), int(r[1]), int(r[2]))
            if owner_id not in best or hit.score > best[owner_id].score:
                best[owner_id] = hit
        # Safe pruning: each retained source has already outranked every discarded score.
        retained: list[Match] = sorted(best.values(), key=_order)[:limit]
        best = {hit.owner_id: hit for hit in retained}
    return sorted(best.values(), key=_order)
