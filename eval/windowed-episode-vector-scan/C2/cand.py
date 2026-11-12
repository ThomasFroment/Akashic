"""C2: split overlap predicate for the windowed episode vector step.

overlap(e, [S, E))  <=>  e.start in [S, E)  or  (e.start < S and e.end > S).

- Range part: candidates whose vector start_ms lies in [S, E) come from the embeddings_time index
  range (cost ~ window density, not history length).
- Stragglers: every candidate not returned by the range (the start < S suffix of the start-DESC
  candidate list, plus candidates whose stored vector start_ms drifted from the episode's, plus
  candidates without a vector) is fetched by primary-key probe
  ``owner_id IN (SELECT value FROM json_each(?))`` (IN-subquery, no join, no ORDER BY, so the
  planner uses the PRIMARY KEY autoindex and does not scan embeddings_time).
- Scores: float64 per-row reduction of the float16 rows with the float32 query, independent of
  the matrix shape and the row position (identical vectors => identical scores => exact ties).
- Order: (score desc, start_ms asc, rowid asc) by an explicit lexsort, so the concatenation
  order of the two fetches does not matter; a top-``limit`` cut through a tie group keeps the
  smallest (start_ms, rowid) keys.
- Unwindowed (S <= 0 and E >= 2**62): one pass over the index range in chunks, as today.

No schema change, no new dependency, encryption untouched.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

import numpy as np

if TYPE_CHECKING:
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

CHUNK_ROWS: Final = 4_000
STORED: Final = np.dtype(np.float16)
VECTOR_DEPTH: Final = 100

_RANGE: Final = (
    "SELECT rowid, owner_id, start_ms, vec FROM embeddings"
    " WHERE owner_kind = 'episode' AND model = ? AND start_ms >= ? AND start_ms < ?"
)
_PROBE: Final = (
    "SELECT rowid, owner_id, start_ms, vec FROM embeddings"
    " WHERE owner_kind = 'episode' AND model = ?"
    " AND owner_id IN (SELECT value FROM json_each(?))"
)

PLANS: Final = [
    (_RANGE, ("m", 0, 1)),
    (_PROBE, ("m", "[1,2,3]")),
]


def _score_rows(
    rows: list[tuple[int, int, int, bytes]], q64: NDArray[np.float64], dims: int
) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.int64], NDArray[np.float64]]:
    """(rowid, owner_id, start_ms, score) arrays for ``rows`` (valid size already checked)."""
    n = len(rows)
    rowid = np.fromiter((r[0] for r in rows), dtype=np.int64, count=n)
    owner = np.fromiter((r[1] for r in rows), dtype=np.int64, count=n)
    start = np.fromiter((r[2] for r in rows), dtype=np.int64, count=n)
    m = np.frombuffer(b"".join(r[3] for r in rows), dtype=STORED).reshape(n, dims)
    # One pairwise float64 reduction per row: same value for the same bytes, wherever it sits.
    scores = (m.astype(np.float64) * q64).sum(axis=1)
    return rowid, owner, start, scores


def episode_vector_step(  # noqa: PLR0913
    conn: Connection,
    model: str,
    qvec: Vector,
    start_ms: int,
    end_ms: int,
    candidates: list[tuple[int, int]],
    limit: int = VECTOR_DEPTH,
) -> list[tuple[int, float]]:
    if not candidates or limit <= 0:
        return []
    q32 = np.ascontiguousarray(qvec, dtype=np.float32)
    dims = len(q32)
    wanted = dims * STORED.itemsize
    q64 = q32.astype(np.float64)
    allowed = {i for i, _ in candidates}

    lo = max(int(start_ms), 0)
    hi = int(end_ms)
    parts: list[tuple[NDArray, NDArray, NDArray, NDArray]] = []
    found: set[int] = set()
    cursor = conn.execute(_RANGE, (model, lo, hi))
    while True:
        chunk = cursor.fetchmany(CHUNK_ROWS)
        if not chunk:
            break
        rows = [r for r in chunk if r[1] in allowed and len(r[3]) == wanted]
        found.update(r[1] for r in chunk if r[1] in allowed)
        if rows:
            parts.append(_score_rows(rows, q64, dims))

    # Stragglers: the start < S suffix (and any candidate the range did not return).
    missing = allowed - found if len(found) < len(allowed) else set()
    if missing:
        rows = [
            r
            for r in conn.execute(_PROBE, (model, json.dumps(sorted(missing))))
            if len(r[3]) == wanted
        ]
        if rows:
            parts.append(_score_rows(rows, q64, dims))
    if not parts:
        return []
    rowid = np.concatenate([p[0] for p in parts])
    owner = np.concatenate([p[1] for p in parts])
    start = np.concatenate([p[2] for p in parts])
    scores = np.concatenate([p[3] for p in parts])
    # lexsort: last key is primary -> (-score, start_ms, rowid).
    order = np.lexsort((rowid, start, -scores))[:limit]
    return list(zip(owner[order].tolist(), scores[order].tolist(), strict=True))
