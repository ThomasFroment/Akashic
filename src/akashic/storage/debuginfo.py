"""Each process's self-description for the Debug page (table ``process_info``).

A process calls :meth:`ProcessInfo.publish` every few seconds with plain numbers, states and
error type names. Never put captured content here: it is shown in the UI without being stored
encrypted any differently from the rest of the database, but the rule is the same as for logs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from akashic.jsonish import as_dict
from akashic.storage import db

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping
    from sqlite3 import Connection

# Transcription that made no progress for this long, while it could run, needs the user's eye.
TRANSCRIBE_STALL_MS: Final = 3_600_000
# Capture states in which processing problems matter; the sidebar also shows them while the UI
# has capture paused.
TRAY_CAPTURE_RUNS: Final = frozenset({"ready", "gaming", "degraded"})
SIDEBAR_CAPTURE_RUNS: Final = TRAY_CAPTURE_RUNS | {"ui_paused"}


@dataclass(frozen=True, slots=True)
class Info:
    name: str
    updated_ms: int
    data: dict[str, object]


class ProcessInfo:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def publish(self, name: str, data: dict[str, object], now_ms: int) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO process_info (name, updated_ms, data) VALUES (?, ?, ?)"
                " ON CONFLICT (name) DO UPDATE SET updated_ms = excluded.updated_ms,"
                " data = excluded.data",
                (name, now_ms, json.dumps(data, separators=(",", ":"))),
            )

    def read(self, name: str) -> dict[str, object] | None:
        row: tuple[str] | None = self._conn.execute(
            "SELECT data FROM process_info WHERE name = ?", (name,)
        ).fetchone()
        return as_dict(json.loads(row[0])) if row is not None else None

    def read_all(self) -> list[Info]:
        rows: list[tuple[str, int, str]] = self._conn.execute(
            "SELECT name, updated_ms, data FROM process_info ORDER BY name"
        ).fetchall()
        return [Info(n, u, as_dict(json.loads(d)) or {}) for n, u, d in rows]


@dataclass(frozen=True, slots=True)
class Processing:
    state: str  # the supervisor's live worker state
    blocked: list[str]  # kinds that cannot run and have jobs waiting
    failed: dict[str, int]
    oldest_pending_ms: dict[str, int]
    horizon_ms: int
    transcribe_stalled_ms: int


def _int_map(value: object) -> dict[str, int] | None:
    items = as_dict(value)
    if items is None or not all(isinstance(v, int) for v in items.values()):
        return None
    return cast("dict[str, int]", items)


def processing_status(conn: Connection, worker: str) -> Processing | None:
    """What the worker published about its queue, or None when the row is missing or unusable."""
    try:
        info = ProcessInfo(conn).read("worker")
    except ValueError:
        return None
    if info is None:
        return None
    blocked = as_dict(info.get("blocked"))
    failed = _int_map(info.get("failed"))
    oldest_pending_ms = _int_map(info.get("oldest_pending_ms"))
    horizon = info.get("horizon_ms")
    stalled = info.get("transcribe_stalled_ms")
    if (
        blocked is None
        or failed is None
        or oldest_pending_ms is None
        or not isinstance(horizon, int)
        or not isinstance(stalled, int)
    ):
        return None
    return Processing(
        worker,
        sorted(kind for kind in blocked if kind in oldest_pending_ms),
        failed,
        oldest_pending_ms,
        horizon,
        stalled,
    )


def processing_attention(
    states: Mapping[str, str], processing: Processing | None, capture_runs: Collection[str]
) -> str | None:
    """The processing verdict while capture is in one of ``capture_runs``: ``processing_stopped``
    (the worker is down), ``needs_attention`` (a kind is blocked or transcription stalled), or
    None."""
    if states.get("capture", "stopped") not in capture_runs:
        return None
    if states.get("worker", "stopped") in {"crashed", "stopped"}:
        return "processing_stopped"
    if processing is not None and (
        processing.blocked or processing.transcribe_stalled_ms >= TRANSCRIBE_STALL_MS
    ):
        return "needs_attention"
    return None
