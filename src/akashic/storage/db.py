"""SQLCipher connection, WAL, transactions and migrations.

All three processes open the same database. Writes are short ``BEGIN IMMEDIATE`` transactions;
readers never block writers (WAL). ``busy_timeout`` absorbs brief lock contention.
"""

from __future__ import annotations

import re
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
    return schema_version(conn)
