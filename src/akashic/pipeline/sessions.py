"""Derive ``app_sessions`` (contiguous focus periods) from focused app window rows.

Incremental: the newest session row carries the id of the last frame it consumed, so each run
only reads new frames. A session is one (process, window title, URL) run of focused window rows;
it closes when a different one arrives or when nothing new arrived for ``IDLE_GAP_MS``.

Frames of other monitors have no focused row; a window spanning monitors is focused on each
one's frame, so frames at one moment count once. Frames are only stored when the screen changed,
so a window left untouched stops producing frames: after ``IDLE_GAP_MS`` its session is closed at
the last frame, and a later frame of the same window starts a new row. Excluded windows never
produce window rows, so they have no sessions. ``app_bands`` reads the sessions back for the
timeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection

IDLE_GAP_MS: Final = 5 * 60_000

type _Key = tuple[str | None, str | None, str | None]


def derive_app_sessions(conn: Connection, now_ms: int) -> int:
    """Consume the focused window rows of new frames. Returns how many frames were consumed."""
    row = conn.execute("SELECT COALESCE(MAX(last_frame_id), 0) FROM app_sessions").fetchone()
    watermark: int = row[0]
    frames = conn.execute(
        "SELECT f.id, f.ts_ms, w.process, w.title, w.url FROM frames f"
        " JOIN frame_windows w ON w.frame_id = f.id AND w.focused = 1"
        " WHERE f.id > ? ORDER BY f.id",
        (watermark,),
    ).fetchall()
    open_row = conn.execute(
        "SELECT id, process, window_title, url, end_ms, frame_count FROM app_sessions"
        " WHERE closed = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    with db.transaction(conn):
        current: tuple[int, _Key, int, int] | None = None  # id, key, end_ms, frame_count
        if open_row is not None:
            current = (
                open_row[0],
                (open_row[1], open_row[2], open_row[3]),
                open_row[4],
                open_row[5],
            )
        for frame_id, ts_ms, process, title, url in frames:
            key: _Key = (process, title, url)
            if current is not None and current[1] == key and ts_ms - current[2] <= IDLE_GAP_MS:
                session_id, _, end_ms, count = current
                if ts_ms > end_ms:
                    end_ms, count = ts_ms, count + 1
                conn.execute(
                    "UPDATE app_sessions SET end_ms = ?, frame_count = ?, last_frame_id = ?"
                    " WHERE id = ?",
                    (end_ms, count, frame_id, session_id),
                )
                current = (session_id, key, end_ms, count)
                continue
            if current is not None:
                conn.execute("UPDATE app_sessions SET closed = 1 WHERE id = ?", (current[0],))
            cursor = conn.execute(
                "INSERT INTO app_sessions (process, window_title, url, start_ms, end_ms,"
                " frame_count, last_frame_id) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (process, title, url, ts_ms, ts_ms, frame_id),
            )
            current = (cursor.lastrowid or 0, key, ts_ms, 1)
        if current is not None and now_ms - current[2] > IDLE_GAP_MS:
            conn.execute("UPDATE app_sessions SET closed = 1 WHERE id = ?", (current[0],))
    return len(frames)


@dataclass(frozen=True, slots=True)
class AppBand:
    start_ms: int
    end_ms: int
    process: str | None
    title: str | None


def app_bands(conn: Connection, start_ms: int, end_ms: int, process: str | None) -> list[AppBand]:
    """Sessions overlapping ``[start_ms, end_ms)``, bounds unclipped; ``process`` ignores case."""
    rows = conn.execute(
        "SELECT start_ms, end_ms, process, window_title FROM app_sessions"
        " WHERE :start_ms < :end_ms AND start_ms < :end_ms AND end_ms >= :start_ms"
        " AND (:process IS NULL OR lower(process) = lower(:process))"
        " ORDER BY start_ms, id",
        {"start_ms": start_ms, "end_ms": end_ms, "process": process},
    ).fetchall()
    return [AppBand(*row) for row in rows]
