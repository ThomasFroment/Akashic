import sys, pathlib, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import harness, fixtures, fake_embed, ref_today
mod = harness.load("C1/cand.py")
work = pathlib.Path("C1/_work_smoke"); work.mkdir(exist_ok=True)
small = fixtures.small()
ctx = harness.open_db(mod, work, "s")
emb = fake_embed.FakeEmbedder(harness.MODEL)
m = harness.populate(mod, ctx, small, emb)
print("sent", emb.texts, "rows", len(harness.vectors(ctx.conn)[0]), "orph", harness.orphans(ctx.conn, m))
for sql in ["EXPLAIN QUERY PLAN SELECT p.text_key FROM (SELECT DISTINCT text_key FROM frames WHERE ts_ms >= 0 AND ts_ms < 9e18 AND text_key IS NOT NULL) k JOIN text_passages p ON p.text_key = k.text_key AND p.model = 'x'"]:
    for r in ctx.conn.execute(sql): print(r)
f = small[5]; fid = [i for i,x in m.frames.items() if x["ts_ms"]==f["ts_ms"]][0]
print(mod.query_frames(ctx, "hello " + f["text"][:60], f["ts_ms"], f["ts_ms"]+1)[:1])
r = mod.query_frames(ctx, "video", fixtures.DAY0_MS, fixtures.DAY0_MS+10**9)
print([(a,b,c["also_seen"]) for a,b,c in r])
harness.close(ctx)
ctx = harness.open_db(ref_today, work, "u"); m = harness.populate(ref_today, ctx, small, fake_embed.FakeEmbedder(harness.MODEL))
mod.upgrade_from_today(ctx); e2 = fake_embed.FakeEmbedder(harness.MODEL); mod.embed_pending(ctx, e2)
print("upgrade sent", e2.texts, "rows", len(harness.vectors(ctx.conn)[0]), "orph", harness.orphans(ctx.conn, m))
harness.close(ctx)
