"""Capture health: the problem set, the state it maps to, and when the state is sent."""

from __future__ import annotations

import io
import logging
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from audio_fakes import MIC, SYSTEM, RecorderRig, speech
from test_capture_gaps import MUSIC, Spy, WriterRig

from akashic import child, ipc
from akashic.capture.audio import WRITER_QUEUE, AudioCapture, Utterance
from akashic.capture.health import (
    AUDIO_UNAVAILABLE,
    DROP_PROBLEM_S,
    GRAB_FAILURE_RUN,
    RUN_LENGTHS,
    SCREEN_IMAGES_FAILING,
    CaptureStatus,
    MonitorHealth,
)
from akashic.storage.audio import AudioStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from screen_rig import Rig

    from akashic.storage.media import MediaStore

IDLE_STEP_S = 20.0  # past the idle interval, so every tick checks again


def sent_states(out: io.BytesIO) -> list[str]:
    return list(ipc.read_statuses(io.BytesIO(out.getvalue())))


def make_context(out: io.BytesIO) -> child.ChildContext:
    unused = cast("Any", None)
    return child.ChildContext("test", unused, unused, unused, unused, _status_out=out)


# --- when the state is sent ------------------------------------------------------------------


def test_an_unchanged_active_state_sends_nothing() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.announce_ready()
    ctx.set_active("ready")
    ctx.set_active("degraded")
    ctx.set_active("degraded")
    assert sent_states(out) == ["ready", "degraded"]


def test_repeated_pause_and_resume_commands_send_nothing_more() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.announce_ready()
    ctx.set_paused(paused=True)
    ctx.set_paused(paused=True)
    ctx.set_paused(paused=False)
    ctx.set_paused(paused=False)
    assert sent_states(out) == ["ready", "paused", "ready"]


def test_nothing_is_sent_once_shutdown_has_begun() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.stop.set()
    assert not ctx.set_active("degraded")
    assert sent_states(out) == []


def test_a_change_computed_before_a_pause_is_discarded() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    epoch, _, _ = ctx.health_snapshot()
    ctx.set_paused(paused=True)
    ctx.set_paused(paused=False)
    assert not ctx.set_active("degraded", epoch=epoch)
    assert sent_states(out) == ["paused", "ready"]


def test_the_health_snapshot_moves_with_pause_and_resume() -> None:
    ctx = make_context(io.BytesIO())
    epoch, _, resumed_at = ctx.health_snapshot()
    ctx.set_paused(paused=True)
    assert ctx.health_snapshot()[:2] == (epoch + 1, True)
    ctx.set_paused(paused=False)
    new_epoch, paused, new_resumed_at = ctx.health_snapshot()
    assert (new_epoch, paused) == (epoch + 2, False)
    assert new_resumed_at >= resumed_at


def test_a_pause_drops_the_problems_and_a_resume_reports_ready() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.announce_ready()
    ctx.set_active("degraded")
    ctx.set_paused(paused=True)
    ctx.set_paused(paused=False)
    assert sent_states(out) == ["ready", "degraded", "paused", "ready"]


def test_a_resume_with_a_sticky_problem_reports_degraded() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.resume_state = "degraded"
    ctx.announce_ready()
    ctx.set_paused(paused=True)
    ctx.set_paused(paused=False)
    assert sent_states(out)[-1] == "degraded"


# --- the state the problems map to -----------------------------------------------------------


class Reported:
    def __init__(self) -> None:
        self.states: list[str] = []
        self.epoch = 0

    def set_active(self, state: ipc.State, *, epoch: int | None) -> bool:
        del epoch
        self.states.append(state)
        return True


def make_status(*, audio_failed: bool = False) -> tuple[CaptureStatus, Reported]:
    reported = Reported()
    status = CaptureStatus(reported.set_active, lambda: reported.epoch, audio_failed=audio_failed)
    return status, reported


def test_problems_beat_gaming_and_clear_back_to_gaming_then_ready() -> None:
    status, reported = make_status()
    status.set_gaming(True)
    status.set_problems([SCREEN_IMAGES_FAILING], 0)
    status.set_problems([], 0)
    status.set_gaming(False)
    assert reported.states == ["gaming", "degraded", "gaming", "ready"]


def test_a_game_ending_reports_degraded_while_a_problem_is_active() -> None:
    status, reported = make_status()
    status.set_gaming(True)
    status.set_problems([SCREEN_IMAGES_FAILING], 0)
    status.set_gaming(False)
    assert reported.states[-1] == "degraded"


def test_a_sticky_problem_survives_an_empty_problem_set() -> None:
    status, reported = make_status(audio_failed=True)
    status.set_problems([], 0)
    assert status.sticky == (AUDIO_UNAVAILABLE,)
    assert reported.states[-1] == "degraded"


def test_problems_from_an_older_epoch_are_not_replayed() -> None:
    status, reported = make_status()
    reported.epoch = 1
    status.set_problems([SCREEN_IMAGES_FAILING], 0)
    assert reported.states == ["ready"]


def test_only_a_change_in_the_problem_set_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    status, _ = make_status()
    with caplog.at_level(logging.WARNING, logger="akashic.capture.health"):
        status.set_problems([SCREEN_IMAGES_FAILING], 0)
        status.set_problems([SCREEN_IMAGES_FAILING], 0)
        status.set_gaming(True)
    assert [r.getMessage() for r in caplog.records] == [
        f"capture problems: {SCREEN_IMAGES_FAILING}"
    ]


def test_a_set_that_becomes_empty_is_logged_once_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    status, _ = make_status()
    with caplog.at_level(logging.INFO, logger="akashic.capture.health"):
        status.set_problems([SCREEN_IMAGES_FAILING], 0)
        status.set_problems([], 0)
        status.set_problems([], 0)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, f"capture problems: {SCREEN_IMAGES_FAILING}"),
        (logging.INFO, "capture problems cleared"),
    ]


def test_a_non_empty_set_changing_to_another_is_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    status, _ = make_status()
    with caplog.at_level(logging.INFO, logger="akashic.capture.health"):
        status.set_problems([SCREEN_IMAGES_FAILING], 0)
        status.set_problems([SCREEN_IMAGES_FAILING, "other_problem"], 0)
        status.set_problems(["other_problem"], 0)
        status.set_problems(["other_problem"], 0)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, f"capture problems: {SCREEN_IMAGES_FAILING}"),
        (logging.WARNING, f"capture problems: other_problem, {SCREEN_IMAGES_FAILING}"),
        (logging.WARNING, "capture problems: other_problem"),
    ]


def test_an_empty_set_at_start_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    status, _ = make_status()
    with caplog.at_level(logging.INFO, logger="akashic.capture.health"):
        status.set_problems([], 0)
    assert caplog.records == []


def test_a_discarded_change_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    ctx = make_context(io.BytesIO())
    status = CaptureStatus(ctx.set_active, lambda: ctx.pause_epoch, audio_failed=False)
    ctx.stop.set()
    with caplog.at_level(logging.WARNING, logger="akashic.capture.health"):
        status.set_problems([SCREEN_IMAGES_FAILING], 0)
    assert caplog.records == []


# --- the screen loop's capture failures reach the writer's health ------------------------------


def fail_captures(rig: Rig, count: int) -> None:
    rig.captures.refuse.add(1)  # the window capture will not start: each check reports one failure
    for step in range(count):
        rig.tick(step * IDLE_STEP_S)


def test_three_failed_captures_in_a_row_are_a_problem(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    fail_captures(rig, GRAB_FAILURE_RUN - 1)
    assert rig.writer.problems(float("-inf")) == []
    rig.tick(GRAB_FAILURE_RUN * IDLE_STEP_S)
    assert rig.writer.problems(float("-inf")) == [f"{SCREEN_IMAGES_FAILING}@Monitor 1"]


def test_failures_before_the_resume_do_not_count(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    fail_captures(rig, GRAB_FAILURE_RUN)
    assert rig.writer.problems(float("-inf")) == [f"{SCREEN_IMAGES_FAILING}@Monitor 1"]
    assert rig.writer.problems(float("inf")) == []


def test_a_good_check_clears_the_record(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    fail_captures(rig, GRAB_FAILURE_RUN)
    rig.captures.refuse.clear()
    rig.settle(GRAB_FAILURE_RUN * IDLE_STEP_S)
    assert rig.writer.problems(float("-inf")) == []


def test_a_locked_session_does_not_touch_the_record(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    fail_captures(rig, GRAB_FAILURE_RUN - 1)
    rig.windows.locked = True
    rig.tick(GRAB_FAILURE_RUN * IDLE_STEP_S)
    rig.windows.locked = False
    rig.tick((GRAB_FAILURE_RUN + 1) * IDLE_STEP_S)
    assert rig.writer.problems(float("-inf")) == [f"{SCREEN_IMAGES_FAILING}@Monitor 1"]


# --- how many events make each kind of screen run a problem -------------------------------------


@pytest.mark.parametrize(
    ("kind", "length"),
    [("failure", 1), ("disk_full", 1), ("capture", GRAB_FAILURE_RUN), ("drop", GRAB_FAILURE_RUN)],
)
def test_each_run_kind_has_its_own_length(kind: Any, length: int) -> None:  # noqa: ANN401
    assert RUN_LENGTHS[kind] == length
    health = MonitorHealth()
    for i in range(length - 1):
        health.record("m", kind, float(i))
    assert health.problems(0.0, {}) == []
    health.record("m", kind, float(length))
    assert len(health.problems(0.0, {})) == 1


# --- audio segments dropped by the writer ------------------------------------------------------


def audio_capture(
    dropped_at: dict[str, float], *, down: bool = False, elsewhere_at: float | None = None
) -> AudioCapture:
    capture = object.__new__(AudioCapture)
    rigs = [RecorderRig("mic"), RecorderRig("system")]
    if not down:
        for rig in rigs:
            rig.recorder._ok_at = 1000.0  # pyright: ignore[reportPrivateUsage]
    capture._recorders = [r.recorder for r in rigs]  # pyright: ignore[reportPrivateUsage]

    def dropped_since(t: float) -> frozenset[str]:
        return frozenset(k for k, v in dropped_at.items() if v >= t)

    capture._writer = cast("Any", SimpleNamespace(dropped_since=dropped_since))  # pyright: ignore[reportPrivateUsage]
    capture._sampler = cast("Any", SimpleNamespace(voice_elsewhere_at=lambda: elsewhere_at))  # pyright: ignore[reportPrivateUsage]
    return capture


def test_a_drop_time_of_one_stream_names_that_stream() -> None:
    assert audio_capture({"mic": 995.0}).problems(1000.0, 0.0) == ("mic_segments_dropped",)
    assert audio_capture({"system": 995.0}).problems(1000.0, 0.0) == ("system_segments_dropped",)


def test_a_drop_is_a_problem_from_exactly_the_window_start() -> None:
    assert DROP_PROBLEM_S == 60.0
    assert audio_capture({"mic": 940.0}).problems(1000.0, 0.0) == ("mic_segments_dropped",)
    assert audio_capture({"mic": 939.9}).problems(1000.0, 0.0) == ()


def test_a_drop_before_the_resume_is_not_a_problem() -> None:
    assert audio_capture({"mic": 990.0}).problems(1000.0, 990.0) == ("mic_segments_dropped",)
    assert audio_capture({"mic": 989.9}).problems(1000.0, 990.0) == ()


def test_down_codes_come_first_then_dropped_mic_then_system() -> None:
    capture = audio_capture({"system": 999.0, "mic": 999.0}, down=True)
    assert capture.problems(1000.0, 0.0) == (
        "mic_unavailable",
        "system_audio_unavailable",
        "mic_segments_dropped",
        "system_segments_dropped",
    )


def test_voice_elsewhere_is_a_problem_within_the_window_only() -> None:
    assert audio_capture({}).problems(1000.0, 0.0) == ()
    assert audio_capture({}, elsewhere_at=995.0).problems(1000.0, 0.0) == ("voice_elsewhere",)
    assert audio_capture({}, elsewhere_at=940.0).problems(1000.0, 0.0) == ("voice_elsewhere",)
    assert audio_capture({}, elsewhere_at=939.9).problems(1000.0, 0.0) == ()


def test_voice_elsewhere_before_the_resume_is_not_a_problem() -> None:
    assert audio_capture({}, elsewhere_at=990.0).problems(1000.0, 990.0) == ("voice_elsewhere",)
    assert audio_capture({}, elsewhere_at=989.9).problems(1000.0, 990.0) == ()


def test_voice_elsewhere_comes_last() -> None:
    capture = audio_capture({"system": 999.0, "mic": 999.0}, down=True, elsewhere_at=999.0)
    assert capture.problems(1000.0, 0.0) == (
        "mic_unavailable",
        "system_audio_unavailable",
        "mic_segments_dropped",
        "system_segments_dropped",
        "voice_elsewhere",
    )


def test_voice_elsewhere_degrades_even_during_a_game() -> None:
    status, reported = make_status()
    status.set_gaming(True)
    status.set_problems(["voice_elsewhere"], 0)
    status.set_problems([], 0)
    assert reported.states == ["gaming", "degraded", "gaming"]


@pytest.fixture
def store(conn: Connection, media: MediaStore) -> AudioStore:
    return AudioStore(conn, media)


def test_a_full_writer_queue_sets_the_drop_time(store: AudioStore) -> None:
    w = WriterRig(store, Spy())
    utterance = Utterance(0, 1_000, speech(10), 800, continues=False)
    for _ in range(WRITER_QUEUE):
        w.writer.submit("mic", utterance, MIC)
    assert w.writer.dropped_since(float("-inf")) == frozenset()
    before = time.monotonic()
    w.writer.submit("mic", utterance, MIC)
    assert w.writer.dropped_since(before) == {"mic"}


def test_a_failed_store_sets_the_drop_time(
    store: AudioStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = WriterRig(store, Spy())

    def boom(*_a: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(w.writer, "_add_with_retry", boom)
    w.sessions.names = frozenset({"discord.exe"})
    w.clock.now = w.clock.now + 500
    w.sampler.sample()
    stop = threading.Event()
    stop.set()
    before = time.monotonic()
    start = w.clock.now - 500
    w.writer.submit(
        "system", Utterance(start, start + 2_000, speech(60), 1_500, continues=False), SYSTEM
    )
    w.writer.run(stop)
    assert w.writer.dropped_since(before) == {"system"}


@pytest.mark.parametrize("audible", [frozenset({"keepassxc.exe", "discord.exe"}), None, MUSIC])
def test_excluded_unknown_and_music_drops_are_not_problems(
    store: AudioStore, audible: frozenset[str] | None
) -> None:
    w = WriterRig(store, Spy())
    assert w.write(0, audible) is None
    assert w.writer.dropped_since(float("-inf")) == frozenset()


def test_segments_thrown_away_at_stop_do_not_set_a_drop_time(store: AudioStore) -> None:
    w = WriterRig(store, Spy())
    w.writer.submit("mic", Utterance(0, 1_000, speech(10), 800, continues=False), MIC)
    w.writer._drop_queued()  # pyright: ignore[reportPrivateUsage]
    assert w.writer.dropped_since(float("-inf")) == frozenset()


# --- the sidebar words --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "words"),
    [
        ("mic_segments_dropped", "some microphone speech could not be saved in the last minute"),
        ("system_segments_dropped", "some system audio could not be saved in the last minute"),
    ],
)
def test_the_sidebar_words_each_dropped_code(code: str, words: str) -> None:
    js = (Path(__file__).parents[1] / "src" / "akashic" / "ui" / "app.js").read_text("utf-8")
    assert f'{code}: "{words}"' in js


def test_the_sidebar_words_voice_elsewhere_in_a_plain_sentence() -> None:
    js = (Path(__file__).parents[1] / "src" / "akashic" / "ui" / "app.js").read_text("utf-8")
    assert re.search(r'^\s*voice_elsewhere: "[^"]+",$', js, re.MULTILINE)
