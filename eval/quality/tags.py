"""Change tags: a finder pattern plus a 24-bit id, drawn by pages, the game HUD and videos.

An 8x8 cell tag inside a one-cell light margin. Row 0 and column 0 are dark (finder), row 7 and
column 7 alternate dark and light starting dark (timing), the inner 6x6 holds 24 id bits and a
12-bit check (crc32 of the id). Cells are at least 8 device px.

    python tags.py     # self-test: >= 99 % of 200 tags decode from a lossless and a WebP 4K frame
"""

from __future__ import annotations

import io
import zlib

import numpy as np

GRID = 8
MARGIN = 1
MIN_CELL = 8
DARK = 110  # luma below this is a dark cell
ID_BITS = 24
CHECK_BITS = 12
TAG_CELLS = GRID + 2 * MARGIN
WEBP_QUALITY = 70  # the stored-frame path (capture/imaging.py)
WEBP_METHOD = 2


def check_bits(tag_id: int) -> int:
    return zlib.crc32(tag_id.to_bytes(3, "big")) & ((1 << CHECK_BITS) - 1)


def cells(tag_id: int) -> np.ndarray:
    """8x8 bool matrix, True = dark."""
    if not 0 <= tag_id < 1 << ID_BITS:
        raise ValueError("tag id must fit 24 bits")
    g = np.zeros((GRID, GRID), bool)
    g[0, :] = True
    g[:, 0] = True
    g[GRID - 1, ::2] = True
    g[::2, GRID - 1] = True
    word = (tag_id << CHECK_BITS) | check_bits(tag_id)
    bits = [(word >> (ID_BITS + CHECK_BITS - 1 - i)) & 1 for i in range(ID_BITS + CHECK_BITS)]
    g[1:GRID - 1, 1:GRID - 1] = np.array(bits, bool).reshape(GRID - 2, GRID - 2)
    return g


def encode(tag_id: int, cell: int = MIN_CELL) -> np.ndarray:
    """Grayscale uint8 image of the tag with its light margin."""
    if cell < MIN_CELL:
        raise ValueError("cells must be at least 8 device px")
    img = np.full((TAG_CELLS, TAG_CELLS), 255, np.uint8)
    img[MARGIN:MARGIN + GRID, MARGIN:MARGIN + GRID] = np.where(cells(tag_id), 0, 255)
    return np.kron(img, np.ones((cell, cell), np.uint8))


def paste(frame: np.ndarray, tag_id: int, x: int, y: int, cell: int = MIN_CELL) -> None:
    tag = encode(tag_id, cell)
    frame[y:y + tag.shape[0], x:x + tag.shape[1]] = tag[..., None] if frame.ndim == 3 else tag


def luma(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame
    return (frame[..., :3] @ np.array([0.299, 0.587, 0.114], np.float32)).astype(np.uint8)


def _runs(dark: np.ndarray, rows: np.ndarray, lo: int, hi: int):
    """(y, x, length) of dark runs with lo <= length <= hi in the given rows, row by row then left to right."""
    sub = dark[rows].view(np.int8)
    pad = np.zeros((len(rows), 1), np.int8)
    d = np.diff(np.concatenate((pad, sub, pad), axis=1), axis=1)
    # starts and ends alternate along each row, so in row-major order the k-th start pairs with the k-th end
    sy, sx = np.nonzero(d == 1)
    _, ex = np.nonzero(d == -1)
    length = ex - sx
    ok = (length >= lo) & (length <= hi)
    return zip(rows[sy[ok]], sx[ok], length[ok], strict=True)


def decode(frame: np.ndarray, max_cell: int = 24) -> list[int]:
    """Ids of every valid tag in the frame (finder search, then 8x8 cell sampling)."""
    dark = luma(frame) < DARK
    found: dict[int, tuple[int, int]] = {}
    lo, hi = GRID * (MIN_CELL - 1), GRID * (max_cell + 1)
    for y, x, length in _runs(dark, np.flatnonzero(dark.sum(axis=1) >= lo), lo, hi):
        cell = round(length / GRID)
        if abs(length - cell * GRID) > cell // 2 + 1 or cell < MIN_CELL:
            continue
        tag_id = _read(dark, int(x), int(y), cell)
        if tag_id is not None:
            found.setdefault(tag_id, (int(x), int(y)))
    return sorted(found)


def _read(dark: np.ndarray, x: int, y: int, cell: int) -> int | None:
    span = GRID * cell
    if y + span > dark.shape[0] or x + span > dark.shape[1]:
        return None
    centers = (np.arange(GRID) * cell + cell // 2)
    grid = dark[np.ix_(y + centers, x + centers)]
    if not (grid[0].all() and grid[:, 0].all() and (grid[GRID - 1, ::2]).all() and (~grid[GRID - 1, 1::2]).all()
            and grid[::2, GRID - 1].all() and (~grid[1::2, GRID - 1]).all()):
        return None
    bits = grid[1:GRID - 1, 1:GRID - 1].reshape(-1)
    word = 0
    for b in bits:
        word = (word << 1) | int(b)
    tag_id, check = word >> CHECK_BITS, word & ((1 << CHECK_BITS) - 1)
    return tag_id if check == check_bits(tag_id) else None


# The same drawing for pages and the HUD: an HTML canvas. Keep in step with cells() above.
JS_SOURCE = """
function tagCrc12(id){let c=~0;const b=[(id>>16)&255,(id>>8)&255,id&255];
 for(const x of b){c^=x;for(let k=0;k<8;k++)c=(c>>>1)^(0xEDB88320&-(c&1));}return (~c>>>0)&0xFFF;}
function drawTag(canvas,id,cell){cell=cell||8;const n=10,g=8;canvas.width=canvas.height=n*cell;
 const x=canvas.getContext('2d');x.fillStyle='#fff';x.fillRect(0,0,n*cell,n*cell);x.fillStyle='#000';
 const dark=(r,c)=>{if(r==0||c==0)return true;if(r==g-1)return c%2==0;if(c==g-1)return r%2==0;
  const i=(r-1)*(g-2)+(c-1);const w=(BigInt(id)<<12n)|BigInt(tagCrc12(id));return ((w>>BigInt(35-i))&1n)==1n;};
 for(let r=0;r<g;r++)for(let c=0;c<g;c++)if(dark(r,c))x.fillRect((c+1)*cell,(r+1)*cell,cell,cell);}
"""


def _selftest() -> int:
    from PIL import Image

    rng = np.random.default_rng(7)
    frame = np.full((2160, 3840, 3), 235, np.uint8)
    for y in range(40, 2100, 18):  # text-like noise
        for _ in range(30):
            x0 = int(rng.integers(0, 3700))
            frame[y:y + 10, x0:x0 + int(rng.integers(5, 140))] = int(rng.integers(0, 90))
    ids = [int(i) for i in rng.integers(0, 1 << ID_BITS, 200)]
    for k, tag_id in enumerate(ids):
        paste(frame, tag_id, 20 + (k % 20) * 190, 20 + (k // 20) * 210, cell=int(rng.choice([8, 9, 10, 12])))
    ok = 0
    for name, img in (("lossless", frame), ("webp", None)):
        if img is None:
            buf = io.BytesIO()
            Image.fromarray(frame).save(buf, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
            img = np.asarray(Image.open(buf).convert("RGB"))
        got = set(decode(img))
        rate = sum(i in got for i in ids) / len(ids)
        print(f"{name}: {rate:.3f} decoded, {len(got - set(ids))} spurious")
        ok += rate >= 0.99
    return 0 if ok == 2 else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
