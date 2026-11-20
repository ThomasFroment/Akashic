"""Does the ONNX model run on onnxruntime's CUDA provider in the project env, and does it match CPU?

    uv run python eval/overlapping-speech/P2-gated-separation/cuda_check.py
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent


def si_sdr(est, ref):
    a = np.dot(est, ref) / (np.dot(ref, ref) + 1e-12)
    return 10 * np.log10(np.sum((a * ref) ** 2) / (np.sum((est - a * ref) ** 2) + 1e-12))


def main() -> None:
    try:
        ort.preload_dlls()
    except Exception as e:  # noqa: BLE001
        print("preload_dlls failed", type(e).__name__)
    o = ort.SessionOptions()
    o.log_severity_level = 3
    s = ort.InferenceSession(str(HERE / "model" / "mossformer2_ss_16k.onnx"), o, providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
    print("providers", s.get_providers())
    data = np.load(HERE / "model" / "test_mix.npz")
    ref = np.load(HERE / "model" / "torch_out.npz")
    x = data["mix1"]
    x = (x * (10 ** (-25 / 20) / np.sqrt(np.mean(x ** 2)))).astype(np.float32)
    n = len(x) // 8 * 8
    y = s.run(None, {"mix": x[None, :n]})[0]
    print("cuda vs torch si-sdr", [round(float(si_sdr(y[j], ref["mix1"][j, :n])), 1) for j in range(2)])
    ts = []
    for _ in range(5):
        t = time.perf_counter()
        s.run(None, {"mix": x[None, :8 * 16000]})
        ts.append(time.perf_counter() - t)
    print("cuda rtf 8s", round(float(np.median(ts[1:]) / 8), 4))


if __name__ == "__main__":
    main()
