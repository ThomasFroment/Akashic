"""Capture health: the problem set, the state it maps to, and when the state is sent."""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any, cast

from akashic import child, ipc
from akashic.capture.health import (
    AUDIO_UNAVAILABLE,
    GRAB_FAILURE_RUN,
    SCREEN_IMAGES_FAILING,
    CaptureStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest
    from screen_rig import Rig

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
