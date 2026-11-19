"""Of M3's hold past an unfocused frame on its monitor: seconds where the truth foreground is another process. Numbers only."""
import sys, bisect
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval" / "quality"))
import score

for rid in sys.argv[1:]:
    _, _, conn, _ = score.open_store(rid)
    tr = score.Truth(score.RESULTS / rid)
    runs = tr.fg_runs()
    ss = conn.execute("SELECT s.start_ms, s.end_ms, lower(s.process), f.monitor_id FROM app_sessions s JOIN frames f ON f.id = s.last_frame_id ORDER BY s.start_ms").fetchall()
    starts = [s[0] for s in ss]
    fr = conn.execute("SELECT f.monitor_id, f.ts_ms, EXISTS (SELECT 1 FROM frame_windows w WHERE w.frame_id = f.id AND w.focused = 1) FROM frames f ORDER BY f.ts_ms").fetchall()
    per = {}
    for m, t, foc in fr:
        per.setdefault(m, []).append((t, foc))
    wrong = 0; wrong_kinds = {}; right = 0
    for s0, s1, p, m in ss:
        i = bisect.bisect_right(starts, s1)
        nxt = starts[i] if i < len(starts) else tr.t1
        lst = per[m]; j = bisect.bisect_right([x[0] for x in lst], s1)
        if j < len(lst) and not lst[j][1] and lst[j][0] < nxt:
            a, b = lst[j][0], nxt
            for r in runs:
                o = max(0, min(b, r["t1"]) - max(a, r["t0"]))
                if not o:
                    continue
                if (r["exe"] or "").lower() == p:
                    right += o
                else:
                    wrong += o; wrong_kinds[r["kind"]] = wrong_kinds.get(r["kind"], 0) + o
    print(rid, "truth_other_s", round(wrong / 1000, 1), "truth_same_s", round(right / 1000, 1), {k: round(v / 1000, 1) for k, v in wrong_kinds.items()})
