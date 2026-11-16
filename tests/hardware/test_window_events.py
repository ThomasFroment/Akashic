"""Real SetWinEventHook events from two synthetic helper windows.

    uv run pytest tests/hardware/test_window_events.py -m hardware --no-cov -s

Creates two visible windows, moves, hides, raises and closes them.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from synthetic_window import bring_to_front, right_monitor

from akashic.platform.win.events import Win32WindowEvents

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.hardware

HELPER = Path(__file__).with_name("synthetic_window.py")
user32 = ctypes.WinDLL("user32", use_last_error=True)

SW_HIDE, SW_SHOW, SW_MINIMIZE, SW_RESTORE = 0, 5, 6, 9
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
HWND_TOP = 0
WM_CLOSE = 0x0010
WAIT_S = 1.0

user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def spawn(title: str, x: int, y: int) -> tuple[subprocess.Popen[str], int]:
    area = right_monitor()
    proc = subprocess.Popen(  # noqa: S603 - our own helper script
        [
            sys.executable,
            str(HELPER),
            title,
            str(area.left + x),
            str(area.top + y),
            "500",
            "300",
            "plain",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    return proc, int(proc.stdout.readline())


@pytest.fixture
def windows() -> Iterator[tuple[int, int]]:
    low, low_hwnd = spawn("Akashic events low", 100, 100)
    high, high_hwnd = spawn("Akashic events high", 160, 160)
    time.sleep(0.5)
    yield low_hwnd, high_hwnd
    for proc in (low, high):
        proc.kill()
        proc.wait(5)


def test_real_window_events(windows: tuple[int, int]) -> None:
    low, high = windows
    area = right_monitor()
    source = Win32WindowEvents()
    source.start()
    try:
        source.drain()  # the first drain after start is unhealthy by design

        def expect(hwnd: int, change: str, action: Callable[[], object]) -> None:
            before = time.monotonic()
            action()
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline:
                got = source.drain()
                assert got.healthy, f"unhealthy during {change}"
                after = time.monotonic()
                hits = [e for e in got.events if e.hwnd == hwnd and e.change == change]
                if hits:
                    assert all(before <= e.at <= after for e in hits)
                    return
                time.sleep(0.02)
            pytest.fail(f"no {change} event for the helper window within {WAIT_S}s")

        def move() -> object:
            return user32.SetWindowPos(
                high, None, area.left + 200, area.top + 200, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE
            )

        def move_low() -> object:
            # A pure z-order raise sends no WinEvent for a top-level window, so move it instead.
            return user32.SetWindowPos(
                low, None, area.left + 120, area.top + 120, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE
            )

        expect(high, "moved", move)
        expect(high, "title", lambda: user32.SetWindowTextW(high, "Akashic events renamed"))
        expect(high, "hidden", lambda: user32.ShowWindow(high, SW_MINIMIZE))
        expect(high, "shown", lambda: user32.ShowWindow(high, SW_RESTORE))
        expect(high, "hidden", lambda: user32.ShowWindow(high, SW_HIDE))
        expect(high, "shown", lambda: user32.ShowWindow(high, SW_SHOW))
        expect(low, "moved", move_low)
        assert bring_to_front(low)  # so that high is not already the foreground window
        source.drain()
        expect(high, "focus", lambda: bring_to_front(high))
        expect(high, "destroyed", lambda: user32.PostMessageW(high, WM_CLOSE, 0, 0))
    finally:
        source.close()
