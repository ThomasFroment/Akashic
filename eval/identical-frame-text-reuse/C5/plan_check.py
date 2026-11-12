"""Quick smoke test + EXPLAIN QUERY PLAN of C5's hot statements (small fixture)."""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import fake_embed  # noqa: E402
import fixtures  # noqa: E402
import harness  # noqa: E402

cand = harness.load(HERE / "cand.py")
work = HERE / "_plan"
shutil.rmtree(work, ignore_errors=True)
work.mkdir()
ctx = harness.open_db(cand, work, "p")
small = fixtures.small()
emb = fake_embed.FakeEmbedder(harness.MODEL)
harness.populate(cand, ctx, small, emb)
print("sent", emb.texts, "rows", ctx.conn.execute("SELECT count(*) FROM text_passages").fetchone())
c = ctx.conn
for label, sql, params in (
    ("top_keys", "SELECT p.text_key, p.vec FROM text_passages p WHERE p.model = 'm' AND p.text_key IN"
     " (SELECT text_key FROM frames WHERE ts_ms >= 0 AND ts_ms < 1 AND text_key IS NOT NULL)", ()),
    ("runs", cand._RUNS, {"k": b"x", "lo": 0, "hi": 1, "t": "a", "u": "b", "gap": 1}),
    ("groups", "SELECT text_key, window_title, url, max(ts_ms) FROM frames WHERE text_key IN (?, ?)"
     " AND ts_ms >= ? AND ts_ms < ? GROUP BY text_key, window_title, url", (b"a", b"b", 0, 1)),
    ("missing", cand._MISSING_KEYS, {"model": "m", "since": 0, "limit": 32, "min": 12}),
    ("trigger_probe", "SELECT 1 FROM frames WHERE text_key = ? AND ts_ms >= 0", (b"a",)),
):
    print("--", label)
    for row in c.execute("EXPLAIN QUERY PLAN " + sql, params):
        print("  ", row[3])
f = small[5]
print(cand.query_frames(ctx, harness.kw_query(str(f["text"]), __import__("numpy").random.default_rng(0)), f["ts_ms"] - 3_600_000, f["ts_ms"] + 3_600_000))
harness.close(ctx)
shutil.rmtree(work, ignore_errors=True)
