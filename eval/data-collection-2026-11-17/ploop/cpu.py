import json, sys, time, os, ctypes
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from spike import Cap, child
from passive import sessions
ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
pids = sorted({pid for _, pid, _ in sessions()})
a = child(440, 25, "quiet"); a.stdout.readline()
b = child(1000, 25, "quiet"); b.stdout.readline()
out = {}
for n in (1, 2, 4, 6):
    targets = (pids + [a.pid, b.pid])[:n] if n <= len(pids) + 2 else pids + [a.pid, b.pid]
    caps = [Cap(p) for p in targets] + ([Cap(a.pid, include=False), Cap(b.pid, include=False)] if n == 6 else [])
    c0, t0 = time.process_time(), time.perf_counter()
    while time.perf_counter() - t0 < 4.0:
        for c in caps: c.poll()
        time.sleep(0.02)
    wall = time.perf_counter() - t0
    out[f"{len(caps)}_clients"] = {"cpu_pct_one_core": round(100 * (time.process_time() - c0) / wall, 2),
                                   "samples_per_s": [round(len(c.signal()) / wall) for c in caps]}
    for c in caps: c.close()
a.wait(); b.wait()
print(json.dumps(out))
