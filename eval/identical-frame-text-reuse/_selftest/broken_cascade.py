"""Self-test: today's pipeline without the passage cascade/trigger (must show orphans)."""
from ref_today import *  # noqa: F403
import ref_today

def migrate(ctx):
    sql = ref_today.SCHEMA.read_text("utf-8")
    sql = sql.replace("REFERENCES frames(id) ON DELETE CASCADE", "")
    sql = sql[: sql.index("CREATE TRIGGER frames_passages_au")]
    ref_today.run_migration(ctx.conn, sql, 18)
