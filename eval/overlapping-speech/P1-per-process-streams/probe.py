"""Probe (numbers only): store gap reasons and spans, per-run process tracks, truth pause spans."""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
import core  # noqa: E402
import score  # noqa: E402

for rid in core.RUNS:
    run = core.Run(rid)
    print(rid, "t0..t1 h", (run.truth.t1 - run.truth.t0) / 3.6e6)
    cols = [r[1] for r in run.conn.execute("PRAGMA table_info(capture_gaps)")]
    print(" gaps cols", cols)
    rows = run.conn.execute("SELECT * FROM capture_gaps").fetchall()
    c = Counter()
    for r in rows:
        d = dict(zip(cols, r))
        c[(d.get("kind") or d.get("stream"), d.get("reason"))] += ((d["end_ms"] or run.truth.t1) - d["start_ms"]) / 1000
    for k, v in sorted(c.items(), key=lambda x: -x[1]):
        print("  ", k, round(v, 1))
    print(" truth gap kinds", {k: [(a - run.truth.t0, b - a) for a, b in v] for k, v in score.truth_gap_kinds(run.truth).items()})
    tr = run.tracks()
    for k, t in tr.items():
        print("  track", k, t.exe, t.endpoint, len(t.items), "lag", run.lag(k), "gain", round(run.gain(k), 3))
    segs = run.segments
    print(" segs", len(segs), Counter(s.stream for s in segs), "first", segs[0].start_ms - run.truth.t0, "last", segs[-1].end_ms - run.truth.t0)
    print(" sources", Counter(s.sources for s in segs if s.stream == "system").most_common(12))
