"""Positive control for per-process loopback, silent for the user (VB-CABLE).

A child renders a -15 dBFS 440 Hz tone with WASAPI into "CABLE Input", with a chosen audio stream
category (Other or Communications, IAudioClient2::SetClientProperties). The parent captures that
child only, with process loopback (VAD\\Process_Loopback, include tree) and the Microsoft
ApplicationLoopback sample's parameters (PCM16 stereo 44.1 kHz, LOOPBACK | EVENTCALLBACK |
AUTOCONVERTPCM, buffer 0, event-driven), plus a run with an unrelated pid as negative control.
Prints numbers only: RMS and peak in dBFS of what was captured, per case.

    uv run python eval/quality/spikes/process_loopback_control.py <out.json>
"""

import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
from ctypes import POINTER, Structure, byref, c_float, c_int, c_ubyte, c_ulong, c_ushort, c_void_p, cast, wintypes
from pathlib import Path

import comtypes
import numpy as np
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

from akashic.platform.win import audio as wa

CATEGORY = {"other": 0, "communications": 3}


class IAudioClient2(wa.IAudioClient):
    _iid_ = GUID("{726778CD-F60A-4eda-82DE-E47610CD78AA}")
    _methods_ = (
        COMMETHOD([], HRESULT, "IsOffloadCapable", (["in"], c_int, "Category"), (["out"], POINTER(wintypes.BOOL), "pbOffloadCapable")),
        COMMETHOD([], HRESULT, "SetClientProperties", (["in"], c_void_p, "pProperties")),
        COMMETHOD([], HRESULT, "GetBufferSizeLimits", (["in"], c_void_p, "fmt"), (["in"], wintypes.BOOL, "ev"),
                  (["out"], POINTER(ctypes.c_longlong), "mn"), (["out"], POINTER(ctypes.c_longlong), "mx")),
    )


class IAudioRenderClient(IUnknown):
    _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer", (["in"], ctypes.c_uint32, "n"), (["out"], POINTER(POINTER(c_ubyte)), "ppData")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], ctypes.c_uint32, "n"), (["in"], wintypes.DWORD, "flags")),
    )


class AudioClientProperties(Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("bIsOffload", wintypes.BOOL), ("eCategory", c_int), ("Options", c_int)]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(HRESULT), "hr"), (["out"], POINTER(POINTER(IUnknown)), "punk")),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (COMMETHOD([], HRESULT, "ActivateCompleted", (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),)


class IAgileObject(IUnknown):
    _iid_ = GUID("{94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90}")
    _methods_ = ()


class Handler(comtypes.COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self) -> None:
        super().__init__()
        self.done = threading.Event()

    def ActivateCompleted(self, op):  # noqa: N802
        self.done.set()
        return 0


class ActivationParams(Structure):
    _fields_ = [("ActivationType", c_int), ("TargetProcessId", c_ulong), ("ProcessLoopbackMode", c_int)]


class BlobPropVariant(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("cbSize", c_ulong), ("pad", c_ulong), ("pBlobData", c_void_p)]


def render_child(category: str, seconds: float) -> None:
    """Child role: WASAPI render of a -15 dBFS tone into CABLE Input with the given category."""
    wa._ensure_com()
    cable = wa.WindowsAudioDevices().find("system", "CABLE Input")
    endpoint = wa._enumerator().GetDevice(cable.device_id)
    client = endpoint.Activate(IAudioClient2._iid_, wa._CLSCTX_ALL, None).QueryInterface(IAudioClient2)
    props = AudioClientProperties(16, 0, CATEGORY[category], 0)
    client.SetClientProperties(ctypes.addressof(props))
    sr = 48000
    fmt = wa.WAVEFORMATEX(3, 2, sr, sr * 8, 8, 32, 0)  # IEEE float stereo
    client.Initialize(0, wa._FLAG_AUTOCONVERTPCM | wa._FLAG_SRC_DEFAULT_QUALITY, 2_000_000, 0, byref(fmt), None)
    size = client.GetBufferSize()
    render = client.GetService(byref(IAudioRenderClient._iid_)).QueryInterface(IAudioRenderClient)
    amp = 10 ** (-15 / 20) * math.sqrt(2)
    phase = 0
    print(os.getpid(), flush=True)  # the pid that owns the render session
    client.Start()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        avail = size - client.GetCurrentPadding()
        if avail:
            ptr = render.GetBuffer(avail)
            t = (np.arange(avail) + phase) / sr
            tone = (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
            buf = np.ctypeslib.as_array(cast(ptr, POINTER(c_float)), (avail * 2,))
            buf[0::2] = tone
            buf[1::2] = tone
            render.ReleaseBuffer(avail, 0)
            phase += avail
        time.sleep(0.01)
    client.Stop()


def capture(pid: int, seconds: float) -> dict:
    """Process loopback of `pid`'s tree with the ApplicationLoopback sample's parameters."""
    wa._ensure_com()
    params = ActivationParams(1, pid, 0)  # PROCESS_LOOPBACK, include target process tree
    pv = BlobPropVariant(65, 0, 0, 0, ctypes.sizeof(params), 0, ctypes.addressof(params))  # VT_BLOB
    handler = Handler()
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    activate = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
    activate.argtypes = [ctypes.c_wchar_p, POINTER(GUID), c_void_p, c_void_p, POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    hr = activate("VAD\\Process_Loopback", byref(wa.IAudioClient._iid_), ctypes.addressof(pv),
                  handler.QueryInterface(IActivateAudioInterfaceCompletionHandler), byref(op))
    if hr != 0 or not handler.done.wait(5):
        return {"error": f"activate 0x{hr & 0xFFFFFFFF:08x}"}
    result, punk = op.GetActivateResult()
    if result != 0:
        return {"error": f"activate result 0x{result & 0xFFFFFFFF:08x}"}
    client = punk.QueryInterface(wa.IAudioClient)
    fmt = wa.WAVEFORMATEX(1, 2, 44100, 44100 * 4, 4, 16, 0)  # PCM16 stereo 44.1 kHz, as the sample
    client.Initialize(0, wa._FLAG_LOOPBACK | 0x00040000 | wa._FLAG_AUTOCONVERTPCM, 0, 0, byref(fmt), None)
    event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
    client.SetEventHandle(event)
    cap = client.GetService(byref(wa.IAudioCaptureClient._iid_)).QueryInterface(wa.IAudioCaptureClient)
    client.Start()
    chunks, silent, packets = [], 0, 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctypes.windll.kernel32.WaitForSingleObject(event, 100)
        n = cap.GetNextPacketSize()
        while n:
            data, frames, flags, _, _ = cap.GetBuffer()
            packets += 1
            if flags & 0x2:
                silent += 1
                chunks.append(np.zeros(frames * 2, dtype=np.int16))
            else:
                chunks.append(np.ctypeslib.as_array(cast(data, POINTER(ctypes.c_int16)), (frames * 2,)).copy())
            cap.ReleaseBuffer(frames)
            n = cap.GetNextPacketSize()
    client.Stop()
    pcm = np.concatenate(chunks).astype(np.float64) / 32768 if chunks else np.zeros(1)
    rms = float(np.sqrt(np.mean(pcm**2)))
    return {"packets": packets, "silent_flagged": silent, "samples": int(pcm.size),
            "rms_dbfs": round(20 * math.log10(max(rms, 1e-12)), 1), "peak_dbfs": round(20 * math.log10(max(float(np.abs(pcm).max()), 1e-12)), 1)}


def case(category: str, target: str) -> dict:
    child = subprocess.Popen([sys.executable, __file__, "--render", category, "6"], stdout=subprocess.PIPE, text=True)
    pid = int(child.stdout.readline())
    time.sleep(1.0)
    sleeper = None
    if target == "renderer":
        target_pid = pid  # the process that owns the render stream
    elif target == "launcher":
        target_pid = child.pid  # the venv launcher: the renderer is its child (tree inclusion)
    elif target == "unrelated":
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
        target_pid = sleeper.pid  # a silent process outside the renderer's tree
    else:
        target_pid = os.getpid()  # the capturer itself, an ancestor of the renderer
    out = {"category": category, "target": target, **capture(target_pid, 3.0)}
    child.wait(15)
    if sleeper:
        sleeper.wait(15)
    return out


def main() -> None:
    if sys.argv[1] == "--render":
        render_child(sys.argv[2], float(sys.argv[3]))
        return
    results = [case("other", "renderer"), case("other", "launcher"), case("other", "unrelated"),
               case("other", "self"), case("communications", "renderer"), case("communications", "launcher")]
    Path(sys.argv[1]).write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(json.dumps(results, indent=1))


main()
