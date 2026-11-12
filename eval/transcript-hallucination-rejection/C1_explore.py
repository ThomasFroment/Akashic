"""Mining-split exploration for C1 (dev only)."""
import sys, collections
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import numpy as np
import harness, prod

data = harness.load_cache()
mining = [(cc, lab) for cc, lab in data if lab["split"] == "mining"]
print("mining", len(mining), collections.Counter(l["role"] for _, l in mining))
print("keys", sorted(mining[0][0].keys()))
# windows per segment
nwin = collections.Counter(len({p["seek"] for p in cc["parts"]}) for cc, _ in mining)
print("windows per seg", nwin)
dur = [cc["duration_s"] for cc, _ in mining]
print("dur max", max(dur), "n>=29", sum(d >= 29 for d in dur))
# nonspeech texts
cnt = collections.Counter()
for cc, lab in mining:
    if lab["role"] == "nonspeech":
        cnt[prod.normalize(cc["text"])] += 1
for t, n in cnt.most_common(40):
    print(n, repr(t[:100]))
print("distinct ns texts", len(cnt))
# per part text counts across nonspeech
pc = collections.Counter()
for cc, lab in mining:
    if lab["role"] == "nonspeech":
        for p in cc["parts"]:
            pc[prod.normalize(p["text"])] += 1
print("--- parts")
for t, n in pc.most_common(30):
    print(n, repr(t[:100]))
