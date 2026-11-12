"""Settings > Models: what the current settings need, what is missing, and the Download button.

Downloads run only when the user presses a button (AGENTS.md rule 1): one at a time, in one
background thread, through ``pipeline.models`` (pinned URLs and SHA-256, nothing else). The page
polls the state. Error texts are exception types and file names, never anything captured.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, HTTPException

from akashic.pipeline import models

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from akashic.api.server import Services

type Json = dict[str, object]
type RunIn = Callable[[Callable[[], None]], None]

log = logging.getLogger("api")

MB: Final = 1_000_000


def _in_thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="model-download", daemon=True).start()


@dataclass(slots=True)
class _Progress:
    status: str  # "queued" | "downloading" | "failed"
    done: int = 0
    total: int = 0
    error: str | None = None


class Downloads:
    """The download queue behind the buttons. Thread-safe; one download at a time."""

    def __init__(
        self,
        models_dir: Path,
        runtime_dir: Path,
        wanted: Callable[[], list[models.Needed]],
        opener: models.Opener | None = None,
        run_in: RunIn = _in_thread,
    ) -> None:
        self._models_dir = models_dir
        self._runtime_dir = runtime_dir
        self._wanted = wanted
        self._opener = opener
        self._run_in = run_in
        self._lock = threading.Lock()
        self._state: dict[str, _Progress] = {}
        self._busy = False

    def _ready(self, spec: models.Pinned) -> bool:
        return models.is_ready(self._models_dir, self._runtime_dir, spec)

    def rows(self) -> list[Json]:
        out: list[Json] = []
        with self._lock:
            for need in self._wanted():
                spec = need.spec
                progress = self._state.get(spec.key)
                status = "ready" if self._ready(spec) else "missing"
                if progress is not None and status != "ready":
                    status = progress.status
                out.append(
                    {
                        "key": spec.key,
                        "label": spec.description,
                        "needed_for": need.needed_for,
                        "size_mb": round(spec.total_size / MB),
                        "status": status,
                        "done": progress.done if progress else 0,
                        "total": progress.total if progress else 0,
                        "error": progress.error if progress else None,
                    }
                )
        return out

    def missing(self) -> list[str]:
        return [str(r["key"]) for r in self.rows() if r["status"] != "ready"]

    def start(self, keys: list[str]) -> bool:
        """Queue ``keys`` (each must be wanted and not ready). False while a download runs."""
        specs = {n.spec.key: n.spec for n in self._wanted()}
        todo = [specs[k] for k in keys if k in specs and not self._ready(specs[k])]
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            for spec in todo:
                self._state[spec.key] = _Progress("queued")
        self._run_in(lambda: self._work(todo))
        return True

    def _work(self, todo: list[models.Pinned]) -> None:
        try:
            for spec in todo:
                self._fetch_one(spec)
        finally:
            with self._lock:
                self._busy = False

    def _fetch_one(self, spec: models.Pinned) -> None:
        def progress(_name: str, done: int, total: int) -> None:
            with self._lock:
                self._state[spec.key] = _Progress("downloading", done, total)

        with self._lock:
            self._state[spec.key] = _Progress("downloading", 0, spec.total_size)
        log.info("downloading %s (%d MB)", spec.key, spec.total_size // MB)
        try:
            models.fetch(self._models_dir, self._runtime_dir, spec, self._opener, progress)
        except models.ModelDownloadError as exc:
            log.warning("%s not downloaded: %s", spec.key, exc)
            with self._lock:
                self._state[spec.key] = _Progress("failed", error=str(exc))
            return
        log.info("%s ready", spec.key)
        with self._lock:
            self._state.pop(spec.key, None)


def model_routes(services: Services, downloads: Downloads) -> APIRouter:
    del services  # same shape as the other route builders
    router = APIRouter(prefix="/models")

    @router.get("")
    def listing() -> list[Json]:  # pyright: ignore[reportUnusedFunction]
        return downloads.rows()

    @router.post("/download-all", status_code=202)
    def download_all() -> Json:  # pyright: ignore[reportUnusedFunction]
        missing = downloads.missing()
        if not downloads.start(missing):
            raise HTTPException(409, "a download is already running")
        return {"queued": missing}

    @router.post("/{key}/download", status_code=202)
    def download_one(key: str) -> Json:  # pyright: ignore[reportUnusedFunction]
        if key not in {str(r["key"]) for r in downloads.rows()}:
            raise HTTPException(404, "not a model these settings need")
        if not downloads.start([key]):
            raise HTTPException(409, "a download is already running")
        return {"queued": [key]}

    return router
