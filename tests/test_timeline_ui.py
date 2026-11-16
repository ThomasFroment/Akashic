"""The timeline page and the moment links of the real UI code, over a fake DOM (Node).

Not exercised: layout and real sizes, real images loading, pointer capture, CSS, a real browser.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src/akashic/ui"


def test_timeline_page_and_moment_links() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable for the local JavaScript panel check")
    result = subprocess.run(  # noqa: S603 - fixed local harness and source, no network
        [node, str(ROOT / "tests/timeline_page_check.js"), str(UI)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "timeline page verified" in result.stdout


def test_timeline_js_never_writes_a_style_attribute() -> None:
    source = (UI / "timeline.js").read_text("utf-8")
    assert 'setAttribute("style"' not in source
    assert "setAttribute('style'" not in source
    assert "style=" not in source
