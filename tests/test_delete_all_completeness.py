"""Audit #10: "Delete all recorded data" must reach every table, not only the ones listed today.

``delete_all_recorded_data`` deletes a hand-written list of tables. These tests derive coverage
from the schema itself: a table is covered when it is deleted directly, when every row of it is
removed by an ``ON DELETE CASCADE`` from a covered table, or when it is the full-text index (or
an index shadow table) of a covered table. Anything else must be on the explicit keep-list, so a
new table holding captured or derived data fails here until it is deleted or deliberately kept.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

import numpy as np
from memory_data import NOW, add_frame, add_segment

from akashic.storage import embeddings
from akashic.storage.retention import delete_all_recorded_data

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.storage.media import MediaStore

# SQLite bookkeeping, no user data: AUTOINCREMENT counters only.
KEEP: Final = frozenset({"sqlite_sequence"})
FTS_SHADOWS: Final = ("_data", "_idx", "_docsize", "_config", "_content")
_DELETE = re.compile(r'^\s*DELETE\s+FROM\s+"?(\w+)"?', re.IGNORECASE)
_CONTENT = re.compile(r"content\s*=\s*'?(\w+)'?", re.IGNORECASE)


def schema(conn: Connection) -> dict[str, str]:
    return {
        str(name): str(sql or "")
        for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'")
    }


def directly_deleted(conn: Connection, media: MediaStore) -> set[str]:
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        delete_all_recorded_data(conn, media)
    finally:
        conn.set_trace_callback(None)
    return {m.group(1) for s in statements if (m := _DELETE.match(s))}


def _cascades_fully(conn: Connection, table: str, covered: set[str]) -> bool:
    """Every row goes when its parent goes: a NOT NULL (or primary key) CASCADE reference."""
    columns = {
        str(c[1]): bool(c[3]) or bool(c[5]) for c in conn.execute(f"PRAGMA table_info('{table}')")
    }
    return any(
        str(fk[2]) in covered and str(fk[6]).upper() == "CASCADE" and columns.get(str(fk[3]), False)
        for fk in conn.execute(f"PRAGMA foreign_key_list('{table}')")
    )


def uncovered_tables(conn: Connection, deleted: set[str]) -> set[str]:
    tables = schema(conn)
    covered = set(deleted)
    virtual = {n for n, sql in tables.items() if sql.upper().startswith("CREATE VIRTUAL TABLE")}
    changed = True
    while changed:
        changed = False
        for name, sql in tables.items():
            if name in covered:
                continue
            content = _CONTENT.search(sql) if name in virtual else None
            shadow_of = next(
                (v for v in virtual if name.startswith(v) and name[len(v) :] in FTS_SHADOWS), None
            )
            if (
                (content is not None and content.group(1) in covered)
                or (shadow_of is not None and shadow_of in covered)
                or (name not in virtual and _cascades_fully(conn, name, covered))
            ):
                covered.add(name)
                changed = True
    return set(tables) - covered - KEEP


def test_every_table_is_deleted_or_explicitly_kept(conn: Connection, media: MediaStore) -> None:
    deleted = directly_deleted(conn, media)
    assert deleted, "no DELETE statement was traced"
    assert uncovered_tables(conn, deleted) == set()


def test_the_check_notices_a_new_table_missing_from_the_delete_list(
    conn: Connection, media: MediaStore
) -> None:
    conn.execute("CREATE TABLE clipboard_history (ts_ms INTEGER, text TEXT)")
    conn.execute(
        "CREATE TABLE clipboard_tags (clip_id INTEGER REFERENCES clipboard_history(rowid)"
        " ON DELETE CASCADE, tag TEXT)"
    )
    deleted = directly_deleted(conn, media)
    assert uncovered_tables(conn, deleted) == {"clipboard_history", "clipboard_tags"}


def test_a_nullable_cascade_reference_does_not_count_as_covered(
    conn: Connection, media: MediaStore
) -> None:
    conn.execute(
        "CREATE TABLE notes (frame_id INTEGER REFERENCES frames(id) ON DELETE CASCADE, body TEXT)"
    )
    assert uncovered_tables(conn, directly_deleted(conn, media)) == {"notes"}


def test_every_captured_row_is_gone_after_full_deletion(
    conn: Connection, media: MediaStore, paths: Paths
) -> None:
    frame = add_frame(conn, NOW, "fictionalphrase écran", "Fenêtre")
    segment = add_segment(conn, NOW, text="fictionalphrase parole")
    conn.execute("INSERT INTO frame_passages VALUES (?, 'm', 0, 5, 1, x'0000')", (frame,))
    conn.execute("INSERT INTO segment_passages VALUES (?, 'm', 0, 5, 1, x'0000')", (segment,))
    embeddings.put(conn, "segment", segment, "m", np.ones(2, dtype=np.float32), NOW, NOW)
    media.write_unique("2026/11/08/120000_000_m0.webp", b"synthetic screenshot")
    delete_all_recorded_data(conn, media)
    tables = [n for n in schema(conn) if n not in KEEP]
    non_empty = {
        name
        for name in tables
        if conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]  # noqa: S608 - schema names
        and not any(name.endswith(s) for s in FTS_SHADOWS)
    }
    assert non_empty == set()
    for fts in ("frames_fts", "audio_segments_fts"):
        hits = conn.execute(f"SELECT count(*) FROM {fts} WHERE {fts} MATCH 'fictionalphrase'")  # noqa: S608
        assert hits.fetchone() == (0,)
    assert list(paths.media_dir.iterdir()) == []
