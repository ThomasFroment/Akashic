"""Replay app-session end rules on finished harness runs and rescore M5.1-M5.3.

In memory, on the run's own disposable store; the store is not changed. Each rule rewrites the
stored app bands, then the unchanged `score.m5` scores them:

- current: a session ends at its last stored frame (`pipeline/sessions.py`).
- hold_next_cap<N>: a session is held until the next session starts, at most N s past its last
  frame (screenpipe's rule is "gap under 300 s goes to the earlier frame's app"), and never into a
  stored no-recording gap (all or screen), nor past the end of recording (the run's end).
- hold_confirmed: no fixed cap; held until the next session starts, at most until the last
  unchanged-check confirmation of the last frame's monitor (`monitor_states.checked_ms` of the
  "shown" run covering the last frame) plus one focused-monitor check interval (5 s); a game's
  session also stops at its `game_sessions` end; same gap and end-of-recording cuts.

    uv run python eval/quality/spikes/session_hold_replay.py <out.json> <run-id> [<run-id> ...]
"""

import json
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import score  # noqa: E402

from akashic.pipeline import sessions  # noqa: E402
from akashic.storage.gaps import overlapping  # noqa: E402

CAPS_S = (5, 15, 60, 120, 300)


def held(bands, gaps, cap_ms, stop_ms):
    out = []
    starts = sorted(b.start_ms for b in bands)
    for b in bands:
        nxt = min((s for s in starts if s > b.end_ms), default=None)
        end = min(b.end_ms + cap_ms, stop_ms)  # never past the end of recording
        if nxt is not None:
            end = min(end, nxt)
        for g0, _g1 in gaps:
            if g0 >= b.end_ms:
                end = min(end, g0)
        out.append(sessions.AppBand(**{**{f: getattr(b, f) for f in b.__dataclass_fields__}, "end_ms": max(b.end_ms, end)}))
    return out


CHECK_MS = 5_000


def confirmed(conn, bands, gaps, stop_ms):
    out = []
    starts = sorted(b.start_ms for b in bands)
    games = conn.execute("SELECT lower(process), start_ms, end_ms FROM game_sessions").fetchall()
    for b in bands:
        row = conn.execute(
            "SELECT f.monitor_id FROM app_sessions s JOIN frames f ON f.id = s.last_frame_id"
            " WHERE s.start_ms = ? AND s.end_ms = ? AND s.process IS ?", (b.start_ms, b.end_ms, b.process)).fetchone()
        end = stop_ms
        if row is not None:
            seen = conn.execute(
                "SELECT checked_ms FROM monitor_states WHERE monitor_id = ? AND state = 'shown'"
                " AND start_ms <= ? ORDER BY start_ms DESC LIMIT 1", (row[0], b.end_ms)).fetchone()
            end = min(end, max(b.end_ms, seen[0] if seen else b.end_ms) + CHECK_MS)
        nxt = min((x for x in starts if x > b.end_ms), default=None)
        if nxt is not None:
            end = min(end, nxt)
        for proc, g0, g1 in games:
            if proc == (b.process or "").lower() and g0 <= b.end_ms <= g1 + CHECK_MS:
                end = min(end, max(b.end_ms, g1))
        for g0, _g1 in gaps:
            if g0 >= b.end_ms:
                end = min(end, g0)
        out.append(sessions.AppBand(**{**{f: getattr(b, f) for f in b.__dataclass_fields__}, "end_ms": max(b.end_ms, end)}))
    return out


def main() -> None:
    original = sessions.app_bands
    result = {}
    for rid in sys.argv[2:]:
        tr = score.Truth(score.RESULTS / rid)
        _, _, conn, _media = score.open_store(rid)
        now_ms = int(time.time() * 1000)
        gaps = sorted((g.start_ms, g.end_ms) for g in overlapping(conn, tr.t0, tr.t1, now_ms, ("all", "screen")))
        rows = {}
        for name, cap in [("current", None)] + [(f"hold_next_cap{c}", c * 1000) for c in CAPS_S]:
            sessions.app_bands = original if cap is None else (lambda c, a, b, p, cap=cap: held(original(c, a, b, p), gaps, cap, tr.t1))
            m = score.m5(tr, conn, now_ms)
            rows[name] = {"M5.1_hole_s": m["M5.1"]["seconds"], "M5.2_overlap_s": m["M5.2"]["seconds"],
                          "M5.3_error": round(m["M5.3"]["total_abs_error_ratio"], 3)}
        sessions.app_bands = lambda c, a, b, p: confirmed(c, original(c, a, b, p), gaps, tr.t1)
        m = score.m5(tr, conn, now_ms)
        rows["hold_confirmed"] = {"M5.1_hole_s": m["M5.1"]["seconds"], "M5.2_overlap_s": m["M5.2"]["seconds"],
                                  "M5.3_error": round(m["M5.3"]["total_abs_error_ratio"], 3)}
        sessions.app_bands = original
        result[rid] = rows
        print(rid)
        for name, r in rows.items():
            print(f"  {name:<18} {r}")
    Path(sys.argv[1]).write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
