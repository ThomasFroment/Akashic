from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

import pytest
import sqlcipher3

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
    conn.execute(
        "INSERT INTO frames (ts_ms, monitor, trigger, text) VALUES (1, 0, 'idle', ?)",
        ("SECRETMARKER visible text",),
    )
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
    assert {"frames", "frames_fts"} <= tables


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
        conn.execute(
            "INSERT INTO frames (ts_ms, monitor, trigger, text, window_title)"
            " VALUES (1, 0, 'click', ?, ?)",
            (text, title),
        )

    def search(q: str) -> list[str]:
        return [
            r[0]
            for r in conn.execute(
                "SELECT f.text FROM frames_fts JOIN frames f ON f.id = frames_fts.rowid"
                " WHERE frames_fts MATCH ? ORDER BY f.id",
                (q,),
            )
        ]

    assert search("cafe") == ["Un café à Paris avec Minlo"]
    assert search("reunion equipe") == ["Réunion de l'équipe"]
    assert search("MINLO") == ["Un café à Paris avec Minlo"]
    assert search("discord") == ["Un café à Paris avec Minlo"]

    conn.execute("UPDATE frames SET text = 'changed' WHERE id = 1")
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
        writer.execute("INSERT INTO frames (ts_ms, monitor, trigger) VALUES (1, 0, 'idle')")
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
