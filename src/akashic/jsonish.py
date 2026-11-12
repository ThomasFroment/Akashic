"""Narrowing helpers for parsed JSON/TOML, which arrive typed as ``object``."""

from __future__ import annotations

from typing import cast


def as_dict(value: object) -> dict[str, object] | None:
    """Return ``value`` as a string-keyed dict, or None if it isn't a dict."""
    if not isinstance(value, dict):
        return None
    items = cast("dict[object, object]", value)
    return {str(k): v for k, v in items.items()}


def as_list(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return list(cast("list[object]", value))
