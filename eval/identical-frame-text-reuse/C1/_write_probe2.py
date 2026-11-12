"""Write-path attribution: raw interleaved timings (mean, p90, p99, spikes) for today, C1, and
C1 variants (no text_key index; no guard trigger), plus WAL frames written per insert."""
import sys, pathlib, time, json
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import harness, fixtures, ref_today
here = pathlib.Path(__file__).parent
work = here / "_work_probe2"; work.mkdir(exist_ok=True)
c1 = harness.load(here / "cand.py")
def variant(name, drop):
    m = harness.load(here / "cand.py")
    orig = m.migrate
    def migrate(ctx, orig=orig):
        orig(ctx)
        for s in drop: ctx.conn.execute(s)
    m.migrate = migrate
    return m
mods = {
    "today": ref_today,
    "C1": c1,
    "C1_noindex": variant("noidx", ["DROP INDEX frames_text_key"]),
    "C1_noguard": variant("noguard", ["DROP TRIGGER frames_text_key_guard_bi"]),
}
n = 10_000
stream = fixtures.write_stream(n)
out = {}
for rep in range(2):
    ctxs = {k: harness.open_db(m, work, f"{k}_{rep}") for k, m in mods.items()}
    times = {k: [] for k in mods}
    for at in range(0, n, 500):
        for k, m in mods.items():
            for fr in stream[at:at + 500]:
                t = time.perf_counter_ns(); m.insert_frame(ctxs[k], dict(fr)); times[k].append(time.perf_counter_ns() - t)
    for k in mods:
        a = np.array(times[k]) / 1e6
        pages = int(ctxs[k].conn.execute("PRAGMA page_count").fetchone()[0])
        r = out.setdefault(k, [])
        r.append({"mean": a.mean(), "p50": np.percentile(a, 50), "p90": np.percentile(a, 90), "p99": np.percentile(a, 99), "spikes_gt5ms": int((a > 5).sum()), "db_pages": pages})
        harness.close(ctxs[k])
for k, rs in out.items():
    print(k, " | ".join(" ".join(f"{a}={b:.3f}" if isinstance(b, float) else f"{a}={b}" for a, b in r.items()) for r in rs))
(here / "write_probe2.json").write_text(json.dumps(out, indent=1, default=float))
