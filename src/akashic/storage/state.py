"""Named integers in the ``state`` table: watermarks and the user's paused
choice."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection

# The user's recording choice (1 = paused), read and written only by the app process and kept
# across Lock, Quit and Delete all.
CAPTURE_PAUSED: Final = "capture_paused"

# The last time (ms) a running capture process said it was alive; the next start uses it to date
# a crash.
CAPTURE_ALIVE: Final = "capture_alive_ms"


# 1 once every segment with audio and no speaker turns has a speakers job; 0 when one was
# transcribed without it (voice models absent).
SPEAKERS_BACKFILLED: Final = "speakers_backfilled"


def get_int(conn: Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return default if row is None else int(row[0])


def set_int_in(conn: Connection, key: str, value: int) -> None:
    """Write inside the caller's open transaction."""
    conn.execute(
        "INSERT INTO state (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def set_int(conn: Connection, key: str, value: int) -> None:
    with db.transaction(conn):
        set_int_in(conn, key, value)
