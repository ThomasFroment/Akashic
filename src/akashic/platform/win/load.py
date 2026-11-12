# pyright: basic
"""CPU and GPU utilisation, for the scheduler. Reports numbers; the scheduler decides.

CPU: ``GetSystemTimes`` deltas between calls. GPU: NVML (``nvml.dll``, installed with the NVIDIA
driver) through ctypes: no subprocess, no extra package. Both are None/0 when unavailable.
"""

from __future__ import annotations

import ctypes
import logging
import os
from ctypes import POINTER, Structure, byref, c_uint, c_void_p, wintypes
from pathlib import Path

log = logging.getLogger(__name__)

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetSystemTimes.argtypes = [POINTER(wintypes.FILETIME)] * 3


def _ticks(ft: wintypes.FILETIME) -> int:
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


class _Utilization(Structure):
    _fields_ = (("gpu", c_uint), ("memory", c_uint))


def _load_nvml() -> ctypes.CDLL | None:
    candidates = [
        Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "nvml.dll",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "NVIDIA Corporation"
        / "NVSMI"
        / "nvml.dll",
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ctypes.CDLL(str(path))
            except OSError:
                log.debug("could not load nvml.dll")
    return None


class WindowsLoad:
    def __init__(self) -> None:
        self._last: tuple[int, int] | None = None  # (idle, total) ticks at the previous call
        self._nvml: ctypes.CDLL | None = None
        self._gpu: c_void_p | None = None
        self._init_gpu()

    def _init_gpu(self) -> None:
        nvml = _load_nvml()
        if nvml is None:
            log.info("no NVML: GPU load will not be measured")
            return
        handle = c_void_p()
        if nvml.nvmlInit_v2() != 0 or nvml.nvmlDeviceGetHandleByIndex_v2(0, byref(handle)) != 0:
            log.info("NVML could not open GPU 0: GPU load will not be measured")
            return
        self._nvml, self._gpu = nvml, handle

    def cpu_percent(self) -> float:
        """Busy share of all cores since the previous call (0 on the first call)."""
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel32.GetSystemTimes(byref(idle), byref(kernel), byref(user)):
            return 0.0
        now = (_ticks(idle), _ticks(kernel) + _ticks(user))  # kernel time includes idle time
        last, self._last = self._last, now
        if last is None or now[1] <= last[1]:
            return 0.0
        busy = 1.0 - (now[0] - last[0]) / (now[1] - last[1])
        return max(0.0, min(100.0, busy * 100.0))

    def gpu_percent(self) -> float | None:
        if self._nvml is None or self._gpu is None:
            return None
        util = _Utilization()
        if self._nvml.nvmlDeviceGetUtilizationRates(self._gpu, byref(util)) != 0:
            return None
        return float(util.gpu)
