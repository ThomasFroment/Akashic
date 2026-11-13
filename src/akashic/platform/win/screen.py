# pyright: basic
"""On-demand selected-window rendering and monitor enumeration through Windows GDI.

Production capture uses PrintWindow, with bounded waiting and no desktop-pixel fallback.
One hung app blocks only its own windows.
The rectangular BitBlt helper remains for hardware checks. Smoke test: tests/manual/capture.md.
"""

from __future__ import annotations

import ctypes
import math
import queue
import threading
from ctypes import wintypes
from typing import Final

from PIL import Image

from akashic.capture.ports import Rect, WindowInfo

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_SRCCOPY = 0x00CC0020
_CAPTUREBLT = 0x40000000  # include layered (transparent) windows
_DIB_RGB_COLORS = 0
_PER_MONITOR_AWARE_V2 = -4
_MAX_WINDOW_PIXELS = 16_000_000  # bound on the returned image; larger ones are shrunk
_MAX_RENDER_PIXELS = 50_000_000  # bound on the allocation; larger rects are malformed
_MAX_PENDING_RENDERS = 4
WINDOW_RENDER_WAIT_S: Final = 1.0

_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)

user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
user32.EnumDisplayMonitors.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.RECT),
    _MONITORENUMPROC,
    wintypes.LPARAM,
]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HANDLE]
user32.GetDC.restype = wintypes.HANDLE
user32.ReleaseDC.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HANDLE]
gdi32.CreateCompatibleDC.restype = wintypes.HANDLE
gdi32.CreateDIBSection.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.BitBlt.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.DWORD,
]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HANDLE]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


def set_dpi_aware() -> None:
    """Make every coordinate in this process physical pixels. Call before any window API."""
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR_AWARE_V2))


def _fit_output_bound(image: Image.Image) -> Image.Image:
    pixels = image.width * image.height
    if pixels <= _MAX_WINDOW_PIXELS:
        return image
    scale = math.sqrt(_MAX_WINDOW_PIXELS / pixels)
    size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    return image.resize(size, Image.Resampling.BILINEAR, reducing_gap=2.0)


class GdiScreen:
    def __init__(self) -> None:
        self._renders: dict[int, threading.Thread] = {}

    def grab_window(self, window: WindowInfo) -> Image.Image | None:
        """PrintWindow renders the selected HWND, including behind an occluder.

        The owning app can hang inside PrintWindow. One daemon thread per HWND bounds caller
        wait; a hung window is refused until its render ends, so it blocks only itself, and
        the number of outstanding renders is capped. Late pixels are discarded, never reused.
        """
        self._renders = {h: t for h, t in self._renders.items() if t.is_alive()}
        if window.hwnd in self._renders or len(self._renders) >= _MAX_PENDING_RENDERS:
            return None
        result: queue.Queue[Image.Image | None] = queue.Queue(maxsize=1)

        def read() -> None:
            try:
                result.put(self._render_window(window))
            except OSError:
                result.put(None)

        render = threading.Thread(target=read, daemon=True, name="window-pixels")
        self._renders[window.hwnd] = render
        render.start()
        try:
            image = result.get(timeout=WINDOW_RENDER_WAIT_S)
        except queue.Empty:
            return None  # stays in _renders until the render thread ends
        del self._renders[window.hwnd]  # finished: the thread may still be exiting
        return image

    @staticmethod
    def _render_window(window: WindowInfo) -> Image.Image | None:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(window.hwnd, ctypes.byref(rect)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        # Bound a malformed/during-resize target before allocating.
        if width <= 0 or height <= 0 or width * height > _MAX_RENDER_PIXELS:
            return None
        header = _BitmapInfoHeader()
        header.biSize = ctypes.sizeof(header)
        header.biWidth, header.biHeight = width, -height
        header.biPlanes, header.biBitCount = 1, 32
        dc = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(dc)
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        previous = None
        try:
            if not bitmap or not bits.value or not memory:
                return None
            previous = gdi32.SelectObject(memory, bitmap)
            ctypes.memset(bits, 0, width * height * 4)
            if not user32.PrintWindow(window.hwnd, memory, 2):  # PW_RENDERFULLCONTENT
                return None
            raw = ctypes.string_at(bits.value, width * height * 4)
            image = Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
            # WindowInfo uses DWM bounds (no invisible resize border); PrintWindow uses Win32.
            bounds = window.rect
            if not (
                rect.left <= bounds.left < bounds.right <= rect.right
                and rect.top <= bounds.top < bounds.bottom <= rect.bottom
            ):
                return None
            cropped = image.crop(
                (
                    bounds.left - rect.left,
                    bounds.top - rect.top,
                    bounds.right - rect.left,
                    bounds.bottom - rect.top,
                )
            )
            return _fit_output_bound(cropped)
        finally:
            if previous:
                gdi32.SelectObject(memory, previous)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory:
                gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, dc)

    def monitors(self) -> list[Rect]:
        found: list[Rect] = []

        def callback(
            _monitor: int, _dc: int, rect: ctypes._Pointer[wintypes.RECT], _l: int
        ) -> bool:
            r = rect.contents
            found.append(Rect(r.left, r.top, r.right, r.bottom))
            return True

        user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(callback), 0)
        return sorted(found, key=lambda m: (m.left, m.top))

    def grab(self, rect: Rect) -> Image.Image:
        width, height = rect.width, rect.height
        header = _BitmapInfoHeader()
        header.biSize = ctypes.sizeof(_BitmapInfoHeader)
        header.biWidth = width
        header.biHeight = -height  # top-down rows
        header.biPlanes = 1
        header.biBitCount = 32
        screen_dc = user32.GetDC(None)
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            screen_dc, ctypes.byref(header), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0
        )
        try:
            if not bitmap or not bits.value:
                msg = "CreateDIBSection failed"
                raise OSError(msg)
            previous = gdi32.SelectObject(memory_dc, bitmap)
            ok = gdi32.BitBlt(
                memory_dc, 0, 0, width, height, screen_dc, rect.left, rect.top,
                _SRCCOPY | _CAPTUREBLT,
            )  # fmt: skip
            gdi32.SelectObject(memory_dc, previous)
            if not ok:
                msg = "BitBlt failed"
                raise OSError(msg)
            raw = ctypes.string_at(bits.value, width * height * 4)
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)
        return Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
