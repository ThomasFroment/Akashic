"""Text folding shared by exclusions, time parsing and transcript comparison."""

from __future__ import annotations

import unicodedata


def fold(text: str) -> str:
    """Lower-case without diacritics, for matching (e.g. "Navigation privée")."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))
