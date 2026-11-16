"""A writer spy for the privacy tests, and a rig that wires the capture side to disposable stores.

``SinkSpy`` observes every place captured data can go, so a test can prove that something
forbidden never reached ANY of them, not merely that it is absent afterwards:

* every SQL statement that writes (``set_trace_callback`` sees the statement text with its bound
  values, including writes that are rolled back or deleted again later),
* every encrypted file written through ``MediaStore.write``/``write_unique`` (screenshots, audio),
  by path,
* every log record, and the files the logging setup would create,
* the final content of every table, scanned for the needles.

Synthetic content only: tests plant ``SECRET`` in titles, URLs, channels and text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from capture_fakes import FakeMedia
from screen_rig import build_rig as build_screen_rig

from akashic.capture.exclusions import Exclusions
from akashic.capture.media import MediaTracker
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.media import MediaStore
from akashic.storage.sessions import MediaLog

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path
    from sqlite3 import Connection

    import pytest
    from capture_fakes import FakeUia, FakeWindows
    from screen_rig import Rig

    from akashic.capture.browser_state import BrowserState
    from akashic.capture.ports import Rect, TextBlock, WindowInfo
    from akashic.capture.screen import ScreenCapture
    from akashic.config import Paths

SECRET = "zzsentinel4f2a"  # noqa: S105 - a synthetic marker, not a credential
T0 = 1_700_000_000_000
_WRITE = re.compile(r"^\s*(INSERT|UPDATE|REPLACE|DELETE)\b", re.IGNORECASE)


class _Capture(logging.Handler):
    def __init__(self, sink: list[str]) -> None:
        super().__init__(logging.DEBUG)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(record.getMessage())


class SinkSpy:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.statements: list[str] = []
        self.files: list[str] = []
        self.logs: list[str] = []
        self._handler = _Capture(self.logs)
        self._level: int | None = None

    # --- wiring ------------------------------------------------------------------------------

    def watch(self, conn: Connection) -> None:
        conn.set_trace_callback(self.statements.append)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = MediaStore.write_unique
        spy = self

        def store_file(store: MediaStore, rel_path: str, data: bytes) -> str:
            spy.files.append(rel_path)
            return real(store, rel_path, data)

        monkeypatch.setattr(MediaStore, "write_unique", store_file)
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self._handler)

    def close(self) -> None:
        logging.getLogger().removeHandler(self._handler)
        if self._level is not None:
            logging.getLogger().setLevel(self._level)

    # --- observations ------------------------------------------------------------------------

    def writes(self) -> list[str]:
        """Statements that wrote (or tried to write) rows, in order."""
        return [s for s in self.statements if _WRITE.match(s)]

    def disk_files(self) -> list[Path]:
        return [p for p in self.paths.media_dir.rglob("*") if p.is_file()]

    def table_text(self, conn: Connection) -> str:
        names = [
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if not r[0].startswith("sqlite_")
        ]
        parts: list[str] = []
        for name in names:
            try:
                rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()  # noqa: S608 - own names
            except Exception:  # noqa: BLE001, S112 - a virtual table that cannot be scanned
                continue
            parts.extend(repr(row) for row in rows)
        return "\n".join(parts)

    def leaks(self, conn: Connection, *needles: str) -> list[str]:
        """Which sinks contain any needle (empty list: nothing leaked anywhere)."""
        sinks = {
            "sql": "\n".join(self.writes()),
            "files": "\n".join(self.files),
            "logs": "\n".join(self.logs),
            "tables": self.table_text(conn),
        }
        return [
            name
            for name, text in sinks.items()
            if any(n.casefold() in text.casefold() for n in needles)
        ]

    def nothing_stored(self, conn: Connection) -> bool:
        """No row-writing statement for captured content, no file written, nothing on disk."""
        content = (
            "frames",
            "frame_windows",
            "media_sessions",
            "media_events",
            "audio_segments",
            "game_sessions",
        )
        wrote = [s for s in self.writes() if any(t in s.casefold() for t in content)]
        return not wrote and not self.files and not self.disk_files() and self._empty(conn, content)

    @staticmethod
    def _empty(conn: Connection, tables: tuple[str, ...]) -> bool:
        return all(
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0  # noqa: S608 - constants
            for t in tables
        )


@dataclass
class PrivacyRig:
    """Screen loop + screen writer + media tracker over disposable encrypted stores."""

    spy: SinkSpy
    conn: Connection
    media_conn: Connection
    rig: Rig
    media: FakeMedia
    tracker: MediaTracker

    @property
    def capture(self) -> ScreenCapture:
        return self.rig.capture

    @property
    def windows(self) -> FakeWindows:
        return self.rig.windows

    @property
    def uia(self) -> FakeUia:
        return self.rig.uia

    @property
    def browsers(self) -> BrowserState:
        return self.rig.browsers

    @property
    def clock(self) -> list[float]:
        return self.rig.clock

    def show(self, info: WindowInfo, **kwargs: object) -> None:
        """Put a window in front and focus it (a title change when the hwnd is already there)."""
        self.rig.show(
            info.hwnd,
            info.process,
            info.title,
            info.rect,
            has_frame=info.has_frame,
            **kwargs,  # type: ignore[arg-type]
        )

    def tick(self, advance_s: float = 1.0) -> None:
        """Check the monitors (a second pass paints started captures) and store what was queued."""
        self.clock[0] += advance_s
        self.rig.tick(self.clock[0])
        self.clock[0] += 0.1
        self.rig.tick(self.clock[0])
        self.rig.drain()

    def poll(self, advance_s: float = 2.0) -> None:
        self.clock[0] += advance_s
        self.tracker.poll()

    def frames(self) -> int:
        return self.conn.execute("SELECT count(*) FROM frames").fetchone()[0]

    def sessions(self) -> int:
        return self.media_conn.execute("SELECT count(*) FROM media_sessions").fetchone()[0]

    def leaks(self, *needles: str) -> list[str]:
        return self.spy.leaks(self.conn, *needles)


def build_rig(  # noqa: PLR0913 - one switch per scenario input
    paths: Paths,
    monkeypatch: pytest.MonkeyPatch,
    *,
    user: UserExclusions | None = None,
    api_origin: str | None = "http://127.0.0.1:5123",
    monitors: list[Rect] | None = None,
    focused: WindowInfo | None = None,
    texts: dict[int, Sequence[TextBlock | str] | None] | None = None,
) -> PrivacyRig:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    media_conn = db.connect(paths.db_file, key.subkey("db"))
    spy = SinkSpy(paths)
    spy.watch(conn)
    spy.watch(media_conn)
    spy.install(monkeypatch)
    rig = build_screen_rig(
        conn,
        MediaStore(paths.media_dir, key.subkey("media")),
        monitors=monitors,
        user=user,
        api_origin=api_origin,
        shown=False,
        ts_start=T0,
    )
    rig.clock[0] = 1_000.0
    if texts is not None:
        rig.uia.texts = dict(texts)
    media = FakeMedia(names={"Chrome": "Google Chrome", "Z1": "Zen"})
    tracker = MediaTracker(
        source=media,
        log_store=MediaLog(media_conn),
        exclusions=Exclusions(user or UserExclusions(), api_origin),
        focused_window=rig.windows.focused_window,
        now_ms=lambda: int(rig.clock[0] * 1000),
        browsers=rig.browsers,
    )
    private = PrivacyRig(spy, conn, media_conn, rig, media, tracker)
    if focused is not None:
        private.show(focused)
    return private


def close_rig(rig: PrivacyRig) -> None:
    rig.spy.close()
    rig.conn.close()
    rig.media_conn.close()


def make_rig_factory(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, created: list[PrivacyRig]
) -> Callable[..., PrivacyRig]:
    def make(**kwargs: object) -> PrivacyRig:
        rig = build_rig(paths, monkeypatch, **kwargs)  # type: ignore[arg-type]
        created.append(rig)
        return rig

    return make
