"""Capture gaps: periods when something was not being recorded, and why.

Rows are content-free (a closed vocabulary and two integers). A row with ``end_ms IS NULL`` is
still going on. Every end time is written as ``MAX(?, start_ms)``, so a clock that moved backwards
can never produce a row the schema refuses. ``overlapping`` reads them back, treating a stale
heartbeat as a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from akashic.storage import db, state

if TYPE_CHECKING:
    from collections.abc import Collection
    from sqlite3 import Connection

HEARTBEAT_MS: Final = 5_000
HEARTBEAT_STALE_MS: Final = 2 * HEARTBEAT_MS

type GapKind = Literal["all", "screen", "screen_text", "mic", "system"]
type GapReason = Literal[
    "paused",
    "stopped",
    "crashed",
    "stalled",
    "locked",
    "ocr_unavailable",
    "audio_unavailable",
    "disabled",
    "device_missing",
    "device_failed",
    "excluded",
    "unknown_sources",
    "music",
    "backlog",
    "write_failed",
    "ui_open",
    "voice_elsewhere",
]

_CLOSE_OPEN = "UPDATE capture_gaps SET end_ms = MAX(?, start_ms) WHERE end_ms IS NULL"


@dataclass(frozen=True, slots=True)
class GapSpan:
    kind: GapKind
    reason: GapReason
    start_ms: int
    end_ms: int


def overlapping(
    conn: Connection, a: int, b: int, now: int, kinds: Collection[GapKind]
) -> list[GapSpan]:
    """Gaps of ``kinds`` that overlap [a, b], sorted by start.

    A closed row [s, e) overlaps when s <= b and e > a; an empty row never overlaps. An open row
    ends at ``now``. When the capture heartbeat is stale and no clean stop is open, capture died:
    open rows end at the heartbeat (or their start) and a synthetic ``all/crashed`` span covers
    the time from the heartbeat to ``now``.
    """
    if not kinds:
        return []
    heartbeat = state.get_int(conn, state.CAPTURE_ALIVE, 0)
    crashed = (
        heartbeat > 0
        and now - heartbeat > HEARTBEAT_STALE_MS
        and conn.execute(
            "SELECT 1 FROM capture_gaps"
            " WHERE end_ms IS NULL AND kind = 'all' AND reason = 'stopped'"
        ).fetchone()
        is None
    )
    rows = conn.execute(
        "SELECT kind, reason, start_ms, end_ms FROM capture_gaps"  # noqa: S608
        f" WHERE kind IN ({','.join('?' * len(kinds))}) AND start_ms <= ?"
        " AND (end_ms IS NULL OR (end_ms > ? AND end_ms > start_ms))",
        (*kinds, b, a),
    ).fetchall()
    spans: list[GapSpan] = []
    for kind, reason, start_ms, closed_end_ms in rows:
        if closed_end_ms is not None:
            end_ms = closed_end_ms
        elif crashed:
            end_ms = max(heartbeat, start_ms)
            if end_ms <= max(a, start_ms):
                continue
        elif now < a:
            continue
        else:
            end_ms = now
        spans.append(GapSpan(kind, reason, start_ms, end_ms))
    if crashed and "all" in kinds and heartbeat <= b and now > a:
        spans.append(GapSpan("all", "crashed", heartbeat, now))
    return sorted(spans, key=lambda span: span.start_ms)


class GapLog:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    @property
    def conn(self) -> Connection:
        return self._conn

    def recover(self, start_ms: int) -> None:
        """Settle what the previous run left behind, in one transaction.

        After a clean stop only the ``stopped`` row is closed, at this start. After a crash every
        open row is closed at this start (never before its own start or the heartbeat) and the
        time from the heartbeat to this start is recorded as ``crashed``. The heartbeat is then
        spent, so a run that dies before its own first heartbeat is not blamed for the older
        one's time.
        """
        with db.transaction(self._conn) as conn:
            heartbeat = state.get_int(conn, state.CAPTURE_ALIVE, 0)
            stopped = conn.execute(
                f"{_CLOSE_OPEN} AND kind = 'all' AND reason = 'stopped'",
                (start_ms,),
            ).rowcount
            if not stopped:
                conn.execute(_CLOSE_OPEN, (max(start_ms, heartbeat),))
                if 0 < heartbeat < start_ms:
                    self._insert(conn, "all", "crashed", heartbeat, start_ms)
            conn.execute("DELETE FROM state WHERE key = ?", (state.CAPTURE_ALIVE,))

    def open(self, kind: GapKind, reason: GapReason, ts_ms: int) -> int:
        with db.transaction(self._conn) as conn:
            return self._insert(conn, kind, reason, ts_ms, None)

    def close(self, gap_id: int, ts_ms: int) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "UPDATE capture_gaps SET end_ms = MAX(?, start_ms) WHERE id = ?", (ts_ms, gap_id)
            )

    def add(self, kind: GapKind, reason: GapReason, start_ms: int, end_ms: int) -> int:
        with db.transaction(self._conn) as conn:
            return self._insert(conn, kind, reason, start_ms, max(start_ms, end_ms))

    def extend(self, gap_id: int, end_ms: int) -> None:
        """Move the end later; an earlier end (clock went back) never shrinks the row."""
        with db.transaction(self._conn) as conn:
            conn.execute(
                "UPDATE capture_gaps SET end_ms = MAX(?, end_ms, start_ms) WHERE id = ?",
                (end_ms, gap_id),
            )

    def heartbeat(self, ts_ms: int) -> None:
        state.set_int(self._conn, state.CAPTURE_ALIVE, ts_ms)

    def stop(self, ts_ms: int) -> None:
        """Close every open row and open ``stopped``, which the next start closes."""
        with db.transaction(self._conn) as conn:
            conn.execute(_CLOSE_OPEN, (ts_ms,))
            self._insert(conn, "all", "stopped", ts_ms, None)

    @staticmethod
    def _insert(
        conn: Connection, kind: GapKind, reason: GapReason, start_ms: int, end_ms: int | None
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, ?, ?, ?)",
            (kind, reason, start_ms, end_ms),
        )
        row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover - INSERT always sets it
            msg = "insert returned no row id"
            raise RuntimeError(msg)
        return row_id
