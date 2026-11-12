"""Repeat the harness write phase to separate noise from structural tail cost."""
import sys, pathlib, time, hmac, hashlib, json
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import harness, fixtures
mod = harness.load(pathlib.Path(__file__).with_name("cand.py"))
work = pathlib.Path(__file__).with_name("_work_probe"); work.mkdir(exist_ok=True)
runs = []
for k in range(3):
    runs.append(harness.phase_write(mod, work))
    print({a: round(b, 3) for a, b in runs[-1].items()}, flush=True)
# HMAC cost alone over the stream's texts
stream = fixtures.write_stream(10_000)
key = harness.Ctx.subkey("frame-text-key")
t = []
for f in stream:
    s = time.perf_counter_ns(); hmac.new(key, str(f["text"]).encode("utf-8", "surrogatepass"), hashlib.sha256).digest(); t.append(time.perf_counter_ns() - s)
t = np.array(t) / 1e3
print(f"hmac_us p50={np.percentile(t,50):.1f} p99={np.percentile(t,99):.1f} max={t.max():.1f}")
add = [r["write_add_p99_ms"] for r in runs]; add50 = [r["write_add_p50_ms"] for r in runs]
print("add_p99 runs", [round(a, 3) for a in add], "add_p50 runs", [round(a, 3) for a in add50])
json.dump({"runs": runs, "hmac_us_p50": float(np.percentile(t,50)), "hmac_us_p99": float(np.percentile(t,99))}, open(pathlib.Path(__file__).with_name("write_probe.json"), "w"), indent=1)
