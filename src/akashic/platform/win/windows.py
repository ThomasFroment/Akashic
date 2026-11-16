# pyright: basic
"""Foreground window (with the D3D fullscreen state), every visible top-level window with its
facts (minimized, cloaked, owner, styles, constant alpha, hosted UWP process, foreign children,
ghost), cursor and session-lock facts (ctypes, no deps)."""

from __future__ import annotations

import ctypes
import dataclasses
import time
from ctypes import wintypes

from akashic.capture.ports import FRAME_HOST, Rect, TopWindow, WindowInfo
from akashic.platform.win.process import image_name

user32 = ctypes.WinDLL("user32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
shell32 = ctypes.WinDLL("shell32")

_GW_OWNER = 4
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_POPUP = 0x80000000
_WS_EX_TOOLWINDOW = 0x80
_WS_EX_LAYERED = 0x80000
_LWA_ALPHA = 2
_DWMWA_CLOAKED = 14
_GHOST_CLASS = "Ghost"
_DWM = "dwm.exe"
_WS_CAPTION = 0x00C00000  # title bar + border; borderless and fullscreen windows lack it
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_DESKTOP_SWITCHDESKTOP = 0x100
_CORE_WINDOW_CLASS = "Windows.UI.Core.CoreWindow"
_QUNS_RUNNING_D3D_FULL_SCREEN = 3
_D3D_CACHE_S = 1.0  # the focus poll runs ~10 Hz; the shell query need not
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetForegroundWindow.restype = wintypes.HWND
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
user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetLayeredWindowAttributes.argtypes = [
    wintypes.HWND,
    ctypes.POINTER(wintypes.COLORREF),
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.POINTER(wintypes.DWORD),
]
user32.IsHungAppWindow.argtypes = [wintypes.HWND]
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
shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
shell32.SHQueryUserNotificationState.restype = ctypes.c_long  # HRESULT, checked by hand

try:  # undocumented export; when absent no ghost is traced back to its window
    _hung_window_from_ghost = user32.HungWindowFromGhostWindow
    _hung_window_from_ghost.argtypes = [wintypes.HWND]
    _hung_window_from_ghost.restype = wintypes.HWND
except AttributeError:
    _hung_window_from_ghost = None


class Win32Windows:
    def __init__(self) -> None:
        self._d3d_fullscreen_value = False
        self._d3d_read_at = float("-inf")

    def focused_window(self) -> WindowInfo | None:
        hwnd = user32.GetForegroundWindow()
        if not hwnd or user32.IsIconic(hwnd):
            return None
        info = self._info(hwnd)
        if info is None:
            return None
        return dataclasses.replace(info, exclusive_fullscreen=self._d3d_fullscreen())

    def windows(self) -> list[TopWindow] | None:
        visible: list[int] = []

        def visit(hwnd: int, _param: int) -> bool:
            if user32.IsWindowVisible(hwnd):
                visible.append(hwnd)
            return True

        if not user32.EnumWindows(_WNDENUMPROC(visit), 0):
            return None
        return [w for hwnd in visible if (w := self._top_window(hwnd)) is not None]

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

    def _d3d_fullscreen(self) -> bool:
        now = time.monotonic()
        if now - self._d3d_read_at >= _D3D_CACHE_S:
            self._d3d_fullscreen_value = self._query_d3d_fullscreen()
            self._d3d_read_at = now
        return self._d3d_fullscreen_value

    @staticmethod
    def _query_d3d_fullscreen() -> bool:
        state = ctypes.c_int()
        try:
            hr = shell32.SHQueryUserNotificationState(ctypes.byref(state))
        except OSError:
            return False
        return hr == 0 and state.value == _QUNS_RUNNING_D3D_FULL_SCREEN

    def _info(self, hwnd: int) -> WindowInfo | None:
        rect = self._rect(hwnd)
        if rect is None:
            return None
        _, process, _ = self._identity(hwnd)
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE) & 0xFFFFFFFF
        return WindowInfo(int(hwnd), process, _title(hwnd), rect, _has_frame(style))

    def _top_window(self, hwnd: int) -> TopWindow | None:
        rect = self._rect(hwnd)
        if rect is None:
            return None
        pid, process, uwp_unresolved = self._identity(hwnd)
        class_name = _class_name(hwnd)
        style = user32.GetWindowLongW(hwnd, _GWL_STYLE) & 0xFFFFFFFF
        exstyle = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) & 0xFFFFFFFF
        is_ghost = class_name == _GHOST_CLASS or (
            process == _DWM and bool(user32.IsHungAppWindow(hwnd))
        )
        return TopWindow(
            hwnd=int(hwnd),
            pid=pid,
            process=process,
            uwp_unresolved=uwp_unresolved,
            class_name=class_name,
            title=_title(hwnd),
            rect=rect,
            minimized=bool(user32.IsIconic(hwnd)),
            cloaked=_cloaked(hwnd),
            owner=user32.GetWindow(hwnd, _GW_OWNER) or None,
            popup=bool(style & _WS_POPUP),
            tool=bool(exstyle & _WS_EX_TOOLWINDOW),
            alpha=_alpha(hwnd) if exstyle & _WS_EX_LAYERED else None,
            is_ghost=is_ghost,
            ghost_of=_ghost_of(hwnd) if is_ghost else None,
            foreign_children=self._foreign_children(hwnd, {_window_pid(hwnd), pid}),
            has_frame=_has_frame(style),
        )

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

    def _identity(self, hwnd: int) -> tuple[int, str, bool]:
        """(pid, executable, uwp_unresolved) behind a window; for a UWP app, the app's own and not
        its frame host's. A frame host with no app found keeps the host and is flagged."""
        pid = _window_pid(hwnd)
        name = image_name(pid)
        if name != FRAME_HOST:
            return pid, name, False
        hosted = self._hosted_pid(hwnd, pid)
        if hosted is None:
            return pid, name, True
        return hosted, image_name(hosted), False

    @staticmethod
    def _foreign_children(hwnd: int, own_pids: set[int]) -> tuple[str, ...] | None:
        """Sorted executable names of descendants owned by other processes; None when one of
        them cannot be named."""
        foreign: set[int] = set()

        def visit(child: int, _param: int) -> bool:
            pid = _window_pid(child)
            if pid not in own_pids:
                foreign.add(pid)
            return True

        user32.EnumChildWindows(hwnd, _WNDENUMPROC(visit), 0)
        names = {image_name(pid) for pid in foreign}
        return None if "" in names else tuple(sorted(names))

    @staticmethod
    def _hosted_pid(frame: int, host_pid: int) -> int | None:
        """PID of the CoreWindow child that belongs to the app, not to the frame host. None when
        there is none (the app is suspended or not yet shown): the caller keeps the host's name,
        and such a window is then judged unknown."""
        found: list[int] = []

        def visit(child: int, _param: int) -> bool:
            if (
                _class_name(child) == _CORE_WINDOW_CLASS
                and (owner := _window_pid(child)) != host_pid
            ):
                found.append(owner)
                return False
            return True

        user32.EnumChildWindows(frame, _WNDENUMPROC(visit), 0)
        return found[0] if found else None


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)  # a window class name is at most 256 characters
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _has_frame(style: int) -> bool:
    return style & _WS_CAPTION == _WS_CAPTION


def _cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD()
    hr = dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return hr == 0 and cloaked.value != 0


def _alpha(hwnd: int) -> int | None:
    alpha = ctypes.c_ubyte()
    flags = wintypes.DWORD()
    if not user32.GetLayeredWindowAttributes(hwnd, None, ctypes.byref(alpha), ctypes.byref(flags)):
        return None
    return alpha.value if flags.value & _LWA_ALPHA else None


def _ghost_of(hwnd: int) -> int | None:
    if _hung_window_from_ghost is None:
        return None
    return _hung_window_from_ghost(hwnd) or None
