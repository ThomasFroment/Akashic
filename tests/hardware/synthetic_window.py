"""A disposable native window for the exclusion checks (shows nothing but its own text).

    python synthetic_window.py TITLE X Y WIDTH HEIGHT [KIND [PARENT_HWND]]

Creates one top-level window at physical pixel coordinates (per-monitor DPI aware, so X/Y/size are
virtual-screen pixels, negative X allowed), prints the window handle, then pumps messages for 120
seconds or until closed. KIND:

    plain (default), password  an EDIT child that has the focus (password-masked for password)
    none                       no child
    tool                       WS_EX_TOOLWINDOW
    popup                      plus an owned WS_POPUP window; its handle is printed second
    layered0, layered128       WS_EX_LAYERED with constant alpha 0 or 128
    cloaked                    cloaked by DWM
    minimized                  minimized
    hidden                     never shown
    hang                       stops pumping messages after about 1 second
    child                      no top-level window: a child of PARENT_HWND, which may belong to
                               another process
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from akashic.capture.ports import Rect

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x80
WS_EX_LAYERED = 0x80000
LWA_ALPHA = 2
SW_SHOWMINNOACTIVE = 7
DWMWA_CLOAK = 13
HANG_AFTER_MS = 1000
SAFETY_TIMER = 1
HANG_TIMER = 2
HANG_DURATION_S = 120
LAYERED_ALPHA = {"layered0": 0, "layered128": 128}
EDIT_KINDS = ("plain", "password")
ES_PASSWORD = 0x20
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_SETFOCUS = 0x0007
WM_ACTIVATE = 0x0006
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = (
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    )


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND,
    wintypes.COLORREF,
    ctypes.c_ubyte,
    wintypes.DWORD,
]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
dwmapi.DwmSetWindowAttribute.argtypes = [
    wintypes.HWND,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]


def apply_kind(hwnd: int, kind: str) -> None:
    if kind in LAYERED_ALPHA:
        user32.SetLayeredWindowAttributes(hwnd, 0, LAYERED_ALPHA[kind], LWA_ALPHA)
    if kind == "cloaked":
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CLOAK, ctypes.byref(wintypes.BOOL(1)), 4)
    if kind == "minimized":
        user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
    if kind == "hang":
        user32.SetTimer(hwnd, HANG_TIMER, HANG_AFTER_MS, None)


def main() -> int:
    title, x, y, w, h = sys.argv[1], *(int(v) for v in sys.argv[2:6])
    kind = sys.argv[6] if len(sys.argv) > 6 else "plain"
    parent = int(sys.argv[7]) if kind == "child" else None
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    child: list[int] = []

    def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if child and (msg == WM_SETFOCUS or (msg == WM_ACTIVATE and wparam & 0xFFFF)):
            user32.SetFocus(child[0])  # keyboard focus belongs to the edit, whenever we get it
            return 0
        if msg == WM_TIMER and wparam == HANG_TIMER:
            user32.KillTimer(hwnd, HANG_TIMER)  # one-shot: a second tick would pump the queue
            time.sleep(HANG_DURATION_S)  # the UI thread stops answering until the process is killed
            return 0
        if msg == WM_TIMER:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    keep = WNDPROC(proc)
    cls = WNDCLASSW()
    cls.lpfnWndProc = keep
    cls.hInstance = kernel32.GetModuleHandleW(None)
    cls.hbrBackground = wintypes.HBRUSH(6)  # COLOR_WINDOW + 1
    cls.lpszClassName = "AkashicM01Synthetic"
    user32.RegisterClassW(ctypes.byref(cls))
    visible = 0 if kind == "hidden" else WS_VISIBLE
    exstyle = WS_EX_TOOLWINDOW if kind == "tool" else WS_EX_LAYERED if kind in LAYERED_ALPHA else 0
    if parent is None:
        hwnd = user32.CreateWindowExW(
            exstyle, cls.lpszClassName, title, WS_OVERLAPPEDWINDOW | visible, x, y, w, h,
            None, None, cls.hInstance, None,
        )  # fmt: skip
    else:
        hwnd = user32.CreateWindowExW(
            0, cls.lpszClassName, title, WS_CHILD | WS_VISIBLE, x, y, w, h,
            parent, None, cls.hInstance, None,
        )  # fmt: skip
    apply_kind(hwnd, kind)
    if kind in EDIT_KINDS:
        style = WS_CHILD | WS_VISIBLE | WS_BORDER | (ES_PASSWORD if kind == "password" else 0)
        edit = user32.CreateWindowExW(
            0, "EDIT", "", style, 10, 10, 300, 28, hwnd, None, cls.hInstance, None
        )
        child.append(edit)
        user32.SetFocus(edit)
    print(int(hwnd or 0), flush=True)  # noqa: T201 - the handle goes to the parent test
    if kind == "popup":
        owned = user32.CreateWindowExW(
            0, cls.lpszClassName, title, WS_POPUP | WS_VISIBLE, x + 20, y + 20, w // 2, h // 2,
            hwnd, None, cls.hInstance, None,
        )  # fmt: skip
        print(int(owned or 0), flush=True)  # noqa: T201
    user32.SetTimer(hwnd, SAFETY_TIMER, 120_000, None)  # close after two minutes
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


def right_monitor() -> Rect:
    """The non-primary monitor with left > 0: the one that does not face the user."""
    from akashic.capture.ports import Rect  # noqa: PLC0415

    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    found: list[Rect] = []
    proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def visit(handle: int, hdc: int, rect: ctypes._Pointer[wintypes.RECT], param: int) -> bool:
        del handle, hdc, param
        r = rect.contents
        found.append(Rect(r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, proc_type(visit), 0)
    right = [m for m in found if m.left > 0]
    if not right:
        import pytest  # noqa: PLC0415

        pytest.skip("no monitor with left > 0")
    return min(right, key=lambda m: m.left)


def on_right_monitor(dx: int, dy: int, width: int, height: int) -> Rect:
    """A rectangle at an offset inside the right-hand monitor."""
    from akashic.capture.ports import Rect  # noqa: PLC0415

    m = right_monitor()
    return Rect(m.left + dx, m.top + dy, m.left + dx + width, m.top + dy + height)


def launch_window(
    title: str, rect: Rect, kind: str, parent: int | None = None
) -> tuple[subprocess.Popen[str], int]:
    """Start this script as a window of ``kind``; returns it and its first printed handle."""
    process = subprocess.Popen(  # noqa: S603 - our own helper script, synthetic content only
        [
            sys.executable,
            __file__,
            title,
            str(rect.left),
            str(rect.top),
            str(rect.width),
            str(rect.height),
            kind,
            *([] if parent is None else [str(parent)]),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    return process, int(process.stdout.readline().strip())


def bring_to_front(hwnd: int) -> bool:
    """SetForegroundWindow is refused for background processes; attach input queues to get it."""
    for _ in range(3):
        foreground = user32.GetForegroundWindow()
        this_thread = kernel32.GetCurrentThreadId()
        other = user32.GetWindowThreadProcessId(foreground, None)
        user32.keybd_event(0x87, 0, 0, 0)  # F24, bound to nothing: any input lifts the lock
        user32.keybd_event(0x87, 0, 2, 0)
        user32.AttachThreadInput(this_thread, other, True)  # noqa: FBT003
        try:
            user32.ShowWindow(hwnd, 5)
            user32.SetForegroundWindow(hwnd)
        finally:
            user32.AttachThreadInput(this_thread, other, False)  # noqa: FBT003
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if user32.GetForegroundWindow() == hwnd:
                time.sleep(0.8)  # keyboard focus follows the foreground window a moment later
                return True
            time.sleep(0.05)
    return False


if __name__ == "__main__":
    sys.exit(main())
