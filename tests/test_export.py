"""Encrypted export end to end: the folder, its keys, validation, cancel, the routes, the live
store left alone. What is kept per table is in test_export_scope.py."""

from __future__ import annotations

import http.client
import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import sqlcipher3
from api_client import Client
from conftest import FAST_ARGON2
from export_world import (
    HIGH,
    LOW,
    PASSPHRASE,
    World,
    open_export,
    open_live,
    run_export,
)

from akashic import app as app_module
from akashic.api.export import Exports
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.config import Paths
from akashic.storage import db
from akashic.storage import export as export_module
from akashic.storage.crypto import (
    CryptoError,
    DataKey,
    WrongPassphraseError,
    unwrap_data_key,
    wrap_data_key,
    write_keyfile_atomic,
)
from akashic.storage.export import (
    CATEGORIES,
    ExportCancel,
    ExportCancelled,
    ExportError,
    ExportScope,
    validate,
)
from akashic.storage.frames import FrameStore
from akashic.storage.gaps import GapLog
from akashic.storage.media import MediaStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from sqlite3 import Connection

pytestmark = pytest.mark.usefixtures("fast_keywrap")

LIVE_PASSPHRASE = "live passphrase"  # noqa: S105 - a test value
TINY = "tiny"
BAD_BODY = "The export request is not valid."
BAD_RANGE = "The range must be whole, non-negative milliseconds."
BAD_CATEGORIES = "The categories must be a list of names."
SECRET = "SECRET-PASSPHRASE-9"  # noqa: S105 - a marker that must never be echoed


def dump(conn: Connection) -> dict[str, list[Any]]:
    """Every row of every table, in a comparable order."""
    names = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    ]
    return {
        name: sorted(conn.execute(f'SELECT * FROM "{name}"').fetchall(), key=repr)  # noqa: S608
        for name in names
    }


def media_files(root: Path) -> dict[str, bytes]:
    folder = root / "media"
    return {
        path.relative_to(folder).as_posix(): path.read_bytes()
        for path in folder.rglob("*")
        if path.is_file()
    }


def live_state(world: World) -> tuple[dict[str, list[Any]], dict[str, bytes], bytes]:
    keyfile = Paths(world.root).keyfile
    return dump(world.conn), media_files(world.root), keyfile.read_bytes()


@pytest.fixture
def live_keyfile(world: World) -> None:
    write_keyfile_atomic(
        Paths(world.root).keyfile, wrap_data_key(world.key, LIVE_PASSPHRASE, FAST_ARGON2)
    )


@dataclass
class Finished:
    target: Path
    files: int
    size: int
    missing: int
    before_ms: int
    after_ms: int
    progress: list[tuple[int, int]]


@pytest.fixture
def finished(world: World, live_keyfile: None, tmp_path: Path) -> Finished:
    del live_keyfile
    progress: list[tuple[int, int]] = []
    before = int(time.time() * 1000)
    target = tmp_path / "out"
    result = run_export(world, target, report=lambda d, t: progress.append((d, t)))
    return Finished(
        target,
        result.files,
        result.bytes,
        result.missing_files,
        before,
        int(time.time() * 1000),
        progress,
    )


def test_a_finished_export_holds_only_the_keyfile_database_and_media(
    world: World, finished: Finished
) -> None:
    found = {p.relative_to(finished.target).as_posix() for p in finished.target.rglob("*")}
    expected_media = {f"media/{name}" for name in media_files(world.root)}
    folders = {"db", "media"} | {
        parent.as_posix()
        for name in expected_media
        for parent in Path(name).parents
        if parent.as_posix() != "."
    }
    assert found == {"db/keyfile", "db/akashic.db"} | expected_media | folders
    assert not finished.target.with_name("out.partial").exists()


def test_the_export_opens_with_its_own_passphrase_and_keys_only(
    world: World, finished: Finished
) -> None:
    paths = Paths(finished.target)
    keyfile = paths.keyfile.read_bytes()
    with pytest.raises(WrongPassphraseError):
        unwrap_data_key(keyfile, LIVE_PASSPHRASE)
    new_key = unwrap_data_key(keyfile, PASSPHRASE)
    assert new_key.raw() != world.key.raw()
    with pytest.raises(db.DatabaseKeyError):
        db.connect(paths.db_file, world.key.subkey("db"))
    db.connect(paths.db_file, new_key.subkey("db")).close()


def test_every_media_file_decrypts_with_the_new_key_under_its_own_path(
    world: World, finished: Finished
) -> None:
    with open_export(finished.target) as (_conn, new_key):
        new = MediaStore(Paths(finished.target).media_dir, new_key.subkey("media"))
        old_key_store = MediaStore(Paths(finished.target).media_dir, world.key.subkey("media"))
        for path, data in world.files.values():
            assert new.read(path) == data
            with pytest.raises(CryptoError):
                old_key_store.read(path)


def test_the_database_has_the_live_schema_and_version_and_stays_consistent(
    world: World, finished: Finished
) -> None:
    master = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
    with open_export(finished.target) as (conn, _key):
        assert conn.execute(master).fetchall() == world.conn.execute(master).fetchall()
        assert db.schema_version(conn) == db.schema_version(world.conn)
        before = db.schema_version(conn)
        db.migrate(conn)  # raises StaleSchemaError when the copy were not current
        assert db.schema_version(conn) == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for index in (
            "frame_windows_fts",
            "media_sessions_fts",
            "audio_segments_fts",
            "episodes_fts",
        ):
            conn.execute(f"INSERT INTO {index} ({index}) VALUES ('integrity-check')")  # noqa: S608


def test_the_result_counts_files_and_folder_bytes_and_progress_reaches_the_total(
    world: World, finished: Finished
) -> None:
    on_disk = sum(p.stat().st_size for p in finished.target.rglob("*") if p.is_file())
    assert (finished.files, finished.missing, finished.size) == (len(world.files), 0, on_disk)
    done = [d for d, _ in finished.progress]
    assert finished.progress[0] == (0, 4)
    assert done == sorted(done)
    assert finished.progress[-1] == (4 + len(world.files),) * 2


def test_the_live_store_is_unchanged_by_a_successful_export(
    world: World, live_keyfile: None, tmp_path: Path
) -> None:
    del live_keyfile
    before = live_state(world)
    run_export(world, tmp_path / "out")
    assert live_state(world) == before


def test_the_snapshot_closes_open_gaps_and_leaves_one_stopped_gap_and_a_heartbeat(
    finished: Finished,
) -> None:
    with open_export(finished.target) as (conn, _key):
        ((key, value),) = conn.execute("SELECT key, value FROM state").fetchall()
        assert key == "capture_alive_ms"
        assert finished.before_ms <= value <= finished.after_ms
        open_gaps = conn.execute(
            "SELECT kind, reason, start_ms FROM capture_gaps WHERE end_ms IS NULL"
        ).fetchall()
        assert open_gaps == [("all", "stopped", value)]
        closed = conn.execute(
            "SELECT end_ms FROM capture_gaps WHERE reason = 'ocr_unavailable'"
        ).fetchone()
        assert closed == (value,)
        GapLog(conn).recover(value + 1_000)
        assert conn.execute(
            "SELECT count(*) FROM capture_gaps WHERE reason = 'crashed'"
        ).fetchone() == (0,)


def test_rows_captured_after_the_snapshot_land_in_the_live_store_only(
    world: World, tmp_path: Path
) -> None:
    gone_path, _ = world.files["shot_before"]
    late: list[int] = []

    def capture(done: int, _total: int) -> None:
        if done == 1 and not late:  # the snapshot is taken
            late.append(
                world.conn.execute(
                    "INSERT INTO frames (ts_ms, monitor_id, trigger) VALUES (?, 0, 'idle')",
                    (HIGH + 1,),
                ).lastrowid
                or 0
            )
            world.media.delete(gone_path)

    result = run_export(world, tmp_path / "out", report=capture)
    assert world.conn.execute("SELECT count(*) FROM frames WHERE id = ?", late).fetchone() == (1,)
    with open_export(tmp_path / "out") as (conn, _key):
        assert conn.execute("SELECT count(*) FROM frames WHERE id = ?", late).fetchone() == (0,)
        # The file deleted meanwhile is counted and its reference cleared.
        assert result.missing_files == 1
        assert result.files == len(world.files) - 1
        row = conn.execute(
            "SELECT image_path FROM frames WHERE id = ?", (world.ids["frame_before"],)
        ).fetchone()
        assert row == (None,)


def test_an_empty_target_folder_is_filled_and_a_new_one_is_created(
    world: World, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    run_export(world, empty)
    assert (empty / "db" / "keyfile").is_file()
    run_export(world, tmp_path / "fresh")
    assert (tmp_path / "fresh" / "db" / "keyfile").is_file()


def test_a_failed_export_leaves_nothing_and_keeps_an_empty_target_folder(
    world: World, live_keyfile: None, tmp_path: Path
) -> None:
    del live_keyfile
    target = tmp_path / "empty"
    target.mkdir()
    path, _ = world.files["audio_in"]
    victim = world.root / "media" / (path + ".enc")
    victim.write_bytes(b"not an encrypted file")
    before = live_state(world)
    with pytest.raises(CryptoError):
        run_export(world, target)
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert not target.with_name("empty.partial").exists()
    assert live_state(world) == before
    other = tmp_path / "new"
    with pytest.raises(CryptoError):
        run_export(world, other)
    assert not other.exists()
    assert not other.with_name("new.partial").exists()


def test_a_failed_final_rename_restores_the_users_empty_folder(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "empty"
    target.mkdir()
    fail_partial_renames(monkeypatch, PermissionError(), 10_000)
    with pytest.raises(PermissionError):
        run_export(world, target)
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert not target.with_name("empty.partial").exists()


def fail_partial_renames(monkeypatch: pytest.MonkeyPatch, error: OSError, times: int) -> list[int]:
    """Make the rename of a .partial folder raise ``error`` ``times`` times, then work."""
    monkeypatch.setattr(export_module, "_RENAME_RETRY_SECONDS", 0)
    real_rename = Path.rename
    calls: list[int] = []

    def flaky(self: Path, destination: Path) -> Path:
        if not self.name.endswith(".partial"):
            return real_rename(self, destination)
        calls.append(1)
        if len(calls) <= times:
            raise error
        return real_rename(self, destination)

    monkeypatch.setattr(Path, "rename", flaky)
    return calls


def test_a_rename_blocked_for_a_while_succeeds_on_a_later_try(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out"
    calls = fail_partial_renames(monkeypatch, PermissionError(), export_module._RENAME_TRIES - 1)
    run_export(world, target)
    assert len(calls) == export_module._RENAME_TRIES
    assert (target / "db" / "keyfile").is_file()
    assert not target.with_name("out.partial").exists()


def test_a_rename_blocked_on_every_try_fails_after_the_last_and_restores_the_folder(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "empty"
    target.mkdir()
    calls = fail_partial_renames(monkeypatch, PermissionError(), 10_000)
    with pytest.raises(PermissionError):
        run_export(world, target)
    assert len(calls) == export_module._RENAME_TRIES
    assert target.is_dir()
    assert list(target.iterdir()) == []
    assert not target.with_name("empty.partial").exists()


def test_an_os_error_other_than_permission_is_not_retried(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "out"
    calls = fail_partial_renames(monkeypatch, OSError("disk"), 10_000)
    with pytest.raises(OSError, match="disk") as caught:
        run_export(world, target)
    assert not isinstance(caught.value, PermissionError)
    assert len(calls) == 1
    assert not target.exists()
    assert not target.with_name("out.partial").exists()


@pytest.mark.parametrize("target_existed", [False, True], ids=["no_target", "empty_target"])
def test_a_cancel_during_the_rename_retries_ends_cancelled_without_a_partial(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, target_existed: bool
) -> None:
    target = tmp_path / "out"
    if target_existed:
        target.mkdir()
    cancel = ExportCancel()
    calls = fail_partial_renames(monkeypatch, PermissionError(), 10_000)
    real_sleep = export_module.sleep

    def cancelling_sleep(seconds: float) -> None:
        real_sleep(seconds)
        if len(calls) == 2:
            cancel.cancel()

    monkeypatch.setattr(export_module, "sleep", cancelling_sleep)
    with pytest.raises(ExportCancelled):
        run_export(world, target, cancel=cancel)
    assert len(calls) == 2
    if target_existed:
        assert target.is_dir()
        assert list(target.iterdir()) == []
    else:
        assert not target.exists()
    assert not target.with_name("out.partial").exists()


def test_a_folder_that_cannot_be_recreated_does_not_replace_the_rename_error(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "empty"
    target.mkdir()
    fail_partial_renames(monkeypatch, PermissionError("rename"), 10_000)
    real_mkdir = Path.mkdir

    def refuse_target(self: Path, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        if self == target:
            raise PermissionError("mkdir")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse_target)
    with pytest.raises(PermissionError, match="rename"):
        run_export(world, target)
    assert not target.with_name("empty.partial").exists()


@pytest.mark.parametrize("cancel_at", [0, 1, 2, 8, 15, 16])
def test_a_cancel_at_any_step_removes_the_partial_and_leaves_the_live_store_alone(
    world: World, live_keyfile: None, tmp_path: Path, cancel_at: int
) -> None:
    del live_keyfile
    before = live_state(world)
    cancel = ExportCancel()
    target = tmp_path / "out"

    def stop(done: int, _total: int) -> None:
        if done == cancel_at:
            cancel.cancel()

    with pytest.raises(ExportCancelled):
        run_export(world, target, cancel=cancel, report=stop)
    assert not target.exists()
    assert not target.with_name("out.partial").exists()
    assert live_state(world) == before


def test_a_cancel_interrupts_the_running_statement() -> None:
    cancel = ExportCancel()
    conn = sqlcipher3.connect(":memory:", check_same_thread=False)
    errors: list[BaseException] = []
    started = threading.Event()

    def spin() -> None:
        cancel.attach(conn)
        started.set()
        try:
            conn.execute(
                "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c)"
                " SELECT count(*) FROM c"
            ).fetchall()
        except sqlcipher3.OperationalError as exc:
            errors.append(exc)

    thread = threading.Thread(target=spin)
    thread.start()
    assert started.wait(5)
    time.sleep(0.2)
    cancel.cancel()
    thread.join(10)
    assert not thread.is_alive()
    assert errors
    with pytest.raises(ExportCancelled):
        cancel.attach(conn)
    conn.close()


# --- validation ---


def refused(tmp_path: Path, target: Path | None = None, **given: Any) -> ExportError:  # noqa: ANN401
    arguments: dict[str, Any] = {
        "passphrase": PASSPHRASE,
        "scope": ExportScope(),
        "data_dir": tmp_path / "data",
    }
    arguments.update(given)
    with pytest.raises(ExportError) as raised:
        validate(target or tmp_path / "out", **arguments)
    return raised.value


def test_a_valid_request_passes_validation(tmp_path: Path) -> None:
    validate(tmp_path / "out", PASSPHRASE, ExportScope(1, 2, CATEGORIES), tmp_path / "data")
    (tmp_path / "empty").mkdir()
    validate(tmp_path / "empty", PASSPHRASE, ExportScope(), tmp_path / "data")


def test_a_relative_target_or_a_drive_root_is_refused(tmp_path: Path) -> None:
    refused(tmp_path, Path("relative") / "folder")
    refused(tmp_path, Path(tmp_path.anchor))


def test_a_missing_parent_folder_is_refused(tmp_path: Path) -> None:
    refused(tmp_path, tmp_path / "nowhere" / "out")


def test_a_file_or_a_non_empty_folder_as_target_is_refused(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x")
    refused(tmp_path, tmp_path / "file")
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    refused(tmp_path, tmp_path / "full")


def test_the_live_data_folder_and_anything_inside_it_is_refused(tmp_path: Path) -> None:
    (tmp_path / "data" / "inner").mkdir(parents=True)
    refused(tmp_path, tmp_path / "data")
    refused(tmp_path, tmp_path / "data" / "inner")
    refused(tmp_path, tmp_path / "data" / "new")


def test_an_existing_partial_folder_is_refused_and_left_alone(tmp_path: Path) -> None:
    partial = tmp_path / "out.partial"
    partial.mkdir()
    (partial / "keep").write_text("x")
    refused(tmp_path)
    assert (partial / "keep").read_text() == "x"


def test_a_short_passphrase_is_refused(tmp_path: Path) -> None:
    refused(tmp_path, passphrase=TINY)


def test_a_range_that_is_empty_or_backwards_is_refused(tmp_path: Path) -> None:
    refused(tmp_path, scope=ExportScope(5, 5))
    refused(tmp_path, scope=ExportScope(6, 5))
    validate(tmp_path / "out", PASSPHRASE, ExportScope(5, None), tmp_path / "data")
    validate(tmp_path / "out", PASSPHRASE, ExportScope(None, 5), tmp_path / "data")


def test_an_unknown_category_is_refused(tmp_path: Path) -> None:
    refused(tmp_path, scope=ExportScope(categories=frozenset({"screenshots", "diary"})))


def test_a_target_folder_that_cannot_be_listed_is_refused_without_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "unreadable-folder-name"
    target.mkdir()

    def deny(self: Path) -> Iterator[Path]:
        raise PermissionError(13, "denied", str(self))

    monkeypatch.setattr(Path, "iterdir", deny)
    error = refused(tmp_path, target)
    assert str(error) == "The export folder cannot be read."
    assert "unreadable-folder-name" not in str(error)
    assert error.__cause__ is None


def test_a_refusal_never_contains_the_input(tmp_path: Path) -> None:
    target = tmp_path / "unique-folder-name" / "out"
    for error in (
        refused(tmp_path, target),
        refused(tmp_path, passphrase=TINY),
        refused(tmp_path, scope=ExportScope(7_654_321, 1)),
    ):
        text = str(error)
        assert "unique-folder-name" not in text
        assert "tiny" not in text
        assert "7654321" not in text


# --- routes ---


@dataclass
class Rig:
    client: Client
    exports: Exports
    services: Services
    world: World
    target: Path
    gate: threading.Event
    failure: list[BaseException]

    def body(self, **changes: object) -> dict[str, object]:
        body: dict[str, object] = {"target": str(self.target), "passphrase": PASSPHRASE}
        body.update(changes)
        return body

    def raw_post(self, payload: bytes) -> tuple[int, str]:
        request = http.client.HTTPConnection("127.0.0.1", self.client.server.port, timeout=10)
        request.request(
            "POST",
            "/api/export",
            body=payload,
            headers={
                "Cookie": f"akashic_session={self.client.token}",
                "Content-Type": "application/json",
            },
        )
        response = request.getresponse()
        text = response.read().decode()
        request.close()
        return response.status, text

    def wait(self, *states: str, timeout_s: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.client.call("/api/export")[1]
            if status["state"] in states:
                return cast("dict[str, Any]", status)
            assert time.monotonic() < deadline, status
            time.sleep(0.05)


@pytest.fixture
def rig(world: World, tmp_path: Path) -> Iterator[Rig]:
    gate = threading.Event()
    gate.set()
    failure: list[BaseException] = []

    def open_db() -> Connection:
        assert gate.wait(30)
        if failure:
            raise failure[0]
        return open_live(world)

    exports = Exports(open_db, world.media, world.root)
    token = new_token()
    services = Services(
        world.conn,
        FrameStore(world.conn, world.media),
        token,
        Controls(dict, lambda: None, lambda: None),
        exports=exports,
    )
    server = ApiServer(services)
    server.start()
    yield Rig(
        Client(server, token, world.conn), exports, services, world, tmp_path / "out", gate, failure
    )
    gate.set()
    exports.cancel_and_wait(30)
    server.stop()


def test_the_status_is_idle_before_the_first_export(rig: Rig) -> None:
    assert rig.client.call("/api/export") == (
        200,
        {"state": "idle", "done": 0, "total": 0, "error": None, "target": None, "result": None},
    )


def test_a_valid_post_returns_202_and_the_export_finishes(rig: Rig) -> None:
    status, body = rig.client.call("/api/export", "POST", rig.body(start_ms=LOW, end_ms=HIGH))
    assert status == 202
    assert body["target"] == str(rig.target)
    done = rig.wait("done")
    assert done["error"] is None
    assert done["done"] == done["total"] > 0
    assert set(done["result"]) == {"files", "bytes", "missing_files"}
    assert (rig.target / "db" / "keyfile").is_file()


def test_a_second_post_while_one_runs_is_409_and_delete_cancels_it(rig: Rig) -> None:
    rig.gate.clear()
    assert rig.client.call("/api/export", "POST", rig.body())[0] == 202
    status, _ = rig.client.call("/api/export", "POST", rig.body())
    assert status == 409
    status, body = rig.client.call("/api/export", "DELETE")
    assert status == 200
    assert body["state"] == "running"
    rig.gate.set()
    cancelled = rig.wait("cancelled")
    assert cancelled["error"] is None
    assert not rig.target.exists()
    assert not rig.target.with_name("out.partial").exists()
    status, body = rig.client.call("/api/export", "DELETE")  # nothing runs: a no-op
    assert (status, body["state"]) == (200, "cancelled")


def test_a_failed_export_reports_only_the_exception_type(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    rig.failure.append(RuntimeError(f"cannot open {rig.target} with {SECRET}"))
    with caplog.at_level(logging.DEBUG):
        assert rig.client.call("/api/export", "POST", rig.body())[0] == 202
        failed = rig.wait("failed")
    assert failed["error"] == "RuntimeError"
    assert SECRET not in caplog.text
    assert str(rig.target) not in caplog.text
    assert not rig.target.with_name("out.partial").exists()


def test_a_request_the_export_refuses_is_422_and_starts_nothing(rig: Rig) -> None:
    status, body = rig.client.call("/api/export", "POST", rig.body(passphrase=TINY))
    assert status == 422
    assert body["detail"] == "The passphrase must have at least 8 characters."
    assert rig.client.call("/api/export")[1]["state"] == "idle"


def test_an_unreadable_target_folder_is_422_with_no_path_in_body_or_log(
    rig: Rig, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.target.mkdir()
    real = Path.iterdir

    def deny(self: Path) -> Iterator[Path]:
        if self == rig.target:
            raise PermissionError(13, "denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", deny)
    with caplog.at_level(logging.DEBUG):
        status, body = rig.client.call("/api/export", "POST", rig.body())
    assert status == 422
    assert body["detail"] == "The export folder cannot be read."
    assert str(rig.target) not in json.dumps(body) + caplog.text
    assert rig.client.call("/api/export")[1]["state"] == "idle"


def test_the_failed_message_in_the_ui_is_a_plain_sentence() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/akashic/ui/settings.js").read_text("utf-8")
    line = next(x for x in source.splitlines() if 's.state === "failed") outcome.textContent' in x)
    assert "s.error" not in line
    assert "The export failed." in line


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"target": 5}, BAD_BODY),
        ({"target": None}, BAD_BODY),
        ({"passphrase": 5}, BAD_BODY),
        ({"passphrase": None}, BAD_BODY),
        ({"start_ms": "1"}, BAD_RANGE),
        ({"start_ms": True}, BAD_RANGE),
        ({"start_ms": -1}, BAD_RANGE),
        ({"start_ms": 1.5}, BAD_RANGE),
        ({"end_ms": "1"}, BAD_RANGE),
        ({"end_ms": False}, BAD_RANGE),
        ({"end_ms": -1}, BAD_RANGE),
        ({"categories": "screenshots"}, BAD_CATEGORIES),
        ({"categories": [1]}, BAD_CATEGORIES),
        ({"categories": {"a": 1}}, BAD_CATEGORIES),
    ],
)
def test_a_malformed_body_field_is_a_fixed_422_that_echoes_nothing(
    rig: Rig, caplog: pytest.LogCaptureFixture, changes: dict[str, object], message: str
) -> None:
    body = rig.body(passphrase=SECRET)
    body.update(changes)
    with caplog.at_level(logging.DEBUG):
        status, text = rig.raw_post(json.dumps(body).encode())
    assert status == 422
    assert json.loads(text) == {"detail": message}
    assert SECRET not in text
    assert str(rig.target) not in text
    assert SECRET not in caplog.text
    assert str(rig.target) not in caplog.text
    assert rig.client.call("/api/export")[1]["state"] == "idle"


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'"text"', b"5", b"null", b""])
def test_a_body_that_is_not_a_json_object_is_a_fixed_422(rig: Rig, payload: bytes) -> None:
    status, text = rig.raw_post(payload)
    assert status == 422
    assert json.loads(text) == {"detail": BAD_BODY}
    assert rig.client.call("/api/export")[1]["state"] == "idle"


def test_a_missing_target_or_passphrase_is_422(rig: Rig) -> None:
    assert rig.raw_post(json.dumps({"passphrase": PASSPHRASE}).encode())[0] == 422
    assert rig.raw_post(json.dumps({"target": str(rig.target)}).encode())[0] == 422


def test_an_empty_category_list_means_all_of_them(rig: Rig) -> None:
    assert rig.client.call("/api/export", "POST", rig.body(categories=[], start_ms=None))[0] == 202
    rig.wait("done")
    with open_export(rig.target) as (conn, _key):
        assert conn.execute("SELECT count(*) FROM audio_segments").fetchone()[0] == 6


def test_the_export_never_takes_the_services_db_lock(rig: Rig) -> None:
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with rig.services.db():
            held.set()
            release.wait(60)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(5)
        assert rig.client.call("/api/export", "POST", rig.body())[0] == 202
        rig.wait("done")
    finally:
        release.set()
        holder.join(10)


# --- lock and quit ---


def test_cancel_and_wait_leaves_no_thread_and_no_partial(rig: Rig) -> None:
    rig.gate.clear()
    assert rig.exports.start(str(rig.target), PASSPHRASE, ExportScope())
    threading.Timer(0.3, rig.gate.set).start()
    rig.exports.cancel_and_wait(30)
    assert rig.exports.status()["state"] == "cancelled"
    assert not [t for t in threading.enumerate() if t.name == "export"]
    assert not rig.target.with_name("out.partial").exists()


class Recorder:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def cancel_and_wait(self, timeout_s: float) -> None:
        del timeout_s
        self.calls.append("export")

    def stop(self) -> None:
        self.calls.append("api")

    def shutdown(self) -> None:
        self.calls.append("children")

    def close(self) -> None:
        self.calls.append("db")


def test_tear_down_stops_the_export_before_the_api_the_children_and_the_database() -> None:
    calls: list[str] = []
    fake = cast("Any", Recorder(calls))
    app_module._tear_down(fake, fake, fake, fake, fake)
    assert calls == ["export", "api", "children", "db", "db"]


def test_tear_down_without_an_export_still_stops_the_rest() -> None:
    calls: list[str] = []
    fake = cast("Any", Recorder(calls))
    app_module._tear_down(None, fake, fake, fake, fake)
    assert calls == ["api", "children", "db", "db"]


def test_the_partial_folder_of_another_export_is_never_removed(
    world: World, tmp_path: Path
) -> None:
    other = tmp_path / "out.partial"
    other.mkdir()
    (other / "mine").write_text("x")
    with pytest.raises(FileExistsError):
        run_export(world, tmp_path / "out")
    assert (other / "mine").read_text() == "x"
    shutil.rmtree(other)


def test_a_new_data_key_is_made_for_each_export(world: World, tmp_path: Path) -> None:
    keys: list[DataKey] = []
    for name in ("one", "two"):
        run_export(world, tmp_path / name)
        with open_export(tmp_path / name) as (_conn, key):
            keys.append(key)
    assert keys[0].raw() != keys[1].raw()
