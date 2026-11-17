"""Reading capture gaps back: overlap rules, open rows and the stale-heartbeat crash."""

from __future__ import annotations

from typing import TYPE_CHECKING, get_args

import pytest
import sqlcipher3

from akashic.capture import gaps as capture_gaps
from akashic.storage import state
from akashic.storage.gaps import (
    HEARTBEAT_MS,
    HEARTBEAT_STALE_MS,
    GapKind,
    GapLog,
    GapReason,
    GapSpan,
    overlapping,
)

if TYPE_CHECKING:
    from sqlite3 import Connection

ALL_KINDS: tuple[GapKind, ...] = ("all", "screen", "screen_text", "mic", "system")
NOW = 100_000


def add(conn: Connection, kind: str, reason: str, start: int, end: int | None) -> None:
    conn.execute(
        "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, ?, ?, ?)",
        (kind, reason, start, end),
    )


def beat(conn: Connection, ts: int) -> None:
    state.set_int(conn, state.CAPTURE_ALIVE, ts)


def test_heartbeat_constants_live_in_storage_and_capture_reuses_them() -> None:
    assert HEARTBEAT_MS == 5_000
    assert HEARTBEAT_STALE_MS == 10_000
    assert capture_gaps.HEARTBEAT_MS is HEARTBEAT_MS


def test_ui_open_is_a_valid_reason(conn: Connection) -> None:
    assert "ui_open" in get_args(GapReason.__value__)
    add(conn, "screen", "ui_open", 10, 20)
    assert overlapping(conn, 0, 100, NOW, ["screen"]) == [GapSpan("screen", "ui_open", 10, 20)]


def test_voice_elsewhere_is_a_valid_reason(conn: Connection) -> None:
    assert "voice_elsewhere" in get_args(GapReason.__value__)
    log = GapLog(conn)
    log.add("mic", "voice_elsewhere", 10, 20)
    log.open("system", "voice_elsewhere", 30)
    assert overlapping(conn, 0, 100, NOW, ["mic", "system"]) == [
        GapSpan("mic", "voice_elsewhere", 10, 20),
        GapSpan("system", "voice_elsewhere", 30, NOW),
    ]


def test_closed_rows_overlap_when_they_start_before_b_and_end_after_a(conn: Connection) -> None:
    add(conn, "mic", "paused", 10, 20)
    assert overlapping(conn, 19, 30, NOW, ["mic"]) == [GapSpan("mic", "paused", 10, 20)]
    assert overlapping(conn, 0, 10, NOW, ["mic"]) == [GapSpan("mic", "paused", 10, 20)]
    assert overlapping(conn, 20, 30, NOW, ["mic"]) == []
    assert overlapping(conn, 0, 9, NOW, ["mic"]) == []


def test_a_zero_length_row_is_never_returned(conn: Connection) -> None:
    add(conn, "mic", "device_failed", 10, 10)
    assert overlapping(conn, 10, 10, NOW, ["mic"]) == []


def test_a_zero_length_row_inside_a_wider_window_is_not_returned(conn: Connection) -> None:
    add(conn, "mic", "device_failed", 10, 10)
    assert overlapping(conn, 5, 15, NOW, ["mic"]) == []


def test_a_crashed_open_row_opened_after_the_heartbeat_is_not_returned(conn: Connection) -> None:
    add(conn, "mic", "paused", 5_000, None)
    beat(conn, 1_000)
    assert overlapping(conn, 0, NOW, NOW, ["mic"]) == []
    assert overlapping(conn, 0, NOW, NOW, ALL_KINDS) == [GapSpan("all", "crashed", 1_000, NOW)]


def test_results_are_filtered_by_kind_and_sorted_by_start(conn: Connection) -> None:
    add(conn, "system", "paused", 30, 40)
    add(conn, "mic", "paused", 10, 40)
    add(conn, "screen", "stalled", 20, 40)
    spans = overlapping(conn, 0, 50, NOW, ["system", "mic"])
    assert [(s.kind, s.start_ms) for s in spans] == [("mic", 10), ("system", 30)]
    assert [s.start_ms for s in overlapping(conn, 0, 50, NOW, ALL_KINDS)] == [10, 20, 30]


def test_empty_kinds_return_nothing(conn: Connection) -> None:
    add(conn, "mic", "paused", 10, 20)
    assert overlapping(conn, 0, 50, NOW, []) == []


def test_an_open_row_ends_at_now(conn: Connection) -> None:
    add(conn, "mic", "paused", 100, None)
    assert overlapping(conn, 150, 160, 200, ["mic"]) == [GapSpan("mic", "paused", 100, 200)]
    assert overlapping(conn, 200, 300, 200, ["mic"]) == [GapSpan("mic", "paused", 100, 200)]
    assert overlapping(conn, 50, 90, 200, ["mic"]) == []
    assert overlapping(conn, 250, 300, 200, ["mic"]) == []


def test_without_a_heartbeat_there_is_no_crash(conn: Connection) -> None:
    add(conn, "mic", "paused", 100, None)
    assert overlapping(conn, 0, NOW, NOW, ALL_KINDS) == [GapSpan("mic", "paused", 100, NOW)]
    beat(conn, 0)
    assert overlapping(conn, 0, NOW, NOW, ALL_KINDS) == [GapSpan("mic", "paused", 100, NOW)]


def test_a_fresh_heartbeat_is_not_a_crash(conn: Connection) -> None:
    add(conn, "mic", "paused", 100, None)
    beat(conn, NOW - HEARTBEAT_STALE_MS)
    assert overlapping(conn, 0, NOW, NOW, ALL_KINDS) == [GapSpan("mic", "paused", 100, NOW)]


def test_a_stale_heartbeat_closes_open_rows_and_adds_a_crashed_span(conn: Connection) -> None:
    add(conn, "mic", "paused", 500, None)
    beat(conn, 1_000)
    assert overlapping(conn, 0, NOW, NOW, ALL_KINDS) == [
        GapSpan("mic", "paused", 500, 1_000),
        GapSpan("all", "crashed", 1_000, NOW),
    ]
    assert overlapping(conn, 1_500, NOW, NOW, ALL_KINDS) == [GapSpan("all", "crashed", 1_000, NOW)]


def test_the_crashed_span_needs_all_in_kinds_and_an_overlap(conn: Connection) -> None:
    add(conn, "mic", "paused", 500, None)
    beat(conn, 1_000)
    assert overlapping(conn, 0, NOW, NOW, ["mic"]) == [GapSpan("mic", "paused", 500, 1_000)]
    assert overlapping(conn, 0, 999, NOW, ["all"]) == []
    assert overlapping(conn, 0, 1_000, NOW, ["all"]) == [GapSpan("all", "crashed", 1_000, NOW)]


def test_an_open_stopped_row_means_a_clean_stop_not_a_crash(conn: Connection) -> None:
    add(conn, "all", "stopped", 600, None)
    add(conn, "mic", "paused", 500, None)
    beat(conn, 1_000)
    assert overlapping(conn, 0, NOW, NOW, ALL_KINDS) == [
        GapSpan("mic", "paused", 500, NOW),
        GapSpan("all", "stopped", 600, NOW),
    ]


def test_sqlite_errors_propagate(conn: Connection) -> None:
    conn.close()
    with pytest.raises(sqlcipher3.Error):
        overlapping(conn, 0, 10, NOW, ["mic"])
