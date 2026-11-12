"""Named integers in the ``state`` table: watermarks and short-lived flags shared by processes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection

# Until when (ms) someone is watching the live microphone meter in Settings. The API sets it on
# every meter poll; capture publishes its numbers every second only while it is in the future.
LEVELS_WANTED_UNTIL: Final = "levels_wanted_until_ms"


def get_int(conn: Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return default if row is None else int(row[0])


def set_int(conn: Connection, key: str, value: int) -> None:
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
