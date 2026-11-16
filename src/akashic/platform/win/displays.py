# pyright: basic
"""Active monitors from DisplayConfig (stable identity) joined to HMONITOR geometry."""

from __future__ import annotations

import ctypes
import hashlib
from ctypes import wintypes
from typing import Final

from akashic.capture.ports import MonitorInfo, Rect

user32 = ctypes.WinDLL("user32", use_last_error=True)
shcore = ctypes.WinDLL("shcore", use_last_error=True)

_ERROR_INSUFFICIENT_BUFFER: Final = 122
_MAX_TRIES: Final = 5
_QDC_ONLY_ACTIVE_PATHS: Final = 2
_GET_SOURCE_NAME: Final = 1
_GET_TARGET_NAME: Final = 2
_MDT_EFFECTIVE_DPI: Final = 0
_MONITORINFOF_PRIMARY: Final = 1
_BASE_DPI: Final = 96.0
_FALLBACK_NAME: Final = "Monitor"
_PER_MONITOR_AWARE_V2: Final = -4

_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
)


class _Luid(ctypes.Structure):
    _fields_ = (("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG))


class _Rational(ctypes.Structure):
    _fields_ = (("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT))


class _PathSourceInfo(ctypes.Structure):
    _fields_ = (
        ("adapterId", _Luid),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    )


class _PathTargetInfo(ctypes.Structure):
    _fields_ = (
        ("adapterId", _Luid),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("rotation", wintypes.UINT),
        ("scaling", wintypes.UINT),
        ("refreshRate", _Rational),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.UINT),
    )


class _PathInfo(ctypes.Structure):
    _fields_ = (
        ("sourceInfo", _PathSourceInfo),
        ("targetInfo", _PathTargetInfo),
        ("flags", wintypes.UINT),
    )


class _ModeInfo(ctypes.Structure):
    """Opaque: only its size matters to QueryDisplayConfig."""

    _fields_ = (("bytes", ctypes.c_ubyte * 64),)


class _DeviceInfoHeader(ctypes.Structure):
    _fields_ = (
        ("type", wintypes.UINT),
        ("size", wintypes.UINT),
        ("adapterId", _Luid),
        ("id", wintypes.UINT),
    )


class _SourceDeviceName(ctypes.Structure):
    _fields_ = (("header", _DeviceInfoHeader), ("viewGdiDeviceName", wintypes.WCHAR * 32))


class _TargetDeviceName(ctypes.Structure):
    _fields_ = (
        ("header", _DeviceInfoHeader),
        ("flags", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("edidManufactureId", wintypes.WORD),
        ("edidProductCodeId", wintypes.WORD),
        ("connectorInstance", wintypes.UINT),
        ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
        ("monitorDevicePath", wintypes.WCHAR * 128),
    )


class _MonitorInfoEx(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    )


user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
user32.GetDisplayConfigBufferSizes.argtypes = [
    wintypes.UINT,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT),
]
user32.GetDisplayConfigBufferSizes.restype = wintypes.LONG
user32.QueryDisplayConfig.argtypes = [
    wintypes.UINT,
    ctypes.POINTER(wintypes.UINT),
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.UINT),
    ctypes.c_void_p,
    ctypes.c_void_p,
]
user32.QueryDisplayConfig.restype = wintypes.LONG
user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
user32.DisplayConfigGetDeviceInfo.restype = wintypes.LONG
user32.EnumDisplayMonitors.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.RECT),
    _MONITORENUMPROC,
    wintypes.LPARAM,
]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
user32.GetMonitorInfoW.restype = wintypes.BOOL
shcore.GetDpiForMonitor.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.POINTER(wintypes.UINT),
    ctypes.POINTER(wintypes.UINT),
]
shcore.GetDpiForMonitor.restype = ctypes.c_long


def set_dpi_aware() -> None:
    """Make every coordinate in this process physical pixels. Call before any window API."""
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR_AWARE_V2))


def _device_info[T: ctypes.Structure](
    info: T, kind: int, adapter: _Luid, target_id: int
) -> tuple[int, T]:
    info.header.type = kind
    info.header.size = ctypes.sizeof(info)
    info.header.adapterId = adapter
    info.header.id = target_id
    return user32.DisplayConfigGetDeviceInfo(ctypes.byref(info)), info


def _display_targets() -> tuple[int, list[tuple[str, str, str]]]:
    """One attempt: (error code, [(GDI source name, target device path, friendly name)])."""
    path_count, mode_count = wintypes.UINT(), wintypes.UINT()
    code = user32.GetDisplayConfigBufferSizes(
        _QDC_ONLY_ACTIVE_PATHS, ctypes.byref(path_count), ctypes.byref(mode_count)
    )
    if code:
        return code, []
    paths = (_PathInfo * path_count.value)()
    modes = (_ModeInfo * mode_count.value)()
    code = user32.QueryDisplayConfig(
        _QDC_ONLY_ACTIVE_PATHS,
        ctypes.byref(path_count),
        paths,
        ctypes.byref(mode_count),
        modes,
        None,
    )
    if code:
        return code, []
    targets: list[tuple[str, str, str]] = []
    for path in paths[: path_count.value]:
        code, source = _device_info(
            _SourceDeviceName(), _GET_SOURCE_NAME, path.sourceInfo.adapterId, path.sourceInfo.id
        )
        if code:
            return code, []
        code, target = _device_info(
            _TargetDeviceName(), _GET_TARGET_NAME, path.targetInfo.adapterId, path.targetInfo.id
        )
        if code:
            return code, []
        targets.append(
            (source.viewGdiDeviceName, target.monitorDevicePath, target.monitorFriendlyDeviceName)
        )
    return 0, targets


def _gdi_monitors() -> dict[str, tuple[Rect, float, bool]] | None:
    """GDI device name -> (rect, scale, primary) per HMONITOR; None if enumeration fails."""
    found: dict[str, tuple[Rect, float, bool]] = {}

    def callback(monitor: int, _dc: int, _rect: ctypes._Pointer[wintypes.RECT], _l: int) -> bool:
        info = _MonitorInfoEx()
        info.cbSize = ctypes.sizeof(info)
        dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return True
        if shcore.GetDpiForMonitor(
            monitor, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
        ):
            return True
        r = info.rcMonitor
        found[info.szDevice] = (
            Rect(r.left, r.top, r.right, r.bottom),
            dpi_x.value / _BASE_DPI,
            bool(info.dwFlags & _MONITORINFOF_PRIMARY),
        )
        return True

    if not user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(callback), 0):
        return None
    return found


class Win32Displays:
    def monitors(self) -> list[MonitorInfo] | None:
        """Needs a per-monitor-aware V2 process (set_dpi_aware) so rects are physical pixels;
        contract in ``Displays``."""
        for _ in range(_MAX_TRIES):
            code, targets = _display_targets()
            if code == _ERROR_INSUFFICIENT_BUFFER:
                continue
            if code:
                return None
            geometry = _gdi_monitors()
            if geometry is None:
                return None
            monitors = [
                MonitorInfo(
                    hashlib.sha256(device_path.lower().encode()).hexdigest(),
                    friendly_name or _FALLBACK_NAME,
                    *geometry[gdi_name],
                )
                for gdi_name, device_path, friendly_name in targets
                if gdi_name in geometry
            ]
            return sorted(monitors, key=lambda m: (m.rect.left, m.rect.top))
        return None
