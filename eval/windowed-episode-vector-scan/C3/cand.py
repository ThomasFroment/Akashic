"""C3 (final, after one fix round): gated allow-list probe replaying today's float32 scores.

The first try (f64/cand_f64.py, float64 per-row scores) left near-tie mismatches: where two owners' exact scores
differ by less than float32 resolution, today's order is decided by the float32 rounding of
the matrix shape and row position each vector had in today's scan. This variant reproduces
that matrix exactly, so the scores are bitwise today's:

- today's scan reads embeddings_time in (start_ms, rowid) order, CHUNK_ROWS rows per
  fetchmany, then drops non-allowed and damaged rows; each chunk's survivors form one matrix;
- the probe returns every allowed row (rowid, start_ms) and the chunk boundaries inside the
  probed key range come from the covering index: one count up to the first probed key
  (index-only, O(rank)) and one LIMIT 1 OFFSET lookup per boundary crossed (O(span));
- each chunk group is scored as today (float16 -> float32 matrix @ query) and ordered by
  (-score32, start_ms, rowid), which is today's stable insertion order at equal scores.

Exact ties (equal float64 per-row dot, i.e. identical vectors) are then put in
(start_ms, rowid) order wherever they are adjacent, and a cut through such a run keeps the
smallest keys. Same gate and fallback as cand.py; the fallback is today's loop verbatim plus
rowid/start_ms columns.
"""

from __future__ import annotations

import json
from typing import Final

import numpy as np

STORED: Final = np.dtype(np.float16)
VECTOR_DEPTH: Final = 100
CHUNK_ROWS: Final = 4_000  # embeddings.CHUNK_ROWS: today's matrix boundaries
FAR: Final = 2**62
K: Final = 9_000  # fitted in fit_k.py (see notes.txt)
POOL: Final = 64  # rows past the cut examined for exact-tie runs

PROBE_SQL: Final = (
    "SELECT rowid, owner_id, start_ms, vec FROM embeddings"
    " WHERE owner_kind = 'episode' AND model = ?"
    " AND owner_id IN (SELECT value FROM json_each(?))"
)
COUNT_SQL: Final = (
    "SELECT count(*) FROM embeddings WHERE owner_kind = 'episode' AND model = ?"
    " AND start_ms >= 0 AND start_ms < ?"
)
BOUNDARY_SQL: Final = (
    "SELECT start_ms, rowid FROM embeddings WHERE owner_kind = 'episode' AND model = ?"
    " AND start_ms >= ? ORDER BY start_ms, rowid LIMIT 1 OFFSET ?"
)
SCAN_SQL: Final = (
    "SELECT rowid, owner_id, start_ms, vec FROM embeddings WHERE owner_kind = ? AND model = ?"
    " AND start_ms >= ? AND start_ms < ?"
)
_M = "qwen3-embedding-0.6b-q8"
PLANS = [
    (PROBE_SQL, (_M, "[1,2,3]")),
    (COUNT_SQL, (_M, 1_760_000_000_000)),
    (BOUNDARY_SQL, (_M, 1_760_000_000_000, 4_000)),
    (SCAN_SQL, ("episode", _M, 0, FAR)),
]


def _f16(blobs: list[bytes]) -> np.ndarray:
    return np.frombuffer(b"".join(blobs), dtype=STORED).reshape(len(blobs), -1)


def _finish(parts: list[tuple], q64: np.ndarray, limit: int) -> list[tuple[int, float]]:
    """parts: per-group (score32, float16 matrix, start_ms, rowid, owner_id) arrays."""
    if not parts:
        return []
    s32, st, rid, oid = (np.concatenate([p[k] for p in parts]) for k in (0, 2, 3, 4))
    order = np.lexsort((rid, st, -s32))[: limit + POOL]  # today's order: stable by index key
    # Exact per-row float64 dots, only for the pool (identical vectors -> identical values).
    offs = np.cumsum([0] + [len(p[0]) for p in parts])
    part = np.searchsorted(offs, order, side="right") - 1
    m16 = np.vstack([parts[g][1][n] for g, n in zip(part.tolist(), (order - offs[part]).tolist(),
                                                     strict=True)])  # fmt: skip
    s64 = np.zeros(len(s32))
    s64[order] = (m16.astype(np.float64) * q64).sum(axis=1)
    entries = list(zip(s32[order].tolist(), s64[order].tolist(), st[order].tolist(),
                       rid[order].tolist(), oid[order].tolist(), strict=True))  # fmt: skip
    # Exact ties adjacent in today's order go to (start_ms, rowid) order; a run crossing the
    # cut is ordered over the whole run, so the kept members are its smallest keys.
    out: list[tuple] = []
    k = 0
    while k < len(entries) and len(out) < limit:
        j = k + 1
        while j < len(entries) and entries[j][1] == entries[k][1]:
            j += 1
        out.extend(sorted(entries[k:j], key=lambda e: (e[2], e[3])) if j - k > 1
                   else entries[k:j])  # fmt: skip
        k = j
    return [(int(e[4]), float(e[0])) for e in out[:limit]]


def _score_group(rows: list[tuple], q32: np.ndarray) -> tuple:
    m16 = _f16([r[3] for r in rows])
    s32 = m16.astype(np.float32) @ q32  # today's exact shape and row order
    st = np.fromiter((r[2] for r in rows), np.int64, len(rows))
    rid = np.fromiter((r[0] for r in rows), np.int64, len(rows))
    oid = np.fromiter((r[1] for r in rows), np.int64, len(rows))
    return s32, m16, st, rid, oid


def probe(conn, model, q, allowed: list[int], limit=VECTOR_DEPTH):
    q32 = np.ascontiguousarray(q, dtype=np.float32)
    q64 = q32.astype(np.float64)
    want = len(q32) * STORED.itemsize
    rows = conn.execute(PROBE_SQL, (model, json.dumps(sorted(allowed)))).fetchall()
    if not rows:
        return []
    rows.sort(key=lambda r: (r[2], r[0]))  # index order of today's scan
    s0 = rows[0][2]
    base = conn.execute(COUNT_SQL, (model, s0)).fetchone()[0]  # index rows before s0
    last = (rows[-1][2], rows[-1][0])
    bounds: list[tuple[int, int]] = []
    c = base // CHUNK_ROWS + 1
    while True:
        b = conn.execute(BOUNDARY_SQL, (model, s0, c * CHUNK_ROWS - base)).fetchone()
        if b is None or (b[0], b[1]) > last:
            break
        bounds.append((b[0], b[1]))
        c += 1
    parts: list[tuple] = []
    group: list[tuple] = []
    nb = 0
    for r in rows:
        while nb < len(bounds) and (r[2], r[0]) >= bounds[nb]:
            if group:
                parts.append(_score_group(group, q32))
                group = []
            nb += 1
        if len(r[3]) == want:
            group.append(r)
    if group:
        parts.append(_score_group(group, q32))
    return _finish(parts, q64, limit)


def scan(conn, model, q, allowed: set[int] | None, limit=VECTOR_DEPTH):
    q32 = np.ascontiguousarray(q, dtype=np.float32)
    q64 = q32.astype(np.float64)
    want = len(q32) * STORED.itemsize
    cur = conn.execute(SCAN_SQL, ("episode", model, 0, FAR))
    parts: list[tuple] = []
    while True:
        rows = cur.fetchmany(CHUNK_ROWS)
        if not rows:
            break
        rows = [r for r in rows if len(r[3]) == want and (allowed is None or r[1] in allowed)]
        if rows:
            parts.append(_score_group(rows, q32))
    return _finish(parts, q64, limit)


def episode_vector_step(conn, model, qvec, start_ms, end_ms, candidates, limit=VECTOR_DEPTH):  # noqa: PLR0913
    allowed = {i for i, _ in candidates}
    if not allowed:
        return []
    windowed = start_ms > 0 or end_ms < FAR
    if windowed and len(allowed) <= K:
        return probe(conn, model, qvec, list(allowed), limit)
    return scan(conn, model, qvec, allowed, limit)
