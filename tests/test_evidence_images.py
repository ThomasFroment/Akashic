"""Evidence images: the per-window crop route, the episode's frame and window, the UI URLs."""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from memory_data import NOW, add_episode, add_frame, add_window, window_of
from PIL import Image, ImageDraw

from akashic.api import frames as api_frames
from akashic.api import server as api_server
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.capture.media import process_for_app
from akashic.storage import monitors
from akashic.storage.crypto import CryptoError, DataKey
from akashic.storage.frames import FrameStore, NewFrame, NewWindow
from akashic.storage.gaps import GapLog
from akashic.storage.media import MediaStore
from akashic.storage.monitors import MonitorLog, MonitorRow, frame_moment, monitor_rect

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.config import Paths

type Rect = tuple[int, int, int, int]
type Colour = tuple[int, int, int]

A_RECT: Rect = (0, 0, 800, 400)
B_RECT: Rect = (-600, -100, 0, 300)  # left of and above the main monitor
A_MOVED: Rect = (100, 100, 900, 500)
FIRST_LAYOUT_MS = 1_000
SECOND_LAYOUT_MS = 50_000  # the second layout keeps only the main monitor, moved

GREY: Colour = (40, 40, 40)
RED: Colour = (220, 40, 40)
BLUE: Colour = (40, 60, 220)
GREEN: Colour = (40, 200, 60)
YELLOW: Colour = (230, 230, 30)
BLACK: Colour = (0, 0, 0)

UI_DIR = Path(api_server.__file__).resolve().parent.parent / "ui"


def episode_evidence(conn: Connection, episode_id: int, now_ms: int) -> tuple[int, int] | None:
    return monitors.episode_evidence(conn, episode_id, now_ms, process_for_app)


def lossless(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "WEBP", lossless=True)
    return out.getvalue()


def composite(rect: Rect, paints: list[tuple[Rect, Colour]]) -> bytes:
    """The monitor image: grey, then the rectangles back to front, in desktop coordinates."""
    image = Image.new("RGB", (rect[2] - rect[0], rect[3] - rect[1]), GREY)
    draw = ImageDraw.Draw(image)
    for (left, top, right, bottom), colour in paints:
        draw.rectangle(
            (left - rect[0], top - rect[1], right - rect[0] - 1, bottom - rect[1] - 1), fill=colour
        )
    return lossless(image)


def app(
    z: int, hwnd: int, rect: Rect, *, painted: bool = True, process: str = "x.exe"
) -> NewWindow:
    return NewWindow("app", z, hwnd, *rect, process=process, title="t", painted=painted)


def decode(body: bytes) -> Image.Image:
    return Image.open(io.BytesIO(body)).convert("RGB")


def near(pixel: object, colour: Colour, tolerance: int = 40) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(cast("Colour", pixel), colour, strict=True))


@dataclass
class Scene:
    frame_id: int
    windows: dict[int, int]  # z -> window id


@dataclass
class Env:
    port: int
    token: str
    services: Services
    conn: Connection
    frames: FrameStore
    ids: dict[str, int]
    scenes: dict[str, Scene]

    def get(self, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers={"Cookie": f"akashic_session={self.token}"})
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
        return resp.status, body

    def crop(self, scene: str, z: int, query: str = "") -> tuple[int, bytes]:
        s = self.scenes[scene]
        return self.get(f"/api/frames/{s.frame_id}/image?window={s.windows[z]}{query}")

    def add(
        self, name: str, ts: int, monitor: str, windows: list[NewWindow], image: bytes | None
    ) -> None:
        frame_id = self.frames.add(NewFrame(ts, self.ids[monitor], "click", image, tuple(windows)))
        stored = self.frames.get(frame_id)
        assert stored is not None
        self.scenes[name] = Scene(frame_id, {w.z: w.id for w in stored.windows})

    def path_of(self, scene: str) -> str:
        stored = self.frames.get(self.scenes[scene].frame_id)
        assert stored is not None
        assert stored.image_path is not None
        return stored.image_path


# Main monitor scene, front to back: F blue (z0), pending P (z1), hidden (z2), shell (z3), the
# window W (z4), and U below it (z5). Crop of W is (100, 50, 500, 250): 400 x 200.
F_RECT: Rect = (300, 100, 700, 350)
P_RECT: Rect = (400, 50, 500, 90)
HIDDEN_RECT: Rect = (100, 50, 200, 120)
SHELL_RECT: Rect = (100, 200, 250, 260)
W_RECT: Rect = (100, 50, 500, 250)
U_RECT: Rect = (50, 150, 400, 300)


@pytest.fixture
def env(paths: Paths, conn: Connection) -> Iterator[Env]:
    key = DataKey.generate()
    frames = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    log = MonitorLog(conn)
    ids = log.present(
        [
            MonitorRow("a", "A", A_RECT, 1.0, is_main=True),
            MonitorRow("b", "B", B_RECT, 1.0, is_main=False),
        ],
        FIRST_LAYOUT_MS,
    )
    log.present([MonitorRow("a", "A", A_MOVED, 1.0, is_main=True)], SECOND_LAYOUT_MS)
    token = new_token()
    services = Services(conn, frames, token, Controls(dict, lambda: None, lambda: None))
    server = ApiServer(services)
    server.start()
    e = Env(server.port, token, services, conn, frames, ids, {})
    main_image = composite(
        A_RECT,
        [
            (U_RECT, GREEN),
            (W_RECT, RED),
            (SHELL_RECT, YELLOW),
            (HIDDEN_RECT, YELLOW),
            (P_RECT, YELLOW),
            (F_RECT, BLUE),
        ],
    )
    e.add(
        "main",
        10_000,
        "a",
        [
            app(0, 11, F_RECT),
            app(1, 12, P_RECT, painted=False),
            NewWindow("hidden", 2, None, *HIDDEN_RECT),
            NewWindow("shell", 3, None, *SHELL_RECT),
            app(4, 14, W_RECT),
            app(5, 15, U_RECT),
        ],
        main_image,
    )
    # Window sticking out past the left and top edges of the monitor left of and above main.
    w2, f2 = (-700, -150, -300, 100), (-450, 0, -100, 200)
    e.add(
        "side",
        10_000,
        "b",
        [app(0, 31, f2), app(1, 32, w2)],
        composite(B_RECT, [(w2, RED), (f2, BLUE)]),
    )
    tall = (750, 0, 800, 400)
    e.add("tall", 12_000, "a", [app(0, 21, tall)], composite(A_RECT, [(tall, RED)]))
    e.add("noimage", 11_000, "a", [app(0, 41, W_RECT)], None)
    covered = (100, 100, 200, 200)
    e.add(
        "covered",
        13_000,
        "a",
        [app(0, 51, (50, 50, 300, 300)), app(1, 52, covered)],
        composite(A_RECT, [(covered, RED), ((50, 50, 300, 300), BLUE)]),
    )
    off = (2_000, 2_000, 2_100, 2_100)
    e.add("offscreen", 14_000, "a", [app(0, 61, off)], composite(A_RECT, [(W_RECT, RED)]))
    small = lossless(Image.new("RGB", (100, 100), RED))  # smaller than the monitor rect says
    e.add("outside_image", 15_000, "a", [app(0, 71, (500, 300, 700, 380))], small)
    moved = (200, 150, 400, 250)
    e.add("moved", 60_000, "a", [app(0, 81, moved)], composite(A_MOVED, [(moved, RED)]))
    e.add(
        "gone_monitor",
        60_000,
        "b",
        [app(0, 91, (-500, 0, -300, 100))],
        composite(B_RECT, [((-500, 0, -300, 100), RED)]),
    )
    e.add(
        "before_layout",
        500,
        "a",
        [app(0, 92, W_RECT)],
        composite(A_RECT, [(W_RECT, RED)]),
    )
    yield e
    server.stop()


# --- monitor_rect ----------------------------------------------------------------------------


def test_monitor_rect_is_the_layout_in_effect_at_ts(env: Env) -> None:
    a, b = env.ids["a"], env.ids["b"]
    assert monitor_rect(env.conn, a, FIRST_LAYOUT_MS) == A_RECT
    assert monitor_rect(env.conn, b, FIRST_LAYOUT_MS) == B_RECT
    assert monitor_rect(env.conn, b, SECOND_LAYOUT_MS - 1) == B_RECT
    assert monitor_rect(env.conn, a, SECOND_LAYOUT_MS) == A_MOVED
    assert monitor_rect(env.conn, b, SECOND_LAYOUT_MS) is None
    assert monitor_rect(env.conn, a, FIRST_LAYOUT_MS - 1) is None
    assert monitor_rect(env.conn, 999, FIRST_LAYOUT_MS) is None


# --- the crop route --------------------------------------------------------------------------


def test_the_crop_hides_everything_in_front_whatever_its_role(env: Env) -> None:
    status, body = env.crop("main", 4)
    assert status == 200
    shown = decode(body)
    assert shown.size == (400, 200)  # native size: the window's rect, nothing of the desktop
    for point in ((150, 20), (50, 100), (250, 20), (150, 120)):
        assert near(shown.getpixel(point), RED), point
    assert near(shown.getpixel((250, 100)), BLACK, 30)  # app in front, painted
    assert near(shown.getpixel((350, 20)), BLACK, 30)  # app in front, pending
    assert near(shown.getpixel((50, 30)), BLACK, 30)  # hidden row
    assert near(shown.getpixel((50, 175)), BLACK, 30)  # shell row
    colours = {shown.getpixel((x, y)) for x in range(0, 400, 7) for y in range(0, 200, 7)}
    assert not any(near(c, GREEN) or near(c, YELLOW) for c in colours)  # nothing from below


def test_the_response_is_webp_and_the_stored_frame_is_unchanged(env: Env) -> None:
    before = env.frames.image(env.path_of("main"))
    stored = env.frames.get(env.scenes["main"].frame_id)
    conn = http.client.HTTPConnection("127.0.0.1", env.port, timeout=10)
    s = env.scenes["main"]
    conn.request(
        "GET",
        f"/api/frames/{s.frame_id}/image?window={s.windows[4]}",
        headers={"Cookie": f"akashic_session={env.token}"},
    )
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.getheader("content-type") == "image/webp"
    assert env.frames.image(env.path_of("main")) == before
    assert env.frames.get(s.frame_id) == stored


def test_the_front_window_is_not_masked_by_what_is_behind_it(env: Env) -> None:
    status, body = env.crop("main", 0)
    assert status == 200
    shown = decode(body)
    assert shown.size == (400, 250)
    assert near(shown.getpixel((200, 125)), BLUE)
    assert near(shown.getpixel((5, 5)), BLUE)


def test_the_origin_is_the_monitor_corner_with_negative_coordinates_and_a_clipped_crop(
    env: Env,
) -> None:
    status, body = env.crop("side", 1)
    assert status == 200
    shown = decode(body)
    assert shown.size == (300, 200)  # (-700..-300, -150..100) clipped to the monitor
    assert near(shown.getpixel((50, 50)), RED)
    assert near(shown.getpixel((250, 50)), RED)
    assert near(shown.getpixel((50, 150)), RED)
    assert near(shown.getpixel((200, 150)), BLACK, 30)  # the front window covers it


def test_the_layout_at_the_frames_time_gives_the_origin(env: Env) -> None:
    status, body = env.crop("moved", 0)
    assert status == 200
    shown = decode(body)
    assert shown.size == (200, 100)
    assert near(shown.getpixel((100, 50)), RED)


def test_w_fits_the_crop_into_w_by_four_w(env: Env) -> None:
    assert decode(env.crop("main", 4, "&w=100")[1]).size == (100, 50)
    assert decode(env.crop("main", 4, "&w=16")[1]).size == (16, 8)
    # 50 x 400 at w=20 fits (20, 80): the height bound is 4w, so 10 x 80.
    assert decode(env.crop("tall", 0, "&w=20")[1]).size == (10, 80)
    assert env.crop("main", 4, "&w=5")[0] == 422


def test_without_window_the_route_is_as_before(env: Env) -> None:
    s = env.scenes["main"]
    status, body = env.get(f"/api/frames/{s.frame_id}/image")
    assert (status, body) == (200, env.frames.image(env.path_of("main")))
    assert decode(body).size == (800, 400)
    assert decode(env.get(f"/api/frames/{s.frame_id}/image?w=200")[1]).size == (200, 100)


@pytest.fixture
def decrypts(env: Env, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    read = env.frames.image

    def counting(path: str) -> bytes:
        calls.append(path)
        return read(path)

    monkeypatch.setattr(env.frames, "image", counting)
    return calls


def other_frames_window(env: Env) -> int:
    return env.scenes["side"].windows[1]


@pytest.mark.parametrize(
    "case",
    [
        "no_frame",
        "no_image",
        "unknown_window",
        "other_frames_window",
        "hidden",
        "shell",
        "pending",
        "layout_without_monitor",
        "before_any_layout",
        "fully_covered",
        "outside_the_monitor",
    ],
)
def test_geometry_that_rules_a_crop_out_is_a_404_without_decrypting(
    env: Env, decrypts: list[str], case: str
) -> None:
    main = env.scenes["main"]
    paths = {
        "no_frame": "/api/frames/9999/image?window=1",
        "no_image": f"/api/frames/{env.scenes['noimage'].frame_id}/image"
        f"?window={env.scenes['noimage'].windows[0]}",
        "unknown_window": f"/api/frames/{main.frame_id}/image?window=99999",
        "other_frames_window": f"/api/frames/{main.frame_id}/image"
        f"?window={other_frames_window(env)}",
        "hidden": f"/api/frames/{main.frame_id}/image?window={main.windows[2]}",
        "shell": f"/api/frames/{main.frame_id}/image?window={main.windows[3]}",
        "pending": f"/api/frames/{main.frame_id}/image?window={main.windows[1]}",
        "layout_without_monitor": f"/api/frames/{env.scenes['gone_monitor'].frame_id}/image"
        f"?window={env.scenes['gone_monitor'].windows[0]}",
        "before_any_layout": f"/api/frames/{env.scenes['before_layout'].frame_id}/image"
        f"?window={env.scenes['before_layout'].windows[0]}",
        "fully_covered": f"/api/frames/{env.scenes['covered'].frame_id}/image"
        f"?window={env.scenes['covered'].windows[1]}",
        "outside_the_monitor": f"/api/frames/{env.scenes['offscreen'].frame_id}/image"
        f"?window={env.scenes['offscreen'].windows[0]}",
    }
    assert env.get(paths[case])[0] == 404
    assert env.get(paths[case] + "&w=100")[0] == 404
    assert decrypts == []


def test_a_crop_outside_the_stored_image_is_a_404(env: Env) -> None:
    assert env.crop("outside_image", 0)[0] == 404


def test_a_missing_file_is_a_404_and_a_decrypt_failure_a_500(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(_path: str) -> bytes:
        raise FileNotFoundError

    def broken(_path: str) -> bytes:
        raise CryptoError("secret detail")

    monkeypatch.setattr(env.frames, "image", gone)
    assert env.crop("main", 4)[0] == 404
    monkeypatch.setattr(env.frames, "image", broken)
    assert env.crop("main", 4)[0] == 500


def test_decrypt_masking_and_encoding_run_outside_the_db_lock(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lock_is_free() -> bool:
        got: list[bool] = []

        def probe() -> None:
            acquired = env.services._lock.acquire(timeout=1)
            got.append(acquired)
            if acquired:
                env.services._lock.release()

        worker = threading.Thread(target=probe)
        worker.start()
        worker.join(5)
        return got == [True]

    seen: list[bool] = []
    read, render = env.frames.image, api_frames._render

    def image(path: str) -> bytes:
        seen.append(lock_is_free())
        return read(path)

    def rendering(webp: bytes, width: int | None, crop: object) -> bytes | None:
        seen.append(lock_is_free())
        return render(webp, width, crop)  # type: ignore[arg-type]

    monkeypatch.setattr(env.frames, "image", image)
    monkeypatch.setattr(api_frames, "_render", rendering)
    assert env.crop("main", 4, "&w=100")[0] == 200
    assert seen == [True, True]


# --- the cache -------------------------------------------------------------------------------


def test_a_crop_is_cached_per_window_and_never_served_for_another_view(env: Env) -> None:
    main = env.scenes["main"]
    path = env.path_of("main")
    crop = env.crop("main", 4, "&w=100")[1]
    keys = set(env.services._thumbs)
    assert keys == {(main.frame_id, path, main.windows[4], 100)}
    whole = env.get(f"/api/frames/{main.frame_id}/image?w=100")[1]
    front = env.crop("main", 0, "&w=100")[1]
    assert decode(whole).size == (100, 50)
    assert whole != crop
    assert front != crop
    assert set(env.services._thumbs) == keys | {
        (main.frame_id, path, 0, 100),
        (main.frame_id, path, main.windows[0], 100),
    }
    assert env.crop("main", 4, "&w=100")[1] == crop  # hit


def test_a_cache_hit_is_exact_on_frame_path_window_and_width(env: Env) -> None:
    main = env.scenes["main"]
    path = env.path_of("main")
    window = main.windows[4]
    env.services.put_thumb((main.frame_id, path, window, 100), b"fake", 0)
    assert env.crop("main", 4, "&w=100") == (200, b"fake")
    assert env.crop("main", 4, "&w=101")[1] != b"fake"
    assert env.crop("main", 0, "&w=100")[1] != b"fake"
    assert env.get(f"/api/frames/{main.frame_id}/image?w=100")[1] != b"fake"
    assert env.get(f"/api/frames/{main.frame_id}/image?window={window}")[1] != b"fake"
    # A reused frame id with a new image path misses.
    env.conn.execute("UPDATE frames SET image_path = 'new.webp' WHERE id = ?", (main.frame_id,))
    assert env.crop("main", 4, "&w=100")[0] == 404  # no such file: the old entry is not served


def test_only_w_requests_are_cached(env: Env) -> None:
    env.crop("main", 4)
    env.crop("main", 4)
    assert env.services._thumbs == {}


def test_clear_thumbs_and_the_generation_guard_cover_crops(env: Env) -> None:
    env.crop("main", 4, "&w=100")
    assert len(env.services._thumbs) == 1
    env.services.clear_thumbs()
    assert env.services._thumbs == {}
    main = env.scenes["main"]
    key = (main.frame_id, env.path_of("main"), main.windows[4], 100)
    stale = env.services.thumb_generation() - 1
    env.services.put_thumb(key, b"old", stale)
    assert env.services.thumb(key) is None


# --- the episode's frame and window ----------------------------------------------------------


def paint(conn: Connection, *frames: int) -> None:
    for frame in frames:
        conn.execute("UPDATE frames SET image_path = 'x.enc' WHERE id = ?", (frame,))
        conn.execute("UPDATE frame_windows SET painted = 1 WHERE frame_id = ?", (frame,))


def episode(  # noqa: PLR0913 - the fields of one episode row
    conn: Connection,
    kind: str,
    title: str,
    *,
    start: int = 10_000,
    end: int = 20_000,
    app_name: str | None = "Google Chrome",
    url: str | None = None,
) -> int:
    ep = add_episode(conn, kind, start, end, title)
    conn.execute("UPDATE episodes SET app = ?, url = ? WHERE id = ?", (app_name, url, ep))
    return ep


def test_the_earliest_matching_painted_frame_in_the_span_wins(conn: Connection) -> None:
    ep = episode(conn, "video", "Cat Video")
    before = add_frame(conn, 9_000, "t", "Cat Video")
    other_title = add_frame(conn, 10_100, "t", "Dog Video")
    unpainted = add_frame(conn, 10_200, "t", "Cat Video")
    no_image = add_frame(conn, 10_300, "t", "Cat Video")
    other_app = add_frame(conn, 10_400, "t", "Cat Video", process="code.exe")
    side = add_frame(conn, 12_000, "t", "the CAT video - Zen", monitor_id=1, focused=False)
    later = add_frame(conn, 15_000, "t", "Cat Video")
    ends = add_frame(conn, 20_000, "t", "Cat Video")
    paint(conn, before, other_title, other_app, side, later, ends)
    conn.execute("UPDATE frames SET image_path = 'x.enc' WHERE id = ?", (unpainted,))
    conn.execute("UPDATE frame_windows SET painted = 1 WHERE frame_id = ?", (no_image,))
    assert episode_evidence(conn, ep, NOW) == (side, window_of(conn, side))  # unfocused, monitor 1


def test_ties_go_by_frame_id_then_lowest_z(conn: Connection) -> None:
    ep = episode(conn, "work", "Sprint")
    first = add_frame(conn, 12_000, "t", "A")
    second = add_frame(conn, 12_000, "t", "B")
    behind = add_window(conn, first, 1, "t", process="chrome.exe", title="C", hwnd=5)
    paint(conn, first, second)
    assert episode_evidence(conn, ep, NOW) == (first, window_of(conn, first))
    conn.execute("UPDATE frame_windows SET painted = 0 WHERE id = ?", (window_of(conn, first),))
    assert episode_evidence(conn, ep, NOW) == (first, behind)


def test_music_follows_the_media_title_rule_and_conversations_ignore_the_title(
    conn: Connection,
) -> None:
    music = episode(conn, "music", "Some Song", app_name="Spotify")
    talk = episode(conn, "conversation", "Call with X", app_name="discord.exe")
    song = add_frame(conn, 12_000, "t", "Spotify - some song", process="spotify.exe")
    other = add_frame(conn, 11_000, "t", "Spotify - Another", process="spotify.exe")
    call = add_frame(conn, 13_000, "t", "General", process="discord.exe")
    paint(conn, song, other, call)
    assert episode_evidence(conn, music, NOW) == (song, window_of(conn, song))
    assert episode_evidence(conn, talk, NOW) == (call, window_of(conn, call))


def test_an_episode_without_an_app_has_no_evidence(conn: Connection) -> None:
    ep = episode(conn, "work", "Quiet", app_name=None)
    paint(conn, add_frame(conn, 12_000, "t", "A"))
    assert episode_evidence(conn, ep, NOW) is None
    assert episode_evidence(conn, 9999, NOW) is None


# Fallback: the monitor's latest frame at or before the start, only while it still holds.
HWND = 7


@dataclass
class Desk:
    conn: Connection
    log: MonitorLog
    mid: int
    ep: int
    frame: int


@pytest.fixture
def desk(conn: Connection) -> Desk:
    log = MonitorLog(conn)
    mid = log.present([MonitorRow("index:0", "Monitor 1", A_RECT, 1.0, is_main=True)], 500)[
        "index:0"
    ]
    log.state(mid, "shown", 1_000)
    frame = add_frame(conn, 2_000, "t", "Cat Video", monitor_id=mid, hwnd=HWND)
    paint(conn, frame)
    ep = episode(conn, "video", "Cat Video", start=5_000, end=9_000)
    return Desk(conn, log, mid, ep, frame)


def test_a_valid_frame_before_the_start_is_the_fallback(desk: Desk) -> None:
    assert episode_evidence(desk.conn, desk.ep, NOW) == (
        desk.frame,
        window_of(desk.conn, desk.frame),
    )


def test_a_match_inside_the_span_beats_the_fallback(desk: Desk) -> None:
    inside = add_frame(desk.conn, 6_000, "t", "Cat Video", monitor_id=desk.mid, hwnd=HWND)
    paint(desk.conn, inside)
    assert episode_evidence(desk.conn, desk.ep, NOW) == (inside, window_of(desk.conn, inside))


def test_no_fallback_after_a_hidden_run(desk: Desk) -> None:
    desk.log.state(desk.mid, "hidden", 3_000)
    assert episode_evidence(desk.conn, desk.ep, NOW) is None


@pytest.mark.parametrize("state", ["dropped", "failed"])
def test_no_fallback_after_a_dropped_or_failed_run(desk: Desk, state: str) -> None:
    desk.log.state(desk.mid, state, 3_000)  # type: ignore[arg-type]
    assert episode_evidence(desk.conn, desk.ep, NOW) is None


def test_no_fallback_across_a_stopping_gap(desk: Desk) -> None:
    GapLog(desk.conn).add("screen", "paused", 3_000, 4_000)
    assert episode_evidence(desk.conn, desk.ep, NOW) is None


def test_no_fallback_across_a_layout_without_the_monitor(desk: Desk) -> None:
    desk.log.present([MonitorRow("index:1", "Monitor 2", B_RECT, 1.0, is_main=True)], 3_000)
    assert episode_evidence(desk.conn, desk.ep, NOW) is None


def test_no_fallback_from_a_nearby_frame_of_another_app_or_an_older_one(desk: Desk) -> None:
    nearby = add_frame(
        desk.conn, 4_000, "t", "Else", process="code.exe", monitor_id=desk.mid, hwnd=8
    )
    paint(desk.conn, nearby)
    assert episode_evidence(desk.conn, desk.ep, NOW) is None  # the older matching frame is not used


def test_the_episode_route_returns_both_ids_or_both_null(env: Env) -> None:
    ep = episode(env.conn, "work", "Sprint", app_name="code.exe")
    frame = add_frame(env.conn, 12_000, "t", "A", process="code.exe", monitor_id=env.ids["a"])
    paint(env.conn, frame)
    status, body = env.get(f"/api/episodes/{ep}")
    out = cast("dict[str, object]", json.loads(body))
    assert (status, out["frame_id"], out["window_id"]) == (200, frame, window_of(env.conn, frame))
    assert (out["frame_ms"], out["monitor_id"]) == (12_000, env.ids["a"])
    env.conn.execute("DELETE FROM frames WHERE id = ?", (frame,))
    out = cast("dict[str, object]", json.loads(env.get(f"/api/episodes/{ep}")[1]))
    assert (out["frame_id"], out["window_id"]) == (None, None)
    assert (out["frame_ms"], out["monitor_id"]) == (None, None)


def test_frame_moment_is_the_frame_time_and_monitor_or_none(env: Env) -> None:
    assert frame_moment(env.conn, env.scenes["side"].frame_id) == (10_000, env.ids["b"])
    assert frame_moment(env.conn, env.scenes["tall"].frame_id) == (12_000, env.ids["a"])
    assert frame_moment(env.conn, 99_999) is None


# --- Ask sources -----------------------------------------------------------------------------


def test_ask_episode_sources_gain_frame_and_window_and_others_are_unchanged(env: Env) -> None:
    shown = episode(env.conn, "work", "Sprint", app_name="code.exe")
    bare = episode(env.conn, "work", "Quiet", app_name=None)
    frame = add_frame(env.conn, 12_000, "t", "A", process="code.exe", monitor_id=env.ids["a"])
    paint(env.conn, frame)
    line = {"n": 1, "kind": "line", "id": 9}
    framed = {"n": 4, "kind": "frame", "id": frame, "window_id": 3}
    gone = {"n": 5, "kind": "frame", "id": 99_999, "window_id": 3}
    sources = [
        line,
        {"n": 2, "kind": "episode", "id": shown},
        {"n": 3, "kind": "episode", "id": bare},
        framed,
        gone,
    ]
    env.conn.execute(
        "INSERT INTO questions (id, asked_ms, text, state, answer, sources)"
        " VALUES (5, ?, 'q', 'done', 'a', ?)",
        (NOW, json.dumps(sources)),
    )
    env.conn.execute(
        "INSERT INTO questions (id, asked_ms, text, state, answer, sources, thread_id)"
        " VALUES (6, ?, 'q2', 'done', 'a', ?, 5)",
        (NOW, json.dumps(sources)),
    )
    expected = [
        line,
        {
            "n": 2,
            "kind": "episode",
            "id": shown,
            "frame_id": frame,
            "window_id": window_of(env.conn, frame),
            "frame_ms": 12_000,
            "monitor_id": env.ids["a"],
        },
        {
            "n": 3,
            "kind": "episode",
            "id": bare,
            "frame_id": None,
            "window_id": None,
            "frame_ms": None,
            "monitor_id": None,
        },
        {**framed, "frame_ms": 12_000, "monitor_id": env.ids["a"]},
        {**gone, "frame_ms": None, "monitor_id": None},
    ]
    one = json.loads(env.get("/api/ask/5")[1])
    assert one["sources"] == expected
    thread = json.loads(env.get("/api/ask/thread/5")[1])
    assert [t["sources"] for t in thread] == [expected, expected]


# --- UI URLs ---------------------------------------------------------------------------------


def ui(name: str) -> str:
    return (UI_DIR / name).read_text("utf-8")


def test_search_hits_ask_sources_and_episode_page_ask_for_the_window_crop() -> None:
    assert "`/api/frames/${hit.id}/image?window=${hit.window_id}&w=480`" in ui("app.js")
    memory = ui("memory.js")
    assert "image?window=${s.window_id}&w=320`" in memory
    assert 'const frameId = s.kind === "frame" ? s.id : s.frame_id;' in memory
    assert "if (!s.window_id) return null;" in memory
    assert "`/api/frames/${e.frame_id}/image?window=${e.window_id}&w=960`" in memory
    assert "e.frame_id && e.window_id ?" in memory


def test_only_the_frame_viewer_asks_for_a_whole_monitor_image() -> None:
    whole = [
        f"{name}: {m.group(0)}"
        for name in ("app.js", "memory.js")
        for m in re.finditer(r"/api/frames/[^`\"']*?/image(?!\?window=)[^`\"']*", ui(name))
    ]
    assert whole == ["app.js: /api/frames/${f.id}/image"]
