"""Diagnose seam outcomes of a run: per deferred segment, N+1's window-0 channels (seam ms,
solo ms, active span), their best profiles (C2 LLR, enrolment-only store) and the copy-check
correlation.   python candidates/R2seam_diag.py R2seam_C2_calib_t0"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1])); sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("SEAM_BASE", "candidates/C2_calib.py")
import numpy as np
import harness as H
import R2seam_wrap as W
from C2_lib import decode, window_turns, WINDOW
from C2_decide import llr_matrix
from common import FIXTURE, OUT, SharedModels, unit

run = sys.argv[1]
d = json.loads((OUT / run / "details.json").read_text(encoding="utf-8"))
index = json.loads((FIXTURE / "index.json").read_text(encoding="utf-8"))
blob = (FIXTURE / "segments.bin").read_bytes()
by = {e["id"]: e for e in index["segments"]}
nxt = {e["continues"]: e for e in index["segments"] if e["kind"] == "cont_second"}
models = SharedModels()
base = W.base
store = base.make_store(); owners = H.enrol_all(base, store, models, index, blob)
pids = [p for p in store.sum if store.n[p] > 0]
C = np.stack([unit(store.sum[p]) for p in pids]); K = np.stack([base._other(store.fold[p], base._CONSTS) for p in pids])
seam = d["extras"]["seam"]
for r in d["segments"]:
    if not r.get("deferred"): continue
    why = seam[str(r["id"])]
    e, e1 = by[r["id"]], nxt[r["id"]]
    mode = e["cont_mode"]
    if why == "confirmed" and mode != "switch_early": continue
    x, x1 = H.decode(blob, e), H.decode(blob, e1)
    lead = e1["lead_ms"] * 16
    corr = float(np.corrcoef(x1[:lead], x[-lead:])[0, 1])  # waveform (iteration 2)
    half = WINDOW // 2
    chunk = np.concatenate([x[-half:], x1[lead:lead + half]]); pad = np.zeros(WINDOW, np.float32); pad[:len(chunk)] = chunk
    lo, hi = half - lead, half
    lg = models.segment_logits(pad); act = decode(lg); per = WINDOW / 589
    centre = (np.arange(len(act)) + 0.5) * per
    print(f"{r['id']} {mode} {why} prov={r.get('provisional')} single={r['single']} N+1 spk={list(e1['speech_ms_after_lead'])} corr={corr:.2f}")
    for t in window_turns(pad, lg, len(chunk), 0, 0, min_ms=1):
        sm = int((act[:, t.ch] & (centre >= lo) & (centre < hi)).sum() * per / 16)
        line = f"   ch{t.ch} seam={sm} solo={t.speech_ms} span={t.start_ms - half * 1000 // 16000}..{t.end_ms - half * 1000 // 16000} (0 = cut)"
        if t.samples is not None:
            _, L = llr_matrix(unit(models.embed(t.samples))[None], np.array([t.speech_ms], float), C, K)
            o = np.argsort(-L[0])[:2]
            line += " " + ", ".join(f"{owners[pids[j]]}:{L[0, j]:.1f}" for j in o)
        print(line)
