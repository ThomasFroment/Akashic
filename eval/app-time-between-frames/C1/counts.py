"""Row counts per run (numbers only)."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
import core  # noqa
import score
RUNS = ["20261118-smoke-s11-target-2220", "20261118-smoke-s11-target-2241", "20261119-standard-s21-target-0315",
        "20261119-standard-s21-target-1818", "20261119-day-s31-target-0547"]
for rid in RUNS:
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _ = score.open_store(rid)
    q = lambda s, *a: conn.execute(s, a).fetchone()[0]
    t = time.perf_counter()
    rows = conn.execute("SELECT monitor_id, ts_ms FROM frames WHERE ts_ms >= ? AND ts_ms < ? ORDER BY monitor_id, ts_ms", (tr.t0 - 3600_000, tr.t1 + 3600_000)).fetchall()
    dt = (time.perf_counter() - t) * 1000
    print(rid[-9:], "h=%.2f" % ((tr.t1 - tr.t0) / 3.6e6), "frames", q("SELECT COUNT(*) FROM frames"), "fetch_ms=%.1f" % dt,
          "sessions", q("SELECT COUNT(*) FROM app_sessions"), "mstates", q("SELECT COUNT(*) FROM monitor_states"),
          "states", conn.execute("SELECT state, COUNT(*) FROM monitor_states GROUP BY state").fetchall(),
          "gaps", conn.execute("SELECT kind, reason, COUNT(*) FROM capture_gaps GROUP BY kind, reason").fetchall(),
          "games", q("SELECT COUNT(*) FROM game_sessions"), "nofocus_frames", q("SELECT COUNT(*) FROM frames f WHERE NOT EXISTS (SELECT 1 FROM frame_windows w WHERE w.frame_id=f.id AND w.focused=1)"))
    conn.close()
