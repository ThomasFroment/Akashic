"""Process-loopback spike. Numbers only; no audio is written anywhere."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
import time
from ctypes import POINTER, byref, c_float

import numpy as np

HERE = __file__.rsplit("\\", 1)[0]
sys.path.insert(0, HERE)
import comtypes  # noqa: E402

from wasapi import (  # noqa: E402
    IAudioCaptureClient, enumerator, fmt_float, process_loopback_client,
)

ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # below normal
RATE = 16000
import os
PY = sys._base_executable
ENV = dict(os.environ, PYTHONPATH=os.path.join(sys.prefix, "Lib", "site-packages"))
results: dict = {}


class Cap:
    def __init__(self, pid: int, include: bool = True, rate: int = RATE, ch: int = 1, convert: bool = True):
        self.client = process_loopback_client(pid, include)
        fmt = fmt_float(rate, ch)
        flags = 0x00020000 | (0x80000000 | 0x08000000 if convert else 0)
        self.client.Initialize(0, flags, 2_000_000, 0, byref(fmt), None)
        self.cap = self.client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
        self.ch = ch
        self.chunks: list[np.ndarray] = []
        self.silent_flags = 0
        self.client.Start()

    def poll(self):
        n = self.cap.GetNextPacketSize()
        while n:
            data, frames, flags, _, _ = self.cap.GetBuffer()
            if flags & 0x2:
                self.silent_flags += 1
                arr = np.zeros(frames * self.ch, np.float32)
            else:
                arr = np.ctypeslib.as_array(ctypes.cast(data, POINTER(c_float)), (frames * self.ch,)).copy()
            self.cap.ReleaseBuffer(frames)
            self.chunks.append(arr)
            n = self.cap.GetNextPacketSize()

    def signal(self):
        x = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)
        if self.ch > 1:
            x = x.reshape(-1, self.ch).mean(axis=1)
        return x

    def close(self):
        self.client.Stop()


def tone_stats(x: np.ndarray, freqs=(440.0, 1000.0), rate=RATE):
    if len(x) < rate // 2:
        return {"samples": int(len(x))}
    seg = x[len(x) // 4 :]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    f = np.fft.rfftfreq(len(seg), 1 / rate)
    total = float(np.sum(spec**2)) + 1e-12
    out = {"samples": int(len(x)), "rms": round(float(np.sqrt(np.mean(seg**2))), 5)}
    for fr in freqs:
        band = (f > fr - 15) & (f < fr + 15)
        out[f"share_{int(fr)}"] = round(float(np.sum(spec[band] ** 2)) / total, 3)
    return out


def child(freq, seconds, mode, endpoint=-1, delay=0.0):
    args = [PY, f"{HERE}\\tone.py", str(freq), str(seconds), mode, str(endpoint)]
    if delay:
        args = [PY, "-c", f"import time,subprocess,sys;time.sleep({delay});subprocess.run({args!r})"]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, env=ENV, creationflags=0x4000)
    return p


def run_caps(caps, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for c in caps:
            c.poll()
        time.sleep(0.01)


def cpu_s():
    k, u = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    c, e = ctypes.c_ulonglong(), ctypes.c_ulonglong()
    ctypes.windll.kernel32.GetProcessTimes(ctypes.windll.kernel32.GetCurrentProcess(), byref(c), byref(e), byref(k), byref(u))
    return (k.value + u.value) / 1e7


def t_format():
    """Which formats Initialize accepts."""
    out = {}
    p = child(440, 3, "mute")
    p.stdout.readline()
    for name, kw in {
        "16k_mono_float_autoconvert": dict(rate=16000, ch=1, convert=True),
        "48k_stereo_float_noconvert": dict(rate=48000, ch=2, convert=False),
        "16k_mono_float_noconvert": dict(rate=16000, ch=1, convert=False),
    }.items():
        try:
            c = Cap(p.pid, **kw)
            run_caps([c], 1.0)
            out[name] = {"ok": True, **tone_stats(c.signal(), rate=kw["rate"])}
            c.close()
        except Exception as exc:  # noqa: BLE001
            out[name] = {"ok": False, "err": repr(exc)[:160]}
    p.wait()
    return out


def t_mute_modes():
    out = {}
    for mode in ("mute", "vol0", "quiet"):
        p = child(440, 3, mode)
        p.stdout.readline()
        c = Cap(p.pid)
        run_caps([c], 2.0)
        out[mode] = tone_stats(c.signal())
        c.close()
        p.wait()
    return out


def t_separation():
    a = child(440, 5, "quiet")
    b = child(1000, 5, "quiet")
    a.stdout.readline()
    b.stdout.readline()
    t0 = cpu_s()
    caps = {"include_A": Cap(a.pid), "include_B": Cap(b.pid), "exclude_A": Cap(a.pid, include=False)}
    run_caps(list(caps.values()), 3.0)
    cpu = cpu_s() - t0
    out = {k: tone_stats(c.signal()) for k, c in caps.items()}
    for c in caps.values():
        c.close()
    a.wait()
    b.wait()
    out["cpu_s_for_3_clients_3s_wall"] = round(cpu, 3)
    return out


def t_endpoints():
    en = enumerator()
    devs = en.EnumAudioEndpoints(0, 1)
    n = devs.GetCount()
    out = {"active_render_endpoints": n}
    for i in range(n):
        p = child(440, 3, "quiet", i)
        line = p.stdout.readline()
        if not line:
            out[f"endpoint_{i}"] = "child failed"
            p.wait()
            continue
        c = Cap(p.pid)
        run_caps([c], 1.5)
        out[f"endpoint_{i}"] = tone_stats(c.signal())
        c.close()
        p.wait()
    return out


def t_late_start():
    p = child(440, 3, "quiet", -1, delay=1.5)
    time.sleep(0.3)
    # the python -c wrapper is the parent; its child renders: include tree covers it
    c = Cap(p.pid)
    run_caps([c], 4.0)
    x = c.signal()
    c.close()
    p.wait()
    first = x[: RATE]
    return {"total": tone_stats(x), "first_second_rms": round(float(np.sqrt(np.mean(first**2))) if len(first) else 0, 5)}


def t_exit():
    p = child(440, 1.0, "quiet")
    p.stdout.readline()
    c = Cap(p.pid)
    err = None
    try:
        run_caps([c], 3.0)
    except Exception as exc:  # noqa: BLE001
        err = repr(exc)[:120]
    return {"after_exit_error": err, "samples": int(len(c.signal())), **tone_stats(c.signal())}


if __name__ == "__main__":
    tests = sys.argv[1:] or ["format", "mute", "sep", "endpoints", "late", "exit"]
    fns = {"format": t_format, "mute": t_mute_modes, "sep": t_separation, "endpoints": t_endpoints,
           "late": t_late_start, "exit": t_exit}
    for t in tests:
        try:
            results[t] = fns[t]()
        except Exception as exc:  # noqa: BLE001
            results[t] = {"error": repr(exc)[:300]}
        print(t, json.dumps(results[t]), flush=True)
