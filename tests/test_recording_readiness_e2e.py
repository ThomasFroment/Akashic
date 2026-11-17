"""Cross-feature flows of the recording-readiness epic, through real stores and handlers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from ask_fakes import FakeChat, ask, make
from capture_fakes import window
from export_world import HIGH, LOW, PASSPHRASE, World, open_export, run_export
from memory_data import MINUTE
from test_media_tracker import Rig, paused, playing
from test_media_tracker import make_rig as make_rig  # noqa: PLC0414 - re-exported fixture
from test_processing_status import HOUR, Light, Stub, heartbeat
from test_processing_status import Rig as JobRig

from akashic.capture.media import PAUSE_CLOSE_MS
from akashic.search import keyword
from akashic.storage.debuginfo import TRAY_CAPTURE_RUNS, processing_attention, processing_status
from akashic.storage.export import ExportScope
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from sqlite3 import Connection

pytestmark = pytest.mark.usefixtures("fast_keywrap")

TEN_MIN = 10 * 60_000


def test_a_video_through_a_pause_a_lock_and_a_browser_starting_locked_keeps_one_row(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig()
    rig.source.states = [playing()]
    rig.at(0)
    rig.at(10)
    rig.source.states = [paused()]
    for second in range(12, 12 + 5 * 60 + 1, 2):
        rig.at(second)
    assert PAUSE_CLOSE_MS > 5 * 60 * 1000
    assert rig.rows()[0][4] == 12_000  # still open, ending where the pause began
    rig.source.states = [playing()]
    resume = 12 + 5 * 60 + 2
    rig.at(resume)
    rig.focus[0] = None  # a UAC prompt takes the focus
    for second in range(resume + 2, resume + 120, 2):
        if second == resume + 20:  # a browser starts playing while locked
            rig.source.states = [playing(), playing("Other", app_id="C1")]
        rig.at(second, locked=True)
    assert [(r[1], r[6]) for r in rig.rows()] == [("Video A", 0)]
    unlock = resume + 120
    rig.focus_browser(window(process="chrome.exe", title="Other - YouTube - Google Chrome"))
    rig.at(unlock)
    rig.tracker.close_all()
    rows = rig.rows()
    assert [(r[1], r[3]) for r in rows] == [("Video A", 0), ("Other", unlock * 1000)]
    assert rows[0][4] == unlock * 1000  # carried on through the lock, never split


def test_a_search_and_an_ask_on_an_export_see_only_what_was_exported(
    world: World, tmp_path: Path
) -> None:
    run_export(world, tmp_path / "out", ExportScope(LOW, HIGH))
    with open_export(tmp_path / "out", PASSPHRASE) as (conn, _key):
        assert len(keyword.search(conn, "quokkainside")) == 1
        assert keyword.search(conn, "quokkabefore") == []
        plan = json.dumps(
            {
                "time_text": "",
                "people": [],
                "kinds": ["video"],
                "keywords": ["quokkainside"],
                "semantic_query": "",
            }
        )
        handler, chat, _ = make(conn, FakeChat(("It was an In page [1].",), plan=plan))
        job = ask(conn, "what was on quokkainside?")
        handler.run(job)
        answer, state = conn.execute(
            "SELECT answer, state FROM questions WHERE id = ?", (job.payload["question_id"],)
        ).fetchone()
        assert state == "done"
        assert answer == "It was an In page [1]."
        assert "quokkainside" in chat.prompts[0][1]
        assert "quokkabefore" not in chat.prompts[0][1]


def test_a_game_or_a_pause_never_needs_attention_but_an_hour_without_progress_does(
    conn: Connection,
) -> None:
    rig = JobRig(conn, Stub(raises=ValueError()), Light())
    rig.add()
    states = {"capture": "ready", "worker": "ready"}
    game = GameLog(conn)

    def verdict() -> str | None:
        heartbeat(conn, rig.runner, rig.queue).beat("ready", "idle", rig.clock[0], 100.0)
        return processing_attention(states, processing_status(conn, "ready"), TRAY_CAPTURE_RUNS)

    session = game.open(process="game.exe", window_title="G", ts_ms=rig.clock[0])
    for _ in range(120):  # two hours in a game
        rig.clock[0] += MINUTE
        game.heartbeat(session, rig.clock[0])
        rig.advance(0)
    assert verdict() is None
    game.close(session, rig.clock[0])
    for _ in range(12):  # two hours of Pause: only light work may run
        rig.advance(TEN_MIN, only=["light"])
    assert verdict() is None
    for _ in range(7):
        rig.advance(TEN_MIN)
    assert rig.stalled >= HOUR
    assert verdict() == "needs_attention"
