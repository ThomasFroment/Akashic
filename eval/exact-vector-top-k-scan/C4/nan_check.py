"""Do the candidate's NaN-scope answers equal the NaN-dropped stable oracle (both sizes)?"""
import sys, importlib.util
sys.path.insert(0, ".")
import harness, build_db
s = importlib.util.spec_from_file_location("c4", "C4/cand.py"); m = importlib.util.module_from_spec(s); sys.modules["c4"] = m; s.loader.exec_module(m)
for size in sys.argv[1:]:
    meta = build_db.ensure(size); conn = build_db.connect(build_db.db_path(size)); harness.conn_holder[0] = conn
    qs = [q for q in harness.make_queries(conn, meta, 300) if harness.nan_eligible(q, meta)]
    ok = sum([r.owner_id for r in harness.call(m, meta["model"], q)] == harness.nan_dropped_oracle(conn, meta["model"], q) for q in qs)
    print(size, f"cand == NaN-dropped oracle in {ok}/{len(qs)}")
