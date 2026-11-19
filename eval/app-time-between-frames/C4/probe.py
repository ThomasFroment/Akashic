"""Counts only (no captured text): what the real stores hold for the presence channels.

    uv run python eval/app-time-between-frames/C4/probe.py
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
import core  # noqa: E402,F401  (puts eval/quality and src on sys.path)
import score  # noqa: E402

RUNS = ["20261118-smoke-s11-target-2220", "20261118-smoke-s11-target-2241", "20261119-standard-s21-target-0315",
        "20261119-standard-s21-target-1818", "20261119-day-s31-target-0547"]

for rid in RUNS:
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _ = score.open_store(rid)
    hours = (tr.t1 - tr.t0) / 3_600_000
    n_frames = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    n_sess = conn.execute("SELECT COUNT(*) FROM app_sessions").fetchone()[0]
    seg = conn.execute("SELECT stream, COUNT(*), SUM(end_ms - start_ms), SUM(sources != '[]') FROM audio_segments GROUP BY stream").fetchall()
    focused = {p.lower() for (p,) in conn.execute("SELECT DISTINCT process FROM frame_windows WHERE focused = 1 AND process IS NOT NULL")}
    srcs = [json.loads(s) for (s,) in conn.execute("SELECT sources FROM audio_segments WHERE stream = 'system'")]
    match = sum(any(x in focused for x in s) for s in srcs)
    n_src_names = len({x for s in srcs for x in s})
    n_src_focused = len({x for s in srcs for x in s} & focused)
    gaps = conn.execute("SELECT kind, reason, COUNT(*) FROM capture_gaps GROUP BY kind, reason").fetchall()
    t = time.perf_counter()
    rows = conn.execute("SELECT monitor_id, ts_ms FROM frames WHERE ts_ms >= ? AND ts_ms < ? ORDER BY monitor_id, ts_ms", (tr.t0, tr.t1 + 60000)).fetchall()
    t_frames = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    arows = conn.execute("SELECT stream, start_ms, end_ms, sources FROM audio_segments WHERE start_ms < ? AND end_ms > ?", (tr.t1, tr.t0)).fetchall()
    t_audio = (time.perf_counter() - t) * 1000
    print(rid[-14:], f"h={hours:.2f} frames={n_frames} sessions={n_sess} segs={[(a, b, round((c or 0) / 1000), d) for a, b, c, d in seg]}"
          f" sys_segs_with_focused_src={match}/{len(srcs)} src_names={n_src_names} of_which_focused={n_src_focused}"
          f" gaps={[(a, b, c) for a, b, c in gaps]} fetch_frames_ms={t_frames:.2f} ({len(rows)}) fetch_audio_ms={t_audio:.2f} ({len(arows)})")
    conn.close()
