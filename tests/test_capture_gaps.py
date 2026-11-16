"""Capture gaps: the schema, crash and stop recovery, the writer thread's rules, the audio and
OCR reports, retention and Delete all."""

from __future__ import annotations

import io
import sqlite3
import threading
from collections import Counter
from typing import TYPE_CHECKING, Any, cast

import pytest
import sqlcipher3
from audio_fakes import SETTINGS, T0, Clock, FakeDetector, FakeDevices, FakeSessions, speech
from capture_fakes import (
    LONG_TEXT,
    FakeOcr,
    blank,
)
from screen_rig import fake_platform

from akashic import child
from akashic.capture import gaps as gaps_module
from akashic.capture import main as capture_main
from akashic.capture.audio import SegmentWriter, SourceSampler, StreamRecorder, Utterance
from akashic.capture.exclusions import Exclusions
from akashic.capture.gaps import (
    MAX_PENDING_DROPS,
    CaptureGaps,
)
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.ports import AudioDevice, AudioError, AudioPlatform, CapturePlatform
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db, state
from akashic.storage.audio import AudioStore
from akashic.storage.crypto import DataKey
from akashic.storage.gaps import GapLog
from akashic.storage.retention import DAY_MS, apply_retention, delete_all_recorded_data

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.capture.ports import AudioKind
    from akashic.config import Paths
    from akashic.storage.gaps import GapKind, GapReason
    from akashic.storage.media import MediaStore

Row = tuple[str, str, int, int | None]


def rows(conn: Connection) -> list[Row]:
    return [
        (str(k), str(r), int(s), None if e is None else int(e))
        for k, r, s, e in conn.execute(
            "SELECT kind, reason, start_ms, end_ms FROM capture_gaps ORDER BY id"
        )
    ]


# --- schema ----------------------------------------------------------------------------------


def insert(conn: Connection, kind: object, reason: object, start: object, end: object) -> None:
    conn.execute(
        "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES (?, ?, ?, ?)",
        (kind, reason, start, end),
    )


def test_the_table_accepts_valid_rows_and_has_a_start_index(conn: Connection) -> None:
    insert(conn, "all", "paused", 10, None)
    insert(conn, "mic", "device_failed", 10, 10)
    assert rows(conn) == [("all", "paused", 10, None), ("mic", "device_failed", 10, 10)]
    indexes = conn.execute("PRAGMA index_list('capture_gaps')").fetchall()
    on_start = [
        i
        for i in indexes
        if [c[2] for c in conn.execute(f"PRAGMA index_info('{i[1]}')")] == ["start_ms"]
    ]
    assert on_start


@pytest.mark.parametrize("kind", ["all", "screen", "screen_text", "mic", "system"])
def test_every_listed_kind_is_accepted(conn: Connection, kind: str) -> None:
    insert(conn, kind, "paused", 1, None)


@pytest.mark.parametrize(
    "reason",
    [
        "paused", "stopped", "crashed", "stalled", "locked", "ocr_unavailable",
        "audio_unavailable", "disabled", "device_missing", "device_failed", "excluded",
        "unknown_sources", "music", "backlog", "write_failed",
    ],
)  # fmt: skip
def test_every_listed_reason_is_accepted(conn: Connection, reason: str) -> None:
    insert(conn, "all", reason, 1, None)


@pytest.mark.parametrize(
    ("kind", "reason", "start", "end"),
    [
        ("monitor", "paused", 1, None),
        ("", "paused", 1, None),
        (None, "paused", 1, None),
        ("all", "vacation", 1, None),
        ("all", "PAUSED", 1, None),
        ("all", None, 1, None),
        ("all", "paused", None, None),
        ("all", "paused", "anything", None),
        ("all", "paused", 1.5, None),
        ("all", "paused", b"blob", None),
        ("all", "paused", 1, "anything"),
        ("all", "paused", 1, 2.5),
        ("all", "paused", 1, b"blob"),
        ("all", "paused", 10, 9),
        ("all", "paused", 0, -1),
    ],
)
def test_the_table_refuses_anything_that_could_hold_content(
    conn: Connection, kind: object, reason: object, start: object, end: object
) -> None:
    with pytest.raises(sqlcipher3.IntegrityError):
        insert(conn, kind, reason, start, end)
    assert rows(conn) == []


def test_an_update_cannot_make_a_row_invalid_either(conn: Connection) -> None:
    insert(conn, "all", "paused", 10, None)
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute("UPDATE capture_gaps SET end_ms = 5")
    with pytest.raises(sqlcipher3.IntegrityError):
        conn.execute("UPDATE capture_gaps SET reason = 'secret text'")


# --- recovery of the previous run -----------------------------------------------------------


def seed(conn: Connection, *specs: Row) -> None:
    for kind, reason, start, end in specs:
        insert(conn, kind, reason, start, end)


def test_after_a_clean_stop_the_stopped_row_is_closed_at_the_new_start(conn: Connection) -> None:
    seed(conn, ("all", "stopped", 1_000, None))
    state.set_int(conn, state.CAPTURE_ALIVE, 900)  # a stale heartbeat must not matter
    GapLog(conn).recover(5_000)
    assert rows(conn) == [("all", "stopped", 1_000, 5_000)]


def test_after_a_crash_open_rows_close_and_the_heartbeat_gap_is_recorded(
    conn: Connection,
) -> None:
    seed(conn, ("all", "paused", 100, None), ("screen_text", "ocr_unavailable", 200, None))
    state.set_int(conn, state.CAPTURE_ALIVE, 900)
    GapLog(conn).recover(1_000)
    assert rows(conn) == [
        ("all", "paused", 100, 1_000),
        ("screen_text", "ocr_unavailable", 200, 1_000),
        ("all", "crashed", 900, 1_000),
    ]
    assert state.get_int(conn, state.CAPTURE_ALIVE, 0) == 0  # spent


def test_without_a_heartbeat_open_rows_still_close_and_no_crash_is_added(
    conn: Connection,
) -> None:
    seed(conn, ("mic", "device_failed", 50, None), ("system", "device_missing", 2_000, None))
    GapLog(conn).recover(1_000)
    assert rows(conn) == [
        ("mic", "device_failed", 50, 1_000),
        ("system", "device_missing", 2_000, 2_000),  # never before its own start
    ]


@pytest.mark.parametrize("heartbeat", [1_000, 1_500])
def test_a_heartbeat_at_or_after_the_start_adds_no_crash_row(
    conn: Connection, heartbeat: int
) -> None:
    seed(conn, ("all", "paused", 100, None))
    state.set_int(conn, state.CAPTURE_ALIVE, heartbeat)
    GapLog(conn).recover(1_000)
    assert rows(conn) == [("all", "paused", 100, max(1_000, heartbeat))]


def test_a_run_that_died_before_its_first_heartbeat_leaves_no_row_open(conn: Connection) -> None:
    state.set_int(conn, state.CAPTURE_ALIVE, 900)
    log = GapLog(conn)
    log.recover(1_000)  # run 2 starts after a crash and spends the heartbeat
    log.open("mic", "device_failed", 1_100)  # ... flushes a row and dies before its first beat
    log.recover(2_000)  # run 3
    assert rows(conn) == [("all", "crashed", 900, 1_000), ("mic", "device_failed", 1_100, 2_000)]


def test_recovery_survives_a_clock_that_moved_backwards(conn: Connection) -> None:
    seed(conn, ("all", "stopped", 9_000, None))
    state.set_int(conn, state.CAPTURE_ALIVE, 9_500)  # later than the new start
    GapLog(conn).recover(1_000)  # raises nothing
    assert rows(conn) == [("all", "stopped", 9_000, 9_000)]
    assert not conn.in_transaction


def test_a_crash_recovery_survives_a_clock_that_moved_backwards(conn: Connection) -> None:
    seed(conn, ("mic", "device_failed", 8_000, None))
    state.set_int(conn, state.CAPTURE_ALIVE, 9_500)  # later than the new start
    GapLog(conn).recover(1_000)
    assert rows(conn) == [("mic", "device_failed", 8_000, 9_500)]  # no crashed row


def test_a_crash_with_a_leftover_row_from_the_future_stays_consistent(conn: Connection) -> None:
    seed(conn, ("all", "paused", 9_000, None))
    state.set_int(conn, state.CAPTURE_ALIVE, 500)
    GapLog(conn).recover(1_000)
    assert rows(conn) == [("all", "paused", 9_000, 9_000), ("all", "crashed", 500, 1_000)]


def test_recovery_is_one_transaction(conn: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(conn, ("all", "paused", 100, None))
    state.set_int(conn, state.CAPTURE_ALIVE, 900)
    real = GapLog._insert

    def boom(*_a: object, **_k: object) -> int:
        raise sqlcipher3.OperationalError("database is locked")

    monkeypatch.setattr(GapLog, "_insert", staticmethod(boom))
    with pytest.raises(sqlcipher3.OperationalError):
        GapLog(conn).recover(1_000)
    monkeypatch.setattr(GapLog, "_insert", staticmethod(real))
    assert rows(conn) == [("all", "paused", 100, None)]  # nothing half done
    assert state.get_int(conn, state.CAPTURE_ALIVE, 0) == 900


# --- the gap thread, driven tick by tick ------------------------------------------------------


class FlakyLog(GapLog):
    """A GapLog whose next calls to named methods raise the queued exceptions."""

    def __init__(self, conn: Connection) -> None:
        super().__init__(conn)
        self.fail: dict[str, list[Exception]] = {}
        self.calls: list[str] = []

    def _maybe(self, name: str) -> None:
        self.calls.append(name)
        queue = self.fail.get(name)
        if queue:
            raise queue.pop(0)

    def recover(self, start_ms: int) -> None:
        self._maybe("recover")
        super().recover(start_ms)

    def open(self, kind: GapKind, reason: GapReason, ts_ms: int) -> int:
        self._maybe("open")
        return super().open(kind, reason, ts_ms)

    def close(self, gap_id: int, ts_ms: int) -> None:
        self._maybe("close")
        super().close(gap_id, ts_ms)

    def add(self, kind: GapKind, reason: GapReason, start_ms: int, end_ms: int) -> int:
        self._maybe("add")
        return super().add(kind, reason, start_ms, end_ms)

    def extend(self, gap_id: int, end_ms: int) -> None:
        self._maybe("extend")
        super().extend(gap_id, end_ms)

    def heartbeat(self, ts_ms: int) -> None:
        self._maybe("heartbeat")
        super().heartbeat(ts_ms)

    def stop(self, ts_ms: int) -> None:
        self._maybe("stop")
        super().stop(ts_ms)


def busy() -> Exception:
    return sqlcipher3.OperationalError("database is locked")


class Rig:
    def __init__(self, conn: Connection, start_ms: int = 10_000) -> None:
        self.conn = conn
        self.clock = Clock(start_ms)
        self.log = FlakyLog(conn)
        self.gaps = CaptureGaps(self.log, self.clock, start_ms)
        self.paused = threading.Event()
        self.halted = threading.Event()  # paused, UI open or failsafe
        self.locked = False

    def tick(self, advance_ms: int = 1_000) -> None:
        self.clock.now += advance_ms
        self.gaps._tick(self.paused, self.halted, lambda: self.locked)


@pytest.fixture
def rig(conn: Connection) -> Rig:
    return Rig(conn)


def test_pause_and_resume_open_and_close_one_row(rig: Rig) -> None:
    rig.tick()
    assert rows(rig.conn) == []
    rig.paused.set()
    rig.tick()
    rig.tick()
    assert rows(rig.conn) == [("all", "paused", 12_000, None)]
    rig.paused.clear()
    rig.tick()
    assert rows(rig.conn) == [("all", "paused", 12_000, 14_000)]


def test_the_ui_open_halt_opens_one_ui_open_row_and_closes_when_it_ends(rig: Rig) -> None:
    rig.halted.set()
    rig.tick()
    rig.tick()
    assert rows(rig.conn) == [("all", "ui_open", 11_000, None)]
    rig.halted.clear()
    rig.tick()
    assert rows(rig.conn) == [("all", "ui_open", 11_000, 13_000)]


def test_a_pause_during_the_ui_halt_switches_the_row_to_paused_and_back_on_resume(
    rig: Rig,
) -> None:
    rig.halted.set()
    rig.tick()
    rig.paused.set()
    rig.tick()
    assert [r[:2] for r in rows(rig.conn)] == [("all", "ui_open"), ("all", "paused")]
    assert rows(rig.conn)[0][3] == 12_000  # closed as the pause row opened
    rig.paused.clear()  # resume while the UI is still open: still halted
    rig.tick()
    assert [r[:2] for r in rows(rig.conn)] == [
        ("all", "ui_open"),
        ("all", "paused"),
        ("all", "ui_open"),
    ]
    assert [r[3] for r in rows(rig.conn)] == [12_000, 13_000, None]
    rig.halted.clear()
    rig.tick()
    assert all(end is not None for *_, end in rows(rig.conn))


def test_pausing_again_while_paused_adds_no_second_row(rig: Rig) -> None:
    rig.paused.set()
    for _ in range(5):
        rig.tick()
    rig.gaps.open("all", "paused", rig.clock.now)  # a duplicate report
    rig.tick()
    assert len(rows(rig.conn)) == 1


def test_a_capture_that_starts_paused_opens_a_row(rig: Rig) -> None:
    rig.paused.set()
    rig.tick()
    assert rows(rig.conn) == [("all", "paused", 11_000, None)]


def test_pause_twice_makes_two_rows(rig: Rig) -> None:
    for _ in range(2):
        rig.paused.set()
        rig.tick()
        rig.paused.clear()
        rig.tick()
    assert [r[:2] for r in rows(rig.conn)] == [("all", "paused")] * 2


def test_lock_and_pause_are_tracked_separately(rig: Rig) -> None:
    rig.locked = True
    rig.tick()
    rig.paused.set()
    rig.tick()
    assert rows(rig.conn) == [("screen", "locked", 11_000, None), ("all", "paused", 12_000, None)]
    rig.locked = False
    rig.tick()
    assert rows(rig.conn) == [("screen", "locked", 11_000, 13_000), ("all", "paused", 12_000, None)]
    rig.paused.clear()
    rig.tick()
    assert all(end is not None for *_, end in rows(rig.conn))


def test_an_unreadable_lock_state_keeps_the_last_known_one(rig: Rig) -> None:
    rig.locked = True
    rig.tick()
    boom_calls: list[int] = []

    def broken() -> bool:
        boom_calls.append(1)
        raise OSError

    rig.clock.now += 1_000
    rig.gaps._tick(rig.paused, rig.halted, broken)
    assert boom_calls
    assert rows(rig.conn) == [("screen", "locked", 11_000, None)]


def test_a_failing_pause_stamp_is_the_time_of_the_tick_that_saw_it(rig: Rig) -> None:
    rig.log.fail["recover"] = [busy(), busy(), busy()]
    rig.paused.set()
    rig.tick()  # sees the pause at 11 000, recovery busy
    rig.tick()  # still busy
    rig.paused.clear()
    rig.tick()  # sees the resume at 13 000, still busy
    assert rows(rig.conn) == []
    assert rig.gaps.stats["recovery_failed"] == 3
    rig.tick()  # recovery succeeds
    assert rows(rig.conn) == [("all", "paused", 11_000, 13_000)]


def test_no_heartbeat_and_no_flush_before_recovery_has_succeeded(rig: Rig) -> None:
    rig.log.fail["recover"] = [busy()]
    rig.gaps.drop("mic", "music", 1, 2)
    rig.paused.set()
    rig.tick()
    assert rows(rig.conn) == []
    assert state.get_int(rig.conn, state.CAPTURE_ALIVE, 0) == 0
    assert "heartbeat" not in rig.log.calls
    rig.tick()
    assert [r[:2] for r in rows(rig.conn)] == [("mic", "music"), ("all", "paused")]  # in order
    assert state.get_int(rig.conn, state.CAPTURE_ALIVE, 0) == rig.clock.now


def test_recovery_runs_before_any_new_row_and_does_not_close_it(conn: Connection) -> None:
    seed(conn, ("all", "paused", 100, None))
    state.set_int(conn, state.CAPTURE_ALIVE, 5_000)
    rig = Rig(conn, start_ms=10_000)
    rig.paused.set()
    rig.tick()
    assert rows(conn) == [
        ("all", "paused", 100, 10_000),
        ("all", "crashed", 5_000, 10_000),
        ("all", "paused", 11_000, None),
    ]
    assert rig.log.calls.index("recover") < rig.log.calls.index("open")


def test_the_heartbeat_is_written_every_five_seconds(rig: Rig) -> None:
    rig.tick()
    first = state.get_int(rig.conn, state.CAPTURE_ALIVE, 0)
    assert first == 11_000
    rig.tick(2_000)
    rig.tick(2_000)
    assert state.get_int(rig.conn, state.CAPTURE_ALIVE, 0) == first  # < 5 s
    rig.tick(2_000)
    assert state.get_int(rig.conn, state.CAPTURE_ALIVE, 0) == rig.clock.now


# --- stall -----------------------------------------------------------------------------------


def test_a_long_gap_between_ticks_is_a_stall(rig: Rig) -> None:
    rig.tick()
    rig.tick(60_000)
    assert rows(rig.conn) == [("all", "stalled", 11_000, 71_000)]


@pytest.mark.parametrize(("advance", "stalled"), [(30_000, False), (30_001, True)])
def test_the_stall_threshold_is_thirty_seconds(rig: Rig, advance: int, *, stalled: bool) -> None:
    rig.tick()
    rig.tick(advance)
    assert bool(rows(rig.conn)) is stalled


def test_a_busy_recovery_does_not_make_a_false_stall(rig: Rig) -> None:
    rig.log.fail["recover"] = [busy()] * 40
    for _ in range(40):  # 40 s of failing ticks, 1 s apart
        rig.tick()
    rig.log.fail["recover"] = []
    rig.tick()
    assert [r for r in rows(rig.conn) if r[1] == "stalled"] == []


def test_a_stall_while_recovery_fails_is_written_after_it_succeeds(rig: Rig) -> None:
    rig.log.fail["recover"] = [busy(), busy()]
    rig.tick()
    rig.tick(60_000)  # stall seen, recovery fails
    assert rows(rig.conn) == []
    rig.tick()  # recovery succeeds
    assert rows(rig.conn) == [("all", "stalled", 11_000, 71_000)]


# --- busy and rejected writes -----------------------------------------------------------------


@pytest.mark.parametrize("error", [sqlcipher3.OperationalError, sqlite3.OperationalError])
def test_a_busy_write_stays_queued_and_ends_the_flush(rig: Rig, error: type[Exception]) -> None:
    rig.tick()
    rig.gaps.drop("mic", "music", 1, 2)
    rig.gaps.drop("system", "music", 3, 4)
    rig.log.fail["add"] = [error("database is locked")]
    rig.tick()
    assert rows(rig.conn) == []
    assert rig.gaps.stats["write_busy"] == 1  # one failure ended that tick
    rig.tick()
    assert rows(rig.conn) == [("mic", "music", 1, 2), ("system", "music", 3, 4)]


def test_a_rejected_event_is_dropped_counted_and_does_not_block_the_queue(rig: Rig) -> None:
    rig.tick()
    rig.gaps.drop("mic", "music", 1, 2)
    rig.gaps.drop("mic", "excluded", 3, 4)
    rig.log.fail["add"] = [sqlcipher3.IntegrityError("CHECK constraint failed")]
    rig.tick()
    assert rows(rig.conn) == [("mic", "excluded", 3, 4)]
    assert rig.gaps.stats["write_rejected"] == 1
    assert not rig.gaps._events


def test_a_non_busy_operational_error_is_rejected_not_retried(rig: Rig) -> None:
    class Disk(sqlcipher3.OperationalError):
        sqlite_errorcode = 10  # SQLITE_IOERR

    rig.tick()
    rig.gaps.drop("mic", "music", 1, 2)
    rig.log.fail["add"] = [Disk("disk I/O error")]
    rig.tick()
    assert rig.gaps.stats["write_rejected"] == 1
    assert rig.gaps.stats["write_busy"] == 0


def test_a_failing_flush_is_logged_by_type_only(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.tick()
    rig.gaps.drop("mic", "music", 1, 2)
    rig.log.fail["add"] = [sqlcipher3.IntegrityError("SECRET-CONTENT")]
    with caplog.at_level("DEBUG"):
        rig.tick()
    assert "IntegrityError" in caplog.text
    assert "SECRET-CONTENT" not in caplog.text


def test_a_failed_heartbeat_is_counted_and_retried(rig: Rig) -> None:
    rig.log.fail["heartbeat"] = [busy()]
    rig.tick()
    assert rig.gaps.stats["heartbeat_failed"] == 1
    rig.tick()
    assert state.get_int(rig.conn, state.CAPTURE_ALIVE, 0) == rig.clock.now


def test_a_tick_that_raises_is_counted_and_the_loop_goes_on(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop = threading.Event()
    calls = 0

    def tick(*_a: object) -> None:
        nonlocal calls
        calls += 1
        if calls >= 3:
            stop.set()
        raise RuntimeError

    monkeypatch.setattr(gaps_module, "TICK_S", 0.001)
    monkeypatch.setattr(rig.gaps, "_tick", tick)
    rig.gaps.run(stop, rig.paused, rig.halted, lambda: False)
    assert rig.gaps.stats["tick_failed"] == 3


def test_a_clock_jump_backwards_cannot_make_an_event_fail_forever(rig: Rig) -> None:
    rig.paused.set()
    rig.tick()  # open at 11 000
    rig.paused.clear()
    rig.clock.now = 5_000
    rig.gaps._tick(rig.paused, rig.halted, lambda: False)  # resume stamped before the pause
    assert rows(rig.conn) == [("all", "paused", 11_000, 11_000)]
    assert rig.gaps.stats["write_rejected"] == 0


def test_a_drop_with_an_end_before_its_start_is_stored_valid(rig: Rig) -> None:
    rig.tick()
    rig.gaps.drop("mic", "music", 500, 100)
    rig.tick()
    assert rows(rig.conn) == [("mic", "music", 500, 500)]


def test_a_merge_with_an_earlier_end_never_shrinks_the_row(rig: Rig) -> None:
    rig.tick()
    rig.gaps.drop("mic", "music", 1_000, 5_000)
    rig.gaps.drop("mic", "music", 2_000, 3_000, merge=True)
    rig.tick()
    assert rows(rig.conn) == [("mic", "music", 1_000, 5_000)]


# --- drops: merging and the queue bound -------------------------------------------------------


def test_a_drop_writes_a_closed_row(rig: Rig) -> None:
    rig.gaps.drop("system", "excluded", T0, T0 + 2_000)
    rig.tick()
    assert rows(rig.conn) == [("system", "excluded", T0, T0 + 2_000)]


def test_a_merging_drop_extends_the_previous_row_within_thirty_seconds(rig: Rig) -> None:
    rig.gaps.drop("system", "music", 0, 2_000)
    rig.gaps.drop("system", "music", 32_000, 34_000, merge=True)  # exactly 30 s after the end
    rig.tick()
    assert rows(rig.conn) == [("system", "music", 0, 34_000)]


def test_a_merging_drop_more_than_thirty_seconds_later_makes_a_new_row(rig: Rig) -> None:
    rig.gaps.drop("system", "music", 0, 2_000)
    rig.gaps.drop("system", "music", 32_001, 34_000, merge=True)
    rig.tick()
    assert len(rows(rig.conn)) == 2


def test_a_drop_without_the_merge_flag_never_merges(rig: Rig) -> None:
    rig.gaps.drop("system", "music", 0, 2_000)
    rig.gaps.drop("system", "music", 3_000, 4_000)
    rig.tick()
    assert len(rows(rig.conn)) == 2


def test_merging_is_per_stream_and_reason(rig: Rig) -> None:
    rig.gaps.drop("system", "music", 0, 2_000)
    rig.gaps.drop("mic", "music", 3_000, 4_000, merge=True)
    rig.gaps.drop("system", "excluded", 3_000, 4_000, merge=True)
    rig.tick()
    assert len(rows(rig.conn)) == 3


def test_a_rejected_first_row_is_not_extended_by_the_next_drop(rig: Rig) -> None:
    rig.tick()
    rig.gaps.drop("system", "music", 0, 2_000)
    rig.gaps.drop("system", "music", 3_000, 4_000, merge=True)
    rig.log.fail["add"] = [sqlcipher3.IntegrityError("x")]
    rig.tick()
    assert rows(rig.conn) == [("system", "music", 3_000, 4_000)]


def test_only_drops_are_bounded_and_overflow_is_counted(rig: Rig) -> None:
    for i in range(MAX_PENDING_DROPS):
        rig.gaps.drop("mic", "music", i, i)
    rig.gaps.drop("mic", "music", 99_999, 99_999)
    rig.gaps.drop("system", "excluded", 99_999, 99_999, merge=True)
    assert rig.gaps.stats["drops_overflowed"] == 2
    rig.paused.set()
    rig.gaps._watch(
        rig.clock.now, paused=True, halted=True, locked=True
    )  # transitions are never refused
    rig.gaps.open("mic", "device_failed", 5)
    rig.gaps.close("mic", 6)
    kinds = [type(e).__name__ for e in rig.gaps._events]
    assert kinds.count("_Open") == 3
    assert kinds.count("_Close") == 1
    assert kinds.count("_Span") == MAX_PENDING_DROPS


def test_a_drop_after_a_lost_one_does_not_merge_into_an_older_row(rig: Rig) -> None:
    rig.gaps.drop("mic", "music", 0, 1)
    for i in range(MAX_PENDING_DROPS - 1):
        rig.gaps.drop("system", "music", i, i)
    rig.gaps.drop("mic", "music", 10, 11, merge=True)  # lost
    assert rig.gaps.stats["drops_overflowed"] == 1
    rig.tick(1)
    rig.gaps.drop("mic", "music", 12, 13, merge=True)  # continues a lost drop
    rig.tick(1)
    rig.gaps._flush(5.0)
    mic = [r for r in rows(rig.conn) if r[0] == "mic"]
    assert mic == [("mic", "music", 0, 1), ("mic", "music", 12, 13)]


def test_a_long_backlog_does_not_hold_up_the_pause_watch(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gaps_module, "TICK_FLUSH_S", 0.0)
    rig.tick()
    for i in range(50):
        rig.gaps.drop("mic", "music", i, i)
    rig.paused.set()
    rig.tick()
    opens = [e for e in rig.gaps._events if isinstance(e, gaps_module._Open)]
    assert [(o.kind, o.reason, o.ts_ms) for o in opens] == [("all", "paused", 12_000)]
    assert len(rig.gaps._events) == 51  # the backlog is still waiting behind it


# --- clean stop ------------------------------------------------------------------------------


def test_a_clean_stop_closes_everything_and_opens_stopped(rig: Rig) -> None:
    rig.paused.set()
    rig.locked = True
    rig.tick()
    rig.gaps.open("mic", "device_failed", 20_000)
    rig.tick()
    rig.gaps.finish(50_000)
    assert rows(rig.conn) == [
        ("all", "paused", 11_000, 50_000),
        ("screen", "locked", 11_000, 50_000),
        ("mic", "device_failed", 20_000, 50_000),
        ("all", "stopped", 50_000, None),
    ]


def test_stop_never_closes_a_row_before_its_own_start(rig: Rig) -> None:
    rig.gaps.open("mic", "device_failed", 90_000)
    rig.tick()
    rig.gaps.finish(50_000)
    assert ("mic", "device_failed", 90_000, 90_000) in rows(rig.conn)


def test_transitions_after_the_stop_began_are_ignored(rig: Rig) -> None:
    rig.tick()
    rig.gaps.open("mic", "device_failed", 20_000)
    rig.gaps.finish(50_000)
    rig.gaps.open("system", "device_missing", 51_000)
    rig.gaps.close("mic", 52_000)
    rig.gaps.flush_late()
    assert rows(rig.conn) == [
        ("mic", "device_failed", 20_000, 50_000),
        ("all", "stopped", 50_000, None),
    ]


def test_drops_reported_after_the_stop_are_written_late_as_closed_rows(rig: Rig) -> None:
    rig.tick()
    rig.gaps.finish(50_000)
    rig.gaps.drop("system", "backlog", 48_000, 49_000)
    rig.gaps.flush_late()
    assert rows(rig.conn) == [
        ("all", "stopped", 50_000, None),
        ("system", "backlog", 48_000, 49_000),
    ]


def test_the_next_start_closes_the_stopped_row(conn: Connection, rig: Rig) -> None:
    rig.tick()
    rig.gaps.finish(50_000)
    again = Rig(conn, start_ms=60_000)
    again.tick()
    assert rows(conn) == [("all", "stopped", 50_000, 60_000)]  # and no crashed row
    assert not [r for r in rows(conn) if r[1] == "crashed"]


def test_finish_retries_recovery_and_writes_the_stop_when_it_succeeds(rig: Rig) -> None:
    rig.log.fail["recover"] = [busy()]
    rig.tick()  # recovery fails
    rig.gaps.finish(50_000)  # tries again, succeeds
    assert rows(rig.conn) == [("all", "stopped", 50_000, None)]


def test_if_recovery_still_fails_at_stop_nothing_is_flushed_and_no_stop_is_written(
    rig: Rig,
) -> None:
    rig.log.fail["recover"] = [busy()] * 5
    rig.paused.set()
    rig.tick()
    rig.gaps.drop("mic", "music", 1, 2)
    rig.gaps.finish(50_000)
    rig.gaps.flush_late()
    assert rows(rig.conn) == []


def test_events_still_pending_at_stop_mean_no_stopped_row(rig: Rig) -> None:
    rig.tick()
    rig.gaps.drop("mic", "music", 1, 2)
    rig.log.fail["add"] = [busy()]
    rig.gaps.finish(50_000)
    assert [r for r in rows(rig.conn) if r[1] == "stopped"] == []


def test_a_stop_that_was_not_written_is_recorded_as_a_crash_next_time(
    conn: Connection, rig: Rig
) -> None:
    rig.tick()
    rig.paused.set()
    rig.tick()
    rig.log.fail["stop"] = [busy()]
    with pytest.raises(sqlcipher3.OperationalError):
        rig.gaps.finish(50_000)
    again = Rig(conn, start_ms=60_000)
    again.tick()
    found = rows(conn)
    assert ("all", "crashed", 11_000, 60_000) in found  # the first heartbeat
    assert ("all", "paused", 12_000, 60_000) in found  # nothing is left open


# --- the stream recorder's reports ------------------------------------------------------------


class Spy:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def open(self, kind: str, reason: str, ts_ms: int) -> None:
        self.events.append(("open", kind, reason, ts_ms))

    def close(self, kind: str, ts_ms: int) -> None:
        self.events.append(("close", kind, ts_ms))

    def drop(
        self, kind: str, reason: str, start_ms: int, end_ms: int, *, merge: bool = False
    ) -> None:
        self.events.append(("drop", kind, reason, start_ms, end_ms, merge))


class Recorder:
    def __init__(
        self,
        kind: AudioKind = "mic",
        sink: Any = None,  # noqa: ANN401 - a spy or the real sink
        clock: Clock | None = None,
    ) -> None:
        self.devices = FakeDevices()
        self.clock = clock if clock is not None else Clock(T0)
        self.sink = sink if sink is not None else Spy()

        def discard(*_args: object) -> None:
            return None

        self.recorder = StreamRecorder(
            kind=kind,
            devices=self.devices,
            detector=FakeDetector(),
            settings=SETTINGS,
            sink=discard,
            clock_ms=self.clock,
            stats=Counter(),
            gaps=cast("Any", self.sink),
        )
        self.stop = threading.Event()
        self.paused = threading.Event()

    def step(self) -> None:
        self.recorder._iterate(self.stop, self.paused, self.paused)


def test_a_missing_device_opens_device_missing() -> None:
    r = Recorder()
    r.devices.devices["mic"] = None
    r.stop.set()
    r.step()
    assert r.sink.events == [("open", "mic", "device_missing", T0)]


def test_an_open_error_opens_device_failed() -> None:
    r = Recorder("system")
    r.devices.open_error = AudioError("busy")
    r.stop.set()
    r.step()
    assert r.sink.events == [("open", "system", "device_failed", T0)]


def test_a_successful_open_alone_closes_nothing_but_the_first_read_does() -> None:
    r = Recorder()
    r.devices.scripts["mic"].append([None])
    r.step()  # opens
    assert r.sink.events == []
    r.clock.now += 20
    r.step()  # the first read: a timeout None counts
    assert r.sink.events == [("close", "mic", T0 + 20)]


def test_a_read_error_opens_device_failed_and_releases_the_device() -> None:
    r = Recorder()
    r.devices.scripts["mic"].append([AudioError("unplugged")])
    r.step()
    r.step()
    assert r.sink.events == [("open", "mic", "device_failed", T0)]
    assert r.recorder._input is None


def test_a_loop_fault_opens_device_failed() -> None:
    r = Recorder()
    r.stop.set()
    r.recorder._recover_from_fault("RuntimeError", r.stop)
    assert r.sink.events == [("open", "mic", "device_failed", T0)]


def test_pausing_closes_the_streams_device_row() -> None:
    r = Recorder()
    r.paused.set()
    r.stop.set()
    r.step()
    assert r.sink.events == [("close", "mic", T0)]


def test_a_default_device_switch_that_reopens_fine_records_no_row() -> None:
    r = Recorder()
    r.devices.scripts["mic"].extend([[None], [None]])
    r.step()
    r.step()  # one successful read
    r.sink.events.clear()
    r.devices.devices["mic"] = AudioDevice("mic-2", "Other Mic")
    r.recorder._follow_default_device()  # releases for the reopen
    r.step()  # reopen
    assert [e for e in r.sink.events if e[0] == "open"] == []


def real_gaps(conn: Connection) -> tuple[CaptureGaps, Clock]:
    clock = Clock(T0)
    return CaptureGaps(GapLog(conn), clock, T0), clock


def flush(gaps: CaptureGaps) -> None:
    gaps._settle()
    gaps._flush(5.0)


def test_a_device_that_opens_but_fails_every_first_read_keeps_one_row(conn: Connection) -> None:
    gaps, clock = real_gaps(conn)
    r = Recorder(sink=gaps, clock=clock)
    r.devices.scripts["mic"].extend([AudioError("x")] for _ in range(6))
    for _ in range(12):  # open, fail, open, fail ...
        clock.now += 100
        r.step()
    flush(gaps)
    assert len(rows(conn)) == 1
    assert rows(conn)[0][:2] == ("mic", "device_failed")
    assert rows(conn)[0][3] is None


def test_the_first_good_read_closes_the_row_and_repeats_do_not_reopen(conn: Connection) -> None:
    gaps, clock = real_gaps(conn)
    r = Recorder(sink=gaps, clock=clock)
    r.devices.scripts["mic"].extend([[AudioError("x")], [None, None]])
    for _ in range(5):
        clock.now += 100
        r.step()
    flush(gaps)
    (row,) = rows(conn)
    assert row[:2] == ("mic", "device_failed")
    assert row[3] is not None
    assert row[3] >= row[2]


def test_a_change_of_reason_closes_the_old_row_and_opens_a_new_one(conn: Connection) -> None:
    gaps, clock = real_gaps(conn)
    r = Recorder(sink=gaps, clock=clock)
    r.stop.set()
    r.devices.devices["mic"] = None
    clock.now += 100
    r.step()  # device_missing
    r.step()  # again, same reason
    r.devices.devices["mic"] = AudioDevice("mic-1", "Fake Mic")
    r.devices.open_error = AudioError("busy")
    clock.now += 100
    r.step()  # device_failed
    flush(gaps)
    assert rows(conn) == [
        ("mic", "device_missing", T0 + 100, T0 + 200),
        ("mic", "device_failed", T0 + 200, None),
    ]


def test_the_two_streams_have_separate_device_rows(conn: Connection) -> None:
    gaps, _ = real_gaps(conn)
    mic, system = Recorder("mic", gaps), Recorder("system", gaps)
    mic.devices.devices["mic"] = None
    mic.stop.set()
    system.stop.set()
    system.devices.open_error = AudioError("x")
    mic.step()
    system.step()
    flush(gaps)
    assert {r[:2] for r in rows(conn)} == {("mic", "device_missing"), ("system", "device_failed")}


def test_pause_closes_the_device_row_for_real(conn: Connection) -> None:
    gaps, clock = real_gaps(conn)
    r = Recorder(sink=gaps, clock=clock)
    r.devices.devices["mic"] = None
    r.stop.set()
    r.step()
    r.paused.set()
    clock.now += 500
    r.step()
    flush(gaps)
    assert rows(conn) == [("mic", "device_missing", T0, T0 + 500)]


# --- the segment writer's drops ---------------------------------------------------------------


@pytest.fixture
def store(conn: Connection, media: MediaStore) -> AudioStore:
    return AudioStore(conn, media)


class WriterRig:
    def __init__(self, store: AudioStore, sink: Any) -> None:  # noqa: ANN401
        self.clock = Clock(T0)
        self.sessions = FakeSessions(frozenset())
        self.sampler = SourceSampler(self.sessions, self.clock)
        self.sink = sink
        self.writer = SegmentWriter(
            store,
            self.sampler,
            Exclusions(UserExclusions()),
            self.clock,
            Counter(),
            sink,
        )

    def write(
        self,
        start: int,
        audible: frozenset[str] | None,
        kind: AudioKind = "system",
        length: int = 2_000,
    ) -> int | None:
        self.sessions.names = audible
        self.clock.now = start + 500
        self.sampler.sample()
        utterance = Utterance(start, start + length, speech(60), 1_500, False)  # noqa: FBT003
        return self.writer.write(kind, utterance)


MUSIC = frozenset({"spotify.exe"})
VOICE = frozenset({"discord.exe"})


@pytest.mark.parametrize(
    ("audible", "reason"),
    [
        (frozenset({"keepassxc.exe", "discord.exe"}), "excluded"),
        (None, "unknown_sources"),
        (MUSIC, "music"),
    ],
)
def test_a_gated_drop_reports_its_stream_reason_and_span(
    store: AudioStore, audible: frozenset[str] | None, reason: str
) -> None:
    spy = Spy()
    w = WriterRig(store, spy)
    assert w.write(T0, audible) is None
    assert spy.events == [("drop", "system", reason, T0, T0 + 2_000, False)]


def test_a_kept_segment_reports_nothing(store: AudioStore) -> None:
    spy = Spy()
    assert WriterRig(store, spy).write(T0, VOICE) is not None
    assert spy.events == []


def test_a_full_writer_queue_reports_a_backlog_drop_that_never_merges(store: AudioStore) -> None:
    from akashic.capture.audio import WRITER_QUEUE  # noqa: PLC0415

    spy = Spy()
    w = WriterRig(store, spy)
    utterance = Utterance(T0, T0 + 1_000, speech(10), 800, False)  # noqa: FBT003
    for _ in range(WRITER_QUEUE + 2):
        w.writer.submit("mic", utterance)
    drops = [e for e in spy.events if e[0] == "drop"]
    assert len(drops) == 2
    assert all(d[2] == "backlog" and d[5] is False for d in drops)
    assert drops[0][:5] == ("drop", "mic", "backlog", T0, T0 + 1_000)


def test_a_write_that_fails_reports_write_failed(
    store: AudioStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = Spy()
    w = WriterRig(store, spy)

    def boom(*_a: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(w.writer, "_add_with_retry", boom)
    w.sessions.names = VOICE
    w.clock.now = T0 + 500
    w.sampler.sample()
    stop = threading.Event()
    stop.set()
    w.writer.submit("mic", Utterance(T0, T0 + 2_000, speech(60), 1_500, False))  # noqa: FBT003
    w.writer.run(stop)
    assert spy.events == [("drop", "mic", "write_failed", T0, T0 + 2_000, False)]


def test_repeated_same_reason_drops_ask_to_merge(store: AudioStore) -> None:
    spy = Spy()
    w = WriterRig(store, spy)
    for i in range(3):
        w.write(T0 + i * 5_000, MUSIC)
    assert [e[5] for e in spy.events] == [False, True, True]


def test_a_stored_segment_or_another_reason_breaks_the_chain(store: AudioStore) -> None:
    spy = Spy()
    w = WriterRig(store, spy)
    w.write(T0, MUSIC)
    w.write(T0 + 5_000, VOICE)
    w.write(T0 + 10_000, MUSIC)
    w.write(T0 + 15_000, None)
    w.write(T0 + 20_000, MUSIC)
    assert [e[5] for e in spy.events] == [False, False, False, False]


def test_the_chain_is_per_stream(store: AudioStore) -> None:
    spy = Spy()
    w = WriterRig(store, spy)
    w.write(T0, None, "mic")
    w.write(T0 + 5_000, None, "system")
    w.write(T0 + 10_000, None, "mic")
    assert [(e[1], e[5]) for e in spy.events] == [("mic", False), ("system", False), ("mic", True)]


def test_music_stored_music_gives_two_rows_and_leaves_the_segment_uncovered(
    conn: Connection, store: AudioStore
) -> None:
    gaps, _ = real_gaps(conn)
    w = WriterRig(store, gaps)
    w.write(T0, MUSIC)
    segment = w.write(T0 + 5_000, VOICE)
    w.write(T0 + 10_000, MUSIC)
    w.write(T0 + 15_000, MUSIC)
    assert segment is not None
    flush(gaps)
    assert rows(conn) == [
        ("system", "music", T0, T0 + 2_000),
        ("system", "music", T0 + 10_000, T0 + 17_000),
    ]
    segment_start = T0 + 5_000
    assert not any(s <= segment_start <= (e or 0) for _, _, s, e in rows(conn))


def test_a_long_music_stretch_is_one_row(conn: Connection, store: AudioStore) -> None:
    gaps, _ = real_gaps(conn)
    w = WriterRig(store, gaps)
    for i in range(200):
        w.write(T0 + i * 4_000, MUSIC)
    flush(gaps)
    assert rows(conn) == [("system", "music", T0, T0 + 199 * 4_000 + 2_000)]


def test_a_gap_longer_than_thirty_seconds_between_drops_starts_a_new_row(
    conn: Connection, store: AudioStore
) -> None:
    gaps, _ = real_gaps(conn)
    w = WriterRig(store, gaps)
    w.write(T0, MUSIC)
    w.write(T0 + 40_000, MUSIC)
    flush(gaps)
    assert len(rows(conn)) == 2


# --- audio off at start, OCR unavailable, the whole process ------------------------------------


def make_platform(*, ocr: FakeOcr | None, locked: bool = False) -> CapturePlatform:
    return fake_platform(ocr=ocr, locked=locked, text=LONG_TEXT[0].text)


class HeldStdin:
    """The parent's pipe: the key, then silence until released (EOF would stop the child)."""

    def __init__(self, key: DataKey) -> None:
        self._key = io.BytesIO(key.raw())
        self.release = threading.Event()

    def readinto(self, buffer: Any) -> int:  # noqa: ANN401
        return self._key.readinto(buffer)

    def readline(self, size: int = -1) -> bytes:
        del size
        self.release.wait(30)
        return b""


def run_capture(
    paths: Paths,
    key: DataKey,
    platform: CapturePlatform,
    *,
    paused: bool = True,
    seconds: float = 0.7,
    **kw: object,
) -> int:
    stdin = HeldStdin(key)

    def body(ctx: child.ChildContext) -> None:
        threading.Timer(seconds, ctx.stop.set).start()
        try:
            capture_main.run_capture(ctx, platform, **kw)  # type: ignore[arg-type]
        finally:
            stdin.release.set()

    argv = ["--data-dir", str(paths.root), *(["--paused"] if paused else [])]
    return child.run_child("capture", argv, body, cast("Any", stdin), io.BytesIO())


@pytest.fixture
def keyed(paths: Paths) -> DataKey:
    k = DataKey.generate()
    c = db.connect(paths.db_file, k.subkey("db"))
    db.migrate(c)
    c.close()
    return k


def read_rows(paths: Paths, key: DataKey) -> list[Row]:
    c = db.connect(paths.db_file, key.subkey("db"))
    try:
        return rows(c)
    finally:
        c.close()


def test_a_capture_that_starts_paused_records_pause_and_stop_and_the_next_start_closes_stopped(
    paths: Paths, keyed: DataKey
) -> None:
    assert run_capture(paths, keyed, make_platform(ocr=FakeOcr())) == child.EXIT_OK
    first = read_rows(paths, keyed)
    paused = [r for r in first if r[1] == "paused"]
    stopped = [r for r in first if r[1] == "stopped"]
    assert len(paused) == 1
    assert paused[0][3] is not None
    assert len(stopped) == 1
    assert stopped[0][3] is None
    assert paused[0][3] == stopped[0][2]  # closed at the time the stop began
    assert not [r for r in first if r[3] is None and r[1] != "stopped"]
    assert run_capture(paths, keyed, make_platform(ocr=FakeOcr())) == child.EXIT_OK
    second = read_rows(paths, keyed)
    assert second[0] == (*first[0][:3], first[0][3])
    old_stopped = next(r for r in second if r[1] == "stopped")
    assert old_stopped[3] is not None
    assert old_stopped[3] >= old_stopped[2]
    assert not [r for r in second if r[1] == "crashed"]


def test_ocr_unavailable_is_a_gap_for_the_life_of_the_process(paths: Paths, keyed: DataKey) -> None:
    assert run_capture(paths, keyed, make_platform(ocr=None), paused=False) == child.EXIT_OK
    found = [r for r in read_rows(paths, keyed) if r[1] == "ocr_unavailable"]
    assert len(found) == 1
    assert found[0][0] == "screen_text"
    assert found[0][3] is not None  # closed by the stop


def test_ocr_present_records_no_ocr_gap(paths: Paths, keyed: DataKey) -> None:
    run_capture(paths, keyed, make_platform(ocr=FakeOcr()), paused=False)
    assert [r for r in read_rows(paths, keyed) if r[1] == "ocr_unavailable"] == []


def test_a_lock_during_capture_records_a_locked_row(paths: Paths, keyed: DataKey) -> None:
    run_capture(paths, keyed, make_platform(ocr=FakeOcr(), locked=True), paused=False)
    assert [r[:2] for r in read_rows(paths, keyed) if r[1] == "locked"] == [("screen", "locked")]


def test_audio_that_cannot_be_built_opens_audio_unavailable_for_both_streams(
    paths: Paths, keyed: DataKey
) -> None:
    def broken() -> Any:  # noqa: ANN401
        raise FileNotFoundError

    run_capture(
        paths,
        keyed,
        make_platform(ocr=FakeOcr()),
        audio_platform=AudioPlatform(FakeDevices(), FakeSessions()),
        detector_factory=broken,
    )
    found = {r[:2] for r in read_rows(paths, keyed) if r[1] == "audio_unavailable"}
    assert found == {("mic", "audio_unavailable"), ("system", "audio_unavailable")}
    assert all(r[3] is not None for r in read_rows(paths, keyed) if r[1] == "audio_unavailable")


def test_audio_switched_off_opens_disabled_for_both_streams(paths: Paths, keyed: DataKey) -> None:
    (paths.root / "config.toml").write_text("[audio]\nenabled = false\n", encoding="utf-8")
    run_capture(
        paths,
        keyed,
        make_platform(ocr=FakeOcr()),
        audio_platform=AudioPlatform(FakeDevices(), FakeSessions()),
        detector_factory=FakeDetector,
    )
    found = {r[:2] for r in read_rows(paths, keyed) if r[1] == "disabled"}
    assert found == {("mic", "disabled"), ("system", "disabled")}
    assert [r for r in read_rows(paths, keyed) if r[1] == "audio_unavailable"] == []


def test_working_audio_records_no_availability_gap(paths: Paths, keyed: DataKey) -> None:
    run_capture(
        paths,
        keyed,
        make_platform(ocr=FakeOcr()),
        audio_platform=AudioPlatform(FakeDevices(), FakeSessions()),
        detector_factory=FakeDetector,
    )
    reasons = {r[1] for r in read_rows(paths, keyed)}
    assert not reasons & {"audio_unavailable", "disabled"}


def test_capture_debug_data_carries_the_gap_counters(paths: Paths, keyed: DataKey) -> None:
    from akashic.storage.debuginfo import ProcessInfo  # noqa: PLC0415

    run_capture(paths, keyed, make_platform(ocr=FakeOcr()), seconds=1.2)
    c = db.connect(paths.db_file, keyed.subkey("db"))
    (info,) = [i for i in ProcessInfo(c).read_all() if i.name == "capture"]
    c.close()
    assert isinstance(info.data["gaps"], dict)


# --- the OCR gate without an engine -----------------------------------------------------------


def test_the_gate_without_an_engine_returns_no_text_and_never_recognizes() -> None:
    gate = OcrGate(None)
    image = blank(size=(200, 100))
    result = gate.read("k", image)
    assert (result.text, result.ocr_ran) == ("", False)


def test_the_gate_with_an_engine_still_recognizes_text() -> None:
    from capture_fakes import with_text_block  # noqa: PLC0415

    image = with_text_block(blank())
    result = OcrGate(FakeOcr()).read("k", image)
    assert (result.text, result.ocr_ran) == ("recognized words", True)


# --- retention and Delete all -------------------------------------------------------------------

NOW = 20_400 * DAY_MS


def old(days: float) -> int:
    return NOW - int(days * DAY_MS)


ALL_TYPES: dict[str, int | None] = {
    "screenshots": 10,
    "frame_text": 20,
    "raw_audio": 30,
    "transcripts": 40,
    "media_sessions": 50,
    "app_sessions": 60,
    "episodes": 70,
    "embeddings": 80,
}


def test_gap_rows_expire_at_the_longest_limit_when_every_type_has_one(
    conn: Connection, media: MediaStore
) -> None:
    seed(
        conn,
        ("all", "paused", old(100), old(99)),  # past the longest limit (80 d)
        ("mic", "music", old(81), old(80.5)),  # also past
        ("mic", "music", old(79), old(78)),  # inside it
        ("all", "stopped", old(200), None),  # open: never deleted
    )
    removed = apply_retention(conn, media, ALL_TYPES, NOW)
    assert removed["capture_gaps"] == 2
    assert [r[2] for r in rows(conn)] == [old(79), old(200)]


def test_a_row_ending_exactly_at_the_cutoff_is_kept(conn: Connection, media: MediaStore) -> None:
    seed(conn, ("all", "paused", old(90), old(80)))
    apply_retention(conn, media, ALL_TYPES, NOW)
    assert len(rows(conn)) == 1


def test_a_row_that_spans_the_cutoff_is_kept(conn: Connection, media: MediaStore) -> None:
    seed(conn, ("all", "paused", old(90), old(70)))
    apply_retention(conn, media, ALL_TYPES, NOW)
    assert len(rows(conn)) == 1


@pytest.mark.parametrize("forever", list(ALL_TYPES))
def test_one_type_on_forever_keeps_every_gap_row(
    conn: Connection, media: MediaStore, forever: str
) -> None:
    seed(conn, ("all", "paused", old(1000), old(999)))
    limits = {**ALL_TYPES, forever: None}
    removed = apply_retention(conn, media, limits, NOW)
    assert "capture_gaps" not in removed
    assert len(rows(conn)) == 1


def test_the_default_forever_keeps_gap_rows(conn: Connection, media: MediaStore) -> None:
    seed(conn, ("all", "paused", old(1000), old(999)))
    apply_retention(conn, media, {}, NOW)
    assert len(rows(conn)) == 1


def test_retention_deletes_many_gap_rows_in_batches(conn: Connection, media: MediaStore) -> None:
    conn.executemany(
        "INSERT INTO capture_gaps (kind, reason, start_ms, end_ms) VALUES ('mic', 'music', ?, ?)",
        [(old(500) + i, old(500) + i) for i in range(2_500)],
    )
    assert apply_retention(conn, media, ALL_TYPES, NOW)["capture_gaps"] == 2_500
    assert rows(conn) == []


def test_delete_all_removes_gaps_and_the_heartbeat_so_a_restart_records_nothing(
    conn: Connection, media: MediaStore
) -> None:
    seed(conn, ("all", "stopped", 100, None), ("mic", "music", 1, 2))
    state.set_int(conn, state.CAPTURE_ALIVE, 500)
    delete_all_recorded_data(conn, media)
    assert rows(conn) == []
    assert state.get_int(conn, state.CAPTURE_ALIVE, 0) == 0
    GapLog(conn).recover(10_000)  # the restart
    assert rows(conn) == []


def test_delete_all_keeps_the_remembered_pause_but_not_the_heartbeat(
    conn: Connection, media: MediaStore
) -> None:
    state.set_int(conn, state.CAPTURE_PAUSED, 1)
    state.set_int(conn, state.CAPTURE_ALIVE, 500)
    delete_all_recorded_data(conn, media)
    assert state.get_int(conn, state.CAPTURE_PAUSED, 0) == 1
    assert state.get_int(conn, state.CAPTURE_ALIVE, 0) == 0
