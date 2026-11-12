"""Media sessions, game sessions and the queries about them. App sessions live in
``pipeline/sessions.py`` (derived from frames); this module has the capture-side writers.

Rows stay open while the thing is happening and carry a heartbeat ``end_ms``. After a crash the
next start closes leftovers at their last heartbeat, so a session never claims time it didn't see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection

# A game row whose heartbeat is older than this is treated as finished (crashed capture).
GAME_HEARTBEAT_MAX_AGE_MS: Final = 30_000


class MediaLog:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def open(self, *, app_id: str, app: str, title: str, artist: str | None, ts_ms: int) -> int:
        with db.transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO media_sessions (app_id, app, title, artist, start_ms, end_ms)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (app_id, app, title, artist or None, ts_ms, ts_ms),
            )
            row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover - INSERT always sets it
            msg = "insert returned no row id"
            raise RuntimeError(msg)
        return row_id

    def touch(self, session_id: int, *, end_ms: int, played_ms: int) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "UPDATE media_sessions SET end_ms = ?, played_ms = ? WHERE id = ?",
                (end_ms, played_ms, session_id),
            )

    def add_event(self, session_id: int, ts_ms: int, state: str) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO media_events (media_session_id, ts_ms, state) VALUES (?, ?, ?)",
                (session_id, ts_ms, state),
            )

    def set_url(self, session_id: int, url: str) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute("UPDATE media_sessions SET url = ? WHERE id = ?", (url, session_id))

    def close(self, session_id: int, *, end_ms: int, played_ms: int) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "UPDATE media_sessions SET end_ms = ?, played_ms = ?, closed = 1 WHERE id = ?",
                (end_ms, played_ms, session_id),
            )

    def close_stale(self) -> int:
        """Close rows left open by a crash, at their last heartbeat."""
        with db.transaction(self._conn) as conn:
            return conn.execute("UPDATE media_sessions SET closed = 1 WHERE closed = 0").rowcount

    def find_url(self, title: str, since_ms: int) -> str | None:
        """The address of a recent browser frame whose window title contains ``title``.

        Frames already went through exclusions, so this can only return URLs we are allowed to
        keep. ``instr`` is case-sensitive, which is right: the tab title repeats the media title.
        """
        row = self._conn.execute(
            "SELECT url FROM frames WHERE ts_ms >= ? AND url IS NOT NULL"
            " AND instr(window_title, ?) > 0 ORDER BY ts_ms DESC LIMIT 1",
            (since_ms, title),
        ).fetchone()
        return str(row[0]) if row else None


class GameLog:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def open(self, *, process: str, window_title: str | None, ts_ms: int) -> int:
        with db.transaction(self._conn) as conn:
            cursor = conn.execute(
                "INSERT INTO game_sessions (process, window_title, start_ms, end_ms)"
                " VALUES (?, ?, ?, ?)",
                (process, window_title, ts_ms, ts_ms),
            )
            row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover - INSERT always sets it
            msg = "insert returned no row id"
            raise RuntimeError(msg)
        return row_id

    def heartbeat(self, session_id: int, ts_ms: int) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute("UPDATE game_sessions SET end_ms = ? WHERE id = ?", (ts_ms, session_id))

    def close(self, session_id: int, ts_ms: int) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "UPDATE game_sessions SET end_ms = ?, closed = 1 WHERE id = ?", (ts_ms, session_id)
            )

    def close_stale(self) -> int:
        with db.transaction(self._conn) as conn:
            return conn.execute("UPDATE game_sessions SET closed = 1 WHERE closed = 0").rowcount


def gaming_now(conn: Connection, now_ms: int) -> bool:
    """True while a game session is open and its heartbeat is fresh. The worker's scheduler
    asks this before starting a GPU job."""
    row = conn.execute(
        "SELECT 1 FROM game_sessions WHERE closed = 0 AND end_ms >= ? LIMIT 1",
        (now_ms - GAME_HEARTBEAT_MAX_AGE_MS,),
    ).fetchone()
    return row is not None
