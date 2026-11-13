"""Capture health, end to end: the audio device clock, the grab record, what capture publishes,
and how the status API, the tray and the sidebar present it."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from api_client import Client
from audio_fakes import FakeDetector, FakeDevices, FakeSessions, RecorderRig, speech
from capture_fakes import FakeMedia, FakeOcr, FakeScreen, FakeUia, FakeWindows, window
from waits import wait_for

from akashic import app, child, ipc, tray
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.capture import audio as audio_module
from akashic.capture import main as capture_main
from akashic.capture.audio import AudioCapture
from akashic.capture.browser_state import BrowserState
from akashic.capture.exclusions import Exclusions
from akashic.capture.health import (
    AUDIO_UNAVAILABLE,
    DEVICE_DOWN_S,
    SCREEN_IMAGES_FAILING,
    CaptureStatus,
)
from akashic.capture.ports import AudioError, AudioPlatform, CapturePlatform
from akashic.capture.ocr_gate import OcrGate
from akashic.capture.screen import GRAB_FAILURE_RUN, ScreenCapture
from akashic.config import Exclusions as UserExclusions
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.debuginfo import ProcessInfo
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.sessions import GameLog

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

    from akashic.capture.vad import SpeechDetector
    from akashic.config import Paths

UI_DIR = Path(__file__).resolve().parents[1] / "src" / "akashic" / "ui"
INF = float("inf")


# --- the audio device clock ------------------------------------------------------------------


def _run(rig: RecorderRig) -> tuple[threading.Event, threading.Thread]:
    stop = threading.Event()
    thread = threading.Thread(target=rig.recorder.run, args=(stop, threading.Event()), daemon=True)
    thread.start()
    return stop, thread


def _down(rig: RecorderRig, since: float = -INF) -> float:
    return rig.recorder.down_s(time.monotonic(), since)


def test_a_recorder_that_never_read_counts_from_the_resume() -> None:
    rig = RecorderRig()
    assert rig.recorder.down_s(1000.0, 940.0) == 60.0
    assert rig.recorder.down_s(1000.0, 1000.0) == 0.0


def test_a_read_that_times_out_with_no_data_is_a_successful_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_module, "READ_TIMEOUT_S", 0.01)
    rig = RecorderRig()  # the device opens and has nothing to say
    stop, thread = _run(rig)
    try:
        assert wait_for(lambda: _down(rig) < 5, 10)
    finally:
        stop.set()
        thread.join(timeout=5)


def test_a_device_whose_reads_always_fail_never_counts_as_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_module, "RETRY_S", 0.01)
    rig = RecorderRig()
    rig.devices.scripts["mic"] += [[AudioError("gone")] for _ in range(300)]
    stop, thread = _run(rig)
    try:
        assert wait_for(lambda: len(rig.devices.opened) >= 4, 10)  # opened and failed, again
        assert _down(rig) == INF
        assert rig.recorder.down_s(time.monotonic(), time.monotonic() - 61) >= DEVICE_DOWN_S
    finally:
        stop.set()
        thread.join(timeout=5)


def test_a_device_that_cannot_be_acquired_never_counts_as_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_module, "RETRY_S", 0.01)
    rig = RecorderRig()
    rig.devices.open_error = AudioError("busy")
    stop, thread = _run(rig)
    try:
        time.sleep(0.3)
        assert _down(rig) == INF
    finally:
        stop.set()
        thread.join(timeout=5)


class _AlwaysFails(FakeDetector):
    def probability(self, window: Any) -> float:
        del window
        msg = "model fault"
        raise RuntimeError(msg)


def test_a_device_that_opens_but_whose_every_iteration_faults_never_counts_as_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio_module, "RETRY_S", 0.01)
    rig = RecorderRig()
    rig.devices.scripts["mic"] += [[speech(20)] for _ in range(300)]
    rig.recorder._detector = _AlwaysFails()  # pyright: ignore[reportPrivateUsage]
    stop, thread = _run(rig)
    try:
        assert wait_for(lambda: len(rig.devices.opened) >= 3, 10)
        assert _down(rig) == INF
    finally:
        stop.set()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("since", "down"),
    [
        (940.0, ["mic_unavailable", "system_audio_unavailable"]),  # exactly 60 s counts
        (940.1, []),  # 59.9 s does not
    ],
)
def test_audio_capture_names_each_recorder_down_for_a_minute(
    since: float, down: list[str]
) -> None:
    capture = object.__new__(AudioCapture)
    rigs = [RecorderRig("mic"), RecorderRig("system")]
    capture._recorders = [r.recorder for r in rigs]  # pyright: ignore[reportPrivateUsage]
    assert list(capture.problems(1000.0, since)) == down


def test_audio_capture_reports_only_the_recorder_that_is_down() -> None:
    capture = object.__new__(AudioCapture)
    rigs = [RecorderRig("mic"), RecorderRig("system")]
    rigs[0].recorder._ok_at = 990.0  # pyright: ignore[reportPrivateUsage]
    capture._recorders = [r.recorder for r in rigs]  # pyright: ignore[reportPrivateUsage]
    assert capture.problems(1000.0, 0.0) == ("system_audio_unavailable",)


# --- the screen loop's record ---------------------------------------------------------------


@pytest.fixture
def screen_capture(paths: Paths) -> Iterator[ScreenCapture]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    yield ScreenCapture(
        screen=FakeScreen(),
        windows=FakeWindows(focused=window()),
        uia=FakeUia(),
        gate=OcrGate(FakeOcr()),
        store=FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media"))),
        exclusions=Exclusions(UserExclusions()),
        browsers=BrowserState(),
        games=GameLog(conn),
    )
    conn.close()


FAILED = ["error", "loop_error", "image_unavailable", "stored_no_image", "unchanged_no_image"]
GOOD = ["stored", "stored_black", "unchanged", "game_stored", "game_black"]
NEUTRAL = [
    "locked",
    "excluded:password",
    "excluded:app",
    "no_window",
    "duplicate_window",
    "window_changed",
    "no_monitors",
]


def _note(capture: ScreenCapture, *outcomes: str) -> None:
    for outcome in outcomes:
        capture._note(outcome)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("outcome", FAILED)
def test_each_failure_outcome_counts_toward_the_run(
    screen_capture: ScreenCapture, outcome: str
) -> None:
    _note(screen_capture, outcome, outcome)
    assert not screen_capture.images_failing(-INF)
    _note(screen_capture, outcome)
    assert screen_capture.images_failing(-INF)


@pytest.mark.parametrize("outcome", GOOD)
def test_each_success_outcome_clears_the_record(
    screen_capture: ScreenCapture, outcome: str
) -> None:
    _note(screen_capture, *["error"] * GRAB_FAILURE_RUN)
    assert screen_capture.images_failing(-INF)
    _note(screen_capture, outcome)
    assert not screen_capture.images_failing(-INF)
    _note(screen_capture, "error", "error")  # the run starts over
    assert not screen_capture.images_failing(-INF)


@pytest.mark.parametrize("outcome", NEUTRAL)
def test_neutral_outcomes_neither_clear_nor_add(
    screen_capture: ScreenCapture, outcome: str
) -> None:
    _note(screen_capture, "error", "error", outcome, "error")
    assert screen_capture.images_failing(-INF)  # not cleared, and the run did not restart
    _note(screen_capture, outcome, outcome, outcome)
    assert screen_capture.images_failing(-INF)


def test_neutral_outcomes_alone_never_make_a_failure(screen_capture: ScreenCapture) -> None:
    _note(screen_capture, *NEUTRAL * 3)
    assert not screen_capture.images_failing(-INF)


def test_mixed_failures_in_the_order_produced(screen_capture: ScreenCapture) -> None:
    _note(screen_capture, "error", "image_unavailable", "stored", "loop_error", "stored_no_image")
    assert not screen_capture.images_failing(-INF)
    _note(screen_capture, "unchanged_no_image")
    assert screen_capture.images_failing(-INF)


def test_all_three_failures_must_be_after_the_resume() -> None:
    clock = iter(range(10))
    capture = object.__new__(ScreenCapture)
    capture.stats = Counter()
    capture._failures = deque(maxlen=GRAB_FAILURE_RUN)  # pyright: ignore[reportPrivateUsage]
    capture._clock = lambda: float(next(clock))  # type: ignore[method-assign]  # pyright: ignore
    for _ in range(5):
        capture._note("error")  # pyright: ignore[reportPrivateUsage]  # at 0..4; kept: 2, 3, 4
    assert capture.images_failing(2.0)
    assert not capture.images_failing(2.5)  # one of the three is from before the resume


# --- what capture publishes ------------------------------------------------------------------


def _platform() -> CapturePlatform:
    return CapturePlatform(
        screen=FakeScreen(),
        windows=FakeWindows(focused=window()),
        uia=FakeUia(texts={1: ["x" * 80]}),
        ocr=FakeOcr(),
        media=FakeMedia(),
    )


@pytest.fixture
def key(paths: Paths) -> DataKey:
    k = DataKey.generate()
    conn = db.connect(paths.db_file, k.subkey("db"))
    db.migrate(conn)
    conn.close()
    return k


class _Out(io.BytesIO):
    """What the supervisor sees, plus the process_info problems at the moment of each write."""

    def __init__(self, paths: Paths, key: DataKey) -> None:
        super().__init__()
        self._paths, self._key = paths, key
        self.rows: list[tuple[str, object]] = []

    def write(self, data: Any) -> int:
        n = super().write(data)
        row: object = "unreadable"
        try:
            conn = db.connect(self._paths.db_file, self._key.subkey("db"))
            try:
                info = ProcessInfo(conn).read("capture")
            finally:
                conn.close()
            row = "no row" if info is None else info.get("problems", "no key")
        except Exception:  # noqa: BLE001, S110 - recorded as unreadable
            pass
        self.rows.append((bytes(data).decode(), row))
        return n

    def states(self) -> list[str]:
        return list(ipc.read_statuses(io.BytesIO(self.getvalue())))


def _run_capture(
    paths: Paths, key: DataKey, *, paused: bool = False, run_s: float = 0.8, **kw: object
) -> tuple[int, _Out]:
    def body(ctx: child.ChildContext) -> None:
        threading.Timer(run_s, ctx.stop.set).start()
        capture_main.run_capture(ctx, _platform(), **kw)  # type: ignore[arg-type]

    buf = io.BytesIO()
    if paused:
        ipc.send_command(buf, "pause")
    out = _Out(paths, key)
    code = child.run_child(
        "capture",
        ["--data-dir", str(paths.root)],
        body,
        io.BytesIO(key.raw() + buf.getvalue()),
        out,
    )
    return code, out


def _broken() -> SpeechDetector:
    raise FileNotFoundError


def _info(paths: Paths, key: DataKey) -> dict[str, object]:
    conn = db.connect(paths.db_file, key.subkey("db"))
    try:
        info = ProcessInfo(conn).read("capture")
    finally:
        conn.close()
    assert info is not None
    return info


def _audio_platform() -> AudioPlatform:
    return AudioPlatform(FakeDevices(), FakeSessions())


def test_an_unavailable_audio_stack_reports_degraded_with_its_problem(
    paths: Paths, key: DataKey
) -> None:
    code, out = _run_capture(paths, key, audio_platform=_audio_platform(), detector_factory=_broken)
    assert code == child.EXIT_OK
    assert _info(paths, key)["problems"] == [AUDIO_UNAVAILABLE]
    states = out.states()
    assert "degraded" in states
    assert states[-1] == "stopping"
    assert "ready" not in states[states.index("degraded") :]  # no spurious ready after it


def test_the_problems_are_in_the_row_before_degraded_is_sent(paths: Paths, key: DataKey) -> None:
    _, out = _run_capture(paths, key, audio_platform=_audio_platform(), detector_factory=_broken)
    seen = [row for data, row in out.rows if "degraded" in data]
    assert seen
    assert all(row == [AUDIO_UNAVAILABLE] for row in seen), seen


def test_a_paused_start_keeps_the_sticky_problem_in_the_row(paths: Paths, key: DataKey) -> None:
    code, out = _run_capture(
        paths, key, paused=True, audio_platform=_audio_platform(), detector_factory=_broken
    )
    assert code == child.EXIT_OK
    assert "degraded" not in out.states()  # paused wins
    assert _info(paths, key)["problems"] == [AUDIO_UNAVAILABLE]


def test_working_audio_reports_no_problem(paths: Paths, key: DataKey) -> None:
    code, out = _run_capture(
        paths, key, audio_platform=_audio_platform(), detector_factory=FakeDetector
    )
    assert code == child.EXIT_OK
    assert _info(paths, key)["problems"] == []
    assert "degraded" not in out.states()


def test_audio_switched_off_is_never_degraded(paths: Paths, key: DataKey) -> None:
    (paths.root / "config.toml").write_text("[audio]\nenabled = false\n", encoding="utf-8")
    code, out = _run_capture(paths, key, audio_platform=_audio_platform(), detector_factory=_broken)
    assert code == child.EXIT_OK
    assert _info(paths, key)["problems"] == []
    assert "degraded" not in out.states()
    assert out.states()[-1] == "stopping"


def test_no_audio_platform_is_not_a_problem(paths: Paths, key: DataKey) -> None:
    code, out = _run_capture(paths, key)
    assert code == child.EXIT_OK
    assert "degraded" not in out.states()


def test_shutdown_sends_no_second_ready_before_stopping(paths: Paths, key: DataKey) -> None:
    _, out = _run_capture(paths, key)
    assert out.states().count("ready") == 1
    assert out.states()[-1] == "stopping"


def test_disabled_capture_never_reports_degraded(
    paths: Paths, key: DataKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKASHIC_CAPTURE", "0")
    out = io.BytesIO()

    def body(ctx: child.ChildContext) -> None:
        threading.Timer(0.3, ctx.stop.set).start()
        capture_main._body(ctx)  # pyright: ignore[reportPrivateUsage]

    child.run_child("capture", ["--data-dir", str(paths.root)], body, io.BytesIO(key.raw()), out)
    assert "degraded" not in list(ipc.read_statuses(io.BytesIO(out.getvalue())))


def _fake_capture(*, failing: bool) -> Any:
    return SimpleNamespace(images_failing=lambda since: failing)


def _fake_audio(*codes: str) -> Any:
    return SimpleNamespace(problems=lambda now, since: codes)


def _status(*, audio_failed: bool) -> CaptureStatus:
    return CaptureStatus(lambda state, *, epoch: True, lambda: 0, audio_failed=audio_failed)


def test_active_problems_while_running_are_all_the_detected_ones() -> None:
    problems = capture_main._active_problems(  # pyright: ignore[reportPrivateUsage]
        _status(audio_failed=True),
        _fake_capture(failing=True),
        _fake_audio("mic_unavailable", "system_audio_unavailable"),
        paused=False,
        since=0.0,
    )
    assert problems == [
        AUDIO_UNAVAILABLE,
        "mic_unavailable",
        "system_audio_unavailable",
        SCREEN_IMAGES_FAILING,
    ]


def test_active_problems_while_paused_are_only_the_sticky_one() -> None:
    active = capture_main._active_problems  # pyright: ignore[reportPrivateUsage]
    args = (_fake_capture(failing=True), _fake_audio("mic_unavailable"))
    assert active(_status(audio_failed=True), *args, paused=True, since=0.0) == [AUDIO_UNAVAILABLE]
    assert active(_status(audio_failed=False), *args, paused=True, since=0.0) == []


def test_active_problems_with_no_audio_recorders() -> None:
    problems = capture_main._active_problems(  # pyright: ignore[reportPrivateUsage]
        _status(audio_failed=False), _fake_capture(failing=False), None, paused=False, since=0.0
    )
    assert problems == []


def test_a_health_reading_that_raises_is_skipped_not_fatal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = cast("Any", SimpleNamespace(health_snapshot=lambda: (0, False, 0.0)))
    bad = SimpleNamespace(images_failing=lambda since: 1 / 0)
    with caplog.at_level("WARNING"):
        got = capture_main._collect(  # pyright: ignore[reportPrivateUsage]
            ctx, _status(audio_failed=False), cast("Any", bad), None, cast("Any", None)
        )
    assert got is None
    assert "collecting capture health failed: ZeroDivisionError" in caplog.text


# --- the status API ---------------------------------------------------------------------------


class _Api:
    def __init__(self, client: Client, states: dict[str, str]) -> None:
        self.client, self.states = client, states

    def status(self) -> dict[str, Any]:
        code, body = self.client.call("/api/status")
        assert code == 200
        return cast("dict[str, Any]", body)

    def publish_raw(self, data: object) -> None:
        text = data if isinstance(data, str) else json.dumps(data)
        self.client.conn.execute(
            "INSERT OR REPLACE INTO process_info (name, updated_ms, data) VALUES ('capture', 1, ?)",
            (text,),
        )
        self.client.conn.commit()


@pytest.fixture
def status_api(paths: Paths, conn: Connection) -> Iterator[_Api]:
    k = DataKey.generate()
    frames = FrameStore(conn, MediaStore(paths.media_dir, k.subkey("media")))
    states = {"capture": "degraded"}
    token = new_token()
    controls = Controls(lambda: dict(states), lambda: None, lambda: None)
    server = ApiServer(Services(conn, frames, token, controls))
    server.start()
    yield _Api(Client(server, token, conn), states)
    server.stop()


def test_the_status_lists_the_problems_of_a_degraded_capture(status_api: _Api) -> None:
    status_api.publish_raw({"problems": ["mic_unavailable", SCREEN_IMAGES_FAILING]})
    assert status_api.status()["capture_problems"] == ["mic_unavailable", SCREEN_IMAGES_FAILING]


@pytest.mark.parametrize("state", ["ready", "gaming", "paused", "starting", "crashed"])
def test_the_status_has_no_problems_unless_capture_is_degraded(
    status_api: _Api, state: str
) -> None:
    status_api.publish_raw({"problems": ["mic_unavailable"]})
    status_api.states["capture"] = state
    assert status_api.status()["capture_problems"] == []


def test_the_status_has_no_problems_without_a_capture_row(status_api: _Api) -> None:
    assert status_api.status()["capture_problems"] == []


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"problems": None},
        {"problems": "mic_unavailable"},
        {"problems": {"mic_unavailable": 1}},
        {"problems": ["mic_unavailable", 3]},
        {"problems": [None]},
        {"problems": [["x"]]},
        ["not", "a", "dict"],
        "not json at all {",
        "null",
        "42",
    ],
)
def test_the_status_survives_a_malformed_problems_value(status_api: _Api, data: object) -> None:
    status_api.publish_raw(data)
    assert status_api.status()["capture_problems"] == []


def test_the_status_keeps_an_empty_problem_list_empty(status_api: _Api) -> None:
    status_api.publish_raw({"problems": []})
    assert status_api.status()["capture_problems"] == []


# --- the tray and the app ---------------------------------------------------------------------


class _Sup:
    def __init__(self, **states: str) -> None:
        self._states = states

    def states(self) -> dict[str, str]:
        return dict(self._states)


def test_the_overall_state_of_a_missing_capture_is_stopped() -> None:
    assert app._overall_state(cast("Any", _Sup())) == "stopped"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("state", ["starting", "stopping", "stopped", "crashed", "degraded"])
def test_worker_backlog_shows_only_for_ready_capture(state: str) -> None:
    got = app._overall_state(cast("Any", _Sup(capture=state, worker="backlog")))  # pyright: ignore[reportPrivateUsage]
    assert got != "backlog"


def test_the_tray_names_the_new_states() -> None:
    assert tray.LABELS["degraded"] == "Recording (with problems)"
    assert tray.LABELS["restarting"] == "Restarting…"


def test_the_degraded_icon_is_full_colour_and_the_restarting_one_is_faded() -> None:
    def top(state: str) -> int:
        value = tray.make_icon(state).getchannel("A").getextrema()[1]
        assert isinstance(value, int)
        return value

    assert top("degraded") == top("recording")
    assert top("restarting") < top("recording")


# --- the sidebar ------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_the_sidebar_words_and_dot_colours() -> None:
    js = (UI_DIR / "app.js").read_text("utf-8")
    start = js.index("const STATUS_LABELS")
    end = js.index("let signedIn")
    script = (
        js[start:end]
        + "\nconsole.log(JSON.stringify({L: STATUS_LABELS, D: DOT_CLASSES, "
        + "empty: problemSentence([]), none: problemSentence(undefined), "
        + "some: problemSentence(['mic_unavailable','screen_images_failing','made_up'])}));"
    )
    done = subprocess.run(  # noqa: S603
        [str(shutil.which("node")), "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    got = json.loads(done.stdout)
    assert got["L"] == {
        "ready": "Recording",
        "gaming": "Recording (game mode)",
        "degraded": "Recording (with problems)",
        "starting": "Starting…",
        "crashed": "Restarting…",
        "paused": "Paused",
    }
    assert got["D"]["degraded"] == "warn"
    assert "has problems" in got["empty"]
    assert got["empty"] == got["none"]
    assert "microphone" in got["some"]
    assert "screenshots" in got["some"]
    assert "made_up" not in got["some"]
    assert "undefined" not in got["some"]


def test_the_sidebar_falls_back_to_stopped_and_has_a_warning_dot() -> None:
    js = (UI_DIR / "app.js").read_text("utf-8")
    assert 'STATUS_LABELS[capture] ?? "Stopped"' in js
    assert ".dot.warn" in (UI_DIR / "app.css").read_text("utf-8")
