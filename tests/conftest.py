"""Shared test fixtures."""

from __future__ import annotations

import logging
import sys
import time
from typing import TYPE_CHECKING, Final

import pytest
from capture_fakes import (
    LONG_TEXT,
    SCREEN,
    FakeOcr,
    FakeScreen,
    FakeUia,
    FakeWindows,
    window,
)
from screen_rig import Rig
from sink_spy import close_rig, make_rig_factory

from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.screen import ScreenCapture, Settings
from akashic.config import Exclusions as UserExclusions
from akashic.config import Paths
from akashic.storage import db
from akashic.storage.crypto import Argon2Params, DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from sink_spy import PrivacyRig

    from akashic.capture.ports import Rect
    from akashic.capture.uia_budget import UiaBudget

# Cheap parameters for tests only; production uses crypto.DEFAULT_ARGON2.
FAST_ARGON2: Final = Argon2Params(time_cost=1, memory_kib=8 * 1024, parallelism=1)


@pytest.fixture(autouse=True)
def _isolated_logging() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction] - autouse
    """setup_logging() replaces root handlers and sys.excepthook; undo that after each test."""
    root = logging.getLogger()
    handlers, level, hook = list(root.handlers), root.level, sys.excepthook
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    for h in handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)
    sys.excepthook = hook


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(tmp_path / "data")
    p.ensure()
    return p


@pytest.fixture(autouse=True)
def _no_real_capture(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction] - autouse
    """Spawned capture processes inherit this and stay idle instead of recording the screen."""
    monkeypatch.setenv("AKASHIC_CAPTURE", "0")


@pytest.fixture
def key() -> DataKey:
    return DataKey.generate()


@pytest.fixture
def conn(paths: Paths, key: DataKey) -> Iterator[Connection]:
    """A migrated, empty database (test files that need something else define their own)."""
    c = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def media(paths: Paths, key: DataKey) -> MediaStore:
    return MediaStore(paths.media_dir, key.subkey("media"))


@pytest.fixture
def privacy_rig(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., PrivacyRig]]:
    """A capture rig over disposable stores watched by the writer spy (see sink_spy)."""
    created: list[PrivacyRig] = []
    yield make_rig_factory(paths, monkeypatch, created)
    for rig in created:
        close_rig(rig)


@pytest.fixture
def make_rig(paths: Paths) -> Iterator[Callable[..., Rig]]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    store = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))

    def build(
        *,
        monitors: list[Rect] | None = None,
        user: UserExclusions | None = None,
        settings: Settings | None = None,
        budget: UiaBudget | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Rig:
        screen = FakeScreen(rects=monitors or [SCREEN])
        windows = FakeWindows(focused=window())
        uia = FakeUia(texts={1: LONG_TEXT})
        ocr = FakeOcr()
        counter = iter(range(1_000_000, 10**9))
        capture = ScreenCapture(
            screen=screen,
            windows=windows,
            uia=uia,
            gate=OcrGate(ocr),
            store=store,
            exclusions=Exclusions(user or UserExclusions()),
            settings=settings or Settings(min_gap_s=0.2),  # tests step in 0.1 s
            budget=budget,
            clock=clock,
            now_ms=lambda: next(counter),
            browsers=BrowserState(),
            games=GameLog(conn),
        )
        return Rig(capture, screen, windows, uia, ocr, store, conn)

    yield build
    conn.close()
