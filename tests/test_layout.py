"""Architecture rules from AGENTS.md that are cheap to enforce mechanically."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "akashic"
PLATFORM = SRC / "platform" / "win"
CORE = ("capture", "pipeline", "storage", "search", "api")


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_relaxed_typing_only_in_platform_win() -> None:
    relaxed = re.compile(r"^#\s*pyright:\s*(basic|standard|off)", re.MULTILINE)
    offenders = [
        str(p.relative_to(SRC))
        for p in _py_files()
        if PLATFORM not in p.parents and relaxed.search(p.read_text("utf-8"))
    ]
    assert offenders == []


def test_core_does_not_import_platform_except_main() -> None:
    pattern = re.compile(r"^\s*(?:from|import)\s+akashic\.platform\b", re.MULTILINE)
    offenders = [
        str(p.relative_to(SRC))
        for p in _py_files()
        if p.relative_to(SRC).parts[0] in CORE
        and p.name != "main.py"
        and pattern.search(p.read_text("utf-8"))
    ]
    assert offenders == []


def test_servers_never_bind_all_interfaces() -> None:
    pattern = re.compile(r"""["'](0\.0\.0\.0|::)["']""")
    offenders = [
        str(p.relative_to(SRC)) for p in _py_files() if pattern.search(p.read_text("utf-8"))
    ]
    assert offenders == []
