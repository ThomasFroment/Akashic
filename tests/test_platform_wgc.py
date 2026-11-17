"""WgcCaptures against fake projection objects; no real window or GPU is touched."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from winrt.windows.graphics.directx import DirectXPixelFormat

from akashic.capture.ports import CapturedFrame, WindowCaptures
from akashic.log import FaultLog
from akashic.platform.win import wgc
from akashic.platform.win.wgc import WgcCaptures

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    type Handler = Callable[[object, object], None]

RED = b"\x00\x00\xff\xff"  # BGRA
GREEN = b"\x00\xff\x00\xff"
SIZE = (640, 480)
REMOVED, HUNG, RESET = 0x887A0005, 0x887A0006, 0x887A0007


def hresult_error(code: int, text: str = "device") -> OSError:
    error = OSError(text)
    error.winerror = code - (1 << 32)  # ctypes reports it signed
    return error


class FakeDevice:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeBitmap:
    def __init__(self, width: int, height: int, fill: bytes) -> None:
        self.pixel_width = width
        self.pixel_height = height
        self.fill = fill
        self.read_error: Exception | None = None

    def copy_to_buffer(self, buffer: bytearray) -> None:
        if self.read_error is not None:
            raise self.read_error
        buffer[:] = self.fill * (self.pixel_width * self.pixel_height)


class FakeSurface:
    def __init__(self, bitmap: FakeBitmap) -> None:
        self.bitmap = bitmap


class FakeFrame:
    def __init__(self, content: tuple[int, int], surface: FakeSurface) -> None:
        self.content_size = SimpleNamespace(width=content[0], height=content[1])
        self.surface = surface
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeItem:
    def __init__(self, size: tuple[int, int]) -> None:
        self.size = SimpleNamespace(width=size[0], height=size[1])
        self.closed_handler: Handler | None = None

    def add_closed(self, handler: Handler) -> int:
        self.closed_handler = handler
        return 1

    def remove_closed(self, token: int) -> None:
        pass

    def fire_closed(self) -> None:
        assert self.closed_handler is not None
        self.closed_handler(None, None)


class FakeSession:
    def __init__(self, world: World) -> None:
        self.world = world
        self.is_border_required = True
        self.is_cursor_capture_enabled = True
        self.min_update_interval = timedelta(seconds=99)
        self.started = False
        self.closed = False

    def start_capture(self) -> None:
        if self.world.start_error is not None:
            raise self.world.start_error
        self.started = True

    def close(self) -> None:
        self.closed = True


class FakePool:
    def __init__(self, world: World, device: object, size: tuple[int, int]) -> None:
        self.world = world
        self.device = device
        self.size = size
        self.hwnd = 0
        self.args: tuple[object, ...] = ()
        self.handler: Handler | None = None
        self.session: FakeSession | None = None
        self.item: FakeItem | None = None
        self.queue: list[FakeFrame] = []
        self.recreated: list[tuple[object, ...]] = []
        self.closed = False

    def create_capture_session(self, item: FakeItem) -> FakeSession:
        self.item = item
        self.session = FakeSession(self.world)
        return self.session

    def add_frame_arrived(self, handler: Handler) -> int:
        self.handler = handler
        return 2

    def remove_frame_arrived(self, token: int) -> None:
        pass

    def try_get_next_frame(self) -> FakeFrame | None:
        return self.queue.pop(0) if self.queue else None

    def recreate(self, device: object, fmt: object, buffers: int, size: tuple[int, int]) -> None:
        self.recreated.append((device, fmt, buffers, size))

    def close(self) -> None:
        self.closed = True


class World:
    def __init__(self) -> None:
        self.supported = True
        self.device_available = True
        self.invalid: set[int] = set()
        self.start_error: Exception | None = None
        self.item_error: Exception | None = None
        self.copy_error: Exception | None = None
        self.devices: list[FakeDevice] = []
        self.pools: list[FakePool] = []
        self.items: dict[int, FakeItem] = {}
        self.window_size = SIZE
        self.pending_hwnd = 0  # the pool factory is not told the hwnd; the item call just before is

    def new_device(self) -> FakeDevice | None:
        if not self.device_available:
            return None
        device = FakeDevice()
        self.devices.append(device)
        return device

    def make_item(self, args: tuple[int]) -> FakeItem | None:
        (hwnd,) = args
        if self.item_error is not None:
            raise self.item_error
        if hwnd in self.invalid:
            return None
        item = FakeItem(self.window_size)
        self.items[hwnd] = item
        self.pending_hwnd = hwnd
        return item

    def make_pool(
        self, device: object, fmt: object, buffers: int, size: tuple[int, int]
    ) -> FakePool:
        pool = FakePool(self, device, size)
        pool.args = (device, fmt, buffers, size)
        pool.hwnd = self.pending_hwnd
        self.pools.append(pool)
        return pool

    async def copy(self, surface: FakeSurface) -> FakeBitmap:
        if self.copy_error is not None:
            raise self.copy_error
        bitmap = surface.bitmap
        return FakeBitmap(bitmap.pixel_width, bitmap.pixel_height, bitmap.fill)

    def pools_of(self, hwnd: int) -> list[FakePool]:
        return [p for p in self.pools if p.hwnd == hwnd]

    def deliver(
        self,
        hwnd: int,
        fill: bytes = RED,
        content: tuple[int, int] | None = None,
        bitmap_size: tuple[int, int] | None = None,
    ) -> FakeFrame:
        pool = self.pools_of(hwnd)[-1]
        width, height = bitmap_size or pool.size
        frame = FakeFrame(content or pool.size, FakeSurface(FakeBitmap(width, height, fill)))
        pool.queue.append(frame)
        assert pool.handler is not None
        pool.handler(None, None)
        return frame


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(wgc, "_new_device", w.new_device)
    monkeypatch.setattr(
        wgc, "GraphicsCaptureSession", SimpleNamespace(is_supported=lambda: w.supported)
    )
    monkeypatch.setattr(
        wgc, "GraphicsCaptureItem", SimpleNamespace(try_create_from_window_id=w.make_item)
    )
    monkeypatch.setattr(
        wgc, "Direct3D11CaptureFramePool", SimpleNamespace(create_free_threaded=w.make_pool)
    )
    monkeypatch.setattr(
        wgc, "SoftwareBitmap", SimpleNamespace(create_copy_from_surface_async=w.copy)
    )
    return w


@pytest.fixture
def caps(world: World) -> Iterator[WgcCaptures]:
    assert not world.pools  # the fake projection is installed before WgcCaptures is built
    captures = WgcCaptures()
    yield captures
    captures.close()


def session_of(world: World, hwnd: int) -> FakeSession:
    session = world.pools_of(hwnd)[-1].session
    assert session is not None
    return session


def test_implements_the_window_captures_port(caps: WgcCaptures) -> None:
    port: WindowCaptures = caps
    assert port.failed() == frozenset()


def test_start_opens_a_borderless_cursorless_session_on_a_free_threaded_pool(
    world: World, caps: WgcCaptures
) -> None:
    assert caps.start(10, 250) is True
    pool = world.pools_of(10)[0]
    assert pool.args == (
        world.devices[0],
        DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED,
        1,
        SIZE,
    )
    session = session_of(world, 10)
    assert session.is_border_required is False
    assert session.is_cursor_capture_enabled is False
    assert session.min_update_interval == timedelta(milliseconds=250)
    assert session.started


@pytest.mark.parametrize("ms", [0, -5])
def test_no_minimum_interval_when_ms_is_zero_or_less(
    world: World, caps: WgcCaptures, ms: int
) -> None:
    assert caps.start(10, ms)
    assert session_of(world, 10).min_update_interval == timedelta(0)


def test_start_false_for_an_invalid_hwnd(world: World, caps: WgcCaptures) -> None:
    world.invalid.add(10)
    assert caps.start(10, 100) is False
    assert world.pools == []
    assert caps.failed() == frozenset()


def test_start_false_when_the_item_creation_raises(world: World, caps: WgcCaptures) -> None:
    world.item_error = OSError("destroyed")
    assert caps.start(10, 100) is False
    assert caps.latest_seq(10) is None
    assert caps.failed() == frozenset()


def test_start_false_when_capture_is_unsupported(world: World, caps: WgcCaptures) -> None:
    world.supported = False
    assert caps.start(10, 100) is False
    assert world.pools == []


def test_start_false_when_no_device_can_be_created_and_works_once_one_can(
    world: World,
) -> None:
    world.device_available = False
    captures = WgcCaptures()
    try:
        assert captures.start(10, 100) is False
        assert world.pools == []
        world.device_available = True
        assert captures.start(10, 100) is True
    finally:
        captures.close()


def test_failed_open_leaves_nothing_allocated(world: World, caps: WgcCaptures) -> None:
    world.start_error = RuntimeError("no")
    assert caps.start(10, 100) is False
    pool = world.pools_of(10)[0]
    assert pool.closed
    assert pool.session is not None
    assert pool.session.closed
    assert caps.latest_seq(10) is None
    assert caps.failed() == frozenset()


def test_start_false_after_close(world: World) -> None:
    captures = WgcCaptures()
    captures.close()
    assert captures.start(10, 100) is False
    assert world.pools == []


def test_start_false_at_max_captures_unless_the_hwnd_is_already_live(
    world: World, caps: WgcCaptures
) -> None:
    for hwnd in range(1, 33):
        assert caps.start(hwnd, 100)
    assert caps.start(33, 100) is False
    assert world.pools_of(33) == []
    assert caps.start(5, 200) is True


def test_start_on_a_live_hwnd_only_updates_the_interval_and_keeps_the_frame(
    world: World, caps: WgcCaptures
) -> None:
    caps.start(10, 100)
    world.deliver(10)
    seq = caps.latest_seq(10)
    assert caps.start(10, 400) is True
    assert len(world.pools_of(10)) == 1
    assert session_of(world, 10).min_update_interval == timedelta(milliseconds=400)
    assert caps.latest_seq(10) == seq


def test_start_on_a_failed_hwnd_tears_it_down_and_starts_fresh(
    world: World, caps: WgcCaptures
) -> None:
    caps.start(10, 100)
    world.deliver(10)
    world.items[10].fire_closed()
    assert caps.failed() == {10}
    assert caps.start(10, 100) is True
    assert caps.failed() == frozenset()
    old, new = world.pools_of(10)
    assert old.closed
    assert not new.closed
    assert caps.latest_seq(10) is None


def test_no_seq_until_a_first_frame_then_one_growing_counter_across_captures(
    world: World, caps: WgcCaptures
) -> None:
    caps.start(1, 0)
    caps.start(2, 0)
    assert caps.latest_seq(1) is None
    assert caps.grab(1) is None
    world.deliver(1)
    first = caps.latest_seq(1)
    world.deliver(2)
    second = caps.latest_seq(2)
    world.deliver(1)
    third = caps.latest_seq(1)
    assert first is not None
    assert second is not None
    assert third is not None
    assert first < second < third


def test_grab_returns_a_fresh_native_size_rgb_copy_of_the_newest_frame(
    world: World, caps: WgcCaptures
) -> None:
    caps.start(10, 0)
    before = time.monotonic()
    world.deliver(10, RED)
    after = time.monotonic()
    frame = caps.grab(10)
    assert isinstance(frame, CapturedFrame)
    assert frame.seq == caps.latest_seq(10)
    assert before <= frame.at <= after
    assert frame.image.mode == "RGB"
    assert frame.image.size == SIZE
    assert frame.image.getpixel((3, 3)) == (255, 0, 0)

    world.deliver(10, GREEN)
    assert frame.image.getpixel((3, 3)) == (255, 0, 0)
    newer = caps.grab(10)
    assert newer is not None
    assert newer.seq > frame.seq
    assert newer.image is not frame.image
    assert newer.image.getpixel((3, 3)) == (0, 255, 0)


def test_the_frame_is_checked_in_and_each_capture_keeps_its_own_copy(
    world: World, caps: WgcCaptures
) -> None:
    caps.start(1, 0)
    caps.start(2, 0)
    first = world.deliver(1)
    second = world.deliver(2)
    assert first.closed
    assert second.closed
    bitmaps = [caps._captures[h].bitmap for h in (1, 2)]
    assert bitmaps[0] is not bitmaps[1]
    assert all(b is not None for b in bitmaps)
    assert bitmaps[0] is not first.surface.bitmap


def test_a_size_change_recreates_the_pool_and_keeps_the_frame_cropped(
    world: World, caps: WgcCaptures
) -> None:
    caps.start(10, 0)
    world.deliver(10, content=(800, 600), bitmap_size=SIZE)
    pool = world.pools_of(10)[0]
    assert pool.recreated == [
        (world.devices[0], DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED, 1, (800, 600))
    ]
    frame = caps.grab(10)
    assert frame is not None
    assert frame.image.size == SIZE

    pool.size = (800, 600)
    world.deliver(10, content=(300, 200), bitmap_size=(800, 600))
    shrunk = caps.grab(10)
    assert shrunk is not None
    assert shrunk.image.size == (300, 200)
    assert len(pool.recreated) == 2


def test_item_closed_fails_the_hwnd_and_drops_its_frame(world: World, caps: WgcCaptures) -> None:
    caps.start(10, 0)
    world.deliver(10)
    world.items[10].fire_closed()
    assert caps.failed() == {10}
    assert caps.latest_seq(10) is None
    assert caps.grab(10) is None


def test_an_unreadable_frame_fails_the_hwnd(world: World, caps: WgcCaptures) -> None:
    caps.start(10, 0)
    world.deliver(10)
    world.copy_error = RuntimeError("unreadable")
    world.deliver(10)
    assert caps.failed() == {10}
    assert caps.latest_seq(10) is None
    assert caps.grab(10) is None


def test_an_unreadable_bitmap_in_grab_fails_the_hwnd(world: World, caps: WgcCaptures) -> None:
    caps.start(10, 0)
    world.deliver(10)
    cast("FakeBitmap", caps._captures[10].bitmap).read_error = RuntimeError("unreadable")
    assert caps.grab(10) is None
    assert caps.failed() == {10}


def test_stop_drops_the_frame_clears_failed_and_is_idempotent(
    world: World, caps: WgcCaptures
) -> None:
    caps.start(10, 0)
    world.deliver(10)
    world.items[10].fire_closed()
    caps.stop(10)
    caps.stop(10)
    caps.stop(99)
    assert caps.failed() == frozenset()
    assert caps.latest_seq(10) is None
    assert caps.grab(10) is None
    assert world.pools_of(10)[0].closed


def test_a_callback_after_stop_stores_nothing(world: World, caps: WgcCaptures) -> None:
    caps.start(10, 0)
    pool = world.pools_of(10)[0]
    handler = pool.handler
    assert handler is not None
    caps.stop(10)
    pool.queue.append(FakeFrame(SIZE, FakeSurface(FakeBitmap(*SIZE, RED))))
    handler(None, None)
    assert caps.latest_seq(10) is None
    assert caps.grab(10) is None


@pytest.mark.parametrize("code", [REMOVED, HUNG, RESET])
def test_device_loss_rebuilds_the_device_and_restarts_each_hwnd_with_its_interval(
    world: World, caps: WgcCaptures, code: int
) -> None:
    caps.start(1, 100)
    caps.start(2, 500)
    world.deliver(1)
    world.deliver(2)
    world.copy_error = hresult_error(code)
    world.deliver(1)
    assert caps.failed() == frozenset()  # the call above only noticed the loss
    world.copy_error = None

    assert caps.latest_seq(2) is None  # frames dropped, pending new ones
    assert len(world.devices) == 2
    assert world.devices[0].closed
    assert caps.failed() == frozenset()
    assert session_of(world, 1).min_update_interval == timedelta(milliseconds=100)
    assert session_of(world, 2).min_update_interval == timedelta(milliseconds=500)
    assert world.pools_of(1)[-1].args[0] is world.devices[1]
    assert world.pools_of(1)[0].closed

    world.deliver(1)
    assert caps.latest_seq(1) is not None
    assert caps.latest_seq(2) is None


def test_device_loss_in_grab_is_not_a_capture_failure(world: World, caps: WgcCaptures) -> None:
    caps.start(10, 0)
    world.deliver(10)
    cast("FakeBitmap", caps._captures[10].bitmap).read_error = hresult_error(REMOVED)
    assert caps.grab(10) is None
    assert caps.failed() == frozenset()
    assert len(world.devices) == 2
    assert session_of(world, 10).started


def test_device_loss_while_opening_rebuilds_on_the_next_call(
    world: World, caps: WgcCaptures
) -> None:
    world.start_error = hresult_error(HUNG)
    assert caps.start(10, 100) is False
    assert len(world.devices) == 1
    world.start_error = None
    assert caps.failed() == frozenset()
    assert len(world.devices) == 2
    assert caps.start(10, 100) is True


def test_hwnds_that_cannot_be_restarted_are_failed(world: World, caps: WgcCaptures) -> None:
    caps.start(1, 0)
    caps.start(2, 0)
    world.copy_error = hresult_error(RESET)
    world.deliver(1)
    world.invalid.add(2)
    assert caps.failed() == {2}
    assert caps.latest_seq(1) is None
    world.invalid.clear()
    assert caps.start(2, 0) is True
    assert caps.failed() == frozenset()


def test_no_device_after_a_loss_fails_every_started_hwnd(world: World, caps: WgcCaptures) -> None:
    caps.start(1, 0)
    caps.start(2, 0)
    world.copy_error = hresult_error(REMOVED)
    world.deliver(1)
    world.device_available = False
    assert caps.failed() == {1, 2}


def test_a_hung_app_keeps_its_last_frame_and_is_not_failed(world: World, caps: WgcCaptures) -> None:
    caps.start(10, 0)
    world.deliver(10)
    seq = caps.latest_seq(10)
    for _ in range(3):
        assert caps.failed() == frozenset()
        assert caps.latest_seq(10) == seq
        assert caps.grab(10) is not None


def test_close_stops_everything_releases_device_and_loop_and_is_idempotent(
    world: World,
) -> None:
    captures = WgcCaptures()
    captures.start(1, 0)
    captures.start(2, 0)
    world.deliver(1)
    captures.close()
    captures.close()
    assert all(p.closed for p in world.pools)
    assert all(d.closed for d in world.devices)
    assert captures._loop.is_closed()
    assert captures.start(1, 0) is False
    assert captures.latest_seq(1) is None
    assert captures.grab(1) is None
    assert captures.failed() == frozenset()
    captures.stop(1)


def test_no_public_method_raises(
    world: World, caps: WgcCaptures, monkeypatch: pytest.MonkeyPatch
) -> None:
    caps.start(10, 0)
    world.deliver(10)

    def boom(*_args: object) -> None:
        raise RuntimeError("os")

    monkeypatch.setattr(WgcCaptures, "_restart_if_lost", boom)
    assert caps.start(11, 0) is False
    assert caps.latest_seq(10) is None
    assert caps.grab(10) is None
    assert caps.failed() == frozenset()
    monkeypatch.setattr(wgc, "_release", boom)
    caps.stop(10)
    caps.close()


def test_logs_carry_only_hresult_codes_never_text(
    world: World, caps: WgcCaptures, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=wgc.log.name)
    caps.start(10, 0)
    world.copy_error = hresult_error(REMOVED, "SECRET window title chrome.exe")
    world.deliver(10)
    world.copy_error = RuntimeError("SECRET window title chrome.exe")
    caps.failed()
    world.deliver(10)
    assert "0x887A0005" in caplog.text
    assert "SECRET" not in caplog.text
    assert "chrome" not in caplog.text


def test_a_refused_window_is_logged_once_per_interval_but_always_counted(
    world: World, caps: WgcCaptures, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=wgc.log.name)
    now = [0.0]
    caps.faults = FaultLog(wgc.log, clock=lambda: now[0])
    world.start_error = hresult_error(REMOVED + 100)
    for _ in range(5):
        assert not caps.start(10, 0)
    assert caplog.text.count("capture start failed") == 1
    assert sum(caps.faults.counts().values()) == 5
    now[0] = 301.0
    caps.start(10, 0)
    assert caplog.text.count("capture start failed") == 2
    assert "(1 so far)" in caplog.text
    assert "(6 so far)" in caplog.text
