"""Copy-check statistics on the 120 continuation pairs: 32 ms log-envelope correlation (as in
the candidate), envelope correlation on loud frames only, and raw waveform correlation; true
copy (lead vs N's last lead_ms) vs a shifted negative (lead vs N's audio 3 s earlier)."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
import harness as H
from common import FIXTURE
index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8")); blob = (FIXTURE / "segments.bin").read_bytes()
by = {e["id"]: e for e in index["segments"]}
def env(x):
    n = len(x) // 512; return np.log((x[: n * 512].astype(np.float64) ** 2).reshape(n, 512).mean(1) + 1e-10)
def stats(a, b):
    ea, eb = env(a), env(b)
    c1 = float(np.corrcoef(ea, eb)[0, 1])
    m = np.maximum(ea, eb) > np.median(np.concatenate([ea, eb]))
    c2 = float(np.corrcoef(ea[m], eb[m])[0, 1]) if m.sum() > 4 else float("nan")
    c3 = float(np.corrcoef(a, b)[0, 1])
    return c1, c2, c3
pos, neg = [], []
for e in index["segments"]:
    if e["kind"] != "cont_second": continue
    x, x1 = H.decode(blob, by[e["continues"]]), H.decode(blob, e)
    L = e["lead_ms"] * 16
    pos.append(stats(x1[:L], x[-L:])); neg.append(stats(x1[:L], x[-L - 48000 : -48000]))
pos, neg = np.array(pos), np.array(neg)
for i, k in enumerate(["logenv", "logenv_loud", "waveform"]):
    print(f"{k:12s} copy min={np.nanmin(pos[:, i]):.3f} p5={np.nanpercentile(pos[:, i], 5):.3f} n<0.8={int((pos[:, i] < 0.8).sum())} | shifted max={np.nanmax(neg[:, i]):.3f} p95={np.nanpercentile(neg[:, i], 95):.3f}")
