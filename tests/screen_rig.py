"""The screen-capture rig shared by the screen tests and the make_rig fixture."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from store_reads import latest_frames

from akashic.capture.ports import Rect

if TYPE_CHECKING:
    from sqlite3 import Connection

    from capture_fakes import FakeOcr, FakeScreen, FakeUia, FakeWindows

    from akashic.capture.screen import ScreenCapture
    from akashic.storage.frames import FrameStore

MON_B = Rect(800, 0, 1600, 600)


@dataclass
class Rig:
    capture: ScreenCapture
    screen: FakeScreen
    windows: FakeWindows
    uia: FakeUia
    ocr: FakeOcr
    store: FrameStore
    conn: Connection
    clock: list[float] = field(default_factory=lambda: [0.0])

    def frames(self) -> list[tuple[str, int, str | None]]:
        rows = latest_frames(self.conn, 100)
        return [(f.trigger, f.monitor, f.text_source) for f in reversed(rows)]
