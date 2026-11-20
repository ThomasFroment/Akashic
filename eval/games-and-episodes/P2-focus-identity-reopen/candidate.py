"""P2: focus-built game rows reopened by process identity (low-privilege fallback of C2).

No process handle is held. Game rows come from today's foreground game-mode logic with the C1
verdict, but a row is not closed by focus loss:

- identity = (exe, process creation time) of the foreground pid, read once per new (pid, hwnd)
  with OpenProcess(QUERY_LIMITED_INFORMATION) + GetProcessTimes (no SYNCHRONIZE needed);
- front stretches are today's rows keyed by identity instead of exe (a restart under the same exe
  is a new identity and always starts a new row);
- consecutive stretches of one identity merge when the gap <= PULSE_S (G3 pulsetime), across visits
  to other windows and other games (rows of two running games may overlap);
- a stretch shorter than FLICKER_MS never opens a row (it may extend an open row of its identity);
- a stored no-recording gap (Pause, lock) closes every row; nothing is merged across it.

END_MODE "front": a row ends at its last in-front tick (no exit signal).
END_MODE "alive": the last row of an identity before its exit (or before a Pause) ends at the last
1 s liveness poll that saw it, and a game still alive at a resume gets a row from the resume. The
harness has this signal as ``ctx.alive`` (observer's ~1 s process record); in production it would
be IsWindow(game hwnd) once a second on the existing capture loop (0.3 us, no process handle).

Detection (C1): exclusion first (content-free row, game mode kept, name never written), user lists,
launcher game folder / GameConfigStore -> game, D3D exclusive -> game, then on the borderless
branch only: has frame / DEFAULT_NOT_GAMES / launcher root outside its game folder / UI-toolkit
window class -> not a game, else today's covering rule.

Episodes: C3 video chains (same app+title+artist, url compared only when both have one, gap <= 180 s
or a stored recording gap within 30 min, no other item of the app between; MIN_PLAYED_MS on the
chain sum), production games and conversations, app runs that drop only the game's own visits.
"""

from __future__ import annotations

import bisect
import json
import os

import ge

from akashic.capture import gaming
from akashic.pipeline import episodes as production

PULSE_S = int(os.environ.get("P2_PULSE_S", "3600"))
END_MODE = os.environ.get("P2_END", "front")
FLICKER_MS = 2_000
POLL_MS = 1_000  # liveness poll period (END_MODE "alive")
CHAIN_P_MS = 180_000
CHAIN_GAP_MAX_MS = 30 * 60_000

NAME = f"P2: focus rows + identity reopen (P={PULSE_S} s, end={END_MODE}) + C1 + C3 (180 s)"
SIGNALS = ("class_name", "facts_lookup")  # read once per new hwnd/pid and cached; counted per check here

# --- C1 verdict ----------------------------------------------------------------------------------

TOOLKIT_CLASSES = frozenset({"MozillaWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "WinUIDesktopWin32WindowClass",
                             "ApplicationFrameWindow", "Windows.UI.Core.CoreWindow", "ConsoleWindowClass"})
DEFAULT_EXCLUDED = frozenset({"qualityvault.exe"})


def _exe_facts() -> dict[str, dict]:
    """Launcher facts per exe as the 60 s background scan would cache them (from realwin.json)."""
    data = json.loads((ge.HERE / "realwin.json").read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for c in data["cases"]:
        out.setdefault(c["process"], {k: c.get(k) for k in ("under_launcher_game_dir", "in_gameconfigstore",
                                                            "under_steam_root", "exe_dir_kind")})
    return out


EXE_FACTS = _exe_facts()


def _facts(window, ctx) -> dict:
    facts = getattr(ctx, "facts", None)
    return facts if facts else EXE_FACTS.get(window.process, {})


def is_game(window, ctx) -> bool:
    p = window.process
    # exclusion is read first so nothing below logs the name; the verdict itself is unchanged
    if not p or gaming.covers_monitor(window.rect, ctx.monitors) is None:
        return False
    settings = ctx.settings
    if p in settings.games:
        return True
    if p in settings.not_games:
        return False
    facts = _facts(window, ctx)
    if facts.get("under_launcher_game_dir") or facts.get("in_gameconfigstore"):
        return True
    if window.exclusive_fullscreen:
        return True  # D3D exclusive stays a game even on the deny-list (#150)
    if window.has_frame or p in gaming.DEFAULT_NOT_GAMES:
        return False
    if facts.get("under_steam_root"):
        return False  # Steam root outside steamapps\common: Big Picture, steamwebhelper
    cls = facts.get("class") or ""
    return not (cls.startswith("Chrome_WidgetWin_") or cls in TOOLKIT_CLASSES)


def writes_name(window, ctx) -> bool:
    return window.process not in getattr(ctx, "excluded", DEFAULT_EXCLUDED)


# --- game rows ----------------------------------------------------------------------------------


def _creation(ctx, exe: str, t: int) -> int | None:
    """Creation time of the newest running process of ``exe`` at t."""
    best = None
    for a, b in ctx.alive.get(exe, ()):
        if a <= t < b and (best is None or a > best):
            best = a
    return best


def _hwnd_owner(ctx) -> dict[tuple[str, int], int | None]:
    """Harness stand-in for GetProcessTimes on the window's pid: the observer's process span that
    overlaps the window's sightings most (the fg log and the game's own start event are up to ~1 s
    apart, so a lookup by time alone gives a new window to the exiting process)."""
    seen: dict[tuple[str, int], list[int]] = {}
    for c in ctx.checks:
        if c.window is not None and c.window.hwnd and c.window.process in ctx.alive:
            s = seen.setdefault((c.window.process, c.window.hwnd), [c.ts_ms, c.ts_ms])
            s[1] = c.ts_ms
    out = {}
    for (exe, hwnd), (a, b) in seen.items():
        spans = ctx.alive[exe]
        best = max(spans, key=lambda s: (min(b + 1, s[1]) - max(a, s[0]), s[0]))
        out[(exe, hwnd)] = best[0] if min(b + 1, best[1]) > max(a, best[0]) else _creation(ctx, exe, a)
    return out


def _alive_until(ctx, exe: str, created: int | None) -> int | None:
    for a, b in ctx.alive.get(exe, ()):
        if a == created:
            return b
    return None


def stretches(ctx):
    """Today's GameMode with identity as the key: (identity, start, last, segment)."""
    grace = int(1000 * ctx.settings.end_grace_s)
    gap_starts = sorted(a for a, _b, _r in ctx.gaps)
    owner = _hwnd_owner(ctx)
    out = []
    cur = None
    start = last = 0
    prev_t = None
    gi = 0
    for c in ctx.checks:
        while gi < len(gap_starts) and gap_starts[gi] <= c.ts_ms:
            if cur is not None and prev_t is not None and gap_starts[gi] > prev_t:
                out.append((cur, start, last, gi))
                cur = None
            gi += 1
        prev_t = c.ts_ms
        if c.window is not None and is_game(c.window, ctx):
            key = (c.window.process, c.window.hwnd)
            created = owner[key] if key in owner else _creation(ctx, c.window.process, c.ts_ms)
            ident = (c.window.process, created)
            if cur is None:
                cur, start = ident, c.ts_ms
            elif ident != cur:
                out.append((cur, start, last, gi))
                cur, start = ident, c.ts_ms
            last = c.ts_ms
        elif cur is not None and c.ts_ms - last >= grace:
            out.append((cur, start, last, gi))
            cur = None
    if cur is not None:
        out.append((cur, start, last, gi))
    return out


def merged_rows(ctx, pulse_s: int):
    """[identity, start, end, segment] after the identity merge and the flicker rule."""
    pulse = pulse_s * 1000
    open_row: dict = {}  # identity -> its current row
    flicker: dict = {}  # identity -> (first flicker start, segment) since its last row
    rows = []
    for ident, a, b, seg in stretches(ctx):
        row = open_row.get(ident)
        if row is not None and row[3] == seg and a - row[2] <= pulse:
            row[2] = max(row[2], b)
            continue
        if b - a < FLICKER_MS:
            f = flicker.get(ident)
            if f is None or f[1] != seg or a - f[0] > pulse:
                flicker[ident] = (a, seg)
            continue  # a flicker never opens a row
        f = flicker.pop(ident, None)
        if f is not None and f[1] == seg and a - f[0] <= pulse:
            a = f[0]  # a row that does open starts at the flicker just before it (same identity)
        row = [ident, a, b, seg]
        open_row[ident] = row
        rows.append(row)
    return rows


def extend_alive(ctx, rows, last_ms: int):
    """END_MODE "alive": last row per (identity, segment) ends at the last liveness poll that saw
    the process (capped at the Pause that ends the segment); a game alive at a resume gets a row."""
    gaps = sorted((a, b) for a, b, _r in ctx.gaps)
    seg_end = [a for a, _b in gaps] + [last_ms]
    seg_start = [ctx.first_frame] + [b for _a, b in gaps]

    def poll_floor(t: int) -> int:
        return ctx.first_frame + (t - ctx.first_frame) // POLL_MS * POLL_MS

    last_of = {}
    for r in rows:
        last_of[(r[0], r[3])] = r
    extra = []
    for (ident, seg), r in list(last_of.items()):
        exe, created = ident
        exit_ms = _alive_until(ctx, exe, created)
        if exit_ms is None:
            continue
        r[2] = max(r[2], min(poll_floor(exit_ms), seg_end[seg]))
        # still alive at the next resume: the process gets a row there (reopen at max(creation, resume))
        nxt = seg + 1
        while nxt < len(seg_start) and exit_ms > seg_start[nxt] and (ident, nxt) not in last_of:
            a = max(created, seg_start[nxt])
            row = [ident, a, max(a, min(poll_floor(exit_ms), seg_end[nxt])), nxt]
            extra.append(row)
            nxt += 1
        if nxt < len(seg_start) and (ident, nxt) in last_of and exit_ms > seg_start[nxt]:
            first = min((x for x in rows if x[0] == ident and x[3] == nxt), key=lambda x: x[1])
            first[1] = max(created, seg_start[nxt])
    return sorted(rows + extra, key=lambda r: r[1])


def game_rows(conn, ctx, pulse_s: int | None = None, end_mode: str | None = None):
    pulse_s = PULSE_S if pulse_s is None else pulse_s
    end_mode = END_MODE if end_mode is None else end_mode
    rows = merged_rows(ctx, pulse_s)
    if end_mode == "alive":
        last_ms = max(ctx.t1, conn.execute("SELECT max(ts_ms) FROM frames").fetchone()[0] or ctx.t1)
        rows = extend_alive(ctx, rows, last_ms)
    names = ge.titles(ctx)
    return [(ident[0], names.get(ident[0]), a, b) for ident, a, b, _seg in rows]


# --- episodes -----------------------------------------------------------------------------------


def build_media_chains(conn, horizon: int) -> int:
    rows = conn.execute(
        "SELECT id, app, title, artist, url, start_ms, end_ms, played_ms FROM media_sessions m"
        " WHERE closed = 1 AND NOT EXISTS (SELECT 1 FROM episode_items i"
        "  WHERE i.item_kind = 'media_session' AND i.item_id = m.id) ORDER BY start_ms, id").fetchall()
    gaps = conn.execute("SELECT start_ms, COALESCE(end_ms, start_ms) FROM capture_gaps"
                        " WHERE kind IN ('all', 'screen') ORDER BY start_ms").fetchall()
    gap_a = [a for a, _b in gaps]

    def recorded_off(a: int, b: int) -> int:
        """Time in [a, b] covered by stored recording gaps."""
        i = max(0, bisect.bisect_right(gap_a, b) - 1)
        tot = 0
        for ga, gb in gaps[: i + 1]:
            tot += max(0, min(gb, b) - max(ga, a))
        return tot

    chains: list[dict] = []
    last_chain_of_app: dict[str, dict] = {}
    for sid, app, title, artist, url, a, b, played in rows:
        ch = last_chain_of_app.get(app)  # the app's latest item: nothing else of the app between
        gap = a - ch["end"] if ch else None
        if (ch is not None and ch["title"] == title and ch["artist"] == artist
                and not (url and ch["url"] and url != ch["url"])
                and (gap <= CHAIN_P_MS or (gap <= CHAIN_GAP_MAX_MS and gap - recorded_off(ch["end"], a) <= CHAIN_P_MS))):
            ch["end"] = max(ch["end"], b)
            ch["played"] += played
            ch["rows"].append((sid, a, b))
            ch["url"] = ch["url"] or url
            continue
        ch = {"app": app, "title": title, "artist": artist, "url": url, "start": a, "end": b, "played": played,
              "rows": [(sid, a, b)]}
        chains.append(ch)
        last_chain_of_app[app] = ch
    built = 0
    for ch in chains:
        if ch["played"] < production.MIN_PLAYED_MS or ch["end"] + CHAIN_P_MS > horizon:
            continue
        heard, seen = [], set()
        for _sid, a, b in ch["rows"]:
            for s in production._segments(conn, a, b):  # noqa: SLF001
                if s.id not in seen and s.stream == "system" and (not s.sources or production.app_matches_source(ch["app"], s.sources)):
                    seen.add(s.id)
                    heard.append(s)
        url = ch["url"]
        music = (url is not None and any(h in url for h in production.MUSIC_HOSTS)) or any(
            m in ch["app"].casefold() for m in production.MUSIC_APPS)
        head = f"{ch['title']}" + (f" - {ch['artist']}" if ch["artist"] else "") + f" ({ch['app']})"
        spoken = " ".join(s.transcript for s in heard)
        built += production._add_episode(  # noqa: SLF001
            conn, kind="music" if music else "video", ref=f"media:{ch['rows'][0][0]}", start_ms=ch["start"],
            end_ms=ch["end"], title=ch["title"], app=ch["app"], url=url, body=head if not spoken else f"{head}\n{spoken}",
            items=[*(("media_session", sid) for sid, _a, _b in ch["rows"]), *(("video_segment", s.id) for s in heard)],
            transcripts={s.id: s.transcript for s in heard})
    return built


def build_app_runs(conn, horizon: int) -> int:
    """Production build_app_runs, except a game row drops only its own process's visits."""
    row = conn.execute("SELECT COALESCE(MAX(item_id), 0) FROM episode_items WHERE item_kind = 'app_session'").fetchone()
    rows = conn.execute("SELECT id, process, window_title, url, start_ms, end_ms FROM app_sessions"
                        " WHERE id > ? AND process IS NOT NULL ORDER BY start_ms, id", (row[0],)).fetchall()
    if not rows:
        return 0
    games = conn.execute("SELECT process, start_ms, end_ms FROM game_sessions WHERE start_ms < ? AND end_ms > ?",
                         (rows[-1][5], rows[0][4])).fetchall()
    visits = [production._Visit(r[0], r[1], r[2] or "", r[3], r[4], r[5]) for r in rows  # noqa: SLF001
              if not any(g[0] == r[1] and g[1] < r[5] and g[2] > r[4] for g in games)]
    built = 0
    for run in production._runs(visits):  # noqa: SLF001
        kind = production._run_kind(run[0].process)  # noqa: SLF001
        start_ms, end_ms = run[0].start_ms, max(v.end_ms for v in run)
        if end_ms + production.CLOSE_GAP_MS > horizon:
            break
        if end_ms - start_ms < (production.MIN_BROWSING_MS if kind == "browsing" else production.MIN_WORK_MS):
            continue
        titles = production._top_titles(run)  # noqa: SLF001
        process = max({v.process for v in run}, key=lambda p: sum(v.end_ms - v.start_ms for v in run if v.process == p))
        url = next((v.url for v in sorted(run, key=lambda v: v.end_ms - v.start_ms, reverse=True) if v.url), None)
        lead = titles[0][0] if titles else production.app_label(process)
        body = "\n".join(t for t, _ in titles)
        if kind == "work":
            body = f"{production.app_label(process)}\n{body}" if body else production.app_label(process)
        built += production._add_episode(  # noqa: SLF001
            conn, kind=kind, ref=f"{kind}:{run[0].id}", start_ms=start_ms, end_ms=end_ms, title=lead, app=process,
            url=url, body=body, items=[("app_session", v.id) for v in run])
    return built


def episodes(conn, ctx):
    horizon = production.horizon_ms(conn, ctx.now_ms)
    closed = ctx.now_ms - production.CLOSE_GAP_MS
    build_media_chains(conn, horizon)
    production.build_games(conn, closed)
    production.build_conversations(conn, horizon)
    build_app_runs(conn, closed)
    return conn.execute("SELECT id, kind, start_ms, end_ms, title FROM episodes").fetchall()
