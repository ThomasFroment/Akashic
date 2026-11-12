"""C4: columnar chunk scan with a plain preallocated float16 -> float32 cast.

Per fetched chunk (Python objects only for columns, never per-row objects):
  1. lengths (and owner ids only when a filter needs them) -> boolean keep mask, so the kept rows
     and their order equal today's filtered list;
  2. one b"".join of the kept blobs, one frombuffer(float16).reshape(n, d);
  3. np.copyto into a float32 buffer preallocated once per call and reused across chunks, then
     ``buf[:n] @ query``: the same (n, d) @ (d,) matvec as today -> bit-identical scores;
  4. NaN scores are dropped; top-k by threshold, survivors only;
  5. Python objects (Match / Scored) only for the final winners.

Embeddings: survivors are s > T (T = current limit-th score), stable merge (previous best first,
then the chunk in scan order) -> ties in scan order, as today.
Passages: survivors are s >= T (T = current limit-th owner max); lexsort((seq, -s, owner)) keeps
the first max passage per owner in scan order, then lexsort((owner, -s))[:limit].
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from itertools import compress
from operator import itemgetter
from typing import TYPE_CHECKING, Final, Literal

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable
    from sqlite3 import Connection

CHUNK_ROWS: Final = 4_000
STORED: Final = np.dtype(np.float16)
_SUB_ROWS: Final = 512
_TABLE_MAX: Final = 16 * 2**20  # 16 MB bool lookup table at most
_get_owner = itemgetter(0)
type Kind = Literal["segment", "frame"]
_TABLES: Final = {
    "segment": ("segment_passages", "segment_id", "audio_segments", "transcript", "start_ms"),
    "frame": ("frame_passages", "frame_id", "frames", "text", "ts_ms"),
}


@dataclass(frozen=True, slots=True)
class Scored:
    owner_id: int
    score: float


@dataclass(frozen=True, slots=True)
class Match:
    owner_id: int
    score: float
    start_char: int
    end_char: int


def _int_array(ids: set) -> np.ndarray | None:
    """Sorted int64 array of ``ids``, or None when a plain integer array would not keep Python's
    set semantics (non-integers, out-of-int64 values)."""
    arr = np.array(list(ids)) if ids else np.empty(0, np.int64)
    return np.unique(arr).astype(np.int64) if arr.dtype.kind in "ib" else None


class _Filter:
    """Vectorized ``(allowed is None or o in allowed) and o not in excluded`` per chunk.

    One boolean table over [lo, hi] of the given ids when it spans <= _TABLE_MAX entries (one
    gather per chunk, no binary search); else searchsorted; Python ``in`` for non-integer ids.
    """

    def __init__(self, allowed: set | None, excluded: set) -> None:
        self.allowed, self.excluded = allowed, excluded
        a = None if allowed is None else _int_array(allowed)
        e = _int_array(excluded)
        self.py = (allowed is not None and a is None) or e is None
        if self.py:
            return
        self.a, self.e = a, e
        self.default = allowed is None  # verdict for ids outside [lo, hi]
        both = e if a is None else np.concatenate((a, e))
        self.lo, hi = int(both.min()), int(both.max())
        self.table = None
        if hi - self.lo < _TABLE_MAX:
            t = np.full(hi - self.lo + 1, self.default, np.bool_)
            if a is not None:
                t[a - self.lo] = True
            t[e - self.lo] = False
            self.table = t

    def mask(self, rows: list) -> np.ndarray:
        n = len(rows)
        if self.py:
            al, ex = self.allowed, self.excluded
            return np.fromiter(((al is None or o in al) and o not in ex
                                for o in map(_get_owner, rows)), np.bool_, n)  # fmt: skip
        ids = np.fromiter(map(_get_owner, rows), np.int64, n)
        if self.table is not None:
            t = self.table
            idx = ids - self.lo
            out = (idx < 0) | (idx >= t.size)
            np.clip(idx, 0, t.size - 1, out=idx)
            m = t[idx]
            m[out] = self.default
            return m
        m = np.ones(n, np.bool_) if self.a is None else _isin(ids, self.a)
        if self.e.size:
            m &= ~_isin(ids, self.e)
        return m


def _isin(ids: np.ndarray, sorted_arr: np.ndarray) -> np.ndarray:
    if not sorted_arr.size:
        return np.zeros(ids.size, np.bool_)
    pos = np.searchsorted(sorted_arr, ids)
    np.minimum(pos, sorted_arr.size - 1, out=pos)
    return sorted_arr[pos] == ids


def _matrix(blobs: list, keep: np.ndarray | None, n_keep: int, dims: int, buf: np.ndarray) -> np.ndarray:
    """float32 (n_keep, dims) view of the kept float16 blobs, in ``buf``."""
    if keep is not None:
        blobs = list(compress(blobs, keep.tolist()))
    # Joined in sub-blocks of ~256 KB: one 2 MB join per chunk is a fresh OS allocation (page
    # faults on every chunk); small joins reuse heap blocks and stay cache-resident for the cast.
    for a in range(0, n_keep, _SUB_ROWS):
        part = blobs[a : a + _SUB_ROWS]
        half = np.frombuffer(b"".join(part), dtype=STORED).reshape(len(part), dims)
        np.copyto(buf[a : a + len(part)], half)
    return buf[:n_keep]


def embeddings_top(  # noqa: PLR0913, PLR0915
    conn: Connection,
    owner_kind: str,
    model: str,
    query,
    *,
    start_ms: int = 0,
    end_ms: int = 2**62,
    only: Iterable[int] | None = None,
    exclude: Iterable[int] = (),
    limit: int = 30,
) -> list[Scored]:
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
    dims = len(unit_query)
    wanted_bytes = dims * STORED.itemsize
    if limit <= 0:
        return []
    filt = _Filter(allowed, excluded) if allowed is not None or excluded else None
    buf = np.empty((CHUNK_ROWS, dims), np.float32)
    best_ids = np.empty(0, np.int64)
    best_s = np.empty(0, np.float32)
    get_id, get_vec = itemgetter(0), itemgetter(1)
    while rows := cursor.fetchmany(CHUNK_ROWS):
        n = len(rows)
        if n > len(buf):
            buf = np.empty((n, dims), np.float32)
        blobs = list(map(get_vec, rows))
        keep = np.fromiter(map(len, blobs), np.intp, n) == wanted_bytes
        if filt is not None:
            keep &= filt.mask(rows)
        n_keep = int(np.count_nonzero(keep))
        if not n_keep:
            continue
        all_kept = n_keep == n
        scores = _matrix(blobs, None if all_kept else keep, n_keep, dims, buf) @ unit_query
        full = len(best_s) >= limit
        with np.errstate(invalid="ignore"):
            surv = scores > best_s[-1] if full else ~np.isnan(scores)
        idx = np.flatnonzero(surv)
        if not idx.size:
            continue
        rowpos = idx if all_kept else np.flatnonzero(keep)[idx]
        ids = np.fromiter(map(get_id, map(rows.__getitem__, rowpos.tolist())), np.int64, idx.size)
        all_s = np.concatenate((best_s, scores[idx]))
        all_ids = np.concatenate((best_ids, ids))
        order = np.argsort(-all_s, kind="stable")[:limit]
        best_s, best_ids = all_s[order], all_ids[order]
    return [Scored(i, s) for i, s in zip(best_ids.tolist(), best_s.tolist(), strict=True)]


def passages_top(  # noqa: PLR0913, PLR0915
    conn: Connection,
    model: str,
    query,
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
    dims = len(query)
    wanted_bytes = dims * 2
    filt = None if allowed is None else _Filter(allowed, set())
    buf = np.empty((CHUNK_ROWS, dims), np.float32)
    # Best passage per retained owner, best first by (-score, owner_id).
    b_own = np.empty(0, np.int64)
    b_s = np.empty(0, np.float32)
    b_seq = np.empty(0, np.int64)
    b_sc = np.empty(0, np.int64)
    b_ec = np.empty(0, np.int64)
    base = 0  # scan position of the chunk's first row
    get_vec = itemgetter(3)
    while rows := cursor.fetchmany(CHUNK_ROWS):
        n = len(rows)
        if n > len(buf):
            buf = np.empty((n, dims), np.float32)
        blobs = list(map(get_vec, rows))
        keep = np.fromiter(map(len, blobs), np.intp, n) == wanted_bytes
        if filt is not None:
            keep &= filt.mask(rows)
        n_keep = int(np.count_nonzero(keep))
        if not n_keep:
            base += n
            continue
        all_kept = n_keep == n
        scores = _matrix(blobs, None if all_kept else keep, n_keep, dims, buf) @ query
        full = limit > 0 and len(b_s) >= limit
        with np.errstate(invalid="ignore"):
            surv = scores >= b_s[limit - 1] if full else ~np.isnan(scores)
        idx = np.flatnonzero(surv)
        if not idx.size:
            base += n
            continue
        rowpos = idx if all_kept else np.flatnonzero(keep)[idx]
        sel = [rows[i] for i in rowpos.tolist()]
        m = len(sel)
        own = np.fromiter((int(r[0]) for r in sel), np.int64, m)
        sc = np.fromiter((int(r[1]) for r in sel), np.int64, m)
        ec = np.fromiter((int(r[2]) for r in sel), np.int64, m)
        a_own = np.concatenate((b_own, own))
        a_s = np.concatenate((b_s, scores[idx]))
        a_seq = np.concatenate((b_seq, rowpos + base))
        a_sc = np.concatenate((b_sc, sc))
        a_ec = np.concatenate((b_ec, ec))
        base += n
        # First max passage per owner (scan order breaks equal maxima).
        o = np.lexsort((a_seq, -a_s, a_own))
        first = np.ones(o.size, np.bool_)
        first[1:] = a_own[o][1:] != a_own[o][:-1]
        o = o[first]
        # Best owners by (-score, owner_id), cut at limit.
        o = o[np.lexsort((a_own[o], -a_s[o]))][: max(limit, 0)]
        b_own, b_s, b_seq, b_sc, b_ec = a_own[o], a_s[o], a_seq[o], a_sc[o], a_ec[o]
    return [
        Match(i, s, a, b)
        for i, s, a, b in zip(b_own.tolist(), b_s.tolist(), b_sc.tolist(), b_ec.tolist(), strict=True)
    ]
