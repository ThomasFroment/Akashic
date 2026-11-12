# pyright: basic
"""WASAPI recording (microphone and playback loopback) and "who is making sound right now".

Written on comtypes because the libraries tried first failed on this PC: ``soundcard`` asserts on
the Elgato Wave:3 mix format (docs/CAPTURE.md). Windows converts to our format for us
(shared mode with ``AUTOCONVERTPCM``): every stream arrives as mono float32 at 16 kHz, which is
what both Silero VAD and Whisper want, so there is no resampling code here.

Each open stream and each ``WindowsAudioSessions`` belongs to the one thread that uses it. COM is
multithreaded (``coinit_flags = 0``) like the rest of the capture process.
"""

from __future__ import annotations

import ctypes
import importlib
import logging
import sys
import threading
import time
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_float,
    c_int,
    c_longlong,
    c_ubyte,
    c_void_p,
    cast,
    wintypes,
)
from ctypes import c_uint32 as c_u32
from ctypes import c_uint64 as c_u64
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from akashic.capture.ports import AudioDevice, AudioError
from akashic.platform.win.process import image_name
from akashic.storage.opus import SAMPLE_RATE

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from akashic.capture.ports import AudioInput, AudioKind

# COINIT_MULTITHREADED. Must be set before comtypes is first imported, hence import_module.
sys.coinit_flags = 0  # type: ignore[attr-defined]
comtypes = importlib.import_module("comtypes")
COMMETHOD = comtypes.COMMETHOD
GUID = comtypes.GUID
IUnknown = comtypes.IUnknown
HRESULT = ctypes.HRESULT

log = logging.getLogger(__name__)

_E_RENDER: Final = 0
_E_CAPTURE: Final = 1
_E_CONSOLE: Final = 0
_STATE_ACTIVE: Final = 1
_CLSCTX_ALL: Final = 23
_SHARED: Final = 0
_FLAG_LOOPBACK: Final = 0x00020000
_FLAG_AUTOCONVERTPCM: Final = 0x80000000
_FLAG_SRC_DEFAULT_QUALITY: Final = 0x08000000
_BUFFER_SILENT: Final = 0x2
_WAVE_FORMAT_IEEE_FLOAT: Final = 3
_VT_LPWSTR: Final = 31
_SESSION_ACTIVE: Final = 1
_AUDIBLE_PEAK: Final = 0.001  # about -60 dBFS: a session that is open but silent doesn't count
_POLL_S: Final = 0.01
_SESSION_REFRESH_S: Final = 30.0


class WAVEFORMATEX(Structure):
    _pack_ = 1
    _fields_ = (
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    )


class PROPERTYKEY(Structure):
    _fields_ = (("fmtid", GUID), ("pid", wintypes.DWORD))


class PROPVARIANT(Structure):
    # 24 bytes on x64, large enough for every variant we could be handed.
    _fields_ = (
        ("vt", wintypes.WORD),
        ("r1", wintypes.WORD),
        ("r2", wintypes.WORD),
        ("r3", wintypes.WORD),
        ("data", c_void_p),
        ("data2", c_void_p),
    )


class IPropertyStore(IUnknown):
    _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_u32), "cProps")),
        COMMETHOD(
            [],
            HRESULT,
            "GetAt",
            (["in"], c_u32, "iProp"),
            (["out"], POINTER(PROPERTYKEY), "pkey"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetValue",
            (["in"], POINTER(PROPERTYKEY), "key"),
            (["out"], POINTER(PROPVARIANT), "pv"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "SetValue",
            (["in"], POINTER(PROPERTYKEY), "key"),
            (["in"], POINTER(PROPVARIANT), "propvar"),
        ),
        COMMETHOD([], HRESULT, "Commit"),
    )


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "Activate",
            (["in"], POINTER(GUID), "iid"),
            (["in"], wintypes.DWORD, "dwClsCtx"),
            (["in"], c_void_p, "pActivationParams"),
            (["out"], POINTER(POINTER(IUnknown)), "ppInterface"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "OpenPropertyStore",
            (["in"], wintypes.DWORD, "stgmAccess"),
            (["out"], POINTER(POINTER(IPropertyStore)), "ppProperties"),
        ),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_void_p), "ppstrId")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(wintypes.DWORD), "pdwState")),
    )


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_u32), "pcDevices")),
        COMMETHOD(
            [],
            HRESULT,
            "Item",
            (["in"], c_u32, "nDevice"),
            (["out"], POINTER(POINTER(IMMDevice)), "ppDevice"),
        ),
    )


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "EnumAudioEndpoints",
            (["in"], c_int, "dataFlow"),
            (["in"], wintypes.DWORD, "dwStateMask"),
            (["out"], POINTER(POINTER(IMMDeviceCollection)), "ppDevices"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDefaultAudioEndpoint",
            (["in"], c_int, "dataFlow"),
            (["in"], c_int, "role"),
            (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint"),
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDevice",
            (["in"], wintypes.LPCWSTR, "pwstrId"),
            (["out"], POINTER(POINTER(IMMDevice)), "ppDevice"),
        ),
        COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], c_void_p, "p")),
        COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], c_void_p, "p")),
    )


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "Initialize",
            (["in"], c_int, "ShareMode"),
            (["in"], wintypes.DWORD, "StreamFlags"),
            (["in"], c_longlong, "hnsBufferDuration"),
            (["in"], c_longlong, "hnsPeriodicity"),
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["in"], c_void_p, "AudioSessionGuid"),
        ),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_u32), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_u32), "pNumPaddingFrames")),
        COMMETHOD(
            [],
            HRESULT,
            "IsFormatSupported",
            (["in"], c_int, "ShareMode"),
            (["in"], POINTER(WAVEFORMATEX), "pFormat"),
            (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch"),
        ),
        COMMETHOD(
            [], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")
        ),
        COMMETHOD(
            [],
            HRESULT,
            "GetDevicePeriod",
            (["out"], POINTER(c_longlong), "phnsDefaultDevicePeriod"),
            (["out"], POINTER(c_longlong), "phnsMinimumDevicePeriod"),
        ),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "eventHandle")),
        COMMETHOD(
            [],
            HRESULT,
            "GetService",
            (["in"], POINTER(GUID), "riid"),
            (["out"], POINTER(POINTER(IUnknown)), "ppv"),
        ),
    )


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD(
            [],
            HRESULT,
            "GetBuffer",
            (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
            (["out"], POINTER(c_u32), "pNumFramesToRead"),
            (["out"], POINTER(wintypes.DWORD), "pdwFlags"),
            (["out"], POINTER(c_u64), "pu64DevicePosition"),
            (["out"], POINTER(c_u64), "pu64QPCPosition"),
        ),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_u32, "NumFramesRead")),
        COMMETHOD(
            [], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_u32), "pNumFramesInNextPacket")
        ),
    )


class IAudioMeterInformation(IUnknown):
    _iid_ = GUID("{C02216F6-8C67-4B5B-9D00-D008E73E0064}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetPeakValue", (["out"], POINTER(c_float), "pfPeak")),
        COMMETHOD([], HRESULT, "GetMeterChannelCount", (["out"], POINTER(c_u32), "pnChannelCount")),
        COMMETHOD([], HRESULT, "GetChannelsPeakValues", (["in"], c_u32, "u32ChannelCount")),
        COMMETHOD([], HRESULT, "QueryHardwareSupport", (["out"], POINTER(wintypes.DWORD), "x")),
    )


class IAudioSessionControl(IUnknown):
    _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "pRetVal")),
        COMMETHOD([], HRESULT, "GetDisplayName", (["out"], POINTER(c_void_p), "pRetVal")),
        COMMETHOD([], HRESULT, "SetDisplayName", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
        COMMETHOD([], HRESULT, "GetIconPath", (["out"], POINTER(c_void_p), "pRetVal")),
        COMMETHOD([], HRESULT, "SetIconPath", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")),
        COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "pRetVal")),
        COMMETHOD(
            [], HRESULT, "SetGroupingParam", (["in"], c_void_p, "a"), (["in"], c_void_p, "b")
        ),
        COMMETHOD([], HRESULT, "RegisterAudioSessionNotification", (["in"], c_void_p, "p")),
        COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification", (["in"], c_void_p, "p")),
    )


class IAudioSessionControl2(IAudioSessionControl):
    _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], POINTER(c_void_p), "pRetVal")),
        COMMETHOD(
            [], HRESULT, "GetSessionInstanceIdentifier", (["out"], POINTER(c_void_p), "pRetVal")
        ),
        COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(wintypes.DWORD), "pRetVal")),
        COMMETHOD([], HRESULT, "IsSystemSoundsSession"),
        COMMETHOD([], HRESULT, "SetDuckingPreference", (["in"], wintypes.BOOL, "optOut")),
    )


class IAudioSessionEnumerator(IUnknown):
    _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
        COMMETHOD(
            [],
            HRESULT,
            "GetSession",
            (["in"], c_int, "SessionCount"),
            (["out"], POINTER(POINTER(IAudioSessionControl)), "Session"),
        ),
    )


class IAudioSessionManager2(IUnknown):
    _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    _methods_ = (
        # IAudioSessionManager
        COMMETHOD([], HRESULT, "GetAudioSessionControl", (["in"], c_void_p, "a")),
        COMMETHOD([], HRESULT, "GetSimpleAudioVolume", (["in"], c_void_p, "a")),
        # IAudioSessionManager2
        COMMETHOD(
            [],
            HRESULT,
            "GetSessionEnumerator",
            (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "SessionEnum"),
        ),
        COMMETHOD([], HRESULT, "RegisterSessionNotification", (["in"], c_void_p, "p")),
        COMMETHOD([], HRESULT, "UnregisterSessionNotification", (["in"], c_void_p, "p")),
        COMMETHOD(
            [],
            HRESULT,
            "RegisterDuckNotification",
            (["in"], c_void_p, "a"),
            (["in"], c_void_p, "b"),
        ),
        COMMETHOD([], HRESULT, "UnregisterDuckNotification", (["in"], c_void_p, "p")),
    )


_CLSID_ENUMERATOR = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_FRIENDLY_NAME = PROPERTYKEY(GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14)

ole32 = ctypes.WinDLL("ole32")
ole32.CoTaskMemFree.argtypes = [c_void_p]
ole32.PropVariantClear.argtypes = [POINTER(PROPVARIANT)]

_com = threading.local()


def _ensure_com() -> None:
    """Initialize COM (multithreaded) on the calling thread once."""
    if getattr(_com, "ready", False):
        return
    try:
        comtypes.CoInitializeEx(0)
    except OSError:
        log.debug("COM was already initialized on this thread with another mode")
    _com.ready = True


def _enumerator():  # noqa: ANN202 - COM object
    _ensure_com()
    return comtypes.CoCreateInstance(_CLSID_ENUMERATOR, interface=IMMDeviceEnumerator)


def _take_string(pointer: int | None) -> str:
    """Read and free a CoTaskMem wide string returned by COM."""
    if not pointer:
        return ""
    try:
        return ctypes.wstring_at(pointer)
    finally:
        ole32.CoTaskMemFree(pointer)


def _device_id(device) -> str:  # noqa: ANN001
    return _take_string(device.GetId())


def _device_name(device) -> str:  # noqa: ANN001
    store = device.OpenPropertyStore(0)  # STGM_READ
    value = store.GetValue(byref(_FRIENDLY_NAME))
    try:
        return ctypes.wstring_at(value.data) if value.vt == _VT_LPWSTR and value.data else ""
    finally:
        ole32.PropVariantClear(byref(value))


def _flow(kind: AudioKind) -> int:
    return _E_RENDER if kind == "system" else _E_CAPTURE


def _describe(device) -> AudioDevice:  # noqa: ANN001
    return AudioDevice(_device_id(device), _device_name(device))


class WindowsAudioDevices:
    def find(self, kind: AudioKind, name_contains: str) -> AudioDevice | None:
        try:
            enumerator = _enumerator()
            if not name_contains:
                return _describe(enumerator.GetDefaultAudioEndpoint(_flow(kind), _E_CONSOLE))
            wanted = name_contains.casefold()
            devices = enumerator.EnumAudioEndpoints(_flow(kind), _STATE_ACTIVE)
            for i in range(devices.GetCount()):
                found = _describe(devices.Item(i))
                if wanted in found.name.casefold():
                    return found
        except comtypes.COMError, OSError:
            log.warning("could not look up %s audio devices", kind)
        return None

    def names(self, kind: AudioKind) -> list[str]:
        try:
            devices = _enumerator().EnumAudioEndpoints(_flow(kind), _STATE_ACTIVE)
            return [_describe(devices.Item(i)).name for i in range(devices.GetCount())]
        except comtypes.COMError, OSError:
            log.warning("could not list %s audio devices", kind)
            return []

    def open(self, kind: AudioKind, device: AudioDevice) -> AudioInput:
        try:
            return _WasapiInput(kind, device)
        except (comtypes.COMError, OSError) as exc:
            msg = f"cannot open {kind} device ({_hresult(exc)})"
            raise AudioError(msg) from exc


def _hresult(exc: BaseException) -> str:
    code = exc.args[0] if exc.args else 0
    return f"0x{code & 0xFFFFFFFF:08x}" if isinstance(code, int) else type(exc).__name__


class _WasapiInput:
    """Polling shared-mode capture; loopback for playback devices."""

    def __init__(self, kind: AudioKind, device: AudioDevice) -> None:
        enumerator = _enumerator()
        endpoint = enumerator.GetDevice(device.device_id)
        # QueryInterface, not ctypes.cast: it takes its own reference (one release per wrapper).
        client: Any = endpoint.Activate(IAudioClient._iid_, _CLSCTX_ALL, None).QueryInterface(
            IAudioClient
        )
        fmt = WAVEFORMATEX(_WAVE_FORMAT_IEEE_FLOAT, 1, SAMPLE_RATE, SAMPLE_RATE * 4, 4, 32, 0)
        flags = _FLAG_AUTOCONVERTPCM | _FLAG_SRC_DEFAULT_QUALITY
        if kind == "system":
            flags |= _FLAG_LOOPBACK
        client.Initialize(_SHARED, flags, 10_000_000, 0, byref(fmt), None)
        service = client.GetService(byref(IAudioCaptureClient._iid_))
        self._capture: Any = service.QueryInterface(IAudioCaptureClient)
        self._client: Any = client
        self._closed = False
        client.Start()

    def read(self, timeout_s: float) -> NDArray[np.float32] | None:
        deadline = time.monotonic() + timeout_s
        chunks: list[NDArray[np.float32]] = []
        while True:
            try:
                packet = self._capture.GetNextPacketSize()
                while packet:
                    data, frames, flags, _, _ = self._capture.GetBuffer()
                    if flags & _BUFFER_SILENT:
                        chunk = np.zeros(frames, dtype=np.float32)
                    else:
                        chunk = np.ctypeslib.as_array(
                            cast(data, POINTER(c_float)), (frames,)
                        ).copy()
                    self._capture.ReleaseBuffer(frames)
                    chunks.append(chunk)
                    packet = self._capture.GetNextPacketSize()
            except (comtypes.COMError, OSError) as exc:
                msg = f"audio device failed ({_hresult(exc)})"
                raise AudioError(msg) from exc
            if chunks:
                return chunks[0] if len(chunks) == 1 else np.concatenate(chunks)
            if time.monotonic() >= deadline:
                return None
            time.sleep(_POLL_S)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.Stop()
        except comtypes.COMError, OSError:
            log.debug("stopping an audio client failed (device already gone)")
        self._capture = None
        self._client = None


class WindowsAudioSessions:
    """Which processes are audible right now, across every active playback device."""

    def __init__(self) -> None:
        self._managers: list = []
        self._refreshed = float("-inf")
        self._names: dict[int, str] = {}

    def active_processes(self) -> frozenset[str] | None:
        """None (unknown) when the OS could not be asked, as opposed to nothing being audible."""
        try:
            self._refresh_managers()
            names: set[str] = set()
            for manager in self._managers:
                names.update(self._audible(manager))
        except comtypes.COMError, OSError:
            self._refreshed = float("-inf")  # devices changed under us: look them up again
            return None
        return frozenset(names)

    def _refresh_managers(self) -> None:
        if time.monotonic() - self._refreshed < _SESSION_REFRESH_S:
            return
        self._refreshed = time.monotonic()
        self._names.clear()
        managers = []
        devices = _enumerator().EnumAudioEndpoints(_E_RENDER, _STATE_ACTIVE)
        for i in range(devices.GetCount()):
            try:
                interface = devices.Item(i).Activate(IAudioSessionManager2._iid_, _CLSCTX_ALL, None)
                managers.append(interface.QueryInterface(IAudioSessionManager2))
            except comtypes.COMError, OSError:
                log.debug("playback device has no session manager")
        self._managers = managers

    def _audible(self, manager) -> set[str]:  # noqa: ANN001
        found: set[str] = set()
        sessions = manager.GetSessionEnumerator()
        for i in range(sessions.GetCount()):
            control = sessions.GetSession(i)
            if control.GetState() != _SESSION_ACTIVE:
                continue
            if control.QueryInterface(IAudioMeterInformation).GetPeakValue() < _AUDIBLE_PEAK:
                continue
            pid = control.QueryInterface(IAudioSessionControl2).GetProcessId()
            name = self._process_name(pid) if pid else ""
            if name:
                found.add(name)
        return found

    def _process_name(self, pid: int) -> str:
        cached = self._names.get(pid)
        if cached is not None:
            return cached
        name = image_name(pid)
        self._names[pid] = name
        return name
