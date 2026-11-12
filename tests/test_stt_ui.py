"""Exercise the actual STT panel through its DOM/API ports with invented local data."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_stt_panel_reference_provenance_and_score_rendering() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the local JavaScript panel check")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed local harness and source, no network
        [node, str(root / "tests/stt_ui_check.js"), str(root / "src/akashic/ui/stt.js")],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "denominators verified" in result.stdout
