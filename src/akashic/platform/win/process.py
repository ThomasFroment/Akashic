"""Start a program that cannot outlive us: a child in a Windows job object.

``launch`` runs the program without a console window and puts it in a job object set to kill
its members when the last handle to the job closes. The handle lives as long as this Python
process does, so the child dies with the worker even when the worker is killed. Reports no facts
beyond "still running" and the exit code; the program's output is thrown away (a server's own
log can quote what it was asked).

Manual smoke test: tests/manual/search.md.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import subprocess
from ctypes import wintypes
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x2000
CREATE_NO_WINDOW: Final = 0x08000000
PROCESS_ALL_ACCESS: Final = 0x1F0FFF
PROCESS_QUERY_LIMITED_INFORMATION: Final = 0x1000
MAX_IMAGE_PATH_CHARS: Final = 32_768
TERMINATE_WAIT_S: Final = 5.0


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]  # Windows only
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CreateJobObjectW.restype = ctypes.c_void_p


def image_name(pid: int) -> str:
    """Lower-cased file name of the executable of ``pid``; empty when it can't be opened."""
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(MAX_IMAGE_PATH_CHARS)
        buffer = ctypes.create_unicode_buffer(MAX_IMAGE_PATH_CHARS)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return PureWindowsPath(buffer.value).name.lower()
    finally:
        _kernel32.CloseHandle(handle)


class _BasicLimits(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _IoCounters(ctypes.Structure):
    _fields_ = tuple((name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    ))  # fmt: skip


class _ExtendedLimits(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


@functools.cache
def _kill_on_close_job() -> int:
    """The one job for the life of this process: closing it (process exit, however it happens)
    kills every child assigned to it."""
    job = _kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _kernel32.SetInformationJobObject(
        ctypes.c_void_p(job),
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    return int(job)


class ChildProgram:
    """A running child. ``terminate`` asks politely, then kills."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self) -> None:
        if self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(TERMINATE_WAIT_S)
        except subprocess.TimeoutExpired:
            self._process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(TERMINATE_WAIT_S)


def launch(args: Sequence[str], env: Mapping[str, str]) -> ChildProgram:
    """Start ``args`` with exactly ``env`` as its environment."""
    job = _kill_on_close_job()
    process = subprocess.Popen(  # noqa: S603 - args are built by our own code
        list(args),
        env=dict(env),
        stdin=subprocess.PIPE,  # llama-server's router watches stdin for EOF to know we're gone
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )
    handle = _kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, process.pid)  # noqa: FBT003
    if not handle or not _kernel32.AssignProcessToJobObject(
        ctypes.c_void_p(job), ctypes.c_void_p(handle)
    ):
        process.kill()
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    _kernel32.CloseHandle(ctypes.c_void_p(handle))
    return ChildProgram(process)
