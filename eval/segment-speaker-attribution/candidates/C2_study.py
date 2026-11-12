"""Dev study: segmentation-side features that could reveal a merged/overlapped second voice."""
import json, pickle, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
from C2_lib import PER_FRAME, SR, WINDOW, window_starts
from C2_cache import dec
from common import FIXTURE, OUT, SharedModels
DEST = OUT / "C2_calib"
index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
blob = (FIXTURE / "segments.bin").read_bytes()
lp = DEST / "logits.pkl"
if lp.exists():
    L = pickle.loads(lp.read_bytes())
else:
    models = SharedModels(); L = {}
    for e in index["segments"]:
        if e["kind"] == "drift": continue
        x = dec(blob, e); body = x[e["lead_ms"] * SR // 1000:]
        ws = []
        for st in window_starts(len(body)):
            ch = body[st:st + WINDOW]; pad = np.zeros(WINDOW, np.float32); pad[:len(ch)] = ch
            ws.append((len(ch), models.segment_logits(pad).astype(np.float16)))
        L[e["id"]] = ws
    lp.write_bytes(pickle.dumps(L))
PS = ((), (0,), (1,), (2,), (0, 1), (0, 2), (1, 2))
M = np.zeros((7, 3))
for c, mem in enumerate(PS):
    for j in mem: M[c, j] = 1
fms = PER_FRAME * 1000 / SR
def feats(ws):
    f = dict(solo2=0.0, act2=0.0, ovl=0.0, soft2=0.0, softovl=0.0, maxp_ovl=0.0)
    for valid, lg in ws:
        lg = lg.astype(np.float32); real = min(len(lg), int(valid / PER_FRAME)); lg = lg[:real]
        best = lg.argmax(1); act = M[best].astype(bool); alone = act.sum(1) == 1
        solo = np.array([(act[:, j] & alone).sum() for j in range(3)]) * fms
        tot = act.sum(0) * fms
        order = np.argsort(-tot)
        f["solo2"] = max(f["solo2"], np.sort(solo)[-2]); f["act2"] = max(f["act2"], tot[order[1]])
        f["ovl"] = max(f["ovl"], (act.sum(1) >= 2).sum() * fms)
        p = np.exp(lg - lg.max(1, keepdims=True)); p /= p.sum(1, keepdims=True)
        pj = p @ M  # (frames, 3) P(active)
        dom = int(np.argmax(pj.sum(0)))
        f["soft2"] = max(f["soft2"], float(np.delete(pj, dom, 1).sum(0).max() * fms))
        f["softovl"] = max(f["softovl"], float(p[:, 4:].sum() * fms))
        # longest 0.3 s run of high overlap posterior
        k = max(1, int(300 / fms)); po = np.convolve(p[:, 4:].sum(1) + np.delete(pj, dom, 1).max(1) * 0, np.ones(k) / k, "valid")
        f["maxp_ovl"] = max(f["maxp_ovl"], float(po.max()) if len(po) else 0)
    return f
groups = {"solo": [], "mixed_backchannel": [], "mixed_reply": [], "mixed_alt": [], "sw_early_first": [], "similar_mixed": [], "cont_second": []}
for e in index["segments"]:
    if e["id"] not in L: continue
    F = feats(L[e["id"]])
    if e["kind"] == "solo" or (e["kind"] == "cont_first" and e.get("cont_mode") != "switch_early") or e["kind"] == "impostor": g = "solo"
    elif e["kind"] == "cont_first": g = "sw_early_first"
    elif e["kind"] == "cont_second": g = "cont_second"
    elif "mixed_backchannel" in e["subsets"]: g = "mixed_backchannel"
    elif "mixed_reply" in e["subsets"]: g = "mixed_reply"
    elif "mixed_alt" in e["subsets"]: g = "mixed_alt"
    else: g = "similar_mixed"
    if g != "solo" and e["single"] is not None: g = "solo"
    groups[g].append(F)
groups = {g: r for g, r in groups.items() if r}
for key in ["solo2", "act2", "ovl", "soft2", "softovl", "maxp_ovl"]:
    print(key)
    for g, rows in groups.items():
        v = np.array([r[key] for r in rows])
        print(f"  {g:18s} n={len(v):4d} q10/50/90 {np.percentile(v,10):7.1f} {np.percentile(v,50):7.1f} {np.percentile(v,90):7.1f}")
    s = np.array([r[key] for r in groups["solo"]])
    for th in np.percentile(s, [90, 95, 98, 99]):
        det = {g: float(np.mean([r[key] >= th for r in rows])) for g, rows in groups.items() if g != "solo"}
        print(f"   th={th:7.1f} solo_fp={np.mean(s >= th):.3f} det={ {k: round(v,2) for k,v in det.items()} }")
