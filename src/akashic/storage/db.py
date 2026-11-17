"""SQLCipher connection, WAL, transactions and migrations.

All three processes open the same database. Writes are short ``BEGIN IMMEDIATE`` transactions;
readers never block writers (WAL). ``busy_timeout`` absorbs brief lock contention; the audio
writer and the media tracker retry beyond it.
"""

from __future__ import annotations

import errno
import re
import sqlite3
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Final

import sqlcipher3

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path
    from sqlite3 import Connection

BUSY_TIMEOUT_MS: Final = 5_000
SQLITE_BUSY: Final = 5
SQLITE_LOCKED: Final = 6
SQLITE_FULL: Final = 13
_PRIMARY_CODE_MASK: Final = 0xFF
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class DatabaseKeyError(Exception):
    """The key does not open this database (or the file is not a database)."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


def connect(path: Path, db_key: bytes) -> Connection:
    """Open (or create) the encrypted database with ``db_key`` as the raw SQLCipher key."""
    if len(db_key) != 32:  # noqa: PLR2004 - SQLCipher raw keys are exactly 256 bits
        msg = "database key must be 32 bytes"
        raise ValueError(msg)
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we issue BEGIN/COMMIT ourselves (see transaction()).
    conn = sqlcipher3.connect(
        str(path),
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    try:
        # Raw key form: no PBKDF2 inside SQLCipher, our key is already uniformly random.
        conn.execute(f"PRAGMA key = \"x'{db_key.hex()}'\"")
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlcipher3.DatabaseError as exc:
            raise DatabaseKeyError from exc
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # Temp tables and sort spills stay in memory: never as plaintext temp files on disk.
        conn.execute("PRAGMA temp_store = MEMORY")
    except BaseException:
        conn.close()
        raise
    return conn


def limit_wait(conn: Connection, busy_timeout_ms: int) -> None:
    """Shorten how long a statement on ``conn`` waits for a busy database. Call it only from the
    thread that uses ``conn``."""
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")


@contextmanager
def transaction(conn: Connection) -> Generator[Connection]:
    """A short write transaction. Takes the write lock up front to avoid upgrade deadlocks."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        _rollback_if_open(conn)
        raise
    try:
        conn.execute("COMMIT")
    except BaseException:
        _rollback_if_open(conn)
        raise


DB_ERRORS: Final = (sqlite3.Error, sqlcipher3.Error)


def is_busy(exc: BaseException) -> bool:
    """True for a lock conflict that may clear on its own. An error without a code counts: older
    drivers and test doubles don't set one."""
    if not isinstance(exc, sqlcipher3.OperationalError | sqlite3.OperationalError):
        return False
    code: int | None = getattr(exc, "sqlite_errorcode", None)
    return code is None or code & _PRIMARY_CODE_MASK in (SQLITE_BUSY, SQLITE_LOCKED)


def is_disk_full(exc: BaseException) -> bool:
    """True when a write failed because the disk is full."""
    if isinstance(exc, OSError):
        return exc.errno == errno.ENOSPC
    if not isinstance(exc, sqlcipher3.OperationalError | sqlite3.OperationalError):
        return False
    code: int | None = getattr(exc, "sqlite_errorcode", None)
    if code is None:  # the driver sets no code
        return "disk is full" in str(exc)
    return code & _PRIMARY_CODE_MASK == SQLITE_FULL


def _rollback_if_open(conn: Connection) -> None:
    """Roll back unless SQLite already did; a failing rollback must not mask the real error."""
    if not conn.in_transaction:
        return
    with suppress(sqlcipher3.Error):
        conn.execute("ROLLBACK")


def load_migrations() -> list[Migration]:
    """Numbered ``NNNN_name.sql`` files from the migrations package, in order, gap-free."""
    found: list[Migration] = []
    for entry in resources.files("akashic.storage.migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append(Migration(int(match.group(1)), entry.name, entry.read_text("utf-8")))
    found.sort(key=lambda m: m.version)
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            msg = f"migration {migration.name} out of sequence (expected {expected:04d})"
            raise RuntimeError(msg)
    return found


def schema_version(conn: Connection) -> int:
    row: tuple[int] = conn.execute("PRAGMA user_version").fetchone()
    return row[0]


def migrate(conn: Connection, migrations: list[Migration] | None = None) -> int:
    """Apply pending migrations, each atomically. Returns the resulting schema version."""
    pending = load_migrations() if migrations is None else migrations
    current = schema_version(conn)
    latest = pending[-1].version if pending else 0
    if current > latest:
        msg = f"database schema v{current} is newer than this app (v{latest})"
        raise RuntimeError(msg)
    for migration in pending:
        if migration.version <= current:
            continue
        # executescript() can't run inside an open transaction, so the script carries its own.
        script = (
            f"BEGIN IMMEDIATE;\n{migration.sql}\n"
            f"PRAGMA user_version = {migration.version};\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    _refuse_stale_schema(conn)
    return schema_version(conn)


class StaleSchemaError(RuntimeError):
    """The store was made by an earlier version of the 0001 schema, which is edited in place."""


def _refuse_stale_schema(conn: Connection) -> None:
    frames_columns = {row[1] for row in conn.execute("PRAGMA table_info(frames)")}
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    lacks_monitor_id = bool(frames_columns) and "monitor_id" not in frames_columns
    window_indexes = {"frame_windows_process", "frame_windows_hwnd"}
    lacks_window_indexes = "frame_windows" in names and not window_indexes <= names
    if lacks_monitor_id or lacks_window_indexes:
        msg = (
            "This data folder was created by an older Akashic whose database layout has since"
            " changed. Move the folder aside to start a new one, or set AKASHIC_DATA_DIR to"
            " another folder. Nothing was changed."
        )
        raise StaleSchemaError(msg)
