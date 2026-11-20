"""ONNX check in the project env: onnxruntime CPU output vs eager torch (SI-SDR), separation quality
on the test mixtures (SI-SDR vs the true sources, best permutation), CPU and CUDA timing.

    uv run python eval/overlapping-speech/P2-gated-separation/check_onnx.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

HERE = Path(__file__).resolve().parent
SR = 16_000


def si_sdr(est, ref):
    est, ref = est - est.mean(), ref - ref.mean()
    a = np.dot(est, ref) / (np.dot(ref, ref) + 1e-12)
    t = a * ref
    return 10 * np.log10(np.sum(t ** 2) / (np.sum((est - t) ** 2) + 1e-12))


def norm(x):
    return x * (10 ** (-25 / 20) / (np.sqrt(np.mean(x ** 2)) + 1e-8))


def session(provider: str):
    o = ort.SessionOptions()
    o.log_severity_level = 3
    return ort.InferenceSession(str(HERE / "model" / "mossformer2_ss_16k.onnx"), o, providers=[provider])


def main() -> None:
    data = np.load(HERE / "model" / "test_mix.npz")
    ref = np.load(HERE / "model" / "torch_out.npz")
    out = {"available_providers": ort.get_available_providers()}
    cpu = session("CPUExecutionProvider")
    for k in sorted(f for f in data.files if f.startswith("mix")):
        i = k[3:]
        x = norm(data[k]).astype(np.float32)
        y = cpu.run(None, {"mix": x[None]})[0]
        r = ref[k]
        sep = max((si_sdr(y[0], data[f"a{i}"]) + si_sdr(y[1], data[f"b{i}"])) / 2, (si_sdr(y[1], data[f"a{i}"]) + si_sdr(y[0], data[f"b{i}"])) / 2)
        out[k] = {"len_s": len(x) / SR, "onnx_vs_torch_si_sdr_db": [round(float(si_sdr(y[j], r[j])), 1) for j in range(2)],
                  "max_abs_diff": float(np.abs(y - r).max()), "sep_si_sdr_vs_truth_db": round(float(sep), 1),
                  "mix_si_sdr_vs_truth_db": round(float((si_sdr(data[k], data[f"a{i}"]) + si_sdr(data[k], data[f"b{i}"])) / 2), 1)}
    x = norm(data["mix1"][: 8 * SR]).astype(np.float32)
    ts = []
    for _ in range(10):
        t = time.perf_counter()
        cpu.run(None, {"mix": x[None]})
        ts.append(time.perf_counter() - t)
    out["cpu_rtf_8s_median9"] = float(np.median(ts[1:]) / 8)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
