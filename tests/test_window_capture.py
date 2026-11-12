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
from defects import known_defect
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
    thread = capture._window_thread
    if thread is not None:
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


def test_only_one_render_is_ever_outstanding(gdi: tuple[GdiScreen, list[Renderer]]) -> None:
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
    thread = capture._window_thread
    assert thread is not None
    thread.join(timeout=10)
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
        (0, 0, 5000, 4000),  # more than 16 megapixels
    ],
)
def test_malformed_or_huge_windows_are_refused_before_allocating(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32], rect: tuple[int, int, int, int]
) -> None:
    user32 = win32(rect)
    assert render(WINDOW) is None
    assert user32.gdi32.dibs == 0
    assert user32.printed == []


def test_a_window_that_refuses_to_print_gives_no_image(
    win32: Callable[[tuple[int, int, int, int]], FakeUser32],
) -> None:
    user32 = win32((100, 200, 420, 430))
    user32.print_ok = False
    assert render(WINDOW) is None
    gdi32 = user32.gdi32
    assert (gdi32.deleted_objects, gdi32.deleted_dcs, user32.released_dcs) == (1, 1, 1)


@known_defect(158, "while one PrintWindow render hangs, every other window returns None")
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
