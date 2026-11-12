"""Honest after-fetch cost: the harness floor keeps chunk i alive while fetching chunk i+1; the
candidate frees it first, which makes its own fetchmany cheaper. This times both floors, the
candidate with the bit-shift conversion (fix round) and with the base copyto conversion, and
today, interleaved, median of N, ns per scanned row.

usage: python floor_check.py [size] [runs]
"""

import gc
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build_db  # noqa: E402
import harness  # noqa: E402
import ref_today  # noqa: E402

spec = importlib.util.spec_from_file_location("c3cand", HERE / "cand.py")
cand = importlib.util.module_from_spec(spec)
sys.modules["c3cand"] = cand
spec.loader.exec_module(cand)

size = sys.argv[1] if len(sys.argv) > 1 else "100k"
runs = int(sys.argv[2]) if len(sys.argv) > 2 else 7
meta = build_db.ensure(size)
conn = build_db.connect(build_db.db_path(size))
harness.conn_holder[0] = conn
model = meta["model"]
out = {}
for sc in harness.scenarios(conn, meta):
    fsql, fp, _, _ = harness.scan_sqls(model, sc)

    def floor_walrus():
        n = 0
        cur = conn.execute(fsql, fp)
        while rows := cur.fetchmany(4000):
            n += len(rows)
        return n

    def floor_free_first():
        n = 0
        cur = conn.execute(fsql, fp)
        while True:
            rows = cur.fetchmany(4000)
            if not rows:
                return n
            n += len(rows)
            del rows

    def c3(shift):
        def f():
            cand.USE_BIT_SHIFT = shift
            harness.call(cand, model, sc)
        return f

    fns = {"floor_walrus": floor_walrus, "floor_free_first": floor_free_first,
           "c3_shift": c3(True), "c3_copyto": c3(False),
           "today": lambda: harness.call(ref_today, model, sc)}  # fmt: skip
    nrows = floor_walrus()
    t = {k: [] for k in fns}
    names = list(fns)
    for r in range(runs + 1):
        for name in names[r % len(names):] + names[: r % len(names)]:
            gc.collect()
            t0 = time.perf_counter()
            fns[name]()
            if r:
                t[name].append(time.perf_counter() - t0)
    med = {k: statistics.median(v) / nrows * 1e9 for k, v in t.items()}
    row = {"rows": nrows, **{k: round(v) for k, v in med.items()}}
    for who in ("c3_shift", "c3_copyto", "today"):
        row[who + "_after_walrus"] = round(med[who] - med["floor_walrus"])
        row[who + "_after_free_first"] = round(med[who] - med["floor_free_first"])
    out[sc["name"]] = row
    print(sc["name"], json.dumps(row), flush=True)
cand.USE_BIT_SHIFT = True
(HERE / f"floor_check_{size}.json").write_text(json.dumps(out, indent=1), "utf-8")
