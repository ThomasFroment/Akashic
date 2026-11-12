"""Reads of stored rows for assertions, kept out of the production stores."""

from __future__ import annotations

from typing import TYPE_CHECKING

from akashic.storage.frames import StoredFrame

if TYPE_CHECKING:
    from sqlite3 import Connection


def latest_frames(conn: Connection, limit: int = 20) -> list[StoredFrame]:
    rows = conn.execute(
        "SELECT id, ts_ms, monitor, trigger, process, window_title, url, text, text_source,"
        " image_path FROM frames ORDER BY ts_ms DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [StoredFrame(*r) for r in rows]


def embedding_count(conn: Connection, owner_kind: str, model: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM embeddings WHERE owner_kind = ? AND model = ?", (owner_kind, model)
    ).fetchone()
    return int(row[0])
