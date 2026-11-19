"""Shared pieces of the app-time harness: the candidate context, simulated capture records, loading.

A candidate is a module with

    bands(conn, start_ms, end_ms, now_ms, ctx) -> list[sessions.AppBand]

returning the app bands for [start_ms, end_ms) (bounds unclipped, like `sessions.app_bands`).
`conn` is the run's store (SQLCipher, or a plain in-memory SQLite for the synthetic timelines;
same schema). `ctx` is a `Ctx`; everything on it is read-only and read from the store at call time,
so its cost is inside the timed call.

Simulated capture records (the brief's budget allows one cheap new record) live in TEMP tables on
the same connection, never in the store file:

- `sim_fg(ts_ms, hwnd, process, state)`: one row per foreground change, as a WinEvent
  EVENT_SYSTEM_FOREGROUND hook would see it. state 'app' carries the process; 'excluded' (a window
  Akashic excludes) and 'unknown' (a window the observer could not name) carry no process or hwnd.
  While capture is stopped (an `all` gap) nothing is written; when the gap ends, a row repeats the
  foreground of that moment, as a capture restart would.
- `sim_idle(start_ms, end_ms)`: stretches with no keyboard or mouse input (GetLastInputInfo),
  start back-dated to the last input, written once a stretch reaches IDLE_RECORD_MIN_MS. Empty on
  the real runs (see run.py's notes): read as "always active".

Reading either through `ctx` marks the candidate as using simulated records.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "eval" / "quality"))
sys.path.insert(0, str(REPO / "src"))

from akashic.pipeline import sessions  # noqa: E402
from akashic.storage.gaps import overlapping  # noqa: E402

AppBand = sessions.AppBand
ORIGINAL_APP_BANDS = sessions.app_bands  # captured before any patch: today's derivation
IDLE_RECORD_MIN_MS = 30_000
EXCLUDED_PROCESSES = {"qualityvault.exe"}  # the harness's Akashic config excludes it (eval/quality/run.py)

SIM_SCHEMA = """
CREATE TEMP TABLE IF NOT EXISTS sim_fg (ts_ms INTEGER NOT NULL, hwnd INTEGER, process TEXT,
    state TEXT NOT NULL CHECK (state IN ('app', 'excluded', 'unknown')));
CREATE INDEX IF NOT EXISTS temp.sim_fg_ts ON sim_fg (ts_ms);
CREATE TEMP TABLE IF NOT EXISTS sim_idle (start_ms INTEGER NOT NULL, end_ms INTEGER);
CREATE INDEX IF NOT EXISTS temp.sim_idle_start ON sim_idle (start_ms);
DELETE FROM temp.sim_fg;
DELETE FROM temp.sim_idle;
"""


def install_sim(conn, fg_rows, idle_rows) -> None:
    for stmt in SIM_SCHEMA.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.executemany("INSERT INTO temp.sim_fg VALUES (?, ?, ?, ?)", fg_rows)
    conn.executemany("INSERT INTO temp.sim_idle VALUES (?, ?)", idle_rows)


class Usage:
    """Which simulated records a candidate read, over the whole harness run."""

    def __init__(self) -> None:
        self.fg = False
        self.idle = False

    def label(self) -> str:
        return "+".join(n for n, u in (("fg", self.fg), ("idle", self.idle)) if u) or "no"


class Ctx:
    def __init__(self, conn, start_ms: int, end_ms: int, now_ms: int, usage: Usage | None = None) -> None:
        self.conn, self.start_ms, self.end_ms, self.now_ms = conn, start_ms, end_ms, now_ms
        self._usage = usage or Usage()
        self._memo: dict = {}

    def stored_bands(self, process: str | None = None) -> list[AppBand]:
        """Today's `sessions.app_bands` for the range (sessions end at their last stored frame)."""
        return ORIGINAL_APP_BANDS(self.conn, self.start_ms, self.end_ms, process)

    def gaps(self, kinds=("all", "screen")) -> list:
        """Stored no-recording gaps of `kinds` overlapping the range (GapSpan: kind, reason, start_ms, end_ms)."""
        key = ("gaps", tuple(kinds))
        if key not in self._memo:
            self._memo[key] = overlapping(self.conn, self.start_ms, self.end_ms, self.now_ms, kinds)
        return self._memo[key]

    @property
    def rec_end_ms(self) -> int:
        """End of recording: the start of a stop or crash gap still running at now, else now."""
        if "rec_end" not in self._memo:
            end = self.now_ms
            for g in overlapping(self.conn, self.start_ms, self.now_ms, self.now_ms, ("all",)):
                if g.reason in ("stopped", "crashed") and g.end_ms >= self.now_ms:
                    end = min(end, g.start_ms)
            self._memo["rec_end"] = end
        return self._memo["rec_end"]

    def sim_fg(self) -> list[tuple[int, int | None, str | None, str]]:
        """SIMULATED foreground changes in the range, plus the last one before it."""
        self._usage.fg = True
        if "fg" not in self._memo:
            self._memo["fg"] = self.conn.execute(
                "SELECT * FROM (SELECT ts_ms, hwnd, process, state FROM temp.sim_fg WHERE ts_ms < ?"
                " ORDER BY ts_ms DESC LIMIT 1)"
                " UNION ALL SELECT ts_ms, hwnd, process, state FROM temp.sim_fg"
                " WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms",
                (self.start_ms, self.start_ms, self.end_ms)).fetchall()
        return self._memo["fg"]

    def sim_idle(self) -> list[tuple[int, int | None]]:
        """SIMULATED no-input stretches overlapping the range; end None = still idle at now."""
        self._usage.idle = True
        if "idle" not in self._memo:
            self._memo["idle"] = self.conn.execute(
                "SELECT start_ms, end_ms FROM temp.sim_idle WHERE start_ms < ?"
                " AND (end_ms IS NULL OR end_ms > ?) ORDER BY start_ms",
                (self.end_ms, self.start_ms)).fetchall()
        return self._memo["idle"]


def load(path: str | Path):
    p = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(f"cand_{p.stem}", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def merge(spans):
    out = []
    for a, b in sorted(spans):
        if b <= a:
            continue
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def overlap_ms(a, b) -> int:
    a, b = merge(a), merge(b)
    i = j = tot = 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if hi > lo:
            tot += hi - lo
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return tot
