from __future__ import annotations

import errno
import secrets
import sqlite3
from typing import TYPE_CHECKING

import pytest
import sqlcipher3
from memory_data import add_frame, add_monitor, add_window

from akashic.storage import db, state
from akashic.storage.db import DatabaseKeyError, Migration

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from sqlite3 import Connection

    type OpenDb = Callable[[Path, bytes], Connection]


@pytest.fixture
def key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def open_db() -> Iterator[OpenDb]:
    opened: list[Connection] = []

    def open_database(path: Path, db_key: bytes) -> Connection:
        conn = db.connect(path, db_key)
        opened.append(conn)
        return conn

    yield open_database
    for conn in opened:
        conn.close()


def test_database_is_encrypted_on_disk(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    path = tmp_path / "a.db"
    conn = open_db(path, key)
    db.migrate(conn)
    add_frame(conn, 1, "SECRETMARKER visible text")
    conn.close()
    for f in tmp_path.iterdir():
        data = f.read_bytes()
        assert b"SECRETMARKER" not in data
        assert b"SQLite format 3" not in data


def test_wrong_key_is_rejected(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    path = tmp_path / "a.db"
    db.migrate(open_db(path, key))
    with pytest.raises(DatabaseKeyError):
        open_db(path, secrets.token_bytes(32))


def test_key_length(tmp_path: Path, open_db: OpenDb) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        open_db(tmp_path / "a.db", b"short")


def test_pragmas(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    conn = open_db(tmp_path / "a.db", key)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS
    assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
    assert conn.execute("PRAGMA cipher_version").fetchone()[0].startswith("4.")


def test_migrations_apply_once(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    conn = open_db(tmp_path / "a.db", key)
    latest = len(db.load_migrations())
    assert db.migrate(conn) == latest
    assert db.migrate(conn) == latest
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"frames", "frame_windows", "frame_windows_fts", "window_passages"} <= tables
    assert not {"frames_fts", "frame_passages", "question_frames"} & tables
    frame_columns = {r[1] for r in conn.execute("PRAGMA table_info(frames)")}
    assert frame_columns == {"id", "ts_ms", "monitor_id", "trigger", "image_path"}
    for statement in (
        "UPDATE frames SET process = 'x'",
        "UPDATE frames SET window_title = 'x'",
        "UPDATE frames SET url = 'x'",
    ):
        with pytest.raises(sqlcipher3.OperationalError, match="no such column"):
            conn.execute(statement)


def test_failed_migration_rolls_back(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    conn = open_db(tmp_path / "a.db", key)
    migrations = [
        Migration(1, "0001_ok.sql", "CREATE TABLE a (x);"),
        Migration(2, "0002_bad.sql", "CREATE TABLE b (x); THIS IS NOT SQL;"),
    ]
    with pytest.raises(sqlcipher3.OperationalError):
        db.migrate(conn, migrations)
    assert db.schema_version(conn) == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "a" in tables
    assert "b" not in tables


def test_newer_schema_is_refused(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    conn = open_db(tmp_path / "a.db", key)
    conn.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate(conn)


def test_a_store_from_an_earlier_0001_schema_is_refused_unchanged(
    tmp_path: Path, key: bytes, open_db: OpenDb
) -> None:
    # (a) frames without monitor_id; (b) frame_windows without its process index
    stale = {
        "frames": "CREATE TABLE frames (id INTEGER PRIMARY KEY, ts_ms INTEGER, monitor INTEGER)",
        "frame_windows": "CREATE TABLE frame_windows (id INTEGER PRIMARY KEY, process TEXT)",
    }
    messages: list[str] = []
    for table, ddl in stale.items():
        conn = open_db(tmp_path / f"{table}.db", key)
        conn.execute(ddl)
        conn.execute("PRAGMA user_version = 1")  # an earlier 0001, before it was edited in place
        before = list(conn.execute("SELECT type, name, sql FROM sqlite_master"))
        with pytest.raises(db.StaleSchemaError, match="older Akashic") as raised:
            db.migrate(conn)
        messages.append(str(raised.value))
        assert list(conn.execute("SELECT type, name, sql FROM sqlite_master")) == before
        assert db.schema_version(conn) == 1
    assert messages[0] == messages[1]
    for text in ("database layout has since changed", "AKASHIC_DATA_DIR", "Nothing was changed"):
        assert text in messages[0]
    assert "monitor" not in messages[0]


@pytest.mark.parametrize("present", ["frame_windows_process", "frame_windows_hwnd"])
def test_a_store_lacking_one_frame_windows_index_is_refused_unchanged(
    tmp_path: Path, key: bytes, open_db: OpenDb, present: str
) -> None:
    conn = open_db(tmp_path / "a.db", key)
    conn.execute("CREATE TABLE frame_windows (id INTEGER PRIMARY KEY, process TEXT, hwnd INTEGER)")
    column = "process" if present.endswith("process") else "hwnd"
    conn.execute(f"CREATE INDEX {present} ON frame_windows ({column})")
    conn.execute("PRAGMA user_version = 1")
    before = list(conn.execute("SELECT type, name, sql FROM sqlite_master"))
    with pytest.raises(db.StaleSchemaError, match="older Akashic") as raised:
        db.migrate(conn)
    assert "database layout has since changed" in str(raised.value)
    assert list(conn.execute("SELECT type, name, sql FROM sqlite_master")) == before
    assert db.schema_version(conn) == 1


def test_a_fresh_store_indexes_frame_windows_by_hwnd(
    tmp_path: Path, key: bytes, open_db: OpenDb
) -> None:
    conn = open_db(tmp_path / "a.db", key)
    db.migrate(conn)
    row = conn.execute(
        "SELECT type, tbl_name FROM sqlite_master WHERE name = 'frame_windows_hwnd'"
    ).fetchone()
    assert tuple(row) == ("index", "frame_windows")


def test_db_errors_cover_both_sqlite_trees() -> None:
    assert issubclass(sqlite3.OperationalError, db.DB_ERRORS)
    assert issubclass(sqlcipher3.OperationalError, db.DB_ERRORS)
    assert not issubclass(ValueError, db.DB_ERRORS)


def test_a_store_without_frame_windows_is_not_stale(
    tmp_path: Path, key: bytes, open_db: OpenDb
) -> None:
    conn = open_db(tmp_path / "a.db", key)
    assert db.migrate(conn, [Migration(1, "0001_ok.sql", "CREATE TABLE a (x);")]) == 1
    assert db.migrate(open_db(tmp_path / "b.db", key), []) == 0


def test_a_fresh_store_indexes_frame_windows_by_process(
    tmp_path: Path, key: bytes, open_db: OpenDb
) -> None:
    conn = open_db(tmp_path / "a.db", key)
    db.migrate(conn)
    row = conn.execute(
        "SELECT type, tbl_name FROM sqlite_master WHERE name = 'frame_windows_process'"
    ).fetchone()
    assert tuple(row) == ("index", "frame_windows")
    assert [r[2] for r in conn.execute("PRAGMA index_info(frame_windows_process)")] == ["process"]


@pytest.mark.parametrize("kind", ["all", "screen", "screen_text", "mic", "system"])
def test_voice_elsewhere_is_an_accepted_gap_reason_and_bogus_is_not(
    tmp_path: Path, key: bytes, open_db: OpenDb, kind: str
) -> None:
    conn = open_db(tmp_path / "a.db", key)
    db.migrate(conn)
    sql = "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, ?, 1, 2)"
    conn.execute(sql, (kind, "voice_elsewhere"))
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute(sql, (kind, "bogus"))


def test_migration_files_are_sequential() -> None:
    migrations = db.load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))


def test_forgotten_question_ids_are_never_reused(
    tmp_path: Path, key: bytes, open_db: OpenDb
) -> None:
    # ask:<id> stays in the job queue after a question is deleted: a new question must not
    # inherit that id (and with it the old, completed job).
    conn = open_db(tmp_path / "questions.db", key)
    db.migrate(conn)
    conn.execute("INSERT INTO questions (id, asked_ms, text) VALUES (50, 1, 'old question')")
    conn.execute("DELETE FROM questions")
    conn.execute("INSERT INTO questions (asked_ms, text) VALUES (2, 'next question')")
    assert conn.execute("SELECT id FROM questions").fetchone() == (51,)


def test_migration_gap_detected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("")
    (tmp_path / "0003_c.sql").write_text("")
    (tmp_path / "notes.txt").write_text("")
    monkeypatch.setattr(db.resources, "files", lambda _pkg: tmp_path)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match="out of sequence"):
        db.load_migrations()


def test_transaction_commits_and_rolls_back(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    conn = open_db(tmp_path / "a.db", key)
    conn.execute("CREATE TABLE t (x)")
    with db.transaction(conn):
        conn.execute("INSERT INTO t VALUES (1)")

    def failing_write() -> None:
        with db.transaction(conn):
            conn.execute("INSERT INTO t VALUES (2)")
            _ = 1 / 0

    with pytest.raises(ZeroDivisionError):
        failing_write()
    assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]


def test_fts_matches_without_diacritics(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    conn = open_db(tmp_path / "a.db", key)
    db.migrate(conn)
    rows = [
        ("Un café à Paris avec Minlo", "Discord"),
        ("Réunion de l'équipe", "Notes"),
        ("Nothing relevant", "Chrome"),
    ]
    for text, title in rows:
        add_frame(conn, 1, text, title)

    def search(q: str) -> list[str]:
        return [
            r[0]
            for r in conn.execute(
                "SELECT w.text FROM frame_windows_fts JOIN frame_windows w"
                " ON w.id = frame_windows_fts.rowid"
                " WHERE frame_windows_fts MATCH ? ORDER BY w.id",
                (q,),
            )
        ]

    assert search("cafe") == ["Un café à Paris avec Minlo"]
    assert search("reunion equipe") == ["Réunion de l'équipe"]
    assert search("MINLO") == ["Un café à Paris avec Minlo"]
    assert search("discord") == ["Un café à Paris avec Minlo"]

    conn.execute("UPDATE frame_windows SET text = 'changed' WHERE frame_id = 1")
    assert search("cafe") == []
    conn.execute("DELETE FROM frames WHERE id = 2")
    assert search("reunion") == []


def test_concurrent_connections_share_wal(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    path = tmp_path / "a.db"
    writer = open_db(path, key)
    db.migrate(writer)
    reader = open_db(path, key)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM frames").fetchone()
    with db.transaction(writer):  # readers don't block writers in WAL mode
        add_monitor(writer)
        writer.execute("INSERT INTO frames (ts_ms, monitor_id, trigger) VALUES (1, 0, 'idle')")
    assert reader.execute("SELECT count(*) FROM frames").fetchone()[0] == 0  # snapshot
    reader.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM frames").fetchone()[0] == 1


def test_state_values_round_trip(tmp_path: Path, key: bytes, open_db: OpenDb) -> None:
    conn = open_db(tmp_path / "a.db", key)
    db.migrate(conn)
    assert state.get_int(conn, "x", 7) == 7
    state.set_int(conn, "x", 1)
    state.set_int(conn, "x", 2)  # an upsert, not a second row
    assert state.get_int(conn, "x", 7) == 2
    assert conn.execute("SELECT count(*) FROM state").fetchone() == (1,)


# --- frames and their windows ------------------------------------------------------------------


@pytest.fixture
def frame_conn(tmp_path: Path, key: bytes, open_db: OpenDb) -> Connection:
    conn = open_db(tmp_path / "a.db", key)
    db.migrate(conn)
    return conn


def test_a_frame_needs_a_known_monitor(frame_conn: Connection) -> None:
    with pytest.raises(sqlcipher3.IntegrityError):
        frame_conn.execute("INSERT INTO frames (ts_ms, monitor_id, trigger) VALUES (1, 99, 'idle')")


@pytest.mark.parametrize("column", ["process", "title", "url", "text", "text_source"])
@pytest.mark.parametrize("role", ["hidden", "shell"])
def test_a_non_app_row_cannot_carry_content(frame_conn: Connection, role: str, column: str) -> None:
    frame = add_frame(frame_conn, 1, "x")
    value = "uia" if column == "text_source" else "v"
    with pytest.raises(sqlcipher3.IntegrityError):
        frame_conn.execute(
            f"INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1, {column})"  # noqa: S608
            " VALUES (?, ?, 5, ?, 0, 0, 1, 1, ?)",
            (frame, role, 7 if role == "shell" else None, value),
        )


@pytest.mark.parametrize("role", ["hidden", "shell"])
def test_a_non_app_row_cannot_be_focused(frame_conn: Connection, role: str) -> None:
    frame = add_frame(frame_conn, 1, "x")
    with pytest.raises(sqlcipher3.IntegrityError):
        frame_conn.execute(
            "INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1, focused)"
            " VALUES (?, ?, 5, ?, 0, 0, 1, 1, 1)",
            (frame, role, 7 if role == "shell" else None),
        )


def test_an_app_row_needs_an_hwnd_and_a_hidden_row_must_not_have_one(
    frame_conn: Connection,
) -> None:
    frame = add_frame(frame_conn, 1, "x")
    insert = (
        "INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1)"
        " VALUES (?, ?, ?, ?, 0, 0, 1, 1)"
    )
    with pytest.raises(sqlcipher3.IntegrityError):
        frame_conn.execute(insert, (frame, "app", 5, None))
    with pytest.raises(sqlcipher3.IntegrityError):
        frame_conn.execute(insert, (frame, "hidden", 6, 3))
    frame_conn.execute(insert, (frame, "hidden", 6, None))
    frame_conn.execute(insert, (frame, "shell", 7, 3))


def test_only_one_window_per_frame_can_be_focused(frame_conn: Connection) -> None:
    frame = add_frame(frame_conn, 1, "x")  # its window is focused
    with pytest.raises(sqlcipher3.IntegrityError):
        add_window(frame_conn, frame, 1, "y", focused=True)
    add_window(frame_conn, frame, 1, "y")
    other = add_frame(frame_conn, 2, "z")
    assert other != frame  # another frame has its own focused window


def test_a_z_position_is_used_once_per_frame(frame_conn: Connection) -> None:
    frame = add_frame(frame_conn, 1, "x")
    with pytest.raises(sqlcipher3.IntegrityError):
        add_window(frame_conn, frame, 0, "y")


def test_only_known_text_sources_are_stored(frame_conn: Connection) -> None:
    frame = add_frame(frame_conn, 1, "x")
    for source in ("uia", "ocr", "ocr_deferred"):
        add_window(
            frame_conn, frame, {"uia": 1, "ocr": 2, "ocr_deferred": 3}[source], "t", source=source
        )
    with pytest.raises(sqlcipher3.IntegrityError):
        add_window(frame_conn, frame, 4, "t", source="typed")


def test_deleting_a_frame_removes_its_windows_search_entries_and_passages(
    frame_conn: Connection,
) -> None:
    frame = add_frame(frame_conn, 1, "searchable words")
    (window,) = frame_conn.execute("SELECT id FROM frame_windows").fetchone()
    frame_conn.execute("INSERT INTO window_passages VALUES (?, 'm', 0, 5, 1, x'0000')", (window,))
    frame_conn.execute("DELETE FROM frames WHERE id = ?", (frame,))
    for table in ("frame_windows", "window_passages"):
        assert frame_conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)  # noqa: S608
    assert frame_conn.execute(
        "SELECT count(*) FROM frame_windows_fts WHERE frame_windows_fts MATCH 'searchable'"
    ).fetchone() == (0,)


def _driver_error(cls: type[Exception], code: int | None, message: str) -> Exception:
    error = cls(message)
    if code is not None:
        error.sqlite_errorcode = code  # type: ignore[attr-defined]
    return error


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.ENOSPC, "No space left on device"),
        _driver_error(sqlite3.OperationalError, 13, "database or disk is full"),
        _driver_error(sqlcipher3.OperationalError, 13 | (1 << 8), "database or disk is full"),
        _driver_error(sqlcipher3.OperationalError, None, "database or disk is full"),
    ],
)
def test_is_disk_full_recognizes_a_full_disk(error: Exception) -> None:
    assert db.is_disk_full(error)


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EACCES, "denied"),
        _driver_error(sqlite3.OperationalError, 5, "database is locked"),
        _driver_error(sqlcipher3.OperationalError, None, "database is locked"),
        ValueError("disk is full"),
    ],
)
def test_is_disk_full_rejects_other_errors(error: Exception) -> None:
    assert not db.is_disk_full(error)
