"""Build episodes from captured rows: videos, calls, games, browsing, work.

Pure SQL and Python, no model: a pass is cheap and runs in the worker's housekeeping. Episodes
are built only from *closed* periods (the horizon below) and each source row is used once, so a
pass is incremental and repeating it changes nothing. ``body`` (a digest of the evidence) is
what search and the answering model see; no language-model summary is written.

What becomes an episode:

- ``video`` / ``music``: a closed media session that played at least a minute. The body holds its
  title and channel plus what was said on the system audio while it played (the transcript of
  the video, which is what "something about a game" is matched against).
- ``game``: a closed fullscreen-game session of at least two minutes.
- ``conversation``: speech heard while a voice app (Discord, Teams, ...) or the game being
  played was making sound, chained while pauses stay under five minutes, together with the
  microphone speech in the same stretch. People are the voices in it (``episode_people``).
- ``browsing`` / ``work``: runs of app sessions in a browser or in any other program, chained
  while gaps stay under five minutes; at least three and five minutes long. Time spent in a
  game is left out.

Episodes may overlap in time: a video is watched during a browsing run. Nothing is deleted from
the evidence tables; ``episode_items`` only links to it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from akashic.storage import db, state

if TYPE_CHECKING:
    from sqlite3 import Connection

log = logging.getLogger(__name__)

CLOSE_GAP_MS: Final = 5 * 60_000  # silence that ends a conversation / a browsing run
MIC_MARGIN_MS: Final = 30_000  # microphone speech this close to a call belongs to it
CONVERSATIONS_SINCE: Final = "conversations_since_ms"  # state key: how far back to look
MIN_PLAYED_MS: Final = 60_000
MIN_GAME_MS: Final = 2 * 60_000
MIN_BROWSING_MS: Final = 3 * 60_000
MIN_WORK_MS: Final = 5 * 60_000
MAX_BODY_CHARS: Final = 6_000
MAX_TITLES: Final = 8

# Lower-case executable names whose sound means people are talking to each other.
VOICE_APPS: Final = frozenset(
    {
        "discord.exe", "discordptb.exe", "discordcanary.exe", "teams.exe", "ms-teams.exe",
        "zoom.exe", "skype.exe", "telegram.exe", "whatsapp.exe", "teamspeak.exe",
        "ts3client_win64.exe", "mumble.exe", "guilded.exe", "slack.exe", "signal.exe",
    }
)  # fmt: skip
BROWSER_PROCESSES: Final = frozenset(
    {"chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "zen.exe", "opera.exe", "vivaldi.exe"}
)
MUSIC_APPS: Final = ("spotify", "deezer", "apple music", "itunes", "tidal", "foobar", "musicbee")
MUSIC_HOSTS: Final = ("music.youtube.com", "open.spotify.com", "soundcloud.com", "deezer.com")


def app_label(process: str | None) -> str:
    """``discord.exe`` -> ``Discord``."""
    stem = (process or "").removesuffix(".exe")
    return stem[:1].upper() + stem[1:]


def app_matches_source(app: str, sources: tuple[str, ...]) -> bool:
    """Whether a media session's display name ("Zen", "Google Chrome") is one of the processes
    that were making sound. Sessions publish names, audio records executables; this is the
    loose bridge between the two."""
    name = app.casefold()
    return any(
        (stem := source.removesuffix(".exe")) and (stem in name or name in stem)
        for source in sources
    )


@dataclass(frozen=True, slots=True)
class _Segment:
    id: int
    stream: str
    start_ms: int
    end_ms: int
    speech_ms: int
    sources: tuple[str, ...]
    transcript: str


@dataclass(slots=True)
class _Cluster:
    segments: list[_Segment] = field(default_factory=list[_Segment])

    @property
    def start_ms(self) -> int:
        return self.segments[0].start_ms

    @property
    def end_ms(self) -> int:
        return max(s.end_ms for s in self.segments)


def _clip(text: str, limit: int = MAX_BODY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def horizon_ms(conn: Connection, now_ms: int) -> int:
    """Everything before this is final: old enough to be closed, and no transcription or
    speaker job is still to run on anything earlier."""
    row = conn.execute(
        "SELECT MIN(data_ms) FROM jobs WHERE kind IN ('transcribe', 'speakers')"
        " AND state IN ('pending', 'running')"
    ).fetchone()
    pending: int | None = row[0]
    closed = now_ms - CLOSE_GAP_MS
    return closed if pending is None else min(closed, pending)


def _segment(row: tuple[int, str, int, int, int, str, str]) -> _Segment:
    sources = tuple(str(x) for x in json.loads(row[5]))
    return _Segment(row[0], row[1], row[2], row[3], row[4], sources, row[6])


def _segments(conn: Connection, start_ms: int, end_ms: int) -> list[_Segment]:
    """Transcribed segments overlapping [start_ms, end_ms], oldest first."""
    rows = conn.execute(
        "SELECT id, stream, start_ms, end_ms, speech_ms, sources, transcript FROM audio_segments"
        " WHERE status = 'done' AND transcript IS NOT NULL AND transcript != ''"
        " AND start_ms < ? AND end_ms > ? ORDER BY start_ms, id",
        (end_ms, start_ms),
    ).fetchall()
    return [_segment(r) for r in rows]


def _add_episode(  # noqa: PLR0913 - one row and its links
    conn: Connection,
    *,
    kind: str,
    ref: str,
    start_ms: int,
    end_ms: int,
    title: str,
    app: str | None,
    url: str | None,
    body: str,
    items: list[tuple[str, int]],
    people: dict[int, int] | None = None,
    transcripts: dict[int, str] | None = None,
) -> bool:
    with db.transaction(conn):
        # Evidence was read before this transaction. Retention/retranscription may have
        # changed it meanwhile; never put the stale text back into a new digest.
        if transcripts is not None and any(
            conn.execute("SELECT transcript FROM audio_segments WHERE id = ?", (i,)).fetchone()
            != (text,)
            for i, text in transcripts.items()
        ):
            return False
        cursor = conn.execute(
            "INSERT OR IGNORE INTO episodes (kind, ref, start_ms, end_ms, title, app, url, body)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, ref, start_ms, end_ms, title, app, url, _clip(body)),
        )
        if cursor.rowcount != 1 or cursor.lastrowid is None:
            return False
        episode_id = cursor.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO episode_items (episode_id, item_kind, item_id) VALUES (?, ?, ?)",
            [(episode_id, kind_, id_) for kind_, id_ in items],
        )
        conn.executemany(
            "INSERT INTO episode_people (episode_id, speaker_id, speech_ms) VALUES (?, ?, ?)",
            [(episode_id, speaker, ms) for speaker, ms in (people or {}).items()],
        )
    return True


# --- videos and music ----------------------------------------------------------------------


def build_media(conn: Connection, horizon: int) -> int:
    rows = conn.execute(
        "SELECT id, app, title, artist, url, start_ms, end_ms FROM media_sessions m"
        " WHERE closed = 1 AND played_ms >= ? AND end_ms <= ?"
        " AND NOT EXISTS (SELECT 1 FROM episode_items i"
        "                 WHERE i.item_kind = 'media_session' AND i.item_id = m.id)"
        " ORDER BY start_ms",
        (MIN_PLAYED_MS, horizon),
    ).fetchall()
    built = 0
    for session_id, app, title, artist, url, start_ms, end_ms in rows:
        heard = [
            s
            for s in _segments(conn, start_ms, end_ms)
            if s.stream == "system" and (not s.sources or app_matches_source(app, s.sources))
        ]
        host_music = url is not None and any(h in url for h in MUSIC_HOSTS)
        music = host_music or any(m in app.casefold() for m in MUSIC_APPS)
        head = f"{title}" + (f" - {artist}" if artist else "") + f" ({app})"
        spoken = " ".join(s.transcript for s in heard)
        body = head if not spoken else f"{head}\n{spoken}"
        built += _add_episode(
            conn,
            kind="music" if music else "video",
            ref=f"media:{session_id}",
            start_ms=start_ms,
            end_ms=end_ms,
            title=title,
            app=app,
            url=url,
            body=body,
            items=[("media_session", session_id), *(("video_segment", s.id) for s in heard)],
            transcripts={s.id: s.transcript for s in heard},
        )
    return built


# --- games ---------------------------------------------------------------------------------


def build_games(conn: Connection, horizon: int) -> int:
    rows = conn.execute(
        "SELECT id, process, window_title, start_ms, end_ms FROM game_sessions g"
        " WHERE closed = 1 AND end_ms - start_ms >= ? AND end_ms <= ?"
        " AND NOT EXISTS (SELECT 1 FROM episode_items i"
        "                 WHERE i.item_kind = 'game_session' AND i.item_id = g.id)"
        " ORDER BY start_ms",
        (MIN_GAME_MS, horizon),
    ).fetchall()
    built = 0
    for game_id, process, window_title, start_ms, end_ms in rows:
        title = window_title or app_label(process)
        built += _add_episode(
            conn,
            kind="game",
            ref=f"game:{game_id}",
            start_ms=start_ms,
            end_ms=end_ms,
            title=title,
            app=process,
            url=None,
            body=f"{title} ({process})",
            items=[("game_session", game_id)],
        )
    return built


# --- conversations -------------------------------------------------------------------------


def _games(conn: Connection, start_ms: int, end_ms: int) -> list[tuple[str, int, int]]:
    """Game sessions (process, start, end) overlapping the range."""
    rows = conn.execute(
        "SELECT process, start_ms, end_ms FROM game_sessions WHERE start_ms < ? AND end_ms > ?",
        (end_ms, start_ms),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _is_voice(seg: _Segment, games: list[tuple[str, int, int]]) -> bool:
    """Sound from a voice app, or from the game that was running at that moment (in-game chat)."""
    if VOICE_APPS.intersection(seg.sources):
        return True
    return any(
        process in seg.sources and start < seg.end_ms and end > seg.start_ms
        for process, start, end in games
    )


def _conversation_clusters(
    conn: Connection, horizon: int, since_ms: int
) -> tuple[list[_Cluster], int | None]:
    """Final clusters among the unlinked segments ending after ``since_ms``, and the start of
    the earliest cluster that may still grow (None when there is none)."""
    rows = conn.execute(
        "SELECT id, stream, start_ms, end_ms, speech_ms, sources, transcript FROM audio_segments a"
        " WHERE status = 'done' AND transcript IS NOT NULL AND transcript != ''"
        " AND end_ms > ?"
        " AND NOT EXISTS (SELECT 1 FROM episode_items i"
        "                 WHERE i.item_kind = 'segment' AND i.item_id = a.id)"
        " ORDER BY start_ms, id",
        (since_ms,),
    ).fetchall()
    segments = [_segment(r) for r in rows]
    if not segments:
        return [], None
    games = _games(conn, segments[0].start_ms, segments[-1].end_ms)
    clusters: list[_Cluster] = []
    for seg in segments:
        if not _is_voice(seg, games):
            continue
        if clusters and seg.start_ms - clusters[-1].end_ms <= CLOSE_GAP_MS:
            clusters[-1].segments.append(seg)
        else:
            clusters.append(_Cluster([seg]))
    for cluster in clusters:  # the microphone side of the same stretch
        taken = {s.id for s in cluster.segments}
        cluster.segments.extend(
            s
            for s in segments
            if s.id not in taken
            and s.stream == "mic"
            and s.start_ms < cluster.end_ms + MIC_MARGIN_MS
            and s.end_ms > cluster.start_ms - MIC_MARGIN_MS
        )
        cluster.segments.sort(key=lambda s: (s.start_ms, s.id))
    # A cluster is final once no later speech could still chain onto it.
    final = [c for c in clusters if c.end_ms + CLOSE_GAP_MS <= horizon]
    growing = [c.start_ms for c in clusters if c.end_ms + CLOSE_GAP_MS > horizon]
    return final, min(growing, default=None)


def _main_voice_app(cluster: _Cluster) -> str:
    counts: dict[str, int] = {}
    for seg in cluster.segments:
        for source in seg.sources:
            if source in VOICE_APPS:
                counts[source] = counts.get(source, 0) + 1
    if counts:
        return max(counts, key=lambda s: counts[s])
    for seg in cluster.segments:
        if seg.sources:
            return seg.sources[0]
    return ""


def build_conversations(conn: Connection, horizon: int) -> int:
    since = state.get_int(conn, CONVERSATIONS_SINCE, 0)
    final, growing_start = _conversation_clusters(conn, horizon, since)
    built = 0
    for cluster in final:
        ids = [s.id for s in cluster.segments]
        marks = ",".join("?" * len(ids))
        people = {
            int(r[0]): int(r[1])
            for r in conn.execute(
                "SELECT speaker_id, sum(speech_ms) FROM speaker_turns"  # noqa: S608
                f" WHERE speaker_id IS NOT NULL AND segment_id IN ({marks}) GROUP BY speaker_id",
                ids,
            ).fetchall()
        }
        app = _main_voice_app(cluster)
        label = app_label(app)
        added = _add_episode(
            conn,
            kind="conversation",
            ref=f"conversation:{cluster.segments[0].id}",
            start_ms=cluster.start_ms,
            end_ms=cluster.end_ms,
            title=f"{label} conversation" if label else "Conversation",
            app=app or None,
            url=None,
            body=" ".join(s.transcript for s in cluster.segments),
            items=[("segment", s.id) for s in cluster.segments],
            people=people,
            transcripts={s.id: s.transcript for s in cluster.segments},
        )
        if not added:
            return built  # retry changed evidence before advancing the conversation watermark
        built += 1
    # Every segment ending before this point is linked already or can never join a future
    # cluster: later speech starts after the horizon (pending transcriptions hold it back),
    # and a cluster takes microphone speech at most MIC_MARGIN_MS before its start. So the
    # next pass need not read it again; without this the scan grows with all of history.
    new_since = horizon - CLOSE_GAP_MS - MIC_MARGIN_MS
    if growing_start is not None:
        new_since = min(new_since, growing_start - MIC_MARGIN_MS)
    if new_since > since:
        state.set_int(conn, CONVERSATIONS_SINCE, new_since)
    return built


# --- browsing and work ---------------------------------------------------------------------


@dataclass(slots=True)
class _Visit:
    id: int
    process: str
    title: str
    url: str | None
    start_ms: int
    end_ms: int


def _run_kind(process: str) -> str:
    return "browsing" if process in BROWSER_PROCESSES else "work"


def _runs(visits: list[_Visit]) -> list[list[_Visit]]:
    runs: list[list[_Visit]] = []
    for visit in visits:
        if (
            runs
            and _run_kind(runs[-1][-1].process) == _run_kind(visit.process)
            and visit.start_ms - max(v.end_ms for v in runs[-1]) <= CLOSE_GAP_MS
        ):
            runs[-1].append(visit)
        else:
            runs.append([visit])
    return runs


def _top_titles(run: list[_Visit]) -> list[tuple[str, int]]:
    spent: dict[str, int] = {}
    for v in run:
        if v.title:
            spent[v.title] = spent.get(v.title, 0) + max(v.end_ms - v.start_ms, 1_000)
    return sorted(spent.items(), key=lambda kv: kv[1], reverse=True)[:MAX_TITLES]


def build_app_runs(conn: Connection, horizon: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(item_id), 0) FROM episode_items WHERE item_kind = 'app_session'"
    ).fetchone()
    rows = conn.execute(
        "SELECT id, process, window_title, url, start_ms, end_ms FROM app_sessions"
        " WHERE id > ? AND process IS NOT NULL ORDER BY start_ms, id",
        (row[0],),
    ).fetchall()
    if not rows:
        return 0
    games = conn.execute(
        "SELECT start_ms, end_ms FROM game_sessions WHERE start_ms < ? AND end_ms > ?",
        (rows[-1][5], rows[0][4]),
    ).fetchall()
    visits = [
        _Visit(r[0], r[1], r[2] or "", r[3], r[4], r[5])
        for r in rows
        if not any(g[0] < r[5] and g[1] > r[4] for g in games)
    ]
    built = 0
    for run in _runs(visits):
        kind = _run_kind(run[0].process)
        start_ms, end_ms = run[0].start_ms, max(v.end_ms for v in run)
        if end_ms + CLOSE_GAP_MS > horizon:
            break  # may still grow; later runs are newer still
        if end_ms - start_ms < (MIN_BROWSING_MS if kind == "browsing" else MIN_WORK_MS):
            continue
        titles = _top_titles(run)
        process = max(
            {v.process for v in run},
            key=lambda p: sum(v.end_ms - v.start_ms for v in run if v.process == p),
        )
        url = next(
            (
                v.url
                for v in sorted(run, key=lambda v: v.end_ms - v.start_ms, reverse=True)
                if v.url
            ),
            None,
        )
        lead = titles[0][0] if titles else app_label(process)
        body = "\n".join(t for t, _ in titles)
        if kind == "work":
            body = f"{app_label(process)}\n{body}" if body else app_label(process)
        built += _add_episode(
            conn,
            kind=kind,
            ref=f"{kind}:{run[0].id}",
            start_ms=start_ms,
            end_ms=end_ms,
            title=lead,
            app=process,
            url=url,
            body=body,
            items=[("app_session", v.id) for v in run],
        )
    return built


def build_episodes(conn: Connection, now_ms: int) -> dict[str, int]:
    """One incremental pass over every source. Returns how many episodes each step made."""
    horizon = horizon_ms(conn, now_ms)
    return {
        "media": build_media(conn, horizon),
        "games": build_games(conn, horizon),
        "conversations": build_conversations(conn, horizon),
        "app_runs": build_app_runs(conn, horizon),
    }
