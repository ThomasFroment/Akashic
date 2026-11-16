"""The timeline API: /api/stage (the desk at a moment) and /api/strip (a day's app bands)."""

from __future__ import annotations

import http.client
import io
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
import sqlcipher3
from memory_data import add_visit, window_of
from PIL import Image

from akashic.api import frames as api_frames
from akashic.api import timeline
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.api.timeline import day_range_ms
from akashic.storage import monitors
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore, NewFrame, NewWindow
from akashic.storage.gaps import GapLog
from akashic.storage.media import MediaStore
from akashic.storage.monitors import MonitorLog, MonitorRow

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.config import Paths

type Json = dict[str, object]

L_RECT = (0, 0, 1920, 1080)
R_RECT = (-1280, 0, 0, 1024)  # left of the main monitor: negative coordinates
LAYOUT_MS = 1_000
SECOND_LAYOUT_MS = 100_000
KEYS = ["L", "R", "H", "D", "F", "N"]
FIELDS = {"id", "name", "rect", "scale", "state", "frame_id", "checked_ms", "focused", "windows"}


def app(z: int, hwnd: int, process: str, title: str, *, focused: bool = False) -> NewWindow:
    return NewWindow(
        "app",
        z,
        hwnd,
        z * 10,
        z * 10 + 1,
        500,
        400,
        process=process,
        title=title,
        url="https://secret.test",
        text="secret text",
        text_source="uia",
        focused=focused,
    )


@dataclass
class Desk:
    port: int
    token: str
    conn: Connection
    frames: FrameStore
    ids: dict[str, int]
    f: dict[str, int]  # frame ids by name

    def raw(self, path: str) -> tuple[int, bytes]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers={"Cookie": f"akashic_session={self.token}"})
        resp = c.getresponse()
        body = resp.read()
        c.close()
        return resp.status, body

    def stage(self, ts: int) -> Json:
        status, body = self.raw(f"/api/stage?ts={ts}")
        assert status == 200, body
        return cast("Json", json.loads(body))

    def monitors(self, ts: int) -> dict[str, Json]:
        by_id = {v: k for k, v in self.ids.items()}
        out = cast("list[Json]", self.stage(ts)["monitors"])
        return {by_id[cast("int", m["id"])]: m for m in out}


def _webp() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 100, 50)).save(out, "WEBP")
    return out.getvalue()


@pytest.fixture
def desk(paths: Paths, conn: Connection) -> Iterator[Desk]:
    key = DataKey.generate()
    frames = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    log = MonitorLog(conn)
    ids = log.present(
        [
            MonitorRow("L", "Left", L_RECT, 1.0, is_main=True),
            MonitorRow("R", "Right", R_RECT, 1.5, is_main=False),
            MonitorRow("H", "Hid", (2000, 0, 2500, 500), 1.0, is_main=False),
            MonitorRow("D", "Drop", (2500, 0, 3000, 500), 1.0, is_main=False),
            MonitorRow("F", "Fail", (3000, 0, 3500, 500), 1.0, is_main=False),
            MonitorRow("N", "Never", (3500, 0, 4000, 500), 1.0, is_main=False),
        ],
        LAYOUT_MS,
    )
    log.state(ids["L"], "shown", LAYOUT_MS)
    log.state(ids["R"], "shown", LAYOUT_MS)
    log.state(ids["H"], "hidden", LAYOUT_MS)
    log.state(ids["D"], "dropped", LAYOUT_MS)
    log.state(ids["F"], "failed", LAYOUT_MS)
    log.state(ids["L"], "shown", 18_000)  # confirms the run again
    f: dict[str, int] = {}

    def add(
        name: str, ts: int, monitor: str, windows: list[NewWindow], image: bytes | None = None
    ) -> None:
        f[name] = frames.add(NewFrame(ts, ids[monitor], "click", image, tuple(windows)))

    add(
        "l1",
        10_000,
        "L",
        [
            app(0, 11, "a.exe", "A"),
            NewWindow("hidden", 1, None, 0, 0, 5, 5),
            NewWindow("shell", 2, None, 0, 0, 6, 6),
            app(3, 12, "b.exe", "B", focused=True),
        ],
        _webp(),
    )
    add("r1", 15_000, "R", [app(0, 21, "c.exe", "C", focused=True)])
    for key_ in "HDF":
        add(key_.lower(), 16_000, key_, [app(0, 31, "z.exe", "Z", focused=True)])
    add("l2", 20_000, "L", [app(0, 11, "a.exe", "A2")])
    token = new_token()
    server = ApiServer(Services(conn, frames, token, Controls(dict, lambda: None, lambda: None)))
    server.start()
    yield Desk(server.port, token, conn, frames, ids, f)
    server.stop()


# --- /api/stage ------------------------------------------------------------------------------


def test_stage_has_one_entry_per_monitor_in_id_order_with_values_from_stage_at(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int]] = []
    real = monitors.stage_at

    def spy(conn: Connection, ts: int, now: int) -> list[monitors.MonitorAt]:
        calls.append((ts, now))
        return real(conn, ts, now)

    monkeypatch.setattr(monitors, "stage_at", spy)
    before = time.time_ns() // 1_000_000
    out = desk.stage(17_000)
    after = time.time_ns() // 1_000_000
    assert set(out) == {"ts_ms", "monitors", "prev_ms", "next_ms"}
    assert out["ts_ms"] == 17_000
    [(ts, now)] = calls
    assert ts == 17_000
    assert before <= now <= after  # the wall clock, never ts
    rows = cast("list[Json]", out["monitors"])
    assert [m["id"] for m in rows] == sorted(desk.ids.values())
    assert all(set(m) == FIELDS for m in rows)
    expected = real(desk.conn, 17_000, now)
    for m, e in zip(rows, expected, strict=True):
        assert (m["id"], m["name"], m["rect"], m["scale"]) == (
            e.monitor_id,
            e.name,
            list(e.rect),
            e.scale,
        )
        assert (m["state"], m["frame_id"], m["checked_ms"]) == (e.state, e.frame_id, e.checked_ms)
    by_key = desk.monitors(17_000)
    assert by_key["R"]["rect"] == [-1280, 0, 0, 1024]  # negative coordinates pass through
    assert (by_key["L"]["scale"], by_key["R"]["scale"]) == (1.0, 1.5)  # mixed scales too


def test_no_layout_at_or_before_ts_gives_no_monitors(desk: Desk) -> None:
    out = desk.stage(LAYOUT_MS - 1)
    assert out["monitors"] == []
    assert out["next_ms"] == 10_000


def test_the_layout_in_effect_at_ts_is_used(desk: Desk) -> None:
    log = MonitorLog(desk.conn)
    log.present([MonitorRow("L", "Left", (5, 6, 1925, 1086), 1.0, is_main=True)], SECOND_LAYOUT_MS)
    rows = cast("list[Json]", desk.stage(SECOND_LAYOUT_MS + 1)["monitors"])
    assert [(m["id"], m["rect"]) for m in rows] == [(desk.ids["L"], [5, 6, 1925, 1086])]
    assert len(cast("list[Json]", desk.stage(SECOND_LAYOUT_MS - 1)["monitors"])) == len(KEYS)


def test_only_shown_monitors_carry_a_frame_and_windows(desk: Desk) -> None:
    m = desk.monitors(17_000)
    assert {k: (m[k]["state"], m[k]["frame_id"]) for k in KEYS} == {
        "L": ("shown", desk.f["l1"]),
        "R": ("shown", desk.f["r1"]),
        "H": ("hidden", None),
        "D": ("dropped", None),
        "F": ("failed", None),
        "N": ("not_recording", None),
    }
    for k in "HDFN":
        assert (m[k]["windows"], m[k]["focused"]) == ([], False)  # frames exist for H, D, F


def test_shown_with_no_frame_yet_has_null_frame_and_no_windows(desk: Desk) -> None:
    r = desk.monitors(12_000)["R"]  # R's first frame is at 15_000
    assert (r["state"], r["frame_id"], r["windows"], r["focused"]) == ("shown", None, [], False)


def test_each_shown_monitor_reports_its_latest_frame_at_or_before_ts(desk: Desk) -> None:
    assert desk.monitors(19_999)["L"]["frame_id"] == desk.f["l1"]
    assert desk.monitors(20_000)["L"]["frame_id"] == desk.f["l2"]


def test_windows_are_the_app_rows_front_to_back_without_private_fields(desk: Desk) -> None:
    conn = desk.conn
    first, last = window_of(conn, desk.f["l1"], 0), window_of(conn, desk.f["l1"], 3)
    windows = desk.monitors(17_000)["L"]["windows"]
    assert windows == [
        {"id": first, "process": "a.exe", "title": "A", "rect": [0, 1, 500, 400]},
        {"id": last, "process": "b.exe", "title": "B", "rect": [30, 31, 500, 400]},
    ]
    dumped = json.dumps(desk.stage(17_000))
    for secret in ("hwnd", "secret", "https://"):
        assert secret not in dumped


def test_focused_is_the_shown_monitor_with_the_newest_focused_stage_frame(desk: Desk) -> None:
    at_17 = desk.monitors(17_000)
    assert [at_17[k]["focused"] for k in KEYS] == [False, True, False, False, False, False]
    # L has the only frame at 12_000, with a focused row; R has none yet.
    at_12 = desk.monitors(12_000)
    assert (at_12["L"]["focused"], at_12["R"]["focused"]) == (True, False)
    # L's newest frame has no focused row, so R's older focused frame is the newest one.
    at_21 = desk.monitors(21_000)
    assert (at_21["L"]["focused"], at_21["R"]["focused"]) == (False, True)


def test_ties_are_all_marked_and_none_when_no_focused_row(desk: Desk) -> None:
    desk.conn.execute("UPDATE frames SET ts_ms = 15_000 WHERE id = ?", (desk.f["l1"],))
    tied = desk.monitors(17_000)
    assert (tied["L"]["focused"], tied["R"]["focused"]) == (True, True)
    desk.conn.execute("UPDATE frame_windows SET focused = 0")
    assert [m["focused"] for m in desk.monitors(17_000).values()] == [False] * len(KEYS)


def test_prev_and_next_are_the_nearest_frame_times_of_any_monitor(desk: Desk) -> None:
    out = desk.stage(17_000)
    assert (out["prev_ms"], out["next_ms"]) == (16_000, 20_000)  # H's frame counts
    out = desk.stage(10_000)  # strictly before and after
    assert (out["prev_ms"], out["next_ms"]) == (None, 15_000)
    out = desk.stage(20_000)
    assert (out["prev_ms"], out["next_ms"]) == (16_000, None)


def test_a_crash_makes_every_monitor_not_recording_from_the_heartbeat_on(desk: Desk) -> None:
    GapLog(desk.conn).heartbeat(20_000)  # long stale now
    assert {m["state"] for m in desk.monitors(19_999).values()} == {
        "shown",
        "hidden",
        "dropped",
        "failed",
        "not_recording",
    }
    for ts in (20_000, 30_000):
        crashed = desk.monitors(ts)
        assert {m["state"] for m in crashed.values()} == {"not_recording"}
        assert all(m["frame_id"] is None and m["windows"] == [] for m in crashed.values())


def test_a_fresh_heartbeat_is_not_a_crash(desk: Desk) -> None:
    now = time.time_ns() // 1_000_000
    GapLog(desk.conn).heartbeat(now)
    assert desk.monitors(now - 1)["L"]["state"] == "shown"


def test_an_open_clean_stop_is_not_a_crash(desk: Desk) -> None:
    log = GapLog(desk.conn)
    log.heartbeat(20_000)
    log.stop(40_000)
    assert desk.monitors(30_000)["L"]["state"] == "shown"
    assert desk.monitors(40_000)["L"]["state"] == "not_recording"


@pytest.mark.parametrize("kind", ["all", "screen"])
@pytest.mark.parametrize("reason", ["paused", "ui_open", "stopped"])
def test_a_stopping_gap_covering_ts_makes_every_monitor_not_recording(
    desk: Desk, kind: str, reason: str
) -> None:
    GapLog(desk.conn).add(kind, reason, 16_500, 17_500)  # type: ignore[arg-type]
    covered = desk.monitors(17_000)
    assert {m["state"] for m in covered.values()} == {"not_recording"}
    assert all(m["frame_id"] is None and m["windows"] == [] for m in covered.values())
    assert desk.monitors(18_000)["L"]["state"] == "shown"


def test_the_stage_does_no_image_work(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("image work in /api/stage")

    monkeypatch.setattr(desk.frames, "image", never)
    monkeypatch.setattr(api_frames, "_render", never)
    assert desk.monitors(17_000)["L"]["frame_id"] == desk.f["l1"]  # that frame has an image


@pytest.mark.parametrize("query", ["", "?ts=abc", "?ts=1.5", "?ts=-1", f"?ts={2**62 + 1}"])
def test_a_bad_ts_is_a_422(desk: Desk, query: str) -> None:
    assert desk.raw(f"/api/stage{query}")[0] == 422


def test_the_largest_ts_is_accepted(desk: Desk) -> None:
    assert desk.raw(f"/api/stage?ts={2**62}")[0] == 200
    assert desk.raw("/api/stage?ts=0")[0] == 200


# --- /api/strip ------------------------------------------------------------------------------

DAY = "2026-11-08"


def strip(desk: Desk, query: str) -> list[Json]:
    status, body = desk.raw(f"/api/strip?{query}")
    assert status == 200, body
    return cast("list[Json]", json.loads(body)["bands"])


def test_strip_lists_the_days_bands_unclipped_and_filters_case_insensitively(desk: Desk) -> None:
    start, end = day_range_ms(DAY)
    add_visit(desk.conn, "chrome.exe", "Before", start - 3_600_000, start + 1_000)
    add_visit(desk.conn, "Code.exe", "Inside", start + 5_000, start + 9_000)
    add_visit(desk.conn, "chrome.exe", "After", end - 1_000, end + 3_600_000)
    add_visit(desk.conn, "chrome.exe", "Yesterday", start - 7_200_000, start - 3_600_000)
    add_visit(desk.conn, "chrome.exe", "Tomorrow", end + 1_000, end + 2_000)
    everything = strip(desk, f"day={DAY}")
    assert everything == [
        {
            "start_ms": start - 3_600_000,
            "end_ms": start + 1_000,
            "process": "chrome.exe",
            "title": "Before",
        },
        {
            "start_ms": start + 5_000,
            "end_ms": start + 9_000,
            "process": "Code.exe",
            "title": "Inside",
        },
        {
            "start_ms": end - 1_000,
            "end_ms": end + 3_600_000,
            "process": "chrome.exe",
            "title": "After",
        },
    ]
    assert strip(desk, f"day={DAY}&process=") == everything
    assert [b["title"] for b in strip(desk, f"day={DAY}&process=CODE.EXE")] == ["Inside"]
    assert [b["title"] for b in strip(desk, f"day={DAY}&process=chrome.exe")] == ["Before", "After"]
    assert strip(desk, "day=2026-01-01") == []


@pytest.mark.parametrize(
    "day", ["", "nonsense", "20261108", "2026-02-30", "2026-11-8x", "0001-01-01"]
)
def test_strip_rejects_a_malformed_or_unconvertible_day(desk: Desk, day: str) -> None:
    assert desk.raw(f"/api/strip?day={day}")[0] == 422


def test_strip_without_a_day_is_a_422(desk: Desk) -> None:
    assert desk.raw("/api/strip")[0] == 422


def test_a_sqlite_error_in_the_strip_is_a_500(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> list[object]:
        raise sqlcipher3.OperationalError("disk I/O error")

    monkeypatch.setattr(timeline, "app_bands", broken)
    assert desk.raw(f"/api/strip?day={DAY}")[0] == 500


def test_the_old_timeline_route_is_gone(desk: Desk) -> None:
    assert desk.raw(f"/api/timeline?day={DAY}")[0] == 404
