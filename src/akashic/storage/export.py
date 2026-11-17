"""Encrypted export: a copy of the store, under its own passphrase and keys, for a range and some
categories.

The export is a data folder (``db/keyfile``, ``db/akashic.db``, ``media/...``) built in
``<target>.partial`` and renamed into place when whole. Steps: a consistent snapshot of the live
database copied into a database under new keys; the scope filter, which deletes what is out of
scope (the existing triggers keep derived rows current); the media files, re-encrypted under the
new key; a rebuild of every full-text index and a VACUUM, so nothing deleted survives in a page
or an index. The live store is only read, through its own connection: never the shared one.
"""

from __future__ import annotations

import shutil
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from time import sleep, time
from typing import TYPE_CHECKING, Final

import numpy as np

from akashic.storage import db, retention, state
from akashic.storage.crypto import (
    MIN_PASSPHRASE_CHARS,
    DataKey,
    wrap_data_key,
    write_keyfile_atomic,
)
from akashic.storage.gaps import GapLog
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path
    from sqlite3 import Connection

    from numpy.typing import NDArray

_BEFORE_ALL: Final = -(2**62)
_AFTER_ALL: Final = 2**62
_DATABASE_PHASES: Final = 4  # snapshot, scope, full-text indexes, VACUUM
_MEDIA_REFERENCES: Final = (
    ("frames", "image_path"),
    ("frame_windows", "capture_path"),
    ("audio_segments", "audio_path"),
)
_SESSION_ITEMS: Final = (
    ("media_session", "media_sessions"),
    ("app_session", "app_sessions"),
    ("game_session", "game_sessions"),
)
# Kinds whose body is the digest of the evidence in the range; the others are cleared at an edge.
_EVIDENCE_BODY_KINDS: Final = "('conversation', 'video', 'music')"
_EDGE_KINDS: Final = "('browsing', 'work', 'game')"
_OUTSIDE_RANGE: Final = "end_ms <= ? OR start_ms >= ?"
_PLAIN_FTS: Final = ("media_sessions_fts", "audio_segments_fts", "episodes_fts")
_RENAME_TRIES: Final = 10
_RENAME_RETRY_SECONDS: Final = 0.5


class ExportError(Exception):
    """The request cannot start. The message is a fixed sentence, safe to show: it never
    contains the passphrase, a path or a date."""


class ExportCancelled(Exception):  # noqa: N818 - a state, not a failure
    """The user cancelled the export."""


@dataclass(frozen=True, slots=True)
class ExportScope:
    """What to export. A range is [start_ms, end_ms), a missing bound is open; empty
    ``categories`` means all of them."""

    start_ms: int | None = None
    end_ms: int | None = None
    categories: frozenset[str] = frozenset()

    @property
    def excluded(self) -> frozenset[str]:
        return CATEGORIES - self.categories if self.categories else frozenset()

    @property
    def is_everything(self) -> bool:
        return self.start_ms is None and self.end_ms is None and not self.excluded


@dataclass(frozen=True, slots=True)
class ExportResult:
    files: int
    bytes: int
    missing_files: int


class ExportCancel:
    """Cancel flag that also interrupts the SQLite statement running on the attached connection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._conn: Connection | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._conn is not None:
                self._conn.interrupt()

    def attach(self, conn: Connection | None) -> None:
        with self._lock:
            if conn is not None:
                self.check()
            self._conn = conn

    def check(self) -> None:
        if self._cancelled:
            raise ExportCancelled


def partial_path(target: Path) -> Path:
    return target.with_name(target.name + ".partial")


def validate(target: Path, passphrase: str, scope: ExportScope, data_dir: Path) -> None:
    """Raise ``ExportError`` when this export cannot be made. Reads the file system only."""
    if not target.is_absolute() or not target.name:
        msg = "The export folder must be a full path to a new folder."
        raise ExportError(msg)
    if not target.parent.is_dir():
        msg = "The folder that should hold the export does not exist."
        raise ExportError(msg)
    try:
        occupied = target.exists() and (not target.is_dir() or any(target.iterdir()))
    except OSError:
        msg = "The export folder cannot be read."
        raise ExportError(msg) from None
    if occupied:
        msg = "The export folder must not exist yet, or be empty."
        raise ExportError(msg)
    live = data_dir.resolve()
    resolved = target.resolve()
    if resolved == live or live in resolved.parents:
        msg = "The export cannot go into the Akashic data folder."
        raise ExportError(msg)
    if partial_path(target).exists():
        msg = "An unfinished export is in the way. Remove its .partial folder first."
        raise ExportError(msg)
    if len(passphrase) < MIN_PASSPHRASE_CHARS:
        msg = f"The passphrase must have at least {MIN_PASSPHRASE_CHARS} characters."
        raise ExportError(msg)
    if scope.start_ms is not None and scope.end_ms is not None and scope.start_ms >= scope.end_ms:
        msg = "The start of the range must be before its end."
        raise ExportError(msg)
    if not scope.categories <= CATEGORIES:
        msg = "Unknown category."
        raise ExportError(msg)


@contextmanager
def _registered(conn: Connection, cancel: ExportCancel) -> Generator[Connection]:
    """Close ``conn`` on exit; until then ``cancel`` can interrupt its statements."""
    try:
        cancel.attach(conn)
        yield conn
    finally:
        cancel.attach(None)
        conn.close()


class _Progress:
    def __init__(self, report: Callable[[int, int], None]) -> None:
        self._report = report
        self.done = 0
        self.total = _DATABASE_PHASES

    def advance(self, steps: int = 1) -> None:
        self.done += steps
        self._report(self.done, self.total)


def write_export(  # noqa: PLR0913 - one call site; each argument is a distinct input
    *,
    open_db: Callable[[], Connection],
    media: MediaStore,
    target: Path,
    passphrase: str,
    scope: ExportScope,
    cancel: ExportCancel,
    report: Callable[[int, int], None],
) -> ExportResult:
    """Build the export in ``<target>.partial`` and move it to ``target``. On any failure,
    including cancel, the partial folder is removed and the exception propagates."""
    partial = partial_path(target)
    partial.mkdir()  # an existing one is somebody else's: never removed
    new_key = DataKey.generate()
    try:
        progress = _Progress(report)
        progress.advance(0)
        files, missing = _write_partial(
            open_db,
            media,
            partial,
            new_key,
            passphrase,
            scope=scope,
            cancel=cancel,
            progress=progress,
        )
        cancel.check()
        target_existed = target.is_dir()
        if target_existed:
            target.rmdir()  # validated empty
        try:
            _rename_when_released(partial, target, cancel)
        except BaseException:  # restore the folder on cancel as well as OSError, then re-raise
            if target_existed:
                # The folder may be delete-pending or recreated; the original error matters more.
                with suppress(OSError):
                    target.mkdir()
            raise
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        if cancel.cancelled:
            raise ExportCancelled from None
        raise
    finally:
        new_key.wipe()
    return ExportResult(files, _folder_bytes(target), missing)


def _rename_when_released(partial: Path, target: Path, cancel: ExportCancel) -> None:
    # Windows refuses a directory rename while a scanner holds a file below it.
    for _ in range(_RENAME_TRIES - 1):
        try:
            partial.rename(target)
        except PermissionError:
            sleep(_RENAME_RETRY_SECONDS)
            cancel.check()
        else:
            return
    partial.rename(target)


def _write_partial(  # noqa: PLR0913 - the steps share these inputs
    open_db: Callable[[], Connection],
    live_media: MediaStore,
    partial: Path,
    new_key: DataKey,
    passphrase: str,
    *,
    scope: ExportScope,
    cancel: ExportCancel,
    progress: _Progress,
) -> tuple[int, int]:
    """Returns (media files written, media files missing)."""
    new_db_key = new_key.subkey("db")
    write_keyfile_atomic(partial / "db" / "keyfile", wrap_data_key(new_key, passphrase))
    cancel.check()
    snapshot_ms = _snapshot(open_db, partial / "db" / "akashic.db", new_db_key, cancel)
    progress.advance()
    with _registered(db.connect(partial / "db" / "akashic.db", new_db_key), cancel) as copy:
        GapLog(copy).stop(snapshot_ms)
        with db.transaction(copy):
            _apply_scope(copy, scope, snapshot_ms)
        progress.advance()
        cancel.check()
        new_media = MediaStore(partial / "media", new_key.subkey("media"))
        files, missing = _copy_media(copy, live_media, new_media, cancel, progress)
        _rebuild_indexes(copy)
        progress.advance()
        cancel.check()
        copy.execute("VACUUM")
        progress.advance()
        copy.execute("PRAGMA journal_mode = DELETE")  # no -wal or -shm left behind
    return files, missing


def _snapshot(
    open_db: Callable[[], Connection], copy_path: Path, new_db_key: bytes, cancel: ExportCancel
) -> int:
    """Copy the whole live database, as of one instant, into ``copy_path`` under the new key.
    Returns that instant (ms). Foreign keys are off because tables are copied in schema order."""
    with _registered(open_db(), cancel) as src:
        src.execute("PRAGMA foreign_keys = OFF")
        src.execute(f"ATTACH DATABASE ? AS export KEY \"x'{new_db_key.hex()}'\"", (str(copy_path),))
        src.execute("BEGIN")
        version = db.schema_version(src)  # the first read fixes the snapshot
        snapshot_ms = int(time() * 1000)
        src.execute("SELECT sqlcipher_export('export')").fetchone()
        src.execute(f"PRAGMA export.user_version = {version}")
        src.execute("COMMIT")
        src.execute("DETACH DATABASE export")
    return snapshot_ms


def _folder_bytes(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())


def _apply_scope(conn: Connection, scope: ExportScope, snapshot_ms: int) -> None:
    _drop_private_state(conn, snapshot_ms)
    if scope.is_everything:
        return
    low = _BEFORE_ALL if scope.start_ms is None else scope.start_ms
    high = _AFTER_ALL if scope.end_ms is None else scope.end_ms
    ranged = scope.start_ms is not None or scope.end_ms is not None
    # Out-of-range rows go first, so the category drops only touch what stays.
    if ranged:
        conn.execute(f"DELETE FROM episodes WHERE {_OUTSIDE_RANGE}", (low, high))  # noqa: S608
        _drop_rows_out_of_range(conn, low, high)
    _drop_excluded_categories(conn, scope.excluded)
    _settle_episodes(conn, low, high)
    _drop_unreferenced_speakers(conn)
    _rebuild_speaker_profiles(conn)
    if ranged:
        _clip_history(conn, low, high)


def _drop_private_state(conn: Connection, snapshot_ms: int) -> None:
    """What no export carries: questions, speech-comparison data, process info, query vectors,
    finished or failed jobs, and every state value but the heartbeat."""
    for table in ("questions", "stt_runs", "stt_samples", "process_info", "state"):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
    conn.execute("DELETE FROM embeddings WHERE owner_kind = 'query'")
    conn.execute(
        "DELETE FROM jobs WHERE state IN ('done', 'failed')"
        " OR kind IN ('ask', 'embed_query', 'stt_compare')"
    )
    conn.execute("UPDATE jobs SET state = 'pending' WHERE state = 'running'")
    conn.execute("INSERT INTO state (key, value) VALUES (?, ?)", (state.CAPTURE_ALIVE, snapshot_ms))


def _drop_rows_out_of_range(conn: Connection, low: int, high: int) -> None:
    conn.execute("DELETE FROM frames WHERE ts_ms < ? OR ts_ms >= ?", (low, high))
    for table in ("audio_segments", "media_sessions", "app_sessions", "game_sessions"):
        conn.execute(
            f"DELETE FROM {table} WHERE {_OUTSIDE_RANGE}",  # noqa: S608 - fixed names
            (low, high),
        )
    conn.execute("DELETE FROM media_events WHERE ts_ms < ? OR ts_ms >= ?", (low, high))
    conn.execute("DELETE FROM jobs WHERE data_ms < ? OR data_ms >= ?", (low, high))


def _drop_excluded_categories(conn: Connection, excluded: frozenset[str]) -> None:
    """The effect of each retention purger on every row, with none of its guards."""
    drops = (_CATEGORY_DROPS[category] for category in _CATEGORY_DROPS if category in excluded)
    for drop in dict.fromkeys(drops):
        drop(conn, excluded)


def _drop_excluded_episodes(conn: Connection, _excluded: frozenset[str]) -> None:
    conn.execute("DELETE FROM episodes")


def _drop_excluded_media_sessions(conn: Connection, _excluded: frozenset[str]) -> None:
    conn.execute("DELETE FROM media_sessions")


def _drop_excluded_app_sessions(conn: Connection, _excluded: frozenset[str]) -> None:
    conn.execute("DELETE FROM app_sessions")
    conn.execute("DELETE FROM game_sessions")


def _drop_excluded_embeddings(conn: Connection, _excluded: frozenset[str]) -> None:
    for table in ("embeddings", "segment_passages", "window_passages"):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names


def _drop_excluded_screen(conn: Connection, excluded: frozenset[str]) -> None:
    if {"screenshots", "frame_text"} <= excluded:
        conn.execute("DELETE FROM frames")
        return
    if "screenshots" in excluded:
        conn.execute("UPDATE frames SET image_path = NULL WHERE image_path IS NOT NULL")
        conn.execute("UPDATE frame_windows SET capture_path = NULL WHERE capture_path IS NOT NULL")
    if "frame_text" in excluded:
        conn.execute(
            "UPDATE frame_windows SET text = NULL, text_source = NULL"
            " WHERE text IS NOT NULL OR text_source IS NOT NULL"
        )


def _drop_excluded_audio(conn: Connection, excluded: frozenset[str]) -> None:
    if {"raw_audio", "transcripts"} <= excluded:
        conn.execute("DELETE FROM audio_segments")
        return
    if "raw_audio" in excluded:
        conn.execute("UPDATE audio_segments SET audio_path = NULL WHERE audio_path IS NOT NULL")
    if "transcripts" in excluded:
        conn.execute("UPDATE audio_segments SET transcript = NULL WHERE transcript IS NOT NULL")


type _CategoryDrop = Callable[[Connection, frozenset[str]], None]

# Episodes first. A drop may read the other excluded categories (screen and audio pairs).
_CATEGORY_DROPS: Final[dict[str, _CategoryDrop]] = {
    "episodes": _drop_excluded_episodes,
    "screenshots": _drop_excluded_screen,
    "frame_text": _drop_excluded_screen,
    "raw_audio": _drop_excluded_audio,
    "transcripts": _drop_excluded_audio,
    "media_sessions": _drop_excluded_media_sessions,
    "app_sessions": _drop_excluded_app_sessions,
    "embeddings": _drop_excluded_embeddings,
}
assert _CATEGORY_DROPS.keys() == retention.PURGERS.keys(), "a purger has no export drop"  # noqa: S101
CATEGORIES: Final = frozenset(_CATEGORY_DROPS)


def _settle_episodes(conn: Connection, low: int, high: int) -> None:
    """Make the remaining episodes say only what the remaining evidence supports."""
    for item_kind, table in _SESSION_ITEMS:
        conn.execute(
            f"DELETE FROM episode_items WHERE item_kind = ?"  # noqa: S608 - fixed names
            f" AND item_id NOT IN (SELECT id FROM {table})",
            (item_kind,),
        )
    conn.execute(
        "DELETE FROM episodes WHERE NOT EXISTS"
        " (SELECT 1 FROM episode_items i WHERE i.episode_id = episodes.id)"
    )
    retained = "(SELECT r.body FROM retained_episode_bodies r WHERE r.id = episodes.id)"
    # The trigger on a body change drops the episode's vector: unchanged bodies keep theirs.
    conn.execute(
        f"UPDATE episodes SET body = {retained}"  # noqa: S608 - fixed text
        f" WHERE kind IN {_EVIDENCE_BODY_KINDS} AND body != {retained}"
    )
    at_edge = f"kind IN {_EDGE_KINDS} AND (start_ms < ? OR end_ms > ?)"
    conn.execute(
        f"DELETE FROM episodes WHERE {at_edge} AND NOT EXISTS"  # noqa: S608 - fixed text
        " (SELECT 1 FROM frame_windows w JOIN frames f ON f.id = w.frame_id"
        "  WHERE w.role = 'app' AND w.title = episodes.title"
        "  AND f.ts_ms BETWEEN episodes.start_ms AND episodes.end_ms)",
        (low, high),
    )
    conn.execute(
        f"UPDATE episodes SET body = '' WHERE {at_edge} AND body != ''",  # noqa: S608
        (low, high),
    )
    conn.execute(
        f"UPDATE episodes SET url = NULL WHERE {at_edge} AND url IS NOT NULL",  # noqa: S608
        (low, high),
    )


def _drop_unreferenced_speakers(conn: Connection) -> None:
    """Speakers nothing refers to (the user is always kept). Vectors need no pass: the schema's
    triggers drop an owner's vectors whenever the owner is deleted or its text changes."""
    conn.execute(
        "DELETE FROM speakers WHERE is_self = 0"
        " AND id NOT IN (SELECT speaker_id FROM speaker_turns WHERE speaker_id IS NOT NULL)"
        " AND id NOT IN (SELECT speaker_id FROM audio_segments WHERE speaker_id IS NOT NULL)"
        " AND id NOT IN (SELECT speaker_id FROM episode_people)"
    )


def _rebuild_speaker_profiles(conn: Connection) -> None:
    """Each profile is the float32 sum of the embeddings of its remaining in-profile turns."""
    sums: dict[int, tuple[NDArray[np.float32], int]] = {}
    for speaker_id, blob in conn.execute(
        "SELECT speaker_id, embedding FROM speaker_turns"
        " WHERE in_profile = 1 AND speaker_id IS NOT NULL AND embedding IS NOT NULL ORDER BY id"
    ):
        vec = np.frombuffer(bytes(blob), dtype=np.float32)
        total, count = sums.get(speaker_id, (np.zeros_like(vec), 0))
        sums[speaker_id] = (total + vec, count + 1)
    conn.execute("UPDATE speakers SET vec_sum = NULL, vec_n = 0")
    conn.executemany(
        "UPDATE speakers SET vec_sum = ?, vec_n = ? WHERE id = ?",
        [(total.tobytes(), count, speaker_id) for speaker_id, (total, count) in sums.items()],
    )


def _clip_history(conn: Connection, low: int, high: int) -> None:
    """Gap and monitor-state rows shrink to the range. The open ``stopped`` gap stays and moves
    its start to the end of the range when the snapshot is later."""
    conn.execute(
        f"DELETE FROM capture_gaps WHERE end_ms IS NOT NULL AND ({_OUTSIDE_RANGE})",  # noqa: S608
        (low, high),
    )
    conn.execute(
        "UPDATE capture_gaps SET start_ms = MAX(start_ms, ?), end_ms = MIN(end_ms, ?)"
        " WHERE end_ms IS NOT NULL",
        (low, high),
    )
    conn.execute(
        "UPDATE capture_gaps SET start_ms = MIN(start_ms, ?) WHERE end_ms IS NULL", (high,)
    )
    conn.execute("DELETE FROM monitor_states WHERE start_ms >= ? OR checked_ms <= ?", (high, low))
    conn.execute(
        "UPDATE monitor_states SET start_ms = MAX(start_ms, ?), checked_ms = MIN(checked_ms, ?)",
        (low, high),
    )


def _copy_media(
    conn: Connection,
    live: MediaStore,
    new: MediaStore,
    cancel: ExportCancel,
    progress: _Progress,
) -> tuple[int, int]:
    """Re-encrypt every file the copy still references. A file gone from the live store since
    the snapshot loses its reference. Returns (files written, files missing)."""
    referenced = "SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
    progress.total += sum(
        conn.execute(
            f"SELECT count(*) FROM {table} WHERE {column} IS NOT NULL"  # noqa: S608 - fixed names
        ).fetchone()[0]
        for table, column in _MEDIA_REFERENCES
    )
    progress.advance(0)
    updates: list[tuple[str, str, int, str | None]] = []
    files = 0
    for table, column in _MEDIA_REFERENCES:
        for row_id, path in conn.execute(referenced.format(table=table, column=column)):
            cancel.check()
            try:
                data = live.read(path)
            except FileNotFoundError:
                updates.append((table, column, row_id, None))
            else:
                written = new.write_unique(path, data)
                files += 1
                if written != path:
                    updates.append((table, column, row_id, written))
            progress.advance()
    with db.transaction(conn):
        for table, column, row_id, value in updates:
            conn.execute(
                f"UPDATE {table} SET {column} = ? WHERE id = ?",  # noqa: S608 - fixed names
                (value, row_id),
            )
    return files, sum(1 for *_, value in updates if value is None)


def _rebuild_indexes(conn: Connection) -> None:
    """Make each full-text index hold exactly what its triggers would index for the rows left."""
    with db.transaction(conn):
        for index in _PLAIN_FTS:
            conn.execute(f"INSERT INTO {index} ({index}) VALUES ('rebuild')")  # noqa: S608
        # Same indexing rule as the frame_windows triggers in 0001_schema.sql.
        conn.execute("INSERT INTO frame_windows_fts (frame_windows_fts) VALUES ('delete-all')")
        conn.execute(
            "INSERT INTO frame_windows_fts (rowid, text, title, url)"
            " SELECT id, text, title, url FROM frame_windows WHERE role = 'app'"
        )
