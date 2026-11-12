"""Quick after-fetch bench on the 100k DB: python bench.py mod1.py [mod2.py ...]"""
import gc, sys, time, statistics, importlib.util
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE))
import build_db, harness as H, ref_today
size = "100k"
meta = build_db.ensure(size); conn = build_db.connect(build_db.db_path(size)); H.conn_holder[0] = conn
mods = {}
for p in sys.argv[1:]:
    s = importlib.util.spec_from_file_location(Path(p).stem + str(len(mods)), p); m = importlib.util.module_from_spec(s)
    sys.modules[s.name] = m; s.loader.exec_module(m); mods[p] = m
model = meta["model"]
for sc in H.scenarios(conn, meta):
    fetch_sql, fp, _, _ = H.scan_sqls(model, sc)
    def fetch():
        cur = conn.execute(fetch_sql, fp)
        while cur.fetchmany(4000): pass
    fns = {"fetch": fetch, **{k: (lambda m=m: H.call(m, model, sc)) for k, m in mods.items()}}
    rows = sum(1 for _ in conn.execute(fetch_sql, fp))
    t = {k: [] for k in fns}
    for r in range(8):
        for k, f in fns.items():
            gc.collect(); a = time.perf_counter(); f(); t[k].append(time.perf_counter() - a)
    med = {k: statistics.median(v[1:]) for k, v in t.items()}
    print(sc["name"].ljust(36), f"fetch={med['fetch']/rows*1e9:6.0f}", " ".join(f"{Path(k).stem}={(med[k]-med['fetch'])/rows*1e9:5.0f}" for k in mods))
