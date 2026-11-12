# pyright: basic
"""Foreground window, window under a point, cursor and session-lock facts (ctypes, no deps)."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from akashic.capture.ports import Rect, WindowInfo
from akashic.platform.win.process import image_name

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

_GA_ROOT = 2
_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000  # title bar + border; borderless and fullscreen windows lack it
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DESKTOP_SWITCHDESKTOP = 0x100
_FRAME_HOST = "applicationframehost.exe"  # hosts every UWP app window
_CORE_WINDOW_CLASS = "Windows.UI.Core.CoreWindow"
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.IsIconic.argtypes = [wintypes.HWND]
user32.EnumChildWindows.argtypes = [wintypes.HWND, _WNDENUMPROC, wintypes.LPARAM]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]


class _LastInputInfo(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
user32.GetLastInputInfo.restype = wintypes.BOOL
dwmapi.DwmGetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]


class Win32Windows:
    def focused_window(self) -> WindowInfo | None:
        hwnd = user32.GetForegroundWindow()
        if not hwnd or user32.IsIconic(hwnd):
            return None
        return self._info(hwnd)

    def inspect_window(self, hwnd: int) -> WindowInfo | None:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None
        return self._info(hwnd)

    def window_at(self, x: int, y: int) -> WindowInfo | None:
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return None
        root = user32.GetAncestor(hwnd, _GA_ROOT)
        return self._info(root or hwnd)

    def cursor_pos(self) -> tuple[int, int]:
        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return point.x, point.y

    def last_input_ms(self) -> int:
        """Tick (ms since boot, wraps after 49 days) of the session's last keyboard or mouse
        input. Only *when*, never what: no key, no position."""
        info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0
        return int(info.dwTime)

    def is_locked(self) -> bool:
        # The input desktop can't be opened while the workstation is locked (the secure
        # desktop is showing), or during a UAC prompt.
        desktop = user32.OpenInputDesktop(0, 0, _DESKTOP_SWITCHDESKTOP)
        if not desktop:
            return True
        user32.CloseDesktop(desktop)
        return False

    def _info(self, hwnd: int) -> WindowInfo | None:
        rect = self._rect(hwnd)
        if rect is None:
            return None
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE) & 0xFFFFFFFF
        has_frame = style & _WS_CAPTION == _WS_CAPTION
        return WindowInfo(int(hwnd), self._process_name(hwnd), buffer.value, rect, has_frame)

    @staticmethod
    def _rect(hwnd: int) -> Rect | None:
        rect = wintypes.RECT()
        # Extended frame bounds exclude the invisible resize border and drop shadow.
        hr = dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        )
        if hr != 0 and not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return Rect(rect.left, rect.top, rect.right, rect.bottom)

    def _process_name(self, hwnd: int) -> str:
        """The executable behind a window; for a UWP app, the app's own and not its frame host."""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = image_name(pid.value)
        if name == _FRAME_HOST:
            hosted = self._hosted_pid(hwnd, pid.value)
            if hosted is not None:
                name = image_name(hosted) or name
        return name

    @staticmethod
    def _hosted_pid(frame: int, host_pid: int) -> int | None:
        """PID of the CoreWindow child that belongs to the app, not to the frame host. None when
        there is none (the app is suspended or not yet shown): the caller keeps the host's name,
        and the title rules still apply to such a window."""
        found: list[int] = []

        def visit(child: int, _param: int) -> bool:
            buffer = ctypes.create_unicode_buffer(len(_CORE_WINDOW_CLASS) + 2)
            if (
                user32.GetClassNameW(child, buffer, len(buffer))
                and buffer.value == _CORE_WINDOW_CLASS
            ):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(child, ctypes.byref(owner))
                if owner.value != host_pid:
                    found.append(owner.value)
                    return False
            return True

        user32.EnumChildWindows(frame, _WNDENUMPROC(visit), 0)
        return found[0] if found else None
