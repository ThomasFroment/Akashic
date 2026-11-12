"""Screen frames: one row per capture, screenshot and text sharing one timestamp."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from akashic.pipeline.jobs import NewJob, enqueue_in
from akashic.storage import db

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.storage.media import MediaStore

log = logging.getLogger(__name__)

type TextSource = Literal["uia", "ocr"]

OCR_FRAME = "ocr_frame"


@dataclass(frozen=True, slots=True)
class NewFrame:
    ts_ms: int  # UTC milliseconds, the moment of capture
    monitor: int
    trigger: str
    process: str | None = None
    window_title: str | None = None
    url: str | None = None
    text: str | None = None
    text_source: TextSource | None = None
    image_webp: bytes | None = None  # None: no screenshot (e.g. DRM black frame)
    focused: bool = True  # False: a window on another monitor that did not have focus


@dataclass(frozen=True, slots=True)
class StoredFrame:
    id: int
    ts_ms: int
    monitor: int
    trigger: str
    process: str | None
    window_title: str | None
    url: str | None
    text: str | None
    text_source: str | None
    image_path: str | None


_COLUMNS = "id, ts_ms, monitor, trigger, process, window_title, url, text, text_source, image_path"


def image_rel_path(ts_ms: int, monitor: int) -> str:
    """``YYYY/MM/DD/<ts_ms>_m<monitor>.webp``: folder by local date, name by the UTC instant, so
    two captures an hour apart at a clock change never share a name."""
    local = datetime.fromtimestamp(ts_ms / 1000, UTC).astimezone()
    return f"{local:%Y/%m/%d}/{ts_ms}_m{monitor}.webp"


class FrameStore:
    def __init__(self, conn: Connection, media: MediaStore) -> None:
        self._conn = conn
        self._media = media

    @property
    def media(self) -> MediaStore:
        """Where the screenshots live (retention and Settings delete files through it)."""
        return self._media

    def add(self, frame: NewFrame) -> int:
        """Write the image, then the row. If the row fails the image is removed again."""
        rel_path: str | None = None
        if frame.image_webp is not None:
            rel_path = image_rel_path(frame.ts_ms, frame.monitor)
            rel_path = self._media.write_unique(rel_path, frame.image_webp)
        try:
            with db.transaction(self._conn) as conn:
                cursor = conn.execute(
                    "INSERT INTO frames (ts_ms, monitor, process, window_title, url, text,"
                    " text_source, trigger, image_path, focused)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        frame.ts_ms,
                        frame.monitor,
                        frame.process,
                        frame.window_title,
                        frame.url,
                        frame.text,
                        frame.text_source,
                        frame.trigger,
                        rel_path,
                        int(frame.focused),
                    ),
                )
                row_id = cursor.lastrowid
                if row_id is not None and frame.trigger == "game" and rel_path and not frame.text:
                    # Gaming mode reads no text while the game runs; the worker does it later.
                    enqueue_in(
                        conn,
                        NewJob(
                            OCR_FRAME, f"{OCR_FRAME}:{row_id}", frame.ts_ms, {"frame_id": row_id}
                        ),
                        frame.ts_ms,
                    )
        except BaseException:
            if rel_path is not None:
                self._media.delete(rel_path)
            raise
        if row_id is None:  # pragma: no cover - INSERT always sets it
            msg = "insert returned no row id"
            raise RuntimeError(msg)
        return row_id

    def get(self, frame_id: int) -> StoredFrame | None:
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM frames WHERE id = ?",  # noqa: S608 - constant column list
            (frame_id,),
        ).fetchone()
        return StoredFrame(*row) if row is not None else None

    def set_text(self, frame_id: int, text: str, source: TextSource) -> None:
        with db.transaction(self._conn) as conn:
            conn.execute(
                "UPDATE frames SET text = ?, text_source = ? WHERE id = ?", (text, source, frame_id)
            )

    def image(self, frame: StoredFrame) -> bytes | None:
        return self._media.read(frame.image_path) if frame.image_path else None
