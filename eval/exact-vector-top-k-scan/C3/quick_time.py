"""min-of-N timing of cand vs fetch floor for the harness scenarios (100k), ns/row."""
import sys, time, gc, importlib.util
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build_db, harness, ref_today
spec = importlib.util.spec_from_file_location("c3cand", sys.argv[1] if len(sys.argv) > 1 else HERE / "cand.py")
cand = importlib.util.module_from_spec(spec); sys.modules["c3cand"] = cand; spec.loader.exec_module(cand)
size = sys.argv[2] if len(sys.argv) > 2 else "100k"
meta = build_db.ensure(size); conn = build_db.connect(build_db.db_path(size)); harness.conn_holder[0] = conn
model = meta["model"]
for sc in harness.scenarios(conn, meta):
    fsql, fp, _, _ = harness.scan_sqls(model, sc)
    def fetch():
        n = 0; cur = conn.execute(fsql, fp)
        while rows := cur.fetchmany(4000): n += len(rows)
        return n
    rows = fetch()
    t = {"fetch": [], "cand": [], "today": []}
    for r in range(7):
        for k, f in (("fetch", fetch), ("cand", lambda: harness.call(cand, model, sc)), ("today", lambda: harness.call(ref_today, model, sc))):
            gc.collect(); t0 = time.perf_counter(); f(); t[k].append(time.perf_counter() - t0)
    mn = {k: min(v) for k, v in t.items()}
    print(f"{sc['name']:36s} fetch={mn['fetch']/rows*1e9:6.0f} cand_after={(mn['cand']-mn['fetch'])/rows*1e9:6.0f} today_after={(mn['today']-mn['fetch'])/rows*1e9:6.0f}")
