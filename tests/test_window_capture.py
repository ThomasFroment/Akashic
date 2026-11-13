"""Selected-window capture (``GdiScreen.grab_window``) without touching real windows.

The bounded wait, the single render thread and the discarding of late pixels are driven through
a ``_render_window`` stand-in. The crop to the window's DWM bounds is driven through stand-ins for
the ``user32``/``gdi32`` calls, with a real buffer that the fake ``PrintWindow`` paints. The
occluded-window behaviour itself needs real windows: tests/manual/CHECKLIST.md, "Screen capture".
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest
from PIL import Image
from waits import wait_for

from akashic.capture.ports import Rect, WindowInfo
from akashic.platform.win import screen as win_screen
from akashic.platform.win.screen import WINDOW_RENDER_WAIT_S, GdiScreen

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

WINDOW = WindowInfo(42, "app.exe", "App", Rect(110, 220, 410, 420))


class Renderer:
    """A ``_render_window`` stand-in: counts calls and can hang until released."""

    def __init__(self, image: Image.Image | None = None, *, hang: bool = False) -> None:
        self.image = image if image is not None else Image.new("RGB", (4, 3), "red")
        self.release = threading.Event()
        if not hang:
            self.release.set()
        self.calls: list[int] = []
        self.finished = threading.Event()
        self.error: type[BaseException] | None = None

    def __call__(self, window: WindowInfo) -> Image.Image | None:
        self.calls.append(window.hwnd)
        try:
            self.release.wait(timeout=30)
            if self.error is not None:
                raise self.error
            return self.image
        finally:
            self.finished.set()


@pytest.fixture
def gdi(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[GdiScreen, list[Renderer]]]:
    """A GdiScreen whose renders go to the last Renderer appended to the returned list."""
    capture = GdiScreen()
    renderers: list[Renderer] = []

    def render(window: WindowInfo) -> Image.Image | None:
        return renderers[-1](window)

    monkeypatch.setattr(capture, "_render_window", render)
    yield capture, renderers
    for renderer in renderers:
        renderer.release.set()  # never leave a render thread hanging past the test
    join_renders(capture)


def join_renders(capture: GdiScreen) -> None:
    for thread in list(capture._renders.values()):
        thread.join(timeout=10)


def test_a_responsive_window_is_returned(gdi: tuple[GdiScreen, list[Renderer]]) -> None:
    capture, renderers = gdi
    renderers.append(Renderer())
    image = capture.grab_window(WINDOW)
    assert image is renderers[0].image
    assert renderers[0].calls == [WINDOW.hwnd]


def test_a_failed_render_is_no_image(gdi: tuple[GdiScreen, list[Renderer]]) -> None:
    capture, renderers = gdi
    renderer = Renderer()
    renderer.error = OSError
    renderers.append(renderer)
    assert capture.grab_window(WINDOW) is None


def test_a_hung_window_costs_at_most_the_bound(gdi: tuple[GdiScreen, list[Renderer]]) -> None:
    capture, renderers = gdi
    renderers.append(Renderer(hang=True))
    started = time.monotonic()
    assert capture.grab_window(WINDOW) is None
    elapsed = time.monotonic() - started
    assert WINDOW_RENDER_WAIT_S * 0.9 <= elapsed < WINDOW_RENDER_WAIT_S + 2.0


def test_only_one_render_per_window_is_ever_outstanding(
    gdi: tuple[GdiScreen, list[Renderer]],
) -> None:
    capture, renderers = gdi
    hung = Renderer(hang=True)
    renderers.append(hung)
    assert capture.grab_window(WINDOW) is None  # waits out the bound
    before = threading.active_count()
    for _ in range(5):  # the next ticks while the app is still hung
        started = time.monotonic()
        assert capture.grab_window(WINDOW) is None
        assert time.monotonic() - started < WINDOW_RENDER_WAIT_S / 2  # no new wait either
    assert hung.calls == [WINDOW.hwnd]  # no second PrintWindow into the hung app
    assert threading.active_count() == before  # and no thread piling up


def test_late_pixels_are_discarded_not_returned_by_the_next_grab(
    gdi: tuple[GdiScreen, list[Renderer]],
) -> None:
    capture, renderers = gdi
    late = Renderer(Image.new("RGB", (4, 3), "blue"), hang=True)
    renderers.append(late)
    assert capture.grab_window(WINDOW) is None
    late.release.set()  # the app recovers and the old render completes
    assert wait_for(late.finished.is_set)
    join_renders(capture)
    fresh = Renderer(Image.new("RGB", (4, 3), "green"))
    renderers.append(fresh)
    image = capture.grab_window(WINDOW)
    assert image is fresh.image  # the fresh render, never the stale blue one
    assert fresh.calls == [WINDOW.hwnd]


# --- the crop from Win32 window bounds to DWM bounds ------------------------------------------


def _target(ref: object) -> Any:  # noqa: ANN401 - whatever ctypes.byref() wrapped
    return cast("Any", ref)._obj  # ctypes' CArgObject


class FakeUser32:
    """GetWindowRect reports ``rect``; PrintWindow paints each pixel with its own coordinates."""

    def __init__(self, rect: tuple[int, int, int, int], gdi32: FakeGdi32) -> None:
        self.rect = rect
        self.gdi32 = gdi32
        self.print_ok = True
        self.released_dcs = 0
        self.printed: list[tuple[int, int]] = []

    def GetWindowRect(self, hwnd: int, ref: object) -> int:  # noqa: N802 - Win32 name
        del hwnd
        rect = _target(ref)
        rect.left, rect.top, rect.right, rect.bottom = self.rect
        return 1

    def GetDC(self, hwnd: object) -> int:  # noqa: N802
        del hwnd
        return 100

    def ReleaseDC(self, hwnd: object, dc: int) -> int:  # noqa: N802
        del hwnd, dc
        self.released_dcs += 1
        return 1

    def PrintWindow(self, hwnd: int, dc: int, flags: int) -> int:  # noqa: N802
        del dc
        self.printed.append((hwnd, flags))
        if not self.print_ok:
            return 0
        buffer, width, height = self.gdi32.buffer, self.gdi32.width, self.gdi32.height
        assert buffer is not None
        if width * height > 1_000_000:  # a per-pixel Python loop is too slow at this size
            ctypes.memset(buffer, 0x40, width * height * 4)
            return 1
        for y in range(height):
            for x in range(width):
                offset = (y * width + x) * 4
                buffer[offset : offset + 4] = bytes((x % 256, y % 256, 7, 0))  # B, G, R, X
        return 1


class FakeGdi32:
    def __init__(self) -> None:
        self.buffer: ctypes.Array[ctypes.c_char] | None = None
        self.width = self.height = 0
        self.dibs = 0
        self.deleted_objects = 0
        self.deleted_dcs = 0

    def CreateCompatibleDC(self, dc: int) -> int:  # noqa: N802
        del dc
        return 200

    def CreateDIBSection(  # noqa: N802, PLR0913, PLR0917 - Win32 signature
        self, dc: int, header: object, usage: int, bits: object, section: object, offset: int
    ) -> int:
        del dc, usage, section, offset
        info = _target(header)
        self.width, self.height = info.biWidth, -info.biHeight
        self.buffer = ctypes.create_string_buffer(self.width * self.height * 4)
        _target(bits).value = ctypes.addressof(self.buffer)
        self.dibs += 1
        return 300

    def SelectObject(self, dc: int, obj: int) -> int:  # noqa: N802
        del dc, obj
        return 400

    def DeleteObject(self, obj: int) -> int:  # noqa: N802
        del obj
        self.deleted_objects += 1
        return 1

    def DeleteDC(self, dc: int) -> int:  # noqa: N802
        del dc
        self.deleted_dcs += 1
        return 1


@pytest.fixture
def win32(monkeypatch: pytest.MonkeyPatch) -> Callable[[tuple[int, int, int, int]], FakeUser32]:
    def install(rect: tuple[int, int, int, int]) -> FakeUser32:
        gdi32 = FakeGdi32()
        user32 = FakeUser32(rect, gdi32)
        monkeypatch.setattr(win_screen, "user32", user32)
        monkeypatch.setattr(win_screen, "gdi32", gdi32)
        return user32

    return install


def render(window: WindowInfo) -> Image.Image | None:
    return GdiScreen._render_window(window)  # the unit under test


def test_the_render_is_cropped_to_the_dwm_bounds(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    # Win32 bounds include the invisible resize border around the DWM (visible) bounds.
    user32 = win32((100, 200, 420, 430))
    image = render(WINDOW)
    assert image is not None
    assert image.size == (WINDOW.rect.width, WINDOW.rect.height)
    # The top-left pixel is the one PrintWindow drew at (110 - 100, 220 - 200).
    assert image.getpixel((0, 0)) == (7, 20, 10)
    assert image.getpixel((299, 199)) == (7, (20 + 199) % 256, (10 + 299) % 256)
    assert user32.printed == [(WINDOW.hwnd, 2)]  # PW_RENDERFULLCONTENT
    gdi32 = user32.gdi32
    assert (gdi32.deleted_objects, gdi32.deleted_dcs, user32.released_dcs) == (1, 1, 1)


@pytest.mark.parametrize(
    "rect",
    [
        (120, 200, 420, 430),  # DWM left edge outside the Win32 rect (moved mid-capture)
        (100, 230, 420, 430),  # top outside
        (100, 200, 400, 430),  # right outside
        (100, 200, 420, 410),  # bottom outside
    ],
)
def test_bounds_that_do_not_fit_the_render_give_no_image(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32], rect: tuple[int, int, int, int]
) -> None:
    user32 = win32(rect)
    assert render(WINDOW) is None
    gdi32 = user32.gdi32
    assert (gdi32.deleted_objects, gdi32.deleted_dcs, user32.released_dcs) == (1, 1, 1)


@pytest.mark.parametrize(
    "rect",
    [
        (100, 200, 100, 430),  # zero width
        (100, 200, 420, 150),  # negative height (mid-resize)
        (0, 0, 10_000, 5_001),  # more than the 50 megapixel allocation cap
        (0, 0, 100_000, 100_000),
    ],
)
def test_malformed_or_huge_windows_are_refused_before_allocating(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32], rect: tuple[int, int, int, int]
) -> None:
    user32 = win32(rect)
    assert render(WINDOW) is None
    assert user32.gdi32.dibs == 0
    assert user32.printed == []
    gdi32 = user32.gdi32
    assert (gdi32.deleted_objects, gdi32.deleted_dcs, user32.released_dcs) == (0, 0, 0)


def test_a_window_that_refuses_to_print_gives_no_image(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    user32 = win32((100, 200, 420, 430))
    user32.print_ok = False
    assert render(WINDOW) is None
    gdi32 = user32.gdi32
    assert (gdi32.deleted_objects, gdi32.deleted_dcs, user32.released_dcs) == (1, 1, 1)


def test_a_hung_render_blocks_only_its_own_window(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    hung = WindowInfo(1, "app.exe", "Frozen app", WINDOW.rect)
    healthy = WindowInfo(2, "app.exe", "Responsive app", WINDOW.rect)

    def render(target: WindowInfo) -> Image.Image | None:
        if target.hwnd == hung.hwnd:
            release.wait(30)
            return None
        return Image.new("RGB", (40, 30), "red")

    monkeypatch.setattr(GdiScreen, "_render_window", staticmethod(render))
    grabber = GdiScreen()
    try:
        assert grabber.grab_window(hung) is None  # bounded: gives up after about one second
        image = grabber.grab_window(healthy)
        assert image is not None
        assert image.size == (40, 30)
    finally:
        release.set()


def test_a_hung_render_is_refused_right_away_and_never_re_rendered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    calls: list[int] = []
    hung = WindowInfo(1, "app.exe", "Frozen app", WINDOW.rect)
    healthy = WindowInfo(2, "app.exe", "Responsive app", WINDOW.rect)

    def render(target: WindowInfo) -> Image.Image | None:
        calls.append(target.hwnd)
        if target.hwnd == hung.hwnd:
            release.wait(30)
            return None
        return Image.new("RGB", (40, 30), "red")

    monkeypatch.setattr(GdiScreen, "_render_window", staticmethod(render))
    grabber = GdiScreen()
    try:
        assert grabber.grab_window(hung) is None
        for _ in range(5):
            started = time.monotonic()
            assert grabber.grab_window(hung) is None
            assert time.monotonic() - started < WINDOW_RENDER_WAIT_S / 2
            image = grabber.grab_window(healthy)  # the other window keeps working
            assert image is not None
            assert image.size == (40, 30)
        assert calls.count(hung.hwnd) == 1  # no new PrintWindow into the hung app
    finally:
        release.set()
        join_renders(grabber)


def test_the_hung_window_is_rendered_again_once_its_render_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    state = {"hang": True}

    def render(target: WindowInfo) -> Image.Image | None:
        del target
        if state["hang"]:
            release.wait(30)
            return Image.new("RGB", (4, 3), "blue")  # late pixels
        return Image.new("RGB", (4, 3), "green")

    monkeypatch.setattr(GdiScreen, "_render_window", staticmethod(render))
    grabber = GdiScreen()
    assert grabber.grab_window(WINDOW) is None
    state["hang"] = False
    release.set()
    join_renders(grabber)
    image = grabber.grab_window(WINDOW)
    assert image is not None
    assert image.getpixel((0, 0)) == (0, 128, 0)  # the fresh render, not the late blue one


def test_outstanding_renders_have_a_fixed_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(win_screen, "WINDOW_RENDER_WAIT_S", 0.05)
    release = threading.Event()
    calls: list[int] = []

    def render(target: WindowInfo) -> Image.Image | None:
        calls.append(target.hwnd)
        if target.hwnd < 100:
            release.wait(30)
            return None
        return Image.new("RGB", (4, 3), "red")

    monkeypatch.setattr(GdiScreen, "_render_window", staticmethod(render))
    grabber = GdiScreen()
    cap = win_screen._MAX_PENDING_RENDERS
    try:
        for hwnd in range(1, cap + 1):  # fill the cap with hung windows
            assert grabber.grab_window(WindowInfo(hwnd, "a.exe", "A", WINDOW.rect)) is None
        before = threading.active_count()
        started = time.monotonic()
        assert grabber.grab_window(WindowInfo(500, "a.exe", "Past", WINDOW.rect)) is None
        assert grabber.grab_window(WindowInfo(100, "b.exe", "Healthy", WINDOW.rect)) is None
        assert time.monotonic() - started < 0.04  # refused right away, no wait
        assert 500 not in calls
        assert 100 not in calls  # past the cap even a healthy window is not rendered
        assert threading.active_count() == before
        release.set()
        join_renders(grabber)
        image = grabber.grab_window(WindowInfo(100, "b.exe", "Healthy", WINDOW.rect))
        assert image is not None  # the cap frees up when the hung renders end
    finally:
        release.set()
        join_renders(grabber)


def test_healthy_windows_never_fill_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        GdiScreen, "_render_window", staticmethod(lambda _w: Image.new("RGB", (4, 3), "red"))
    )
    grabber = GdiScreen()
    for hwnd in range(1, 4 * win_screen._MAX_PENDING_RENDERS):
        assert grabber.grab_window(WindowInfo(hwnd, "a.exe", "A", WINDOW.rect)) is not None


def test_a_failed_render_frees_its_window_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes: list[object] = [OSError, "ok"]

    def render(target: WindowInfo) -> Image.Image | None:
        del target
        outcome = outcomes.pop(0)
        if outcome is OSError:
            raise OSError
        return Image.new("RGB", (4, 3), "red")

    monkeypatch.setattr(GdiScreen, "_render_window", staticmethod(render))
    grabber = GdiScreen()
    assert grabber.grab_window(WINDOW) is None
    join_renders(grabber)
    assert grabber.grab_window(WINDOW) is not None


# --- windows larger than 16 megapixels are shrunk, not dropped --------------------------------

MAX_PIXELS = win_screen._MAX_WINDOW_PIXELS


def big_window(width: int, height: int) -> WindowInfo:
    return WindowInfo(42, "app.exe", "Big", Rect(0, 0, width, height))


def test_a_window_over_16_megapixels_is_downscaled_keeping_its_aspect_ratio(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    user32 = win32((0, 0, 5000, 4000))  # 20 MP
    image = render(big_window(5000, 4000))
    assert image is not None
    assert image.width * image.height <= MAX_PIXELS
    assert image.width * image.height > MAX_PIXELS * 0.95  # shrunk to fit, not to a thumbnail
    assert abs(image.width / image.height - 5000 / 4000) < 0.01
    gdi32 = user32.gdi32
    assert (gdi32.deleted_objects, gdi32.deleted_dcs, user32.released_dcs) == (1, 1, 1)


def test_an_extreme_aspect_ratio_over_the_bound_keeps_both_sides(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    win32((0, 0, 20_000, 1_000))  # 20 MP, a very wide strip
    image = render(big_window(20_000, 1_000))
    assert image is not None
    assert image.width * image.height <= MAX_PIXELS
    assert image.height >= 1
    assert abs(image.width / image.height - 20) < 0.5


def test_a_window_of_exactly_16_megapixels_is_returned_unscaled(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    win32((0, 0, 4000, 4000))
    image = render(big_window(4000, 4000))
    assert image is not None
    assert image.size == (4000, 4000)


def test_one_pixel_over_16_megapixels_is_shrunk_to_fit(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    win32((0, 0, 8001, 2000))  # 16.002 MP
    image = render(big_window(8001, 2000))
    assert image is not None
    assert image.width * image.height <= MAX_PIXELS


def test_a_big_render_cropped_below_the_bound_is_not_scaled(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    win32((0, 0, 5000, 4000))  # a 20 MP render whose DWM bounds are small
    image = render(WindowInfo(42, "app.exe", "Small", Rect(10, 10, 310, 210)))
    assert image is not None
    assert image.size == (300, 200)


def test_a_render_up_to_the_allocation_cap_is_allowed_and_resources_are_freed(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    user32 = win32((0, 0, 6000, 6000))  # 36 MP: above the output bound, below the cap
    image = render(big_window(6000, 6000))
    assert image is not None
    assert image.width * image.height <= MAX_PIXELS
    assert user32.gdi32.dibs == 1


def test_resources_are_freed_when_a_large_render_is_rejected_after_printing(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    user32 = win32((0, 0, 5000, 4000))
    user32.print_ok = False
    assert render(big_window(5000, 4000)) is None
    gdi32 = user32.gdi32
    assert (gdi32.deleted_objects, gdi32.deleted_dcs, user32.released_dcs) == (1, 1, 1)
