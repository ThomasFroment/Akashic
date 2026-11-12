"""Polling for conditions that a background thread or process makes true."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

POLL_INTERVAL_S = 0.01


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    """Poll until ``predicate`` holds or ``timeout`` passes; return its final value.

    The caller picks the failure: ``assert`` for a defect under test, ``RuntimeError`` for setup."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_INTERVAL_S)
    return predicate()
