"""Audio segments: Opus files (encrypted) plus one row each, and the transcription job.

Raw audio is stored as mono Opus in an Ogg container, then encrypted like every other media
file. Opus decodes at 48 kHz whatever it was encoded at, so decoding resamples back to the
16 kHz mono float32 that Whisper and the voice detector use.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from akashic.jsonish import as_list
from akashic.pipeline.jobs import NewJob, enqueue_in
from akashic.storage import db
from akashic.storage.opus import decode_opus

if TYPE_CHECKING:
    from sqlite3 import Connection

    import numpy as np
    from numpy.typing import NDArray

    from akashic.storage.media import MediaStore

log = logging.getLogger(__name__)

TRANSCRIBE: Final = "transcribe"
SPEAKERS: Final = "speakers"  # queued once a transcript exists: who is talking

type Stream = Literal["mic", "system"]
type Status = Literal["pending", "done", "empty", "failed"]


@dataclass(frozen=True, slots=True)
class NewSegment:
    stream: Stream
    start_ms: int  # UTC milliseconds when the first sample was heard
    end_ms: int
    speech_ms: int
    sources: tuple[str, ...] = ()
    continues_id: int | None = None


@dataclass(frozen=True, slots=True)
class StoredSegment:
    id: int
    stream: str
    start_ms: int
    end_ms: int
    speech_ms: int
    sources: tuple[str, ...]
    audio_path: str | None
    continues_id: int | None
    status: str
    language: str | None
    transcript: str | None


_COLUMNS = (
    "id, stream, start_ms, end_ms, speech_ms, sources, audio_path, continues_id, status,"
    " language, transcript"
)


def audio_rel_path(start_ms: int, stream: str) -> str:
    """``audio/YYYY/MM/DD/<start_ms>_<stream>.opus``: folder by local date, name by the UTC
    instant (see ``frames.image_rel_path``)."""
    local = datetime.fromtimestamp(start_ms / 1000, UTC).astimezone()
    return f"audio/{local:%Y/%m/%d}/{start_ms}_{stream}.opus"


def _row(row: tuple[object, ...]) -> StoredSegment:
    (seg_id, stream, start, end, speech, sources, path, cont, status, lang, text) = row
    names = as_list(json.loads(str(sources))) or []
    return StoredSegment(
        id=int(str(seg_id)),
        stream=str(stream),
        start_ms=int(str(start)),
        end_ms=int(str(end)),
        speech_ms=int(str(speech)),
        sources=tuple(str(n) for n in names),
        audio_path=None if path is None else str(path),
        continues_id=None if cont is None else int(str(cont)),
        status=str(status),
        language=None if lang is None else str(lang),
        transcript=None if text is None else str(text),
    )


class AudioStore:
    def __init__(self, conn: Connection, media: MediaStore) -> None:
        self._conn = conn
        self._media = media

    def add(self, segment: NewSegment, opus: bytes, now_ms: int) -> int:
        """Write the audio file, then the row and its transcription job in one transaction.
        If the database write fails the file is removed again."""
        rel_path = self._media.write_unique(audio_rel_path(segment.start_ms, segment.stream), opus)
        try:
            with db.transaction(self._conn) as conn:
                cursor = conn.execute(
                    "INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources,"
                    " audio_path, continues_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        segment.stream,
                        segment.start_ms,
                        segment.end_ms,
                        segment.speech_ms,
                        json.dumps(list(segment.sources)),
                        rel_path,
                        segment.continues_id,
                    ),
                )
                segment_id = cursor.lastrowid
                if segment_id is None:  # pragma: no cover - INSERT always sets it
                    msg = "insert returned no row id"
                    raise RuntimeError(msg)  # noqa: TRY301 - inside the cleanup scope on purpose
                enqueue_in(
                    conn,
                    NewJob(
                        TRANSCRIBE,
                        f"{TRANSCRIBE}:{segment_id}",
                        segment.start_ms,
                        {"segment_id": segment_id},
                    ),
                    now_ms,
                )
        except BaseException:
            self._media.delete(rel_path)
            raise
        return segment_id

    def get(self, segment_id: int) -> StoredSegment | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM audio_segments WHERE id = ?",  # noqa: S608
            (segment_id,),
        ).fetchone()
        return None if row is None else _row(row)

    def set_transcript(  # noqa: PLR0913 - one transcript outcome and its model provenance
        self,
        segment_id: int,
        status: Status,
        language: str | None,
        transcript: str | None,
        confidence: float | None = None,
        *,
        model: str | None = None,
    ) -> None:
        """Store the outcome of transcription. A segment with a transcript also gets its
        ``speakers`` job, in the same transaction."""
        with db.transaction(self._conn) as conn:
            row: tuple[int] | None = conn.execute(
                "SELECT start_ms FROM audio_segments WHERE id = ?", (segment_id,)
            ).fetchone()
            conn.execute(
                "UPDATE audio_segments SET status = ?, language = ?, transcript = ?,"
                " confidence = ?,"
                " transcription_model = ?"
                " WHERE id = ?",
                (status, language, transcript, confidence, model, segment_id),
            )
            if status == "done" and row is not None:
                job = NewJob(
                    SPEAKERS, f"{SPEAKERS}:{segment_id}", row[0], {"segment_id": segment_id}
                )
                enqueue_in(conn, job, time.time_ns() // 1_000_000)

    def requeue(self, segment_id: int, now_ms: int) -> bool:
        """Forget the transcript and queue the segment for Whisper again. False if unknown."""
        with db.transaction(self._conn) as conn:
            row: tuple[int] | None = conn.execute(
                "SELECT start_ms FROM audio_segments WHERE id = ?", (segment_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                "UPDATE audio_segments SET status = 'pending', language = NULL, transcript = NULL,"
                " confidence = NULL, transcription_model = NULL WHERE id = ?",
                (segment_id,),
            )
            key = f"{TRANSCRIBE}:{segment_id}"
            conn.execute("DELETE FROM jobs WHERE key = ?", (key,))
            enqueue_in(conn, NewJob(TRANSCRIBE, key, row[0], {"segment_id": segment_id}), now_ms)
        return True

    def previous_transcript(self, stream: str, before_ms: int, within_ms: int) -> str | None:
        """What was said last on ``stream`` in the ``within_ms`` before ``before_ms``, if
        anything (the context Whisper gets for the next segment)."""
        row = self._conn.execute(
            "SELECT transcript FROM audio_segments WHERE stream = ? AND status = 'done'"
            " AND end_ms <= ? AND end_ms >= ? AND start_ms < ?"
            " ORDER BY end_ms DESC LIMIT 1",
            (stream, before_ms, before_ms - within_ms, before_ms),
        ).fetchone()
        return None if row is None or not row[0] else str(row[0])

    def read_samples(self, segment: StoredSegment) -> NDArray[np.float32] | None:
        """The decrypted, decoded audio, or None when the raw audio was deleted."""
        if segment.audio_path is None or not self._media.exists(segment.audio_path):
            return None
        return decode_opus(self._media.read(segment.audio_path))

    def read_file(self, segment: StoredSegment) -> bytes | None:
        """The decrypted Ogg/Opus file, for playback."""
        if segment.audio_path is None or not self._media.exists(segment.audio_path):
            return None
        return self._media.read(segment.audio_path)
