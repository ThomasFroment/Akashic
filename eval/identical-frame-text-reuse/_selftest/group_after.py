"""Self-test: today's pipeline plus plaintext SHA-256 text keys and (text,title,url) grouping
after fusion (must show unkeyed_hash > 0 and groups_ok close to total)."""
import hashlib, json
from ref_today import *  # noqa: F403
import ref_today

def migrate(ctx):
    ref_today.migrate(ctx)
    ctx.conn.execute("ALTER TABLE frames ADD COLUMN text_hash TEXT")

def insert_frame(ctx, frame):
    i = ref_today.insert_frame(ctx, frame)
    if frame["text"]:
        with ref_today.transaction(ctx.conn) as c:
            c.execute("UPDATE frames SET text_hash = ? WHERE id = ?", (hashlib.sha256(str(frame["text"]).encode()).hexdigest(), i))
    return i

def query_frames(ctx, q_text, start_ms, end_ms):
    saved = ref_today.FRAME_SLOTS
    ref_today.FRAME_SLOTS = 10_000
    try:
        hits = ref_today.query_frames(ctx, q_text, start_ms, end_ms)
    finally:
        ref_today.FRAME_SLOTS = saved
    ids = [h[0] for h in hits]
    rows = dict(((r[0], (r[1], r[2], r[3])) for r in ctx.conn.execute(
        "SELECT id, text, window_title, url FROM frames WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(ids),))))
    seen, out = set(), []
    for h in hits:
        g = rows[h[0]]
        if g not in seen:
            seen.add(g); out.append(h)
        if len(out) == 5:
            break
    return out
