"""Today's episode vector step, copied verbatim (as functions) from the pre-audit code.

- ``top``: src/akashic/storage/embeddings.py:80-132 (``embeddings.top``), unchanged body.
- ``candidates``: the overlap query of src/akashic/search/hybrid.py:197-221 (``_candidates``),
  unchanged SQL except that it also selects ``e.start_ms`` (the harness hands candidates as
  ``(id, start_ms)``); scope fields become keyword arguments (kinds/apps/people optional).
- ``episode_vector_step``: hybrid.py:281-288 (``allowed = set(candidates)`` then
  ``embeddings.top(conn, "episode", model, qvec, only=allowed, limit=VECTOR_DEPTH)``).
  No time bounds are passed, as today.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

CHUNK_ROWS: Final = 4_000
STORED: Final = np.dtype(np.float16)
VECTOR_DEPTH: Final = 100  # hybrid.VECTOR_DEPTH


@dataclass(frozen=True, slots=True)
class Scored:
    owner_id: int
    score: float


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


def candidates(  # noqa: PLR0913
    conn: Connection,
    start_ms: int,
    end_ms: int,
    *,
    kinds: tuple[str, ...] = (),
    apps: list[str] | None = None,
    people: frozenset[int] = frozenset(),
) -> list[tuple[int, int]]:
    """hybrid._candidates: episodes overlapping [start_ms, end_ms), newest first."""

    def fetch(kinds: tuple[str, ...], *, with_people: bool) -> list[tuple[int, int]]:
        sql = "SELECT e.id, e.start_ms FROM episodes e WHERE e.start_ms < :end AND e.end_ms > :start"
        params: dict[str, object] = {"start": start_ms, "end": end_ms}
        if kinds:
            marks = ",".join(f":k{i}" for i in range(len(kinds)))
            sql += f" AND e.kind IN ({marks})"
            params.update({f"k{i}": k for i, k in enumerate(kinds)})
        if apps:
            sql += (
                " AND lower(replace(e.app, '.exe', '')) IN"
                " (SELECT lower(replace(value, '.exe', '')) FROM json_each(:apps))"
            )
            params["apps"] = json.dumps(apps)
        if with_people:
            marks = ",".join(f":p{i}" for i in range(len(people)))
            sql += (
                " AND EXISTS (SELECT 1 FROM episode_people p WHERE p.episode_id = e.id"  # noqa: S608
                f"            AND p.speaker_id IN ({marks}))"
            )
            params.update({f"p{i}": p for i, p in enumerate(sorted(people))})
        sql += " ORDER BY e.start_ms DESC, e.id DESC"
        return [(int(r[0]), int(r[1])) for r in conn.execute(sql, params).fetchall()]

    return fetch(kinds, with_people=bool(people))


def episode_vector_step(  # noqa: PLR0913
    conn: Connection,
    model: str,
    qvec: Vector,
    start_ms: int,
    end_ms: int,
    candidates: list[tuple[int, int]],
    limit: int = VECTOR_DEPTH,
) -> list[tuple[int, float]]:
    """hybrid._ranked_episodes' vector step: the window is not passed to ``top``, as today."""
    del start_ms, end_ms  # today's code does not use them here
    allowed = {i for i, _ in candidates}
    scored = top(conn, "episode", model, qvec, only=allowed, limit=limit)
    return [(s.owner_id, s.score) for s in scored]
