# pyright: basic
"""Per-window Windows.Graphics.Capture, one frame pool and session per window.

Threading: frame-arrived and closed events run on thread-pool threads. One lock guards the
capture table and the shared D3D device's immediate context, and the instance's event loop
only runs while it is held. A callback copies the frame into a CPU SoftwareBitmap and checks it
in before returning, so no capture frame or GPU surface is ever kept. Pools and sessions are
closed outside the lock, on helper threads with a bounded wait. A device loss only
sets a flag; the caller's next public call rebuilds the device and every capture.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any, Final, cast

from PIL import Image
from winrt.windows.graphics.capture import (
    Direct3D11CaptureFramePool,
    GraphicsCaptureItem,
    GraphicsCaptureSession,
)
from winrt.windows.graphics.directx import DirectXPixelFormat
from winrt.windows.graphics.directx.direct3d11.interop import create_direct3d11_device
from winrt.windows.graphics.imaging import SoftwareBitmap

from akashic.capture.ports import CapturedFrame
from akashic.log import FaultLog

if TYPE_CHECKING:
    from collections.abc import Callable

    from winrt.windows.graphics.directx.direct3d11 import IDirect3DDevice

log = logging.getLogger(__name__)

_PIXEL_FORMAT: Final = DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED
_BYTES_PER_PIXEL: Final = 4
_RELEASE_TIMEOUT_SECONDS: Final = 0.3
_DEVICE_LOST_HRESULTS: Final = frozenset({0x887A0005, 0x887A0006, 0x887A0007})  # REMOVED/HUNG/RESET

type _Size = tuple[int, int]


@dataclass(slots=True)
class _Capture:
    hwnd: int
    interval_ms: int
    failed: bool = False
    item: Any = None
    pool: Any = None
    session: Any = None
    closed_token: Any = None
    frame_token: Any = None
    pool_size: _Size = (0, 0)
    bitmap: SoftwareBitmap | None = None
    crop: _Size = (0, 0)  # the part of the bitmap that holds window content
    seq: int | None = None
    at: float = 0.0


def _never_raises[**P, R](default: object) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(method: Callable[P, R]) -> Callable[P, R]:
        @wraps(method)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return method(*args, **kwargs)
            except Exception as error:  # noqa: BLE001
                cast("WgcCaptures", args[0]).faults.note(method.__name__, _hresult_text(error))
                return cast("R", default)

        return guarded

    return decorate


def _hresult(error: Exception) -> int | None:
    winerror = getattr(error, "winerror", None)
    return None if winerror is None else winerror & 0xFFFFFFFF


def _hresult_text(error: Exception) -> str:
    code = _hresult(error)
    return type(error).__name__ if code is None else f"HRESULT 0x{code:08X}"


def _is_device_lost(error: Exception) -> bool:
    return _hresult(error) in _DEVICE_LOST_HRESULTS


def _interval(ms: int) -> timedelta:
    return timedelta(milliseconds=max(ms, 0))


def _new_device(faults: FaultLog) -> IDirect3DDevice | None:
    try:
        return create_direct3d11_device()
    except Exception as error:  # noqa: BLE001
        faults.note("D3D device", _hresult_text(error))
        return None


def _close_native(cap: _Capture) -> None:
    # Pool first: if the session close hangs, the window-sized GPU buffer is already freed.
    # Measured: 0 of 10 pool closes blocked per case (kill, WM_CLOSE exit, window destroyed).
    for step in (
        lambda: cap.item.remove_closed(cap.closed_token),
        lambda: cap.pool.remove_frame_arrived(cap.frame_token),
        lambda: cap.pool.close(),  # noqa: PLW0108
        lambda: cap.session.close(),  # noqa: PLW0108
    ):
        with contextlib.suppress(Exception):
            step()


def _release(*caps: _Capture) -> None:
    """Close captures in parallel, waiting a bounded time in total.

    Closing a session whose window's process died moments ago blocks inside Windows for good
    (measured: no callback involved; it clears if the close starts 3 s later), so a stuck close is
    abandoned on its daemon thread.
    """
    closers = [threading.Thread(target=_close_native, args=(cap,), daemon=True) for cap in caps]
    for closer in closers:
        closer.start()
    deadline = time.monotonic() + _RELEASE_TIMEOUT_SECONDS
    for closer in closers:
        closer.join(max(0.0, deadline - time.monotonic()))
    if stuck := sum(closer.is_alive() for closer in closers):
        log.warning("%d capture closes abandoned after %.1f s", stuck, _RELEASE_TIMEOUT_SECONDS)


def _to_image(bitmap: SoftwareBitmap, crop: _Size) -> Image.Image:
    width, height = bitmap.pixel_width, bitmap.pixel_height
    stride = width * _BYTES_PER_PIXEL
    pixels = bytearray(stride * height)
    bitmap.copy_to_buffer(pixels)
    return Image.frombuffer("RGB", crop, pixels, "raw", "BGRX", stride)


class WgcCaptures:
    def __init__(self, max_captures: int = 32) -> None:
        self._max_captures = max_captures
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        # A window WGC refuses (an elevated one) is retried at every check: a line per failure
        # would be one a second.
        self.faults = FaultLog(log)
        self._device = _new_device(self.faults)
        self._captures: dict[int, _Capture] = {}
        self._counter = itertools.count(1)
        self._lost = False
        self._closed = False

    @_never_raises(default={})
    def fault_counts(self) -> dict[str, int]:
        return self.faults.counts()

    @_never_raises(default=False)
    def start(self, hwnd: int, min_interval_ms: int) -> bool:
        self._restart_if_lost()
        if self._closed:
            return False
        with self._lock:
            cap = self._captures.get(hwnd)
            if cap is not None and not cap.failed:
                cap.interval_ms = min_interval_ms
                cap.session.min_update_interval = _interval(min_interval_ms)
                return True
            failed_cap = self._captures.pop(hwnd, None)
            live = sum(not c.failed for c in self._captures.values())
        if failed_cap is not None:
            _release(failed_cap)
        if live >= self._max_captures:
            return False
        if self._device is None:
            self._device = _new_device(self.faults)
        if self._device is None:
            return False
        return self._open(self._device, hwnd, min_interval_ms)

    @_never_raises(default=None)
    def stop(self, hwnd: int) -> None:
        with self._lock:
            cap = self._captures.pop(hwnd, None)
        if cap is not None:
            _release(cap)

    @_never_raises(default=None)
    def latest_seq(self, hwnd: int) -> int | None:
        self._restart_if_lost()
        with self._lock:
            cap = self._captures.get(hwnd)
            return None if cap is None else cap.seq

    @_never_raises(default=None)
    def grab(self, hwnd: int) -> CapturedFrame | None:
        self._restart_if_lost()
        with self._lock:
            cap = self._captures.get(hwnd)
            if cap is None or cap.seq is None or cap.bitmap is None:
                return None
            bitmap, crop, seq, at = cap.bitmap, cap.crop, cap.seq, cap.at
        try:
            return CapturedFrame(seq, at, _to_image(bitmap, crop))
        except Exception as error:  # noqa: BLE001
            with self._lock:
                self._note_failure(cap, error)
            return None

    @_never_raises(default=frozenset())
    def failed(self) -> frozenset[int]:
        self._restart_if_lost()
        with self._lock:
            return frozenset(h for h, c in self._captures.items() if c.failed)

    @_never_raises(default=None)
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            caps = list(self._captures.values())
            self._captures.clear()
            device, self._device = self._device, None
        _release(*caps)
        if device is not None:
            with contextlib.suppress(Exception):
                device.close()
        self._loop.close()

    def _open(self, device: IDirect3DDevice, hwnd: int, interval_ms: int) -> bool:
        """Start a capture and register it before frames can arrive; False leaves nothing behind."""
        cap = _Capture(hwnd, interval_ms)
        try:
            if not GraphicsCaptureSession.is_supported():
                return False
            item = GraphicsCaptureItem.try_create_from_window_id((hwnd,))
            if item is None:
                return False
            cap.item = item
            size = item.size
            cap.pool_size = (size.width, size.height)
            cap.pool = Direct3D11CaptureFramePool.create_free_threaded(
                device, _PIXEL_FORMAT, 1, cap.pool_size
            )
            cap.session = cap.pool.create_capture_session(item)
            cap.session.is_border_required = False
            cap.session.is_cursor_capture_enabled = False
            cap.session.min_update_interval = _interval(interval_ms)
            cap.closed_token = item.add_closed(lambda _s, _a: self._on_closed(cap))
            cap.frame_token = cap.pool.add_frame_arrived(lambda _s, _a: self._on_frame(cap))
            with self._lock:
                self._captures[hwnd] = cap
                cap.session.start_capture()
        except Exception as error:  # noqa: BLE001
            self.faults.note("capture start", _hresult_text(error))
            with self._lock:
                if self._captures.get(hwnd) is cap:
                    del self._captures[hwnd]
                if _is_device_lost(error):
                    self._lost = True
            _release(cap)
            return False
        return True

    def _on_frame(self, cap: _Capture) -> None:
        arrived = time.monotonic()
        with self._lock:
            if self._lost or self._captures.get(cap.hwnd) is not cap or cap.failed:
                return
            try:
                self._store_frame(cap, arrived)
            except Exception as error:  # noqa: BLE001
                self._note_failure(cap, error)

    def _store_frame(self, cap: _Capture, arrived: float) -> None:
        frame = cap.pool.try_get_next_frame()
        if frame is None:
            return
        try:
            content = frame.content_size
            bitmap = self._loop.run_until_complete(
                SoftwareBitmap.create_copy_from_surface_async(frame.surface)
            )
        finally:
            frame.close()
        content_size = (content.width, content.height)
        if content_size != cap.pool_size:
            cap.pool.recreate(self._device, _PIXEL_FORMAT, 1, content_size)
            cap.pool_size = content_size
        cap.bitmap = bitmap
        cap.crop = (
            min(content_size[0], bitmap.pixel_width),
            min(content_size[1], bitmap.pixel_height),
        )
        cap.at = arrived
        cap.seq = next(self._counter)

    def _on_closed(self, cap: _Capture) -> None:
        with self._lock:
            if self._captures.get(cap.hwnd) is cap:
                self._drop_frame(cap)
                cap.failed = True

    def _note_failure(self, cap: _Capture, error: Exception) -> None:
        """Lock must be held. Device loss waits for the next public call; any other error fails."""
        if self._captures.get(cap.hwnd) is not cap:
            return
        self.faults.note("frame", _hresult_text(error))
        if _is_device_lost(error):
            self._lost = True
        else:
            self._drop_frame(cap)
            cap.failed = True

    @staticmethod
    def _drop_frame(cap: _Capture) -> None:
        cap.bitmap = None
        cap.seq = None

    def _restart_if_lost(self) -> None:
        with self._lock:
            if not self._lost or self._closed:
                return
            self._lost = False
            old = list(self._captures.values())
            self._captures.clear()
            old_device, self._device = self._device, None
        log.warning("device lost: restarting %d captures", len(old))
        _release(*old)
        if old_device is not None:
            with contextlib.suppress(Exception):
                old_device.close()
        device = self._device = _new_device(self.faults)
        for cap in old:
            if device is None or not self._open(device, cap.hwnd, cap.interval_ms):
                with self._lock:
                    self._captures[cap.hwnd] = _Capture(cap.hwnd, cap.interval_ms, failed=True)
