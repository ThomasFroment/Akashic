"""Audit #161: a busy database does not lose an audio segment or leave a media row open.

The audio writer retries the same segment with backoff; the media tracker keeps a session whose
close failed as pending and retries the close at every poll. Other errors are not retried.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest
import sqlcipher3
from audio_fakes import Clock, FakeSessions, speech
from capture_fakes import FakeMedia
from waits import wait_for

from akashic.capture import audio as audio_module
from akashic.capture.audio import SegmentWriter, SourceSampler, Utterance
from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.media import PAUSE_CLOSE_MS, MediaTracker
from akashic.capture.ports import MediaState
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.audio import AudioStore, NewSegment
from akashic.storage.sessions import MediaLog

if TYPE_CHECKING:
    from sqlite3 import Connection

    import numpy as np

    from akashic.config import Paths
    from akashic.storage.media import MediaStore

T0 = 1_794_000_000_000
SECRET_TITLE = "Secret Title 4711"  # noqa: S105 - a marker that must never reach a log


def op_error(cls: type[Exception], code: int | None, msg: str = "database is locked") -> Exception:
    exc = cls(msg)
    if code is not None:
        exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    return exc


BUSY = [
    pytest.param(sqlite3.OperationalError, None, id="stdlib-nocode"),
    pytest.param(sqlcipher3.OperationalError, None, id="sqlcipher-nocode"),
    pytest.param(sqlite3.OperationalError, 5, id="stdlib-busy"),
    pytest.param(sqlcipher3.OperationalError, 5, id="sqlcipher-busy"),
    pytest.param(sqlite3.OperationalError, 6, id="stdlib-locked"),
    pytest.param(sqlcipher3.OperationalError, 6, id="sqlcipher-locked"),
    pytest.param(sqlite3.OperationalError, 5 | (1 << 8), id="busy-recovery"),
    pytest.param(sqlite3.OperationalError, 6 | (1 << 8), id="locked-sharedcache"),
    pytest.param(sqlcipher3.OperationalError, 5 | (2 << 8), id="busy-snapshot"),
]
NOT_BUSY_CODES = [
    pytest.param(sqlite3.OperationalError, 10, id="ioerr"),
    pytest.param(sqlcipher3.OperationalError, 10 | (1 << 8), id="ioerr-read"),
    pytest.param(sqlite3.OperationalError, 13, id="full"),
    pytest.param(sqlite3.OperationalError, 8, id="readonly"),
    pytest.param(sqlite3.OperationalError, 14, id="cantopen"),
    pytest.param(sqlite3.OperationalError, 1, id="error"),
    pytest.param(sqlcipher3.OperationalError, 1, id="sqlcipher-error"),
]


def test_is_busy_classifies_errors() -> None:
    assert db.is_busy(op_error(sqlite3.OperationalError, 5))
    assert not db.is_busy(op_error(sqlite3.OperationalError, 10))
    assert not db.is_busy(ValueError("x"))
    assert not db.is_busy(sqlite3.IntegrityError("x"))
    assert not db.is_busy(OSError("x"))


# --- audio -----------------------------------------------------------------------------------


class ScriptedStore(AudioStore):
    """Raises the scripted errors, one per ``add`` call, then writes for real."""

    def __init__(self, conn: Connection, media: MediaStore, errors: list[Exception]) -> None:
        super().__init__(conn, media)
        self.errors = errors
        self.calls = 0

    def add(self, segment: NewSegment, opus: bytes, now_ms: int) -> int:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return super().add(segment, opus, now_ms)


class AlwaysBusyStore(AudioStore):
    def __init__(self, conn: Connection, media: MediaStore, exc: Exception) -> None:
        super().__init__(conn, media)
        self.exc = exc
        self.calls = 0

    def add(self, segment: NewSegment, opus: bytes, now_ms: int) -> int:
        del segment, opus, now_ms
        self.calls += 1
        raise self.exc


def utt(start: int = T0, *, continues: bool = False) -> Utterance:
    return Utterance(start, start + 1_280, speech(40), 1_280, continues)


class WriterRig:
    def __init__(self, store: AudioStore) -> None:
        self.clock = Clock(T0)
        self.sampler = SourceSampler(FakeSessions(frozenset({"discord.exe"})), self.clock)
        for offset in range(0, 6_000, 500):  # audible all along
            self.clock.now = T0 + offset
            self.sampler.sample()
        self.stats: Counter[str] = Counter()
        self.writer = SegmentWriter(
            store, self.sampler, Exclusions(UserExclusions()), self.clock, self.stats
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.writer.run, args=(self.stop,), daemon=True)

    def last_id(self, kind: str) -> int | None:
        return self.writer._last_id.get(kind)

    def finish(self, timeout: float = 15) -> None:
        self.stop.set()
        self.thread.join(timeout=timeout)
        assert not self.thread.is_alive()


@pytest.fixture
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_module, "WRITE_RETRY_S", (0.01,) * 5)


def counts(conn: Connection) -> tuple[int, int]:
    return (
        conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0],
        conn.execute("SELECT count(*) FROM jobs WHERE kind = 'transcribe'").fetchone()[0],
    )


def enc_files(paths: Paths) -> list[str]:
    return [str(p) for p in paths.media_dir.rglob("*.opus.enc")]


@pytest.mark.usefixtures("fast_retry")
@pytest.mark.parametrize(("cls", "code"), BUSY)
def test_busy_audio_write_is_retried_and_stored_once(
    cls: type[Exception], code: int | None, conn: Connection, media: MediaStore, paths: Paths
) -> None:
    store = ScriptedStore(conn, media, [op_error(cls, code), op_error(cls, code)])
    rig = WriterRig(store)
    rig.writer.submit("system", utt())
    rig.thread.start()
    assert wait_for(lambda: counts(conn) == (1, 1), 5)
    rig.finish()
    assert counts(conn) == (1, 1)
    assert len(enc_files(paths)) == 1
    assert rig.stats["write_failed"] == 0
    assert rig.stats["write_retried"] == 2
    assert rig.stats["segments_system"] == 1
    assert store.calls == 3


@pytest.mark.usefixtures("fast_retry")
@pytest.mark.parametrize(
    "exc",
    [
        *[pytest.param(op_error(p.values[0], p.values[1]), id=p.id) for p in NOT_BUSY_CODES],
        pytest.param(sqlite3.IntegrityError("constraint"), id="integrity"),
        pytest.param(ValueError("boom"), id="valueerror"),
        pytest.param(OSError("disk"), id="oserror"),
        pytest.param(RuntimeError("x"), id="runtime"),
    ],
)
def test_other_audio_errors_fail_at_once(
    exc: Exception, conn: Connection, media: MediaStore
) -> None:
    store = ScriptedStore(conn, media, [exc])
    rig = WriterRig(store)
    rig.writer._last_id["system"] = 7
    rig.writer.submit("system", utt())
    rig.thread.start()
    assert wait_for(lambda: rig.stats["write_failed"] == 1, 5)
    rig.finish()
    assert store.calls == 1
    assert rig.stats["write_retried"] == 0
    assert rig.last_id("system") is None
    assert counts(conn) == (0, 0)


@pytest.mark.usefixtures("fast_retry")
def test_a_retried_write_keeps_the_continuation_chain(conn: Connection, media: MediaStore) -> None:
    store = ScriptedStore(conn, media, [])
    rig = WriterRig(store)
    first = rig.writer.write("system", utt(T0))
    store.errors.append(op_error(sqlite3.OperationalError, 5))
    second = rig.writer.write("system", utt(T0 + 1_300, continues=True))
    assert rig.stats["write_retried"] == 1
    assert rig.last_id("system") == second
    third = rig.writer.write("system", utt(T0 + 2_600, continues=True))
    rows = dict(conn.execute("SELECT id, continues_id FROM audio_segments").fetchall())
    assert rows == {first: None, second: first, third: second}
    assert rig.stats["write_failed"] == 0


@pytest.mark.usefixtures("fast_retry")
def test_a_continuation_after_a_dropped_segment_has_no_parent(
    conn: Connection, media: MediaStore
) -> None:
    store = ScriptedStore(conn, media, [])
    rig = WriterRig(store)
    rig.writer.write("system", utt(T0))
    store.errors.extend(op_error(sqlite3.OperationalError, 5) for _ in range(6))
    rig.writer.submit("system", utt(T0 + 1_300, continues=True))
    rig.thread.start()
    assert wait_for(lambda: rig.stats["write_failed"] == 1, 5)
    rig.writer.submit("system", utt(T0 + 2_600, continues=True))
    assert wait_for(lambda: counts(conn)[0] == 2, 5)
    rig.finish()
    assert conn.execute(
        "SELECT continues_id FROM audio_segments ORDER BY start_ms DESC LIMIT 1"
    ).fetchone() == (None,)


@pytest.mark.usefixtures("fast_retry")
def test_every_attempt_failing_drops_the_segment_and_logs_the_type_only(
    conn: Connection, media: MediaStore, caplog: pytest.LogCaptureFixture
) -> None:
    secret = f"locked {SECRET_TITLE}"
    store = AlwaysBusyStore(conn, media, op_error(sqlite3.OperationalError, 5, secret))
    rig = WriterRig(store)
    rig.writer._last_id["system"] = 3
    caplog.set_level(logging.DEBUG)
    rig.writer.submit("system", utt())
    rig.thread.start()
    assert wait_for(lambda: rig.stats["write_failed"] == 1, 5)
    rig.finish()
    assert store.calls == 6  # first attempt + one per retry delay
    assert rig.stats["write_failed"] == 1
    assert rig.stats["write_retried"] == 5
    assert rig.stats["segments_system"] == 0
    assert rig.last_id("system") is None
    assert counts(conn) == (0, 0)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("OperationalError" in m for m in warnings)
    assert not any(SECRET_TITLE in m or "locked" in m for m in warnings)


def test_a_database_that_stays_busy_leaves_no_orphan_file(
    conn: Connection, media: MediaStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``AudioStore.add`` over a transaction that always fails busy."""
    monkeypatch.setattr(audio_module, "WRITE_RETRY_S", (0.01, 0.01))
    store = AudioStore(conn, media)

    def fail(*_a: object, **_k: object) -> object:
        raise op_error(sqlite3.OperationalError, 5)

    monkeypatch.setattr("akashic.storage.audio.db.transaction", fail)
    rig = WriterRig(store)
    rig.writer.submit("system", utt())
    rig.thread.start()
    assert wait_for(lambda: rig.stats["write_failed"] == 1, 5)
    rig.finish()
    assert enc_files(paths) == []


def test_the_first_retry_comes_within_one_second(conn: Connection, media: MediaStore) -> None:
    assert audio_module.WRITE_RETRY_S[0] <= 1.0
    store = ScriptedStore(conn, media, [op_error(sqlite3.OperationalError, 5)])
    rig = WriterRig(store)
    rig.writer.submit("system", utt())
    started = time.monotonic()
    rig.thread.start()
    assert wait_for(lambda: counts(conn) == (1, 1), 5)
    elapsed = time.monotonic() - started
    rig.finish()
    assert elapsed < 1.5


def test_once_stopping_a_busy_segment_gets_no_retry_and_the_next_is_written(
    conn: Connection, media: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audio_module, "WRITE_RETRY_S", (30.0, 30.0))  # would hang if waited
    store = ScriptedStore(conn, media, [op_error(sqlite3.OperationalError, 5)])
    rig = WriterRig(store)
    rig.writer.submit("system", utt(T0))
    rig.writer.submit("system", utt(T0 + 1_300))
    rig.stop.set()
    started = time.monotonic()
    rig.thread.start()
    rig.thread.join(timeout=5)
    assert not rig.thread.is_alive()
    assert time.monotonic() - started < 3
    assert rig.stats["write_failed"] == 1
    assert rig.stats["write_retried"] == 0
    assert store.calls == 2
    assert counts(conn) == (1, 1)


def test_stop_during_a_retry_wait_ends_the_wait(
    conn: Connection, media: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audio_module, "WRITE_RETRY_S", (30.0, 30.0))
    store = ScriptedStore(conn, media, [op_error(sqlite3.OperationalError, 5)] * 3)
    rig = WriterRig(store)
    rig.writer.submit("system", utt())
    rig.thread.start()
    assert wait_for(lambda: store.calls >= 1, 5)
    time.sleep(0.1)
    started = time.monotonic()
    rig.stop.set()
    rig.thread.join(timeout=5)
    assert not rig.thread.is_alive()
    assert time.monotonic() - started < 2
    assert rig.stats["write_failed"] == 1
    assert store.calls == 1


@pytest.mark.usefixtures("fast_retry")
def test_checks_and_encoding_run_once_per_segment(
    conn: Connection, media: MediaStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ScriptedStore(conn, media, [op_error(sqlite3.OperationalError, 5)] * 3)
    rig = WriterRig(store)
    checks: list[int] = []
    encodes: list[int] = []
    real_check = rig.sampler.check
    real_encode = audio_module.encode_opus

    def check(start: int, end: int) -> tuple[tuple[str, ...], bool]:
        checks.append(start)
        return real_check(start, end)

    def encode(samples: np.ndarray) -> bytes:
        encodes.append(1)
        return real_encode(samples)

    monkeypatch.setattr(rig.sampler, "check", check)
    monkeypatch.setattr(audio_module, "encode_opus", encode)
    rig.writer.write("system", utt())
    assert (len(checks), len(encodes), store.calls) == (1, 1, 4)
    assert counts(conn) == (1, 1)


# --- media -----------------------------------------------------------------------------------


class ScriptedLog(MediaLog):
    """A media log whose calls are recorded and whose close / open / close_stale can fail."""

    def __init__(self, conn: Connection) -> None:
        super().__init__(conn)
        self.close_failures = 0
        self.close_fail_ids: set[int] | None = None
        self.open_failures = 0
        self.stale_failures = 0
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def open(  # noqa: PLR0913
        self, *, app_id: str, app: str, title: str, artist: str | None, ts_ms: int, state: str
    ) -> int:
        self.calls.append(("open", (title,)))
        if self.open_failures:
            self.open_failures -= 1
            raise op_error(sqlite3.OperationalError, 5)
        return super().open(
            app_id=app_id, app=app, title=title, artist=artist, ts_ms=ts_ms, state=state
        )

    def close(self, session_id: int, *, end_ms: int, played_ms: int) -> None:
        self.calls.append(("close", (session_id, end_ms, played_ms)))
        if self.close_failures and (
            self.close_fail_ids is None or session_id in self.close_fail_ids
        ):
            self.close_failures -= 1
            raise op_error(sqlite3.OperationalError, 5, f"locked {SECRET_TITLE}")
        super().close(session_id, end_ms=end_ms, played_ms=played_ms)

    def touch(self, session_id: int, *, end_ms: int, played_ms: int) -> None:
        self.calls.append(("touch", (session_id,)))
        super().touch(session_id, end_ms=end_ms, played_ms=played_ms)

    def add_event(self, session_id: int, ts_ms: int, state: str) -> None:
        self.calls.append(("event", (session_id, state)))
        super().add_event(session_id, ts_ms, state)

    def set_url(self, session_id: int, url: str) -> None:
        self.calls.append(("url", (session_id,)))
        super().set_url(session_id, url)

    def close_stale(self) -> int:
        self.calls.append(("stale", ()))
        if self.stale_failures:
            self.stale_failures -= 1
            raise op_error(sqlite3.OperationalError, 5, f"locked {SECRET_TITLE}")
        return super().close_stale()


class CountingMedia(FakeMedia):
    polls: int = 0

    def sessions(self) -> list[MediaState]:
        self.polls += 1
        return super().sessions()


class MediaRig:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.clock = [0]
        self.source = CountingMedia(names={"Z1": "Zen", "C1": "Google Chrome"})
        self.log = ScriptedLog(conn)
        self.tracker = MediaTracker(
            source=self.source,
            log_store=self.log,
            exclusions=Exclusions(UserExclusions()),
            focused_window=lambda: None,
            now_ms=lambda: self.clock[0],
            interval_s=0.01,
            browsers=BrowserState(),
        )

    def at(self, seconds: float) -> None:
        self.clock[0] = int(seconds * 1000)
        self.tracker.poll()

    def rows(self) -> list[tuple[Any, ...]]:
        """(id, title, start_ms, end_ms, played_ms, closed)"""
        return self.conn.execute(
            "SELECT id, title, start_ms, end_ms, played_ms, closed FROM media_sessions ORDER BY id"
        ).fetchall()

    def events(self) -> list[tuple[int, int, str]]:
        return self.conn.execute(
            "SELECT media_session_id, ts_ms, state FROM media_events ORDER BY id"
        ).fetchall()

    def call_names(self) -> list[str]:
        return [name for name, _ in self.log.calls]


def playing(title: str = "Video A", app_id: str = "Z1") -> MediaState:
    return MediaState(app_id, title, "Chan", "playing")


@pytest.fixture
def mrig(conn: Connection) -> MediaRig:
    return MediaRig(conn)


def test_a_failed_close_is_retried_with_the_first_attempts_times(mrig: MediaRig) -> None:
    mrig.source.states = [playing()]
    mrig.at(1)
    mrig.at(3)
    mrig.source.states = []
    mrig.log.close_failures = 2
    mrig.at(5)  # gone: ended at 3 s; the close fails
    assert mrig.rows()[0][5] == 0
    assert mrig.tracker.stats["close_failed"] == 1
    mrig.at(20)  # the retry fails again, long after
    assert mrig.tracker.stats["close_failed"] == 2
    mrig.at(40)
    assert mrig.rows() == [(1, "Video A", 1000, 3000, 2000, 1)]
    assert mrig.events() == [(1, 1000, "playing"), (1, 3000, "stopped")]


def test_a_pending_session_gets_nothing_but_close_attempts(mrig: MediaRig) -> None:
    mrig.source.states = [playing()]
    mrig.at(1)
    mrig.at(3)
    mrig.source.states = []
    mrig.log.close_failures = 3
    mrig.at(4)  # fails
    mrig.log.calls.clear()
    mrig.source.states = [playing()]  # back with the same title
    mrig.at(5)  # retry fails
    mrig.source.states = [MediaState("Z1", "Video A", "Chan", "paused")]
    mrig.at(6)  # retry fails
    assert set(mrig.call_names()) == {"close"}
    mrig.source.states = [MediaState("Z1", "Video A", "Chan", "paused")]
    mrig.at(7)  # retry succeeds, nothing else happens
    assert set(mrig.call_names()) == {"close"}
    assert mrig.rows() == [(1, "Video A", 1000, 3000, 2000, 1)]
    states = [s for sid, _, s in mrig.events() if sid == 1]
    assert states == ["playing", "stopped"]


def test_a_title_change_with_a_failing_close_opens_nothing_until_it_closes(
    mrig: MediaRig,
) -> None:
    mrig.source.states = [playing("Video A")]
    mrig.at(1)
    mrig.at(3)
    mrig.source.states = [playing("Video B")]
    mrig.log.close_failures = 2
    mrig.at(5)  # close of A fails
    mrig.at(7)  # still fails
    assert [(r[1], r[5]) for r in mrig.rows()] == [("Video A", 0)]
    mrig.at(9)  # close succeeds, B opens at this very poll
    rows = mrig.rows()
    assert [(r[1], r[5]) for r in rows] == [("Video A", 1), ("Video B", 0)]
    assert rows[1][2] == 9000
    assert [(sid, s) for sid, _, s in mrig.events()] == [
        (1, "playing"),
        (1, "stopped"),
        (2, "playing"),
    ]


def test_the_old_title_returning_gets_no_events_on_the_pending_row(mrig: MediaRig) -> None:
    mrig.source.states = [playing("Video A")]
    mrig.at(1)
    mrig.source.states = [playing("Video B")]
    mrig.log.close_failures = 1
    mrig.at(3)  # A's close fails
    mrig.log.calls.clear()
    mrig.source.states = [playing("Video A")]
    mrig.at(5)  # A's close succeeds; the source plays a different title (A again): a new row
    assert mrig.call_names()[0] == "close"
    assert [r[5] for r in mrig.rows()] == [1, 0]
    assert [s for sid, _, s in mrig.events() if sid == 1] == ["playing", "stopped"]


def test_close_all_tries_each_session_and_logs_no_title(
    mrig: MediaRig, caplog: pytest.LogCaptureFixture
) -> None:
    mrig.source.states = [playing("Video A", "Z1"), playing(SECRET_TITLE, "C1")]
    mrig.at(1)
    mrig.at(2)
    mrig.log.close_failures = 1
    mrig.log.close_fail_ids = {1}
    caplog.set_level(logging.DEBUG)
    mrig.clock[0] = 3000
    mrig.tracker.close_all()
    assert {r[1]: r[5] for r in mrig.rows()} == {"Video A": 0, SECRET_TITLE: 1}
    assert mrig.tracker.stats["close_failed"] == 1
    assert SECRET_TITLE not in caplog.text
    assert "locked" not in caplog.text
    mrig.clock[0] = 9000
    mrig.tracker.close_all()  # the pending one gets another attempt
    assert [r[5] for r in mrig.rows()] == [1, 1]
    assert mrig.rows()[0][3] == 3000  # end fixed at the first attempt
    assert [s for _, _, s in mrig.events()].count("stopped") == 2


def test_one_failing_close_does_not_stop_others_in_the_same_poll(mrig: MediaRig) -> None:
    mrig.source.states = [playing("Video A", "Z1"), playing("Video C", "C1")]
    mrig.at(1)
    mrig.at(2)
    mrig.source.states = []
    mrig.log.close_failures = 1
    mrig.log.close_fail_ids = {1}
    mrig.at(4)
    assert [(r[1], r[5]) for r in mrig.rows()] == [("Video A", 0), ("Video C", 1)]
    mrig.at(6)
    assert [r[5] for r in mrig.rows()] == [1, 1]


def test_a_long_pause_close_that_fails_is_retried(mrig: MediaRig) -> None:
    mrig.source.states = [playing()]
    mrig.at(1)
    mrig.source.states = [MediaState("Z1", "Video A", "Chan", "paused")]
    mrig.at(2)
    mrig.log.close_failures = 1
    late = 2 + PAUSE_CLOSE_MS / 1000
    mrig.at(late + 1)
    assert mrig.rows()[0][5] == 0
    mrig.at(late + 3)
    assert mrig.rows()[0][5] == 1
    assert [s for _, _, s in mrig.events()].count("stopped") == 1


def test_a_failed_open_leaves_no_row_behind(mrig: MediaRig) -> None:
    mrig.source.states = [playing()]
    mrig.log.open_failures = 1
    with pytest.raises(sqlite3.OperationalError):
        mrig.tracker.poll()
    assert mrig.rows() == []
    assert mrig.events() == []
    mrig.at(2)
    assert len(mrig.rows()) == 1
    assert [s for _, _, s in mrig.events()] == ["playing"]


def test_a_failing_first_event_rolls_the_row_back(conn: Connection) -> None:
    class BadEvent(MediaLog):
        @staticmethod
        def _insert_event(conn: Connection, session_id: int, ts_ms: int, state: str) -> None:
            del conn, session_id, ts_ms, state
            raise op_error(sqlite3.OperationalError, 5)

    with pytest.raises(sqlite3.OperationalError):
        BadEvent(conn).open(
            app_id="Z1", app="Zen", title="t", artist=None, ts_ms=1, state="playing"
        )
    assert conn.execute("SELECT count(*) FROM media_sessions").fetchone() == (0,)


def test_a_failing_stopped_event_leaves_the_row_open(conn: Connection) -> None:
    class BadStop(MediaLog):
        @staticmethod
        def _insert_event(conn: Connection, session_id: int, ts_ms: int, state: str) -> None:
            if state == "stopped":
                raise op_error(sqlite3.OperationalError, 5)
            MediaLog._insert_event(conn, session_id, ts_ms, state)

    log = BadStop(conn)
    sid = log.open(app_id="Z1", app="Zen", title="t", artist=None, ts_ms=1, state="playing")
    with pytest.raises(sqlite3.OperationalError):
        log.close(sid, end_ms=5, played_ms=4)
    assert conn.execute("SELECT closed, end_ms, played_ms FROM media_sessions").fetchone() == (
        0,
        1,
        0,
    )


def run_tracker(rig: MediaRig, stop: threading.Event) -> tuple[threading.Thread, list[Any]]:
    errors: list[Any] = []

    def target() -> None:
        try:
            rig.tracker.run(stop, threading.Event(), lambda: False)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, errors


def test_a_failing_close_stale_neither_kills_the_thread_nor_polls(mrig: MediaRig) -> None:
    mrig.log.stale_failures = 3
    mrig.source.states = [playing()]
    stop = threading.Event()
    thread, errors = run_tracker(mrig, stop)
    assert wait_for(lambda: mrig.source.polls > 0, 5)
    assert thread.is_alive()
    names = mrig.call_names()
    assert names.count("stale") == 4  # three failures, then the success, no more
    assert names.index("open") > max(i for i, n in enumerate(names) if n == "stale")
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert mrig.tracker.stats["close_stale_failed"] == 3


def test_stop_while_close_stale_keeps_failing_returns_promptly(
    conn: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    rig = MediaRig(conn)
    rig.log.stale_failures = 10**6
    caplog.set_level(logging.DEBUG)
    stop = threading.Event()
    thread, errors = run_tracker(rig, stop)
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert rig.source.polls == 0
    assert SECRET_TITLE not in caplog.text


def test_close_all_at_stop_failing_is_not_raised_out_of_run(mrig: MediaRig) -> None:
    mrig.source.states = [playing()]
    stop = threading.Event()
    thread, errors = run_tracker(mrig, stop)
    assert wait_for(lambda: len(mrig.rows()) == 1, 5)
    mrig.log.close_failures = 10**6
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert mrig.rows()[0][5] == 0
    assert MediaLog(mrig.conn).close_stale() == 1  # the next start closes it
    assert mrig.rows()[0][5] == 1


def test_close_all_is_one_attempt_per_session(mrig: MediaRig) -> None:
    mrig.source.states = [playing("A", "Z1"), playing("B", "C1")]
    mrig.at(1)
    mrig.log.close_failures = 10**6
    mrig.log.calls.clear()
    mrig.tracker.close_all()
    assert mrig.call_names().count("close") == 2


def test_close_all_retries_a_pending_close(mrig: MediaRig) -> None:
    mrig.source.states = [playing()]
    mrig.at(1)
    mrig.log.close_failures = 1
    mrig.tracker.close_all()
    assert mrig.rows()[0][5] == 0
    mrig.tracker.close_all()
    assert mrig.rows()[0][5] == 1
    assert [s for _, _, s in mrig.events()] == ["playing", "stopped"]
