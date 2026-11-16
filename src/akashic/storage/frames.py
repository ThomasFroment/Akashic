"""Screen frames: one row per capture of a monitor, with one row per window it shows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from akashic.storage import db
from akashic.storage.jobs import NewJob, enqueue_in

if TYPE_CHECKING:
    from collections.abc import Sequence
    from sqlite3 import Connection

    from akashic.storage.media import MediaStore

log = logging.getLogger(__name__)

type TextSource = Literal["uia", "ocr", "ocr_deferred"]
type WindowRole = Literal["app", "hidden", "shell"]

OCR_WINDOW = "ocr_window"


@dataclass(frozen=True, slots=True)
class NewWindow:
    """One window of a frame. Only app rows carry content; hidden and shell rows a rectangle."""

    role: WindowRole
    z: int  # 0 is the front
    hwnd: int | None  # required on app rows, forbidden on hidden rows
    left: int
    top: int
    right: int
    bottom: int
    process: str | None = None
    title: str | None = None
    url: str | None = None
    focused: bool = False
    painted: bool = False  # the stored image shows this window
    text: str | None = None
    text_source: TextSource | None = None
    capture_webp: bytes | None = None  # whole-window image for deferred OCR; needs text None


@dataclass(frozen=True, slots=True)
class NewFrame:
    ts_ms: int  # UTC milliseconds, the moment of capture
    monitor_id: int
    trigger: str
    image_webp: bytes | None = None  # None: no screenshot (e.g. DRM black frame)
    windows: tuple[NewWindow, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredWindow:
    id: int
    role: WindowRole
    z: int
    hwnd: int | None
    left: int
    top: int
    right: int
    bottom: int
    process: str | None
    title: str | None
    url: str | None
    focused: bool
    painted: bool
    text: str | None
    text_source: TextSource | None


@dataclass(frozen=True, slots=True)
class StoredFrame:
    id: int
    ts_ms: int
    monitor_id: int
    trigger: str
    image_path: str | None
    windows: tuple[StoredWindow, ...]  # front to back


_FRAME_COLUMNS = "id, ts_ms, monitor_id, trigger, image_path"
_WINDOW_COLUMNS = (
    "id, role, z, hwnd, x0, y0, x1, y1, process, title, url, focused, painted, text, text_source"
)


def image_rel_path(ts_ms: int, monitor_id: int) -> str:
    """``YYYY/MM/DD/<ts_ms>_m<monitor_id>.webp``: folder by local date, name by the UTC instant,
    so two captures an hour apart at a clock change never share a name."""
    local = datetime.fromtimestamp(ts_ms / 1000, UTC).astimezone()
    return f"{local:%Y/%m/%d}/{ts_ms}_m{monitor_id}.webp"


def capture_rel_path(ts_ms: int, monitor_id: int, z: int) -> str:
    """The window capture next to its frame image: ``<ts_ms>_m<monitor_id>_z<z>.webp``."""
    return image_rel_path(ts_ms, monitor_id).removesuffix(".webp") + f"_z{z}.webp"


def _stored_window(row: Sequence[Any]) -> StoredWindow:
    return StoredWindow(
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        row[6],
        row[7],
        row[8],
        row[9],
        row[10],
        bool(row[11]),
        bool(row[12]),
        row[13],
        row[14],
    )


class FrameStore:
    def __init__(self, conn: Connection, media: MediaStore) -> None:
        self._conn = conn
        self._media = media

    @property
    def media(self) -> MediaStore:
        """Where the screenshots live (retention and Settings delete files through it)."""
        return self._media

    def add(self, frame: NewFrame) -> int:
        """Write the image and the window captures, then the rows and one ``ocr_window`` job per
        capture. If anything fails every file written is removed again."""
        written: list[str] = []
        try:
            rel_path: str | None = None
            if frame.image_webp is not None:
                rel_path = self._media.write_unique(
                    image_rel_path(frame.ts_ms, frame.monitor_id), frame.image_webp
                )
                written.append(rel_path)
            capture_paths: list[str | None] = []
            for w in frame.windows:
                capture_path = None
                if w.capture_webp is not None:
                    capture_path = self._media.write_unique(
                        capture_rel_path(frame.ts_ms, frame.monitor_id, w.z), w.capture_webp
                    )
                    written.append(capture_path)
                capture_paths.append(capture_path)
            with db.transaction(self._conn) as conn:
                row_id: int = conn.execute(
                    "INSERT INTO frames (ts_ms, monitor_id, trigger, image_path)"
                    " VALUES (?, ?, ?, ?) RETURNING id",
                    (frame.ts_ms, frame.monitor_id, frame.trigger, rel_path),
                ).fetchone()[0]
                for w, capture_path in zip(frame.windows, capture_paths, strict=True):
                    window_id: int = conn.execute(
                        "INSERT INTO frame_windows (frame_id, role, z, hwnd, x0, y0, x1, y1,"
                        " process, title, url, focused, painted, text, text_source, capture_path)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                        (
                            row_id,
                            w.role,
                            w.z,
                            w.hwnd,
                            w.left,
                            w.top,
                            w.right,
                            w.bottom,
                            w.process,
                            w.title,
                            w.url,
                            w.focused,
                            w.painted,
                            w.text,
                            w.text_source,
                            capture_path,
                        ),
                    ).fetchone()[0]
                    if capture_path is not None:
                        enqueue_in(
                            conn,
                            NewJob(
                                OCR_WINDOW,
                                f"{OCR_WINDOW}:{window_id}",
                                frame.ts_ms,
                                {"window_id": window_id},
                            ),
                            frame.ts_ms,
                        )
        except BaseException:
            for path in written:
                self._media.delete(path)
            raise
        return row_id

    def get(self, frame_id: int) -> StoredFrame | None:
        row = self._conn.execute(
            f"SELECT {_FRAME_COLUMNS} FROM frames WHERE id = ?",  # noqa: S608 - constant columns
            (frame_id,),
        ).fetchone()
        if row is None:
            return None
        windows = self._conn.execute(
            f"SELECT {_WINDOW_COLUMNS} FROM frame_windows WHERE frame_id = ? ORDER BY z",  # noqa: S608
            (frame_id,),
        ).fetchall()
        return StoredFrame(*row, windows=tuple(_stored_window(w) for w in windows))

    def window_capture(self, window_id: int) -> bytes | None:
        """The decrypted capture of a window; None when it has none."""
        path = self._capture_path(window_id)
        return None if path is None else self._media.read(path)

    def finish_capture(self, window_id: int, text: str | None) -> None:
        """Delete the capture of a window and store the text read from it (None: no text)."""
        with db.transaction(self._conn) as conn:
            path = self._capture_path(window_id)
            if path is None:
                return
            conn.execute(
                "UPDATE frame_windows SET capture_path = NULL, text = ?, text_source = ?"
                " WHERE id = ?",
                (text, None if text is None else "ocr_deferred", window_id),
            )
            self._media.delete(path)

    def _capture_path(self, window_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT capture_path FROM frame_windows WHERE id = ?", (window_id,)
        ).fetchone()
        return row[0] if row else None

    def image_path(self, frame_id: int) -> str | None:
        row = self._conn.execute(
            "SELECT image_path FROM frames WHERE id = ?", (frame_id,)
        ).fetchone()
        return row[0] if row else None

    def image(self, image_path: str) -> bytes:
        return self._media.read(image_path)
