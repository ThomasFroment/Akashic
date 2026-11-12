"""Feature distributions on the mining split (dev only)."""
import sys, collections
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import numpy as np
import harness, prod
norm = prod.normalize
sil = np.load(HERE / "C1" / "silero_fresh.npz")
data = harness.load_cache()
mining = [(cc, lab) for cc, lab in data if lab["split"] == "mining"]

def ngrams(ws, n):
    return {tuple(ws[i:i+n]) for i in range(len(ws)-n+1)}

def echo(text, prompt):
    W = norm(text).split(); P = norm(prompt or "").split()
    if len(W) < 3 or not P: return 0, 0.0
    # longest common run
    best = 0
    pset = {}
    for n in range(1, min(len(W), 12)+1):
        g = ngrams(P, n)
        if any(tuple(W[i:i+n]) in g for i in range(len(W)-n+1)): best = n
        else: break
    g3 = ngrams(P, 3); cov = np.zeros(len(W), bool)
    for i in range(len(W)-2):
        if tuple(W[i:i+3]) in g3: cov[i:i+3] = True
    return best, cov.mean()

rows = []
for cc, lab in mining:
    p = cc["parts"]
    pns = max((x["no_speech_prob"] for x in p), default=0)
    alp = min((x["avg_logprob"] for x in p), default=0)
    cr = max((x["compression_ratio"] for x in p), default=0)
    probs = sil[cc["id"]]
    sp_s = (probs >= 0.5).sum() * 0.032
    chars = len(norm(cc["text"]).replace(" ", ""))
    run, cov = echo(cc["text"], cc["prompt"])
    role = lab["role"]; sub = lab["subset"].split("/")[0]
    rows.append(dict(role=role, sub=sub, pns=pns, alp=alp, cr=cr, sp=sp_s, vsm=cc["vad_speech_ms"]/1000, chars=chars,
                     rate=chars/max(sp_s, 0.1), rate2=chars/max(cc["vad_speech_ms"]/1000,0.1), ratio=float((probs>=0.5).mean()) if len(probs) else 0,
                     run=run, cov=cov, conf=cc["confidence"] or 0, text=cc["text"], today=cc["today"], nw=len(norm(cc["text"]).split())))

def q(vals): 
    vals = np.array(vals); 
    return " ".join(f"{np.quantile(vals, x):.3f}" for x in (0, .01, .05, .25, .5, .75, .95, .99, 1)) if len(vals) else "-"
for f in ("pns", "alp", "cr", "rate", "rate2", "ratio", "run", "cov", "conf"):
    print(f)
    for grp in ("speech", "nonspeech", "speech_tail"):
        print(f"  {grp:11s}", q([r[f] for r in rows if r["role"] == grp and r["nw"] > 0]))
    print("  short     ", q([r[f] for r in rows if r["role"] == "speech" and r["sub"] == "short" and r["nw"] > 0]))
print("speech echo run>=4:", [(r['run'], round(r['cov'],2), r['text'][:60]) for r in rows if r['role']!='nonspeech' and (r['run']>=4 or r['cov']>=0.6)])
print("speech pns>=0.6:", [(round(r['pns'],2), round(r['ratio'],2), r['sub'], r['text'][:40]) for r in rows if r['role']!='nonspeech' and r['pns']>=0.6])
print("speech rate>25:", [(round(r['rate'],1), round(r['sp'],2), r['sub'], r['text'][:40]) for r in rows if r['role']!='nonspeech' and r['rate']>25])
print("speech ratio<0.25:", [(round(r['ratio'],2), r['sub'], r['text'][:40]) for r in rows if r['role']!='nonspeech' and r['ratio']<0.25])
