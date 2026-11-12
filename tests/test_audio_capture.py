"""The audio recorder: timestamps, pause, device trouble, attribution, exclusions, storage."""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from audio_fakes import (
    SETTINGS,
    T0,
    Clock,
    FakeDetector,
    FakeDevices,
    FakeSessions,
    RecorderRig,
    silence,
    speech,
    wall_clock_ms,
)
from defects import known_defect
from sink_spy import SECRET, SinkSpy
from waits import wait_for

from akashic.capture import audio as audio_module
from akashic.capture.audio import (
    AudioCapture,
    SegmentWriter,
    SourceSampler,
    StreamRecorder,
    Utterance,
)
from akashic.capture.exclusions import Exclusions
from akashic.capture.ports import AudioError
from akashic.config import Exclusions as UserExclusions
from akashic.storage.audio import AudioStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    import numpy as np
    from numpy.typing import NDArray

    from akashic.capture.ports import AudioKind
    from akashic.config import Paths
    from akashic.storage.media import MediaStore

SECRET_PROCESS = f"{SECRET}.exe"


def script(*items: np.ndarray | AudioError | None) -> list[np.ndarray | AudioError | None]:
    return list(items)


@pytest.fixture
def store(conn: Connection, media: MediaStore) -> AudioStore:
    return AudioStore(conn, media)


# --- the run loop --------------------------------------------------------------------------


def run_in_thread(rig: RecorderRig) -> tuple[threading.Event, threading.Event, threading.Thread]:
    stop, paused = threading.Event(), threading.Event()
    thread = threading.Thread(target=rig.recorder.run, args=(stop, paused), daemon=True)
    thread.start()
    return stop, paused, thread


def test_pause_closes_the_device_and_drops_what_was_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_module, "READ_TIMEOUT_S", 0.01)
    rig = RecorderRig(realtime=True)
    rig.devices.scripts["mic"] += [script(speech(30)), script(speech(5), silence(30))]
    stop, paused, thread = run_in_thread(rig)
    assert wait_for(
        lambda: len(rig.devices.opened) == 1 and not rig.devices.opened[0][1].script, 15
    )
    paused.set()
    assert wait_for(lambda: rig.devices.opened[0][1].closed, 15)
    assert rig.out == []  # the half-recorded segment was dropped, not written
    paused.clear()
    assert wait_for(lambda: len(rig.devices.opened) == 2, 15)  # reopened after resume
    stop.set()
    thread.join(timeout=5)
    assert rig.detector.resets == 2


def test_device_failure_closes_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_module, "RETRY_S", 0.01)
    rig = RecorderRig(realtime=True)
    rig.devices.scripts["mic"] += [script(AudioError("gone")), script(speech(20), silence(20))]
    stop, _, thread = run_in_thread(rig)
    assert wait_for(lambda: len(rig.out) == 1, 15)
    stop.set()
    thread.join(timeout=5)
    assert rig.devices.opened[0][1].closed
    assert len(rig.devices.opened) == 2


# --- sources, writer, exclusions -----------------------------------------------------------


def test_sampler_unions_what_was_audible_during_the_span() -> None:
    clock = Clock(T0)
    sessions = FakeSessions(frozenset({"discord.exe"}))
    sampler = SourceSampler(sessions, clock, interval_s=1.0, keep_ms=10_000)
    sampler.sample()
    clock.now += 1_000
    sessions.names = frozenset({"chrome.exe", "discord.exe"})
    sampler.sample()
    clock.now += 5_000
    sessions.names = frozenset({"game.exe"})
    sampler.sample()
    assert sampler.between(T0, T0 + 1_000) == ("chrome.exe", "discord.exe")
    assert sampler.between(T0 + 6_000, T0 + 6_500) == ("game.exe",)
    assert sampler.between(T0 + 3_000, T0 + 3_500) == ()
    clock.now += 20_000
    sampler.sample()  # old samples are forgotten
    assert sampler.between(T0, T0 + 1_000) == ()


def _utterance(start: int = T0, *, continues: bool = False) -> Utterance:
    return Utterance(start, start + 2_000, speech(60), 1_500, continues)


def make_writer(
    store: AudioStore, sessions: FakeSessions, clock: Clock
) -> tuple[SegmentWriter, Counter[str], SourceSampler]:
    sampler = SourceSampler(sessions, clock)
    stats: Counter[str] = Counter()
    writer = SegmentWriter(store, sampler, Exclusions(UserExclusions()), clock, stats)
    return writer, stats, sampler


def test_writer_stores_segment_with_sources_and_queues_a_job(
    store: AudioStore, conn: Connection
) -> None:
    clock = Clock(T0 + 3_000)
    writer, stats, sampler = make_writer(store, FakeSessions(frozenset({"discord.exe"})), clock)
    sampler.sample()
    segment_id = writer.write("system", _utterance())
    assert segment_id is not None
    stored = store.get(segment_id)
    assert stored is not None
    assert (stored.stream, stored.sources, stored.start_ms) == ("system", ("discord.exe",), T0)
    assert stats["segments_system"] == 1
    assert conn.execute("SELECT kind, key FROM jobs").fetchall() == [
        ("transcribe", f"transcribe:{segment_id}")
    ]


def test_audio_while_an_excluded_process_is_heard_is_dropped_before_writing(
    store: AudioStore, conn: Connection, paths: Paths
) -> None:
    clock = Clock(T0 + 1_000)
    writer, stats, sampler = make_writer(
        store, FakeSessions(frozenset({"keepassxc.exe", "discord.exe"})), clock
    )
    sampler.sample()
    assert writer.write("system", _utterance()) is None
    assert stats["dropped_excluded"] == 1
    assert conn.execute("SELECT count(*) FROM audio_segments").fetchone() == (0,)
    assert list(paths.media_dir.rglob("*")) == [] or not list(paths.media_dir.rglob("*.enc"))


@pytest.mark.parametrize(
    ("kind", "audible", "kept"),
    [
        ("system", {"spotify.exe"}, False),  # only a music player: lyrics, not speech
        ("system", {"spotify.exe", "discord.exe"}, True),  # a call over the music
        ("system", {"zen.exe"}, True),  # browser music can't be told apart from a video
        ("mic", {"spotify.exe"}, True),  # the microphone is always the user
    ],
)
def test_music_only_system_sound_is_dropped(
    store: AudioStore, kind: AudioKind, audible: set[str], *, kept: bool
) -> None:
    utterance = _utterance()
    clock = Clock(utterance.start_ms)
    writer, stats, sampler = make_writer(store, FakeSessions(frozenset(audible)), clock)
    if kept:
        sampler.sample()
    else:
        for t in range(utterance.start_ms, utterance.end_ms + 1, 250):
            clock.now = t
            sampler.sample()
    assert (writer.write(kind, utterance) is not None) is kept
    assert stats["dropped_music"] == (0 if kept else 1)


def test_continuation_links_to_the_previous_segment_of_the_same_stream(store: AudioStore) -> None:
    clock = Clock(T0)
    writer, _, sampler = make_writer(store, FakeSessions(), clock)
    sampler.sample()
    first = writer.write("mic", _utterance(T0))
    other_stream = writer.write("system", _utterance(T0 + 1))
    clock.now += 2_000
    sampler.sample()
    second = writer.write("mic", _utterance(T0 + 1_500, continues=True))
    clock.now += 7_000
    sampler.sample()  # an utterance is only kept where the sources are known
    third = writer.write("system", _utterance(T0 + 9_000, continues=True))
    assert None not in (first, second, other_stream, third)
    assert store.get(second).continues_id == first  # type: ignore[union-attr]
    assert store.get(third).continues_id == other_stream  # type: ignore[union-attr]
    assert store.get(first).continues_id is None  # type: ignore[union-attr]


def test_a_dropped_segment_breaks_the_continuation_chain(store: AudioStore) -> None:
    clock = Clock(T0)
    sessions = FakeSessions()
    writer, _, sampler = make_writer(store, sessions, clock)
    sampler.sample()
    assert writer.write("mic", _utterance(T0)) is not None
    clock.now = T0 + 2_000
    sessions.names = frozenset({"keepass.exe"})
    sampler.sample()
    assert writer.write("mic", _utterance(T0 + 1_500)) is None
    sessions.names = frozenset()
    clock.now += 60_000
    sampler.sample()
    later = writer.write("mic", _utterance(T0 + 61_000, continues=True))
    assert later is not None
    assert store.get(later).continues_id is None  # type: ignore[union-attr]


# --- everything together -------------------------------------------------------------------


def test_audio_capture_records_both_streams_end_to_end(store: AudioStore, conn: Connection) -> None:
    devices = FakeDevices()
    devices.realtime = True
    devices.scripts["mic"].append([silence(10), speech(40), silence(30)])
    devices.scripts["system"].append([silence(10), speech(40), silence(30)])
    capture = AudioCapture(
        devices=devices,
        sessions=FakeSessions(frozenset({"discord.exe"})),
        detector_factory=FakeDetector,
        store=store,
        exclusions=Exclusions(UserExclusions()),
        settings=replace(SETTINGS, hangover_ms=320),
    )
    stop, paused = threading.Event(), threading.Event()
    thread = threading.Thread(target=capture.run, args=(stop, paused))
    thread.start()
    assert wait_for(
        lambda: conn.execute("SELECT count(*) FROM audio_segments").fetchone() == (2,), 30
    )
    stop.set()
    thread.join(timeout=30)
    assert not thread.is_alive()
    rows = conn.execute(
        "SELECT stream, sources, status FROM audio_segments ORDER BY stream"
    ).fetchall()
    assert rows == [
        ("mic", '["discord.exe"]', "pending"),
        ("system", '["discord.exe"]', "pending"),
    ]
    assert conn.execute("SELECT count(*) FROM jobs WHERE kind = 'transcribe'").fetchone() == (2,)


# --- unknown or excluded sources: nothing is stored, watched by the writer spy ---


class _Audio:
    """An audio store over the test database, watched by the writer spy."""

    def __init__(
        self, conn: Connection, media: MediaStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.conn = conn
        self.spy = SinkSpy(paths)
        self.spy.watch(conn)
        self.spy.install(monkeypatch)
        self.store = AudioStore(conn, media)
        self.stats: Counter[str] = Counter()

    def writer(
        self, sessions: FakeSessions, clock: Clock, user: UserExclusions | None = None
    ) -> tuple[SegmentWriter, SourceSampler]:
        sampler = SourceSampler(sessions, clock)
        exclusions = Exclusions(user or UserExclusions())
        return SegmentWriter(self.store, sampler, exclusions, clock, self.stats), sampler

    def segments(self) -> int:
        return self.conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0]


@pytest.fixture
def audio(
    conn: Connection, media: MediaStore, paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Audio]:
    rig = _Audio(conn, media, paths, monkeypatch)
    yield rig
    rig.spy.close()


def test_the_sampler_marks_a_failed_poll_and_an_empty_span_as_incomplete() -> None:
    clock = Clock(T0)
    sessions = FakeSessions(frozenset({"discord.exe"}))
    sampler = SourceSampler(sessions, clock, interval_s=1.0)
    assert sampler.check(T0, T0 + 500) == ((), False)  # nobody looked: unknown, not silence
    sampler.sample()
    assert sampler.check(T0, T0 + 500) == (("discord.exe",), True)
    clock.now += 1_000
    sessions.names = None
    sampler.sample()
    assert sampler.check(T0, T0 + 1_500)[1] is False  # one failed poll inside the span
    assert sampler.check(T0 - 3_000, T0 - 2_500)[1] is False  # before the first poll
    sessions.names = frozenset()
    clock.now += 5_000
    sampler.sample()
    assert sampler.check(T0 + 5_500, T0 + 6_000) == ((), True)  # silence that was observed


def test_unknown_sources_are_dropped_in_memory_not_stored_as_clean(audio: _Audio) -> None:
    clock = Clock(T0 + 1_000)
    sessions = FakeSessions(error=OSError(f"audio session enumeration failed {SECRET}"))
    writer, sampler = audio.writer(sessions, clock)
    sampler.sample()  # raises inside; the sampler records "unknown"
    assert writer.write("system", _utterance()) is None
    assert audio.stats["dropped_unknown_sources"] == 1
    assert audio.spy.nothing_stored(audio.conn)
    assert audio.spy.leaks(audio.conn, SECRET) == []  # not even the error text reaches a log


@pytest.mark.parametrize(
    "sessions",
    [FakeSessions(None), FakeSessions(error=OSError("x"))],
    ids=["returns-none", "raises"],
)
def test_a_stream_keeps_running_but_persists_nothing_while_sources_are_unknown(
    audio: _Audio, sessions: FakeSessions
) -> None:
    devices = FakeDevices()
    devices.realtime = True
    devices.scripts["mic"].append([silence(10), speech(40), silence(30)])
    devices.scripts["system"].append([silence(10), speech(40), silence(30)])
    capture = AudioCapture(
        devices=devices,
        sessions=sessions,
        detector_factory=FakeDetector,
        store=audio.store,
        exclusions=Exclusions(UserExclusions()),
        settings=replace(SETTINGS, hangover_ms=320),
    )
    stop, paused = threading.Event(), threading.Event()
    thread = threading.Thread(target=capture.run, args=(stop, paused))
    thread.start()
    reached = wait_for(lambda: capture.stats["dropped_unknown_sources"] >= 2, 30)
    stop.set()
    thread.join(timeout=30)
    assert reached
    assert capture.stats["dropped_unknown_sources"] == 2  # both streams heard speech, both dropped
    assert audio.spy.nothing_stored(audio.conn)


def test_an_excluded_source_is_dropped_even_when_another_poll_failed(audio: _Audio) -> None:
    clock = Clock(T0 + 1_000)
    sessions = FakeSessions(frozenset({"keepassxc.exe"}))
    writer, sampler = audio.writer(sessions, clock)
    sampler.sample()
    sessions.names = None
    clock.now += 500
    sampler.sample()
    assert writer.write("system", _utterance()) is None
    assert audio.stats["dropped_excluded"] == 1  # the known reason wins; nothing is stored
    assert audio.spy.nothing_stored(audio.conn)


def test_user_excluded_process_names_are_dropped_before_any_write(audio: _Audio) -> None:
    clock = Clock(T0 + 1_000)
    writer, sampler = audio.writer(
        FakeSessions(frozenset({SECRET_PROCESS, "discord.exe"})),
        clock,
        UserExclusions(processes=(SECRET_PROCESS,)),
    )
    sampler.sample()
    assert writer.write("system", _utterance()) is None
    assert audio.spy.nothing_stored(audio.conn)
    assert audio.spy.leaks(audio.conn, SECRET) == []


def test_a_clean_observed_span_is_still_stored(audio: _Audio) -> None:
    """The unknown rule is not a blanket: a poll that failed far from the span does not matter."""
    clock = Clock(T0 - 60_000)
    sessions = FakeSessions(None)
    writer, sampler = audio.writer(sessions, clock)
    sampler.sample()  # failed a minute ago
    clock.now = T0 + 1_000
    sessions.names = frozenset({"discord.exe"})
    sampler.sample()
    assert writer.write("system", _utterance()) is not None
    assert audio.segments() == 1


def test_negative_control_ignoring_failed_polls_stores_the_segment(
    audio: _Audio, monkeypatch: pytest.MonkeyPatch
) -> None:
    def old_check(
        sampler: SourceSampler, start_ms: int, end_ms: int
    ) -> tuple[tuple[str, ...], bool]:
        del sampler, start_ms, end_ms
        return (), True  # a failed poll looked like silence

    monkeypatch.setattr(SourceSampler, "check", old_check)
    clock = Clock(T0 + 1_000)
    writer, sampler = audio.writer(FakeSessions(None), clock)
    sampler.sample()
    assert writer.write("system", _utterance()) is not None
    assert audio.segments() == 1
    assert not audio.spy.nothing_stored(audio.conn)


# --- a non-audio exception does not kill the recorder ---------------------------


class _FlakyDetector(FakeDetector):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def probability(self, window: NDArray[np.float32]) -> float:
        if not self.failed:
            self.failed = True
            msg = "onnxruntime error"
            raise RuntimeError(msg)
        return super().probability(window)


@known_defect(23, "a non-AudioError exception silently kills a stream's recorder thread")
def test_audio_recorder_survives_a_detector_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audio_module, "READ_TIMEOUT_S", 0.01)
    monkeypatch.setattr(audio_module, "RETRY_S", 0.01)
    devices = FakeDevices()
    for _ in range(3):  # one per (re)opening of the device
        block: list[NDArray[np.float32] | AudioError | None] = [speech(20), silence(20)]
        devices.scripts["mic"].append(block)
    out: list[tuple[AudioKind, Utterance]] = []
    recorder = StreamRecorder(
        kind="mic",
        devices=devices,
        detector=_FlakyDetector(),
        settings=SETTINGS,
        sink=lambda kind, u: out.append((kind, u)),
        clock_ms=wall_clock_ms,
        stats=Counter(),
    )
    stop, paused = threading.Event(), threading.Event()
    died: list[BaseException] = []

    def body() -> None:
        try:
            recorder.run(stop, paused)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
            died.append(exc)

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    try:
        assert wait_for(lambda: bool(out) or bool(died), 10)
        assert died == []
        assert out
    finally:
        stop.set()
        thread.join(timeout=10)
