"""WgcCaptures on the real GPU. Only the device test is read-only; the others open windows.

Run: uv run pytest -m hardware --no-cov -s tests/hardware/test_wgc.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import numpy as np
import pytest
from synthetic_window import right_monitor

from akashic.platform.win.wgc import WgcCaptures

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.hardware

_SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE = 0x2, 0x4, 0x10
_FIRST_FRAME_SECONDS = 1.0
_VRAM_BUDGET_MB = 1500  # a guess for 32 small free-threaded 1-buffer pools; tighten once measured

# Prints the root hwnd of the main window and of each extra Toplevel, then optionally hangs.
_WINDOWS = (
    "import tkinter as t, ctypes, sys, time\n"
    "n = int(sys.argv[1]); hang = sys.argv[2] == 'hang'\n"
    "ox = int(sys.argv[3]); oy = int(sys.argv[4])\n"
    "ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))\n"
    "r = t.Tk(); r.title('Akashic WGC hardware test')\n"
    "r.geometry(f'400x300+{ox + 100}+{oy + 100}')\n"
    "r.configure(bg='red')\n"
    "wins = [r] + [t.Toplevel(r) for _ in range(n - 1)]\n"
    "for i, w in enumerate(wins[1:]):\n"
    "    w.geometry(f'300x200+{ox + 150 + i * 10}+{oy + 150 + i * 10}'); w.configure(bg='red')\n"
    "r.update()\n"
    "u = ctypes.windll.user32; u.GetAncestor.restype = ctypes.c_void_p\n"
    "u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]\n"
    "print(' '.join(str(u.GetAncestor(w.winfo_id(), 2)) for w in wins), flush=True)\n"
    "if hang: time.sleep(40)\n"
    "r.after(60000, r.destroy); r.mainloop()\n"
)


def _wait(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _spawn(count: int = 1, mode: str = "live") -> Iterator[tuple[subprocess.Popen[str], list[int]]]:
    area = right_monitor()
    process = subprocess.Popen(  # noqa: S603 - owned synthetic windows only
        [sys.executable, "-c", _WINDOWS, str(count), mode, str(area.left), str(area.top)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        yield process, [int(h) for h in process.stdout.readline().split()]
    finally:
        process.terminate()
        process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()


@pytest.fixture
def window() -> Iterator[int]:
    for _process, hwnds in _spawn():
        yield hwnds[0]


@pytest.fixture
def captures() -> Iterator[WgcCaptures]:
    wgc_captures = WgcCaptures()
    yield wgc_captures
    wgc_captures.close()


def _vram_used_mb() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout
    return int(out.splitlines()[0])


def test_imports_work_and_a_d3d_device_is_created_without_any_window() -> None:
    captures = WgcCaptures()
    try:
        assert captures._device is not None
        assert captures.failed() == frozenset()
    finally:
        captures.close()


def test_a_real_window_is_captured_without_border_or_cursor(
    window: int, captures: WgcCaptures
) -> None:
    assert captures.start(window, 0)
    session = captures._captures[window].session
    assert session.is_border_required is False
    assert session.is_cursor_capture_enabled is False
    assert _wait(lambda: captures.latest_seq(window) is not None)
    frame = captures.grab(window)
    assert frame is not None
    assert frame.image.mode == "RGB"
    red, green, blue = np.asarray(frame.image)[frame.image.height - 20, frame.image.width - 20]
    assert red > 200
    assert green < 30
    assert blue < 30


def test_first_frame_arrives_quickly(window: int, captures: WgcCaptures) -> None:
    assert captures.start(window, 0)
    assert _wait(lambda: captures.latest_seq(window) is not None, _FIRST_FRAME_SECONDS)


def test_resizing_the_window_recreates_the_pool_and_the_native_size_follows(
    window: int, captures: WgcCaptures
) -> None:
    assert captures.start(window, 0)
    assert _wait(lambda: captures.latest_seq(window) is not None)
    first = captures.grab(window)
    assert first is not None
    user32 = ctypes.windll.user32
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 5
    flags = _SWP_NOMOVE | _SWP_NOZORDER | _SWP_NOACTIVATE
    assert user32.SetWindowPos(window, None, 0, 0, 700, 500, flags)

    def resized() -> bool:
        frame = captures.grab(window)
        return frame is not None and frame.image.size != first.image.size

    assert _wait(resized)
    assert captures.failed() == frozenset()


def test_a_hung_app_keeps_its_last_frame_and_is_not_failed(captures: WgcCaptures) -> None:
    for _process, hwnds in _spawn(mode="hang"):
        assert captures.start(hwnds[0], 0)
        assert _wait(lambda hwnd=hwnds[0]: captures.latest_seq(hwnd) is not None)
        time.sleep(3)
        assert captures.failed() == frozenset()
        assert captures.grab(hwnds[0]) is not None


def test_32_captures_stay_within_the_vram_budget(captures: WgcCaptures) -> None:
    before = _vram_used_mb()
    for _process, hwnds in _spawn(count=32):
        assert all(captures.start(h, 100) for h in hwnds)
        assert _wait(lambda hs=hwnds: all(captures.latest_seq(h) is not None for h in hs), 15)
        used = _vram_used_mb() - before
        assert used < _VRAM_BUDGET_MB, f"32 captures used {used} MB of VRAM"
