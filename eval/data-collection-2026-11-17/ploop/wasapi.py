"""Minimal comtypes WASAPI bindings for the process-loopback spike (render + capture)."""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import POINTER, Structure, byref, c_float, c_int, c_longlong, c_ubyte, c_void_p, wintypes
from ctypes import c_uint32 as u32
from ctypes import c_uint64 as u64

sys.coinit_flags = 0
import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, IUnknown  # noqa: E402

HRESULT = ctypes.HRESULT


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


def fmt_float(rate: int, ch: int) -> WAVEFORMATEX:
    return WAVEFORMATEX(3, ch, rate, rate * 4 * ch, 4 * ch, 32, 0)


def fmt_pcm16(rate: int, ch: int) -> WAVEFORMATEX:
    return WAVEFORMATEX(1, ch, rate, rate * 2 * ch, 2 * ch, 16, 0)


class IAudioClient(IUnknown):
    _iid_ = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _methods_ = (
        COMMETHOD([], HRESULT, "Initialize", (["in"], c_int, "m"), (["in"], wintypes.DWORD, "f"),
                  (["in"], c_longlong, "b"), (["in"], c_longlong, "p"),
                  (["in"], POINTER(WAVEFORMATEX), "fmt"), (["in"], c_void_p, "g")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(u32), "n")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_longlong), "l")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(u32), "p")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], c_int, "m"),
                  (["in"], POINTER(WAVEFORMATEX), "f"), (["out"], POINTER(POINTER(WAVEFORMATEX)), "c")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "f")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(c_longlong), "a"),
                  (["out"], POINTER(c_longlong), "b")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "h")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppv")),
    )


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(POINTER(c_ubyte)), "d"),
                  (["out"], POINTER(u32), "n"), (["out"], POINTER(wintypes.DWORD), "f"),
                  (["out"], POINTER(u64), "p"), (["out"], POINTER(u64), "q")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], u32, "n")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(u32), "n")),
    )


class IAudioRenderClient(IUnknown):
    _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer", (["in"], u32, "n"), (["out"], POINTER(POINTER(c_ubyte)), "d")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], u32, "n"), (["in"], wintypes.DWORD, "f")),
    )


class ISimpleAudioVolume(IUnknown):
    _iid_ = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
    _methods_ = (
        COMMETHOD([], HRESULT, "SetMasterVolume", (["in"], c_float, "v"), (["in"], c_void_p, "g")),
        COMMETHOD([], HRESULT, "GetMasterVolume", (["out"], POINTER(c_float), "v")),
        COMMETHOD([], HRESULT, "SetMute", (["in"], wintypes.BOOL, "m"), (["in"], c_void_p, "g")),
        COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(wintypes.BOOL), "m")),
    )


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = (
        COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], wintypes.DWORD, "c"),
                  (["in"], c_void_p, "p"), (["out"], POINTER(POINTER(IUnknown)), "pp")),
        COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], wintypes.DWORD, "s"), (["out"], POINTER(c_void_p), "pp")),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_void_p), "id")),
        COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(wintypes.DWORD), "s")),
    )


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(u32), "n")),
        COMMETHOD([], HRESULT, "Item", (["in"], u32, "i"), (["out"], POINTER(POINTER(IMMDevice)), "d")),
    )


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = (
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "f"), (["in"], wintypes.DWORD, "s"),
                  (["out"], POINTER(POINTER(IMMDeviceCollection)), "d")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "f"), (["in"], c_int, "r"),
                  (["out"], POINTER(POINTER(IMMDevice)), "d")),
    )


CLSID_ENUM = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(HRESULT), "hr"),
                  (["out"], POINTER(POINTER(IUnknown)), "itf")),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
    )


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = ()


class BLOB(Structure):
    _fields_ = (("cbSize", wintypes.ULONG), ("pBlobData", c_void_p))


class PROPVARIANT_BLOB(Structure):
    _fields_ = (("vt", wintypes.WORD), ("r1", wintypes.WORD), ("r2", wintypes.WORD),
                ("r3", wintypes.WORD), ("blob", BLOB))


class ACTIVATION_PARAMS(Structure):
    _fields_ = (("ActivationType", c_int), ("TargetProcessId", wintypes.DWORD), ("Mode", c_int))


class Handler(comtypes.COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()
        self.hr = None
        self.client = None

    def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(self, this, op):  # noqa: N802
        try:
            hr, itf = op.GetActivateResult()
            self.hr = hr
            if hr == 0 and itf:
                self.client = itf.QueryInterface(IAudioClient)
        except Exception as exc:  # noqa: BLE001
            self.hr = f"exc {exc!r}"
        self.done.set()
        return 0


_mmdev = ctypes.WinDLL("Mmdevapi")
_activate = _mmdev.ActivateAudioInterfaceAsync
_activate.restype = HRESULT
_activate.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT_BLOB), c_void_p, POINTER(c_void_p)]


def process_loopback_client(pid: int, include: bool = True, timeout: float = 5.0):
    comtypes.CoInitializeEx(0) if False else None
    params = ACTIVATION_PARAMS(1, pid, 0 if include else 1)
    pv = PROPVARIANT_BLOB(65, 0, 0, 0, BLOB(ctypes.sizeof(params), ctypes.cast(ctypes.pointer(params), c_void_p)))
    handler = Handler()
    hptr = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    op = c_void_p()
    _activate("VAD\\Process_Loopback", byref(IAudioClient._iid_), byref(pv), ctypes.cast(hptr, c_void_p), byref(op))
    if not handler.done.wait(timeout):
        raise TimeoutError("activation did not complete")
    if handler.client is None:
        raise OSError(f"activation failed: {handler.hr}")
    return handler.client


def enumerator():
    return comtypes.CoCreateInstance(CLSID_ENUM, interface=IMMDeviceEnumerator)


def device_id(dev) -> str:
    p = dev.GetId()
    s = ctypes.wstring_at(p)
    ctypes.windll.ole32.CoTaskMemFree(c_void_p(p))
    return s
