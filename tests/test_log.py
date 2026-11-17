from __future__ import annotations

import faulthandler
import logging
import sys
import threading
import uuid
from typing import TYPE_CHECKING

from akashic.log import FaultLog, enable_crash_log, setup_logging

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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


# --- FaultLog ----------------------------------------------------------------------------------

_LOGGER = "test.faults"


def _fault_log(now: list[float], every_s: float = 300.0) -> FaultLog:
    return FaultLog(logging.getLogger(_LOGGER), every_s, clock=lambda: now[0])


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_fault_is_logged_by_what_and_type_with_the_running_total(
    caplog: pytest.LogCaptureFixture,
) -> None:
    faults = _fault_log([0.0])
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert faults.note("media poll", OSError("x")) == 1
        assert faults.note("media poll", "HRESULT 0x1") == 1
    assert _warnings(caplog) == [
        "media poll failed: OSError (1 so far)",
        "media poll failed: HRESULT 0x1 (1 so far)",
    ]
    assert faults.counts() == {"media poll: OSError": 1, "media poll: HRESULT 0x1": 1}


def test_a_pair_is_logged_once_per_interval_and_the_count_never_resets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = [0.0]
    faults = _fault_log(now)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for _ in range(5):
            faults.note("tick", ValueError("x"))
        now[0] = 299.0
        faults.note("tick", ValueError("x"))
        assert len(_warnings(caplog)) == 1
        now[0] = 300.0
        assert faults.note("tick", ValueError("x")) == 7
    assert _warnings(caplog)[-1] == "tick failed: ValueError (7 so far)"
    assert len(_warnings(caplog)) == 2


def test_each_what_and_label_pair_has_its_own_budget(caplog: pytest.LogCaptureFixture) -> None:
    faults = _fault_log([0.0])
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for _ in range(3):
            faults.note("a", OSError("x"))
            faults.note("a", ValueError("x"))
            faults.note("b", OSError("x"))
    assert len(_warnings(caplog)) == 3
    assert faults.counts() == {"a: OSError": 3, "a: ValueError": 3, "b: OSError": 3}


def test_the_message_and_traceback_never_reach_the_log(caplog: pytest.LogCaptureFixture) -> None:
    faults = _fault_log([0.0])
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        try:
            raise ValueError("SECRET window title")  # noqa: TRY301 - test
        except ValueError as exc:
            faults.note("x", exc)
    assert "ValueError" in caplog.text
    assert "SECRET" not in caplog.text
    assert all(r.exc_info is None for r in caplog.records)


def test_note_never_raises() -> None:
    class Broken(BaseException):
        def __str__(self) -> str:
            raise RuntimeError

        __repr__ = __str__

    faults = _fault_log([0.0])
    assert faults.note("x", Broken()) == 1
    assert faults.note("x", "") == 1
    assert faults.note("x", "100% %s %(a)d") == 1


def test_counts_is_a_copy() -> None:
    faults = _fault_log([0.0])
    faults.note("x", OSError())
    got = faults.counts()
    got["x: OSError"] = 99
    got["other"] = 1
    assert faults.counts() == {"x: OSError": 1}


def test_concurrent_notes_are_all_counted_and_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    faults = _fault_log([0.0])
    threads_n, notes_n = 8, 200

    def work() -> None:
        for _ in range(notes_n):
            faults.note("x", OSError())

    threads = [threading.Thread(target=work) for _ in range(threads_n)]
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert faults.counts() == {"x: OSError": threads_n * notes_n}
    assert len(_warnings(caplog)) == 1
