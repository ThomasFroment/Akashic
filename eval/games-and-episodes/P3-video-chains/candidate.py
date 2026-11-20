"""P3: C3 video viewing chains alone, on today's stored game and media rows.

Video/music episodes are built from *chains* of media rows instead of single rows:

- same app, same title (casefold), artist compared only when both rows have one, url compared
  only when both rows have one;
- an observed (recording-on) gap of at most P between a chain's tail and the next row, with no
  other item of that app between them;
- the recorded-off part of the gap (stored capture_gaps of kind all/screen: Pause, lock, sleep,
  stop) bridged only up to RECORDING_GAP_MAX_MS (30 min), per CAPTURE.md / RETRIEVAL.md;
- MIN_PLAYED_MS applied to the chain's summed played_ms;
- rewatch guard: a new chain when the stored playback position restarts near 0 or the previous
  row reached the media's end. Today's store keeps no position or duration (media_sessions has
  start/end wall times and played_ms; media_events has playing/paused/stopped), so the guard is
  active only when the optional columns start_pos_ms / end_pos_ms / duration_ms exist (synthetic
  set). REWATCH_PROXY = True adds a weak proxy (previous row's last state 'paused'); off by default;
- incremental: a chain is emitted only when it is final at the horizon (all rows closed and before
  the horizon, and the recording-on time since its tail > P or the recorded-off time > 30 min);
  without capture gaps this is exactly "tail + P <= horizon".

Game rows, conversations and app runs: production code, unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from akashic.pipeline import episodes as production
from akashic.pipeline.episodes import (
    MIN_PLAYED_MS,
    MUSIC_APPS,
    MUSIC_HOSTS,
    _add_episode,
    _segments,
    app_matches_source,
    horizon_ms,
)
from akashic.storage import state

NAME = "P3 video chains (C3 alone, P=180 s, today's game rows)"
P_MS = 180_000
RECORDING_GAP_MAX_MS = 30 * 60_000
GAP_KINDS = ("all", "screen")
REWATCH_PROXY = False
CHAINS_SINCE = "media_chains_since_ms"  # state key: rows before it are settled
URL_IDENTITY = False  # variant: equal urls (both set, not a music host) chain across a title change
NEAR_ZERO_MS = 10_000  # position this close to 0 is a restart
END_TOL_MS = 15_000  # position this close to the duration is the end

STATS: dict = {"calls": []}  # per episodes() call: media+game ms and chain counts


@dataclass(slots=True)
class Row:
    id: int
    app: str
    title: str
    artist: str | None
    url: str | None
    start: int
    end: int
    played: int
    closed: int
    linked: bool
    last_state: str | None
    spos: int | None = None
    epos: int | None = None
    dur: int | None = None

    @property
    def tkey(self) -> str:
        return self.title.casefold().strip()


@dataclass(slots=True)
class Chain:
    rows: list[Row] = field(default_factory=list)
    bridged_obs: int = 0
    bridged_rec: int = 0

    @property
    def tail(self) -> Row:
        return self.rows[-1]


def _both_differ(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.casefold() != b.casefold()


def same_item(a: Row, b: Row, url_identity: bool = False) -> bool:
    if a.app != b.app or _both_differ(a.artist, b.artist) or _both_differ(a.url, b.url):
        return False
    if a.tkey == b.tkey:
        return True
    return url_identity and bool(a.url) and a.url == b.url and not any(h in a.url for h in MUSIC_HOSTS)


def off_ms(gaps: list[tuple[int, int]], a: int, b: int) -> int:
    """Recorded-off time inside [a, b] (gaps are merged, sorted)."""
    return sum(max(0, min(b, g1) - max(a, g0)) for g0, g1 in gaps if g0 < b and g1 > a)


def merged_gaps(conn, horizon: int) -> list[tuple[int, int]]:
    spans = sorted((a, horizon if b is None else min(b, horizon)) for a, b in conn.execute(
        "SELECT start_ms, end_ms FROM capture_gaps WHERE kind IN (%s) AND start_ms < ?" % ",".join("?" * len(GAP_KINDS)),
        (*GAP_KINDS, horizon)))
    out: list[list[int]] = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def rewatch(prev: Row, nxt: Row, proxy: bool) -> bool:
    if prev.dur and prev.epos is not None and prev.epos >= prev.dur - END_TOL_MS:
        return True
    if nxt.spos is not None and prev.epos is not None and nxt.spos <= NEAR_ZERO_MS < prev.epos - 30_000:
        return True
    return proxy and prev.last_state == "paused"


def build_chains(rows: list[Row], gaps, p_ms: int, proxy: bool, url_identity: bool, counts: dict) -> list[Chain]:
    """Rows of all apps, sorted by start. One pass per app."""
    by_app: dict[str, list[Row]] = {}
    for r in rows:
        by_app.setdefault(r.app, []).append(r)
    chains: list[Chain] = []
    for app_rows in by_app.values():
        open_: list[Chain] = []
        for i, r in enumerate(app_rows):
            best = None
            # the latest chain of this item (by tail end) is the only one r may extend
            c = max((c for c in open_ if same_item(c.tail, r, url_identity)), key=lambda c: c.tail.end, default=None)
            if c is not None:
                t = c.tail
                lo, hi = t.end, max(t.end, r.start)
                off = off_ms(gaps, lo, hi)
                obs = hi - lo - off
                if obs > p_ms:
                    counts["break_gap"] += 1
                elif off > RECORDING_GAP_MAX_MS:
                    counts["break_recording_gap"] += 1
                elif any(not same_item(t, x, url_identity) and x.id != r.id
                         and ((t.start < x.start < r.start) or (x.start < hi and x.end > lo))
                         for x in app_rows[:i] if x.end > t.start):  # another item of this app between
                    counts["break_other_item"] += 1
                elif rewatch(t, r, proxy):
                    counts["break_rewatch"] += 1
                else:
                    best = (c, obs, off)
            if best is None:
                c = Chain([r])
                open_.append(c)
                chains.append(c)
            else:
                c, obs, off = best
                c.rows.append(r)
                c.bridged_obs += obs > 0 and off == 0
                c.bridged_rec += off > 0
    chains.sort(key=lambda c: c.rows[0].start)
    return chains


def final(c: Chain, gaps, horizon: int, p_ms: int) -> bool:
    if any(not r.closed or r.end > horizon for r in c.rows):
        return False
    t = c.tail.end
    off = off_ms(gaps, t, horizon)
    return horizon - t - off > p_ms or off > RECORDING_GAP_MAX_MS


def load_rows(conn, horizon: int, p_ms: int) -> tuple[list[Row], int]:
    """Every row not yet linked since the watermark, plus the rows around them (context for 'other
    item between' and for spotting a chain that would extend an emitted one)."""
    since = state.get_int(conn, CHAINS_SINCE, 0)
    (lo,) = conn.execute(
        "SELECT MIN(start_ms) FROM media_sessions m WHERE start_ms >= ? AND start_ms <= ? AND NOT EXISTS"
        " (SELECT 1 FROM episode_items i WHERE i.item_kind = 'media_session' AND i.item_id = m.id)",
        (since, horizon)).fetchone()
    if lo is None:
        return [], since
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media_sessions)")}
    pos = all(c in cols for c in ("start_pos_ms", "end_pos_ms", "duration_ms"))
    extra = ", start_pos_ms, end_pos_ms, duration_ms" if pos else ""
    settled, since = since, lo - p_ms - RECORDING_GAP_MAX_MS
    q = ("SELECT id, app, title, artist, url, start_ms, end_ms, played_ms, closed,"
         " EXISTS (SELECT 1 FROM episode_items i WHERE i.item_kind = 'media_session' AND i.item_id = m.id),"
         " (SELECT state FROM media_events e WHERE e.media_session_id = m.id AND e.state != 'stopped'"
         "  ORDER BY ts_ms DESC, id DESC LIMIT 1)" + extra +
         " FROM media_sessions m WHERE start_ms <= ? AND end_ms >= ? ORDER BY start_ms, id")
    return [Row(*r) for r in conn.execute(q, (horizon, since))], settled


def build_media_chains(conn, horizon: int, p_ms: int = P_MS, proxy: bool = REWATCH_PROXY,
                       url_identity: bool = URL_IDENTITY, counts: dict | None = None) -> int:
    counts = counts if counts is not None else {}
    for k in ("rows", "chains", "multi_row_chains", "bridged_obs", "bridged_rec", "break_gap", "break_recording_gap",
              "break_other_item", "break_rewatch", "dropped_min_played", "not_final", "late_extension"):
        counts.setdefault(k, 0)
    rows, settled = load_rows(conn, horizon, p_ms)
    if not rows:
        return 0
    gaps = merged_gaps(conn, horizon)
    chains = build_chains(rows, gaps, p_ms, proxy, url_identity, counts)
    built = 0
    watermark = horizon
    for c in chains:
        linked = [r.linked for r in c.rows]
        if all(linked) or all(r.start < settled for r in c.rows):
            continue  # emitted, or settled context
        if any(linked) or any(r.start < settled for r in c.rows):
            counts["late_extension"] += 1  # a row would extend an emitted/settled chain: a split in the making
            c = Chain([r for r in c.rows if not r.linked and r.start >= settled])
        if not final(c, gaps, horizon, p_ms):
            counts["not_final"] += 1
            watermark = min(watermark, c.rows[0].start)
            continue
        counts["rows"] += len(c.rows)
        played = sum(r.played for r in c.rows)
        if played < MIN_PLAYED_MS:
            counts["dropped_min_played"] += 1  # left unlinked, below the watermark from now on
            continue
        counts["chains"] += 1
        counts["multi_row_chains"] += len(c.rows) > 1
        counts["bridged_obs"] += c.bridged_obs
        counts["bridged_rec"] += c.bridged_rec
        head_row = max(c.rows, key=lambda r: r.played)
        first = c.rows[0]
        start, end = first.start, max(r.end for r in c.rows)
        app, title = first.app, head_row.title
        artist = next((r.artist for r in c.rows if r.artist), None)
        url = next((r.url for r in c.rows if r.url), None)
        heard = [s for s in _segments(conn, start, end)
                 if s.stream == "system" and (not s.sources or app_matches_source(app, s.sources))]
        music = (url is not None and any(h in url for h in MUSIC_HOSTS)) or any(m in app.casefold() for m in MUSIC_APPS)
        head = f"{title}" + (f" - {artist}" if artist else "") + f" ({app})"
        spoken = " ".join(s.transcript for s in heard)
        built += _add_episode(
            conn, kind="music" if music else "video", ref=f"media:{first.id}", start_ms=start, end_ms=end,
            title=title, app=app, url=url, body=head if not spoken else f"{head}\n{spoken}",
            items=[*(("media_session", r.id) for r in c.rows), *(("video_segment", s.id) for s in heard)],
            transcripts={s.id: s.transcript for s in heard},
        )
    state.set_int(conn, CHAINS_SINCE, max(settled, watermark))
    return built


def episodes(conn, ctx):
    horizon = horizon_ms(conn, ctx.now_ms)
    closed = ctx.now_ms - production.CLOSE_GAP_MS
    counts: dict = {}
    t = time.perf_counter()
    build_media_chains(conn, horizon, counts=counts)
    t_media = time.perf_counter()
    production.build_games(conn, closed)
    t_games = time.perf_counter()
    production.build_conversations(conn, horizon)
    production.build_app_runs(conn, closed)
    STATS["calls"].append({"run": ctx.run_id, "media_ms": (t_media - t) * 1000, "games_ms": (t_games - t_media) * 1000,
                           "counts": counts})
    return conn.execute("SELECT id, kind, start_ms, end_ms, title FROM episodes").fetchall()
