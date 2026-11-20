"""Eager torch CPU timing of MossFormer2 on an 8 s test mixture (export-venv), for comparison with
onnxruntime. Prints numbers only.

    export-venv/Scripts/python.exe -I -X utf8 time_torch.py
"""

from __future__ import annotations

import statistics
import time

import sys
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export as ex  # noqa: E402


def main() -> None:
    torch.set_grad_enabled(False)
    m = ex.load()
    x = torch.from_numpy(ex.norm(np.load(ex.HERE / "model" / "test_mix.npz")["mix1"][: 8 * 16000]).astype(np.float32))[None]
    ts = []
    for _ in range(4):
        t = time.perf_counter()
        m(x)
        ts.append(time.perf_counter() - t)
    print(f"torch threads {torch.get_num_threads()} rtf_8s {statistics.median(ts[1:]) / 8:.3f}")


if __name__ == "__main__":
    main()
