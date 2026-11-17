"""Keyword search over the windows of frames (FTS5) and plain browsing by time.

User input never reaches FTS5 syntax: it is split into words, each word is quoted, and the last
one is a prefix match so results appear while typing. Highlight markers in snippets are control
characters, not HTML, so the UI decides how to render them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from akashic.storage.monitors import SHOWN_SQL

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
    " AND (:process IS NULL OR lower(w.process) = lower(:process))"
)

_BROWSE_FIRST_SCAN_FACTOR: Final = 4
_BROWSE_MAX_SCAN: Final = 100_000

# True for window row `c` when it does not repeat the previous row `p` of its hwnd (same process,
# title, URL and text); a repeated row continues the run that `p` belongs to.
STARTS_RUN_SQL: Final = (
    "(p.id IS NULL OR c.process IS NOT p.process OR c.title IS NOT p.title"
    " OR c.url IS NOT p.url OR c.text IS NOT p.text)"
)


# The newest :scanned app rows, each with its previous capture of the same hwnd (one seek in
# frame_windows_hwnd; none if older than :start_ms), and whether it starts a run (last column).
_BROWSE_SQL: Final = (
    "SELECT c.frame_id, c.window_id, c.ts_ms, c.monitor_id, c.process, c.title, c.url,"  # noqa: S608
    f" c.has_image, c.snippet, {STARTS_RUN_SQL}"
    " FROM (SELECT f.id AS frame_id, w.id AS window_id, f.ts_ms, f.monitor_id, w.z, w.hwnd,"
    "  w.process, w.title, w.url, w.text,"
    f"  {SHOWN_SQL} AS has_image, substr(coalesce(w.text, ''), 1, 160) AS snippet"
    "  FROM frame_windows w JOIN frames f ON f.id = w.frame_id"
    f"  WHERE w.role = 'app'{_FILTER_SQL}"
    "  ORDER BY f.ts_ms DESC, f.id DESC, w.z LIMIT :scanned) AS c"
    " LEFT JOIN frame_windows p ON p.id = ("
    "  SELECT w2.id FROM frame_windows w2"
    "  WHERE w2.role = 'app' AND w2.hwnd = c.hwnd AND w2.frame_id < c.frame_id"
    "  ORDER BY w2.frame_id DESC LIMIT 1)"
    "  AND (SELECT ts_ms FROM frames WHERE id = p.frame_id) >= :start_ms"
    " ORDER BY c.ts_ms DESC, c.frame_id DESC, c.z"
)


@dataclass(frozen=True, slots=True)
class Filters:
    start_ms: int = 0
    end_ms: int = 2**62
    process: str | None = None


@dataclass(frozen=True, slots=True)
class Hit:
    frame_id: int
    window_id: int
    ts_ms: int
    monitor_id: int
    process: str | None
    title: str | None
    url: str | None
    has_image: bool  # the stored picture shows this window (its crop can be served)
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


def _params(filters: Filters, limit: int, offset: int) -> dict[str, Any]:
    return {
        "start_ms": filters.start_ms,
        "end_ms": filters.end_ms,
        "process": filters.process,
        "limit": _clamp(limit),
        "offset": max(0, offset),
    }


def _hit(row: Sequence[Any]) -> Hit:
    return Hit(row[0], row[1], row[2], row[3], row[4], row[5], row[6], bool(row[7]), row[8] or "")


def search(
    conn: Connection, query: str, filters: Filters | None = None, limit: int = 30, offset: int = 0
) -> list[Hit]:
    """App windows matching ``query``, best match first, newest first among equals.

    A window row that repeats the previous matching row of the same hwnd (same process, title,
    URL and text) is dropped, so a run of identical captures shows as its first row.
    """
    match = build_match(query)
    if match is None:
        return []
    params = _params(filters or Filters(), limit, offset)
    params["match"] = match
    rows = conn.execute(
        "SELECT m.frame_id, m.window_id, m.ts_ms, m.monitor_id, c.process, c.title, c.url,"  # noqa: S608
        " m.has_image, m.snippet"
        " FROM (SELECT *, LAG(window_id) OVER run AS prev_id FROM ("
        "  SELECT f.id AS frame_id, w.id AS window_id, f.ts_ms, f.monitor_id,"
        f" {SHOWN_SQL} AS has_image, w.hwnd,"
        f" snippet(frame_windows_fts, -1, '{HIGHLIGHT_START}', '{HIGHLIGHT_END}', '…', 24)"
        "  AS snippet, bm25(frame_windows_fts, 1.0, 3.0, 2.0) AS score"
        "  FROM frame_windows_fts JOIN frame_windows w ON w.id = frame_windows_fts.rowid"
        "  JOIN frames f ON f.id = w.frame_id"
        f"  WHERE frame_windows_fts MATCH :match{_FILTER_SQL})"
        "  WINDOW run AS (PARTITION BY hwnd ORDER BY ts_ms, window_id))"
        " AS m JOIN frame_windows c ON c.id = m.window_id"
        " LEFT JOIN frame_windows p ON p.id = m.prev_id"
        f" WHERE {STARTS_RUN_SQL}"
        " ORDER BY m.score, m.ts_ms DESC LIMIT :limit OFFSET :offset",
        params,
    ).fetchall()
    return [_hit(r) for r in rows]


def browse(
    conn: Connection,
    filters: Filters | None = None,
    limit: int = 60,
    offset: int = 0,
) -> list[Hit]:
    """App windows of frames in a time window without a text query, newest first.

    Runs of identical captures collapse to their first row. A run continues only from the
    window's own previous capture, so a different capture of that window in between starts a new
    run (unlike `search`, which compares only matching rows).
    """
    params = _params(filters or Filters(), limit, offset)
    page_start = params["offset"]
    page_end = page_start + params["limit"]
    scanned = page_end * _BROWSE_FIRST_SCAN_FACTOR
    while True:
        rows = conn.execute(_BROWSE_SQL, {**params, "scanned": scanned}).fetchall()
        run_starts = [r for r in rows if r[9]]
        if len(run_starts) >= page_end or len(rows) < scanned or scanned >= _BROWSE_MAX_SCAN:
            return [_hit(r) for r in run_starts[page_start:page_end]]
        scanned = min(scanned * 2, _BROWSE_MAX_SCAN)


def neighbors(conn: Connection, frame_id: int) -> tuple[int | None, int | None]:
    """Ids of the frame just before and just after ``frame_id`` in time on the same monitor
    (previous, next), so stepping never jumps to another screen."""
    row = conn.execute("SELECT ts_ms, monitor_id FROM frames WHERE id = ?", (frame_id,)).fetchone()
    if row is None:
        return None, None
    ts_ms: int = row[0]
    monitor_id: int = row[1]
    prev = conn.execute(
        "SELECT id FROM frames WHERE monitor_id = ? AND (ts_ms, id) < (?, ?)"
        " ORDER BY ts_ms DESC, id DESC LIMIT 1",
        (monitor_id, ts_ms, frame_id),
    ).fetchone()
    nxt = conn.execute(
        "SELECT id FROM frames WHERE monitor_id = ? AND (ts_ms, id) > (?, ?)"
        " ORDER BY ts_ms, id LIMIT 1",
        (monitor_id, ts_ms, frame_id),
    ).fetchone()
    return (prev[0] if prev else None, nxt[0] if nxt else None)


def processes(conn: Connection) -> list[str]:
    """Distinct app names seen so far, for the filter dropdown."""
    # Loose index scan: SELECT DISTINCT would read every frame_windows row.
    rows = conn.execute(
        "WITH RECURSIVE seen(process) AS ("
        " SELECT min(process) FROM frame_windows"
        " UNION ALL"
        " SELECT (SELECT min(process) FROM frame_windows WHERE process > seen.process)"
        " FROM seen WHERE process IS NOT NULL)"
        " SELECT process FROM seen WHERE process IS NOT NULL ORDER BY lower(process)"
    ).fetchall()
    return [r[0] for r in rows]
