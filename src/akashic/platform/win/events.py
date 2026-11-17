# pyright: basic
"""Window change notifications from SetWinEventHook (ctypes, no deps).

Out-of-context hooks only fire on the message loop of the thread that installed them, hence
one dedicated thread that installs the hooks and pumps messages.

Health: ``drain`` reports unhealthy while the hook is down, in the first drain after (re)install
and in the drain that reports a queue overflow. A down hook is reinstalled by a later drain,
with a delay that doubles from 1 s to 60 s between attempts and resets on a healthy drain.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import threading
import time
from ctypes import wintypes

from akashic.capture.ports import WindowChange, WindowChanges, WindowEvent

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

_WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD,
)

user32.SetWinEventHook.argtypes = [
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HMODULE,
    _WINEVENTPROC,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.UnhookWinEvent.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND

_WINEVENT_OUTOFCONTEXT = 0
_OBJID_WINDOW = 0
_OBJID_CLIENT = -4  # Chrome reports a same-title navigation only here
_CHILDID_SELF = 0
_GA_ROOT = 2
_PM_NOREMOVE = 0
_WM_QUIT = 0x0012
_WM_USER = 0x0400

_MAX_EVENTS = 10_000
_INSTALL_WAIT_S = 1.0
_JOIN_WAIT_S = 2.0
_RETRY_FIRST_S = 1.0
_RETRY_MAX_S = 60.0

_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_SYSTEM_MINIMIZESTART = 0x0016
_EVENT_SYSTEM_MINIMIZEEND = 0x0017
_EVENT_OBJECT_DESTROY = 0x8001
_EVENT_OBJECT_SHOW = 0x8002
_EVENT_OBJECT_HIDE = 0x8003
_EVENT_OBJECT_REORDER = 0x8004
_EVENT_OBJECT_LOCATIONCHANGE = 0x800B
_EVENT_OBJECT_NAMECHANGE = 0x800C
_EVENT_OBJECT_CLOAKED = 0x8017
_EVENT_OBJECT_UNCLOAKED = 0x8018

_RANGES = (
    (_EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND),
    (_EVENT_SYSTEM_MINIMIZESTART, _EVENT_SYSTEM_MINIMIZEEND),
    (_EVENT_OBJECT_DESTROY, _EVENT_OBJECT_REORDER),
    (_EVENT_OBJECT_LOCATIONCHANGE, _EVENT_OBJECT_NAMECHANGE),
    (_EVENT_OBJECT_CLOAKED, _EVENT_OBJECT_UNCLOAKED),
)

_CHANGES: dict[int, WindowChange] = {
    _EVENT_OBJECT_SHOW: "shown",
    _EVENT_OBJECT_UNCLOAKED: "shown",
    _EVENT_SYSTEM_MINIMIZEEND: "shown",
    _EVENT_OBJECT_HIDE: "hidden",
    _EVENT_OBJECT_CLOAKED: "hidden",
    _EVENT_SYSTEM_MINIMIZESTART: "hidden",
    _EVENT_OBJECT_DESTROY: "destroyed",
    _EVENT_OBJECT_LOCATIONCHANGE: "moved",
    _EVENT_OBJECT_REORDER: "moved",
    _EVENT_OBJECT_NAMECHANGE: "title",
    _EVENT_SYSTEM_FOREGROUND: "focus",
}


def _set_hook(event_range: tuple[int, int], proc: object) -> int:
    low, high = event_range
    return user32.SetWinEventHook(low, high, None, proc, 0, 0, _WINEVENT_OUTOFCONTEXT)


def _unhook(handles: list[int]) -> None:
    for handle in handles:
        user32.UnhookWinEvent(handle)


def _pump() -> None:
    """Run the message loop until WM_QUIT or an error."""
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        pass


def _quit(thread_id: int) -> None:
    user32.PostThreadMessageW(thread_id, _WM_QUIT, 0, 0)


def _is_child(hwnd: int) -> bool:
    return user32.GetAncestor(hwnd, _GA_ROOT) not in (None, 0, hwnd)


class Win32WindowEvents:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: collections.deque[WindowEvent] = collections.deque(maxlen=_MAX_EVENTS)
        self._installed = False
        self._lost = True
        self._closed = False
        self._overflowed = False
        self._warned = False
        self._thread: threading.Thread | None = None
        self._retry_at = float("-inf")
        self._retry_delay = _RETRY_FIRST_S
        self._proc = _WINEVENTPROC(self._on_event)

    def start(self) -> None:
        if not self._closed and not self._thread_alive():
            self._spawn()

    def drain(self) -> WindowChanges:
        with self._lock:
            if self._closed:
                return WindowChanges(events=(), healthy=False)
            events = tuple(self._events)
            self._events.clear()
            overflowed = self._overflowed
            self._overflowed = False
            healthy = self._installed and not self._lost and not overflowed
            if self._installed:
                self._lost = False
            if healthy:
                self._retry_at = float("-inf")
                self._retry_delay = _RETRY_FIRST_S
        if overflowed:
            log.warning("window event queue overflowed; the oldest events were dropped")
        if not self._installed and not self._thread_alive() and time.monotonic() >= self._retry_at:
            self._spawn()
        return WindowChanges(events, healthy)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._events.clear()
        thread = self._thread
        if thread is not None and thread.native_id is not None and thread.is_alive():
            _quit(thread.native_id)
            thread.join(_JOIN_WAIT_S)

    def _thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _spawn(self) -> None:
        self._retry_at = time.monotonic() + self._retry_delay
        self._retry_delay = min(self._retry_delay * 2, _RETRY_MAX_S)
        ready = threading.Event()
        thread = threading.Thread(
            target=self._run, args=(ready,), name="window-events", daemon=True
        )
        try:
            thread.start()
        except RuntimeError:
            self._report_install_failed(None)
            return
        self._thread = thread
        ready.wait(_INSTALL_WAIT_S)

    def _run(self, ready: threading.Event) -> None:
        handles: list[int] = []
        try:
            # Peeking creates this thread's message queue, which WM_QUIT needs.
            user32.PeekMessageW(
                ctypes.byref(wintypes.MSG()), None, _WM_USER, _WM_USER, _PM_NOREMOVE
            )
            for event_range in _RANGES:
                handle = _set_hook(event_range, self._proc)
                if not handle:
                    self._report_install_failed(ctypes.get_last_error())
                    return
                handles.append(handle)
            self._warned = False
            self._installed = True
            ready.set()
            _pump()
        except OSError:
            log.debug("window event thread failed", exc_info=True)
        finally:
            _unhook(handles)
            with self._lock:
                if not self._installed:
                    self._events.clear()
                self._installed = False
                if not self._closed:
                    self._lost = True
            ready.set()

    def _report_install_failed(self, error: int | None) -> None:
        if self._warned:
            log.debug("window event hook install failed again (error %s)", error)
        else:
            self._warned = True
            log.warning("window event hook install failed (error %s)", error)

    def _on_event(
        self,
        _hook: int,
        event: int,
        hwnd: int | None,
        id_object: int,
        id_child: int,
        _thread: int,
        _ms: int,
    ) -> None:
        at = time.monotonic()
        change = _CHANGES.get(event)
        client_name = id_object == _OBJID_CLIENT and event == _EVENT_OBJECT_NAMECHANGE
        if (
            change is None
            or not (id_object == _OBJID_WINDOW or client_name)
            or id_child != _CHILDID_SELF
            or not hwnd
            or _is_child(hwnd)
        ):
            return
        with self._lock:
            if self._closed:
                return
            if len(self._events) == _MAX_EVENTS:
                self._overflowed = True
            self._events.append(WindowEvent(int(hwnd), change, at))
