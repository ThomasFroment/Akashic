# ruff: noqa: FBT003
"""The new capture ports: value types and the fakes that stand in for them."""

from __future__ import annotations

import dataclasses
from typing import get_args

import pytest
from capture_fakes import (
    SCREEN,
    FakeDisplays,
    FakeWindowCaptures,
    FakeWindowEvents,
    FakeWindowFacts,
    blank,
    monitor,
    top_window,
)
from PIL import Image

from akashic.capture.ports import (
    VOICE_APPS,
    CapturedFrame,
    Displays,
    MonitorInfo,
    TopWindow,
    WindowCaptures,
    WindowChange,
    WindowChanges,
    WindowEvent,
    WindowEvents,
    WindowFacts,
)
from akashic.pipeline import episodes

# Static check: strict pyright fails if a fake does not satisfy its Protocol.
_displays: Displays = FakeDisplays()
_facts: WindowFacts = FakeWindowFacts()
_events: WindowEvents = FakeWindowEvents()
_captures: WindowCaptures = FakeWindowCaptures()


def test_monitor_info_is_positional_frozen_and_value_equal() -> None:
    a = MonitorInfo("k", "n", SCREEN, 1.5, True)
    assert a == MonitorInfo("k", "n", SCREEN, 1.5, True)
    assert hash(a) == hash(MonitorInfo("k", "n", SCREEN, 1.5, True))
    assert [f.name for f in dataclasses.fields(MonitorInfo)] == [
        "key",
        "name",
        "rect",
        "scale",
        "primary",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.key = "x"  # pyright: ignore[reportAttributeAccessIssue]


def test_top_window_field_order_has_no_defaults_and_is_frozen() -> None:
    fields = dataclasses.fields(TopWindow)
    assert [f.name for f in fields] == [
        "hwnd",
        "pid",
        "process",
        "uwp_unresolved",
        "class_name",
        "title",
        "rect",
        "minimized",
        "cloaked",
        "owner",
        "popup",
        "tool",
        "alpha",
        "is_ghost",
        "ghost_of",
        "foreign_children",
        "has_frame",
    ]
    assert all(f.default is dataclasses.MISSING for f in fields)
    w = top_window()
    assert w == top_window()
    assert hash(w) == hash(top_window())
    with pytest.raises(dataclasses.FrozenInstanceError):
        w.title = "x"  # pyright: ignore[reportAttributeAccessIssue]


def test_event_changes_and_frame_types() -> None:
    assert [f.name for f in dataclasses.fields(WindowEvent)] == ["hwnd", "change", "at"]
    assert [f.name for f in dataclasses.fields(WindowChanges)] == ["events", "healthy"]
    assert [f.name for f in dataclasses.fields(CapturedFrame)] == ["seq", "at", "image"]
    event = WindowEvent(1, "shown", 2.0)
    assert event == WindowEvent(1, "shown", 2.0)
    assert hash(event) == hash(WindowEvent(1, "shown", 2.0))
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.hwnd = 2  # pyright: ignore[reportAttributeAccessIssue]
    changes = WindowChanges((event,), True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        changes.healthy = False  # pyright: ignore[reportAttributeAccessIssue]
    frame = CapturedFrame(1, 0.5, blank())
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.seq = 2  # pyright: ignore[reportAttributeAccessIssue]


def test_window_change_literal_has_no_reordered() -> None:
    assert set(get_args(WindowChange.__value__)) == {
        "shown",
        "hidden",
        "destroyed",
        "moved",
        "title",
        "focus",
    }


def test_fake_displays_default_and_copy_and_none() -> None:
    fake = FakeDisplays()
    assert fake.monitors() == [monitor()]
    assert monitor().rect == SCREEN
    assert monitor().scale == 1.0
    assert monitor().primary
    got = fake.monitors()
    assert got is not None
    got.clear()
    assert fake.monitors() == [monitor()]
    assert FakeDisplays(None).monitors() is None


def test_fake_window_facts_keeps_order_returns_copy_or_none() -> None:
    a, b = top_window(hwnd=1), top_window(hwnd=2)
    fake = FakeWindowFacts([b, a])
    got = fake.windows()
    assert got == [b, a]
    assert got is not fake.window_list
    assert FakeWindowFacts(None).windows() is None


def test_fake_window_events_drain_order_health_and_empties() -> None:
    fake = FakeWindowEvents()
    fake.start()
    fake.push(2, "moved", 1.0)
    fake.push(1, "focus", 2.0)
    first = fake.drain()
    assert first.events == (WindowEvent(2, "moved", 1.0), WindowEvent(1, "focus", 2.0))
    assert first.healthy
    fake.healthy = False
    second = fake.drain()
    assert second.events == ()
    assert not second.healthy
    fake.close()
    assert (fake.starts, fake.closes) == (1, 1)


def test_fake_captures_refused_start_starts_nothing() -> None:
    fake = FakeWindowCaptures(refuse={5})
    assert not fake.start(5, 100)
    fake.push(5, blank(), 1.0)
    assert fake.latest_seq(5) is None
    assert fake.grab(5) is None
    assert fake.start(6, 250)
    assert fake.intervals[6] == 250


def test_fake_captures_push_seq_increases_across_hwnds_and_ignores_unstarted() -> None:
    fake = FakeWindowCaptures()
    fake.start(1, 100)
    fake.start(2, 100)
    fake.push(1, blank(), 1.0)
    fake.push(2, blank(), 2.0)
    fake.push(3, blank(), 3.0)
    assert fake.latest_seq(3) is None
    first, second = fake.latest_seq(1), fake.latest_seq(2)
    assert first is not None
    assert second is not None
    assert second > first
    fake.push(1, blank(), 4.0)
    third = fake.latest_seq(1)
    assert third is not None
    assert third > second


def test_fake_captures_grab_returns_copy() -> None:
    fake = FakeWindowCaptures()
    fake.start(1, 100)
    fake.push(1, blank("white", (4, 4)), 1.5)
    frame = fake.grab(1)
    assert frame is not None
    assert (frame.seq, frame.at) == (fake.latest_seq(1), 1.5)
    frame.image.putpixel((0, 0), (0, 0, 0))
    again = fake.grab(1)
    assert again is not None
    assert again.image.getpixel((0, 0)) == (255, 255, 255)
    assert isinstance(again.image, Image.Image)


def test_fake_captures_stop_discards_and_is_idempotent() -> None:
    fake = FakeWindowCaptures(failures={1})
    fake.start(1, 100)
    fake.push(1, blank(), 1.0)
    fake.stop(1)
    fake.stop(1)
    fake.stop(99)
    assert fake.latest_seq(1) is None
    assert fake.grab(1) is None


def test_fake_captures_failed_only_lists_started_configured_hwnds() -> None:
    fake = FakeWindowCaptures(failures={1, 2})
    fake.start(1, 100)
    fake.start(3, 100)
    assert fake.failed() == frozenset({1})


def test_fake_captures_close_stops_everything() -> None:
    fake = FakeWindowCaptures()
    fake.start(1, 100)
    fake.start(2, 100)
    fake.push(1, blank(), 1.0)
    fake.close()
    assert fake.latest_seq(1) is None
    assert fake.grab(2) is None
    assert fake.failed() == frozenset()


def test_voice_apps_live_in_capture_ports_and_episodes_reuses_them() -> None:
    assert len(VOICE_APPS) == 15
    assert all(name == name.lower() and name.endswith(".exe") for name in VOICE_APPS)
    assert episodes.VOICE_APPS is VOICE_APPS
