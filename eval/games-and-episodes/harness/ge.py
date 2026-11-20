"""Shared loader for the games-and-episodes invent harness.

Reads a finished quality run: its truth (scenario.json, truth/*.jsonl via eval/quality/score.Truth)
and its encrypted store (score.open_store), and builds

- a *shadow* store: a plain in-memory SQLite copy of the tables game rows and episodes are derived
  from (window titles, URLs and captured text are left out of frame_windows; nothing is written to
  disk), so candidates can rewrite game_sessions / episodes without touching the run's store;
- a *context* (``Ctx``) for candidates: simulated per-check capture records (the foreground window
  as WindowInfo, today's is_game verdict, which game processes were alive and whether their window
  covered a monitor), stored no-recording gaps, monitors.

Nothing here prints captured text, titles or URLs: callers print numbers, harness exe names and
harness-generated video titles only.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]  # eval/games-and-episodes/harness -> repository root (parents[3] in the _dev copy)
QUALITY = ROOT / "eval" / "quality"
sys.path.insert(0, str(QUALITY))

import score  # noqa: E402  (eval/quality/score.py)

from akashic.capture import gaming  # noqa: E402
from akashic.capture.ports import Rect, WindowInfo  # noqa: E402

RUNS = (
    "20261119-standard-s21-target-0315",
    "20261119-standard-s21-target-1818",
    "20261119-day-s31-target-0547",
)
GAME_EXES = {"qualitygame.exe": "quality game", "outerwilds.exe": "outer wilds"}
VOICE = {"discord.exe"}
BROWSER = {"chrome.exe", "msedge.exe", "zen.exe", "firefox.exe", "brave.exe"}
CHECK_MS = 100  # simulated check grid: production polls the foreground every 0.1 s (screen.Settings.poll_s)
OBSERVER_POLL_MS = 1000  # the observer logs window rects about once a second
STOP_KINDS = ("all", "screen")

# Tables copied to the shadow store. frame_windows loses title, url, text and capture_path.
SHADOW = {
    "monitors": "*",
    "frames": "*",
    "frame_windows": "id, frame_id, role, z, hwnd, x0, y0, x1, y1, process, NULL, NULL, focused,"
    " painted, NULL, NULL, NULL",
    "monitor_states": "*",
    "app_sessions": "*",
    "game_sessions": "*",
    "media_sessions": "*",
    "media_events": "*",
    "capture_gaps": "*",
    "audio_segments": "*",
    "speaker_turns": "*",
    "speakers": "*",
    "jobs": "*",
    "state": "*",
    "episodes": "*",
    "episode_items": "*",
    "episode_people": "*",
}


def category(exe: str | None, kind: str | None = None) -> str:
    """Coarse class of a foreground process, for split causes."""
    p = (exe or "").lower()
    if not p:
        return "foreign" if kind == "foreign" else "none"
    if p in GAME_EXES:
        return p
    if p in VOICE:
        return "voice chat"
    if p in BROWSER:
        return "browser"
    return "other"


@dataclass(frozen=True, slots=True)
class Check:
    """One simulated foreground check as the capture loop would see it."""

    ts_ms: int
    window: WindowInfo | None  # None: no foreground or a minimized one (IsIconic)
    fg_exe: str  # lower-case exe of the foreground window ('' when unknown), even if minimized
    fg_kind: str  # observer's class: game, real, harness, foreign
    verdict: bool  # today's gaming.is_game_window on ``window``
    alive: frozenset[str]  # game processes running (as the observer saw them)
    covering: frozenset[str]  # game processes whose window covers a monitor (not minimized)


@dataclass(slots=True)
class Ctx:
    run_id: str
    t0: int  # drive start (wall ms)
    t1: int  # drive end
    first_frame: int
    now_ms: int  # a time after everything is closed
    monitors: list[Rect]
    checks: list[Check]
    gaps: list[tuple[int, int, str]]  # stored capture_gaps of kind all/screen: (start, end, reason)
    alive: dict[str, list[tuple[int, int]]]  # game process -> running spans (observer)
    modes: dict[str, list[tuple[int, str]]]  # game process -> (t, borderless|exclusive|windowed|minimized)
    settings: gaming.GameSettings = field(default_factory=gaming.GameSettings)
    stored_games: list[tuple[str, str | None, int, int]] = field(default_factory=list)


@dataclass(slots=True)
class TruthGame:
    process: str
    start: int
    end: int
    name: str  # M11 truth name
    m6_start: int  # start as score.m6 uses it (Outer Wilds clamped to the first frame)


@dataclass(slots=True)
class TruthVideo:
    vid: str
    title: str  # harness-generated
    start: int
    end: int
    stretches: list[tuple[int, int]]  # play..pause/ended/last tick


@dataclass(slots=True)
class Run:
    run_id: str
    tr: score.Truth
    shadow: sqlite3.Connection
    ctx: Ctx
    games: list[TruthGame]
    videos: list[TruthVideo]
    fg: list[tuple[int, str, str, int]]  # (t, exe lower, kind, hwnd)

    @property
    def hours(self) -> float:
        return (self.tr.t1 - self.tr.t0) / 3_600_000


# --- store -----------------------------------------------------------------------------------


def shadow_copy(conn) -> sqlite3.Connection:
    mem = sqlite3.connect(":memory:", isolation_level=None)
    for table, cols in SHADOW.items():
        (sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        mem.execute(sql)
        rows = conn.execute(f"SELECT {cols} FROM {table}").fetchall()  # noqa: S608
        if rows:
            marks = ",".join("?" * len(rows[0]))
            mem.executemany(f"INSERT INTO {table} VALUES ({marks})", rows)  # noqa: S608
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL AND tbl_name IN (%s)"
                               % ",".join("?" * len(SHADOW)), tuple(SHADOW)):
        mem.execute(sql)
    mem.execute("ANALYZE")
    return mem


def fresh(shadow: sqlite3.Connection) -> sqlite3.Connection:
    out = sqlite3.connect(":memory:", isolation_level=None)
    shadow.backup(out)
    return out


# --- truth -----------------------------------------------------------------------------------


def truth_games(tr: score.Truth, first_frame: int) -> list[TruthGame]:
    starts = [e for e in tr.game if e["type"] == "game_start"]
    ends = [e for e in tr.game if e["type"] == "game_end"]
    out = [TruthGame("qualitygame.exe", a["wall_ms"], b["wall_ms"], "quality game", a["wall_ms"])
           for a, b in zip(starts, ends, strict=False)]
    if ow := tr.outer_wilds_span():
        out.append(TruthGame("outerwilds.exe", ow[0], ow[1], "outer wilds", max(ow[0], first_frame)))
    return out


def truth_videos(tr: score.Truth) -> list[TruthVideo]:
    built = {e["video"]: e["title"] for e in tr.pages if e.get("type") == "video_built"}
    evs = defaultdict(list)
    for e in tr.pages:
        if e.get("type") == "media" and e.get("ev") in ("play", "pause", "ended", "tick") and (vid := tr.pages_video(e)):
            evs[vid].append(e)
    out = []
    for vid, es in evs.items():
        es.sort(key=lambda e: e["wall_ms"])
        starts = [e["wall_ms"] for e in es if e["ev"] == "play"]
        if not starts:
            continue
        stretches, playing, last_tick = [], None, None
        for e in es:
            if e["ev"] == "play":
                playing = e["wall_ms"]
            elif e["ev"] == "tick":
                last_tick = e["wall_ms"]
            elif playing is not None:
                stretches.append((playing, e["wall_ms"]))
                playing = None
        if playing is not None and last_tick and last_tick > playing:
            stretches.append((playing, last_tick))
        out.append(TruthVideo(vid, built.get(vid, ""), min(starts), max(e["wall_ms"] for e in es), stretches))
    return out


def foreground(tr: score.Truth) -> list[tuple[int, str, str, int]]:
    """Foreground changes; a window logged without its exe ("foreign") takes the exe logged for
    the same hwnd elsewhere in the run, when there is one."""
    fg = sorted((e for e in tr.os if e["t"] == "fg"), key=lambda e: e["wall_ms"])
    known = {e["hwnd"]: e["exe"].lower() for e in fg if e.get("exe") and e.get("hwnd")}
    return [(e["wall_ms"], (e.get("exe") or known.get(e.get("hwnd"), "")).lower(), e.get("kind") or "", e.get("hwnd") or 0)
            for e in fg]


def game_process_record(tr: score.Truth) -> tuple[dict, dict]:
    """Running spans and window modes of the game processes, as the observer logged them."""
    alive: dict[str, list[tuple[int, int]]] = defaultdict(list)
    modes: dict[str, list[tuple[int, str]]] = defaultdict(list)
    # QualityGame: one process per game_start; a new one may start before the old one's end is logged.
    open_starts: list[int] = []
    for e in sorted(tr.game, key=lambda e: e["wall_ms"]):
        if e["type"] == "game_start":
            open_starts.append(e["wall_ms"])
        elif e["type"] == "game_end" and open_starts:
            alive["qualitygame.exe"].append((open_starts.pop(0), e["wall_ms"]))
        elif e["type"] == "mode_change":
            modes["qualitygame.exe"].append((e["wall_ms"], e["state"]))
    for a in open_starts:
        alive["qualitygame.exe"].append((a, tr.t1))
    if ow := tr.outer_wilds_span():
        alive["outerwilds.exe"].append(ow)
    for m in tr.outer_wilds_modes():
        modes["outerwilds.exe"].append((m["t"], "borderless" if m["mode"] == "borderless" else "minimized"))
    for k in modes:
        modes[k].sort()
    return dict(alive), dict(modes)


def at(series: list[tuple[int, str]], t: int, default: str = "") -> str:
    i = bisect.bisect_right(series, (t, "￿")) - 1
    return series[i][1] if i >= 0 else default


def shown(series: list[tuple[int, str]], t: int) -> bool:
    """Whether a game window in front at t is restored: its observed mode at t is fullscreen or
    becomes fullscreen within one observer poll (the rect log lags). QualityGame's "windowed"
    (an exclusive game that lost fullscreen) comes back when it gets the foreground."""
    now = at(series, t, "borderless")
    if now in ("borderless", "exclusive", "windowed"):
        return True
    i = bisect.bisect_right(series, (t, "￿"))
    return i < len(series) and series[i][0] - t <= OBSERVER_POLL_MS and series[i][1] in ("borderless", "exclusive")


def session_mode(series: list[tuple[int, str]], t: int) -> str:
    """The last fullscreen mode (borderless/exclusive) set at or before t."""
    i = bisect.bisect_right(series, (t, "￿")) - 1
    while i >= 0 and series[i][1] not in ("borderless", "exclusive"):
        i -= 1
    return series[i][1] if i >= 0 else "borderless"


def is_alive(spans: list[tuple[int, int]], t: int) -> bool:
    return any(a <= t < b for a, b in spans)


def monitors_of(tr: score.Truth) -> tuple[list[Rect], Rect]:
    rects, primary = [], None
    for m in tr.machine.get("monitors", []):
        r = Rect(m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])
        rects.append(r)
        if m.get("primary"):
            primary = r
    return rects, primary or rects[0]


def build_checks(tr, fg, alive, modes, gaps, monitors, primary, start, end) -> list[Check]:
    """The foreground window at each check: every foreground change plus a 1 s grid, none inside a
    stored no-recording gap. Game windows cover the primary monitor in borderless/exclusive mode;
    an exclusive game that lost fullscreen ("windowed") or a minimized window is not focusable
    (IsIconic -> None). Harness and real non-game windows never cover a monitor here (validated by
    replaying today's GameMode against the stored rows)."""
    times = range(start, end, CHECK_MS)
    gap_spans = sorted((a, b) for a, b, _ in gaps)
    small = Rect(primary.left + 100, primary.top + 100, primary.left + 1200, primary.top + 900)
    out = []
    fg_t = [t for t, *_ in fg]
    for t in sorted(times):
        if any(a <= t < b for a, b in gap_spans):
            continue
        i = bisect.bisect_right(fg_t, t) - 1
        exe, kind, hwnd = (fg[i][1], fg[i][2], fg[i][3]) if i >= 0 else ("", "", 0)
        alive_now = frozenset(p for p, s in alive.items() if is_alive(s, t))
        covering = set()
        for p in alive_now:
            mode = at(modes.get(p, []), t, "borderless")
            if mode in ("borderless", "exclusive"):
                covering.add(p)
        window = None
        if exe in GAME_EXES and exe in alive_now and shown(modes.get(exe, []), t):
            # The game in front covers the primary monitor in its last fullscreen mode; an exclusive
            # game comes back exclusive.
            mode = session_mode(modes.get(exe, []), t)
            window = WindowInfo(hwnd, exe, "", primary, has_frame=False, exclusive_fullscreen=mode == "exclusive")
            covering.add(exe)
        elif exe in GAME_EXES and exe in alive_now:
            window = None  # minimized in front: IsIconic
        elif exe in GAME_EXES:
            window = WindowInfo(hwnd, exe, "", small)
        elif exe or kind:
            window = WindowInfo(hwnd, exe, "", small)
        verdict = window is not None and gaming.is_game_window(window, monitors, gaming.GameSettings())
        out.append(Check(t, window, exe, kind, verdict, alive_now, frozenset(covering)))
    return out


def load(run_id: str) -> Run:
    tr = score.Truth(score.RESULTS / run_id)
    _paths, _key, conn, _media = score.open_store(run_id)
    try:
        shadow = shadow_copy(conn)
    finally:
        conn.close()
    first_frame = shadow.execute("SELECT min(ts_ms) FROM frames").fetchone()[0] or tr.t0
    last = max(tr.t1, shadow.execute("SELECT max(ts_ms) FROM frames").fetchone()[0] or tr.t1)
    gaps = [(a, b if b is not None else last, r) for a, b, r in shadow.execute(
        "SELECT start_ms, end_ms, reason FROM capture_gaps WHERE kind IN ('all', 'screen') ORDER BY start_ms")]
    gaps = [g for g in gaps if g[1] > first_frame and g[0] < last]
    fg = foreground(tr)
    alive, modes = game_process_record(tr)
    monitors, primary = monitors_of(tr)
    checks = build_checks(tr, fg, alive, modes, gaps, monitors, primary, first_frame, tr.t1)
    stored = [(p, w, a, b) for p, w, a, b in shadow.execute(
        "SELECT process, window_title, start_ms, end_ms FROM game_sessions ORDER BY start_ms")]
    ctx = Ctx(run_id, tr.t0, tr.t1, first_frame, tr.t1 + 86_400_000, monitors, checks, gaps, alive, modes,
              stored_games=stored)
    return Run(run_id, tr, shadow, ctx, truth_games(tr, first_frame), truth_videos(tr), fg)


# --- small helpers ------------------------------------------------------------------------------


def overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def rel_s(run: Run, t: int) -> float:
    return round((t - run.tr.t0) / 1000, 1)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


# --- replay ------------------------------------------------------------------------------------


def titles(ctx: Ctx) -> dict[str, str | None]:
    """The window title today's rows stored for each game process (never printed)."""
    out: dict[str, str | None] = {}
    for p, wt, _a, _b in ctx.stored_games:
        if wt and p not in out:
            out[p] = wt
    return out


def replay(ctx: Ctx, is_game=None, end_grace_s: float | None = None) -> list[tuple[str, str | None, int, int]]:
    """Today's GameMode logic over the simulated checks: a game in front opens a row, another game
    in front switches rows at once, ``end_grace_s`` of no game ends it at the last game tick, a
    stored no-recording gap ends it at its start. ``is_game(window, ctx)`` replaces the verdict."""
    grace = int(1000 * (ctx.settings.end_grace_s if end_grace_s is None else end_grace_s))
    names = titles(ctx)
    gap_starts = sorted(a for a, _b, _r in ctx.gaps)
    rows: list[tuple[str, str | None, int, int]] = []
    cur: str | None = None
    start = last = 0
    prev_t = None
    gi = 0
    for c in ctx.checks:
        while gi < len(gap_starts) and gap_starts[gi] <= c.ts_ms:
            if cur is not None and prev_t is not None and gap_starts[gi] > prev_t:
                rows.append((cur, names.get(cur), start, last))
                cur = None
            gi += 1
        prev_t = c.ts_ms
        verdict = c.verdict if is_game is None else (c.window is not None and bool(is_game(c.window, ctx)))
        if verdict:
            p = c.window.process
            if cur is None:
                cur, start = p, c.ts_ms
            elif p != cur:
                rows.append((cur, names.get(cur), start, last))
                cur, start = p, c.ts_ms
            last = c.ts_ms
        elif cur is not None and c.ts_ms - last >= grace:
            rows.append((cur, names.get(cur), start, last))
            cur = None
    if cur is not None:
        rows.append((cur, names.get(cur), start, last))
    return rows
