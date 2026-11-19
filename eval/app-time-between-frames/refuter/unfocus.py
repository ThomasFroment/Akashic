"""How long M3's hold runs past a later frame of the same monitor that has no focused row. Numbers only."""
import sys, bisect
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval" / "quality"))
import score

for rid in sys.argv[1:]:
    _, _, conn, _ = score.open_store(rid)
    tr = score.Truth(score.RESULTS / rid)
    ss = conn.execute("SELECT s.start_ms, s.end_ms, f.monitor_id FROM app_sessions s JOIN frames f ON f.id = s.last_frame_id ORDER BY s.start_ms").fetchall()
    starts = [s[0] for s in ss]
    fr = conn.execute("SELECT f.monitor_id, f.ts_ms, EXISTS (SELECT 1 FROM frame_windows w WHERE w.frame_id = f.id AND w.focused = 1) FROM frames f ORDER BY f.ts_ms").fetchall()
    per = {}
    for m, t, foc in fr:
        per.setdefault(m, []).append((t, foc))
    n = 0; over = 0; big = 0
    for s0, s1, m in ss:
        i = bisect.bisect_right(starts, s1)
        nxt = starts[i] if i < len(starts) else tr.t1
        lst = per[m]; j = bisect.bisect_right([x[0] for x in lst], s1)
        if j < len(lst) and not lst[j][1] and lst[j][0] < nxt:
            n += 1; d = nxt - lst[j][0]; over += d; big = max(big, d)
    print(rid, "sessions", len(ss), "held_past_unfocused_frame", n, "total_s", round(over / 1000, 1), "max_s", round(big / 1000, 1), "truth_s", round((tr.t1 - tr.t0) / 1000))
