"""Content-first screen text, preserving paragraph gaps and column reading order."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from akashic.capture.ports import TextBlock

if TYPE_CHECKING:
    from collections.abc import Iterable

MAX_TEXT_CHARS: Final = 20_000
MAX_UIA_NODES: Final = 3_000
# Below this many characters a UIA result counts as "no text" and the OCR gate takes over
# (games, canvases, apps without accessibility support).
MIN_UIA_CHARS: Final = 40
MAX_LAYOUT_DEPTH: Final = 20


def assemble_text(parts: Iterable[TextBlock], max_chars: int = MAX_TEXT_CHARS) -> str:
    """Controls cannot make an otherwise empty UIA read suppress OCR.

    Deduplicate identical nodes/positions, not words globally: repeated real messages matter.
    """
    seen: set[tuple[str, object]] = set()
    blocks: list[TextBlock] = []
    for block in parts:
        text = "\n".join(" ".join(line.split()) for line in block.text.splitlines()).strip()
        key = (text, block.rect)
        if not block.content or not text or key in seen:
            continue
        seen.add(key)
        blocks.append(TextBlock(text, block.rect))
    return render_blocks(blocks, max_chars)


def _reading_order(blocks: list[TextBlock], depth: int = 0) -> list[TextBlock]:
    if len(blocks) <= 1 or depth >= MAX_LAYOUT_DEPTH or any(b.rect is None for b in blocks):
        return blocks
    positioned = sorted(blocks, key=lambda b: (b.rect.top, b.rect.left) if b.rect else (0, 0))
    first = positioned[0]
    # A full-width heading should precede the columns below it, not join their x intervals.
    if first.rect and all(b.rect and b.rect.top >= first.rect.bottom for b in positioned[1:]):
        return [first, *_reading_order(positioned[1:], depth + 1)]
    by_x = sorted(positioned, key=lambda b: b.rect.left if b.rect else 0)
    columns: list[list[TextBlock]] = [[]]
    right = -(10**9)
    for block in by_x:
        if block.rect is None:
            continue
        gap = max(24, block.rect.height * 2)
        if columns[-1] and block.rect.left - right > gap:
            columns.append([])
        columns[-1].append(block)
        right = max(right, block.rect.right)
    if len(columns) > 1:
        return [b for column in columns for b in _reading_order(column, depth + 1)]
    # A wide heading/footer can bridge the column gutter. Split at the largest empty
    # horizontal band, then look for columns within each section. Doing this only after
    # the column attempt avoids turning aligned multi-column paragraphs into table rows.
    split = _horizontal_split(positioned)
    if split:
        return [
            *_reading_order(positioned[:split], depth + 1),
            *_reading_order(positioned[split:], depth + 1),
        ]
    return positioned


def _horizontal_split(positioned: list[TextBlock]) -> int:
    bottom = -(10**9)
    largest_gap = 0
    split = 0
    for index, block in enumerate(positioned):
        if block.rect is None:  # pragma: no cover - caller requires positioned blocks
            continue
        gap = block.rect.top - bottom
        if index and gap > largest_gap:
            largest_gap, split = gap, index
        bottom = max(bottom, block.rect.bottom)
    return split


def render_blocks(blocks: list[TextBlock], max_chars: int = MAX_TEXT_CHARS) -> str:
    """Order spatial blocks and preserve gaps, with a bounded stored text size."""
    lines: list[str] = []
    used = 0
    previous: TextBlock | None = None
    for block in _reading_order(blocks):
        separator = "\n"
        if (
            previous
            and previous.rect
            and block.rect
            and (
                block.rect.top < previous.rect.top
                or block.rect.top - previous.rect.bottom > max(8, previous.rect.height)
            )
        ):
            separator = "\n\n"
        extra = len(block.text) + (len(separator) if lines else 0)
        if used + extra > max_chars:
            break
        if lines:
            lines.append(separator)
        lines.append(block.text)
        used += extra
        previous = block
    return "".join(lines)
