"""Entry point: unlock, migrate, supervise capture and worker, serve the UI, show the tray.

The tray's message loop owns the main thread. A background thread polls the supervisor.
Quit (tray menu) stops the API server first, then the children, then wipes the key.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from akashic.api.debug import DebugSources
from akashic.api.models import Downloads
from akashic.api.server import ApiServer, Controls, Services, new_token
from akashic.config import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBEDDING_MODEL,
    Config,
    ConfigError,
    Paths,
    default_data_dir,
    load_config,
)
from akashic.log import enable_crash_log, setup_logging
from akashic.offline import apply_offline_env
from akashic.pipeline import models
from akashic.platform.win import autostart
from akashic.storage import db
from akashic.storage.audio import AudioStore
from akashic.storage.crypto import KeyfileError
from akashic.storage.frames import FrameStore
from akashic.storage.media import MediaStore
from akashic.storage.speakers import SpeakerStore
from akashic.supervisor import Supervisor
from akashic.tray import Tray
from akashic.unlock import Outcome, UnlockError, run_until_quit, unlock
from akashic.unlock_dialog import TkUnlockUI, show_error

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from akashic.storage.crypto import DataKey

log = logging.getLogger("app")


def _overall_state(supervisor: Supervisor) -> str:
    """One word for the tray: what the capture process is doing."""
    states = supervisor.states()
    state = states.get("capture", "stopped")
    if state == "ready" and states.get("worker") == "backlog":
        return "backlog"
    return {
        "ready": "recording",
        "gaming": "gaming",
        "paused": "paused",
        "starting": "starting",
    }.get(state, "stopped")


def _debug_settings(paths: Paths) -> dict[str, object]:
    """The settings most useful next to the Debug numbers, as the file says right now."""
    try:
        config = load_config(paths)
    except ConfigError as exc:
        return {"config.toml": f"invalid: {exc}"}
    return {
        "audio.energy_floor": config.audio.energy_floor,
        "audio.mic_gain": config.audio.mic_gain,
        "audio.vad_threshold": config.audio.vad_threshold,
        "audio.mic_device": config.audio.mic_device or "(default)",
        "audio.system_device": config.audio.system_device or "(default)",
        "scheduler.gpu_busy_percent": config.scheduler.gpu_busy_percent,
        "scheduler.cpu_busy_percent": config.scheduler.cpu_busy_percent,
        "ocr_language": config.ocr_language,
        "log_level": config.log_level,
    }


def _embedding_model(paths: Paths) -> str:
    try:
        return load_config(paths).search.embedding_model
    except ConfigError:
        return DEFAULT_EMBEDDING_MODEL


def _ask_blocker(paths: Paths) -> Callable[[], str | None]:
    """Why a question cannot be answered yet (a missing download), or None. Read again at every
    question, so a model fetched while the app runs is noticed."""

    def blocker() -> str | None:
        try:
            chat = load_config(paths).search.chat_model
        except ConfigError:
            chat = DEFAULT_CHAT_MODEL
        if not models.runtime_ready(paths.runtime_dir, models.LLAMA_RUNTIME):
            return "The language-model runtime is not downloaded yet: Settings > Models."
        folder = paths.models_dir / chat
        if not (folder.is_dir() and any(folder.glob("*.gguf"))):
            return f"The chat model ({chat}) is not downloaded yet: Settings > Models."
        return None

    return blocker


def autostart_command() -> str:
    """How Windows starts the app at login: this interpreter, windowless when possible."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    if windowless.is_file():
        exe = windowless
    return f'"{exe}" -m akashic.app'


def _set_autostart(enabled: bool) -> None:  # noqa: FBT001 - a setter for one flag
    if enabled:
        autostart.enable(autostart_command())
    else:
        autostart.disable()
    log.info("start with Windows %s", "on" if enabled else "off")


def _required(paths: Paths) -> list[models.Needed]:
    """What Settings > Models lists: the downloads the current config.toml needs."""
    try:
        config = load_config(paths)
    except ConfigError:
        config = Config()
    return models.required(config)


def _tear_down(api: ApiServer | None, supervisor: Supervisor, conn: sqlite3.Connection) -> None:
    """Stop the API first so no request can restart children, then the children, then the db."""
    try:
        if api is not None:
            api.stop()
    finally:
        try:
            supervisor.shutdown()
        finally:
            conn.close()


def _run(paths: Paths, key: DataKey) -> Outcome:
    """Everything that runs while the app is unlocked. Returns when the user quits or locks."""
    supervisor = Supervisor(paths.root, key)
    quitting = threading.Event()
    locking = threading.Event()

    def lock_app() -> None:
        locking.set()
        quitting.set()

    conn = db.connect(paths.db_file, key.subkey("db"))
    api: ApiServer | None = None
    try:
        log.info("schema v%d", db.migrate(conn))
        frames = FrameStore(conn, MediaStore(paths.media_dir, key.subkey("media")))
        audio = AudioStore(conn, MediaStore(paths.media_dir, key.subkey("media")))

        def restart_children() -> None:
            threading.Thread(target=supervisor.restart, name="restart", daemon=True).start()

        controls = Controls(
            supervisor.states,
            pause=lambda: supervisor.send("pause"),
            resume=lambda: supervisor.send("resume"),
            restart=restart_children,
            autostart_get=autostart.is_enabled,
            autostart_set=_set_autostart,
            while_stopped=supervisor.while_stopped,
        )
        debug = DebugSources(
            data_dir=paths.root,
            logs_dir=paths.logs_dir,
            settings=lambda: _debug_settings(paths),
        )
        server = api = ApiServer(
            Services(
                conn,
                frames,
                new_token(),
                controls,
                audio,
                debug,
                paths.config_file,
                SpeakerStore(conn),
                _embedding_model(paths),
                _ask_blocker(paths),
                Downloads(paths.models_dir, paths.runtime_dir, lambda: _required(paths)),
            )
        )
        supervisor.set_api_port(server.port)
        supervisor.start()
        server.start()

        def open_ui() -> None:
            webbrowser.open(server.open_url())

        def toggle_pause() -> None:
            if _overall_state(supervisor) == "paused":
                controls.resume()
            else:
                controls.pause()

        tray = Tray(
            state=lambda: _overall_state(supervisor),
            open_ui=open_ui,
            toggle_pause=toggle_pause,
            quit_app=quitting.set,
            lock_app=lock_app,
        )

        def monitor() -> None:
            while not quitting.wait(1.0):
                supervisor.poll()
                tray.refresh()
            tray.stop()

        threading.Thread(target=monitor, name="monitor", daemon=True).start()
        open_ui()
        tray.run()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        quitting.set()
        _tear_down(api, supervisor, conn)
    return "lock" if locking.is_set() else "quit"


def main() -> int:
    apply_offline_env(os.environ)
    paths = Paths(default_data_dir())
    try:
        config = load_config(paths)
    except ConfigError as exc:
        show_error(f"config.toml: {exc}")
        return 2
    paths.ensure()
    setup_logging(paths.logs_dir, "app", config.log_level)
    _crash_log = enable_crash_log(paths.logs_dir, "app")  # stays open
    log.info("starting")

    def run(key: DataKey) -> Outcome:
        outcome = _run(paths, key)
        log.info("locked" if outcome == "lock" else "stopped")
        return outcome

    try:
        run_until_quit(lambda: unlock(paths, TkUnlockUI()), run)
    except (UnlockError, KeyfileError) as exc:
        show_error(str(exc))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
