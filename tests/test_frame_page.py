"""The frame page and the result rows of the real panel, over a tiny DOM port."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_frame_page_lists_app_windows_front_to_back_with_their_text_source() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the local JavaScript panel check")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(  # noqa: S603 - fixed local harness and source, no network
        [node, str(root / "tests/frame_page_check.js"), str(root / "src/akashic/ui/app.js")],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "frame page verified" in result.stdout
