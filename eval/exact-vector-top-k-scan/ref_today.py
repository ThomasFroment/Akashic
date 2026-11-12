"""Today's exact vector top-k, copied verbatim from the repo (only the def names differ).

- passages_top   = src/akashic/storage/passages.py   top()   (+ Match, _order, _TABLES)
- embeddings_top = src/akashic/storage/embeddings.py top()   (+ Scored, CHUNK_ROWS, STORED)

``harness.py`` re-checks at start that these bodies still equal the repo's (def line excepted).
This module is also a valid candidate: ``python harness.py ref_today.py ref``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, Literal

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

# --- embeddings.py --------------------------------------------------------------------------

CHUNK_ROWS: Final = 4_000
STORED: Final = np.dtype(np.float16)


@dataclass(frozen=True, slots=True)
class Scored:
    owner_id: int
    score: float


def embeddings_top(  # noqa: PLR0913 - a query vector, where to look and how many
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


# --- passages.py ----------------------------------------------------------------------------

embeddings = SimpleNamespace(CHUNK_ROWS=CHUNK_ROWS)  # passages.py reads embeddings.CHUNK_ROWS
type Kind = Literal["segment", "frame"]
# Only these literal identifiers can enter SQL. All captured values remain bound parameters.
_TABLES: Final = {
    "segment": ("segment_passages", "segment_id", "audio_segments", "transcript", "start_ms"),
    "frame": ("frame_passages", "frame_id", "frames", "text", "ts_ms"),
}


@dataclass(frozen=True, slots=True)
class Match:
    owner_id: int
    score: float
    start_char: int
    end_char: int


def _order(hit: Match) -> tuple[float, int]:
    return -hit.score, hit.owner_id


def passages_top(  # noqa: PLR0913 - same filters as the existing exact vector search
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
            " AND s.image_scope = 'window' AND replace(lower(s.process), '.exe', '')"
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
