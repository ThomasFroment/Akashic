"""C3: gated allow-list probe for the windowed episode vector step.

Windowed question with at most K candidates: fetch exactly the candidate vectors through the
embeddings primary key (owner_kind, owner_id, model) -- one index probe per candidate, no
ORDER BY, no join, membership taken from the candidate ids, never from embeddings.start_ms.
Otherwise (unwindowed, or more than K candidates): today's scan statement, unchanged WHERE.

Both paths score shape-independently: float16 rows widened to float64, multiplied by the
float64 query and summed per row (one reduction per row, so identical vectors give identical
scores whatever the matrix shape or row position), then sort by (-score, start_ms, rowid).
No schema change, no new dependency.
"""

from __future__ import annotations

import json
from typing import Final

import numpy as np

STORED: Final = np.dtype(np.float16)
VECTOR_DEPTH: Final = 100
CHUNK_ROWS: Final = 4_000
FAR: Final = 2**62
# Fitted crossover (see notes.txt / fit_k.py): probe cost = scan cost at about this many ids.
K: Final = 9_000

PROBE_SQL: Final = (
    "SELECT rowid, owner_id, start_ms, vec FROM embeddings"
    " WHERE owner_kind = 'episode' AND model = ?"
    " AND owner_id IN (SELECT value FROM json_each(?))"
)
SCAN_SQL: Final = (
    "SELECT rowid, owner_id, start_ms, vec FROM embeddings WHERE owner_kind = ? AND model = ?"
    " AND start_ms >= ? AND start_ms < ?"
)

PLANS = [
    (PROBE_SQL, ("qwen3-embedding-0.6b-q8", "[1,2,3]")),
    (SCAN_SQL, ("episode", "qwen3-embedding-0.6b-q8", 0, FAR)),
]


def _score(blobs: list[bytes], q64: np.ndarray) -> np.ndarray:
    m = np.frombuffer(b"".join(blobs), dtype=STORED).reshape(len(blobs), -1).astype(np.float64)
    return (m * q64).sum(axis=1)


def _rank(rowid, start, ids, scores, limit):
    if not ids:
        return []
    rowid = np.asarray(rowid, dtype=np.int64)
    start = np.asarray(start, dtype=np.int64)
    scores = np.asarray(scores, dtype=np.float64)
    order = np.lexsort((rowid, start, -scores))[:limit]
    return [(int(ids[k]), float(scores[k])) for k in order]


def probe(conn, model, q, allowed: list[int], limit=VECTOR_DEPTH):
    """Exact candidate set through the primary key."""
    q64 = np.ascontiguousarray(q, dtype=np.float32).astype(np.float64)
    want = len(q64) * STORED.itemsize
    rows = conn.execute(PROBE_SQL, (model, json.dumps(sorted(allowed)))).fetchall()
    rows = [r for r in rows if len(r[3]) == want]
    if not rows:
        return []
    scores = _score([r[3] for r in rows], q64)
    return _rank([r[0] for r in rows], [r[2] for r in rows], [r[1] for r in rows], scores, limit)


def scan(conn, model, q, allowed: set[int] | None, limit=VECTOR_DEPTH):
    """Today's statement (no time bounds), shape-independent scoring and tie order."""
    q64 = np.ascontiguousarray(q, dtype=np.float32).astype(np.float64)
    want = len(q64) * STORED.itemsize
    cur = conn.execute(SCAN_SQL, ("episode", model, 0, FAR))
    rowid: list[int] = []
    start: list[int] = []
    ids: list[int] = []
    parts: list[np.ndarray] = []
    while True:
        rows = cur.fetchmany(CHUNK_ROWS)
        if not rows:
            break
        rows = [r for r in rows if len(r[3]) == want and (allowed is None or r[1] in allowed)]
        if not rows:
            continue
        rowid.extend(r[0] for r in rows)
        ids.extend(r[1] for r in rows)
        start.extend(r[2] for r in rows)
        parts.append(_score([r[3] for r in rows], q64))
    if not ids:
        return []
    return _rank(rowid, start, ids, np.concatenate(parts), limit)


def episode_vector_step(conn, model, qvec, start_ms, end_ms, candidates, limit=VECTOR_DEPTH):  # noqa: PLR0913
    allowed = {i for i, _ in candidates}
    if not allowed:
        return []
    windowed = start_ms > 0 or end_ms < FAR
    if windowed and len(allowed) <= K:
        return probe(conn, model, qvec, list(allowed), limit)
    return scan(conn, model, qvec, allowed, limit)
