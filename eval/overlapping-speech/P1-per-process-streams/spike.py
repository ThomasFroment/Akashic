"""P1 spike on the target PC (numbers only): is "endpoint loopback minus per-process stems" a clean
call track, and what do 1-3 process-loopback clients cost on the capture side?

    uv run python eval/overlapping-speech/P1-per-process-streams/spike.py <out.json> [cable|dt770|cpu|fircpu ...]

Children render harness corpus speech (eval/quality/assets banks) with WASAPI shared mode, stream
category Other, float32 stereo 48 kHz: into "CABLE Input" (inaudible) or, briefly and at about
-45 dBFS, into the default DT-770 endpoint. The parent records, at the same time,
  - endpoint loopback of that endpoint as Akashic opens it (float32 mono, AUTOCONVERTPCM |
    SRC_DEFAULT_QUALITY, 1 s buffer, 10 ms polling), at 16 kHz and at 48 kHz;
  - one process-loopback include-tree client per child (VAD\\Process_Loopback, LOOPBACK |
    EVENTCALLBACK | AUTOCONVERTPCM | SRC_DEFAULT_QUALITY, float32 mono, 2 s buffer), 16 and 48 kHz.
Source A plays alone first, then B joins (B = "the call": its stem is used only as the reference
for what the residual should be). Per 0.5 s block, an 8-tap FIR per stem is fitted by lstsq.
  depth_alone   residual vs mix where A plays alone (mix - A fit)                     dB
  leak          (mix - A fit) - B part, vs A part, where both play: what of A stays in the
                call residual when only A is subtracted (B part from the joint A+B fit)   dB
  joint         mix - (A fit + B fit) vs mix where both play                           dB
Each reported as energy-weighted over blocks ("rms"), median and worst block. Also a single FIR
fitted on the first 4 s of A alone and kept (drift), and a 1-tap gain at the best integer lag.
Nothing but numbers is printed or written; no audio is kept.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
from ctypes import POINTER, Structure, byref, c_float, c_int, c_ulong, c_ushort, c_void_p, cast, wintypes
from pathlib import Path

import numpy as np

from akashic.platform.win import audio as wa  # sets COM to multithreaded before comtypes

import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "eval" / "quality"))

K32 = ctypes.windll.kernel32
_EVENTCALLBACK = 0x00040000
BLOCK_S = 0.5
TAPS = 8
CENTER = 3  # taps cover lags -3..+4 samples around the integer alignment
LONG_S = 8.0


# --- render child ------------------------------------------------------------------------------


class IAudioRenderClient(IUnknown):
    _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer", (["in"], ctypes.c_uint32, "n"), (["out"], POINTER(POINTER(ctypes.c_ubyte)), "ppData")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], ctypes.c_uint32, "n"), (["in"], wintypes.DWORD, "flags")),
    )


def render_child(endpoint: str, bank: str, gain: float, lead_s: float, play_s: float) -> None:
    """Render `play_s` of a corpus bank after `lead_s` of digital silence, float stereo 48 kHz."""
    from scipy.signal import resample_poly

    import assets

    x = assets.load_bank(bank)[: int(play_s * 16000)].astype(np.float64)
    x = (resample_poly(x, 3, 1) * gain).astype(np.float32)
    sig = np.concatenate([np.zeros(int(lead_s * 48000), np.float32), x, np.zeros(48000 // 2, np.float32)])
    wa._ensure_com()
    if endpoint == "default":
        dev = wa._enumerator().GetDefaultAudioEndpoint(0, 0)
    else:
        dev = wa._enumerator().GetDevice(wa.WindowsAudioDevices().find("system", endpoint).device_id)
    client = dev.Activate(wa.IAudioClient._iid_, wa._CLSCTX_ALL, None).QueryInterface(wa.IAudioClient)
    sr = 48000
    fmt = wa.WAVEFORMATEX(3, 2, sr, sr * 8, 8, 32, 0)
    client.Initialize(0, wa._FLAG_AUTOCONVERTPCM | wa._FLAG_SRC_DEFAULT_QUALITY, 2_000_000, 0, byref(fmt), None)
    size = client.GetBufferSize()
    render = client.GetService(byref(IAudioRenderClient._iid_)).QueryInterface(IAudioRenderClient)
    print(os.getpid(), flush=True)
    sys.stdin.readline()  # wait for "go": the parent's capture clients are open
    client.Start()
    pos = 0
    while pos < len(sig):
        avail = size - client.GetCurrentPadding()
        if avail:
            ptr = render.GetBuffer(avail)
            buf = np.ctypeslib.as_array(cast(ptr, POINTER(c_float)), (avail * 2,))
            chunk = sig[pos:pos + avail]
            buf[:] = 0
            buf[0:2 * len(chunk):2] = chunk
            buf[1:2 * len(chunk):2] = chunk
            render.ReleaseBuffer(avail, 0)
            pos += avail
        time.sleep(0.01)
    time.sleep(0.3)
    client.Stop()


# --- process loopback activation (as eval/quality/spikes/process_loopback_control.py) -----------


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (COMMETHOD([], HRESULT, "GetActivateResult", (["out"], POINTER(HRESULT), "hr"), (["out"], POINTER(POINTER(IUnknown)), "punk")),)


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
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort), ("cbSize", c_ulong), ("pad", c_ulong), ("pBlobData", c_void_p)]


def open_process(pid: int, rate: int):
    params = ActivationParams(1, pid, 0)
    pv = BlobPropVariant(65, 0, 0, 0, ctypes.sizeof(params), 0, ctypes.addressof(params))
    handler = Handler()
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    activate = ctypes.WinDLL("Mmdevapi").ActivateAudioInterfaceAsync
    activate.argtypes = [ctypes.c_wchar_p, POINTER(GUID), c_void_p, c_void_p, POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    t = time.perf_counter()
    hr = activate("VAD\\Process_Loopback", byref(wa.IAudioClient._iid_), ctypes.addressof(pv), handler.QueryInterface(IActivateAudioInterfaceCompletionHandler), byref(op))
    if hr != 0 or not handler.done.wait(5):
        raise RuntimeError(f"activate 0x{hr & 0xFFFFFFFF:08x}")
    result, punk = op.GetActivateResult()
    if result != 0:
        raise RuntimeError(f"activate result 0x{result & 0xFFFFFFFF:08x}")
    activate_ms = (time.perf_counter() - t) * 1000
    client = punk.QueryInterface(wa.IAudioClient)
    fmt = wa.WAVEFORMATEX(3, 1, rate, rate * 4, 4, 32, 0)
    client.Initialize(0, wa._FLAG_LOOPBACK | _EVENTCALLBACK | wa._FLAG_AUTOCONVERTPCM | wa._FLAG_SRC_DEFAULT_QUALITY, 20_000_000, 0, byref(fmt), None)
    event = K32.CreateEventW(None, False, False, None)
    client.SetEventHandle(event)
    cap = client.GetService(byref(wa.IAudioCaptureClient._iid_)).QueryInterface(wa.IAudioCaptureClient)
    return client, cap, event, activate_ms


def open_endpoint(endpoint: str, rate: int):
    """Akashic's system-stream client (platform/win/audio._WasapiInput) at `rate`."""
    if endpoint == "default":
        dev = wa._enumerator().GetDefaultAudioEndpoint(0, 0)
    else:
        dev = wa._enumerator().GetDevice(wa.WindowsAudioDevices().find("system", endpoint).device_id)
    client = dev.Activate(wa.IAudioClient._iid_, wa._CLSCTX_ALL, None).QueryInterface(wa.IAudioClient)
    fmt = wa.WAVEFORMATEX(3, 1, rate, rate * 4, 4, 32, 0)
    client.Initialize(0, wa._FLAG_AUTOCONVERTPCM | wa._FLAG_SRC_DEFAULT_QUALITY | wa._FLAG_LOOPBACK, 10_000_000, 0, byref(fmt), None)
    cap = client.GetService(byref(wa.IAudioCaptureClient._iid_)).QueryInterface(wa.IAudioCaptureClient)
    return client, cap, None, 0.0


def mix_format(endpoint: str) -> dict:
    dev = wa._enumerator().GetDefaultAudioEndpoint(0, 0) if endpoint == "default" else wa._enumerator().GetDevice(wa.WindowsAudioDevices().find("system", endpoint).device_id)
    client = dev.Activate(wa.IAudioClient._iid_, wa._CLSCTX_ALL, None).QueryInterface(wa.IAudioClient)
    f = client.GetMixFormat().contents
    return {"rate": f.nSamplesPerSec, "channels": f.nChannels, "bits": f.wBitsPerSample}


class Rec(threading.Thread):
    def __init__(self, name: str, opener, keep: bool = True) -> None:
        super().__init__(daemon=True)
        self.name, self.opener, self.keep = name, opener, keep
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.chunks: list[np.ndarray] = []
        self.qpc0 = None
        self.packets = self.disc = self.silent = 0
        self.frames = 0
        self.activate_ms = 0.0
        self.error = None

    def run(self) -> None:
        try:
            wa._ensure_com()
            client, cap, event, self.activate_ms = self.opener()
            client.Start()
        except Exception as exc:  # noqa: BLE001
            self.error = repr(exc)
            self.ready.set()
            return
        self.ready.set()
        while not self.stop.is_set():
            if event:
                K32.WaitForSingleObject(event, 50)
            else:
                time.sleep(0.01)
            n = cap.GetNextPacketSize()
            while n:
                data, frames, flags, _dev, qpc = cap.GetBuffer()
                if self.qpc0 is None:
                    self.qpc0 = qpc
                self.packets += 1
                self.disc += bool(flags & 1)
                self.silent += bool(flags & 2)
                if self.keep:
                    if flags & 2:
                        self.chunks.append(np.zeros(frames, np.float32))
                    else:
                        self.chunks.append(np.ctypeslib.as_array(cast(data, POINTER(c_float)), (frames,)).copy())
                else:
                    if not flags & 2:
                        np.ctypeslib.as_array(cast(data, POINTER(c_float)), (frames,)).copy()
                self.frames += frames
                cap.ReleaseBuffer(frames)
                n = cap.GetNextPacketSize()
        client.Stop()

    def audio(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(1, np.float32)


def spawn(endpoint: str, bank: str, gain: float, lead: float, play: float):
    p = subprocess.Popen([sys.executable, __file__, "--render", endpoint, bank, str(gain), str(lead), str(play)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=str(REPO))
    pid = int(p.stdout.readline())
    return p, pid


# --- analysis ----------------------------------------------------------------------------------


def db(num: float, den: float) -> float:
    return 10 * math.log10(max(num, 1e-30) / max(den, 1e-30))


def align(ref: np.ndarray, x: np.ndarray, rate: int, search_s: float = 0.5) -> int:
    """Integer L with x[i] ~ ref[i + L] (FFT cross-correlation on the whole capture)."""
    from numpy.fft import irfft, rfft

    n = 1 << (len(ref) + len(x)).bit_length()
    cc = irfft(rfft(ref.astype(np.float64), n) * np.conj(rfft(x.astype(np.float64), n)), n)
    m = int(search_s * rate)
    cand = np.concatenate([cc[-m:], cc[:m + 1]])  # lags -m..m
    return int(np.argmax(np.abs(cand))) - m


def shifted(x: np.ndarray, lag: int, n: int) -> np.ndarray:
    """y[i] = x[i - lag] over 0..n (zeros outside)."""
    y = np.zeros(n, np.float64)
    lo, hi = max(0, lag), min(n, len(x) + lag)
    if hi > lo:
        y[lo:hi] = x[lo - lag:hi - lag]
    return y


def design(x_al: np.ndarray, a: int, b: int) -> np.ndarray:
    return np.stack([x_al[a - (j - CENTER):b - (j - CENTER)] for j in range(TAPS)], axis=1)


def fit(cols: list[np.ndarray], y: np.ndarray):
    a = np.concatenate(cols, axis=1)
    h, *_ = np.linalg.lstsq(a, y, rcond=None)
    return a, h


def analyse(mix: np.ndarray, sa: np.ndarray, sb: np.ndarray, rate: int) -> dict:
    la, lb = align(mix, sa, rate), align(mix, sb, rate)
    n = len(mix)
    xa, xb = shifted(sa, la, n), shifted(sb, lb, n)
    y = mix.astype(np.float64)
    blk = int(BLOCK_S * rate)
    pad = TAPS + 2
    alone, both = [], []
    lag_track = []
    for a in range(pad, n - blk - pad, blk):
        b = a + blk
        ea, eb = np.mean(xa[a:b] ** 2), np.mean(xb[a:b] ** 2)
        if ea < 1e-8:
            continue
        A = design(xa, a, b)
        if eb < 1e-10:
            _, h = fit([A], y[a:b])
            r = y[a:b] - A @ h
            g1 = np.dot(xa[a:b], y[a:b]) / max(np.dot(xa[a:b], xa[a:b]), 1e-30)
            alone.append({"a": a, "res": float(np.sum(r ** 2)), "mix": float(np.sum(y[a:b] ** 2)),
                          "res_gain": float(np.sum((y[a:b] - g1 * xa[a:b]) ** 2)), "h_peak": int(np.argmax(np.abs(h))) - CENTER})
            lag_track.append(int(np.argmax(np.abs(h))) - CENTER)
        elif eb > 1e-8:
            B = design(xb, a, b)
            _, ha = fit([A], y[a:b])
            AB, hab = fit([A, B], y[a:b])
            part_a, part_b = A @ hab[:TAPS], B @ hab[TAPS:]
            leak = (y[a:b] - A @ ha) - part_b
            # long-window fit: A's FIR by lstsq over the last LONG_S seconds (call present)
            w0 = max(pad, b - int(LONG_S * rate))
            if (b - w0) * TAPS <= 3e7:
                _, hl = fit([design(xa, w0, b)], y[w0:b])
                leak_long = (y[a:b] - A @ hl) - part_b
            else:
                leak_long = leak * np.nan
            both.append({"a": a, "leak": float(np.sum(leak ** 2)), "part_a": float(np.sum(part_a ** 2)), "part_b": float(np.sum(part_b ** 2)),
                         "leak_long": float(np.sum(leak_long ** 2)), "A": A, "y": y[a:b].copy(), "pb": part_b,
                         "joint": float(np.sum((y[a:b] - AB @ hab) ** 2)), "mix": float(np.sum(y[a:b] ** 2))})
    out = {"lag_a": la, "lag_b": lb, "blocks_alone": len(alone), "blocks_both": len(both),
           "fir_peak_tap_values": sorted(set(lag_track))}
    if TAPS == 8:
        out.update(tracked(y, sa, la, alone, blk, rate))
    if alone:
        per = [db(x["res"], x["mix"]) for x in alone]
        out["depth_alone_db"] = {"rms": db(sum(x["res"] for x in alone), sum(x["mix"] for x in alone)), "median": statistics.median(per), "worst": max(per), "per_block": [round(v, 1) for v in per]}
        perg = [db(x["res_gain"], x["mix"]) for x in alone]
        out["depth_alone_gain_1tap_db"] = {"rms": db(sum(x["res_gain"] for x in alone), sum(x["mix"] for x in alone)), "median": statistics.median(perg), "worst": max(perg)}
        # single FIR fitted on the first 4 s of A alone, kept for the rest (drift / time variance)
        a0 = alone[0]["a"]
        a1 = min(a0 + int(4 * rate), a0 + int(3e7 // TAPS), alone[-1]["a"] + blk)
        _, h0 = fit([design(xa, a0, a1)], y[a0:a1])
        fixed = []
        for x in alone:
            a, b = x["a"], x["a"] + blk
            fixed.append((float(np.sum((y[a:b] - design(xa, a, b) @ h0) ** 2)), x["mix"]))
        out["depth_alone_fixed_fir_db"] = {"rms": db(sum(r for r, _ in fixed), sum(m for _, m in fixed)), "worst": max(db(r, m) for r, m in fixed)}
    if both:
        per = [db(x["leak"], x["part_a"]) for x in both]
        out["leak_db"] = {"rms": db(sum(x["leak"] for x in both), sum(x["part_a"] for x in both)), "median": statistics.median(per), "worst": max(per), "per_block": [round(v, 1) for v in per],
                          "vs_call_rms": db(sum(x["leak"] for x in both), sum(x["part_b"] for x in both))}
        per = [db(x["leak_long"], x["part_a"]) for x in both]
        out["leak_long_window_db"] = {"window_s": LONG_S, "rms": db(sum(x["leak_long"] for x in both), sum(x["part_a"] for x in both)), "median": statistics.median(per), "worst": max(per),
                                      "per_block": [round(v, 1) for v in per], "vs_call_rms": db(sum(x["leak_long"] for x in both), sum(x["part_b"] for x in both))}
        if alone:  # FIR learned while the call was silent (voice-app session meter low), held while it talks
            per, num, den, numb = [], 0.0, 0.0, 0.0
            for x in both:
                e = float(np.sum((x["y"] - x["A"] @ h0 - x["pb"]) ** 2))
                pa = x["part_a"]
                per.append(db(e, pa))
                num, den, numb = num + e, den + pa, numb + x["part_b"]
            out["leak_held_fir_db"] = {"rms": db(num, den), "median": statistics.median(per), "worst": max(per), "per_block": [round(v, 1) for v in per], "vs_call_rms": db(num, numb)}
        pj = [db(x["joint"], x["mix"]) for x in both]
        out["joint_db"] = {"rms": db(sum(x["joint"] for x in both), sum(x["mix"] for x in both)), "median": statistics.median(pj), "worst": max(pj)}
        out["sir_b_over_a_db"] = db(sum(x["part_b"] for x in both), sum(x["part_a"] for x in both))
    return out


def tracked(y: np.ndarray, sa: np.ndarray, la: int, alone: list[dict], blk: int, rate: int) -> dict:
    """Where A plays alone: per-block integer lag (cross-correlation within +-20 ms of the global
    lag), the drift it implies, and the depth with the lag tracked per block (8 and 32 taps)."""
    if not alone:
        return {}
    m = int(0.02 * rate)
    xa = sa.astype(np.float64)
    lags, d8, d32, mix = [], [], [], []
    for x in alone:
        a, b = x["a"], x["a"] + blk
        best, bl = -1.0, 0
        seg = y[a:b]
        for L in range(la - m, la + m + 1):
            i0, i1 = a - L, b - L
            if i0 < 0 or i1 > len(xa):
                continue
            v = abs(float(np.dot(seg, xa[i0:i1]))) / max(float(np.linalg.norm(xa[i0:i1])), 1e-12)
            if v > best:
                best, bl = v, L
        lags.append(bl)
        xs = shifted(xa, bl, len(y))
        for taps, acc in ((8, d8), (32, d32)):
            cen = taps // 2 - 1
            if a - taps < 0 or b + taps > len(y):
                continue
            A = np.stack([xs[a - (j - cen):b - (j - cen)] for j in range(taps)], axis=1)
            h, *_ = np.linalg.lstsq(A, seg, rcond=None)
            acc.append(float(np.sum((seg - A @ h) ** 2)))
        mix.append(float(np.sum(seg ** 2)))
    t = np.array([x["a"] for x in alone]) / rate
    slope = float(np.polyfit(t, np.array(lags, float), 1)[0]) if len(lags) > 2 else None
    # linearity: magnitude-squared coherence mix vs stem over the A-alone stretch; 1 - coherence is
    # the best any linear time-invariant filter (any length) can leave, per frequency
    from scipy.signal import coherence, welch

    a0, a1 = alone[0]["a"], alone[-1]["a"] + blk
    xs = shifted(xa, la, len(y))
    nper = 4096 if rate == 48000 else 1024
    f, c = coherence(xs[a0:a1], y[a0:a1], fs=rate, nperseg=nper)
    _, pyy = welch(y[a0:a1], fs=rate, nperseg=nper)
    lin = db(float(np.sum((1 - c) * pyy)), float(np.sum(pyy)))
    # long FIR (128 taps) fitted on the first 3 s of A alone, held on the rest
    taps, cen = 128, 16
    l0, l1 = a0 + taps, min(a0 + int(3 * rate), a1)
    A0 = np.stack([xs[l0 - (j - cen):l1 - (j - cen)] for j in range(taps)], axis=1)
    h, *_ = np.linalg.lstsq(A0, y[l0:l1], rcond=None)
    r1 = l1 + 8 * blk if a1 - l1 > 8 * blk else a1 - taps
    A1 = np.stack([xs[l1 - (j - cen):r1 - (j - cen)] for j in range(taps)], axis=1)
    long_db = db(float(np.sum((y[l1:r1] - A1 @ h) ** 2)), float(np.sum(y[l1:r1] ** 2))) if r1 > l1 else None
    return {"tracked_lags": lags, "drift_ppm": None if slope is None else slope / rate * 1e6,
            "depth_alone_tracked8_db": db(sum(d8), sum(mix[:len(d8)])) if d8 else None,
            "depth_alone_tracked32_db": db(sum(d32), sum(mix[:len(d32)])) if d32 else None,
            "lti_bound_db": lin, "coherence_median": float(np.median(c[(f > 100) & (f < 7000)])),
            "depth_alone_fir128_held_db": long_db}


def rms_db(x: np.ndarray) -> float:
    return 10 * math.log10(max(float(np.mean(x.astype(np.float64) ** 2)), 1e-30))


def accuracy_case(endpoint: str, gain_a: float, gain_b: float, lead_a: float, play_a: float, lead_b: float, play_b: float) -> dict:
    ca, pa = spawn(endpoint, "video_en1", gain_a, lead_a, play_a)
    cb, pb = spawn(endpoint, "call_fr1", gain_b, lead_b, play_b)
    recs = []
    for rate in (16000, 48000):
        recs.append(Rec(f"mix{rate}", lambda r=rate: open_endpoint(endpoint, r)))
        recs.append(Rec(f"A{rate}", lambda r=rate: open_process(pa, r)))
        recs.append(Rec(f"B{rate}", lambda r=rate: open_process(pb, r)))
    for r in recs:
        r.start()
        r.ready.wait(10)
    time.sleep(0.3)
    for c in (ca, cb):
        c.stdin.write("go\n")
        c.stdin.flush()
    total = max(lead_a + play_a, lead_b + play_b) + 1.2
    time.sleep(total)
    for r in recs:
        r.stop.set()
    for r in recs:
        r.join(5)
    for c in (ca, cb):
        c.wait(15)
    by = {r.name: r for r in recs}
    out = {"endpoint": endpoint, "mix_format": mix_format(endpoint),
           "clients": {r.name: {"packets": r.packets, "discontinuities": r.disc, "silent_packets": r.silent, "frames": r.frames, "activate_ms": round(r.activate_ms, 1),
                                "error": r.error, "rms_dbfs": round(rms_db(r.audio()), 1)} for r in recs}}
    for rate in (16000, 48000):
        mix = by[f"mix{rate}"].audio()
        floor = mix[: int(0.25 * rate)]  # before the children start
        res = analyse(mix, by[f"A{rate}"].audio(), by[f"B{rate}"].audio(), rate)
        global TAPS, CENTER
        TAPS, CENTER = (128, 16) if rate == 16000 else (384, 48)
        res[f"fir{TAPS}"] = analyse(mix, by[f"A{rate}"].audio(), by[f"B{rate}"].audio(), rate)
        TAPS, CENTER = 8, 3
        res["floor_dbfs_before_play"] = round(rms_db(floor), 1)
        qa, qm = by[f"A{rate}"].qpc0, by[f"mix{rate}"].qpc0
        res["qpc_first_packet_offset_ms_A_vs_mix"] = None if qa is None or qm is None else (qa - qm) / 1e4
        out[str(rate)] = res
    return out


def cpu_case(endpoint: str = "CABLE Input", windows: int = 9, win_s: float = 10.0) -> dict:
    """Capture-process CPU of endpoint loopback (16 kHz, polling) plus 0..3 process clients
    (16 kHz, event) while three children play speech into CABLE Input."""
    play = 4 * (1.5 + windows * win_s) + 10
    kids = [spawn(endpoint, bank, 0.5, 0.0, play) for bank in ("video_en1", "call_fr1", "spare_en1")]
    for c, _ in kids:
        c.stdin.write("go\n")
        c.stdin.flush()
    time.sleep(1.0)
    out = {}
    adg = audiodg_handle()
    for n in range(4):
        recs = [Rec("mix", lambda: open_endpoint(endpoint, 16000), keep=False)]
        recs += [Rec(f"P{i}", lambda pid=kids[i][1]: open_process(pid, 16000), keep=False) for i in range(n)]
        for r in recs:
            r.start()
            r.ready.wait(10)
        time.sleep(1.5)
        cpu, dg = [], []
        for _ in range(windows):
            w0, c0, d0 = time.perf_counter(), time.process_time(), proc_cpu(adg)
            time.sleep(win_s)
            w1, c1, d1 = time.perf_counter(), time.process_time(), proc_cpu(adg)
            cpu.append((c1 - c0) / (w1 - w0) * 100)
            if d0 is not None and d1 is not None:
                dg.append((d1 - d0) / (w1 - w0) * 100)
        for r in recs:
            r.stop.set()
        for r in recs:
            r.join(5)
        out[f"process_clients_{n}"] = {"capture_cpu_pct_median": statistics.median(cpu), "capture_cpu_pct_all": [round(v, 2) for v in cpu],
                                       "audiodg_cpu_pct_median": statistics.median(dg) if dg else None,
                                       "errors": [r.error for r in recs if r.error], "packets": [r.packets for r in recs]}
    for c, _ in kids:
        c.kill()
    base = out["process_clients_0"]["capture_cpu_pct_median"]
    for n in range(1, 4):
        out[f"process_clients_{n}"]["delta_vs_endpoint_only_pct"] = out[f"process_clients_{n}"]["capture_cpu_pct_median"] - base
    return out


def audiodg_handle():
    import subprocess as sp

    try:
        txt = sp.run(["tasklist", "/FI", "IMAGENAME eq audiodg.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10).stdout
        pid = int(txt.strip().splitlines()[0].split(",")[1].strip('"'))
    except Exception:  # noqa: BLE001
        return None
    h = K32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    return h or None


def proc_cpu(h) -> float | None:
    if not h:
        return None
    ft = [wintypes.FILETIME() for _ in range(4)]
    if not K32.GetProcessTimes(h, *(byref(f) for f in ft)):
        return None
    tot = 0
    for f in ft[2:]:
        tot += (f.dwHighDateTime << 32 | f.dwLowDateTime)
    return tot / 1e7


def fir_cpu() -> dict:
    """CPU seconds per audio second of the per-block lstsq FIR subtraction, K = 1..3 stems,
    16 kHz and 48 kHz, 0.5 s blocks (median of 9)."""
    rng = np.random.default_rng(0)
    out = {}
    for rate in (16000, 48000):
        n = 20 * rate
        blk = int(BLOCK_S * rate)
        for k in (1, 2, 3):
            xs = [rng.standard_normal(n + 16) for _ in range(k)]
            y = sum(xs)[:n] + 0.1 * rng.standard_normal(n)
            ts = []
            for rep in range(10):
                c = time.process_time()
                for a in range(8, n - blk, blk):
                    cols = [design(x, a, a + blk) for x in xs]
                    A, h = fit(cols, y[a:a + blk])
                    _ = y[a:a + blk] - A @ h
                if rep:
                    ts.append(time.process_time() - c)
            out[f"{rate}_K{k}_pct_core"] = statistics.median(ts) / 20 * 100
    # P1 as built: 128-tap FIR per stem at 16 kHz, joint lstsq refit on 2 s every 10 s, applied by
    # FFT convolution over each 60 s chunk (candidate.py)
    from scipy.signal import oaconvolve

    rate, n = 16000, 120 * 16000
    for (refit, fitn, tag) in ((160000, 32000, "r10_f2"), (960000, 16000, "r60_f1")):
     for k in (1, 2, 3):
        xs = [rng.standard_normal(n).astype(np.float32) for _ in range(k)]
        y = (sum(xs) + 0.1 * rng.standard_normal(n)).astype(np.float32)
        ts = []
        for rep in range(10):
            c = time.process_time()
            hs = []
            for i in range(0, n - fitn, refit):
                i0, i1 = max(i, 128), i + fitn
                hs = hs or [np.eye(128)[16] for _ in xs]
                for q, x in enumerate(xs):  # per stem: mix minus the other stems through their held FIR
                    tgt = y[i0:i1].astype(np.float64)
                    for p, (xo, ho) in enumerate(zip(xs, hs, strict=True)):
                        if p != q:
                            tgt -= np.convolve(xo[i0 - 128:i1 + 128].astype(np.float64), ho)[128 + 16:128 + 16 + i1 - i0]
                    col = np.stack([x[i0 - (j - 16):i1 - (j - 16)] for j in range(128)], axis=1).astype(np.float64)
                    hs[q], *_ = np.linalg.lstsq(col, tgt, rcond=None)
            r = y.astype(np.float64)
            for x, h in zip(xs, hs, strict=True):
                r -= oaconvolve(x.astype(np.float64), h)[16:16 + n]
            if rep:
                ts.append(time.process_time() - c)
        out[f"held128_perstem_16000_{tag}_K{k}_pct_core"] = statistics.median(ts) / 120 * 100
    return out


def main() -> None:
    if sys.argv[1] == "--render":
        render_child(sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6]))
        return
    path = Path(sys.argv[1])
    which = sys.argv[2:] or ["cable", "dt770", "cpu", "fircpu"]
    res = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    for w in which:
        if w == "cable":
            res["cable"] = accuracy_case("CABLE Input", 1.0, 0.6, 0.5, 30.0, 12.0, 19.0)
        elif w == "dt770":
            res["dt770"] = accuracy_case("default", 0.02, 0.012, 0.5, 14.0, 6.5, 8.0)
        elif w == "cpu":
            res["cpu"] = cpu_case()
        elif w == "fircpu":
            res["fir_cpu"] = fir_cpu()
        path.write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(w, "done", flush=True)


if __name__ == "__main__":
    main()
