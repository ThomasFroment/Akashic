"""The bounded capture stop: a held write lock can delay it, never block it past the deadline."""

from __future__ import annotations

import io
import logging
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import sqlcipher3
from audio_fakes import DEVICES, T0, Clock, FakeDetector, FakeDevices, FakeSessions, speech
from capture_fakes import FakeMedia, FakeOcr
from screen_rig import fake_platform
from test_capture_gaps import HeldStdin, make_platform

from akashic import child, supervisor
from akashic.capture import gaps
from akashic.capture import main as capture_main
from akashic.capture.audio import SegmentWriter, SourceSampler, Utterance
from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.media import MediaTracker
from akashic.capture.ports import AudioPlatform, MediaState
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.audio import AudioStore
from akashic.storage.sessions import MediaLog

if TYPE_CHECKING:
    from sqlite3 import Connection

    import pytest

    from akashic.capture.ports import AudioKind, CapturePlatform
    from akashic.config import Paths
    from akashic.storage.crypto import DataKey
    from akashic.storage.gaps import GapKind, GapReason
    from akashic.storage.media import MediaStore

BUDGET_S = 1.5  # stands in for STOP_BUDGET_S so the held-lock runs stay short
PRIVATE = "secret-title-9731"


def busy_timeout(conn: Connection) -> int:
    return conn.execute("PRAGMA busy_timeout").fetchone()[0]


# --- the deadline and the connection limit -----------------------------------------------------


def test_the_deadline_has_time_left_before_it_and_none_after_it() -> None:
    ahead = capture_main.StopDeadline(time.monotonic() + 60)
    assert 0 < ahead.remaining() <= 60
    assert capture_main.StopDeadline(time.monotonic() - 5).remaining() == 0.0


def test_the_stop_budget_is_below_the_supervisors_kill() -> None:
    assert capture_main.STOP_BUDGET_S == 8.0
    assert capture_main.STOP_BUDGET_S < supervisor.STOP_TIMEOUT_S


def test_limit_wait_sets_the_busy_timeout_of_that_connection_only(
    conn: Connection, paths: Paths, key: DataKey
) -> None:
    other = db.connect(paths.db_file, key.subkey("db"))
    try:
        assert busy_timeout(conn) == db.BUSY_TIMEOUT_MS
        db.limit_wait(conn, 123)
        assert busy_timeout(conn) == 123
        assert busy_timeout(other) == db.BUSY_TIMEOUT_MS
    finally:
        other.close()


def test_the_stop_busy_timeout_is_250_ms(conn: Connection) -> None:
    assert gaps.STOP_BUSY_TIMEOUT_MS == 250
    gaps.limit_wait_at_stop(conn)
    assert busy_timeout(conn) == 250


def test_a_failed_limit_is_logged_by_exception_type_only(
    conn: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    conn.close()
    with caplog.at_level(logging.WARNING):
        gaps.limit_wait_at_stop(conn)
    assert "ProgrammingError" in caplog.text


def test_the_media_thread_limits_its_wait_before_closing_at_stop(conn: Connection) -> None:
    tracker = MediaTracker(
        source=FakeMedia(),
        log_store=MediaLog(conn),
        exclusions=Exclusions(UserExclusions()),
        focused_window=lambda: None,
        now_ms=lambda: T0,
        browsers=BrowserState(),
    )
    stop = threading.Event()
    stop.set()
    tracker.run(stop, threading.Event(), lambda: False)
    assert busy_timeout(conn) == gaps.STOP_BUSY_TIMEOUT_MS


# --- the audio writer at stop ------------------------------------------------------------------


class BusyStore(AudioStore):
    """Every write meets a held lock (or fails with ``error`` when one is given)."""

    def __init__(self, conn: Connection, media: MediaStore, error: Exception | None = None) -> None:
        super().__init__(conn, media)
        self.error = error or sqlite3.OperationalError("database is locked")
        self.calls = 0

    def add(self, segment: Any, opus: bytes, now_ms: int) -> int:  # noqa: ANN401
        del segment, opus, now_ms
        self.calls += 1
        raise self.error


class DropSpy:
    def __init__(self) -> None:
        self.drops: list[tuple[str, str, int, int]] = []

    def open(self, kind: GapKind, reason: GapReason, ts_ms: int) -> None:
        pass

    def close(self, kind: GapKind, ts_ms: int) -> None:
        pass

    def drop(
        self, kind: GapKind, reason: GapReason, start_ms: int, end_ms: int, *, merge: bool = False
    ) -> None:
        del merge
        self.drops.append((kind, reason, start_ms, end_ms))


def utt(start: int) -> Utterance:
    return Utterance(start, start + 1_280, speech(40), 1_280, continues=False)


class AudioRig:
    def __init__(self, store: AudioStore) -> None:
        clock = Clock(T0)
        sampler = SourceSampler(
            FakeSessions(frozenset({"discord.exe"})), clock, Exclusions(UserExclusions())
        )
        for offset in range(0, 20_000, 500):  # audible all along
            clock.now = T0 + offset
            sampler.sample()
        self.stats: Counter[str] = Counter()
        self.spy = DropSpy()
        self.writer = SegmentWriter(
            store, sampler, Exclusions(UserExclusions()), clock, self.stats, self.spy
        )

    def queue(self, count: int, kind: AudioKind = "system") -> None:
        for i in range(count):
            self.writer.submit(kind, utt(T0 + i * 2_000), DEVICES[kind])

    def run_stopped(self) -> None:
        stop = threading.Event()
        stop.set()
        self.writer.run(stop)


def test_the_audio_writer_stores_everything_queued_at_stop_when_the_database_is_free(
    conn: Connection, media: MediaStore, caplog: pytest.LogCaptureFixture
) -> None:
    rig = AudioRig(AudioStore(conn, media))
    rig.queue(3)
    with caplog.at_level(logging.WARNING):
        rig.run_stopped()
    assert conn.execute("SELECT count(*) FROM audio_segments").fetchone() == (3,)
    assert rig.stats["stop_dropped"] == 0
    assert rig.spy.drops == []
    assert "dropped" not in caplog.text
    assert busy_timeout(conn) == gaps.STOP_BUSY_TIMEOUT_MS


def test_the_first_busy_segment_at_stop_drops_every_segment_still_queued(
    conn: Connection, media: MediaStore, caplog: pytest.LogCaptureFixture
) -> None:
    store = BusyStore(conn, media)
    rig = AudioRig(store)
    rig.queue(4)
    with caplog.at_level(logging.WARNING):
        rig.run_stopped()
    assert store.calls == 1  # no second write after the first busy one
    assert rig.stats["write_failed"] == 1
    assert rig.stats["stop_dropped"] == 3
    assert [d[:2] for d in rig.spy.drops] == [("system", "write_failed")] * 4
    assert [d[2] for d in rig.spy.drops] == [T0 + i * 2_000 for i in range(4)]
    assert caplog.text.count("dropped 3 queued") == 1


def test_a_write_failing_for_another_reason_at_stop_does_not_drop_the_queue(
    conn: Connection, media: MediaStore
) -> None:
    store = BusyStore(conn, media, sqlcipher3.IntegrityError("x"))
    rig = AudioRig(store)
    rig.queue(3)
    rig.run_stopped()
    assert store.calls == 3
    assert rig.stats["write_failed"] == 3
    assert rig.stats["stop_dropped"] == 0


def test_a_busy_write_before_the_stop_does_not_drop_the_queue(
    conn: Connection, media: MediaStore
) -> None:
    store = BusyStore(conn, media)
    rig = AudioRig(store)
    rig.queue(1)
    stop = threading.Event()
    thread = threading.Thread(target=rig.writer.run, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while store.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert rig.stats["stop_dropped"] == 0


# --- the whole process --------------------------------------------------------------------------


def run_once(  # noqa: PLR0913 - a scenario's knobs
    paths: Paths,
    key: DataKey,
    platform: CapturePlatform,
    *,
    lock: bool = False,
    release_after: float | None = None,
    seconds: float = 0.7,
    **kw: object,
) -> float:
    """Run capture, request the stop after ``seconds`` and return how long it took to return. With
    ``lock`` another connection holds the write lock from just before the stop: until
    ``release_after`` seconds later, or until capture has returned."""
    stdin = HeldStdin(key)
    started = [0.0]
    elapsed: list[float] = []
    holder_lock = threading.Lock()
    holders: list[Connection] = []

    def release() -> None:
        with holder_lock:
            while holders:
                holder = holders.pop()
                holder.execute("ROLLBACK")
                holder.close()

    def stop_now(ctx: child.ChildContext) -> None:
        if lock:
            holder = db.connect(paths.db_file, key.subkey("db"))
            holder.execute("BEGIN IMMEDIATE")
            holders.append(holder)
            if release_after is not None:
                threading.Timer(release_after, release).start()
        started[0] = time.monotonic()
        ctx.stop.set()

    def body(ctx: child.ChildContext) -> None:
        threading.Timer(seconds, stop_now, args=(ctx,)).start()
        try:
            capture_main.run_capture(ctx, platform, **kw)  # type: ignore[arg-type]
            elapsed.append(time.monotonic() - started[0])
        finally:
            release()
            stdin.release.set()

    argv = ["--data-dir", str(paths.root)]
    code = child.run_child("capture", argv, body, cast("Any", stdin), io.BytesIO())
    assert code == child.EXIT_OK
    return elapsed[0]


def gap_rows(conn: Connection) -> list[tuple[str, str, int, int | None]]:
    return [
        (r[0], r[1], r[2], r[3])
        for r in conn.execute("SELECT kind, reason, start_ms, end_ms FROM capture_gaps ORDER BY id")
    ]


def test_every_thread_run_capture_starts_is_a_daemon(
    paths: Paths, key: DataKey, conn: Connection
) -> None:
    del conn  # migrates the database
    seen: dict[str, bool] = {}
    before: set[threading.Thread] = set()

    class Probe(FakeMedia):
        def sessions(self) -> list[MediaState]:
            if not seen:  # the media thread polls right after the others have started
                time.sleep(0.4)
                for t in threading.enumerate():
                    if t not in before and not isinstance(t, threading.Timer):
                        seen[t.name] = t.daemon
            return super().sessions()

    platform = make_platform(ocr=FakeOcr())
    platform = replace(platform, media=Probe())
    before.update(threading.enumerate())
    run_once(
        paths,
        key,
        platform,
        audio_platform=AudioPlatform(FakeDevices(), FakeSessions()),
        detector_factory=FakeDetector,
    )
    expected = {
        "audio",
        "audio-sources",
        "audio-mic",
        "audio-system",
        "audio-writer",
        "screen-writer",
        "media",
        "gaps",
        "debug-info",
    }
    assert expected <= seen.keys()
    assert [name for name, daemon in seen.items() if not daemon] == []


def test_a_stop_with_no_lock_held_is_prompt_and_written(
    paths: Paths, key: DataKey, conn: Connection
) -> None:
    elapsed = run_once(paths, key, make_platform(ocr=FakeOcr()))
    assert elapsed < 4.0
    stopped = [r for r in gap_rows(conn) if r[1] == "stopped"]
    assert [(r[0], r[3]) for r in stopped] == [("all", None)]


def test_a_lock_freed_within_the_deadline_still_gives_a_clean_stop(
    paths: Paths, key: DataKey, conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_main, "STOP_BUDGET_S", BUDGET_S)
    run_once(paths, key, make_platform(ocr=FakeOcr()), lock=True, release_after=0.5)
    assert [(r[0], r[3]) for r in gap_rows(conn) if r[1] == "stopped"] == [("all", None)]
    run_once(paths, key, make_platform(ocr=FakeOcr()))  # the next start
    found = gap_rows(conn)
    assert not [r for r in found if r[1] == "crashed"]
    assert [r for r in found if r[1] == "stopped" and r[3] is not None]


def test_a_lock_held_past_the_deadline_returns_in_time_and_the_next_start_records_a_crash(
    paths: Paths, key: DataKey, conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capture_main, "STOP_BUDGET_S", BUDGET_S)
    platform = make_platform(ocr=FakeOcr())
    platform = replace(
        platform,
        media=FakeMedia(states=[MediaState("Z1", "Video", "Chan", "playing")], names={"Z1": "Zen"}),
    )
    elapsed = run_once(paths, key, platform, lock=True)
    assert BUDGET_S <= elapsed < BUDGET_S + 3.0
    assert [r for r in gap_rows(conn) if r[1] == "stopped"] == []
    assert conn.execute("SELECT count(*) FROM media_sessions WHERE closed = 0").fetchone()[0] == 1
    run_once(paths, key, make_platform(ocr=FakeOcr()))  # the next start
    found = gap_rows(conn)
    assert [r[:2] for r in found if r[1] == "crashed"] == [("all", "crashed")]
    assert not [r for r in found if r[3] is None and r[1] != "stopped"]
    assert conn.execute("SELECT count(*) FROM media_sessions WHERE closed = 0").fetchone()[0] == 0


def test_the_stop_logs_only_names_counts_and_exception_types(
    paths: Paths,
    key: DataKey,
    conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    del conn  # migrates the database
    monkeypatch.setattr(capture_main, "STOP_BUDGET_S", BUDGET_S)
    platform = fake_platform(ocr=FakeOcr(), text=PRIVATE * 10)
    with caplog.at_level(logging.INFO):
        run_once(paths, key, platform, lock=True)
    assert PRIVATE not in caplog.text
