import sys, cProfile, pstats, importlib.util
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build_db, harness
spec = importlib.util.spec_from_file_location("c3cand", HERE / "cand.py")
cand = importlib.util.module_from_spec(spec); sys.modules["c3cand"] = cand; spec.loader.exec_module(cand)
meta = build_db.ensure("100k"); conn = build_db.connect(build_db.db_path("100k")); harness.conn_holder[0] = conn
scs = {s["name"]: s for s in harness.scenarios(conn, meta)}
sc = scs[sys.argv[1]]
harness.call(cand, meta["model"], sc)
pr = cProfile.Profile(); pr.enable()
for _ in range(3): harness.call(cand, meta["model"], sc)
pr.disable(); pstats.Stats(pr).sort_stats("tottime").print_stats(14)
