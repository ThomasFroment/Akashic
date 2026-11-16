"""Win32Displays against scripted DisplayConfig and HMONITOR results; no real display is read."""

from __future__ import annotations

import ctypes
import hashlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from akashic.capture.ports import MonitorInfo, Rect
from akashic.platform.win import displays

if TYPE_CHECKING:
    from collections.abc import Callable

_Targets = list[tuple[str, str, str]]
_Geometry = dict[str, tuple[Rect, float, bool]]

PATH_A = r"\\?\DISPLAY#GSM5B09#5&1a2b3c&0&UID4352#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
PATH_B = r"\\?\DISPLAY#GSM5B09#5&1a2b3c&0&UID4353#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}"
LEFT = Rect(-1920, 0, 0, 1080)
MAIN = Rect(0, 0, 2560, 1440)


def _key(path: str) -> str:
    return hashlib.sha256(path.lower().encode()).hexdigest()


def _install(
    monkeypatch: pytest.MonkeyPatch,
    attempts: list[tuple[int, _Targets]],
    geometry: _Geometry | None,
) -> list[int]:
    calls: list[int] = []

    def targets() -> tuple[int, _Targets]:
        calls.append(1)
        return attempts[min(len(calls), len(attempts)) - 1]

    monkeypatch.setattr(displays, "_display_targets", targets)
    monkeypatch.setattr(displays, "_gdi_monitors", lambda: geometry)
    return calls


def test_monitors_are_sorted_by_position_whatever_the_enumeration_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        [(0, [("\\\\.\\DISPLAY1", PATH_A, "Main"), ("\\\\.\\DISPLAY2", PATH_B, "Left")])],
        {"\\\\.\\DISPLAY1": (MAIN, 1.5, True), "\\\\.\\DISPLAY2": (LEFT, 1.0, False)},
    )
    result = displays.Win32Displays().monitors()
    assert result == [
        MonitorInfo(_key(PATH_B), "Left", LEFT, 1.0, primary=False),
        MonitorInfo(_key(PATH_A), "Main", MAIN, 1.5, primary=True),
    ]


def test_sort_breaks_a_left_tie_on_top(monkeypatch: pytest.MonkeyPatch) -> None:
    low = Rect(0, 1440, 1920, 2520)
    _install(
        monkeypatch,
        [(0, [("L", PATH_A, "Low"), ("M", PATH_B, "Main")])],
        {"L": (low, 1.0, False), "M": (MAIN, 1.0, True)},
    )
    result = displays.Win32Displays().monitors()
    assert result is not None
    assert [m.name for m in result] == ["Main", "Low"]


def test_key_hashes_the_lowercased_device_path_and_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        [(0, [("G", PATH_A.upper(), "A")])],
        {"G": (MAIN, 1.0, True)},
    )
    first = displays.Win32Displays().monitors()
    second = displays.Win32Displays().monitors()
    assert first is not None
    assert first[0].key == _key(PATH_A)
    assert first == second


def test_paths_differing_only_in_the_instance_part_get_different_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        [(0, [("A", PATH_A, "X"), ("B", PATH_B, "X")])],
        {"A": (LEFT, 1.0, False), "B": (MAIN, 1.0, True)},
    )
    result = displays.Win32Displays().monitors()
    assert result is not None
    assert result[0].key != result[1].key


def test_empty_friendly_name_falls_back_to_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [(0, [("G", PATH_A, "")])], {"G": (MAIN, 1.0, True)})
    result = displays.Win32Displays().monitors()
    assert result is not None
    assert result[0].name == "Monitor"


def test_rect_scale_and_primary_come_through_with_negative_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [(0, [("G", PATH_A, "L")])], {"G": (LEFT, 1.25, False)})
    result = displays.Win32Displays().monitors()
    assert result is not None
    assert (result[0].rect, result[0].scale, result[0].primary) == (LEFT, 1.25, False)


@pytest.mark.parametrize("failures", [0, 1, 4])
def test_insufficient_buffer_retries_until_success(
    monkeypatch: pytest.MonkeyPatch, failures: int
) -> None:
    attempts: list[tuple[int, _Targets]] = [(122, [])] * failures
    attempts.append((0, [("G", PATH_A, "A")]))
    calls = _install(monkeypatch, attempts, {"G": (MAIN, 1.0, True)})
    result = displays.Win32Displays().monitors()
    assert result is not None
    assert len(result) == 1
    assert len(calls) == failures + 1


def test_five_insufficient_buffer_attempts_give_none(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [(122, [])], {})
    assert displays.Win32Displays().monitors() is None
    assert len(calls) == 5


@pytest.mark.parametrize("code", [5, 31, 50, 87])
def test_other_error_codes_return_none_without_retry(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    calls = _install(monkeypatch, [(code, [])], {})
    assert displays.Win32Displays().monitors() is None
    assert len(calls) == 1


def test_enum_display_monitors_failure_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [(0, [("G", PATH_A, "A")])], None)
    assert displays.Win32Displays().monitors() is None


def test_path_without_hmonitor_and_hmonitor_without_path_are_left_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        [(0, [("G1", PATH_A, "A"), ("MISSING", PATH_B, "B")])],
        {"G1": (MAIN, 1.0, True), "ORPHAN": (LEFT, 1.0, False)},
    )
    result = displays.Win32Displays().monitors()
    assert result is not None
    assert [m.name for m in result] == ["A"]


def test_no_active_paths_gives_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [(0, [])], {})
    assert displays.Win32Displays().monitors() == []


# --- the real adapters over a scripted user32 / shcore -----------------------------------------


def test_struct_sizes_match_the_win32_headers() -> None:
    assert ctypes.sizeof(displays._PathInfo) == 72  # pyright: ignore[reportPrivateUsage]
    assert ctypes.sizeof(displays._ModeInfo) == 64  # pyright: ignore[reportPrivateUsage]
    assert ctypes.sizeof(displays._SourceDeviceName) == 20 + 64  # pyright: ignore[reportPrivateUsage]
    assert ctypes.sizeof(displays._TargetDeviceName) == 420  # pyright: ignore[reportPrivateUsage]
    assert ctypes.sizeof(displays._MonitorInfoEx) == 104  # pyright: ignore[reportPrivateUsage]


def _scripted_user32(sizes: int, query: int, info: Callable[[object], int]) -> SimpleNamespace:
    def buffer_sizes(_flags: int, paths: object, modes: object) -> int:
        if sizes == 0:
            paths._obj.value = 1  # type: ignore[attr-defined]
            modes._obj.value = 1  # type: ignore[attr-defined]
        return sizes

    def query_config(*_args: object) -> int:
        return query

    return SimpleNamespace(
        GetDisplayConfigBufferSizes=buffer_sizes,
        QueryDisplayConfig=query_config,
        DisplayConfigGetDeviceInfo=info,
    )


@pytest.mark.parametrize(
    ("sizes", "query", "info_code"),
    [(122, 0, 0), (0, 122, 0), (0, 0, 122)],
)
def test_insufficient_buffer_from_each_display_config_call_is_reported(
    monkeypatch: pytest.MonkeyPatch, sizes: int, query: int, info_code: int
) -> None:
    fake = _scripted_user32(sizes, query, lambda _ref: info_code)
    monkeypatch.setattr(displays, "user32", fake)
    code, targets = displays._display_targets()  # pyright: ignore[reportPrivateUsage]
    assert code == 122
    assert targets == []


@pytest.mark.parametrize(
    ("sizes", "query", "info_code"),
    [(5, 0, 0), (0, 31, 0), (0, 0, 87), (50, 0, 0)],
)
def test_other_codes_from_each_display_config_call_are_passed_up(
    monkeypatch: pytest.MonkeyPatch, sizes: int, query: int, info_code: int
) -> None:
    fake = _scripted_user32(sizes, query, lambda _ref: info_code)
    monkeypatch.setattr(displays, "user32", fake)
    code, _ = displays._display_targets()  # pyright: ignore[reportPrivateUsage]
    assert code == (sizes or query or info_code)
