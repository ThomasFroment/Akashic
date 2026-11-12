"""C3: columnar chunk + run-level selection for passages, threshold-pruned stable top-k for
embeddings. Same SQL, filters, float16 storage and matvec shape as today (scores bit-identical).

Per 4,000-row chunk (Python objects only per column, never per row):
  columns   map(itemgetter) per needed column; the length check is max(map(len)) next to the
            single b"".join; ``only``/``exclude`` become one bool table indexed by owner id
            (gather per chunk) when they are ints in [0, 2**24), else today's sets via
            map(set.__contains__); masked rows are dropped before the join (itertools.compress). Each chunk's objects are freed
            before the next fetchmany (the allocator then reuses them).
  matrix    one frombuffer, converted into a preallocated float32 (CHUNK_ROWS, dims) buffer,
            buf[:k] @ query (same rows, same order, same shape as today's vstack().astype()).
            Fix round (USE_BIT_SHIFT): when every half is finite and the float32 query is small
            enough, the conversion is 3 integer passes (sign-extend, << 13, & 0x8FFFFFFF: the
            float16 value times 2**-112, exact) scored against query * 2**112: every product is
            the same real number as today's, so scores stay bit-identical; else numpy's cast.
  passages  rows below the retained k-th score T are dropped (s >= T once k owners are kept;
            NaN dropped). Owner runs: b = run starts, runmax = maximum.reduceat, first argmax per
            run = minimum.reduceat(where(s == repeat(runmax), seq, BIG)). If an owner has two runs
            in the chunk, fall back to lexsort((seq, -s, o)) first-per-owner. Chunk top-k runs by
            np.partition (ties at the threshold: smallest owner ids). Merge with the retained set
            (<= 2k rows) by lexsort, retained winning same-owner ties (seq = -1, today's strict >),
            final order lexsort((owner, -score))[:limit].
  embeddings rows with s > T survive (today's stable sort puts equal later rows after the kept
            ones); chunk top-k by np.partition (ties at the threshold: first in scan order);
            stable argsort merge with the retained set placed first.
Python objects (Match/Scored, start/end lookups) only for the final winners and per-chunk
retained newcomers (<= limit per chunk).
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

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

CHUNK_ROWS: Final = 4_000
STORED: Final = np.dtype(np.float16)
# Fix round: the base pipeline's float16 -> float32 copyto (~250 ns/row) exceeded the budget;
# False restores it everywhere.
USE_BIT_SHIFT = True
_BIG: Final = np.iinfo(np.int64).max
_OWNER: Final = itemgetter(0)
_EMB_VEC: Final = itemgetter(1)
_PAS_VEC: Final = itemgetter(3)
_NO_I: Final = np.empty(0, np.int64)
_NO_F: Final = np.empty(0, np.float64)
_INF16: Final = 0x7C00  # int16 halves >= this are +inf/NaN
_NEG_INF16: Final = 0xFC00  # uint16 halves >= this are -inf/NaN
_F16_IN_F32: Final = np.int32(-0x70000001)  # 0x8FFFFFFF: sign + 28 low bits


@dataclass(frozen=True, slots=True)
class Scored:
    owner_id: int
    score: float


type Kind = Literal["segment", "frame"]
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


# --- shared chunk pipeline ------------------------------------------------------------------


def _chunk_matrix(  # noqa: PLR0913 - one chunk's columns and the scan's fixed state
    rows: list,
    vecs: list,
    width: int,
    keep: set | np.ndarray | None,
    drop: set | np.ndarray | None,
    buf: np.ndarray,
    query,
    scaled: np.ndarray | None,
    o: np.ndarray | None = None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """(scores, kept row indices or None when every row is kept); scores None if none kept.

    The matrix holds exactly the rows today's list comprehension keeps, in scan order.
    """
    m = len(vecs)
    mask = None
    if keep is not None:
        mask = _member(keep, rows, o)
    if drop is not None:
        out = _member(drop, rows, o)
        mask = ~out if mask is None else mask & ~out
    blob = None
    if mask is None:
        blob = b"".join(vecs)
        if len(blob) != m * width or max(map(len, vecs)) != width:
            blob = None
            mask = np.fromiter(map(len, vecs), np.intp, m) == width
    else:
        mask &= np.fromiter(map(len, vecs), np.intp, m) == width
    idx = None
    if blob is None:
        idx = np.flatnonzero(mask)
        if not idx.size:
            return None, None
        blob = b"".join(compress(vecs, mask.tolist())) if idx.size < m else b"".join(vecs)
        if idx.size == m:
            idx = None
    k = len(blob) // width
    mat = buf[:k]
    u = np.frombuffer(blob, np.int16).reshape(k, -1)
    if scaled is not None and u.max() < _INF16 and u.view(np.uint16).max() < _NEG_INF16:
        # Every half is finite: shift its bits into float32 position (value * 2**-112, exact,
        # f16 subnormals included) and score against query * 2**112. Each product is the same
        # real number as today's, so the gemv rounds identically (bit-identical scores).
        w = mat.view(np.int32)
        np.copyto(w, u)  # sign-extend
        np.left_shift(w, 13, out=w)
        np.bitwise_and(w, _F16_IN_F32, out=w)
        return mat @ scaled, idx
    np.copyto(mat, u.view(STORED))  # a NaN/inf half: today's exact float16 -> float32 cast
    return mat @ query, idx


def _scaled(query) -> np.ndarray | None:
    """query * 2**112 when that is exact and finite for a float32 vector, else None."""
    if not (isinstance(query, np.ndarray) and query.dtype == np.float32 and query.ndim == 1):
        return None
    with np.errstate(invalid="ignore"):
        if not (np.isfinite(query).all() and np.abs(query).max(initial=0.0) < 2.0**15):
            return None
    return query * np.float32(2.0**112)


_TABLE_MAX: Final = 1 << 24  # owner ids up to this use a bool table (<= 16 MB), else sets


def _int_ids(ids: list) -> np.ndarray | None:
    """ids as an int64 array when they are all ints in [0, _TABLE_MAX), else None."""
    try:
        arr = np.array(ids)
    except (OverflowError, ValueError, TypeError):
        return None
    if arr.ndim != 1 or arr.dtype.kind not in "iu" or arr.min() < 0 or arr.max() >= _TABLE_MAX:
        return None
    return arr.astype(np.int64, copy=False)


def _filters(only_ids: list | None, exclude_ids: list) -> tuple:
    """(keep, drop) for _member: one bool table indexed by owner id (last entry False, the
    target of every out-of-range id) when the ids are small non-negative ints, else sets
    (today's semantics: ``owner in set(only)`` and ``owner not in set(exclude)``)."""
    lists = [x for x in (only_ids, exclude_ids) if x]
    arrs = [_int_ids(x) for x in lists]
    if any(a is None for a in arrs):
        keep = None if only_ids is None else set(only_ids)
        drop = set(exclude_ids) or None
        if keep is not None and drop is not None:
            keep, drop = keep - drop, None
        return keep, drop
    if not arrs:
        return None, None
    size = max(int(a.max()) for a in arrs) + 2
    if only_ids is not None:
        table = np.zeros(size, np.bool_)
        table[arrs[0]] = True
        if exclude_ids:
            table[arrs[1]] = False
        return table, None
    table = np.zeros(size, np.bool_)
    table[arrs[0]] = True
    return None, table


def _member(f: set | np.ndarray, rows: list, o: np.ndarray | None) -> np.ndarray:
    """Bool mask: each row's owner is in ``f`` (table lookup or set membership)."""
    if isinstance(f, np.ndarray):
        return f[np.minimum(o.view(np.uint64), f.size - 1)]  # negative ids wrap to huge
    return np.fromiter(map(f.__contains__, map(_OWNER, rows)), np.bool_, len(rows))


# --- embeddings -----------------------------------------------------------------------------


def _embeddings_chunk(  # noqa: PLR0913 - one chunk against the retained set
    rows: list, width: int, keep, drop, buf: np.ndarray, query, scaled, limit: int,
    r_s: np.ndarray, r_o: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    vecs = list(map(_EMB_VEC, rows))
    o = None
    if isinstance(keep, np.ndarray) or isinstance(drop, np.ndarray):
        o = np.fromiter(map(_OWNER, rows), np.int64, len(rows))
    s, idx = _chunk_matrix(rows, vecs, width, keep, drop, buf, query, scaled, o)
    if s is None:
        return r_s, r_o
    sel = np.flatnonzero(s > r_s[-1] if r_s.size == limit else s == s)  # NaN never survives
    if not sel.size:
        return r_s, r_o
    ss = s[sel]
    if sel.size > limit:  # chunk top-k, ties at the threshold: first in scan order
        thr = np.partition(ss, sel.size - limit)[sel.size - limit]
        take = ss > thr
        eq = np.flatnonzero(ss == thr)
        take[eq[: limit - int(np.count_nonzero(take))]] = True
        sel, ss = sel[take], ss[take]
    rows_at = sel if idx is None else idx[sel]
    ids = np.fromiter((rows[i][0] for i in rows_at.tolist()), np.int64, sel.size)
    all_s = np.concatenate((r_s, ss.astype(np.float64)))
    all_o = np.concatenate((r_o, ids))
    order = np.argsort(-all_s, kind="stable")[:limit]  # retained first: today's stable sort
    return all_s[order], all_o[order]


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
    """The ``limit`` owners most similar to ``query``, best first (ties: scan order)."""
    only_ids = None if only is None else list(only)
    exclude_ids = list(exclude)
    if only_ids is not None and not only_ids:
        return []
    cursor = conn.execute(
        "SELECT owner_id, vec FROM embeddings WHERE owner_kind = ? AND model = ?"
        " AND start_ms >= ? AND start_ms < ?",
        (owner_kind, model, start_ms, end_ms),
    )
    if limit <= 0:
        return []
    unit_query = np.ascontiguousarray(query, dtype=np.float32)
    width = len(unit_query) * STORED.itemsize
    keep, drop = _filters(only_ids, exclude_ids)
    buf = np.empty((CHUNK_ROWS, len(unit_query)), np.float32)
    scaled = _scaled(unit_query) if USE_BIT_SHIFT else None
    r_s, r_o = _NO_F, _NO_I  # retained, best first, ties in scan order
    with np.errstate(invalid="ignore", over="ignore"):
        while True:
            rows = cursor.fetchmany(CHUNK_ROWS)
            if not rows:
                break
            r_s, r_o = _embeddings_chunk(
                rows, width, keep, drop, buf, unit_query, scaled, limit, r_s, r_o
            )
            del rows  # free this chunk's objects before the next fetch allocates its own
    return [Scored(o, s) for o, s in zip(r_o.tolist(), r_s.tolist(), strict=True)]


# --- passages -------------------------------------------------------------------------------


def _owner_best(s: np.ndarray, o: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(position of each owner's first max row, owner) for one chunk's surviving rows."""
    n = s.size
    if n == 1:
        return np.zeros(1, np.int64), o
    starts = np.flatnonzero(o[1:] != o[:-1]) + 1
    b = np.empty(starts.size + 1, np.int64)
    b[0] = 0
    b[1:] = starts
    ro = o[b]
    if np.unique(ro).size == b.size:  # one run per owner (the plans tested keep owners contiguous)
        runmax = np.maximum.reduceat(s, b)
        lens = np.diff(np.append(b, n))
        seq = np.arange(n, dtype=np.int64)
        pos = np.minimum.reduceat(np.where(s == np.repeat(runmax, lens), seq, _BIG), b)
        return pos, ro
    order = np.lexsort((np.arange(n), -s, o))  # fallback: first max row per owner
    so = o[order]
    first = np.ones(n, np.bool_)
    first[1:] = so[1:] != so[:-1]
    pos = order[first]
    return pos, o[pos]


def _top_owners(score: np.ndarray, owner: np.ndarray, limit: int) -> np.ndarray:
    """Indices of the ``limit`` best (score desc, owner asc); owners are unique."""
    n = score.size
    if n <= limit:
        return np.arange(n)
    thr = score[np.argpartition(score, n - limit)[n - limit]]
    gt = np.flatnonzero(score > thr)
    eq = np.flatnonzero(score == thr)
    need = limit - gt.size
    if eq.size > need:
        eq = eq[np.argpartition(owner[eq], need - 1)[:need]] if need > 0 else eq[:0]
    return np.concatenate((gt, eq))


def select_passages(  # noqa: PLR0913 - one chunk against the retained set
    s: np.ndarray,
    o: np.ndarray,
    limit: int,
    r_o: np.ndarray,
    r_s: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """Merge one chunk (scores s, owners o in scan order) into the retained set.

    Returns (owner, score, retained index or -1, chunk position or -1) of the new retained
    set in final order, or None when the chunk changes nothing.
    """
    if r_s.size == limit:
        sel = np.flatnonzero(s >= r_s[-1])  # T: equal scores may still win on owner id
    else:
        sel = np.flatnonzero(s == s)  # drop NaN
    if not sel.size:
        return None
    pos, ro = _owner_best(s[sel], o[sel])
    pos = sel[pos]
    rs = s[pos]
    ch = _top_owners(rs, ro, limit)
    nr = r_o.size
    all_o = np.concatenate((r_o, ro[ch]))
    all_s = np.concatenate((r_s, rs[ch].astype(np.float64)))
    all_seq = np.concatenate((np.full(nr, -1, np.int64), pos[ch]))
    neg = -all_s
    order = np.lexsort((all_seq, neg, all_o))  # per owner: best score, then retained/first row
    so = all_o[order]
    first = np.ones(so.size, np.bool_)
    first[1:] = so[1:] != so[:-1]
    u = order[first]
    fin = u[np.lexsort((all_o[u], neg[u]))][:limit]
    src = np.where(fin < nr, fin, -1)
    return all_o[fin], all_s[fin], src, np.where(fin < nr, -1, all_seq[fin])


def _passages_chunk(  # noqa: PLR0913 - one chunk against the retained set
    rows: list, width: int, allowed, buf: np.ndarray, query, scaled, limit: int, kept: tuple,
) -> tuple:
    r_o, r_s, r_a, r_b = kept
    vecs = list(map(_PAS_VEC, rows))
    o = np.fromiter(map(_OWNER, rows), np.int64, len(rows))
    s, idx = _chunk_matrix(rows, vecs, width, allowed, None, buf, query, scaled, o)
    if s is None:
        return kept
    if idx is not None:
        o = o[idx]
    got = select_passages(s, o, limit, r_o, r_s)
    if got is None:
        return kept
    n_o, n_s, src, cpos = got
    old = src >= 0
    new_rows = cpos[~old] if idx is None else idx[cpos[~old]]
    a = np.empty(n_o.size, np.int64)
    e = np.empty(n_o.size, np.int64)
    a[old] = r_a[src[old]]
    e[old] = r_b[src[old]]
    picked = [rows[i] for i in new_rows.tolist()]
    a[~old] = [int(r[1]) for r in picked]
    e[~old] = [int(r[2]) for r in picked]
    return n_o, n_s, a, e


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
    only_ids = None if only is None else list(only)
    if only_ids is not None and not only_ids:
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
    if limit <= 0:
        return []
    allowed, _ = _filters(only_ids, [])
    q = query if isinstance(query, np.ndarray) else np.asarray(query)
    width = len(query) * 2
    buf = np.empty((CHUNK_ROWS, len(query)), np.float32)
    scaled = _scaled(q) if USE_BIT_SHIFT else None
    kept = (_NO_I, _NO_F, _NO_I, _NO_I)  # owner, score, start_char, end_char; final order
    with np.errstate(invalid="ignore", over="ignore"):
        while True:
            rows = cursor.fetchmany(CHUNK_ROWS)
            if not rows:
                break
            kept = _passages_chunk(rows, width, allowed, buf, q, scaled, limit, kept)
            del rows  # free this chunk's objects before the next fetch allocates its own
    r_o, r_s, r_a, r_b = kept
    return [
        Match(o, s, a, b)
        for o, s, a, b in zip(r_o.tolist(), r_s.tolist(), r_a.tolist(), r_b.tolist(), strict=True)
    ]
