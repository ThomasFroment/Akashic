"""Static checks on the live app's network usage."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "akashic"
# Modules allowed to use an HTTP library: the one-time explicit model download, and the client
# for the local llama-server, whose transport refuses any address but 127.0.0.1 (tested in
# test_llm.py).
NETWORK_EXEMPT = {SRC / "pipeline" / "models.py", SRC / "pipeline" / "llm.py"}
NETWORK_MODULES = re.compile(
    r"^\s*(?:import|from)\s+(urllib\.request|http\.client|requests|httpx|aiohttp|urllib3|"
    r"ftplib|smtplib|websocket|websockets|huggingface_hub)\b",
    re.MULTILINE,
)


def test_no_network_libraries_outside_model_download() -> None:
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path not in NETWORK_EXEMPT and NETWORK_MODULES.search(path.read_text("utf-8"))
    ]
    assert offenders == []


def test_ui_has_no_external_urls() -> None:
    ui = SRC / "ui"
    assert ui.is_dir(), "the web UI folder moved: point this check at it"
    assert any(p.suffix in {".html", ".css", ".js"} for p in ui.rglob("*"))
    pattern = re.compile(r"""(?:src|href|url)\s*[=(]\s*["']?(?:https?:)?//""", re.IGNORECASE)
    offenders = [
        str(p.relative_to(SRC))
        for p in ui.rglob("*")
        if p.suffix in {".html", ".css", ".js"} and pattern.search(p.read_text("utf-8"))
    ]
    assert offenders == []
