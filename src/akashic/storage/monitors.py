"""Monitors over time: which monitors existed, where they sat, and what each one showed.

``MonitorLog`` writes two kinds of runs: layout runs (the set of monitors, their rectangles and
scales) and state runs (per monitor: shown, hidden, dropped or failed). A state run grows by
updating ``checked_ms`` while the same state is confirmed, so a long stretch is one row. Current
runs are tracked in memory, so a restart starts new runs.

``stage_at`` and ``latest_valid_frame`` read the history back. A gap that stops the screen
(``all`` or ``screen``) overrides the state runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from akashic.storage import db, gaps

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from sqlite3 import Connection

type MonitorState = Literal["shown", "hidden", "dropped", "failed"]
type StageState = MonitorState | Literal["not_recording"]
type Rect = tuple[int, int, int, int]  # left, top, right, bottom

SCREEN_STOPPING_GAPS: Final[tuple[gaps.GapKind, ...]] = ("all", "screen")


@dataclass(frozen=True, slots=True)
class MonitorRow:
    key: str
    name: str
    rect: Rect
    scale: float
    is_main: bool


@dataclass(frozen=True, slots=True)
class MonitorAt:
    monitor_id: int
    name: str
    rect: Rect
    scale: float
    state: StageState
    frame_id: int | None
    checked_ms: int | None


@dataclass(slots=True)
class _Run:
    run_id: int
    state: MonitorState


class MonitorLog:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._layout: list[MonitorRow] | None = None
        self._ids: dict[str, int] = {}
        self._runs: dict[int, _Run] = {}

    def present(self, rows: Sequence[MonitorRow], ts_ms: int) -> dict[str, int]:
        """Record the monitors now present; a new layout run starts only when the set changed."""
        layout = sorted(rows, key=lambda row: row.key)
        if layout == self._layout:
            return dict(self._ids)
        with db.transaction(self._conn) as conn:
            ids = {
                row.key: conn.execute(
                    "INSERT INTO monitors (key, name) VALUES (?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET name = excluded.name RETURNING id",
                    (row.key, row.name),
                ).fetchone()[0]
                for row in rows
            }
            layout_id = conn.execute(
                "INSERT INTO monitor_layouts (start_ms) VALUES (?) RETURNING id", (ts_ms,)
            ).fetchone()[0]
            conn.executemany(
                "INSERT INTO layout_monitors (layout_id, monitor_id, x0, y0, x1, y1, scale,"
                " is_main) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(layout_id, ids[row.key], *row.rect, row.scale, row.is_main) for row in rows],
            )
        self._layout = layout
        self._ids = ids
        return dict(ids)

    def state(self, monitor_id: int, state: MonitorState, ts_ms: int) -> None:
        current = self._runs.get(monitor_id)
        with db.transaction(self._conn) as conn:
            if current is not None and current.state == state:
                conn.execute(
                    "UPDATE monitor_states SET checked_ms = MAX(?, checked_ms) WHERE id = ?",
                    (ts_ms, current.run_id),
                )
                return
            run_id = conn.execute(
                "INSERT INTO monitor_states (monitor_id, state, start_ms, checked_ms)"
                " VALUES (?, ?, ?, ?) RETURNING id",
                (monitor_id, state, ts_ms, ts_ms),
            ).fetchone()[0]
        self._runs[monitor_id] = _Run(run_id, state)


def stage_at(conn: Connection, ts: int, now: int) -> list[MonitorAt]:
    """The desk at ``ts``: every monitor of the layout then, with its state and latest frame."""
    layout_id = _layout_at(conn, ts)
    if layout_id is None:
        return []
    stopped = bool(gaps.overlapping(conn, ts, ts, now, SCREEN_STOPPING_GAPS))
    members = conn.execute(
        "SELECT m.id, m.name, l.x0, l.y0, l.x1, l.y1, l.scale FROM layout_monitors l"
        " JOIN monitors m ON m.id = l.monitor_id WHERE l.layout_id = ? ORDER BY m.id",
        (layout_id,),
    ).fetchall()
    return [
        MonitorAt(
            monitor_id,
            name,
            (x0, y0, x1, y1),
            scale,
            *_state_at(conn, ts, monitor_id, stopped=stopped),
        )
        for monitor_id, name, x0, y0, x1, y1, scale in members
    ]


def _layout_at(conn: Connection, ts: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM monitor_layouts WHERE start_ms <= ?"
        " ORDER BY start_ms DESC, id DESC LIMIT 1",
        (ts,),
    ).fetchone()
    return None if row is None else row[0]


def monitor_rect(conn: Connection, monitor_id: int, ts: int) -> Rect | None:
    """Where the monitor sat on the desktop at ``ts``; None when the layout then lacked it."""
    layout_id = _layout_at(conn, ts)
    if layout_id is None:
        return None
    row = conn.execute(
        "SELECT x0, y0, x1, y1 FROM layout_monitors WHERE layout_id = ? AND monitor_id = ?",
        (layout_id, monitor_id),
    ).fetchone()
    return None if row is None else (row[0], row[1], row[2], row[3])


def _state_at(
    conn: Connection, ts: int, monitor_id: int, *, stopped: bool
) -> tuple[StageState, int | None, int | None]:
    """The monitor's state, latest frame id and last confirmation at ``ts``."""
    run = None
    if not stopped:
        run = conn.execute(
            "SELECT state, checked_ms FROM monitor_states WHERE monitor_id = ? AND start_ms <= ?"
            " ORDER BY start_ms DESC, id DESC LIMIT 1",
            (monitor_id, ts),
        ).fetchone()
    if run is None:
        return "not_recording", None, None
    state, checked_ms = run
    frame = _latest_frame(conn, monitor_id, ts) if state == "shown" else None
    return state, None if frame is None else frame[0], checked_ms


def _latest_frame(conn: Connection, monitor_id: int, ts: int) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT id, ts_ms FROM frames WHERE monitor_id = ? AND ts_ms <= ?"
        " ORDER BY ts_ms DESC, id DESC LIMIT 1",
        (monitor_id, ts),
    ).fetchone()
    return None if row is None else (row[0], row[1])


def latest_valid_frame(
    conn: Connection, monitor_id: int, hwnd: int, ts: int, now: int
) -> int | None:
    """The monitor's latest frame at ``ts`` if it still shows ``hwnd`` as it was then.

    After the frame, nothing may have interrupted the screen: no stopping gap, no state other
    than ``shown``, and no layout without the monitor.
    """
    frame = _latest_frame(conn, monitor_id, ts)
    if frame is None:
        return None
    frame_id, frame_ts = frame
    window = conn.execute(
        "SELECT 1 FROM frame_windows WHERE frame_id = ? AND hwnd = ?", (frame_id, hwnd)
    ).fetchone()
    if window is None:
        return None
    if frame_ts == ts or _uninterrupted(conn, monitor_id, frame_ts, ts, now):
        return frame_id
    return None


def _uninterrupted(conn: Connection, monitor_id: int, after: int, until: int, now: int) -> bool:
    if gaps.overlapping(conn, after, until, now, SCREEN_STOPPING_GAPS):
        return False
    params = {"m": monitor_id, "ts": until, "f": after}
    return _always(
        conn,
        "SELECT state = 'shown' FROM monitor_states WHERE monitor_id = :m AND start_ms <= :ts"
        " AND start_ms >= (SELECT MAX(start_ms) FROM monitor_states"
        "                  WHERE monitor_id = :m AND start_ms <= :f)",
        params,
    ) and _always(
        conn,
        "SELECT EXISTS (SELECT 1 FROM layout_monitors WHERE layout_id = l.id AND monitor_id = :m)"
        " FROM monitor_layouts l WHERE l.start_ms <= :ts"
        " AND l.start_ms >= (SELECT MAX(start_ms) FROM monitor_layouts WHERE start_ms <= :f)",
        params,
    )


def _always(conn: Connection, sql: str, params: dict[str, int]) -> bool:
    """True when the query returns rows and every one is true. No rows means no run in effect."""
    flags = conn.execute(sql, params).fetchall()
    return bool(flags) and all(flag for (flag,) in flags)


# The stored picture of frame `f` shows window `w`, so its crop can be served.
SHOWN_SQL: Final = "f.image_path IS NOT NULL AND w.painted = 1"

# A painted app window of the episode's app (and url, or title for media without a url).
_MATCH: Final = (
    f"w.role = 'app' AND {SHOWN_SQL}"
    " AND lower(w.process) = :process"
    " AND (:url IS NULL OR w.url = :url)"
    " AND (:media = 0 OR :url IS NOT NULL"
    "      OR instr(lower(coalesce(w.title, '')), lower(:title)) > 0)"
)
_IN_SPAN: Final = (
    "SELECT f.id, w.id FROM frame_windows w JOIN frames f ON f.id = w.frame_id"  # noqa: S608 - constant fragment
    f" WHERE f.ts_ms >= :start AND f.ts_ms < :end AND {_MATCH}"
    " ORDER BY f.ts_ms, f.id, w.z LIMIT 1"
)
_BEFORE_START: Final = (
    "SELECT f.id, w.id, f.monitor_id, w.hwnd"  # noqa: S608 - constant fragment
    " FROM frame_windows w JOIN frames f ON f.id = w.frame_id"
    " WHERE f.id IN (SELECT (SELECT id FROM frames WHERE monitor_id = m.id AND ts_ms <= :start"
    "                        ORDER BY ts_ms DESC, id DESC LIMIT 1) FROM monitors m)"
    f" AND {_MATCH} ORDER BY f.ts_ms DESC, f.id, w.z"
)


def episode_evidence(
    conn: Connection, episode_id: int, now_ms: int, process_of: Callable[[str], str]
) -> tuple[int, int] | None:
    """The (frame id, window id) showing the episode: its earliest painted frame, else the
    latest one before it that still holds. ``process_of`` maps the episode's app to a process."""
    episode = conn.execute(
        "SELECT kind, start_ms, end_ms, title, app, url FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    if episode is None:
        return None
    kind, start_ms, end_ms, title, app, url = episode
    if not app:
        return None
    params = {
        "start": start_ms,
        "end": end_ms,
        "process": process_of(app),
        "url": url,
        "title": title,
        "media": int(kind in {"video", "music"}),
    }
    found = conn.execute(_IN_SPAN, params).fetchone()
    if found is not None:
        return int(found[0]), int(found[1])
    for frame_id, window_id, monitor_id, hwnd in conn.execute(_BEFORE_START, params).fetchall():
        if latest_valid_frame(conn, monitor_id, hwnd, start_ms, now_ms) == frame_id:
            return int(frame_id), int(window_id)
    return None


def frame_moment(conn: Connection, frame_id: int) -> tuple[int, int] | None:
    """The (ts_ms, monitor id) of a frame, or None when it is gone."""
    row = conn.execute("SELECT ts_ms, monitor_id FROM frames WHERE id = ?", (frame_id,)).fetchone()
    return None if row is None else (int(row[0]), int(row[1]))


def window_shown(conn: Connection, window_id: int) -> bool:
    """True when the stored picture of the window's frame shows that window."""
    row = conn.execute(
        f"SELECT {SHOWN_SQL}"  # noqa: S608 - constant fragment
        " FROM frame_windows w JOIN frames f ON f.id = w.frame_id WHERE w.id = ?",
        (window_id,),
    ).fetchone()
    return row is not None and bool(row[0])
