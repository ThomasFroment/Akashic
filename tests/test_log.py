from __future__ import annotations

import faulthandler
import logging
import sys
import uuid
from typing import TYPE_CHECKING

from akashic.log import enable_crash_log, setup_logging

if TYPE_CHECKING:
    from pathlib import Path


def test_logs_to_file_and_uncaught_hides_message(tmp_path: Path) -> None:
    setup_logging(tmp_path / "logs", "unit", "INFO")
    setup_logging(tmp_path / "logs", "unit", "INFO")  # idempotent: no duplicate handlers
    logging.getLogger("unit").info("hello %d", 42)
    secret = "captured-" + uuid.uuid4().hex
    try:
        raise ValueError(f"captured text: {secret}")  # noqa: TRY301 - test
    except ValueError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "logs" / "unit.log").read_text("utf-8")
    assert text.count("hello 42") == 1
    assert "uncaught ValueError" in text
    assert secret not in text
    assert "test_log.py" in text


def test_crash_log_is_enabled_and_appends(tmp_path: Path) -> None:
    out = enable_crash_log(tmp_path / "logs", "unit")
    try:
        assert faulthandler.is_enabled()
        faulthandler.dump_traceback(file=out)
        out.flush()
        dumped = (tmp_path / "logs" / "unit.crash.log").read_text("utf-8")
        assert "test_crash_log_is_enabled_and_appends" in dumped
    finally:
        faulthandler.disable()
        out.close()
