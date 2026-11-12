"""Gate (a): closed-form full-span embedding (frame map -> numpy TSTP + Gemm) vs the app's embed()
on 200 random turns. Pass iff min cosine >= 0.99999. -> out\R2span_gates\gate_a.json"""
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from C2_cache import dec  # noqa: E402
from C2_lib import SR, segment_turns  # noqa: E402
from common import FIXTURE, OUT, SharedModels  # noqa: E402
from R2span_lib import FrameEmbedder  # noqa: E402

index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
blob = (FIXTURE / "segments.bin").read_bytes()
models = SharedModels()
fe = FrameEmbedder()
rng = np.random.default_rng(42)
segs = [e for e in index["segments"] if e["kind"] != "drift"]
rows = []
for i in rng.permutation(len(segs)):
    e = segs[i]
    x = dec(blob, e)
    body = x[e["lead_ms"] * SR // 1000 :]
    turns, _ = segment_turns(models, body)
    ts = [t for t in turns if t.samples is not None]
    if not ts:
        continue
    t = ts[int(rng.integers(len(ts)))]
    app = models.embed(t.samples)
    e_out, st = fe.forward(t.samples)
    n, s1, s2 = st.total()
    cf = fe.head.embed(np.array([n]), s1[None], s2[None])[0]
    rows.append((float(app @ cf), float(app @ e_out), t.speech_ms, st.T))
    if len(rows) == 200:
        break
c = np.array([r[0] for r in rows])
o = np.array([r[1] for r in rows])
res = {"n": len(rows), "min_cos_closed_vs_app": float(c.min()), "mean": float(c.mean()),
       "min_cos_derived_output_vs_app": float(o.min()), "min_steps": int(min(r[3] for r in rows)),
       "min_speech_ms": int(min(r[2] for r in rows)), "pass": bool(c.min() >= 0.99999)}
(OUT / "R2span_gates").mkdir(parents=True, exist_ok=True)
(OUT / "R2span_gates" / "gate_a.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
print(res)
