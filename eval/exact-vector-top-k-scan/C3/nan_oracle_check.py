"""For the NaN-scope queries, does C3 equal the harness's NaN-dropped stable oracle?"""
import sys, importlib.util
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build_db, harness
spec = importlib.util.spec_from_file_location("c3cand", HERE / "cand.py")
cand = importlib.util.module_from_spec(spec); sys.modules["c3cand"] = cand; spec.loader.exec_module(cand)
for size in ("100k",):
    meta = build_db.ensure(size); conn = build_db.connect(build_db.db_path(size)); harness.conn_holder[0] = conn
    qs = harness.make_queries(conn, meta, 300)
    res = []
    for q in qs:
        if harness.nan_eligible(q, meta):
            got = [r.owner_id for r in harness.call(cand, meta["model"], q)]
            res.append((q["i"], got == harness.nan_dropped_oracle(conn, meta["model"], q)))
    print(size, res)
import numpy as np, ref_today
q = qs[71]
c = harness.call(cand, meta["model"], q); t = harness.call(ref_today, meta["model"], q)
o = harness.nan_dropped_oracle(conn, meta["model"], q)
print("cand ", [(r.owner_id, r.score) for r in c[:4]], len(c))
print("today", [(r.owner_id, r.score) for r in t[:4]], len(t))
print("oracle", o[:4], len(o))
print("first diff", next((k, c[k].owner_id, o[k]) for k in range(len(o)) if c[k].owner_id != o[k]))
print("cand 9-13", [(r.owner_id, r.score) for r in c[9:14]])
cur = conn.execute(build_db.EMB_SQL, (q["owner_kind"], meta["model"], q["start"], q["end"]))
pos = {int(r[0]): k for k, r in enumerate(cur)}
ex = harness.Oracle(conn, meta["model"], {**q, "_only_set": None, "_exclude_set": set(q["exclude"])})
for oid in (65050, 13992):
    print(oid, "scanpos", pos[oid], "exact", ex.owner_max(oid), "chunk", pos[oid] // 4000)
