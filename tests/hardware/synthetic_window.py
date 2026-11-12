"""A disposable native window for the exclusion checks (shows nothing but its own text).

    python synthetic_window.py TITLE X Y WIDTH HEIGHT [password|plain|none]

Creates one top-level window at physical pixel coordinates (per-monitor DPI aware, so X/Y/size are
virtual-screen pixels, negative X allowed) with an optional EDIT child that has the focus, prints
the window handle, then pumps messages for 120 seconds or until closed.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_BORDER = 0x00800000
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
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]


def main() -> int:
    title, x, y, w, h = sys.argv[1], *(int(v) for v in sys.argv[2:6])
    kind = sys.argv[6] if len(sys.argv) > 6 else "plain"
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    child: list[int] = []

    def proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if child and (msg == WM_SETFOCUS or (msg == WM_ACTIVATE and wparam & 0xFFFF)):
            user32.SetFocus(child[0])  # keyboard focus belongs to the edit, whenever we get it
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
    hwnd = user32.CreateWindowExW(
        0, cls.lpszClassName, title, WS_OVERLAPPEDWINDOW | WS_VISIBLE, x, y, w, h,
        None, None, cls.hInstance, None,
    )  # fmt: skip
    if kind != "none":
        style = WS_CHILD | WS_VISIBLE | WS_BORDER | (ES_PASSWORD if kind == "password" else 0)
        edit = user32.CreateWindowExW(
            0, "EDIT", "", style, 10, 10, 300, 28, hwnd, None, cls.hInstance, None
        )
        child.append(edit)
        user32.SetFocus(edit)
    print(int(hwnd or 0), flush=True)  # noqa: T201 - the handle goes to the parent test
    user32.SetTimer(hwnd, 1, 120_000, None)  # the safety net: close after two minutes
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
