"""F19: capture halts while Akashic's own UI is open (IPC, child, supervisor, API, tray, e2e)."""

from __future__ import annotations

import http.client
import io
import json
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from audio_fakes import FakeDetector, FakeDevices, FakeSessions
from capture_fakes import LONG_TEXT
from screen_rig import fake_platform
from waits import wait_for

from akashic import child, ipc
from akashic import supervisor as supervisor_module
from akashic.api.server import ApiServer, Controls, Presence, Services, new_token
from akashic.capture import gaps as gaps_module
from akashic.capture import main as capture_main
from akashic.capture.health import CaptureStatus
from akashic.capture.ports import AudioPlatform
from akashic.storage import db
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.supervisor import CHILDREN, ChildSpec, Supervisor
from akashic.tray import LABELS, Tray

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from akashic.config import Paths

ROOT = Path(__file__).resolve().parents[1]

# --- IPC ---------------------------------------------------------------------------------------


def test_the_ui_commands_and_status_are_part_of_the_protocol() -> None:
    buf = io.BytesIO()
    for name in ("ui_open", "ui_closed", "pause", "resume", "stop"):
        ipc.send_command(buf, name)
    ipc.send_status(buf, "ui_paused")
    data = buf.getvalue().split(b"\n")
    assert list(ipc.read_commands(io.BytesIO(b"\n".join(data[:5]) + b"\n"))) == [
        "ui_open",
        "ui_closed",
        "pause",
        "resume",
        "stop",
    ]
    assert list(ipc.read_statuses(io.BytesIO(data[5] + b"\n"))) == ["ui_paused"]


@pytest.mark.parametrize("name", ["ui_paused", "ui", "ui_open ", "UI_OPEN", "halt"])
def test_other_command_names_are_still_protocol_errors(name: str) -> None:
    line = json.dumps({"type": "command", "name": name}).encode() + b"\n"
    with pytest.raises(ipc.ProtocolError):
        list(ipc.read_commands(io.BytesIO(line)))


@pytest.mark.parametrize("state", ["ui_open", "halted", "ui"])
def test_other_status_names_are_still_protocol_errors(state: str) -> None:
    line = json.dumps({"type": "status", "state": state}).encode() + b"\n"
    with pytest.raises(ipc.ProtocolError):
        list(ipc.read_statuses(io.BytesIO(line)))


# --- the child's halt --------------------------------------------------------------------------


def sent(out: io.BytesIO) -> list[str]:
    return list(ipc.read_statuses(io.BytesIO(out.getvalue())))


def make_context(out: io.BytesIO) -> child.ChildContext:
    unused = cast("Any", None)
    return child.ChildContext("test", unused, unused, unused, unused, _status_out=out)


def test_the_halt_is_set_while_paused_ui_open_or_the_failsafe_holds() -> None:
    ctx = make_context(io.BytesIO())
    assert not ctx.halt.is_set()
    ctx.set_ui_open(ui_open=True)
    assert ctx.halt.is_set()
    ctx.set_ui_open(ui_open=False)
    assert not ctx.halt.is_set()
    ctx.set_own_address(visible=True)
    assert ctx.halt.is_set()
    ctx.set_own_address(visible=False)
    assert not ctx.halt.is_set()
    ctx.set_paused(paused=True)
    assert ctx.halt.is_set()
    assert ctx.paused.is_set()


def test_the_reported_state_follows_the_cause_of_the_halt() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.announce_ready()
    ctx.set_ui_open(ui_open=True)
    ctx.set_paused(paused=True)
    ctx.set_ui_open(ui_open=False)
    ctx.set_paused(paused=False)
    ctx.set_own_address(visible=True)
    ctx.set_own_address(visible=False)
    assert sent(out) == ["ready", "ui_paused", "paused", "ready", "ui_paused", "ready"]


def test_ui_closed_never_clears_a_user_pause() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.announce_ready()
    ctx.set_paused(paused=True)
    ctx.set_ui_open(ui_open=True)
    ctx.set_ui_open(ui_open=False)
    assert ctx.halt.is_set()
    assert ctx.paused.is_set()
    assert sent(out) == ["ready", "paused"]


def test_resume_while_the_ui_is_open_stays_halted_as_ui_paused() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.announce_ready()
    ctx.set_ui_open(ui_open=True)
    ctx.set_paused(paused=True)
    ctx.set_paused(paused=False)
    assert ctx.halt.is_set()
    assert not ctx.paused.is_set()
    assert sent(out) == ["ready", "ui_paused", "paused", "ui_paused"]


def test_the_epoch_and_resume_time_move_only_when_the_halt_flips() -> None:
    ctx = make_context(io.BytesIO())
    epoch, halted, resumed_at = ctx.health_snapshot()
    assert not halted
    ctx.set_ui_open(ui_open=True)
    assert ctx.health_snapshot()[:2] == (epoch + 1, True)
    assert ctx.health_snapshot()[2] == resumed_at
    ctx.set_paused(paused=True)  # still halted: no flip
    ctx.set_paused(paused=False)
    ctx.set_own_address(visible=True)
    ctx.set_own_address(visible=False)
    assert ctx.health_snapshot() == (epoch + 1, True, resumed_at)
    ctx.set_ui_open(ui_open=False)
    new_epoch, halted, new_resumed_at = ctx.health_snapshot()
    assert (new_epoch, halted) == (epoch + 2, False)
    assert new_resumed_at >= resumed_at


def test_a_late_active_state_is_not_sent_while_halted() -> None:
    out = io.BytesIO()
    ctx = make_context(out)
    ctx.announce_ready()
    ctx.set_ui_open(ui_open=True)
    ctx.set_active("degraded")
    assert sent(out) == ["ready", "ui_paused"]


def test_a_change_computed_before_the_halt_flipped_is_discarded() -> None:
    ctx = make_context(io.BytesIO())
    epoch, _, _ = ctx.health_snapshot()
    ctx.set_ui_open(ui_open=True)
    ctx.set_ui_open(ui_open=False)
    assert not ctx.set_active("degraded", epoch=epoch)


def test_only_sticky_problems_are_reported_while_halted() -> None:
    def accept(state: ipc.State, *, epoch: int | None) -> bool:
        del state, epoch
        return True

    def writer_problems(since: float) -> list[str]:
        del since
        return ["writer_problem"]

    def audio_problems(now: float, since: float) -> list[str]:
        del now, since
        return ["mic_unavailable"]

    status = CaptureStatus(accept, lambda: 0, audio_failed=True)
    writer = cast("Any", SimpleNamespace(problems=writer_problems))
    audio = cast("Any", SimpleNamespace(problems=audio_problems))
    halted = capture_main._active_problems(status, writer, audio, halted=True, since=0.0)
    running = capture_main._active_problems(status, writer, audio, halted=False, since=0.0)
    assert halted == ["audio_unavailable"]
    assert "writer_problem" in running
    assert "mic_unavailable" in running


class _Pipe:
    """A parent's stdin: the key, then queued command lines; EOF after ``close``."""

    def __init__(self, key: DataKey) -> None:
        self._key = io.BytesIO(key.raw())
        self._lines: queue.Queue[bytes] = queue.Queue()

    def readinto(self, buffer: Any) -> int:  # noqa: ANN401
        return self._key.readinto(buffer)

    def readline(self, size: int = -1) -> bytes:
        del size
        return self._lines.get()

    def command(self, name: str) -> None:
        self._lines.put(json.dumps({"type": "command", "name": name}).encode() + b"\n")

    def close(self) -> None:
        self._lines.put(b"")


@pytest.fixture
def keyed(paths: Paths) -> DataKey:
    k = DataKey.generate()
    conn = db.connect(paths.db_file, k.subkey("db"))
    db.migrate(conn)
    conn.close()
    return k


def run_child_with(
    paths: Paths,
    key: DataKey,
    extra: list[str],
    body: Callable[[child.ChildContext], None],
    out: io.BytesIO,
) -> int:
    pipe = _Pipe(key)
    pipe.close()  # EOF: the body ends the child when it returns
    argv = ["--data-dir", str(paths.root), *extra]
    return child.run_child("capture", argv, body, cast("Any", pipe), out)


def test_a_child_started_with_ui_open_is_halted_before_its_body_and_announces_ui_paused(
    paths: Paths, keyed: DataKey
) -> None:
    seen: list[tuple[bool, list[str]]] = []
    out = io.BytesIO()
    code = run_child_with(
        paths, keyed, ["--ui-open"], lambda ctx: seen.append((ctx.halt.is_set(), sent(out))), out
    )
    assert code == 0
    assert seen[0][0] is True
    assert seen[0][1] == ["ui_paused"]  # nothing was sent before the announcement
    assert sent(out)[0] == "ui_paused"


def test_a_child_started_with_ui_open_and_paused_announces_paused(
    paths: Paths, keyed: DataKey
) -> None:
    halted: list[bool] = []
    out = io.BytesIO()
    run_child_with(
        paths, keyed, ["--ui-open", "--paused"], lambda ctx: halted.append(ctx.halt.is_set()), out
    )
    assert halted == [True]
    assert sent(out)[0] == "paused"


# --- the supervisor ----------------------------------------------------------------------------


@dataclass
class FakeChild:
    pid: int = 1
    alive: bool = True
    returncode: int | None = None
    stdin: io.BytesIO = field(default_factory=io.BytesIO)
    stdout: io.BytesIO = field(default_factory=io.BytesIO)

    def poll(self) -> int | None:
        return None if self.alive else 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.alive = False
        return 0

    def kill(self) -> None:
        self.alive = False


class Fleet:
    def __init__(self) -> None:
        self.specs: list[ChildSpec] = []
        self.children: list[tuple[str, FakeChild]] = []

    def spawn(self, spec: ChildSpec, data_dir: Path) -> subprocess.Popen[bytes]:
        del data_dir
        self.specs.append(spec)
        fake = FakeChild()
        self.children.append((spec.name, fake))
        return cast("subprocess.Popen[bytes]", fake)

    def commands(self, name: str) -> list[str]:
        out: list[str] = []
        for child_name, fake in self.children:
            if child_name != name:
                continue
            data = fake.stdin.getvalue()[32:]  # after the key
            out += [json.loads(line)["name"] for line in data.splitlines() if line]
        return out

    def launches(self, name: str) -> list[ChildSpec]:
        return [s for s in self.specs if s.name == name]


def make_supervisor(
    tmp_path: Path, fleet: Fleet, remembered: list[bool] | None = None
) -> Supervisor:
    return Supervisor(
        tmp_path,
        DataKey.generate(),
        CHILDREN,
        spawn=fleet.spawn,
        remember_paused=(remembered if remembered is not None else []).append,
    )


def test_set_ui_open_tells_only_capture_and_only_on_change(tmp_path: Path) -> None:
    fleet, remembered = Fleet(), list[bool]()
    sup = make_supervisor(tmp_path, fleet, remembered)
    sup.start()
    sup.set_ui_open(True)
    sup.set_ui_open(True)
    sup.set_ui_open(False)
    sup.set_ui_open(False)
    sup.set_ui_open(True)
    assert fleet.commands("capture") == ["ui_open", "ui_closed", "ui_open"]
    assert fleet.commands("worker") == []
    assert remembered == []  # the UI is not the user's Pause


def test_every_capture_launch_carries_ui_open_while_the_ui_is_open(tmp_path: Path) -> None:
    fleet = Fleet()
    sup = make_supervisor(tmp_path, fleet)
    sup.set_ui_open(True)
    sup.start()
    sup.restart()
    sup.while_stopped(lambda: None)
    capture = fleet.launches("capture")
    assert len(capture) == 3
    assert all(s.ui_open for s in capture)
    assert not any(s.ui_open for s in fleet.launches("worker"))


def test_a_crash_restart_in_poll_carries_ui_open(tmp_path: Path) -> None:
    fleet = Fleet()
    now = [1000.0]
    sup = Supervisor(
        tmp_path, DataKey.generate(), CHILDREN, spawn=fleet.spawn, clock=lambda: now[0]
    )
    sup.start()
    sup.set_ui_open(True)
    fleet.children[0][1].alive = False  # capture crashes
    sup.poll()
    now[0] += 5.0
    sup.poll()
    relaunched = fleet.launches("capture")
    assert len(relaunched) == 2
    assert relaunched[1].ui_open
    assert not any(s.ui_open for s in fleet.launches("worker"))


def test_launches_go_without_ui_open_after_the_ui_closed(tmp_path: Path) -> None:
    fleet = Fleet()
    sup = make_supervisor(tmp_path, fleet)
    sup.start()
    sup.set_ui_open(True)
    sup.set_ui_open(False)
    sup.restart()
    assert [s.ui_open for s in fleet.launches("capture")] == [False, False]


def test_a_user_pause_and_the_ui_are_independent_in_the_supervisor(tmp_path: Path) -> None:
    fleet, remembered = Fleet(), list[bool]()
    sup = make_supervisor(tmp_path, fleet, remembered)
    sup.start()
    sup.send("pause")
    sup.set_ui_open(True)
    sup.set_ui_open(False)
    sup.restart()
    assert remembered == [True]
    assert [s.start_paused for s in fleet.launches("capture")] == [False, True]


def test_the_spawned_command_line_carries_ui_open_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    def popen(argv: list[str], **_kw: object) -> object:
        seen.append(argv)
        return object()

    monkeypatch.setattr(supervisor_module.subprocess, "Popen", popen)
    supervisor_module.spawn_module(ChildSpec("capture", "x", ui_open=True), tmp_path)
    supervisor_module.spawn_module(ChildSpec("capture", "x"), tmp_path)
    assert "--ui-open" in seen[0]
    assert "--ui-open" not in seen[1]


# --- presence ----------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_a_lease_halts_for_six_seconds_and_expires_through_the_tick() -> None:
    calls: list[bool] = []
    clock = Clock()
    presence = Presence(calls.append, clock)
    presence.report("a", visible=True)
    assert calls == [True]
    clock.t += 5.0
    presence.expire()
    assert calls[-1] is True  # alive
    clock.t += 1.5
    presence.expire()
    assert calls[-1] is False  # no lease: the UI is closed


def test_a_report_refreshes_the_lease() -> None:
    calls: list[bool] = []
    clock = Clock()
    presence = Presence(calls.append, clock)
    presence.report("a", visible=True)
    clock.t += 5.0
    presence.report("a", visible=True)
    clock.t += 5.0
    presence.expire()
    assert calls[-1] is True
    clock.t += 1.5
    presence.expire()
    assert calls[-1] is False


def test_recording_halts_while_at_least_one_lease_is_alive() -> None:
    calls: list[bool] = []
    clock = Clock()
    presence = Presence(calls.append, clock)
    presence.report("a", visible=True)
    clock.t += 3.0
    presence.report("b", visible=True)
    clock.t += 4.0  # a expired, b alive
    presence.expire()
    assert calls[-1] is True
    presence.report("b", visible=False)
    assert calls[-1] is False


def test_hidden_drops_the_lease_at_once() -> None:
    calls: list[bool] = []
    presence = Presence(calls.append, Clock())
    presence.report("a", visible=True)
    presence.report("a", visible=False)
    assert calls == [True, False]


class Api:
    def __init__(self, server: ApiServer, token: str, state: list[str], presence: Presence) -> None:
        self.server, self.token, self.state, self.presence = server, token, state, presence

    def post(
        self,
        body: object,
        *,
        token: str | None = "",
        origin: str | None = None,
        path: str = "/api/presence",
    ) -> tuple[int, Any]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Cookie"] = f"akashic_session={token or self.token}"
        if origin is not None:
            headers["Origin"] = origin
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, json.loads(data) if data.startswith((b"{", b"[")) else None

    def get(self, path: str) -> Any:  # noqa: ANN401
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        conn.request("GET", path, headers={"Cookie": f"akashic_session={self.token}"})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return json.loads(data)


@pytest.fixture
def api(paths: Paths) -> Iterator[tuple[Api, list[bool], Clock]]:
    key = DataKey.generate()
    conn = db.connect(paths.db_file, key.subkey("db"))
    db.migrate(conn)
    frames = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
    state = ["ready"]
    opened: list[bool] = []
    clock = Clock()
    token = new_token()
    controls = Controls(
        lambda: {"capture": state[0], "worker": "ready"},
        lambda: None,
        lambda: None,
        set_ui_open=opened.append,
    )
    services = Services(conn, frames, token, controls)
    presence = services.presence = Presence(opened.append, clock)
    server = ApiServer(services)
    server.start()
    try:
        yield Api(server, token, state, presence), opened, clock
    finally:
        server.stop()
        conn.close()


def test_a_visible_report_opens_the_lease_and_hidden_drops_it(
    api: tuple[Api, list[bool], Clock],
) -> None:
    client, opened, _ = api
    assert client.post({"page": "p1", "visible": True})[0] == 200
    assert opened == [True]
    assert client.post({"page": "p1", "visible": False})[0] == 200
    assert opened == [True, False]


def test_leases_expire_through_the_apps_tick(api: tuple[Api, list[bool], Clock]) -> None:
    client, opened, clock = api
    client.post({"page": "p1", "visible": True})
    clock.t += 7.0
    # the app's monitor thread calls ``services.presence.expire()`` once a second
    client.presence.expire()
    assert opened[-1] is False


@pytest.mark.parametrize(
    ("state", "paused"),
    [
        ("paused", True),
        ("ui_paused", True),
        ("stopped", True),
        ("crashed", True),
        ("stopping", True),
        ("starting", False),
        ("ready", False),
        ("gaming", False),
        ("degraded", False),
    ],
)
def test_recording_paused_follows_the_capture_state(
    api: tuple[Api, list[bool], Clock], state: str, *, paused: bool
) -> None:
    client, _, _ = api
    client.state[0] = state
    assert client.post({"page": "p1", "visible": True}) == (200, {"recording_paused": paused})


def test_status_paused_is_only_the_users_pause(api: tuple[Api, list[bool], Clock]) -> None:
    client, _, _ = api
    client.state[0] = "ui_paused"
    assert client.get("/api/status")["paused"] is False
    client.state[0] = "paused"
    assert client.get("/api/status")["paused"] is True


def test_presence_without_a_session_is_401(api: tuple[Api, list[bool], Clock]) -> None:
    client, opened, _ = api
    assert client.post({"page": "p", "visible": True}, token=None)[0] == 401
    assert opened == []


def test_presence_from_another_origin_is_403(api: tuple[Api, list[bool], Clock]) -> None:
    client, opened, _ = api
    assert client.post({"page": "p", "visible": True}, origin="https://evil.example")[0] == 403
    assert opened == []


@pytest.mark.parametrize("page", ["", "x" * 65])
def test_an_empty_or_too_long_page_id_is_422(api: tuple[Api, list[bool], Clock], page: str) -> None:
    client, opened, _ = api
    assert client.post({"page": page, "visible": True})[0] == 422
    assert opened == []


def test_a_page_id_of_64_characters_is_accepted(api: tuple[Api, list[bool], Clock]) -> None:
    client, _, _ = api
    assert client.post({"page": "x" * 64, "visible": True})[0] == 200


# --- tray --------------------------------------------------------------------------------------


def test_the_tray_has_a_label_for_ui_paused_that_is_not_paused() -> None:
    assert LABELS["ui_open"] not in {LABELS["paused"], LABELS["recording"]}
    assert LABELS["ui_open"] != "Paused"


def test_the_tray_toggle_offers_pause_while_the_ui_is_open() -> None:
    state = ["ui_open"]
    tray = Tray(
        state=lambda: state[0],
        open_ui=lambda: None,
        toggle_pause=lambda: None,
        quit_app=lambda: None,
    )
    menu = cast("Any", tray._icon).menu
    toggle = menu.items[1]
    assert toggle.text == "Pause"
    state[0] = "paused"
    assert toggle.text == "Resume"


# --- end to end, with fakes --------------------------------------------------------------------


def gap_rows(paths: Paths, key: DataKey) -> list[tuple[str, str, int, int | None]]:
    conn = db.connect(paths.db_file, key.subkey("db"))
    try:
        return [
            (r[0], r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT kind, reason, start_ms, end_ms FROM capture_gaps ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


def frame_count(paths: Paths, key: DataKey) -> int:
    conn = db.connect(paths.db_file, key.subkey("db"))
    try:
        return int(conn.execute("SELECT count(*) FROM frames").fetchone()[0])
    finally:
        conn.close()


class Capture:
    """A capture child over fakes, driven by commands through a pipe, on its own thread."""

    def __init__(self, paths: Paths, key: DataKey, *extra: str) -> None:
        self.paths, self.key = paths, key
        self.pipe, self.out = _Pipe(key), io.BytesIO()
        self.devices = FakeDevices()
        self.platform = fake_platform(ocr=None, text=LONG_TEXT[0].text)
        self.code: int | None = None
        argv = ["--data-dir", str(paths.root), *extra]

        def body(ctx: child.ChildContext) -> None:
            capture_main.run_capture(
                ctx,
                self.platform,
                AudioPlatform(self.devices, FakeSessions()),
                FakeDetector,
            )

        def run() -> None:
            self.code = child.run_child("capture", argv, body, cast("Any", self.pipe), self.out)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def states(self) -> list[str]:
        return sent(self.out)

    def gaps(self, reason: str) -> list[tuple[str, str, int, int | None]]:
        return [r for r in gap_rows(self.paths, self.key) if r[1] == reason and r[0] == "all"]

    def streams_closed(self) -> bool:
        opened = [stream for _, stream in self.devices.opened]
        return bool(opened) and all(s.closed for s in opened)

    def finish(self) -> None:
        self.pipe.close()
        self.thread.join(timeout=30)
        assert not self.thread.is_alive()
        assert self.code == 0


@pytest.fixture
def fast_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gaps_module, "TICK_S", 0.05)


def test_the_ui_opening_pauses_everything_and_closing_it_resumes(
    paths: Paths, keyed: DataKey, fast_gaps: None
) -> None:
    del fast_gaps
    cap = Capture(paths, keyed)
    try:
        assert wait_for(lambda: frame_count(paths, keyed) >= 1, 30)
        assert wait_for(lambda: bool(cap.devices.opened), 30)
        cap.pipe.command("ui_open")
        assert wait_for(lambda: bool(cap.gaps("ui_open")) and cap.gaps("ui_open")[0][3] is None, 30)
        assert wait_for(cap.streams_closed, 30)
        assert cap.states()[-1] == "ui_paused"
        stored = frame_count(paths, keyed)
        opened = len(cap.devices.opened)
        wait_for(lambda: False, 1.0)  # passes go by while halted
        assert frame_count(paths, keyed) == stored
        assert len(cap.devices.opened) == opened  # no device reopened
        cap.pipe.command("ui_closed")
        assert wait_for(lambda: cap.gaps("ui_open")[0][3] is not None, 30)
        assert wait_for(lambda: len(cap.devices.opened) > opened, 30)  # audio is back
        assert wait_for(lambda: frame_count(paths, keyed) > stored, 30)  # so is the screen
        assert cap.states()[-1] == "ready"
        assert len(cap.gaps("ui_open")) == 1
    finally:
        cap.finish()


def test_a_user_pause_survives_the_ui_closing_and_the_gap_follows_it(
    paths: Paths, keyed: DataKey, fast_gaps: None
) -> None:
    del fast_gaps
    cap = Capture(paths, keyed)
    try:
        assert wait_for(lambda: frame_count(paths, keyed) >= 1, 30)
        cap.pipe.command("ui_open")
        assert wait_for(lambda: bool(cap.gaps("ui_open")), 30)
        cap.pipe.command("pause")  # a Pause during the UI halt: the row switches to paused
        assert wait_for(lambda: bool(cap.gaps("paused")) and cap.gaps("paused")[0][3] is None, 30)
        assert cap.gaps("ui_open")[0][3] is not None
        cap.pipe.command("ui_closed")
        wait_for(lambda: False, 1.0)
        assert cap.states()[-1] == "paused"
        assert cap.gaps("paused")[0][3] is None  # still open: the user's Pause holds
        stored = frame_count(paths, keyed)
        wait_for(lambda: False, 1.0)
        assert frame_count(paths, keyed) == stored
        cap.pipe.command("ui_open")
        cap.pipe.command("resume")  # a Resume while the UI is open: back to ui_open
        assert wait_for(
            lambda: len(cap.gaps("ui_open")) == 2 and cap.gaps("ui_open")[1][3] is None, 30
        )
        assert cap.gaps("paused")[0][3] is not None
        assert cap.states()[-1] == "ui_paused"
        cap.pipe.command("ui_closed")
        assert wait_for(lambda: cap.states()[-1] == "ready", 30)
        assert wait_for(lambda: cap.gaps("ui_open")[1][3] is not None, 30)
    finally:
        cap.finish()


def test_a_capture_restart_while_the_ui_is_open_stays_paused(
    paths: Paths, keyed: DataKey, fast_gaps: None
) -> None:
    del fast_gaps
    cap = Capture(paths, keyed, "--ui-open")
    try:
        assert wait_for(lambda: bool(cap.gaps("ui_open")), 30)
        assert cap.states()[0] == "ui_paused"
        wait_for(lambda: False, 1.0)
        assert frame_count(paths, keyed) == 0
        assert cap.devices.opened == []  # no device was ever opened
        cap.pipe.command("ui_closed")
        assert wait_for(lambda: frame_count(paths, keyed) >= 1, 30)
    finally:
        cap.finish()


# --- the pages ---------------------------------------------------------------------------------


def test_the_presence_logic_of_the_page_gates_recorded_content() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the local JavaScript check")
    result = subprocess.run(  # noqa: S603 - fixed local harness and source, no network
        [node, str(ROOT / "tests/presence_page_check.js"), str(ROOT / "src/akashic/ui/app.js")],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "presence page verified" in result.stdout


def test_the_audio_tab_shows_a_static_levels_note_and_no_levels_request() -> None:
    settings = (ROOT / "src/akashic/ui/settings.js").read_text("utf-8")
    assert "Levels are not available while Akashic is open." in settings
    assert "/api/settings/levels" not in settings
