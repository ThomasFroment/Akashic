"""Settings > Export: one encrypted export at a time, in a background thread.

The page starts it, polls the state and may cancel. The request body is read by hand and never
logged: it holds the passphrase and a path. Error responses carry only fixed sentences and
exception type names. The export reads the store through its own connection, so searches and
Settings keep answering while it runs.
"""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, HTTPException, Request

from akashic.jsonish import as_dict, as_list
from akashic.storage.export import (
    ExportCancel,
    ExportCancelled,
    ExportError,
    ExportResult,
    ExportScope,
    partial_path,
    validate,
    write_export,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from sqlite3 import Connection

    from akashic.storage.media import MediaStore

type Json = dict[str, object]

log = logging.getLogger("api")

_BAD_BODY: Final = "The export request is not valid."
_BAD_RANGE: Final = "The range must be whole, non-negative milliseconds."
_BAD_CATEGORIES: Final = "The categories must be a list of names."


class Exports:
    """The export behind the page. Thread-safe; one export at a time."""

    def __init__(
        self, open_db: Callable[[], Connection], media: MediaStore, data_dir: Path
    ) -> None:
        self._open_db = open_db
        self._media = media
        self._data_dir = data_dir
        self._lock = threading.Lock()
        self._state = "idle"
        self._done = 0
        self._total = 0
        self._error: str | None = None
        self._target: str | None = None
        self._result: ExportResult | None = None
        self._cancel: ExportCancel | None = None
        self._thread: threading.Thread | None = None

    def start(self, target: str, passphrase: str, scope: ExportScope) -> bool:
        """Begin an export. False while one runs; ``ExportError`` when it cannot be made."""
        with self._lock:
            if self._state == "running":
                return False
            validate(Path(target), passphrase, scope, self._data_dir)
            self._state, self._done, self._total = "running", 0, 0
            self._error, self._result, self._target = None, None, target
            self._cancel = cancel = ExportCancel()
            self._thread = threading.Thread(
                target=self._run,
                args=(Path(target), passphrase, scope, cancel),
                name="export",
                daemon=True,
            )
            self._thread.start()
        return True

    def status(self) -> Json:
        with self._lock:
            result = self._result
            return {
                "state": self._state,
                "done": self._done,
                "total": self._total,
                "error": self._error,
                "target": self._target,
                "result": None
                if result is None
                else {
                    "files": result.files,
                    "bytes": result.bytes,
                    "missing_files": result.missing_files,
                },
            }

    def cancel(self) -> None:
        with self._lock:
            if self._state == "running" and self._cancel is not None:
                self._cancel.cancel()

    def cancel_and_wait(self, timeout_s: float) -> None:
        """Stop for Lock and Quit: no export thread runs afterwards, or its partial is removed."""
        self.cancel()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout_s)
        if thread.is_alive() and self._target is not None:
            shutil.rmtree(partial_path(Path(self._target)), ignore_errors=True)

    def _progress(self, done: int, total: int) -> None:
        with self._lock:
            self._done, self._total = done, total

    def _run(self, target: Path, passphrase: str, scope: ExportScope, cancel: ExportCancel) -> None:
        result: ExportResult | None = None
        try:
            result = write_export(
                open_db=self._open_db,
                media=self._media,
                target=target,
                passphrase=passphrase,
                scope=scope,
                cancel=cancel,
                report=self._progress,
            )
        except ExportCancelled:
            outcome, error = "cancelled", None
        except Exception as exc:  # noqa: BLE001 - any failure ends the export; only its type is kept
            log.warning("export failed: %s", type(exc).__name__)
            outcome, error = "failed", type(exc).__name__
        else:
            log.info(
                "export done: %d files, %d bytes, %d missing",
                result.files,
                result.bytes,
                result.missing_files,
            )
            outcome, error = "done", None
        with self._lock:
            self._state, self._error, self._result = outcome, error, result


def _optional_ms(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExportError(_BAD_RANGE)
    return value


def _categories(value: object) -> frozenset[str]:
    names = as_list(value)
    if names is None or not all(isinstance(name, str) for name in names):
        raise ExportError(_BAD_CATEGORIES)
    return frozenset(str(name) for name in names)


def _parse(body: object) -> tuple[str, str, ExportScope]:
    fields = as_dict(body)
    if fields is None:
        raise ExportError(_BAD_BODY)
    target, passphrase = fields.get("target"), fields.get("passphrase")
    if not isinstance(target, str) or not isinstance(passphrase, str):
        raise ExportError(_BAD_BODY)
    scope = ExportScope(
        _optional_ms(fields.get("start_ms")),
        _optional_ms(fields.get("end_ms")),
        _categories(fields.get("categories", [])),
    )
    return target, passphrase, scope


def export_routes(exports: Exports) -> APIRouter:
    router = APIRouter(prefix="/export")

    @router.get("")
    def status() -> Json:  # pyright: ignore[reportUnusedFunction]
        return exports.status()

    @router.post("", status_code=202)
    async def start(request: Request) -> Json:  # pyright: ignore[reportUnusedFunction]
        try:
            body = await request.json()
        except ValueError:
            body = None
        try:
            started = exports.start(*_parse(body))
        except ExportError as exc:
            raise HTTPException(422, str(exc)) from None
        if not started:
            raise HTTPException(409, "an export is already running")
        return exports.status()

    @router.delete("")
    def cancel() -> Json:  # pyright: ignore[reportUnusedFunction]
        exports.cancel()
        return exports.status()

    return router
