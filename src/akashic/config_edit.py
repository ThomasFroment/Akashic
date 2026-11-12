"""Change values in config.toml without losing the comments or the order of the file.

Python can read TOML but not write it, and a rewrite would drop the explanations in the file, so
this edits the text in place: it finds ``key = value`` inside ``[section]``, swaps the value (and
keeps a trailing ``# comment``), or adds the line when the key is missing. The caller validates
the result with ``parse_config`` before it replaces the file.
"""

from __future__ import annotations

import json
import re
from typing import Final

_SECTION: Final = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")


class EditError(ValueError):
    """The value can't be written as TOML."""


def toml_value(value: object) -> str:
    """``value`` as a TOML literal: bool, int, float, string or a list of strings."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)  # a JSON string is a valid TOML basic string
    if isinstance(value, (list, tuple)):
        items: list[object] = list(value)  # pyright: ignore[reportUnknownArgumentType]
        if not all(isinstance(v, str) for v in items):
            msg = "lists may only hold strings"
            raise EditError(msg)
        return "[" + ", ".join(json.dumps(str(v), ensure_ascii=False) for v in items) + "]"
    msg = f"cannot write a {type(value).__name__} to TOML"
    raise EditError(msg)


def _value_end(lines: list[str], row: int, col: int) -> tuple[int, int]:
    """Where the value that starts at ``lines[row][col]`` ends: (row, column after its last
    character). Understands strings, arrays that span lines, and comments inside arrays."""
    depth = 0
    r, c = row, col
    while r < len(lines):
        c, depth, done = _scan_line(lines[r], c, depth)
        if done:
            return r, c
        r, c = r + 1, 0
    return len(lines) - 1, len(lines[-1])


def _scan_line(line: str, c: int, depth: int) -> tuple[int, int, bool]:
    """Walk one line of a value: (column reached, array depth, whether the value ended)."""
    while c < len(line):
        ch = line[c]
        if ch in "\"'":
            c = _string_end(line, c)
            if depth == 0:
                return c, depth, True
            continue
        if ch == "#":
            if depth == 0:
                return len(line[:c].rstrip()), depth, True
            return len(line), depth, False  # a comment inside an array: skip the rest
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return c + 1, depth, True
        elif depth == 0 and ch in " \t":
            return c, depth, True
        c += 1
    return len(line.rstrip()), depth, depth == 0


def _string_end(line: str, start: int) -> int:
    quote = line[start]
    c = start + 1
    while c < len(line):
        if line[c] == "\\" and quote == '"':
            c += 2
            continue
        if line[c] == quote:
            return c + 1
        c += 1
    return len(line)


def set_value(text: str, dotted_key: str, value: object) -> str:
    """Return ``text`` with ``section.key`` (or a top-level ``key``) set to ``value``."""
    section, _, key = dotted_key.rpartition(".")
    literal = toml_value(value)
    lines = text.split("\n")
    current = ""
    section_row: int | None = None if section else -1  # -1: the top of the file
    insert_at: int | None = None  # after the last line of the wanted section
    key_line = re.compile(rf"^(\s*){re.escape(key)}\s*=\s*")
    for row, line in enumerate(lines):
        header = _SECTION.match(line)
        if header:
            if current == section and section_row is not None and insert_at is None:
                insert_at = row
            current = header.group(1).strip()
            if current == section:
                section_row = row
            continue
        if current == section:
            found = key_line.match(line)
            if found:
                end_row, end_col = _value_end(lines, row, found.end())
                tail = lines[end_row][end_col:]
                lines[row : end_row + 1] = [line[: found.end()] + literal + tail]
                return "\n".join(lines)
    if section_row is None:  # the section does not exist yet: add it at the end
        body = text if text.endswith("\n") or not text else text + "\n"
        return f"{body}\n[{section}]\n{key} = {literal}\n"
    if insert_at is None:
        insert_at = len(lines)
    while insert_at > 0 and not lines[insert_at - 1].strip():  # stay above the blank lines
        insert_at -= 1
    lines.insert(insert_at, f"{key} = {literal}")
    return "\n".join(lines)


def set_values(text: str, values: dict[str, object]) -> str:
    for dotted_key, value in values.items():
        text = set_value(text, dotted_key, value)
    return text
