"""Keyword search over frames (FTS5) and plain browsing by time.

User input never reaches FTS5 syntax: it is split into words, each word is quoted, and the last
one is a prefix match so results appear while typing. Highlight markers in snippets are control
characters, not HTML, so the UI decides how to render them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from sqlite3 import Connection

HIGHLIGHT_START: Final = "\x02"
HIGHLIGHT_END: Final = "\x03"
MAX_LIMIT: Final = 200
MAX_TERMS: Final = 12
_WORD = re.compile(r"\w+", re.UNICODE)

_FILTER_SQL = (
    " AND f.ts_ms >= :start_ms AND f.ts_ms < :end_ms"
    " AND (:process IS NULL OR lower(f.process) = lower(:process))"
)


@dataclass(frozen=True, slots=True)
class Filters:
    start_ms: int = 0
    end_ms: int = 2**62
    process: str | None = None


@dataclass(frozen=True, slots=True)
class Hit:
    id: int
    ts_ms: int
    monitor: int
    process: str | None
    window_title: str | None
    url: str | None
    has_image: bool
    snippet: str  # text with HIGHLIGHT_START / HIGHLIGHT_END around matches


def build_match(query: str) -> str | None:
    """FTS5 MATCH expression for free text, or None if it holds no searchable word."""
    words = _WORD.findall(query)[:MAX_TERMS]
    if not words:
        return None
    terms = [f'"{w}"' for w in words]
    terms[-1] += "*"
    return " ".join(terms)


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _params(filters: Filters, limit: int, offset: int) -> dict[str, object]:
    return {
        "start_ms": filters.start_ms,
        "end_ms": filters.end_ms,
        "process": filters.process,
        "limit": _clamp(limit),
        "offset": max(0, offset),
    }


def _hit(row: Sequence[Any]) -> Hit:
    return Hit(row[0], row[1], row[2], row[3], row[4], row[5], bool(row[6]), row[7] or "")


def search(
    conn: Connection, query: str, filters: Filters | None = None, limit: int = 30, offset: int = 0
) -> list[Hit]:
    """Frames matching ``query``, best match first, newest first among equals."""
    match = build_match(query)
    if match is None:
        return []
    params = _params(filters or Filters(), limit, offset)
    params["match"] = match
    rows = conn.execute(
        "SELECT f.id, f.ts_ms, f.monitor, f.process, f.window_title, f.url,"  # noqa: S608
        " f.image_path IS NOT NULL,"
        f" snippet(frames_fts, -1, '{HIGHLIGHT_START}', '{HIGHLIGHT_END}', '…', 24)"
        " FROM frames_fts JOIN frames f ON f.id = frames_fts.rowid"
        f" WHERE frames_fts MATCH :match{_FILTER_SQL}"
        " ORDER BY bm25(frames_fts, 1.0, 3.0, 2.0), f.ts_ms DESC LIMIT :limit OFFSET :offset",
        params,
    ).fetchall()
    return [_hit(r) for r in rows]


def browse(
    conn: Connection,
    filters: Filters | None = None,
    limit: int = 60,
    offset: int = 0,
    *,
    newest_first: bool = True,
) -> list[Hit]:
    """Frames in a time window without a text query (the Timeline)."""
    params = _params(filters or Filters(), limit, offset)
    order = "DESC" if newest_first else "ASC"
    rows = conn.execute(
        "SELECT f.id, f.ts_ms, f.monitor, f.process, f.window_title, f.url,"  # noqa: S608
        " f.image_path IS NOT NULL, substr(coalesce(f.text, ''), 1, 160)"
        f" FROM frames f WHERE 1 = 1{_FILTER_SQL}"
        f" ORDER BY f.ts_ms {order}, f.id {order} LIMIT :limit OFFSET :offset",
        params,
    ).fetchall()
    return [_hit(r) for r in rows]


def neighbors(conn: Connection, frame_id: int) -> tuple[int | None, int | None]:
    """Ids of the frame just before and just after ``frame_id`` in time (previous, next)."""
    row = conn.execute("SELECT ts_ms FROM frames WHERE id = ?", (frame_id,)).fetchone()
    if row is None:
        return None, None
    ts_ms: int = row[0]
    prev = conn.execute(
        "SELECT id FROM frames WHERE (ts_ms, id) < (?, ?) ORDER BY ts_ms DESC, id DESC LIMIT 1",
        (ts_ms, frame_id),
    ).fetchone()
    nxt = conn.execute(
        "SELECT id FROM frames WHERE (ts_ms, id) > (?, ?) ORDER BY ts_ms, id LIMIT 1",
        (ts_ms, frame_id),
    ).fetchone()
    return (prev[0] if prev else None, nxt[0] if nxt else None)


def processes(conn: Connection) -> list[str]:
    """Distinct app names seen so far, for the filter dropdown."""
    rows = conn.execute(
        "SELECT DISTINCT process FROM frames WHERE process IS NOT NULL ORDER BY lower(process)"
    ).fetchall()
    return [r[0] for r in rows]
