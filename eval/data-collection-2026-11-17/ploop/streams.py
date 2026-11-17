"""Live, passive: Silero speech windows per separated stream vs the mixed endpoint loopback.
Audio stays in memory; only aggregate numbers are printed. usage: streams.py SECONDS"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import byref

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from passive import name_of, sessions  # noqa: E402
from spike import Cap  # noqa: E402
from wasapi import IAudioCaptureClient, IAudioClient, enumerator, fmt_float  # noqa: E402

import onnxruntime as ort  # noqa: E402

ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 60
W = 512


class Silero:
    def __init__(self):
        path = os.path.join(sys.prefix, "Lib", "site-packages", "faster_whisper", "assets", "silero_vad_v6.onnx")
        o = ort.SessionOptions(); o.intra_op_num_threads = 1; o.inter_op_num_threads = 1
        self.s = ort.InferenceSession(path, o, providers=["CPUExecutionProvider"])

    def probs(self, x):
        h = np.zeros((1, 1, 128), np.float32); c = np.zeros((1, 1, 128), np.float32); ctx = np.zeros(64, np.float32)
        out = []
        for i in range(0, len(x) - W + 1, W):
            w = x[i:i + W]
            if not w.any():
                out.append(0.0); ctx = w[-64:].copy(); continue
            p, h, c = self.s.run(None, {"input": np.concatenate([ctx, w])[None, :], "h": h, "c": c})
            ctx = w[-64:].copy(); out.append(float(p[0]))
        return np.array(out)


class EndpointCap(Cap):
    def __init__(self):  # default render endpoint, as production records it
        dev = enumerator().GetDefaultAudioEndpoint(0, 0)
        self.client = dev.Activate(IAudioClient._iid_, 23, None).QueryInterface(IAudioClient)
        fmt = fmt_float(16000, 1)
        self.client.Initialize(0, 0x00020000 | 0x80000000 | 0x08000000, 10_000_000, 0, byref(fmt), None)
        self.cap = self.client.GetService(byref(IAudioCaptureClient._iid_)).QueryInterface(IAudioCaptureClient)
        self.ch = 1; self.chunks = []; self.silent_flags = 0
        self.client.Start()


def win_rms(x):
    n = len(x) // W
    return np.sqrt(np.mean(x[: n * W].reshape(n, W) ** 2, axis=1))


if __name__ == "__main__":
    roots = {}
    for _, pid, _ in sessions():
        name, parent = name_of(pid)
        pname, _ = name_of(parent) if parent else ("", 0)
        root = parent if pname == name else pid  # e.g. Discord renders from a child of the main process
        roots[name] = root
    caps = {"mix_endpoint": EndpointCap()}
    for name, pid in roots.items():
        caps[f"only:{name}"] = Cap(pid)
        caps[f"all_but:{name}"] = Cap(pid, include=False)
    t0 = time.monotonic()
    while time.monotonic() - t0 < SECONDS:
        for c in caps.values():
            c.poll()
        time.sleep(0.02)
    sig = {k: c.signal() for k, c in caps.items()}
    for c in caps.values():
        c.close()
    n = min(len(v) for v in sig.values()) // W * W
    sig = {k: v[:n] for k, v in sig.items()}
    vad = Silero()
    probs = {k: vad.probs(v) for k, v in sig.items()}
    rms = {k: win_rms(v) for k, v in sig.items()}
    out = {"seconds": round(n / 16000, 1), "streams": {}}
    for k in sig:
        p = probs[k]
        out["streams"][k] = {"speech_window_share": round(float(np.mean(p >= 0.5)), 3),
                             "audible_window_share": round(float(np.mean(rms[k] > 0.003)), 3),
                             "rms": round(float(np.sqrt(np.mean(sig[k] ** 2))), 4)}
    names = list(roots)
    for name in names:
        speech = probs[f"only:{name}"] >= 0.5
        others = rms[f"all_but:{name}"]
        if speech.any():
            s_pow = rms[f"only:{name}"][speech] ** 2
            i_pow = others[speech] ** 2 + 1e-12
            out[f"when {name} speaks"] = {
                "windows": int(speech.sum()),
                "other_apps_audible_share": round(float(np.mean(others[speech] > 0.003)), 3),
                "median_SIR_dB_in_mix": round(float(np.median(10 * np.log10(s_pow / i_pow))), 1),
                "share_SIR_below_10dB": round(float(np.mean(10 * np.log10(s_pow / i_pow) < 10)), 3),
            }
    mix_speech = probs["mix_endpoint"] >= 0.5
    if mix_speech.any():
        owners = {}
        for name in names:
            owners[name] = round(float(np.mean((probs[f"only:{name}"] >= 0.5)[mix_speech])), 3)
        out["mix speech windows explained by each app's own speech"] = owners
        anyone = np.zeros_like(mix_speech)
        for name in names:
            anyone |= probs[f"only:{name}"] >= 0.5
        out["mix speech windows with no single app speech-like"] = round(float(np.mean(~anyone[mix_speech])), 3)
    print(json.dumps(out, indent=1))
