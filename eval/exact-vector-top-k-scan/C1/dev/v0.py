"""C1: columnar chunk scoring with an integer float16 rebias into reused buffers.

Same results as today's passages.top / embeddings.top (ref_today.py), with Python objects only
for the final winners of each chunk:

- per fetchmany(4000) chunk: the blobs and the eligibility mask (vector size, only, exclude)
  are built columnar; the kept rows (same rows and order as today's filtered list, hence the
  same matvec shape and bit-identical float32 scores) are joined into one bytes object;
- float16 -> float32: the int16 bits are sign-extended into a reused int32 buffer, shifted
  left by 13 and masked with 0x8FFFFFFF. Viewed as float32 this is exactly half * 2**-112 for
  every finite half (subnormals and +-0 included), so ``w @ (q * 2**112)`` multiplies the
  same real products as ``half.astype(float32) @ q``. A chunk holding an inf/NaN half (exponent
  31), a query that overflows when scaled, or a BLAS that flushes subnormals (self-test) uses
  the plain astype conversion into the same reused buffer instead;
- top-k on arrays: only rows that can still enter the retained set survive (embeddings:
  s > T, passages: s >= T), then a stable merge with the retained set first. NaN scores are
  dropped (today's order is undefined there).
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
_UP: Final = np.float32(2.0**112)
_MASK: Final = np.int32(-0x70000001)  # 0x8FFFFFFF: sign, low 5 exponent bits, mantissa
_EXP: Final = np.int16(0x7C00)  # float16 exponent bits; all set = inf or NaN
_ID, _VEC4, _VEC2 = itemgetter(0), itemgetter(3), itemgetter(1)


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


# --- float16 rows -> float32 scores ---------------------------------------------------------

_blas_keeps_subnormals: bool | None = None


def _rebias(u: NDArray[np.int16], out: NDArray[np.int32]) -> NDArray[np.float32]:
    np.copyto(out, u)
    np.left_shift(out, 13, out=out)
    np.bitwise_and(out, _MASK, out=out)
    return out.view(np.float32)


def _rebias_is_exact() -> bool:
    """The rebias relies on float32 subnormals reaching BLAS unflushed (no DAZ/FTZ).

    Checked once per process on a chunk-sized matrix (threaded gemv), and on every call for
    the calling thread (its MXCSR could change at any time).
    """
    global _blas_keeps_subnormals  # noqa: PLW0603
    tiny = np.array([[0x0001, 0x0001]], np.int16)  # smallest subnormal half, twice
    one = np.array([_UP, _UP], np.float32)
    if (_rebias(tiny, np.empty((1, 2), np.int32)) @ one)[0] != np.float32(2.0**-23):
        return False
    if _blas_keeps_subnormals is None:
        rng = np.random.default_rng(0)
        bits = rng.integers(-32768, 32768, (CHUNK_ROWS, 256), dtype=np.int16)
        bits[(bits & _EXP) == _EXP] &= np.int16(0x03FF)  # finite halves; many subnormals
        bits[:, ::3] &= np.int16(-0x7C01)  # force a third of them subnormal (exponent 0)
        q = rng.standard_normal(256).astype(np.float32)
        a = bits.view(np.float16).astype(np.float32) @ q
        b = _rebias(bits, np.empty(bits.shape, np.int32)) @ (q * _UP)
        _blas_keeps_subnormals = bool(np.array_equal(a.view(np.int32), b.view(np.int32)))
    return _blas_keeps_subnormals


class _Scorer:
    """Per-call reusable buffers (thread-safe: nothing shared between calls)."""

    def __init__(self, query: np.ndarray, *, exact_query: bool) -> None:
        self.q = query
        self.d = len(query)
        self.up = None
        if exact_query and _rebias_is_exact():
            with np.errstate(over="ignore"):
                up = query * _UP
            if np.isfinite(up).all():
                self.up = up
        self.i32: np.ndarray | None = None
        self.t16: np.ndarray | None = None

    def scores(self, joined: bytes, m: int) -> np.ndarray:
        d = self.d
        u = np.frombuffer(joined, np.int16).reshape(m, d)
        if self.i32 is None:
            self.i32 = np.empty((CHUNK_ROWS, d), np.int32)
            self.t16 = np.empty((CHUNK_ROWS, d), np.int16)
        w = self.i32[:m]
        if self.up is not None:
            t = np.bitwise_and(u, _EXP, out=self.t16[:m])
            if t.max() != _EXP:
                return _rebias(u, w) @ self.up
        f = w.view(np.float32)
        np.copyto(f, u.view(np.float16))
        return f @ self.q


def _eligible(rows: list, blobs: list, wanted: int, allowed, excluded) -> np.ndarray:
    n = len(rows)
    ok = np.fromiter(map(len, blobs), np.intp, n) == wanted
    if allowed is not None or excluded:
        ids = list(map(_ID, rows))
        if allowed is not None:
            ok &= np.fromiter(map(allowed.__contains__, ids), bool, n)
        if excluded:
            ok &= ~np.fromiter(map(excluded.__contains__, ids), bool, n)
    return ok


# --- embeddings.top -------------------------------------------------------------------------


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
    wanted = len(unit_query) * STORED.itemsize
    if limit <= 0:
        return []
    scorer = _Scorer(unit_query, exact_query=unit_query.ndim == 1)
    best_ids: list[int] = []
    best_s = np.empty(0, np.float32)
    while rows := cursor.fetchmany(CHUNK_ROWS):
        blobs = list(map(_VEC2, rows))
        ok = _eligible(rows, blobs, wanted, allowed, excluded)
        kept = np.flatnonzero(ok)
        m = len(kept)
        if not m:
            continue
        if m < len(rows):
            blobs = list(compress(blobs, ok.tolist()))
        s = scorer.scores(b"".join(blobs), m)
        # Strictly better than the last retained score: an equal later row sorts after it.
        live = s > best_s[-1] if len(best_s) >= limit else ~np.isnan(s)
        pos = np.flatnonzero(live)
        if not len(pos):
            continue
        nb = len(best_s)
        both = np.concatenate((best_s, s[pos]))
        order = np.argsort(-both, kind="stable")[:limit].tolist()
        best_ids = [best_ids[i] if i < nb else rows[kept[pos[i - nb]]][0] for i in order]
        best_s = both[order]
    return [Scored(i, s) for i, s in zip(best_ids, best_s.tolist(), strict=True)]


# --- passages.top ---------------------------------------------------------------------------

type Kind = Literal["segment", "frame"]
_TABLES: Final = {
    "segment": ("segment_passages", "segment_id", "audio_segments", "transcript", "start_ms"),
    "frame": ("frame_passages", "frame_id", "frames", "text", "ts_ms"),
}


def passages_top(  # noqa: PLR0913, PLR0914 - same filters as the existing exact vector search
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
    """Max-passage score per source (first scanned passage on ties), ordered by
    (-score, owner_id); all filters precede the distinct-source limit."""
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
    wanted = len(query) * 2
    if limit <= 0:
        return []
    exact_query = (
        isinstance(query, np.ndarray)
        and query.dtype == np.float32
        and query.ndim == 1
        and query.flags.c_contiguous
    )
    scorer = _Scorer(query, exact_query=exact_query)
    # Retained winners, ordered by (-score, owner): parallel owner/score arrays + offsets.
    best_o = np.empty(0, np.int64)
    best_s = np.empty(0, np.float32)
    best: list[tuple[int, int, int]] = []  # (owner_id, start_char, end_char) as returned
    while rows := cursor.fetchmany(CHUNK_ROWS):
        blobs = list(map(_VEC4, rows))
        ok = _eligible(rows, blobs, wanted, allowed, None)
        kept = np.flatnonzero(ok)
        m = len(kept)
        if not m:
            continue
        if m < len(rows):
            blobs = list(compress(blobs, ok.tolist()))
        s = scorer.scores(b"".join(blobs), m)
        # >= : an equal score with a smaller owner_id still outranks the last retained source.
        live = s >= best_s[-1] if len(best_s) >= limit else ~np.isnan(s)
        pos = np.flatnonzero(live)
        if not len(pos):
            continue
        src = kept[pos]  # row index inside the chunk, in scan order
        nb = len(best_s)
        owners = np.concatenate((best_o, np.fromiter((rows[i][0] for i in src.tolist()),
                                                     np.int64, len(src))))  # fmt: skip
        scores = np.concatenate((best_s, s[pos]))
        # Per owner: max score; ties keep the retained hit, then the first scanned passage.
        by_owner = np.lexsort((np.arange(len(scores)), -scores, owners))
        o_sorted = owners[by_owner]
        first = np.ones(len(by_owner), bool)
        first[1:] = o_sorted[1:] != o_sorted[:-1]
        win = by_owner[first]
        top = win[np.lexsort((owners[win], -scores[win]))][:limit].tolist()
        new = []
        for i in top:
            if i < nb:
                new.append(best[i])
            else:
                r = rows[src[i - nb]]
                new.append((r[0], r[1], r[2]))
        best = new
        best_o = owners[top]
        best_s = scores[top]
    return [
        Match(int(o), sc, int(a), int(b))
        for (o, a, b), sc in zip(best, best_s.tolist(), strict=True)
    ]
