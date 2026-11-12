"""Peak VRAM of the two CTC heads (fp16) on a 30 s input, and per-pass time (GPU may be shared)."""

import importlib.util
import time
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("c3", HERE / "C3_ctc.py")
c3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c3)

x = (np.random.default_rng(0).standard_normal(16000 * 30) * 0.05).astype(np.float32)
torch.cuda.reset_peak_memory_stats()
for lang in ("fr", "en"):
    c3._load(lang)
weights = torch.cuda.memory_allocated() / 2**20
for lang in ("fr", "en"):
    c3.log_probs(x, lang)
torch.cuda.synchronize()
ts = []
for _ in range(5):
    t0 = time.perf_counter()
    for lang in ("fr", "en"):
        lp = c3.log_probs(x, lang)
    torch.cuda.synchronize()
    ts.append((time.perf_counter() - t0) * 1000)
toks = [7 + (i % 26) for i in range(400)]
t0 = time.perf_counter()
c3.viterbi(lp, toks)
vit = (time.perf_counter() - t0) * 1000
print(f"weights {weights:.0f} MiB, peak allocated {torch.cuda.max_memory_allocated() / 2**20:.0f} MiB, "
      f"peak reserved {torch.cuda.max_memory_reserved() / 2**20:.0f} MiB; both heads on 30 s: "
      f"median {np.median(ts):.0f} ms; Viterbi T={lp.shape[0]} S={2 * len(toks) + 1}: {vit:.0f} ms")
