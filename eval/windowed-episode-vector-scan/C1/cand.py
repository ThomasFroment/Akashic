"""C1: candidate-bounded range scan for the windowed episode vector step.

Every candidate's vector carries ``embeddings.start_ms == episodes.start_ms``, so all of them lie
in ``[min candidate start, max candidate start]``. Today's single range query on
``embeddings_time`` is run over exactly that span (instead of all history) and rows are kept only
when ``owner_id`` is a candidate. No schema change. Unwindowed (start 0, end FAR): today's range.

Scores are computed shape-independently (float16 -> float64 rows, elementwise product, one
pairwise reduction per row), so identical vectors score identically wherever they sit in the
matrix; ranking is (score desc, start_ms asc, rowid asc).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import numpy as np

if TYPE_CHECKING:
    from sqlite3 import Connection

    from numpy.typing import NDArray

CHUNK_ROWS: Final = 4_000
STORED: Final = np.dtype(np.float16)
VECTOR_DEPTH: Final = 100
FAR: Final = 2**62

SQL: Final = (
    "SELECT rowid, owner_id, start_ms, vec FROM embeddings"
    " WHERE owner_kind = 'episode' AND model = ? AND start_ms >= ? AND start_ms < ?"
)

PLANS = [(SQL, ("m", 0, 1))]


def _bounds(start_ms: int, end_ms: int, candidates: list[tuple[int, int]]) -> tuple[int, int]:
    if start_ms <= 0 and end_ms >= FAR:
        return 0, FAR  # unwindowed: today's range
    starts = [s for _, s in candidates]
    return min(starts), max(starts) + 1


def episode_vector_step(  # noqa: PLR0913
    conn: Connection,
    model: str,
    qvec: NDArray[np.float32],
    start_ms: int,
    end_ms: int,
    candidates: list[tuple[int, int]],
    limit: int = VECTOR_DEPTH,
) -> list[tuple[int, float]]:
    if not candidates or limit <= 0:
        return []
    allowed = {i for i, _ in candidates}
    lo, hi = _bounds(start_ms, end_ms, candidates)
    q64 = np.ascontiguousarray(qvec, dtype=np.float32).astype(np.float64)
    wanted = len(q64) * STORED.itemsize
    cursor = conn.execute(SQL, (model, lo, hi))
    scores: list[NDArray[np.float64]] = []
    keys: list[tuple[int, int, int]] = []  # (start_ms, rowid, owner_id)
    while True:
        rows = cursor.fetchmany(CHUNK_ROWS)
        if not rows:
            break
        rows = [r for r in rows if r[1] in allowed and len(r[3]) == wanted]
        if not rows:
            continue
        m = np.frombuffer(b"".join(r[3] for r in rows), dtype=STORED).reshape(len(rows), -1)
        scores.append((m.astype(np.float64) * q64).sum(axis=1))
        keys.extend((r[2], r[0], r[1]) for r in rows)
    if not keys:
        return []
    s = np.concatenate(scores)
    k = np.array(keys, dtype=np.int64)
    order = np.lexsort((k[:, 1], k[:, 0], -s))[:limit]
    return [(int(k[j, 2]), float(s[j])) for j in order]
