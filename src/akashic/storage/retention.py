"""Per-type retention: delete what has outlived its configured number of days.

Each data type has its own age limit (``config.toml`` ``[retention]``; ``None`` means forever, the
default). Age is the *capture* time of the data, never when it was processed. What each type
removes, and what that leaves behind:

- ``screenshots``: the encrypted image file; the frame row (text, window, URL) stays.
- ``frame_text``: the text of a frame, and its full-text index entry; the screenshot stays.
- ``raw_audio``: the encrypted Opus file; the segment row and its transcript stay.
- ``transcripts``: the transcript of a segment, and its index entry; the audio stays.
- ``media_sessions`` / ``app_sessions``: the closed session rows (and, for media, their events).
  Game sessions follow ``app_sessions``.

A row with nothing left to keep is removed too, so nothing is orphaned: a frame goes once both its
screenshot and its text have expired; an audio segment once both its audio and its transcript
have (its speaker turns go with it; speaker profiles are not time-bound and stay).

Safety rules:

- Work that is still queued is never taken away: a screenshot waiting for deferred OCR and audio
  waiting for transcription or speaker identification are kept until the job has run.
- The newest app session is always kept: it carries the watermark that stops the derivation from
  rebuilding sessions out of frames that still exist.
- Files are deleted before their database reference is cleared, so a crash in between is repaired
  by the next pass instead of leaving a file nobody points at.
- Work happens in short batches, so capture is never held up. Every pass is idempotent.

- ``episodes``: the episode rows (their links and full-text entries go with them, and so do their
  vectors); the evidence they point at stays.
- ``embeddings``: vectors of episodes, transcripts and screen text. Housekeeping does not
  make them again for anything older than the limit.

Nothing is ever deleted to free disk space on its own: only these age limits delete anything.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from akashic.storage import db, state
from akashic.storage.media import MediaPathError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from sqlite3 import Connection

    from akashic.storage.media import MediaStore

log = logging.getLogger(__name__)

DAY_MS: Final = 86_400_000
BATCH: Final = 500


type _Rows = list[tuple[int, str]]  # (row id, media path)

BAD_MEDIA_PATHS: Final = "bad_media_paths"
LOCKED_MEDIA_FILES: Final = "locked_media_files"


def _batches(fetch: Callable[[int], _Rows], handle: Callable[[_Rows], int]) -> int:
    """Run ``handle`` on successive batches from ``fetch(after)`` until one comes back short.

    ``fetch`` receives the highest id seen so far, so a row that cannot be processed (a locked
    file) is skipped for this pass instead of being fetched forever."""
    total = 0
    last_id = 0
    while True:
        rows = fetch(last_id)
        if not rows:
            return total
        total += handle(rows)
        last_id = max(row_id for row_id, _ in rows)
        if len(rows) < BATCH:
            return total


def _delete_files(media: MediaStore, rows: _Rows) -> list[int]:
    """Delete the files of ``rows`` (id, path); returns the ids whose file is gone.

    A locked file or a reference outside the media directory is skipped and its reference kept,
    so one bad row never stops the others."""
    gone: list[int] = []
    for row_id, rel_path in rows:
        try:
            media.delete(rel_path)
            media.prune_empty_dirs(rel_path)
        except (OSError, MediaPathError) as exc:
            log.warning("could not delete a media file: %s", type(exc).__name__)
            continue
        gone.append(row_id)
    return gone


def _marks(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


# --- screen ---------------------------------------------------------------------------------


def _expirable_screenshots(conn: Connection, after: int, cutoff_ms: int) -> _Rows:
    return conn.execute(
        "SELECT id, image_path FROM frames f WHERE id > ? AND ts_ms < ?"
        " AND image_path IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM jobs WHERE key = 'ocr_frame:' || f.id"
        "                 AND state IN ('pending', 'running'))"
        " ORDER BY id LIMIT ?",
        (after, cutoff_ms, BATCH),
    ).fetchall()


def _expirable_audio(conn: Connection, after: int, cutoff_ms: int) -> _Rows:
    return conn.execute(
        "SELECT id, audio_path FROM audio_segments s WHERE id > ? AND start_ms < ?"
        " AND audio_path IS NOT NULL AND status != 'pending'"
        " AND NOT EXISTS (SELECT 1 FROM jobs WHERE key = 'speakers:' || s.id"
        "                 AND state IN ('pending', 'running'))"
        " ORDER BY id LIMIT ?",
        (after, cutoff_ms, BATCH),
    ).fetchall()


def expire_screenshots(conn: Connection, media: MediaStore, cutoff_ms: int) -> int:
    def fetch(after: int) -> _Rows:
        return _expirable_screenshots(conn, after, cutoff_ms)

    def handle(rows: _Rows) -> int:
        gone = _delete_files(media, rows)
        if gone:
            with db.transaction(conn):
                conn.execute(
                    f"UPDATE frames SET image_path = NULL WHERE id IN ({_marks(gone)})",  # noqa: S608
                    gone,
                )
        return len(gone)

    return _batches(fetch, handle)


def expire_frame_text(conn: Connection, _media: MediaStore, cutoff_ms: int) -> int:
    total = 0
    while True:
        with db.transaction(conn):
            changed = conn.execute(
                "UPDATE frames SET text = NULL, text_source = NULL WHERE id IN"
                " (SELECT id FROM frames WHERE ts_ms < ? AND text IS NOT NULL LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
        total += changed
        if changed < BATCH:
            return total


def delete_spent_frames(conn: Connection, cutoff_ms: int) -> int:
    """Frames older than ``cutoff_ms`` whose screenshot is gone. Only called when the text has
    expired for them too, so nothing but window metadata is lost with the row."""
    total = 0
    while True:
        with db.transaction(conn):
            changed = conn.execute(
                "DELETE FROM frames WHERE id IN"
                " (SELECT id FROM frames WHERE ts_ms < ? AND image_path IS NULL"
                " AND text IS NULL LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
        total += changed
        if changed < BATCH:
            return total


# --- audio ----------------------------------------------------------------------------------


def expire_raw_audio(conn: Connection, media: MediaStore, cutoff_ms: int) -> int:
    def fetch(after: int) -> _Rows:
        return _expirable_audio(conn, after, cutoff_ms)

    def handle(rows: _Rows) -> int:
        gone = _delete_files(media, rows)
        if gone:
            with db.transaction(conn):
                conn.execute(
                    f"UPDATE audio_segments SET audio_path = NULL WHERE id IN ({_marks(gone)})",  # noqa: S608
                    gone,
                )
        return len(gone)

    return _batches(fetch, handle)


def expire_transcripts(conn: Connection, _media: MediaStore, cutoff_ms: int) -> int:
    total = 0
    while True:
        with db.transaction(conn):
            changed = conn.execute(
                "UPDATE audio_segments SET transcript = NULL WHERE id IN"
                " (SELECT id FROM audio_segments WHERE start_ms < ? AND transcript IS NOT NULL"
                "  LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
        total += changed
        if changed < BATCH:
            return total


def delete_spent_segments(conn: Connection, cutoff_ms: int) -> int:
    """Segments older than ``cutoff_ms`` whose audio is gone and that wait for no job. Only
    called when the transcript has expired for them too. Speaker turns go with the row."""
    total = 0
    while True:
        with db.transaction(conn):
            changed = conn.execute(
                "DELETE FROM audio_segments WHERE id IN"
                " (SELECT id FROM audio_segments WHERE start_ms < ? AND audio_path IS NULL"
                "  AND transcript IS NULL"
                "  AND status != 'pending' LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
        total += changed
        if changed < BATCH:
            return total


# --- sessions -------------------------------------------------------------------------------


def expire_media_sessions(conn: Connection, _media: MediaStore, cutoff_ms: int) -> int:
    total = 0
    while True:
        with db.transaction(conn):
            changed = conn.execute(  # media_events and the full-text entry go with each row
                "DELETE FROM media_sessions WHERE id IN"
                " (SELECT id FROM media_sessions WHERE closed = 1 AND end_ms < ? LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
        total += changed
        if changed < BATCH:
            return total


def expire_app_sessions(conn: Connection, _media: MediaStore, cutoff_ms: int) -> int:
    total = 0
    while True:
        with db.transaction(conn):
            changed = conn.execute(
                "DELETE FROM app_sessions WHERE id IN"
                " (SELECT id FROM app_sessions WHERE closed = 1 AND end_ms < ?"
                "  AND id != (SELECT id FROM app_sessions ORDER BY last_frame_id DESC LIMIT 1)"
                "  LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
            changed += conn.execute(
                "DELETE FROM game_sessions WHERE id IN"
                " (SELECT id FROM game_sessions WHERE closed = 1 AND end_ms < ? LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
        total += changed
        if changed < BATCH:
            return total


# --- search data ----------------------------------------------------------------------------


def expire_episodes(conn: Connection, _media: MediaStore, cutoff_ms: int) -> int:
    total = 0
    while True:
        with db.transaction(conn):
            changed = conn.execute(
                "DELETE FROM episodes WHERE id IN"
                " (SELECT id FROM episodes WHERE end_ms < ? LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
        total += changed
        if changed < BATCH:
            return total


def expire_embeddings(conn: Connection, _media: MediaStore, cutoff_ms: int) -> int:
    total = 0
    while True:
        with db.transaction(conn):
            changed = conn.execute(
                "DELETE FROM embeddings WHERE rowid IN (SELECT rowid FROM embeddings"
                " WHERE owner_kind != 'query' AND start_ms < ? LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
            passage_rows = conn.execute(
                "DELETE FROM segment_passages WHERE rowid IN"
                " (SELECT p.rowid FROM segment_passages p JOIN audio_segments s"
                " ON s.id = p.segment_id WHERE s.start_ms < ? LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
            frame_rows = conn.execute(
                "DELETE FROM frame_passages WHERE rowid IN"
                " (SELECT p.rowid FROM frame_passages p JOIN frames f"
                " ON f.id = p.frame_id WHERE f.ts_ms < ? LIMIT ?)",
                (cutoff_ms, BATCH),
            ).rowcount
        total += changed + passage_rows + frame_rows
        if changed < BATCH and passage_rows < BATCH and frame_rows < BATCH:
            return total


_MEDIA_FETCHERS: Final[dict[str, Callable[[Connection, int, int], _Rows]]] = {
    "screenshots": _expirable_screenshots,
    "raw_audio": _expirable_audio,
}


def unreleased_media_references(
    conn: Connection, media: MediaStore, kind: str, cutoff_ms: int
) -> dict[str, int]:
    """After a ``kind`` pass: how many expirable rows still hold a media reference, split into
    references outside the media directory and files that could not be deleted. Both are
    empty for kinds that delete no files."""
    left = {BAD_MEDIA_PATHS: 0, LOCKED_MEDIA_FILES: 0}
    fetch = _MEDIA_FETCHERS.get(kind)
    last_id = 0
    while fetch is not None:
        rows = fetch(conn, last_id, cutoff_ms)
        for _, rel_path in rows:
            left[LOCKED_MEDIA_FILES if media.is_inside(rel_path) else BAD_MEDIA_PATHS] += 1
        if len(rows) < BATCH:
            break
        last_id = max(row_id for row_id, _ in rows)
    return left


type Purger = Callable[[Connection, MediaStore, int], int]

# Retention types that have data to delete today, in the order they run. The Settings page
# offers exactly these.
PURGERS: Final[dict[str, Purger]] = {
    "screenshots": expire_screenshots,
    "frame_text": expire_frame_text,
    "raw_audio": expire_raw_audio,
    "transcripts": expire_transcripts,
    "media_sessions": expire_media_sessions,
    "app_sessions": expire_app_sessions,
    "episodes": expire_episodes,
    "embeddings": expire_embeddings,
}


def apply_retention(
    conn: Connection, media: MediaStore, retention: Mapping[str, int | None], now_ms: int
) -> dict[str, int]:
    """Delete everything past its limit. Returns how many items each step removed (only steps
    that ran) plus, when a file was skipped, how many references were kept because their path
    is outside the media directory or their file is locked: numbers only, never content."""
    cutoffs = {k: now_ms - d * DAY_MS for k, d in retention.items() if d is not None}
    removed: dict[str, int] = {}
    kept = {BAD_MEDIA_PATHS: 0, LOCKED_MEDIA_FILES: 0}
    for kind, purge in PURGERS.items():
        if kind in cutoffs:
            removed[kind] = purge(conn, media, cutoffs[kind])
            for reason, count in unreleased_media_references(
                conn, media, kind, cutoffs[kind]
            ).items():
                kept[reason] += count
    removed.update({reason: count for reason, count in kept.items() if count})
    if "screenshots" in cutoffs and "frame_text" in cutoffs:
        both = min(cutoffs["screenshots"], cutoffs["frame_text"])
        removed["frame_rows"] = delete_spent_frames(conn, both)
    if "raw_audio" in cutoffs and "transcripts" in cutoffs:
        both = min(cutoffs["raw_audio"], cutoffs["transcripts"])
        removed["audio_rows"] = delete_spent_segments(conn, both)
    return removed


def delete_all_recorded_data(conn: Connection, media: MediaStore) -> dict[str, int]:
    """Explicit full deletion, with capture/worker stopped and API writers excluded by caller.

    Unlike age-based retention, no pending-work, session or timestamp guard applies. Files go
    first, including orphaned ciphertext/temp files. A failure rolls back database changes and
    retains references for an idempotent retry; already removed files cannot be restored.
    The paused choice is a setting, not recorded data, so it is kept.
    """
    with db.transaction(conn):
        removed = {
            "screenshots": int(
                conn.execute("SELECT count(*) FROM frames WHERE image_path IS NOT NULL").fetchone()[
                    0
                ]
            ),
            "raw_audio": int(
                conn.execute(
                    "SELECT count(*) FROM audio_segments WHERE audio_path IS NOT NULL"
                ).fetchone()[0]
            ),
        }
        media.delete_all()
        # Delete derived parents first to avoid rebuilding digests/answers during bulk removal.
        # Their child rows and FTS entries cascade through the existing schema triggers.
        for table in (
            "questions",
            "stt_runs",
            "stt_samples",
            "episodes",
            "frames",
            "audio_segments",
            "media_sessions",
            "app_sessions",
            "game_sessions",
            "speakers",
            "embeddings",
            "jobs",
            "process_info",
        ):
            removed[table] = conn.execute(f"DELETE FROM {table}").rowcount  # noqa: S608 - fixed names
        removed["state"] = conn.execute(
            "DELETE FROM state WHERE key <> ?", (state.CAPTURE_PAUSED,)
        ).rowcount
    return removed
