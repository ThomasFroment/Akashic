"""Join the C1 key log with harness details (dev only). Usage: C1_analyze.py <out_dir> <key_log>"""
import sys, json, hashlib, collections
from pathlib import Path
out, log = Path(sys.argv[1]), Path(sys.argv[2])
rows = json.loads((out / "details.json").read_text(encoding="utf-8"))["rows"]
keys = {}
for l in log.open(encoding="utf-8"):
    r = json.loads(l); keys[r["id"]] = r
def k(r):
    kk = keys.get(hashlib.sha1(r["seg_id"].encode()).hexdigest()[:12])
    return "+".join(kk["keys"][0]) if kk else "(empty)"
print("== false rejections")
for r in rows:
    if r["role"] != "nonspeech" and r["false_reject"]:
        print(f"  {r['subset']:14s} today={r['today']:7s} m_raw={r['matched_raw']} keys={k(r):22s} ref={r['ref_text'][:30]!r} raw={r['raw_text'][:60]!r}")
print("== missed hallucinations")
miss = collections.Counter()
for r in rows:
    if r.get("halluc") and not r["caught"]:
        miss[r["subset"].split("/")[0]] += 1
        print(f"  {r['subset'][:24]:24s} keys={k(r):18s} raw={r['raw_text'][:70]!r}")
print(miss)
print("== key combos, nonspeech halluc (status)")
c = collections.Counter((k(r), r["status"]) for r in rows if r.get("halluc") and r["role"] == "nonspeech")
for x, n in c.most_common(): print(" ", n, x)
print("== key combos, real speech (status)")
c = collections.Counter((k(r), r["status"]) for r in rows if r["role"] != "nonspeech" and k(r) not in ("", "(empty)"))
for x, n in c.most_common(): print(" ", n, x)
sp = [r for r in rows if r["role"] != "nonspeech"]
unc = collections.Counter((r["subset"].split("/")[0], r["status"]) for r in sp)
print(sorted(unc.items()))
