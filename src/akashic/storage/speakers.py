"""Speaker profiles and the turns they spoke: the identity rules live here.

A profile is the sum of the unit-length voice embeddings that built it (and how many there are);
the centroid is their normalised mean. Keeping the sum makes it exact to take an embedding out
again when the user says a turn was attributed to the wrong person.

Gates, from screenpipe's ``identify_gate.rs`` (see docs/SPEECH.md), on the seconds of
real speech in a turn:

- under 1.0 s: nobody is identified (a "yeah" matches no one and used to create junk speakers);
- 1.0 to 2.0 s: matched against known voices, but never changes a profile or creates one;
- over 2.0 s: matched, and either improves that profile or starts a new, unnamed speaker.

Everything on the microphone stream is the user. All writes for one segment are one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

from akashic.config import Speakers
from akashic.storage import db
from akashic.storage.embeddings import unit

if TYPE_CHECKING:
    from sqlite3 import Connection

    from numpy.typing import NDArray

    type Vector = NDArray[np.float32]

MIN_IDENTIFY_MS: Final = 1_000
MIN_UPDATE_MS: Final = 2_000
SELF_NAME: Final = "You"
# cosine similarity to call two voices the same
DEFAULT_THRESHOLD: Final = Speakers().match_threshold


@dataclass(frozen=True, slots=True)
class NewTurn:
    start_ms: int  # offsets from the start of the segment
    end_ms: int
    speech_ms: int
    embedding: Vector | None  # unit length; None when the turn was too short to embed


@dataclass(frozen=True, slots=True)
class Speaker:
    id: int
    name: str | None
    is_self: bool
    turns: int
    speech_ms: int
    last_ms: int | None  # wall-clock time of the latest turn
    sample: SampleTurn | None  # the best clip to play when naming this voice


@dataclass(frozen=True, slots=True)
class SampleTurn:
    turn_id: int
    segment_id: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class StoredTurn:
    id: int
    segment_id: int
    speaker_id: int | None
    start_ms: int  # offsets in the segment
    end_ms: int
    speech_ms: int
    segment_start_ms: int  # wall-clock time of the segment, UTC ms
    transcript: str | None


class SpeakerError(Exception):
    """A request that makes no sense (merging a speaker into itself, an unknown id)."""


def _vec(blob: object) -> Vector:
    return np.frombuffer(bytes(blob), dtype=np.float32)  # pyright: ignore[reportArgumentType]


def best_match(
    embedding: Vector, profiles: list[tuple[int, Vector]], threshold: float
) -> tuple[int, float] | None:
    """The profile closest to ``embedding`` (cosine), if it is close enough."""
    best: tuple[int, float] | None = None
    probe = unit(embedding)
    for speaker_id, centroid in profiles:
        score = float(np.dot(probe, unit(centroid)))
        if best is None or score > best[1]:
            best = (speaker_id, score)
    return best if best is not None and best[1] >= threshold else None


class SpeakerStore:
    def __init__(self, conn: Connection, threshold: float = DEFAULT_THRESHOLD) -> None:
        self._conn = conn
        self._threshold = threshold

    # --- recording ------------------------------------------------------------------------

    def has_turns(self, segment_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM speaker_turns WHERE segment_id = ? LIMIT 1", (segment_id,)
        ).fetchone()
        return row is not None

    def record_self(self, segment_id: int, speech_ms: int, length_ms: int, now_ms: int) -> None:
        """A microphone segment: one turn, the user's."""
        with db.transaction(self._conn) as conn:
            me = self._self_id(conn, now_ms)
            conn.execute(
                "INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms, speech_ms)"
                " VALUES (?, ?, 0, ?, ?)",
                (segment_id, me, length_ms, speech_ms),
            )
            conn.execute("UPDATE audio_segments SET speaker_id = ? WHERE id = ?", (me, segment_id))

    def record_turns(self, segment_id: int, turns: list[NewTurn], now_ms: int) -> None:
        """Identify turns; attribute the transcript only when all turns identify one person."""
        with db.transaction(self._conn) as conn:
            for turn in turns:
                speaker_id, in_profile = self._identify(conn, turn, now_ms)
                conn.execute(
                    "INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms,"
                    " speech_ms, embedding, in_profile) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        segment_id,
                        speaker_id,
                        turn.start_ms,
                        turn.end_ms,
                        turn.speech_ms,
                        None if turn.embedding is None else unit(turn.embedding).tobytes(),
                        int(in_profile),
                    ),
                )
            self._refresh_segment(conn, segment_id)

    def _identify(self, conn: Connection, turn: NewTurn, now_ms: int) -> tuple[int | None, bool]:
        if turn.embedding is None or turn.speech_ms < MIN_IDENTIFY_MS:
            return None, False
        embedding = unit(turn.embedding)
        match = best_match(embedding, self._profiles(conn), self._threshold)
        may_update = turn.speech_ms >= MIN_UPDATE_MS
        if match is not None:
            if may_update:
                self._add_to_profile(conn, match[0], embedding)
            return match[0], may_update
        if not may_update:
            return None, False
        cursor = conn.execute(
            "INSERT INTO speakers (created_ms) VALUES (?)",
            (now_ms,),
        )
        new_id = cursor.lastrowid
        if new_id is None:  # pragma: no cover - INSERT always sets it
            msg = "insert returned no row id"
            raise RuntimeError(msg)
        self._add_to_profile(conn, new_id, embedding)
        return new_id, True

    # --- profiles -------------------------------------------------------------------------

    @staticmethod
    def _profiles(conn: Connection) -> list[tuple[int, Vector]]:
        rows = conn.execute(
            "SELECT id, vec_sum FROM speakers WHERE vec_n > 0 AND vec_sum IS NOT NULL"
        ).fetchall()
        return [(int(r[0]), _vec(r[1])) for r in rows]

    @staticmethod
    def _add_to_profile(conn: Connection, speaker_id: int, embedding: Vector) -> None:
        row = conn.execute(
            "SELECT vec_sum, vec_n FROM speakers WHERE id = ?", (speaker_id,)
        ).fetchone()
        total = embedding.copy() if row[0] is None else _vec(row[0]) + embedding
        conn.execute(
            "UPDATE speakers SET vec_sum = ?, vec_n = ? WHERE id = ?",
            (total.astype(np.float32).tobytes(), row[1] + 1, speaker_id),
        )

    @staticmethod
    def _remove_from_profile(conn: Connection, speaker_id: int, embedding: Vector) -> None:
        row = conn.execute(
            "SELECT vec_sum, vec_n FROM speakers WHERE id = ?", (speaker_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return
        count = row[1] - 1
        total = None if count <= 0 else (_vec(row[0]) - embedding).astype(np.float32).tobytes()
        conn.execute(
            "UPDATE speakers SET vec_sum = ?, vec_n = ? WHERE id = ?",
            (total, max(count, 0), speaker_id),
        )

    def _self_id(self, conn: Connection, now_ms: int) -> int:
        row = conn.execute("SELECT id FROM speakers WHERE is_self = 1").fetchone()
        if row is not None:
            return int(row[0])
        cursor = conn.execute(
            "INSERT INTO speakers (name, is_self, created_ms) VALUES (?, 1, ?)",
            (SELF_NAME, now_ms),
        )
        assert cursor.lastrowid is not None  # noqa: S101 - INSERT always sets it
        return cursor.lastrowid

    @staticmethod
    def _refresh_segment(conn: Connection, segment_id: int) -> None:
        """Without word alignment, a mixed or partly unknown transcript has no single author."""
        row = conn.execute(
            "SELECT min(speaker_id) FROM speaker_turns WHERE segment_id = ?"
            " HAVING count(DISTINCT speaker_id) = 1 AND count(speaker_id) = count(*)",
            (segment_id,),
        ).fetchone()
        conn.execute(
            "UPDATE audio_segments SET speaker_id = ? WHERE id = ?",
            (None if row is None else row[0], segment_id),
        )

    # --- what the user does ---------------------------------------------------------------

    def all_speakers(self) -> list[Speaker]:
        """Every speaker, the user first, then the most talkative."""
        rows = self._conn.execute(
            "SELECT s.id, s.name, s.is_self, count(t.id), coalesce(sum(t.speech_ms), 0),"
            " max(a.start_ms) FROM speakers s"
            " LEFT JOIN speaker_turns t ON t.speaker_id = s.id"
            " LEFT JOIN audio_segments a ON a.id = t.segment_id"
            " GROUP BY s.id ORDER BY s.is_self DESC, sum(t.speech_ms) DESC, s.id"
        ).fetchall()
        return [
            Speaker(
                int(r[0]),
                None if r[1] is None else str(r[1]),
                bool(r[2]),
                int(r[3]),
                int(r[4]),
                None if r[5] is None else int(r[5]),
                self._sample(int(r[0])),
            )
            for r in rows
        ]

    def _sample(self, speaker_id: int) -> SampleTurn | None:
        row = self._conn.execute(
            "SELECT t.id, t.segment_id, t.start_ms, t.end_ms FROM speaker_turns t"
            " JOIN audio_segments a ON a.id = t.segment_id"
            " WHERE t.speaker_id = ? AND a.audio_path IS NOT NULL"
            " ORDER BY t.speech_ms DESC, t.id DESC LIMIT 1",
            (speaker_id,),
        ).fetchone()
        return None if row is None else SampleTurn(*(int(v) for v in row))

    def turns_of(self, speaker_id: int, limit: int = 20) -> list[StoredTurn]:
        """The latest turns of one speaker, newest first, with what was said."""
        rows = self._conn.execute(
            "SELECT t.id, t.segment_id, t.speaker_id, t.start_ms, t.end_ms, t.speech_ms,"
            " a.start_ms, CASE WHEN a.speaker_id = t.speaker_id THEN a.transcript END"
            " FROM speaker_turns t"
            " JOIN audio_segments a ON a.id = t.segment_id WHERE t.speaker_id = ?"
            " ORDER BY a.start_ms DESC, t.id DESC LIMIT ?",
            (speaker_id, max(1, min(limit, 100))),
        ).fetchall()
        return [
            StoredTurn(
                int(r[0]),
                int(r[1]),
                None if r[2] is None else int(r[2]),
                int(r[3]),
                int(r[4]),
                int(r[5]),
                int(r[6]),
                None if r[7] is None else str(r[7]),
            )
            for r in rows
        ]

    def rename(self, speaker_id: int, name: str | None) -> None:
        """Name a voice (or clear the name with an empty one). The user's own is renamable too."""
        cleaned = (name or "").strip()[:80] or None
        with db.transaction(self._conn) as conn:
            if (
                conn.execute("SELECT 1 FROM speakers WHERE id = ?", (speaker_id,)).fetchone()
                is None
            ):
                msg = "no such speaker"
                raise SpeakerError(msg)
            conn.execute("UPDATE speakers SET name = ? WHERE id = ?", (cleaned, speaker_id))

    def merge(self, source_id: int, target_id: int) -> None:
        """Two profiles of one person become one: ``source`` disappears into ``target``."""
        if source_id == target_id:
            msg = "cannot merge a speaker into itself"
            raise SpeakerError(msg)
        with db.transaction(self._conn) as conn:
            rows = {
                int(r[0]): r
                for r in conn.execute(
                    "SELECT id, is_self, vec_sum, vec_n FROM speakers WHERE id IN (?, ?)",
                    (source_id, target_id),
                )
            }
            if len(rows) != 2:  # noqa: PLR2004 - both must exist
                msg = "no such speaker"
                raise SpeakerError(msg)
            if rows[source_id][1]:
                msg = "the user's own voice cannot be merged into another"
                raise SpeakerError(msg)
            source_sum, source_n = rows[source_id][2], int(rows[source_id][3])
            if source_sum is not None and source_n > 0:
                target_sum = rows[target_id][2]
                total = _vec(source_sum) + (0 if target_sum is None else _vec(target_sum))
                conn.execute(
                    "UPDATE speakers SET vec_sum = ?, vec_n = vec_n + ? WHERE id = ?",
                    (np.asarray(total, dtype=np.float32).tobytes(), source_n, target_id),
                )
            segments = [
                int(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT segment_id FROM speaker_turns WHERE speaker_id = ?",
                    (source_id,),
                )
            ]
            conn.execute(
                "UPDATE speaker_turns SET speaker_id = ? WHERE speaker_id = ?",
                (target_id, source_id),
            )
            conn.execute("DELETE FROM speakers WHERE id = ?", (source_id,))
            for segment_id in segments:
                self._refresh_segment(conn, segment_id)

    def reassign_turn(self, turn_id: int, speaker_id: int | None, now_ms: int) -> int:
        """Move one turn to another speaker, or to a brand-new unnamed one when ``speaker_id``
        is None: how a wrong match is split off. Returns the speaker it now belongs to."""
        with db.transaction(self._conn) as conn:
            row = conn.execute(
                "SELECT segment_id, speaker_id, embedding, in_profile FROM speaker_turns"
                " WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                msg = "no such turn"
                raise SpeakerError(msg)
            segment_id, old_id, blob, in_profile = row
            if speaker_id is None:
                cursor = conn.execute("INSERT INTO speakers (created_ms) VALUES (?)", (now_ms,))
                assert cursor.lastrowid is not None  # noqa: S101 - INSERT always sets it
                target = cursor.lastrowid
            else:
                if (
                    conn.execute("SELECT 1 FROM speakers WHERE id = ?", (speaker_id,)).fetchone()
                    is None
                ):
                    msg = "no such speaker"
                    raise SpeakerError(msg)
                target = speaker_id
            if target == old_id:
                return target
            embedding = None if blob is None else _vec(blob)
            if in_profile and old_id is not None and embedding is not None:
                self._remove_from_profile(conn, int(old_id), embedding)
            if embedding is not None:
                self._add_to_profile(conn, target, embedding)
            conn.execute(
                "UPDATE speaker_turns SET speaker_id = ?, in_profile = ? WHERE id = ?",
                (target, int(embedding is not None), turn_id),
            )
            self._refresh_segment(conn, int(segment_id))
        return target
