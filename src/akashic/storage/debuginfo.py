"""Each process's self-description for the Debug page (table ``process_info``).

A process calls :meth:`ProcessInfo.publish` every few seconds with plain numbers, states and
error type names. Never put captured content here: it is shown in the UI without being stored
encrypted any differently from the rest of the database, but the rule is the same as for logs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from akashic.jsonish import as_dict
from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection


@dataclass(frozen=True, slots=True)
class Info:
    name: str
    updated_ms: int
    data: dict[str, object]


class ProcessInfo:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def publish(self, name: str, data: dict[str, object], now_ms: int) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO process_info (name, updated_ms, data) VALUES (?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET updated_ms = excluded.updated_ms,"
                " data = excluded.data",
                (name, now_ms, json.dumps(data, separators=(",", ":"))),
            )

    def read_all(self) -> list[Info]:
        rows: list[tuple[str, int, str]] = self._conn.execute(
            "SELECT name, updated_ms, data FROM process_info ORDER BY name"
        ).fetchall()
        return [Info(n, u, as_dict(json.loads(d)) or {}) for n, u, d in rows]
