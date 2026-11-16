"""The per-monitor capture loop on the real desk: every monitor's composite, at its native size.

Run on the target PC only (it opens one small synthetic window on the right-hand monitor):

    uv run pytest tests/hardware/test_monitor_capture.py -m hardware --no-cov -s

One loop run writes into a disposable encrypted store that is deleted afterwards. Only the
synthetic window's own title is ever looked for; nothing else on the desk is read or printed.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import threading
import time
from typing import TYPE_CHECKING

import pytest
from PIL import Image
from synthetic_window import launch_window, on_right_monitor

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.screen import ScreenCapture
from akashic.capture.screen_writer import ScreenWriter
from akashic.config import Exclusions as UserExclusions
from akashic.config import Paths
from akashic.platform.win.displays import Win32Displays, set_dpi_aware
from akashic.platform.win.events import Win32WindowEvents
from akashic.platform.win.uia import WindowsUia
from akashic.platform.win.wgc import WgcCaptures
from akashic.platform.win.windows import Win32Windows
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.monitors import MonitorLog
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.capture.ports import MonitorInfo, Rect

pytestmark = pytest.mark.hardware

TITLE = "Akashic F17 synthetic window"
BORDER_PX = 16
WAIT_S = 40.0


def tuple_of(rect: Rect) -> tuple[int, int, int, int]:
    return rect.left, rect.top, rect.right, rect.bottom


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[Paths, DataKey, Connection]]:
    paths = Paths(tmp_path / "data")
    paths.ensure()
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    yield paths, key, conn
    conn.close()
    shutil.rmtree(paths.root, ignore_errors=True)  # the disposable store goes with the test


def test_every_monitor_stores_a_composite_at_its_native_size_and_lists_the_synthetic_window(
    store: tuple[Paths, DataKey, Connection],
) -> None:
    paths, key, conn = store
    set_dpi_aware()
    displays = Win32Displays()
    monitors: list[MonitorInfo] | None = displays.monitors()
    assert monitors, "no monitor reported"
    area = on_right_monitor(200, 200, 400, 300)
    process, _ = launch_window(TITLE, area, "plain")
    media = MediaStore(paths.media_dir, key.subkey("media"))
    writer_conn = db.connect(paths.db_file, key.subkey("db"))
    writer = ScreenWriter(FrameStore(writer_conn, media), MonitorLog(writer_conn), OcrGate(None))
    windows = Win32Windows()
    capture = ScreenCapture(
        displays=displays,
        windows=windows,
        facts=windows,
        events=Win32WindowEvents(),
        captures=WgcCaptures(),
        uia=WindowsUia(),
        writer=writer,
        exclusions=Exclusions(UserExclusions(), "http://127.0.0.1:5123"),
        browsers=BrowserState(),
        games=GameLog(conn),
        on_own_address=lambda _own: None,
    )
    stop, paused = threading.Event(), threading.Event()
    threads = [
        threading.Thread(target=writer.run, args=(stop,), name="screen-writer"),
        threading.Thread(target=capture.run, args=(stop, paused, paused), name="screen"),
    ]
    for thread in threads:
        thread.start()
    reader = db.connect(paths.db_file, key.subkey("db"))
    try:
        deadline = time.monotonic() + WAIT_S
        stored: dict[str, str] = {}
        while time.monotonic() < deadline and len(stored) < len(monitors):
            time.sleep(0.5)
            stored = dict(
                reader.execute(
                    "SELECT m.key, f.image_path FROM frames f"
                    " JOIN monitors m ON m.id = f.monitor_id"
                    " WHERE f.image_path IS NOT NULL ORDER BY f.id"
                ).fetchall()
            )
        listed = reader.execute(
            "SELECT m.key, w.x0, w.y0, w.x1, w.y1 FROM frame_windows w"
            " JOIN frames f ON f.id = w.frame_id JOIN monitors m ON m.id = f.monitor_id"
            " WHERE w.title = ? ORDER BY f.id DESC",
            (TITLE,),
        ).fetchall()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
        capture.close()
        process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()
        reader.close()
        writer_conn.close()

    assert set(stored) == {m.key for m in monitors}, "a monitor stored no composite"
    for monitor in monitors:  # negative coordinates and mixed DPI included: physical pixels
        data = media.read(stored[monitor.key])
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (monitor.rect.width, monitor.rect.height), monitor.rect
    right = next(m for m in monitors if m.rect.contains(*area.center))
    assert listed, "the synthetic window was not listed on any monitor"
    key_listed, left, top, right_edge, bottom = listed[0]
    assert key_listed == right.key
    # The frame may differ from the requested client rectangle by the window border.
    for got, wanted in zip((left, top, right_edge, bottom), tuple_of(area), strict=True):
        assert abs(got - wanted) <= BORDER_PX, (got, wanted)
