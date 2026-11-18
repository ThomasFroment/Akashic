"""The speech search and memory line rows show the tag, through the real UI code."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_rows_show_the_speech_tag_and_nothing_when_empty() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the local JavaScript check")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed local harness and sources, no network
        [
            node,
            str(root / "tests/speech_tag_ui_check.js"),
            str(root / "src/akashic/ui/app.js"),
            str(root / "src/akashic/ui/memory.js"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "speech tag shown" in result.stdout
