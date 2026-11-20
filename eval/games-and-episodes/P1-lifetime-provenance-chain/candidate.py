"""P1 = C1 + C2 + C3 (games-and-episodes invent, 2026-11-20), as narrowed by the user and refuter.

C1 provenance verdict, once per process instance: the exclusion is checked first and only decides
what the row may carry (an excluded game keeps game mode and gets a content-free row: no process
name, no title; the name is never logged); then the user's lists; D3D exclusive fullscreen on a
covering window stays a game (DECISIONS #150); a launcher game folder or GameConfigStore entry is a
game; a launcher's own root outside its game folders (steamwebhelper, Big Picture) is not; the
borderless branch alone is denied by UI-toolkit window class (Chrome_WidgetWin_*, Cascadia, WinUI,
ApplicationFrameWindow, CoreWindow, Console) and Microsoft.Windows* / SystemApps / %WINDIR% package
paths; then today's covering rule.

C2 process-lifetime row keyed (pid, creation time): once an instance is judged a game, its row
spans from its creation (or the start of the recording stretch it was seen in) to its exit
(simulated from ctx.alive; on the PC: WaitForSingleObject(h, 0) per check, ExitTime from
GetProcessTimes). active_ms and last_front_ms come from the foreground checks; gaming_now reads
last_front_ms. A Pause (or another stored no-recording gap: stop, crash, lock, sleep) closes the
row at its start and a new one opens at max(creation, resume). Title: Steam appmanifest name ->
version ProductName (only when OriginalFilename is the running exe, so a renamed host binary such
as python.exe is not trusted) -> most frequent window title. App runs drop only the game's own
process visits.

C3 video viewing chains: media rows of the same (app, title, artist), url compared only when both
are set, chain across gaps <= P with no other item of that app between, and across stored
recording gaps when the uncovered part is <= P and the whole gap <= 30 min; MIN_PLAYED_MS on the
summed played_ms; a rewatch guard (new chain when the position restarts or the media had ended,
from optional position columns); a chain is built once tail + P <= horizon.

Game episodes: one per process instance, chaining that instance's rows across bounded (<= 30 min)
stored gaps (user answer 5: episode span = process lifetime). VARIANT_GAME_EPISODE_PER_ROW turns
that off (one episode per row).
"""

# (no __future__ annotations: the harness loads this module without registering it in sys.modules)

import bisect
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import ge

from akashic.capture import gaming
from akashic.pipeline import episodes as production
from akashic.pipeline.episodes import (
    BROWSER_PROCESSES,
    CLOSE_GAP_MS,
    MIN_BROWSING_MS,
    MIN_GAME_MS,
    MIN_PLAYED_MS,
    MIN_WORK_MS,
    MUSIC_APPS,
    MUSIC_HOSTS,
    _add_episode,
    _runs,
    _segments,
    _top_titles,
    _Visit,
    app_label,
    app_matches_source,
    horizon_ms,
)

NAME = "P1: C1 provenance + C2 process-lifetime rows + C3 viewing chains"
CHAIN_P_MS = int(os.environ.get("P1_CHAIN_P_S", "180")) * 1000
GAP_BOUND_MS = 30 * 60_000  # longest stored recording gap a chain or a game episode bridges
REWATCH_HEAD_MS = 30_000  # a row starting this close to the media's start after a later position
END_SLACK_MS = 5_000
PAUSE_SPLITS = os.environ.get("P1_PAUSE_SPLITS") == "1"  # variant: no chain or game episode across a stored Pause
GAME_EPISODE_PER_ROW = os.environ.get("P1_GAME_EPISODE_PER_ROW") == "1" or PAUSE_SPLITS
CLOSE_REASONS = frozenset({"paused", "stopped", "crashed", "stalled", "locked"})
EXCLUDED = frozenset({"qualityvault.exe"})  # the quality runs' excluded process

# Per-check probes besides the foreground read, worst case (first sight of a process): image path,
# window class, launcher-facts lookup. Steady state is a cached verdict plus WaitForSingleObject(h, 0)
# on the open row's handle (1.0-1.2 us measured, spike_handles.json).
SIGNALS = ("exe_path", "class_name", "facts_lookup")

DENY_CLASSES = frozenset({
    "CASCADIA_HOSTING_WINDOW_CLASS", "WinUIDesktopWin32WindowClass", "ApplicationFrameWindow",
    "Windows.UI.Core.CoreWindow", "ConsoleWindowClass",
})
WINDIR = os.environ.get("WINDIR", r"C:\Windows").lower().rstrip("\\") + "\\"

# --- C1 provenance verdict -------------------------------------------------------------------


def denied_package(path: str | None) -> bool:
    p = (path or "").lower().replace("/", "\\")
    if not p:
        return False
    if p.startswith(WINDIR) or "\\systemapps\\" in p:
        return True
    i = p.find("\\windowsapps\\")
    return i >= 0 and p[i + len("\\windowsapps\\"):].startswith("microsoft.windows")


def denied_class(cls: str | None) -> bool:
    return bool(cls) and (cls in DENY_CLASSES or cls.startswith("Chrome_WidgetWin_"))


def verdict(window, monitors, settings: gaming.GameSettings, facts: dict) -> bool:
    """Whether the focused window is a game. ``facts``: launcher facts of its exe (cached per
    process, refreshed at most once a minute) and its window class / image path."""
    proc = window.process
    if not proc or gaming.covers_monitor(window.rect, monitors) is None:
        return False
    if proc in settings.games:
        return True
    if proc in settings.not_games:
        return False
    if window.exclusive_fullscreen:  # DECISIONS #150: D3D exclusive alone marks a game
        return True
    if facts.get("under_launcher_game_dir") or facts.get("in_gameconfigstore"):
        return True
    if facts.get("under_steam_root"):  # launcher root outside its game folders
        return False
    # borderless branch
    if window.has_frame or proc in gaming.DEFAULT_NOT_GAMES:
        return False
    return not (denied_class(facts.get("class")) or denied_package(facts.get("exe_path")))


def _facts(window, ctx) -> dict:
    facts = getattr(ctx, "facts", None)
    if facts:
        return facts
    return SIM_FACTS.get(window.process, {})


def is_game(window, ctx) -> bool:
    # Exclusion first: an excluded process is judged without keeping or logging its name, and
    # keeps game mode when it is a game (writes_name says the row is content-free).
    return verdict(window, ctx.monitors, getattr(ctx, "settings", gaming.GameSettings()), _facts(window, ctx))


def writes_name(window, ctx) -> bool:
    return window.process not in getattr(ctx, "excluded", EXCLUDED)


# Facts of the two harness game exes for the simulated checks: QualityGame from realwin.json,
# Outer Wilds from spike_handles.json (class UnityWndClass, under a Steam game folder and the
# Steam root, not in GameConfigStore). Other windows never cover a monitor in the simulation.
SIM_FACTS = {
    "qualitygame.exe": {"class": "QualityGame", "under_launcher_game_dir": False, "in_gameconfigstore": False,
                        "under_steam_root": False},
    "outerwilds.exe": {"class": "UnityWndClass", "under_launcher_game_dir": True, "in_gameconfigstore": False,
                       "under_steam_root": True},
}

# --- titles: Steam appmanifest -> ProductName -> most frequent window title ----------------------

_TITLE_CACHE: dict[str, str | None] = {}


def _exe_path(exe: str) -> str | None:
    """Where the harness game exe lives on this PC (the capture side reads it from the process)."""
    if exe == "qualitygame.exe":
        p = ge.HERE / "exes" / "QualityGame.exe"
        return str(p) if p.is_file() else None
    import launchers  # harness/launchers.py (read-only registry and manifest reads)

    for d in launchers.steam_game_dirs():
        for p in Path(d).glob("*.exe"):
            if p.name.lower() == exe:
                return str(p)
    return None


def steam_manifest_name(path: str) -> str | None:
    p = path.lower().replace("/", "\\")
    i = p.find("\\steamapps\\common\\")
    if i < 0:
        return None
    lib, installdir = p[:i], p[i + len("\\steamapps\\common\\"):].split("\\")[0]
    for acf in Path(lib, "steamapps").glob("appmanifest_*.acf"):
        text = acf.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'"installdir"\s+"([^"]+)"', text)
        if m and m.group(1).lower() == installdir:
            n = re.search(r'"name"\s+"([^"]+)"', text)
            return n.group(1) if n else None
    return None


def product_name(path: str, exe: str) -> str | None:
    """Version-resource ProductName, trusted only when OriginalFilename is the running exe's name
    (a renamed host binary carries its host's name: QualityGame.exe says "Python")."""
    import ctypes
    from ctypes import byref, wintypes

    ver = ctypes.WinDLL("version")
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    vals = {}
    for lang in ("040904b0", "040904e4", "000004b0"):
        for key in ("ProductName", "OriginalFilename"):
            if key not in vals and ver.VerQueryValueW(buf, f"\\StringFileInfo\\{lang}\\{key}", byref(ptr), byref(n)) and n.value:
                vals[key] = ctypes.wstring_at(ptr, n.value - 1)
    if vals.get("ProductName") and (vals.get("OriginalFilename") or "").lower() == exe:
        return vals["ProductName"]
    return None


def game_title(exe: str, window_titles: Counter) -> str | None:
    if exe not in _TITLE_CACHE:
        path = _exe_path(exe)
        _TITLE_CACHE[exe] = (steam_manifest_name(path) or product_name(path, exe)) if path else None
    return _TITLE_CACHE[exe] or (window_titles.most_common(1)[0][0] if window_titles else None)


# --- C2 process-lifetime rows ----------------------------------------------------------------


@dataclass(slots=True)
class Instance:
    exe: str
    created: int
    exited: int
    game_at: int | None = None  # first check that judged it a game
    active_ms: int = 0
    last_front_ms: int = 0
    front: list[list[int]] = field(default_factory=list)  # in-front spans (for figures)


@dataclass(slots=True)
class Row:
    inst: Instance
    process: str  # '' for an excluded process: content-free row
    title: str | None
    start: int
    end: int
    active_ms: int = 0
    last_front_ms: int = 0


LAST: dict[str, list[Row]] = {}  # run id -> rows of the last game_rows call (post.py reads it)


def recording_stretches(ctx) -> list[tuple[int, int]]:
    gaps = sorted((a, b) for a, b, r in ctx.gaps if r in CLOSE_REASONS)
    out, t = [], ctx.first_frame
    for a, b in gaps:
        if a > t:
            out.append((t, a))
        t = max(t, b)
    if t < ctx.t1:
        out.append((t, ctx.t1))
    return out


def instances(ctx) -> list[Instance]:
    return sorted((Instance(exe, a, b) for exe, spans in ctx.alive.items() for a, b in spans), key=lambda i: i.created)


def lifetime_rows(ctx) -> list[Row]:
    insts = instances(ctx)
    by_exe: dict[str, list[Instance]] = {}
    for i in insts:
        by_exe.setdefault(i.exe, []).append(i)
    settings = ctx.settings
    checks = ctx.checks
    for k, c in enumerate(checks):
        w = c.window
        if w is None or w.process not in by_exe:
            continue
        t = c.ts_ms
        inst = next((i for i in reversed(by_exe[w.process]) if i.created <= t < i.exited), None)
        if inst is None:
            continue
        if inst.game_at is None:
            # provenance verdict cached per instance; the covering rule is re-read until it holds
            if verdict(w, ctx.monitors, settings, SIM_FACTS.get(w.process, {})):
                inst.game_at = t
        dt = min((checks[k + 1].ts_ms if k + 1 < len(checks) else t + ge.CHECK_MS) - t, 1000)
        inst.active_ms += dt
        inst.last_front_ms = t
        if inst.front and t - inst.front[-1][1] <= 1000:
            inst.front[-1][1] = t + dt
        else:
            inst.front.append([t, t + dt])
    titles = ge.titles(ctx)
    stretches = recording_stretches(ctx)
    rows: list[Row] = []
    for inst in insts:
        if inst.game_at is None:
            continue
        name = inst.exe not in EXCLUDED
        title = game_title(inst.exe, Counter({titles[inst.exe]: 1}) if titles.get(inst.exe) else Counter()) if name else None
        for a, b in stretches:
            if b <= inst.game_at and not (a <= inst.game_at < b):
                continue  # before the stretch it was first judged in: nothing was open then
            s, e = max(inst.created, a), min(inst.exited, b)
            if e <= s:
                continue
            fr = [(x, y) for x, y in inst.front if x < e and y > s]
            rows.append(Row(inst, inst.exe if name else "", title, s, e,
                            sum(min(y, e) - max(x, s) for x, y in fr), max((min(y, e) for _x, y in fr), default=0)))
    rows.sort(key=lambda r: r.start)
    return rows


REOPEN_MS = 300_000
ROWS_MODE = os.environ.get("P1_ROWS", "lifetime")  # "fallback": no process handle


def fallback_rows(ctx) -> list[Row]:
    """When OpenProcess / WaitForSingleObject / GetProcessTimes fail: today's focus rule with the
    C1 verdict, and a row of the same (exe, creation) reopened within REOPEN_MS extends the
    previous one (never across a stored no-recording gap)."""
    rows = ge.replay(ctx, is_game=lambda w, c: verdict(w, c.monitors, c.settings, SIM_FACTS.get(w.process, {})))
    by_key: dict[tuple[str, int], Row] = {}
    insts = {(i.exe, i.created): i for i in instances(ctx)}
    gaps = sorted((a, b) for a, b, r in ctx.gaps if r in CLOSE_REASONS)
    titles = ge.titles(ctx)
    out: list[Row] = []
    for p, _t, a, b in rows:
        created = next((x for x, y in ctx.alive.get(p, []) if x <= a < y), a)
        inst = insts.get((p, created)) or Instance(p, created, b)
        prev = by_key.get((p, created))
        if prev is not None and a - prev.end <= REOPEN_MS and not any(prev.end <= x < a for x, _y in gaps):
            prev.end = b
            continue
        name = p not in EXCLUDED
        title = game_title(p, Counter({titles[p]: 1}) if titles.get(p) else Counter()) if name else None
        row = Row(inst, p if name else "", title, a, b)
        by_key[(p, created)] = row
        out.append(row)
    return out


def game_rows(conn, ctx):
    rows = fallback_rows(ctx) if ROWS_MODE == "fallback" else lifetime_rows(ctx)
    LAST[ctx.run_id] = rows
    return [(r.process, r.title, r.start, r.end) for r in rows]


def gaming_now(rows: list[Row], now_ms: int, grace_ms: int = 5000) -> bool:
    """Game mode: an open row whose game was in front within the grace (last_front_ms)."""
    return any(r.start <= now_ms < r.end and now_ms - r.last_front_ms <= grace_ms for r in rows)


# --- episodes --------------------------------------------------------------------------------

TIMES: dict[str, list[float]] = {"media": [], "games": [], "conversations": [], "app_runs": []}


def _gaps(conn) -> list[tuple[int, int]]:
    return sorted((a, b) for a, b in conn.execute(
        "SELECT start_ms, COALESCE(end_ms, start_ms) FROM capture_gaps WHERE kind IN ('all', 'screen')"))


def _pauses(conn) -> list[tuple[int, int]]:
    return sorted((a, b) for a, b in conn.execute(
        "SELECT start_ms, COALESCE(end_ms, start_ms) FROM capture_gaps WHERE kind = 'all' AND reason = 'paused'"))


def _covered(gaps: list[tuple[int, int]], a: int, b: int) -> int:
    return sum(max(0, min(b, y) - max(a, x)) for x, y in gaps if x < b and y > a)


def bridgeable(gaps, a: int, b: int, p_ms: int, pauses=()) -> bool:
    if any(x < b and y > a for x, y in pauses):
        return False
    d = b - a
    if d <= p_ms:
        return True
    return d <= GAP_BOUND_MS and d - _covered(gaps, a, b) <= p_ms


@dataclass(slots=True)
class _Media:
    id: int
    app: str
    title: str
    artist: str | None
    url: str | None
    start: int
    end: int
    played: int
    pos0: int | None = None
    pos1: int | None = None
    dur: int | None = None


def _same_item(a: _Media, b: _Media) -> bool:
    if (a.app, a.title, a.artist or "") != (b.app, b.title, b.artist or ""):
        return False
    return a.url is None or b.url is None or a.url == b.url


def _rewatch(tail: _Media, r: _Media) -> bool:
    """A new viewing: the media had ended, or the position went back to its start."""
    if tail.dur and tail.pos1 is not None and tail.pos1 >= tail.dur - END_SLACK_MS:
        return True
    return r.pos0 is not None and tail.pos1 is not None and r.pos0 <= REWATCH_HEAD_MS < tail.pos1 - REWATCH_HEAD_MS


def media_chains(rows: list[_Media], gaps, p_ms: int, pauses=()) -> list[list[_Media]]:
    chains: list[list[_Media]] = []
    open_by_app: dict[str, list[_Media]] = {}
    for r in sorted(rows, key=lambda r: (r.start, r.id)):
        ch = open_by_app.get(r.app)
        if ch is not None and _same_item(ch[-1], r) and bridgeable(gaps, ch[-1].end, r.start, p_ms, pauses) and not _rewatch(ch[-1], r):
            ch.append(r)
            continue
        ch = [r]
        chains.append(ch)
        open_by_app[r.app] = ch  # another item of this app ends the previous chain
    return chains


def build_media_chains(conn, horizon: int, p_ms: int = CHAIN_P_MS) -> int:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media_sessions)")}
    pos = "position_start_ms, position_end_ms, duration_ms" if {"position_start_ms", "position_end_ms", "duration_ms"} <= cols else "NULL, NULL, NULL"
    rows = [_Media(*r) for r in conn.execute(
        f"SELECT id, app, title, artist, url, start_ms, end_ms, played_ms, {pos} FROM media_sessions m"  # noqa: S608
        " WHERE closed = 1 AND NOT EXISTS (SELECT 1 FROM episode_items i"
        "                 WHERE i.item_kind = 'media_session' AND i.item_id = m.id)"
        " ORDER BY start_ms")]
    gaps = _gaps(conn)
    built = 0
    for ch in media_chains(rows, gaps, p_ms, _pauses(conn) if PAUSE_SPLITS else ()):
        tail = ch[-1]
        if tail.end + p_ms > horizon:
            continue  # may still grow
        if sum(r.played for r in ch) < MIN_PLAYED_MS:
            continue
        head = ch[0]
        heard = []
        for r in ch:
            heard += [s for s in _segments(conn, r.start, r.end)
                      if s.stream == "system" and (not s.sources or app_matches_source(r.app, s.sources))]
        heard = list({s.id: s for s in heard}.values())
        url = next((r.url for r in ch if r.url), None)
        music = (url is not None and any(h in url for h in MUSIC_HOSTS)) or any(m in head.app.casefold() for m in MUSIC_APPS)
        label = f"{head.title}" + (f" - {head.artist}" if head.artist else "") + f" ({head.app})"
        spoken = " ".join(s.transcript for s in heard)
        built += _add_episode(
            conn, kind="music" if music else "video", ref=f"media:{head.id}", start_ms=head.start, end_ms=max(r.end for r in ch),
            title=head.title, app=head.app, url=url, body=label if not spoken else f"{label}\n{spoken}",
            items=[*(("media_session", r.id) for r in ch), *(("video_segment", s.id) for s in heard)],
            transcripts={s.id: s.transcript for s in heard},
        )
    return built


def _instance_of(ctx, process: str, start: int, end: int) -> tuple[str, int]:
    """The (exe, creation) a row belongs to: stands in for a created_ms column."""
    for a, b in ctx.alive.get(process, []) if process else []:
        if a <= start and end <= b:
            return process, a
    return process, start


def build_game_episodes(conn, horizon: int, ctx) -> int:
    rows = conn.execute(
        "SELECT id, process, window_title, start_ms, end_ms FROM game_sessions g"
        " WHERE closed = 1 AND NOT EXISTS (SELECT 1 FROM episode_items i"
        "                 WHERE i.item_kind = 'game_session' AND i.item_id = g.id)"
        " ORDER BY start_ms").fetchall()
    gaps = _gaps(conn)
    groups: list[list[tuple]] = []
    tails: dict[tuple[str, int], list[tuple]] = {}
    for r in rows:
        key = _instance_of(ctx, r[1], r[3], r[4])
        g = tails.get(key)
        if g is not None and not GAME_EPISODE_PER_ROW and r[3] - g[-1][4] <= GAP_BOUND_MS \
                and r[3] - g[-1][4] - _covered(gaps, g[-1][4], r[3]) <= ge.CHECK_MS * 10:
            g.append(r)
            continue
        g = [r]
        groups.append(g)
        tails[key] = g
    built = 0
    for g in groups:
        start, end = g[0][3], max(r[4] for r in g)
        if end > horizon or end - start < MIN_GAME_MS:
            continue
        process = g[0][1]
        title = g[0][2] or (app_label(process) if process else "Game")
        built += _add_episode(
            conn, kind="game", ref=f"game:{g[0][0]}", start_ms=start, end_ms=end, title=title,
            app=process or None, url=None, body=f"{title} ({process})" if process else title,
            items=[("game_session", r[0]) for r in g],
        )
    return built


def build_app_runs(conn, horizon: int) -> int:
    """Production build_app_runs, except that a game row drops only its own process's visits."""
    row = conn.execute("SELECT COALESCE(MAX(item_id), 0) FROM episode_items WHERE item_kind = 'app_session'").fetchone()
    rows = conn.execute(
        "SELECT id, process, window_title, url, start_ms, end_ms FROM app_sessions"
        " WHERE id > ? AND process IS NOT NULL ORDER BY start_ms, id", (row[0],)).fetchall()
    if not rows:
        return 0
    games = conn.execute("SELECT process, start_ms, end_ms FROM game_sessions WHERE start_ms < ? AND end_ms > ?",
                         (rows[-1][5], rows[0][4])).fetchall()
    visits = [_Visit(r[0], r[1], r[2] or "", r[3], r[4], r[5]) for r in rows
              if not any(p == r[1] and g0 < r[5] and g1 > r[4] for p, g0, g1 in games)]
    built = 0
    for run in _runs(visits):
        kind = "browsing" if run[0].process in BROWSER_PROCESSES else "work"
        start_ms, end_ms = run[0].start_ms, max(v.end_ms for v in run)
        if end_ms + CLOSE_GAP_MS > horizon:
            break
        if end_ms - start_ms < (MIN_BROWSING_MS if kind == "browsing" else MIN_WORK_MS):
            continue
        titles = _top_titles(run)
        process = max({v.process for v in run}, key=lambda p: sum(v.end_ms - v.start_ms for v in run if v.process == p))
        url = next((v.url for v in sorted(run, key=lambda v: v.end_ms - v.start_ms, reverse=True) if v.url), None)
        lead = titles[0][0] if titles else app_label(process)
        body = "\n".join(t for t, _ in titles)
        if kind == "work":
            body = f"{app_label(process)}\n{body}" if body else app_label(process)
        built += _add_episode(conn, kind=kind, ref=f"{kind}:{run[0].id}", start_ms=start_ms, end_ms=end_ms, title=lead,
                              app=process, url=url, body=body, items=[("app_session", v.id) for v in run])
    return built


def episodes(conn, ctx):
    now = ctx.now_ms
    horizon = horizon_ms(conn, now)
    closed = now - CLOSE_GAP_MS
    for name, fn in (("media", lambda: build_media_chains(conn, horizon)),
                     ("games", lambda: build_game_episodes(conn, closed, ctx)),
                     ("conversations", lambda: production.build_conversations(conn, horizon)),
                     ("app_runs", lambda: build_app_runs(conn, closed))):
        t = time.perf_counter()
        fn()
        TIMES[name].append((time.perf_counter() - t) * 1000)
    return conn.execute("SELECT id, kind, start_ms, end_ms, title FROM episodes").fetchall()


# --- self-test of the chain rules on synthetic rows (no harness data has positions) -------------


def _selftest() -> None:
    m = lambda i, a, b, **k: _Media(i, "Zen", "T", "C", k.pop("url", None), a, b, b - a, **k)  # noqa: E731
    gaps = [(10_000_000, 10_600_000)]
    assert len(media_chains([m(1, 0, 100_000), m(2, 104_000, 200_000)], [], 180_000)) == 1  # #189 reopen
    assert len(media_chains([m(1, 0, 100_000), m(2, 400_000, 500_000)], [], 180_000)) == 2  # > P apart
    assert len(media_chains([m(1, 0, 10_000_000), m(2, 10_600_000, 11_000_000)], gaps, 180_000)) == 1  # Pause gap
    assert len(media_chains([m(1, 0, 100_000, url="a"), m(2, 104_000, 200_000, url="b")], [], 180_000)) == 2
    assert len(media_chains([m(1, 0, 100_000, url="a"), m(2, 104_000, 200_000)], [], 180_000)) == 1
    other = _Media(9, "Zen", "Other", "C", None, 101_000, 102_000, 1000)
    assert len(media_chains([m(1, 0, 100_000), other, m(2, 104_000, 200_000)], [], 180_000)) == 3
    # rewatch: ended, or restarted from the head
    assert len(media_chains([m(1, 0, 600_000, pos0=0, pos1=598_000, dur=600_000), m(2, 604_000, 700_000, pos0=0, pos1=96_000)], [], 180_000)) == 2
    assert len(media_chains([m(1, 0, 300_000, pos0=0, pos1=300_000, dur=600_000), m(2, 304_000, 400_000, pos0=2_000, pos1=98_000)], [], 180_000)) == 2
    assert len(media_chains([m(1, 0, 300_000, pos0=0, pos1=300_000, dur=600_000), m(2, 304_000, 400_000, pos0=240_000, pos1=336_000)], [], 180_000)) == 1
    assert denied_package(r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminal_1.2_x64__8wekyb3d8bbwe\WindowsTerminal.exe")
    assert not denied_package(r"C:\Program Files\WindowsApps\SomeStudio.SomeGame_1.0_x64\Game.exe")
    print("selftest ok")


if __name__ == "__main__":
    _selftest()
