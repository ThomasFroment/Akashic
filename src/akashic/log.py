"""Logging setup.

Rule: logs never contain captured content (screen text, window titles, URLs, transcripts,
media titles, speaker names) or key material. Log counts, durations, ids and error types only.
Logs are plaintext so they stay readable when the app is locked; that's why the rule is strict.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from typing import IO, TYPE_CHECKING, Final

if TYPE_CHECKING:
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


def enable_crash_log(logs_dir: Path, process_name: str) -> IO[str]:
    """Dump the stacks of all threads to ``logs/<process_name>.crash.log`` on a hard crash
    (access violation, abort in a native library) that the logging hook never sees.

    faulthandler writes only file names, line numbers and function names, never values, so
    the logging rule above holds. The file stays open for the life of the process."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    out = (logs_dir / f"{process_name}.crash.log").open("a", encoding="utf-8")
    faulthandler.enable(file=out, all_threads=True)
    return out
