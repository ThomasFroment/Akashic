"""The app's small decisions: the tray word, why Ask is blocked, the autostart command, and the
list of recordings that Settings > Speech offers for a comparison sample."""

from __future__ import annotations

import http.client
import json
import sys
from typing import TYPE_CHECKING, cast

import pytest
from memory_data import add_segment

from akashic import app
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.config import DEFAULT_CHAT_MODEL, DEFAULT_CONFIG_TOML
from akashic.pipeline import models
from akashic.storage.crypto import DataKey
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from sqlite3 import Connection

    from akashic.config import Paths
    from akashic.supervisor import Supervisor


class FakeSupervisor:
    def __init__(self, states: dict[str, str]) -> None:
        self._states = states

    def states(self) -> dict[str, str]:
        return dict(self._states)


def overall(**states: str) -> str:
    return app._overall_state(cast("Supervisor", FakeSupervisor(states)))  # pyright: ignore[reportPrivateUsage]


# --- the tray word -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("capture", "expected"),
    [
        ("ready", "recording"),
        ("gaming", "gaming"),
        ("paused", "paused"),
        ("starting", "starting"),
        ("stopping", "stopped"),
        ("stopped", "stopped"),
        ("crashed", "restarting"),
        ("degraded", "degraded"),
    ],
)
def test_the_tray_shows_what_capture_is_doing(capture: str, expected: str) -> None:
    assert overall(capture=capture, worker="ready") == expected


def test_no_capture_process_reads_as_stopped() -> None:
    assert overall() == "stopped"
    assert overall(worker="backlog") == "stopped"


def test_a_worker_backlog_shows_only_while_recording() -> None:
    assert overall(capture="ready", worker="backlog") == "backlog"
    assert overall(capture="paused", worker="backlog") == "paused"  # the pause matters more
    assert overall(capture="gaming", worker="backlog") == "gaming"
    assert overall(capture="degraded", worker="backlog") == "degraded"


# --- why a question cannot be answered yet --------------------------------------------------


def install_runtime(paths: Paths) -> None:
    program = models.runtime_program(paths.runtime_dir, models.LLAMA_RUNTIME)
    program.parent.mkdir(parents=True)
    program.write_bytes(b"")


def install_chat(paths: Paths, name: str) -> Path:
    folder = paths.models_dir / name
    folder.mkdir(parents=True)
    (folder / "model.gguf").write_bytes(b"")
    return folder


def blocker(paths: Paths) -> str | None:
    return app._ask_blocker(paths)()  # pyright: ignore[reportPrivateUsage]


def test_ask_is_blocked_until_the_runtime_is_downloaded(paths: Paths) -> None:
    install_chat(paths, DEFAULT_CHAT_MODEL)
    reason = blocker(paths)
    assert reason is not None
    assert "runtime" in reason


def test_ask_is_blocked_until_the_configured_chat_model_has_a_gguf(paths: Paths) -> None:
    install_runtime(paths)
    reason = blocker(paths)
    assert reason is not None
    assert DEFAULT_CHAT_MODEL in reason
    (paths.models_dir / DEFAULT_CHAT_MODEL).mkdir()  # an empty folder is not a model
    assert blocker(paths) == reason
    install_chat(paths, "other-model")  # a different model than the configured one
    assert blocker(paths) == reason


def test_ask_is_ready_once_both_are_downloaded(paths: Paths) -> None:
    install_runtime(paths)
    install_chat(paths, DEFAULT_CHAT_MODEL)
    assert blocker(paths) is None


def test_the_blocker_reads_the_config_at_every_question(paths: Paths) -> None:
    install_runtime(paths)
    install_chat(paths, DEFAULT_CHAT_MODEL)
    check = app._ask_blocker(paths)  # pyright: ignore[reportPrivateUsage]
    assert check() is None
    paths.config_file.write_text(
        DEFAULT_CONFIG_TOML.replace(
            f'chat_model = "{DEFAULT_CHAT_MODEL}"', 'chat_model = "picked-later"'
        ),
        encoding="utf-8",
    )
    reason = check()
    assert reason is not None
    assert "picked-later" in reason
    install_chat(paths, "picked-later")  # downloaded while the app runs
    assert check() is None


def test_a_broken_config_falls_back_to_the_default_chat_model(paths: Paths) -> None:
    install_runtime(paths)
    paths.config_file.write_text("this is [not toml", encoding="utf-8")
    reason = blocker(paths)
    assert reason is not None
    assert DEFAULT_CHAT_MODEL in reason
    install_chat(paths, DEFAULT_CHAT_MODEL)
    assert blocker(paths) is None


# --- start with Windows ----------------------------------------------------------------------


def test_autostart_prefers_the_windowless_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    command = app.autostart_command()
    assert command.startswith(f'"{tmp_path / "pythonw.exe"}" ')
    assert command.endswith(" -m akashic.app")


def test_autostart_falls_back_to_the_running_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "with space"
    folder.mkdir()
    (folder / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(folder / "python.exe"))
    # Quoted: a path with spaces must stay one argument in the Run key.
    command = app.autostart_command()
    assert command.startswith(f'"{folder / "python.exe"}" ')
    assert command.endswith(" -m akashic.app")


# --- GET /api/stt/recordings -----------------------------------------------------------------


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.port, self.token = port, token

    def get(self, path: str, token: str | None = None) -> tuple[int, object]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers={"Cookie": f"akashic_session={token or self.token}"})
        resp = c.getresponse()
        data = resp.read()
        c.close()
        return resp.status, cast("object", json.loads(data or b"null"))


@pytest.fixture
def client(paths: Paths, conn: Connection) -> Iterator[Client]:
    key = DataKey.generate()
    token = new_token()
    server = ApiServer(
        Services(
            conn,
            FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media"))),
            token,
            Controls(dict, lambda: None, lambda: None),
            config_file=paths.config_file,
        )
    )
    server.start()
    yield Client(server.port, token)
    server.stop()


def recording(
    conn: Connection,
    start_ms: int,
    *,
    text: str | None = "bonjour tout le monde",
    stream: str = "system",
    status: str = "done",
) -> int:
    """A segment whose audio is still on disk."""
    segment = add_segment(conn, start_ms, text=text, stream=stream, status=status)
    conn.execute(
        "UPDATE audio_segments SET audio_path = ? WHERE id = ?", (f"{segment}.opus", segment)
    )
    return segment


def rows(found: object) -> list[dict[str, object]]:
    assert isinstance(found, list)
    return cast("list[dict[str, object]]", found)


def test_recordings_lists_transcribed_audio_newest_first(client: Client, conn: Connection) -> None:
    older = recording(conn, 1_000, text="bonjour Minlo", stream="mic")
    newer = recording(conn, 5_000, text="the new game")
    status, found = client.get("/api/stt/recordings")
    assert status == 200
    assert all("unknown" in str(r.pop("draft_model")) for r in rows(found))
    assert rows(found) == [
        {"segment_id": newer, "start_ms": 5_000, "stream": "system", "transcript": "the new game"},
        {"segment_id": older, "start_ms": 1_000, "stream": "mic", "transcript": "bonjour Minlo"},
    ]


def test_recordings_without_audio_or_not_done_are_not_offered(
    client: Client, conn: Connection
) -> None:
    add_segment(conn, 1_000, text="audio already expired")  # no audio_path
    recording(conn, 2_000, text=None, status="pending")
    recording(conn, 3_000, text=None, status="failed")
    silent = recording(conn, 4_000, text=None)  # done, nothing heard
    _, found = client.get("/api/stt/recordings")
    assert all("unknown" in str(r.pop("draft_model")) for r in rows(found))
    assert rows(found) == [
        {"segment_id": silent, "start_ms": 4_000, "stream": "system", "transcript": ""}
    ]


def test_recordings_can_be_asked_for_one_segment(client: Client, conn: Connection) -> None:
    first = recording(conn, 1_000)
    recording(conn, 2_000)
    _, found = client.get(f"/api/stt/recordings?segment_id={first}")
    assert [r["segment_id"] for r in rows(found)] == [first]
    _, none = client.get("/api/stt/recordings?segment_id=999")
    assert rows(none) == []


def test_recordings_are_bounded_in_count_and_length(client: Client, conn: Connection) -> None:
    for i in range(30):
        recording(conn, i * 1_000, text="mot " * 2_000)
    _, found = client.get("/api/stt/recordings")
    listed = rows(found)
    assert len(listed) == 24
    assert [r["start_ms"] for r in listed] == [i * 1_000 for i in range(29, 5, -1)]
    assert all(len(cast("str", r["transcript"])) == 6_000 for r in listed)


def test_recordings_need_the_session(client: Client, conn: Connection) -> None:
    recording(conn, 1_000)
    status, _ = client.get("/api/stt/recordings", token="not-the-session")  # noqa: S106
    assert status in {401, 403}
