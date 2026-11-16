"""Reads of stored rows for assertions, kept out of the production stores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from akashic.storage.frames import StoredWindow, _stored_window

if TYPE_CHECKING:
    from sqlite3 import Connection


@dataclass(frozen=True, slots=True)
class FrameView:
    """A stored frame flattened over its first window (the old loop stores exactly one)."""

    id: int
    ts_ms: int
    monitor_id: int
    trigger: str
    image_path: str | None
    windows: tuple[StoredWindow, ...]

    def _first(self) -> StoredWindow | None:
        return self.windows[0] if self.windows else None

    @property
    def process(self) -> str | None:
        return w.process if (w := self._first()) else None

    @property
    def window_title(self) -> str | None:
        return w.title if (w := self._first()) else None

    @property
    def url(self) -> str | None:
        return w.url if (w := self._first()) else None

    @property
    def text(self) -> str | None:
        return w.text if (w := self._first()) else None

    @property
    def text_source(self) -> str | None:
        return w.text_source if (w := self._first()) else None


def latest_frames(conn: Connection, limit: int = 20) -> list[FrameView]:
    out: list[FrameView] = []
    for row in conn.execute(
        "SELECT id, ts_ms, monitor_id, trigger, image_path FROM frames"
        " ORDER BY ts_ms DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall():
        windows = conn.execute(
            "SELECT id, role, z, hwnd, x0, y0, x1, y1, process, title, url, focused, painted,"
            " text, text_source FROM frame_windows WHERE frame_id = ? ORDER BY z",
            (row[0],),
        ).fetchall()
        out.append(FrameView(*row, windows=tuple(_stored_window(w) for w in windows)))
    return out


def embedding_count(conn: Connection, owner_kind: str, model: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM embeddings WHERE owner_kind = ? AND model = ?", (owner_kind, model)
    ).fetchone()
    return int(row[0])
