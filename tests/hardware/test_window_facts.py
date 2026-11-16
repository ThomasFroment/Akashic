"""Win32Windows.windows() on the real desktop: the read-only desk check, then synthetic windows.

The synthetic scenarios open windows and take the foreground; run them only when nobody else is
driving the desktop. ``-k desk`` reads the window list and prints nothing but counts.
"""

from __future__ import annotations

import contextlib
import ctypes
import subprocess
import time
from typing import TYPE_CHECKING

import pytest
from synthetic_window import bring_to_front, launch_window, on_right_monitor

from akashic.capture.ports import Rect, TopWindow
from akashic.platform.win.windows import Win32Windows

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

pytestmark = pytest.mark.hardware

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.IsHungAppWindow.argtypes = [ctypes.c_void_p]
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetAncestor.restype = ctypes.c_void_p
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
_GA_ROOT = 2
_WM_CLOSE = 0x0010


def _by_hwnd(windows: list[TopWindow] | None) -> dict[int, TopWindow]:
    assert windows is not None
    return {w.hwnd: w for w in windows}


def _wait_for(find: Callable[[], object], seconds: float = 20) -> object:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if found := find():
            return found
        time.sleep(0.5)
    return None


@pytest.fixture
def synthetic() -> Iterator[Callable[..., int]]:
    processes: list[subprocess.Popen[str]] = []

    def launch(title: str, kind: str, rect: Rect | None = None, parent: int | None = None) -> int:
        process, hwnd = launch_window(
            title, rect or on_right_monitor(200, 200, 400, 300), kind, parent
        )
        processes.append(process)
        return hwnd

    yield launch
    for process in processes:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()


def test_desk_window_list_matches_the_os() -> None:
    windows = Win32Windows().windows()
    assert windows is not None
    hwnds = [w.hwnd for w in windows]
    assert len(hwnds) == len(set(hwnds))
    assert all(user32.IsWindowVisible(h) for h in hwnds)
    assert all(w.minimized == bool(user32.IsIconic(w.hwnd)) for w in windows)
    foreground = user32.GetForegroundWindow()
    root = user32.GetAncestor(foreground, _GA_ROOT) if foreground else None
    if root and user32.IsWindowVisible(root):
        assert int(root) in hwnds


def test_a_newer_window_is_listed_before_the_one_it_covers(
    synthetic: Callable[..., int],
) -> None:
    below = synthetic("AkashicF4 below", "plain")
    above = synthetic("AkashicF4 above", "plain")
    assert bring_to_front(above)
    order = [w.hwnd for w in Win32Windows().windows() or []]
    assert order.index(above) < order.index(below)


def test_plain_tool_popup_and_layered_windows_report_their_styles(
    synthetic: Callable[..., int],
) -> None:
    plain = synthetic("AkashicF4 plain", "plain")
    tool = synthetic("AkashicF4 tool", "tool")
    layered0 = synthetic("AkashicF4 l0", "layered0")
    layered128 = synthetic("AkashicF4 l128", "layered128")
    popup = synthetic("AkashicF4 popup", "popup")
    listed = _by_hwnd(Win32Windows().windows())
    assert (listed[plain].tool, listed[plain].alpha, listed[plain].owner) == (False, None, None)
    assert listed[plain].has_frame is True
    assert listed[tool].tool is True
    assert listed[layered0].alpha == 0
    assert listed[layered128].alpha == 128
    assert listed[popup].popup is False
    assert listed[popup].title == "AkashicF4 popup"
    assert listed[plain].class_name == "AkashicM01Synthetic"
    assert listed[plain].process.startswith("python")
    assert listed[plain].uwp_unresolved is False


def test_an_owned_popup_names_its_owner(synthetic: Callable[..., int]) -> None:
    owner = synthetic("AkashicF4 owner", "popup")
    owned = None
    # The helper prints the owned handle second; find it as the listed popup owned by `owner`.
    owned = _wait_for(
        lambda: next(
            (w for w in Win32Windows().windows() or [] if w.owner == owner and w.popup), None
        )
    )
    assert isinstance(owned, TopWindow)
    assert owned.owner == owner


def test_cloaked_and_minimized_are_flagged_and_hidden_is_absent(
    synthetic: Callable[..., int],
) -> None:
    cloaked = synthetic("AkashicF4 cloaked", "cloaked")
    minimized = synthetic("AkashicF4 minimized", "minimized")
    hidden = synthetic("AkashicF4 hidden", "hidden")
    listed = _by_hwnd(Win32Windows().windows())
    assert listed[cloaked].cloaked is True
    assert listed[minimized].minimized is True
    assert hidden not in listed


def test_a_child_of_another_process_is_a_foreign_child(synthetic: Callable[..., int]) -> None:
    parent = synthetic("AkashicF4 parent", "none")
    alone = synthetic("AkashicF4 alone", "none")
    synthetic("AkashicF4 child", "child", Rect(0, 0, 100, 100), parent)
    listed = _by_hwnd(_wait_for(Win32Windows().windows))  # type: ignore[arg-type]
    assert listed[alone].foreign_children == ()
    children = listed[parent].foreign_children
    assert children is not None
    assert any(name.startswith("python") for name in children)


def test_calculator_resolves_to_its_own_process() -> None:
    windows = Win32Windows()
    before = set(_by_hwnd(windows.windows()))
    subprocess.Popen(["calc.exe"])  # noqa: S607 - the Windows calculator, a hosted app

    def opened() -> TopWindow | None:
        return next(
            (
                w
                for w in windows.windows() or []
                if w.hwnd not in before and w.class_name == "ApplicationFrameWindow"
            ),
            None,
        )

    frame = _wait_for(opened)
    if not isinstance(frame, TopWindow):
        pytest.skip("Calculator opened no hosted window")
    user32.PostMessageW(frame.hwnd, _WM_CLOSE, 0, 0)  # only the window this test opened
    assert frame.process != "applicationframehost.exe"
    assert frame.uwp_unresolved is False


def test_a_hung_window_is_replaced_by_a_ghost_that_points_back(
    synthetic: Callable[..., int],
) -> None:
    hung = synthetic("AkashicF4 hang", "hang")
    windows = Win32Windows()

    def find() -> TopWindow | None:
        # Windows makes the ghost only once something asks if the window hangs, as the shell does.
        user32.IsHungAppWindow(hung)
        return next((w for w in windows.windows() or [] if w.is_ghost and w.ghost_of == hung), None)

    ghost = _wait_for(find, seconds=30)
    assert isinstance(ghost, TopWindow)
    assert ghost.ghost_of == hung
