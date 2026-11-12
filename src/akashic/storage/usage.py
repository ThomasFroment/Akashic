"""How much disk the recordings use, per kind, and how fast it grows.

Everything is kept on the data drive, so this is what lets the user choose retention limits by
looking at real numbers. Sizes come from the files themselves (screenshots and audio, told apart
by folder) and from the length of the stored text. Growth is what was written in the last seven
days. Numbers only: nothing here reads captured content.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.config import Paths

WEEK_MS: Final = 7 * 86_400_000
AUDIO_DIR: Final = "audio"  # folder of raw audio inside media/; everything else is screenshots


@dataclass(frozen=True, slots=True)
class Usage:
    kind: str  # a retention type
    bytes: int
    bytes_7d: int  # written during the last seven days


@dataclass(frozen=True, slots=True)
class Report:
    usage: tuple[Usage, ...]  # screenshots, raw audio (files) and the two texts (in the database)
    database_bytes: int  # the whole database file with its log: holds the texts and the rest
    free_bytes: int | None  # on the data drive; None when it cannot be read

    @property
    def total_bytes(self) -> int:
        """Everything on disk: the media files plus the database (which already holds the texts)."""
        files = sum(u.bytes for u in self.usage if u.kind in {"screenshots", "raw_audio"})
        return files + self.database_bytes


def _walk(folder: Path, since_s: float) -> tuple[int, int]:
    """(total bytes, bytes modified since ``since_s``) of every file under ``folder``."""
    total = recent = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0, 0
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                sub_total, sub_recent = _walk(Path(entry.path), since_s)
                total, recent = total + sub_total, recent + sub_recent
            elif entry.is_file(follow_symlinks=False):
                info = entry.stat(follow_symlinks=False)
                total += info.st_size
                recent += info.st_size if info.st_mtime >= since_s else 0
        except OSError:
            continue
    return total, recent


def media_usage(media_dir: Path, now_ms: int) -> tuple[Usage, Usage]:
    """Screenshots and raw audio, from the files under ``media/``."""
    since_s = (now_ms - WEEK_MS) / 1000
    audio = _walk(media_dir / AUDIO_DIR, since_s)
    shots_total = shots_recent = 0
    try:
        top = list(os.scandir(media_dir))
    except OSError:
        top = []
    for entry in top:
        if entry.name != AUDIO_DIR and entry.is_dir(follow_symlinks=False):
            total, recent = _walk(Path(entry.path), since_s)
            shots_total, shots_recent = shots_total + total, shots_recent + recent
    return Usage("screenshots", shots_total, shots_recent), Usage("raw_audio", *audio)


def _text_usage(conn: Connection, sql: str, kind: str, now_ms: int) -> Usage:
    row: tuple[int, int] = conn.execute(sql, (now_ms - WEEK_MS,)).fetchone()
    return Usage(kind, row[0], row[1])


def database_bytes(db_file: Path) -> int:
    """The database plus its write-ahead log."""
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += db_file.with_name(db_file.name + suffix).stat().st_size
        except OSError:
            continue
    return total


def storage_report(conn: Connection, paths: Paths, now_ms: int) -> Report:
    shots, audio = media_usage(paths.media_dir, now_ms)
    text = _text_usage(
        conn,
        "SELECT coalesce(sum(length(CAST(text AS BLOB))), 0),"
        " coalesce(sum(CASE WHEN ts_ms >= ? THEN length(CAST(text AS BLOB)) END), 0)"
        " FROM frames WHERE text IS NOT NULL",
        "frame_text",
        now_ms,
    )
    spoken = _text_usage(
        conn,
        "SELECT coalesce(sum(length(CAST(transcript AS BLOB))), 0),"
        " coalesce(sum(CASE WHEN start_ms >= ? THEN length(CAST(transcript AS BLOB)) END), 0)"
        " FROM audio_segments WHERE transcript IS NOT NULL",
        "transcripts",
        now_ms,
    )
    vectors = _text_usage(
        conn,
        "SELECT coalesce(sum(length(vec)), 0),"
        " coalesce(sum(CASE WHEN start_ms >= ? THEN length(vec) END), 0)"
        " FROM (SELECT vec, start_ms FROM embeddings WHERE owner_kind != 'query'"
        " UNION ALL SELECT p.vec, s.start_ms FROM segment_passages p"
        " JOIN audio_segments s ON s.id = p.segment_id"
        " UNION ALL SELECT p.vec, f.ts_ms FROM frame_passages p"
        " JOIN frames f ON f.id = p.frame_id)",
        "embeddings",
        now_ms,
    )
    episodes = _text_usage(
        conn,
        "SELECT coalesce(sum(size), 0), coalesce(sum(CASE WHEN start_ms >= ? THEN size END), 0)"
        " FROM (SELECT start_ms, length(CAST(title || body AS BLOB))"
        "       AS size FROM episodes)",
        "episodes",
        now_ms,
    )
    try:
        free: int | None = shutil.disk_usage(paths.root).free
    except OSError:
        free = None
    return Report(
        (shots, audio, text, spoken, vectors, episodes), database_bytes(paths.db_file), free
    )
