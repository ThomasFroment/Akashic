"""Which playback endpoint a sound was heard on: per-endpoint source checks, the recorded device
carried to the writer, and voice chat that plays on a device that is not recorded."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING, Any

import pytest
from audio_fakes import DEVICES, SYSTEM, T0, Clock, FakeSessions, RecorderRig, silence, speech
from test_capture_gaps import flush, real_gaps, rows

from akashic.capture.audio import (
    MIXER_PROCESSES,
    AudioCapture,
    SegmentWriter,
    SourceCheck,
    SourceSampler,
    Utterance,
)
from akashic.capture.exclusions import Exclusions
from akashic.capture.ports import AudioDevice, AudioError
from akashic.config import Exclusions as UserExclusions
from akashic.storage.audio import AudioStore

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.storage.media import MediaStore

A, B = "spk-1", "spk-2"  # A is the recorded fake system device
MIXER = "elgato.wavelink.exe"
type Heard = dict[str, frozenset[str]] | frozenset[str] | None


@pytest.fixture
def store(conn: Connection, media: MediaStore) -> AudioStore:
    return AudioStore(conn, media)


def polls(a: set[str] | None = None, b: set[str] | None = None) -> dict[str, frozenset[str]]:
    return {A: frozenset(a or ()), B: frozenset(b or ())}


class Drops:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def open(self, kind: str, reason: str, ts_ms: int) -> None:
        pass

    def close(self, kind: str, ts_ms: int) -> None:
        pass

    def drop(
        self, kind: str, reason: str, start_ms: int, end_ms: int, *, merge: bool = False
    ) -> None:
        self.events.append((kind, reason, start_ms, end_ms, merge))


class WriterRig:
    """One poll at T0 + 500, then a 2 s utterance from T0."""

    def __init__(
        self,
        store: AudioStore,
        heard: Heard,
        user: UserExclusions | None = None,
    ) -> None:
        self.store = store
        self.clock = Clock(T0 + 500)
        self.sessions = FakeSessions(heard)
        self.sampler = SourceSampler(self.sessions, self.clock, Exclusions(UserExclusions()))
        self.stats: Counter[str] = Counter()
        self.writer = SegmentWriter(
            store, self.sampler, Exclusions(user or UserExclusions()), self.clock, self.stats
        )
        self.sampler.sample()

    def write(self, kind: str = "system") -> int | None:
        utterance = Utterance(T0, T0 + 2_000, speech(60), 1_500, continues=False)
        return self.writer.write(kind, utterance, DEVICES[kind])  # type: ignore[arg-type]

    def sources(self, kind: str = "system") -> tuple[str, ...] | None:
        segment_id = self.write(kind)
        if segment_id is None:
            return None
        stored = self.store.get(segment_id)
        assert stored is not None
        return stored.sources


# --- the per-endpoint check ------------------------------------------------------------------


def test_check_splits_the_recorded_endpoint_from_everywhere() -> None:
    sessions = FakeSessions(polls({"game.exe"}, {"discord.exe"}))
    sampler = SourceSampler(sessions, Clock(T0), Exclusions(UserExclusions()))
    sampler.sample()
    assert sampler.check(T0, T0 + 500, A) == SourceCheck(
        ("game.exe",), ("discord.exe", "game.exe"), complete=True
    )


def test_the_microphone_check_has_no_recorded_endpoint_and_never_misses_one() -> None:
    sampler = SourceSampler(
        FakeSessions({B: frozenset({"discord.exe"})}), Clock(T0), Exclusions(UserExclusions())
    )
    sampler.sample()
    assert sampler.check(T0, T0 + 500, None) == SourceCheck((), ("discord.exe",), complete=True)
    assert not sampler.check(T0, T0 + 500, A).complete


def test_a_poll_without_the_endpoint_makes_the_span_incomplete() -> None:
    clock = Clock(T0)
    sessions = FakeSessions(polls())
    sampler = SourceSampler(sessions, clock, Exclusions(UserExclusions()))
    sampler.sample()
    clock.now += 500
    sessions.names = {B: frozenset()}
    sampler.sample()
    assert not sampler.check(T0, T0 + 600, A).complete


# --- refresh ---------------------------------------------------------------------------------


def test_a_check_that_misses_its_endpoint_refreshes_exactly_the_next_poll() -> None:
    clock = Clock(T0)
    sessions = FakeSessions({B: frozenset()})
    sampler = SourceSampler(sessions, clock, Exclusions(UserExclusions()))
    sampler.sample()
    assert not sampler.check(T0, T0 + 500, A).complete
    sessions.names = polls()
    for _ in range(3):
        clock.now += 1_000
        sampler.sample()
    assert sessions.refreshes == [False, True, False, False]


def test_a_poll_missing_the_recorded_endpoint_refreshes_exactly_the_next_poll() -> None:
    clock = Clock(T0)
    sessions = FakeSessions({B: frozenset()})
    sampler = SourceSampler(sessions, clock, Exclusions(UserExclusions()))
    sampler.set_recorded(A)
    sampler.sample()
    sessions.names = polls()
    for _ in range(3):
        clock.now += 1_000
        sampler.sample()
    assert sessions.refreshes == [False, True, False, False]


def test_a_missing_endpoint_that_is_not_recorded_asks_for_no_refresh() -> None:
    sessions = FakeSessions({B: frozenset()})
    sampler = SourceSampler(sessions, Clock(T0), Exclusions(UserExclusions()))
    sampler.sample()
    sampler.sample()
    assert sessions.refreshes == [False, False]


# --- the recorder tells the sampler what it records ------------------------------------------


def _system_rig() -> tuple[RecorderRig, list[str | None]]:
    seen: list[str | None] = []
    rig = RecorderRig(kind="system", on_device=seen.append)
    return rig, seen


def test_the_system_recorder_sets_the_endpoint_on_acquire_and_clears_it_on_release() -> None:
    rig, seen = _system_rig()
    assert rig.recorder._acquire()
    assert seen == [A]
    rig.recorder._release(keep=False)
    assert seen == [A, None]


def test_a_device_failure_and_a_device_change_clear_the_endpoint() -> None:
    rig, seen = _system_rig()
    assert rig.recorder._acquire()
    rig.devices.opened[0][1].script.append(AudioError("unplugged"))
    rig.recorder._step()
    assert seen == [A, None]
    assert rig.recorder._acquire()
    rig.devices.devices["system"] = AudioDevice(B, "Other")
    rig.recorder._follow_default_device()
    assert seen == [A, None, A, None]
    assert rig.recorder._acquire()
    assert seen[-1] == B


def test_a_halted_recorder_clears_the_endpoint() -> None:
    rig, seen = _system_rig()
    assert rig.recorder._acquire()
    halt = threading.Event()
    halt.set()
    rig.recorder._iterate(threading.Event(), halt, threading.Event())
    assert seen == [A, None]


def test_only_the_system_recorder_of_the_capture_tells_the_sampler(store: AudioStore) -> None:
    from audio_fakes import FakeDetector, FakeDevices  # noqa: PLC0415

    capture = AudioCapture(
        devices=FakeDevices(),
        sessions=FakeSessions(),
        detector_factory=FakeDetector,
        store=store,
        exclusions=Exclusions(UserExclusions()),
        settings=RecorderRig().recorder._settings,
    )
    mic, system = capture._recorders
    assert mic._acquire()
    assert capture._sampler._recorded is None
    assert system._acquire()
    assert capture._sampler._recorded == A
    system._release(keep=False)
    assert capture._sampler._recorded is None


# --- utterances carry their device -----------------------------------------------------------


def test_an_utterance_closed_by_silence_carries_its_device() -> None:
    rig = RecorderRig(kind="system")
    assert rig.recorder._acquire()
    rig.deliver(speech(30))
    rig.deliver(silence(30))
    assert rig.out_devices == [SYSTEM]


def test_an_utterance_closed_early_by_a_device_change_carries_the_old_device() -> None:
    rig = RecorderRig(kind="system")
    assert rig.recorder._acquire()
    rig.deliver(speech(30))
    rig.devices.devices["system"] = AudioDevice(B, "Other")
    rig.recorder._follow_default_device()
    assert rig.out_devices == [SYSTEM]


def test_an_utterance_closed_by_a_device_failure_or_a_stop_carries_its_device() -> None:
    rig = RecorderRig(kind="mic")
    assert rig.recorder._acquire()
    rig.deliver(speech(30))
    rig.devices.opened[0][1].script.append(AudioError("unplugged"))
    rig.recorder._step()
    assert rig.out_devices == [DEVICES["mic"]]
    assert rig.recorder._acquire()
    rig.deliver(speech(30))
    stop = threading.Event()
    stop.set()
    rig.recorder.run(stop, threading.Event(), threading.Event())
    assert rig.out_devices == [DEVICES["mic"]] * 2


def test_the_writer_checks_system_segments_with_the_device_and_mic_segments_with_none(
    store: AudioStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = WriterRig(store, polls())
    asked: list[str | None] = []
    real = rig.sampler.check

    def spy(start: int, end: int, endpoint_id: str | None) -> SourceCheck:
        asked.append(endpoint_id)
        return real(start, end, endpoint_id)

    monkeypatch.setattr(rig.sampler, "check", spy)
    rig.write("system")
    rig.write("mic")
    assert asked == [A, None]


# --- what a segment is attributed to ---------------------------------------------------------


def test_audit_case_the_voice_app_on_another_endpoint_is_not_a_source(store: AudioStore) -> None:
    rig = WriterRig(store, polls({"game.exe"}, {"discord.exe"}))
    assert rig.sources() == ("game.exe",)


def test_mixer_case_a_mixer_on_the_recorded_endpoint_falls_back_to_everywhere(
    store: AudioStore,
) -> None:
    rig = WriterRig(store, polls({MIXER}, {"discord.exe"}))
    assert rig.sources() == ("discord.exe",)


def test_music_case_a_mixer_makes_the_music_on_another_endpoint_count(store: AudioStore) -> None:
    rig = WriterRig(store, polls({MIXER}, {"spotify.exe"}))
    assert rig.sources() is None
    assert rig.stats["dropped_music"] == 1


def test_music_on_another_endpoint_does_not_drop_the_recorded_voice(store: AudioStore) -> None:
    rig = WriterRig(store, polls({"discord.exe"}, {"spotify.exe"}))
    assert rig.sources() == ("discord.exe",)


def test_mixer_names_are_never_stored(store: AudioStore) -> None:
    assert MIXER in MIXER_PROCESSES
    assert WriterRig(store, polls({MIXER, "game.exe"})).sources() == ("game.exe",)
    assert WriterRig(store, polls({MIXER}, {"game.exe"})).sources() == ("game.exe",)
    assert WriterRig(store, polls({MIXER}, {"game.exe"})).sources("mic") == ("game.exe",)


def test_a_microphone_segment_stores_what_was_audible_everywhere(store: AudioStore) -> None:
    rig = WriterRig(store, polls({"game.exe"}, {"discord.exe"}))
    assert rig.sources("mic") == ("discord.exe", "game.exe")


def test_an_excluded_process_on_another_endpoint_drops_the_segment(
    store: AudioStore, conn: Connection
) -> None:
    rig = WriterRig(store, polls({"game.exe"}, {"keepassxc.exe"}))
    assert rig.write() is None
    assert rig.stats["dropped_excluded"] == 1
    assert conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0] == 0


def test_the_exclusion_check_runs_before_the_unknown_check(store: AudioStore) -> None:
    rig = WriterRig(store, {B: frozenset({"keepassxc.exe"})})  # no entry for the recorded A
    assert rig.write() is None
    assert (rig.stats["dropped_excluded"], rig.stats["dropped_unknown_sources"]) == (1, 0)


def test_a_missing_recorded_endpoint_drops_the_segment_as_unknown(
    store: AudioStore, conn: Connection
) -> None:
    rig = WriterRig(store, {B: frozenset({"game.exe"})})
    assert rig.write() is None
    assert rig.stats["dropped_unknown_sources"] == 1
    assert conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0] == 0


# --- voice chat on a device that is not recorded ---------------------------------------------


VOICE_ELSEWHERE = polls({"game.exe"}, {"discord.exe"})


class Stretch:
    def __init__(
        self,
        heard: Any = VOICE_ELSEWHERE,  # noqa: ANN401
        *,
        recorded: str | None = A,
        user: UserExclusions | None = None,
    ) -> None:
        self.clock = Clock(T0)
        self.sessions = FakeSessions(heard)
        self.drops = Drops()
        self.sampler = SourceSampler(
            self.sessions, self.clock, Exclusions(user or UserExclusions()), gaps=self.drops
        )
        self.sampler.set_recorded(recorded)

    def poll(self, heard: Any = ..., *, advance: int = 1_000) -> None:  # noqa: ANN401
        self.clock.now += advance
        if heard is not ...:
            self.sessions.names = heard
        self.sampler.sample()


def test_a_stretch_starts_one_interval_back_and_continues_from_the_previous_poll() -> None:
    s = Stretch()
    s.poll(advance=0)
    s.poll()
    s.poll()
    assert s.drops.events == [
        ("system", "voice_elsewhere", T0 - 1_000, T0, False),
        ("system", "voice_elsewhere", T0, T0 + 1_000, True),
        ("system", "voice_elsewhere", T0 + 1_000, T0 + 2_000, True),
    ]


@pytest.mark.parametrize(
    ("heard", "recorded"),
    [
        pytest.param(VOICE_ELSEWHERE, None, id="no endpoint recorded"),
        pytest.param(None, A, id="the poll failed"),
        pytest.param({B: frozenset({"discord.exe"})}, A, id="recorded endpoint missing"),
        pytest.param(polls({MIXER}, {"discord.exe"}), A, id="mixer on the recorded endpoint"),
        pytest.param(polls({"discord.exe"}, {"discord.exe"}), A, id="also on the recorded one"),
    ],
)
def test_no_voice_elsewhere_is_reported(heard: Any, recorded: str | None) -> None:  # noqa: ANN401
    s = Stretch(heard, recorded=recorded)
    s.poll()
    s.poll()
    assert s.drops.events == []


def test_voice_elsewhere_at_is_none_until_a_poll_finds_voice_elsewhere() -> None:
    s = Stretch(polls({"game.exe"}))
    s.poll()
    assert s.sampler.voice_elsewhere_at() is None
    before = time.monotonic()
    s.poll(VOICE_ELSEWHERE)
    first = s.sampler.voice_elsewhere_at()
    assert first is not None
    assert before <= first <= time.monotonic()
    time.sleep(0.01)
    s.poll()
    second = s.sampler.voice_elsewhere_at()
    assert second is not None
    assert second > first


@pytest.mark.parametrize(
    ("heard", "recorded"),
    [
        pytest.param(VOICE_ELSEWHERE, None, id="no endpoint recorded"),
        pytest.param(polls({MIXER}, {"discord.exe"}), A, id="mixer on the recorded endpoint"),
    ],
)
def test_voice_elsewhere_at_ignores_polls_that_do_not_count(
    heard: Any,  # noqa: ANN401
    recorded: str | None,
) -> None:
    s = Stretch(heard, recorded=recorded)
    s.poll()
    assert s.sampler.voice_elsewhere_at() is None


def test_voice_elsewhere_at_keeps_its_value_over_quiet_and_failed_polls() -> None:
    s = Stretch()
    s.poll()
    seen = s.sampler.voice_elsewhere_at()
    assert seen is not None
    s.poll(polls({"game.exe"}))
    s.poll(None)
    assert s.sampler.voice_elsewhere_at() == seen


def test_voice_elsewhere_at_ignores_an_excluded_voice_app() -> None:
    s = Stretch(user=UserExclusions(processes=("discord.exe",)))
    s.poll()
    assert s.sampler.voice_elsewhere_at() is None


def test_an_excluded_voice_app_elsewhere_writes_no_gap() -> None:
    s = Stretch(user=UserExclusions(processes=("discord.exe",)))
    s.poll()
    s.poll()
    assert s.drops.events == []


def test_a_voice_app_that_is_not_excluded_is_still_reported_next_to_an_excluded_one() -> None:
    heard = polls({"game.exe"}, {"discord.exe", "teamspeak.exe"})
    s = Stretch(heard, user=UserExclusions(processes=("discord.exe",)))
    s.poll()
    s.poll()
    assert [e[:2] for e in s.drops.events] == [("system", "voice_elsewhere")] * 2


def test_a_quiet_poll_a_failed_poll_or_a_late_poll_starts_a_new_stretch() -> None:
    s = Stretch()
    s.poll()
    s.poll(polls({"game.exe"}))  # quiet
    s.poll(VOICE_ELSEWHERE)
    s.poll(None)  # failed
    s.poll(VOICE_ELSEWHERE)
    s.poll(advance=5_000)  # the sampler stalled
    assert [(e[2], e[3], e[4]) for e in s.drops.events] == [
        (T0, T0 + 1_000, False),
        (T0 + 2_000, T0 + 3_000, False),
        (T0 + 4_000, T0 + 5_000, False),
        (T0 + 9_000, T0 + 10_000, False),
    ]


def test_a_halt_splits_a_stretch() -> None:
    s = Stretch()
    halt = threading.Event()

    class Script(threading.Event):
        calls = 0

        def is_set(self) -> bool:
            return self.calls >= 4

        def wait(self, timeout: float | None = None) -> bool:
            del timeout
            self.calls += 1
            s.clock.now += 1_000
            if self.calls == 2:
                halt.set()
            elif self.calls == 3:
                halt.clear()
            return False

    s.sampler.run(Script(), halt)
    assert [(e[2], e[3], e[4]) for e in s.drops.events] == [
        (T0 - 1_000, T0, False),
        (T0, T0 + 1_000, True),
        (T0 + 2_000, T0 + 3_000, False),
    ]


def test_one_stretch_is_one_merged_row_and_two_stretches_are_two(conn: Connection) -> None:
    gaps, clock = real_gaps(conn)
    sessions = FakeSessions(VOICE_ELSEWHERE)
    sampler = SourceSampler(sessions, clock, Exclusions(UserExclusions()), gaps=gaps)
    sampler.set_recorded(A)
    for _ in range(4):
        clock.now += 1_000
        sampler.sample()
    flush(gaps)
    assert [r[:2] for r in rows(conn)] == [("system", "voice_elsewhere")]
    sessions.names = polls({"game.exe"})
    clock.now += 1_000
    sampler.sample()
    sessions.names = VOICE_ELSEWHERE
    for _ in range(3):
        clock.now += 1_000
        sampler.sample()
    flush(gaps)
    assert [r[:2] for r in rows(conn)] == [("system", "voice_elsewhere")] * 2


# --- logs ------------------------------------------------------------------------------------


def test_no_log_line_holds_a_process_name_or_an_endpoint_id(
    store: AudioStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, "akashic")
    s = Stretch(polls({"game.exe", "keepassxc.exe"}, {"discord.exe"}))
    s.poll()
    s.poll()
    s.poll({B: frozenset({"discord.exe"})})  # recorded endpoint missing: a refresh is asked
    s.poll()
    rig = WriterRig(store, polls({"game.exe"}, {"keepassxc.exe"}))
    rig.write()
    assert any("voice chat" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        text = record.getMessage()
        assert not any(w in text for w in ("game.exe", "discord.exe", "keepassxc.exe", A, B)), text
