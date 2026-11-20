"""Separation cost: MossFormer2 ONNX real-time factor on CPU and on CUDA (median of 9 after a warm-up,
per window length), and the device memory the CUDA session takes (nvidia-smi device used memory,
sampled every 200 ms, minus the reading before the session existed: run under gpu.lock). Window
lengths: 3, 5, 8, 12 s of a test mixture (corpus speech). Prints one JSON line.

    uv run python eval/overlapping-speech/P2-gated-separation/bench_sep.py [cpu|cuda|both]
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402

LENS = (3, 5, 8, 12)
REPS = 9


def session(prov):
    o = ort.SessionOptions()
    o.log_severity_level = 3
    return ort.InferenceSession(str(HERE / "model" / "mossformer2_ss_16k.onnx"), o, providers=[(prov, {"cudnn_conv_algo_search": "HEURISTIC", "arena_extend_strategy": "kSameAsRequested"}) if prov.startswith("CUDA") else prov, "CPUExecutionProvider"])


def bench(sess, x):
    out = {}
    for s in LENS:
        xi = x[None, :s * 16000]
        sess.run(None, {"mix": xi})
        ts = []
        for _ in range(REPS):
            t = time.perf_counter()
            sess.run(None, {"mix": xi})
            ts.append(time.perf_counter() - t)
        out[str(s)] = statistics.median(ts) / s
    return out


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "both"
    x = np.load(HERE / "model" / "test_mix.npz")["mix2"]
    x = (x * (10 ** (-25 / 20) / np.sqrt(np.mean(x ** 2)))).astype(np.float32)
    res = {"lens_s": LENS, "reps": REPS}
    if which in ("cpu", "both"):
        res["cpu_rtf"] = bench(session("CPUExecutionProvider"), x)
    if which in ("cuda", "both"):
        ort.preload_dlls()
        deltas = []
        for k in range(3):
            with core.VramPeak() as vp:
                s = session("CUDAExecutionProvider")
                r = bench(s, x) if k == 0 else {str(n): None for n in LENS}
                if k:
                    for n in LENS:
                        s.run(None, {"mix": x[None, :n * 16000]})
                del s
            if k == 0:
                res["cuda_rtf"] = r
            deltas.append(vp.peak_mib)
        res["cuda_vram_delta_mib_runs"] = deltas
        res["cuda_vram_delta_mib"] = statistics.median(deltas)
    print(json.dumps(res))


if __name__ == "__main__":
    main()
