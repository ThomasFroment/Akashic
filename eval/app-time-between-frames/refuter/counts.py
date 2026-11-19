"""Row counts per hour and query timings on a run store. Numbers only."""
import sys, time, statistics
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval" / "quality"))
import score

rid = sys.argv[1]
_, _, conn, _ = score.open_store(rid)
tr = score.Truth(score.RESULTS / rid)
hours = (tr.t1 - tr.t0) / 3_600_000
print("run_hours", round(hours, 2))
for t in ("frames", "frame_windows", "app_sessions", "monitor_states", "capture_gaps", "game_sessions"):
    try:
        n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(t, n, "per_h", round(n / hours, 1))
    except Exception as e:
        print(t, "ERR", type(e).__name__)
n = conn.execute("SELECT count(*) FROM frame_windows WHERE focused = 1").fetchone()[0]
print("focused_rows", n, "per_h", round(n / hours, 1))
# frames with no focused row
n = conn.execute("SELECT count(*) FROM frames f WHERE NOT EXISTS (SELECT 1 FROM frame_windows w WHERE w.frame_id = f.id AND w.focused = 1)").fetchone()[0]
print("frames_without_focused_row", n)
ms = conn.execute("SELECT monitor_id, state, count(*), min(start_ms), max(checked_ms) FROM monitor_states GROUP BY monitor_id, state").fetchall()
for r in ms:
    print("monitor_states", r[0], r[1], r[2])

def timeit(fn, k=9):
    ts = []
    for _ in range(k):
        t = time.perf_counter(); fn(); ts.append((time.perf_counter() - t) * 1000)
    return round(statistics.median(ts), 2)

a, b = tr.t0, tr.t1
print("ms_sessions", timeit(lambda: conn.execute("SELECT start_ms, end_ms, process, window_title, last_frame_id FROM app_sessions WHERE start_ms < ? AND end_ms >= ? ORDER BY start_ms", (b, a)).fetchall()))
print("ms_sessions_join_frame_monitor", timeit(lambda: conn.execute("SELECT s.start_ms, s.end_ms, s.process, f.monitor_id FROM app_sessions s JOIN frames f ON f.id = s.last_frame_id WHERE s.start_ms < ? AND s.end_ms >= ? ORDER BY s.start_ms", (b, a)).fetchall()))
print("ms_monitor_states", timeit(lambda: conn.execute("SELECT monitor_id, state, start_ms, checked_ms FROM monitor_states WHERE start_ms < ? ORDER BY start_ms", (b,)).fetchall()))
print("ms_gaps", timeit(lambda: conn.execute("SELECT kind, reason, start_ms, end_ms FROM capture_gaps WHERE start_ms < ? ORDER BY start_ms", (b,)).fetchall()))
print("ms_all_frames_monitor_ts", timeit(lambda: conn.execute("SELECT id, monitor_id, ts_ms FROM frames WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", (a, b)).fetchall()))
print("ms_focused_rows_join", timeit(lambda: conn.execute("SELECT f.ts_ms, w.process, w.hwnd FROM frames f JOIN frame_windows w ON w.frame_id = f.id AND w.focused = 1 WHERE f.ts_ms >= ? AND f.ts_ms < ? ORDER BY f.ts_ms", (a, b)).fetchall()))
print("explain_frames_ts", conn.execute("EXPLAIN QUERY PLAN SELECT id, monitor_id, ts_ms FROM frames WHERE ts_ms >= ? AND ts_ms < ?", (a, b)).fetchall())
