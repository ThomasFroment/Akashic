"""A/B bench: min-of-N after-fetch ns/row for cand variants on the harness scenarios."""
import sys, time, gc, importlib.util, numpy as np
sys.path.insert(0, ".")
import harness, build_db, ref_today
size = sys.argv[1]; paths = sys.argv[2:]
mods = []
for p in paths:
    s = importlib.util.spec_from_file_location(p.replace("/", "_").replace(".", "_"), p); m = importlib.util.module_from_spec(s); sys.modules[s.name] = m; s.loader.exec_module(m); mods.append((p, m))
meta = build_db.ensure(size); conn = build_db.connect(build_db.db_path(size)); harness.conn_holder[0] = conn; model = meta["model"]
for sc in harness.scenarios(conn, meta):
    fsql, fp, _, _ = harness.scan_sqls(model, sc)
    def fetch():
        cur = conn.execute(fsql, fp); n = 0
        while r := cur.fetchmany(4000): n += len(r)
        return n
    rows = fetch()
    fns = {"fetch": fetch, **{p: (lambda m=m: harness.call(m, model, sc)) for p, m in mods}}
    t = {k: [] for k in fns}
    for r in range(int(sys.argv[0:1] and 11)):
        for k, f in fns.items():
            gc.collect(); a = time.perf_counter(); f(); t[k].append(time.perf_counter() - a)
    fm = min(t["fetch"])
    print(f"{sc['name']:36s} fetch={fm/rows*1e9:6.0f} " + " ".join(f"{p}: min={(min(v)-fm)/rows*1e9:5.0f} med={(np.median(v)-np.median(t['fetch']))/rows*1e9:5.0f}" for p, v in t.items() if p != "fetch"))
