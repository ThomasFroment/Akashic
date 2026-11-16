"""Architecture rules from AGENTS.md and docs/ARCHITECTURE.md, checked mechanically."""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

OLD_JOBS = "akashic.pipeline.jobs"
SRC = Path(__file__).resolve().parents[1] / "src" / "akashic"
PLATFORM = SRC / "platform" / "win"
CORE = ("capture", "pipeline", "storage", "search", "api")


def _py_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _imported_akashic_names(path: Path) -> set[str]:
    """Dotted akashic names imported anywhere in the file; ``from m import x`` gives ``m.x``."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {n for n in names if n.split(".")[0] == "akashic" and "." in n}


def _imported_akashic_packages(path: Path) -> set[str]:
    return {name.split(".")[1] for name in _imported_akashic_names(path)}


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


def test_storage_imports_no_higher_package() -> None:
    higher = {"pipeline", "search", "api", "capture", "platform"}
    offenders = [
        str(p.relative_to(SRC))
        for p in sorted((SRC / "storage").rglob("*.py"))
        if _imported_akashic_packages(p) & higher
    ]
    assert offenders == []


def test_outside_capture_only_ports_and_occlusion_are_imported() -> None:
    allowed = {"akashic.capture.ports", "akashic.capture.occlusion"}
    exceptions = {  # until B10 #11
        ("api/memory.py", "akashic.capture.media"),
        ("api/ask.py", "akashic.capture.media"),
    }
    offenders = [
        f"{relative}: {name}"
        for p in _py_files()
        if (relative := p.relative_to(SRC).as_posix()).split("/")[0] not in {"capture", "platform"}
        for name in sorted(_imported_akashic_names(p))
        if name.split(".")[1] == "capture"
        and ".".join(name.split(".")[:3]) not in allowed
        and (relative, ".".join(name.split(".")[:3])) not in exceptions
    ]
    assert offenders == []


def test_the_import_collector_sees_type_checking_and_in_function_imports(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from akashic.capture.screen import Screen\n"
        "def late() -> None:\n"
        "    import akashic.capture.audio\n"
        "    from akashic.capture import gaps\n",
        "utf-8",
    )
    assert _imported_akashic_names(sample) == {
        "akashic.capture.screen.Screen",
        "akashic.capture.audio",
        "akashic.capture.gaps",
    }


def test_servers_never_bind_all_interfaces() -> None:
    pattern = re.compile(r"""["'](0\.0\.0\.0|::)["']""")
    offenders = [
        str(p.relative_to(SRC)) for p in _py_files() if pattern.search(p.read_text("utf-8"))
    ]
    assert offenders == []


def test_jobs_module_lives_in_storage_without_shim() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(OLD_JOBS)
    jobs = importlib.import_module("akashic.storage.jobs")
    assert jobs.MAX_ATTEMPTS == 5
    for name in (
        "BACKOFF_BASE_MS",
        "BACKOFF_FACTOR",
        "BACKOFF_MAX_MS",
        "PermanentJobError",
        "Job",
        "NewJob",
        "enqueue_in",
        "_backoff_ms",
        "JobQueue",
    ):
        assert hasattr(jobs, name)
    for method in (
        "enqueue",
        "claim",
        "complete",
        "fail",
        "release_running",
        "pending_count",
        "purge_done",
    ):
        assert hasattr(jobs.JobQueue, method)


def test_no_file_mentions_the_old_jobs_module() -> None:
    root = SRC.parents[1]
    offenders = [
        str(p.relative_to(root))
        for d in ("src", "scripts", "tests")
        for p in sorted((root / d).rglob("*.py"))
        if p.resolve() != Path(__file__).resolve() and OLD_JOBS in p.read_text("utf-8")
    ]
    assert offenders == []
