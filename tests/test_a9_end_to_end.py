"""A9 end to end: the scenarios that cross features, with fakes for the platform only.

Each test drives the real screen loop, verdicts, desk, composer, ScreenWriter, stores and API
(and the media tracker, the capture child, the episode builder and the ask job where the
scenario names them) over disposable encrypted stores, with the writer spy watching every SQL
write, file and log record. Hardware and real browsers are out of scope (tests/hardware,
tests/manual/CHECKLIST.md).
"""

from __future__ import annotations

import http.client
import io
import json
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np
import pytest
from ask_fakes import FakeChat, FlushClock, ask
from capture_fakes import FakeOcr, monitor, window, with_text_block
from memory_data import NOW
from oracles import HashEmbedder
from PIL import Image
from sink_spy import SECRET
from store_reads import latest_frames
from test_delete_all import assert_empty
from test_ui_presence import Capture, frame_count
from waits import wait_for

from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.capture.exclusions import Exclusions
from akashic.capture.gaming import GameSettings
from akashic.capture.health import SCREEN_FRAMES_DROPPED
from akashic.capture.media import MediaTracker
from akashic.capture.ports import MediaState, Rect
from akashic.config import Exclusions as UserExclusions
from akashic.pipeline.ask import AskHandler
from akashic.pipeline.embed import EMBED, EmbedHandler, enqueue_missing
from akashic.pipeline.episodes import build_episodes
from akashic.pipeline.ocr_windows import OcrWindowHandler
from akashic.search import keyword
from akashic.search.timeparse import resolve
from akashic.storage import db
from akashic.storage.gaps import GapLog
from akashic.storage.jobs import Job, JobQueue
from akashic.storage.retention import apply_retention, delete_all_recorded_data
from akashic.storage.sessions import MediaLog

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from sqlite3 import Connection

    from screen_rig import Rig
    from sink_spy import PrivacyRig

    from akashic.capture.ports import WindowInfo
    from akashic.config import Paths
    from akashic.storage.crypto import DataKey
    from akashic.storage.frames import FrameStore
    from akashic.storage.media import MediaStore

type Colour = tuple[int, int, int]
type Json = dict[str, object]

WHITE: Colour = (255, 255, 255)
BLACK: Colour = (0, 0, 0)
RED: Colour = (220, 40, 40)
GREEN: Colour = (40, 200, 60)
BLUE: Colour = (40, 60, 220)
YELLOW: Colour = (230, 230, 30)
MAGENTA: Colour = (220, 40, 220)
VIDEO_URL = "https://www.youtube.com/watch?v=abc"
BANK_URL = "https://www.paypal.com/myaccount"
NEEDLES = (SECRET, "Private Browsing", "Incognito", "paypal")


# --- helpers ---------------------------------------------------------------------------------


def solid(colour: Colour, width: int = 800, height: int = 600) -> Image.Image:
    return Image.new("RGB", (width, height), colour)


def row(n: int) -> list[Rect]:
    return [Rect(800 * i, 0, 800 * (i + 1), 600) for i in range(n)]


def pixel(image: Image.Image, x: int, y: int) -> Colour:
    value = image.getpixel((x, y))
    assert isinstance(value, tuple)
    return (value[0], value[1], value[2])


def near(found: Colour, wanted: Colour, tolerance: int = 45) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(found, wanted, strict=True))


def has_colour(image: Image.Image, wanted: Colour, tolerance: int = 45) -> bool:
    pixels = np.asarray(image.convert("RGB")).astype(int)
    return bool((np.abs(pixels - np.array(wanted)) <= tolerance).all(axis=2).any())


def walk_to(rig: PrivacyRig, until_s: float) -> None:
    """Poll media every 2 s up to ``until_s``: a longer gap would close the row as a suspend."""
    while rig.clock[0] < until_s:
        rig.poll(min(2.0, until_s - rig.clock[0]))


def chrome(title: str = "Video - YouTube - Google Chrome", hwnd: int = 1) -> WindowInfo:
    return window(hwnd=hwnd, process="chrome.exe", title=title, rect=Rect(0, 0, 800, 600))


def stored_image(rig: Rig, frame_id: int) -> Image.Image | None:
    path = rig.conn.execute("SELECT image_path FROM frames WHERE id = ?", (frame_id,)).fetchone()[0]
    if path is None:
        return None
    return Image.open(io.BytesIO(rig.store.image(path))).convert("RGB")


def frame_ids(rig: Rig, key: str) -> list[int]:
    return [
        int(r[0])
        for r in rig.conn.execute(
            "SELECT f.id FROM frames f JOIN monitors m ON m.id = f.monitor_id"
            " WHERE m.key = ? ORDER BY f.ts_ms, f.id",
            (key,),
        )
    ]


def newest_image(rig: Rig, key: str) -> Image.Image:
    image = stored_image(rig, frame_ids(rig, key)[-1])
    assert image is not None
    return image


def processes_stored(conn: Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT DISTINCT process FROM frame_windows")} - {
        "None"
    }


def states_of(conn: Connection, key: str) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT s.state FROM monitor_states s JOIN monitors m ON m.id = s.monitor_id"
            " WHERE m.key = ? ORDER BY s.id",
            (key,),
        )
    ]


class Api:
    def __init__(self, conn: Connection, frames: FrameStore) -> None:
        self.token = new_token()
        self.server = ApiServer(
            Services(conn, frames, self.token, Controls(dict, lambda: None, lambda: None))
        )
        self.server.start()

    def get(self, path: str) -> tuple[int, bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        c.request("GET", path, headers={"Cookie": f"akashic_session={self.token}"})
        resp = c.getresponse()
        body = resp.read()
        c.close()
        return resp.status, body

    def json(self, path: str) -> Json:
        status, body = self.get(path)
        assert status == 200, body
        return cast("Json", json.loads(body))


@contextmanager
def serving(conn: Connection, frames: FrameStore) -> Generator[Api]:
    api = Api(conn, frames)
    try:
        yield api
    finally:
        api.server.stop()


# --- E2E 1: private windows on side monitors --------------------------------------------------


def test_zen_private_and_chrome_incognito_on_side_monitors_are_blank_unlisted_and_unlogged(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(monitors=row(3), focused=window(title="Notes"))
    rig.rig.show(4, "notepad.exe", "Side notes", Rect(800, 0, 1200, 600), focus=False,
                 image=solid(WHITE, 400, 600))  # fmt: skip
    rig.rig.show(
        5,
        "zen.exe",
        f"{SECRET} Page — Mozilla Firefox Private Browsing",
        Rect(1200, 0, 1600, 600),
        focus=False,
        url=f"https://example.org/{SECRET}",
        image=solid(RED, 400, 600),
    )
    rig.rig.show(6, "notepad.exe", "More notes", Rect(1600, 0, 2000, 600), focus=False,
                 image=solid(WHITE, 400, 600))  # fmt: skip
    rig.rig.show(
        7,
        "chrome.exe",
        f"{SECRET} Page - Google Chrome",
        Rect(2000, 0, 2400, 600),
        focus=False,
        url=f"https://example.org/{SECRET}",
        image=solid(RED, 400, 600),
    )
    rig.uia.captions[7] = f"{SECRET} Page - Google Chrome (Incognito)"
    rig.media.states = [
        MediaState("Z1", "Zen clip", "A channel", "playing"),
        MediaState("Chrome", "Chrome clip", "A channel", "playing"),
    ]
    rig.tick()
    zen_seen = rig.clock[0]

    # Blank in the stored composites, absent from the window lists.
    for key in ("m2", "m3"):
        image = newest_image(rig.rig, key)
        assert near(pixel(image, 200, 300), WHITE), key
        assert near(pixel(image, 600, 300), BLACK), key
        assert not has_colour(image, RED), key
    assert processes_stored(rig.conn) == {"notepad.exe"}
    hidden = rig.conn.execute("SELECT count(*) FROM frame_windows WHERE role = 'hidden'")
    assert hidden.fetchone() == (2,)
    assert rig.rig.captures.starts.count(5) == 0
    assert rig.rig.captures.starts.count(7) == 0
    assert rig.leaks(*NEEDLES) == []

    # Each one cuts its own browser's media for 60 s after it was last seen.
    for _ in range(3):
        rig.poll()
    assert rig.sessions() == 0
    rig.rig.hide(5)  # the Zen window closes
    rig.tick(15.0)  # the side monitors are checked again: only Chrome's verdict is renewed
    chrome_seen = rig.clock[0]
    walk_to(rig, zen_seen + 55)
    assert rig.sessions() == 0
    walk_to(rig, zen_seen + 62)
    apps = rig.media_conn.execute("SELECT app FROM media_sessions").fetchall()
    assert apps == [("Zen",)]  # Zen's cut lapsed, Chrome's has not
    walk_to(rig, chrome_seen + 55)
    assert rig.sessions() == 1
    walk_to(rig, chrome_seen + 62)
    apps = rig.media_conn.execute("SELECT app FROM media_sessions ORDER BY id").fetchall()
    assert apps == [("Zen",), ("Google Chrome",)]
    assert rig.leaks(*NEEDLES) == []


# --- E2E 2: bank navigation and typing in the bar --------------------------------------------


@pytest.mark.parametrize("address_lags", [False, True], ids=["address-first", "address-lags"])
def test_a_kept_chrome_tab_navigating_to_a_bank_stores_none_of_its_pixels_or_address(
    privacy_rig: Callable[..., PrivacyRig], *, address_lags: bool
) -> None:
    rig = privacy_rig(focused=chrome())
    rig.show(chrome(), image=solid(GREEN), url=VIDEO_URL)
    rig.tick()
    assert rig.frames() == 1
    assert near(pixel(newest_image(rig.rig, "m1"), 400, 300), GREEN)

    navigated = chrome(f"{SECRET} Mon espace - Google Chrome")
    rig.show(navigated, image=solid(RED), url=VIDEO_URL if address_lags else BANK_URL)
    rig.rig.repaint(1, solid(RED))  # the bank's pixels arrive first
    rig.rig.events.push(1, "title", rig.clock[0])
    rig.tick(6.0)
    if address_lags:
        rig.uia.urls[1] = BANK_URL  # the address bar catches up on a later read
    rig.tick(6.0)
    rig.tick(6.0)

    for frame_id in (r[0] for r in rig.conn.execute("SELECT id FROM frames")):
        image = stored_image(rig.rig, frame_id)
        assert image is None or not has_colour(image, RED)
    assert rig.conn.execute(
        "SELECT count(*) FROM frame_windows WHERE url LIKE '%paypal%'"
    ).fetchone() == (0,)
    assert rig.leaks(SECRET, "paypal") == []


def test_typing_in_the_bar_of_a_bank_page_keeps_it_as_the_security_guide_says(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(focused=chrome("Mon espace - Google Chrome"))
    rig.uia.urls[1] = BANK_URL
    rig.tick()
    assert rig.rig.last_note() == "hidden"
    assert rig.frames() == 0
    rig.uia.urls[1] = "mon solde du mois"  # the user types in the bar: no longer an address
    rig.tick(6.0)
    assert rig.frames() == 1
    assert rig.conn.execute("SELECT url FROM frame_windows WHERE focused").fetchone() == (None,)
    rig.uia.urls[1] = BANK_URL  # Escape restores the address
    rig.tick(6.0)
    assert rig.rig.last_note() == "hidden"
    assert rig.frames() == 1


# --- E2E 3: dropdown, PiP, DevTools and the shell surfaces -----------------------------------


def desk_with_private_window(rig: PrivacyRig) -> None:
    """A kept Chrome window with its dropdown, PiP and DevTools, and a Zen private window."""
    rig.rig.show(5, "zen.exe", f"{SECRET} Page — Mozilla Firefox Private Browsing",
                 Rect(800, 0, 1600, 600), focus=False, url=VIDEO_URL,
                 image=solid(RED))  # fmt: skip
    rig.show(chrome(), image=solid(GREEN), url=VIDEO_URL)
    rig.rig.show(2, "chrome.exe", "", Rect(100, 100, 300, 200), focus=False, image=solid(RED),
                 popup=True, owner=1, class_name="Chrome_WidgetWin_1")  # fmt: skip
    rig.rig.show(3, "chrome.exe", "Picture in picture", Rect(500, 400, 780, 580), focus=False,
                 image=solid(BLUE), class_name="Chrome_WidgetWin_1")  # fmt: skip
    rig.rig.show(4, "chrome.exe", "DevTools - youtube.com", Rect(50, 350, 450, 550), focus=False,
                 image=solid(YELLOW), class_name="Chrome_WidgetWin_1")  # fmt: skip
    rig.uia.urls[2] = None  # a dropdown has no address bar
    rig.uia.urls[3] = None
    rig.uia.urls[4] = "devtools://devtools/bundled/devtools_app.html"


def test_dropdown_pip_devtools_and_shell_surfaces_with_a_private_window_open(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(monitors=row(2))
    desk_with_private_window(rig)
    surfaces = (
        (10, "explorer.exe", "MultitaskingViewFrame", Rect(300, 250, 500, 330)),  # Alt-Tab
        (
            11,
            "explorer.exe",
            "XamlExplorerHostIslandWindow",
            Rect(600, 20, 780, 120),
        ),  # Snap Assist
        (12, "snippingtool.exe", "XamlWindow", Rect(20, 20, 120, 80)),  # Win+Shift+S
    )
    for hwnd, process, class_name, rect in surfaces:
        rig.rig.show(
            hwnd, process, "", rect, focus=False, image=solid(MAGENTA), class_name=class_name
        )
    rig.tick()

    image = newest_image(rig.rig, "m1")
    assert near(pixel(image, 200, 150), BLACK)  # the <select> dropdown is blank
    assert near(pixel(image, 640, 490), BLUE)  # the PiP shows
    assert near(pixel(image, 250, 450), YELLOW)  # DevTools of the kept window shows
    assert near(pixel(image, 700, 300), GREEN)
    for point in ((400, 290), (690, 70), (70, 50)):  # the shell surfaces are never painted
        assert near(pixel(image, *point), BLACK), point
    assert not has_colour(image, MAGENTA)
    assert not has_colour(image, RED)
    started = set(rig.rig.captures.starts)
    assert started.isdisjoint({2, 5, 10, 11, 12})
    assert {1, 3, 4} <= started
    windows = rig.conn.execute(
        "SELECT hwnd, role FROM frame_windows WHERE frame_id = ? ORDER BY z",
        (frame_ids(rig.rig, "m1")[-1],),
    ).fetchall()
    assert {h for h, role in windows if role == "app"} == {1, 3, 4}
    assert sum(role == "hidden" for _, role in windows) == 4
    assert rig.leaks(*NEEDLES) == []


def test_chrome_pip_and_devtools_go_blank_when_a_chrome_incognito_window_opens(
    privacy_rig: Callable[..., PrivacyRig],
) -> None:
    rig = privacy_rig(monitors=row(2))
    desk_with_private_window(rig)
    rig.tick()
    before = newest_image(rig.rig, "m1")
    assert near(pixel(before, 640, 490), BLUE)
    rig.rig.show(21, "chrome.exe", f"{SECRET} Page - Google Chrome", Rect(800, 0, 1600, 600),
                 focus=False, url=VIDEO_URL, image=solid(RED))  # fmt: skip
    rig.uia.captions[21] = f"{SECRET} Page - Google Chrome (Incognito)"
    rig.tick(6.0)
    after = newest_image(rig.rig, "m1")
    assert near(pixel(after, 640, 490), BLACK)  # the PiP now follows the strictest Chrome window
    assert near(pixel(after, 250, 450), BLACK)
    assert near(pixel(after, 700, 300), GREEN)  # the kept window itself is unchanged
    assert rig.leaks(*NEEDLES) == []


# --- E2E 4: the UI pauses everything, media included -----------------------------------------


@pytest.fixture
def fast_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    from akashic.capture import gaps as gaps_module  # noqa: PLC0415 - patched for this test only

    monkeypatch.setattr(gaps_module, "TICK_S", 0.05)


@pytest.fixture
def keyed(paths: Paths) -> DataKey:
    from akashic.storage.crypto import DataKey  # noqa: PLC0415 - runtime use only here

    k = DataKey.generate()
    conn = db.connect(paths.db_file, k.subkey("db"))
    db.migrate(conn)
    conn.close()
    return k


def media_rows(paths: Paths, key: DataKey) -> list[tuple[int, int]]:
    conn = db.connect(paths.db_file, key.subkey("db"))
    try:
        return [
            (int(r[0]), int(r[1]))
            for r in conn.execute("SELECT id, closed FROM media_sessions ORDER BY id")
        ]
    finally:
        conn.close()


def test_opening_the_ui_stops_the_media_log_with_the_screen_and_audio_and_closing_resumes_it(
    paths: Paths, keyed: DataKey, fast_gaps: None
) -> None:
    del fast_gaps
    cap = Capture(paths, keyed)
    cap.platform.media.names["Spotify"] = "Spotify"  # type: ignore[attr-defined]
    cap.platform.media.states = [MediaState("Spotify", "A song", "An artist", "playing")]  # type: ignore[attr-defined]
    try:
        assert wait_for(lambda: len(media_rows(paths, keyed)) == 1, 30)
        assert wait_for(lambda: frame_count(paths, keyed) >= 1, 30)
        cap.pipe.command("ui_open")
        assert wait_for(lambda: bool(cap.gaps("ui_open")), 30)
        assert wait_for(lambda: media_rows(paths, keyed)[0][1] == 1, 30)  # its session closed
        assert wait_for(cap.streams_closed, 30)
        stored, sessions = frame_count(paths, keyed), len(media_rows(paths, keyed))
        wait_for(lambda: False, 4.5)  # more than two media polls go by
        assert (frame_count(paths, keyed), len(media_rows(paths, keyed))) == (stored, sessions)
        cap.pipe.command("ui_closed")
        assert wait_for(lambda: len(media_rows(paths, keyed)) == sessions + 1, 30)
    finally:
        cap.finish()


# --- E2E 5: the Ask answer for a video on a side monitor -------------------------------------

TITLE = "Speedrun Hollow Knight"


def embed_everything(conn: Connection) -> None:
    queue = JobQueue(conn)
    embedder = HashEmbedder()
    handler = EmbedHandler(conn, embedder, lambda: True, lambda: NOW)
    while enqueue_missing(conn, queue, embedder.model, NOW):
        job = queue.claim([EMBED], NOW)
        assert job is not None
        handler.run(job)
        queue.complete(job.id, NOW)


def test_the_answer_for_a_video_on_a_side_monitor_is_its_masked_crop_and_opens_its_moment(
    paths: Paths, conn: Connection, key: DataKey, make_rig: Callable[..., Rig]
) -> None:
    afternoon = resolve("yesterday afternoon", NOW)
    assert afternoon is not None
    base = afternoon.start_ms + 2 * 3_600_000
    left, right = Rect(0, 0, 800, 600), Rect(800, 0, 1600, 600)
    rig = make_rig(monitors=[left, right], shown=False, ts_start=base)
    video = Rect(900, 50, 1500, 450)
    rig.show(1, "notepad.exe", "Notes", left, image=solid(WHITE))
    rig.show(2, "zen.exe", f"{TITLE} - YouTube — Zen Browser", video, focus=False,
             url="youtube.com/watch?v=abc", image=solid(BLUE, 600, 400))  # fmt: skip
    rig.show(3, "discord.exe", "Discord", Rect(1300, 250, 1550, 400), focus=False,
             image=solid(RED, 250, 150))  # fmt: skip
    rig.settle(0.0)
    rig.drain()

    media_conn = db.connect(paths.db_file, key.subkey("db"))
    source = FakeMediaNamed()
    tracker = MediaTracker(
        source=source,
        log_store=MediaLog(media_conn),
        exclusions=Exclusions(UserExclusions()),
        focused_window=rig.windows.focused_window,
        now_ms=lambda: base - 1_000 + int(rig.clock[0] * 1000),
        monotonic_ms=lambda: int(rig.clock[0] * 1000),
        awake_ms=lambda: int(rig.clock[0] * 1000),
        browsers=rig.browsers,
    )
    source.states = [MediaState("Z1", TITLE, "A channel", "playing")]
    for second in range(0, 72, 2):  # a little over a minute of playing
        rig.clock[0] = float(second)
        tracker.poll()
    source.states = []
    rig.clock[0] = 74.0
    tracker.poll()
    media_conn.close()
    assert build_episodes(conn, NOW)["media"] == 1
    embed_everything(conn)

    plan = {"time_text": "yesterday afternoon", "people": [], "kinds": ["video"], "keywords": [],
            "semantic_query": "a video watched"}  # fmt: skip
    chat = FakeChat(pieces=("You watched it [1].",), plan=json.dumps(plan))
    handler = AskHandler(
        conn, chat, HashEmbedder(), lambda: True, lambda: True, lambda: NOW, FlushClock()
    )
    handler.run(ask(conn, "What video did I watch yesterday afternoon?"))
    assert TITLE in chat.prompts[0][1]

    with serving(conn, rig.store) as api:
        answer = api.json("/api/ask/1")
        episode = next(s for s in cast("list[Json]", answer["sources"]) if s["kind"] == "episode")
        moment = api.json(f"/api/stage?ts={episode['frame_ms']}")
        status, body = api.get(
            f"/api/frames/{episode['frame_id']}/image?window={episode['window_id']}"
        )
    assert status == 200
    crop = Image.open(io.BytesIO(body)).convert("RGB")
    assert crop.size == (600, 400)  # the video window's rect, nothing of the monitor
    assert near(pixel(crop, 100, 100), BLUE)
    assert near(pixel(crop, 500, 300), BLACK)  # the Discord window in front is masked
    assert not has_colour(crop, RED)
    assert not has_colour(crop, WHITE)  # nothing of the other monitor

    # The link carries the frame's time, the video's monitor and the window; the stage shows them.
    right_id = rig.conn.execute("SELECT id FROM monitors WHERE key = 'm2'").fetchone()[0]
    assert episode["monitor_id"] == right_id
    monitors_at = {m["id"]: m for m in cast("list[Json]", moment["monitors"])}
    listed = cast("list[Json]", monitors_at[right_id]["windows"])
    assert episode["window_id"] in {w["id"] for w in listed}
    assert next(w for w in listed if w["id"] == episode["window_id"])["process"] == "zen.exe"
    ui = (Path(__file__).resolve().parents[1] / "src/akashic/ui/memory.js").read_text("utf-8")
    assert "momentHref(s.frame_ms, s.monitor_id, s.window_id)" in ui


class FakeMediaNamed:
    """Windows media sessions with Zen's display name resolved."""

    def __init__(self) -> None:
        self.states: list[MediaState] = []

    def sessions(self) -> list[MediaState]:
        return list(self.states)

    def app_name(self, app_id: str) -> str | None:
        return {"Z1": "Zen"}.get(app_id)


# --- E2E 6: a Discord window beside a browser ------------------------------------------------


def test_a_phrase_in_a_discord_window_beside_a_browser_is_found_under_discord_only(
    make_rig: Callable[..., Rig], monkeypatch: pytest.MonkeyPatch
) -> None:
    def read(ocr: FakeOcr, image: Image.Image) -> str:
        del ocr
        return "the zebrafish party starts tonight" if has_colour(image, BLUE) else "other words"

    monkeypatch.setattr(FakeOcr, "recognize", read)
    rig = make_rig(monitors=[Rect(0, 0, 800, 600)], shown=False)
    rig.uia.texts[1] = ["quarterly report of the finance team, with a long list of items"]
    rig.show(2, "discord.exe", "Discord", Rect(0, 0, 400, 600), focus=False,
             image=with_text_block(solid(BLUE, 400, 600)))  # fmt: skip
    rig.show(1, "chrome.exe", "Report - Google Chrome", Rect(400, 0, 800, 600),
             url="example.org/report", image=solid(GREEN, 400, 600))  # fmt: skip
    rig.settle(0.0)
    rig.drain()

    assert {h.process for h in keyword.search(rig.conn, "zebrafish party")} == {"discord.exe"}
    assert {h.process for h in keyword.search(rig.conn, "quarterly report")} == {"chrome.exe"}
    assert keyword.search(rig.conn, "tonight quarterly") == []  # the phrase spans both windows
    assert keyword.search(rig.conn, "zebrafish finance") == []


# --- E2E 7: game mode -------------------------------------------------------------------------


def test_game_mode_rates_window_captures_and_the_texts_read_after_the_game(
    make_rig: Callable[..., Rig], paths: Paths
) -> None:
    left, right = Rect(0, 0, 800, 600), Rect(800, 0, 1600, 600)
    rig = make_rig(
        monitors=[left, right],
        shown=False,
        game_settings=GameSettings(interval_s=60.0, end_grace_s=5.0, heartbeat_s=10),
    )
    rig.show(2, "discord.exe", "Discord", right, focus=False, image=solid(BLUE))
    rig.show(7, "game.exe", "The Game", left, has_frame=False, image=solid(GREEN))
    rig.settle(0.0)
    rig.drain()
    rig.repaint(2, solid(YELLOW))
    rig.tick(15.2)  # the side monitor, at the side rate
    rig.drain()
    rig.repaint(7, solid(MAGENTA))
    rig.tick(30.0)  # not yet: the game monitor waits for its 60 s
    rig.drain()
    rig.tick(60.2)
    rig.drain()
    triggers = rig.conn.execute(
        "SELECT m.key, f.trigger FROM frames f JOIN monitors m ON m.id = f.monitor_id ORDER BY f.id"
    ).fetchall()
    assert [k for k, _ in triggers].count("m1") == 2  # at 0 s and at 60 s
    assert [k for k, _ in triggers].count("m2") == 2  # at 0 s and at 15 s (it changed)
    assert {t for _, t in triggers} == {"game"}
    pending = rig.conn.execute(
        "SELECT id, process FROM frame_windows WHERE capture_path IS NOT NULL ORDER BY id"
    ).fetchall()
    assert {p for _, p in pending} == {"discord.exe", "game.exe"}
    assert rig.conn.execute(
        "SELECT count(*) FROM frame_windows WHERE text IS NOT NULL"
    ).fetchone() == (0,)
    assert rig.ocr.calls == 0

    rig.show(1, "notepad.exe", "Notes", left, image=solid(WHITE))
    rig.tick(66.0)
    rig.tick(66.1)
    rig.drain()
    assert rig.game_flags == [True, False]

    ocr = FakeOcr(text="zebrafish party tonight")
    handler = OcrWindowHandler(rig.store, lambda: ocr)
    for window_id, _ in pending:
        handler.run(Job(1, "ocr_window", f"ocr_window:{window_id}", {"window_id": window_id}, 0, 1))
    sources = rig.conn.execute(
        "SELECT DISTINCT text_source FROM frame_windows"
        " WHERE process IN ('discord.exe', 'game.exe') AND text IS NOT NULL"
    ).fetchall()
    assert sources == [("ocr_deferred",)]
    hits = keyword.search(rig.conn, "zebrafish party", limit=30)
    assert {h.process for h in hits} == {"discord.exe", "game.exe"}  # one hit per window
    assert rig.conn.execute(
        "SELECT count(*) FROM frame_windows WHERE capture_path IS NOT NULL"
    ).fetchone() == (0,)
    assert list(paths.media_dir.rglob("*_z*.webp.enc")) == []  # the deferred captures are gone


# --- E2E 8: the stage on three monitors --------------------------------------------------------


def test_the_stage_at_any_moment_on_three_monitors_with_mixed_scale_and_negative_coordinates(
    conn: Connection, make_rig: Callable[..., Rig]
) -> None:
    base = 1_700_000_000_000
    a, b, c = Rect(-800, 0, 0, 600), Rect(0, 0, 800, 600), Rect(800, 0, 1600, 600)
    rig = make_rig(monitors=[a, b, c], shown=False)
    rig.displays.monitor_list = [
        monitor(key="a", name="Monitor 1", rect=a, scale=1.5, primary=False),
        monitor(key="b", name="Monitor 2", rect=b, scale=1.0, primary=True),
        monitor(key="c", name="Monitor 3", rect=c, scale=2.0, primary=False),
    ]
    rig.capture._now_ms = lambda: base + int(rig.clock[0] * 1000)  # a stage ts is a clock time
    rig.show(1, "notepad.exe", "Notes", b, image=solid(GREEN))
    rig.show(2, "notepad.exe", "Left notes", a, focus=False, image=solid(BLUE))
    rig.show(3, "zen.exe", f"{SECRET} Page — Mozilla Firefox Private Browsing", c, focus=False,
             url=VIDEO_URL, image=solid(RED))  # fmt: skip
    rig.settle(10.0)
    rig.drain()
    saved = rig.facts.window_list
    rig.facts.window_list = None  # the window list cannot be read
    rig.tick(30.0)
    rig.facts.window_list = saved
    rig.settle(45.0)
    rig.drain()

    def at(second: float) -> int:
        return base + int(second * 1000)

    with serving(conn, rig.store) as api:

        def stage(second: float) -> dict[str, Json]:
            out = cast("list[Json]", api.json(f"/api/stage?ts={at(second)}")["monitors"])
            return {cast("str", m["name"]): m for m in out}

        first = stage(10.2)
        assert [first[n]["rect"] for n in ("Monitor 1", "Monitor 2", "Monitor 3")] == [
            [-800, 0, 0, 600], [0, 0, 800, 600], [800, 0, 1600, 600]
        ]  # fmt: skip
        assert [first[n]["scale"] for n in ("Monitor 1", "Monitor 2", "Monitor 3")] == [
            1.5,
            1.0,
            2.0,
        ]
        assert [first[n]["state"] for n in ("Monitor 1", "Monitor 2", "Monitor 3")] == [
            "shown", "shown", "hidden"
        ]  # fmt: skip
        assert first["Monitor 2"]["focused"] is True
        assert first["Monitor 3"]["windows"] == []
        assert first["Monitor 3"]["frame_id"] is None
        assert [w["process"] for w in cast("list[Json]", first["Monitor 1"]["windows"])] == [
            "notepad.exe"
        ]
        assert SECRET not in json.dumps(first)

        failed = stage(30.1)
        assert {m["state"] for m in failed.values()} == {"failed"}
        assert all(m["frame_id"] is None for m in failed.values())

        later = stage(45.2)
        assert {m["state"] for m in later.values()} == {"shown", "hidden"}
        assert later["Monitor 2"]["frame_id"] == frame_ids(rig, "b")[-1]
        assert stage(12.0)["Monitor 2"]["frame_id"] == frame_ids(rig, "b")[0]

        GapLog(conn).heartbeat(at(50.0))  # the capture died here; nothing wrote a crash row yet
        assert stage(49.0)["Monitor 2"]["state"] == "shown"
        crashed = stage(60.0)
        assert {m["state"] for m in crashed.values()} == {"not_recording"}
        assert all(m["frame_id"] is None and m["windows"] == [] for m in crashed.values())


# --- E2E 9: encoder overload ------------------------------------------------------------------


def test_encoder_overload_drops_side_pictures_retakes_them_and_reports_each_monitor(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(monitors=row(3), shown=False)
    rig.show(1, "notepad.exe", "Notes", Rect(0, 0, 800, 600), image=solid(WHITE))
    rig.show(2, "notepad.exe", "Side", Rect(800, 0, 1600, 600), focus=False, image=solid(BLUE))
    rig.show(3, "notepad.exe", "Far side", Rect(1600, 0, 2400, 600), focus=False,
             image=solid(GREEN))  # fmt: skip
    rig.settle(0.0)  # nothing is stored yet: the encoder is behind
    for step, second in enumerate((15.2, 30.4)):
        for hwnd, colours in ((1, (YELLOW, RED)), (2, (MAGENTA, BLUE)), (3, (RED, YELLOW))):
            rig.repaint(hwnd, solid(colours[step]))
        rig.tick(second)
    assert rig.writer.lost("m2")
    assert rig.writer.lost("m3")
    assert not rig.writer.lost("m1")  # side pictures go first
    problems = rig.writer.problems(0)
    assert f"{SCREEN_FRAMES_DROPPED}@Monitor 2" in problems
    assert f"{SCREEN_FRAMES_DROPPED}@Monitor 3" in problems
    assert not any(p.endswith("Monitor 1") for p in problems)

    rig.drain()
    assert "dropped" in states_of(rig.conn, "m2")
    assert rig.writer.lost("m2")  # still missing a stored picture

    rig.tick(45.6)  # the next check retakes it even though nothing changed on the screen
    rig.drain()
    assert not rig.writer.lost("m2")
    assert states_of(rig.conn, "m2")[-1] == "shown"
    assert frame_ids(rig, "m2")  # the retaken picture is stored
    assert rig.writer.problems(0) == []


# --- E2E 10: retention and Delete all ----------------------------------------------------------


def captured_history(rig: Rig) -> None:
    """A normal frame with text on two monitors, then a game with deferred window captures."""
    rig.show(1, "notepad.exe", "Notes", Rect(0, 0, 800, 600), image=solid(WHITE))
    rig.show(2, "discord.exe", "Discord", Rect(800, 0, 1600, 600), focus=False, image=solid(BLUE))
    rig.settle(0.0)
    rig.drain()
    rig.show(7, "game.exe", "The Game", Rect(0, 0, 800, 600), has_frame=False, image=solid(GREEN))
    rig.settle(20.0)
    rig.drain()


def seed_derived_copies(conn: Connection) -> None:
    window_id = conn.execute(
        "SELECT id FROM frame_windows WHERE text IS NOT NULL ORDER BY id LIMIT 1"
    ).fetchone()[0]
    conn.execute("INSERT INTO window_passages VALUES (?, 'm', 0, 5, 1, x'0000')", (window_id,))
    question = conn.execute(
        "INSERT INTO questions (asked_ms, text, state, answer, sources)"
        " VALUES (0, 'q', 'done', 'a', '[]')"
    ).lastrowid
    conn.execute("INSERT INTO question_windows VALUES (?, ?)", (question, window_id))


def test_retention_removes_window_rows_texts_with_their_copies_and_deferred_captures(
    make_rig: Callable[..., Rig], media: MediaStore, paths: Paths
) -> None:
    rig = make_rig(monitors=row(2), shown=False, ts_start=1_000_000)
    captured_history(rig)
    seed_derived_copies(rig.conn)
    assert list(paths.media_dir.rglob("*_z*.webp.enc"))  # deferred captures wait for their OCR
    rig.conn.execute("UPDATE jobs SET state = 'done' WHERE kind = 'ocr_window'")  # the OCR ran
    result = apply_retention(
        rig.conn,
        media,
        {"screenshots": 30, "frame_text": 30, "embeddings": 30},
        1_000_000 + 40 * 86_400_000,
    )
    assert result["screenshots"] > 0
    for table in ("frames", "frame_windows", "window_passages", "question_windows"):
        assert rig.conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table  # noqa: S608
    assert rig.conn.execute("SELECT state, answer, sources FROM questions").fetchall() == [
        ("failed", "", "[]")
    ]  # the saved answer that used the text is cleared too
    assert not [p for p in paths.media_dir.rglob("*") if p.is_file()]


def test_delete_all_removes_monitor_tables_window_rows_texts_copies_and_deferred_captures(
    make_rig: Callable[..., Rig], media: MediaStore, paths: Paths
) -> None:
    rig = make_rig(monitors=row(2), shown=False)
    captured_history(rig)
    seed_derived_copies(rig.conn)
    pending = rig.conn.execute(
        "SELECT count(*) FROM frame_windows WHERE capture_path IS NOT NULL"
    ).fetchone()[0]
    assert pending > 0  # a capture still waits for its OCR job
    for table in ("monitors", "monitor_layouts", "layout_monitors", "monitor_states"):
        assert rig.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0, table  # noqa: S608
    delete_all_recorded_data(rig.conn, media)
    assert_empty(rig.conn, paths)
    assert latest_frames(rig.conn) == []
