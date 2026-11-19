"""Largest C1 holes on the day run (truth exe of the harness's own apps, numbers otherwise): frames
stored during the hole per monitor, and whether those frames hold a window row of the truth exe
(focused or not).

    uv run python eval/app-time-between-frames/C1/diagnose3.py
"""
import sys, time
from collections import Counter
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "harness"))
import core  # noqa: E402
from core import Ctx, install_sim, merge  # noqa: E402
import score  # noqa: E402

rid = "20261119-day-s31-target-0547"
c1 = core.load(HERE / "c1.py")
now = int(time.time() * 1000)
tr = score.Truth(score.RESULTS / rid)
_, _, conn, _ = score.open_store(rid)
install_sim(conn, [], [])
bands = c1.bands(conn, tr.t0, tr.t1, now, Ctx(conn, tr.t0, tr.t1, now))
cov = merge([(x.start_ms, x.end_ms) for x in bands] + [(g.start_ms, g.end_ms) for g in core.overlapping(conn, tr.t0, tr.t1, now, ("all", "screen"))])
names = score.monitor_names(conn)
holes = []
for r in (r for r in tr.fg_runs() if r["kind"] != "foreign"):
    a = r["t0"]
    for c0, c1_ in cov + [(r["t1"], r["t1"])]:
        if c1_ <= a:
            continue
        h0, h1 = a, min(max(c0, a), r["t1"])
        if h1 > h0:
            holes.append((h1 - h0, h0, h1, r))
        a = max(a, c1_)
        if a >= r["t1"]:
            break
holes.sort(key=lambda x: -x[0])
by_exe = Counter()
for d, h0, h1, r in holes:
    by_exe[(r["exe"] or "?", r["kind"], r["monitor"])] += d
print("hole s by (truth exe, kind, monitor):", sorted(((k, round(v / 1000, 1)) for k, v in by_exe.items()), key=lambda x: -x[1])[:8])
for d, h0, h1, r in holes[:8]:
    fr = conn.execute("SELECT f.monitor_id, COUNT(DISTINCT f.id), SUM(w.focused), SUM(lower(w.process) = lower(?)), SUM(lower(w.process) = lower(?) AND w.focused = 1)"
                      " FROM frames f LEFT JOIN frame_windows w ON w.frame_id = f.id WHERE f.ts_ms >= ? AND f.ts_ms < ? GROUP BY f.monitor_id",
                      (r["exe"], r["exe"], h0, h1)).fetchall()
    gm = conn.execute("SELECT COUNT(*) FROM game_sessions WHERE lower(process) = lower(?) AND start_ms < ? AND end_ms > ?", (r["exe"], h1, h0)).fetchone()[0]
    print(f"hole {d / 1000:.1f} s exe={r['exe']} kind={r['kind']} truth_mon={r['monitor']} game_rows={gm} frames(mon, n, focused_rows, exe_rows, exe_focused):",
          [(names.get(m, m), n, fo, ex, ef) for m, n, fo, ex, ef in fr])
conn.close()
