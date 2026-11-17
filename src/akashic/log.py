"""Logging setup.

Rule: logs never contain captured content (screen text, window titles, URLs, transcripts,
media titles, speaker names) or key material. Log counts, durations, ids and error types only.
Logs are plaintext so they stay readable when the app is locked; that's why the rule is strict.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from typing import IO, TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import TracebackType

MAX_BYTES: Final = 5 * 1024 * 1024
BACKUPS: Final = 5
FORMAT: Final = "%(asctime)s %(process)d %(name)s %(levelname)s %(message)s"


def setup_logging(logs_dir: Path, process_name: str, level: str) -> None:
    """Log to ``logs/<process_name>.log`` (rotated). Uncaught exceptions are logged too."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        logs_dir / f"{process_name}.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
        old.close()
    root.addHandler(handler)
    root.setLevel(level)

    def log_uncaught(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        # Type and traceback only: exception messages can quote data (e.g. a failing value).
        del exc
        logging.getLogger(process_name).critical(
            "uncaught %s\n%s", exc_type.__name__, "".join(traceback.format_tb(tb)).rstrip()
        )

    sys.excepthook = log_uncaught


class FaultLog:
    """Counts recurring faults and logs each (what, label) pair once per ``every_s``.

    The label is an exception's type name or a caller-supplied string; both must follow the
    module's logging rule. The exception's message and traceback are never read."""

    def __init__(
        self,
        logger: logging.Logger,
        every_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._logger = logger
        self._every_s = every_s
        self._clock = clock
        self._counts: dict[str, int] = {}
        self._logged_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def note(self, what: str, error: BaseException | str) -> int:
        """Count one fault; return the running total for this (what, label)."""
        label = error if isinstance(error, str) else type(error).__name__
        key = f"{what}: {label}"
        with self._lock:
            count = self._counts.get(key, 0) + 1
            self._counts[key] = count
            now = self._clock()
            if now - self._logged_at.get(key, float("-inf")) >= self._every_s:
                self._logged_at[key] = now
                self._logger.warning("%s failed: %s (%d so far)", what, label, count)
            return count

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


def enable_crash_log(logs_dir: Path, process_name: str) -> IO[str]:
    """Dump the stacks of all threads to ``logs/<process_name>.crash.log`` on a hard crash
    (access violation, abort in a native library) that the logging hook never sees.

    faulthandler writes only file names, line numbers and function names, never values, so
    the logging rule above holds. The file stays open for the life of the process."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    out = (logs_dir / f"{process_name}.crash.log").open("a", encoding="utf-8")
    faulthandler.enable(file=out, all_threads=True)
    return out
