"""Reading the plain-text log files for the Debug page: every file, merged or alone, by level.

Log lines look like ``2026-11-08 19:45:10,156 29312 capture INFO ready (schema v3)`` (see
``akashic.log``). A line that doesn't start that way (a traceback) belongs to the entry before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
MAX_READ_BYTES: Final = 6 * 1024 * 1024  # a log rotates at 5 MB, so this reads a whole file
MERGED: Final = "all"
FILE_NAME: Final = re.compile(r"^[A-Za-z0-9_-]+\.log(\.\d+)?$")
_LINE: Final = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (\d+) (\S+) "
    r"(DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)$"
)


@dataclass(frozen=True, slots=True)
class LogFile:
    name: str
    size: int
    modified_ms: int


@dataclass(slots=True)
class Entry:
    source: str  # file name without ".log": "capture", "worker", "app"
    ts: str
    pid: int
    logger: str
    level: str
    message: str


def list_files(logs_dir: Path) -> list[LogFile]:
    """Every log file, current ones first (rotated backups ``x.log.1`` follow)."""
    if not logs_dir.is_dir():
        return []
    found = [
        LogFile(p.name, p.stat().st_size, int(p.stat().st_mtime * 1000))
        for p in logs_dir.iterdir()
        if p.is_file() and FILE_NAME.match(p.name)
    ]
    return sorted(found, key=lambda f: (".log." in f.name, f.name))


def parse(text: str, source: str) -> list[Entry]:
    entries: list[Entry] = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if match:
            ts, pid, logger, level, message = match.groups()
            entries.append(Entry(source, ts, int(pid), logger, level, message))
        elif entries:
            entries[-1].message += "\n" + line
    return entries  # lines before the first entry (a cut-off start) are dropped


def read_file(path: Path) -> list[Entry]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - MAX_READ_BYTES))
            data = fh.read()
    except FileNotFoundError:
        return []
    source = path.name.split(".log", 1)[0]
    return parse(data.decode("utf-8", errors="replace"), source)


def read_logs(logs_dir: Path, name: str) -> list[Entry] | None:
    """Entries of one file, or of every current file merged by time (``name == "all"``).
    None when ``name`` is not a log file that exists: the name never becomes a path unchecked."""
    files = list_files(logs_dir)
    if name == MERGED:
        merged = [e for f in files if ".log." not in f.name for e in read_file(logs_dir / f.name)]
        merged.sort(key=lambda e: e.ts)  # stable: lines of the same millisecond keep file order
        return merged
    if any(f.name == name for f in files):
        return read_file(logs_dir / name)
    return None


def select(entries: list[Entry], *, min_level: str, query: str, limit: int) -> list[Entry]:
    """The last ``limit`` entries at or above ``min_level`` that contain ``query``."""
    floor = LEVELS.index(min_level) if min_level in LEVELS else 0
    needle = query.lower()
    chosen = [
        e
        for e in entries
        if LEVELS.index(e.level) >= floor and (not needle or needle in e.message.lower())
    ]
    return chosen[-limit:]
