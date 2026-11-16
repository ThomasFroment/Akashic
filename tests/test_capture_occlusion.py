"""Occlusion geometry: visible parts of a window and the masked crop."""

from __future__ import annotations

import itertools

from PIL import Image

from akashic.capture.occlusion import area, masked_crop, visible_parts
from akashic.capture.ports import Rect

TARGET = Rect(-20, -10, 30, 20)


def cells(rects: list[Rect]) -> list[tuple[int, int]]:
    return [(x, y) for r in rects for y in range(r.top, r.bottom) for x in range(r.left, r.right)]


def assert_exact(target: Rect, above: list[Rect]) -> list[Rect]:
    parts = visible_parts(target, above)
    got = cells(parts)
    assert len(got) == len(set(got)), "parts overlap"
    want = {
        (x, y)
        for y in range(target.top, target.bottom)
        for x in range(target.left, target.right)
        if not any(c.contains(x, y) for c in above)
    }
    assert set(got) == want
    assert area(parts) == len(want)
    return parts


def test_no_cover_returns_target() -> None:
    assert visible_parts(TARGET, []) == [TARGET]
    assert visible_parts(TARGET, [Rect(100, 100, 120, 120)]) == [TARGET]


def test_fully_covered_or_empty_target() -> None:
    assert visible_parts(TARGET, [Rect(-100, -100, 100, 100)]) == []
    assert visible_parts(Rect(5, 5, 5, 20), []) == []
    assert visible_parts(Rect(5, 5, 20, 5), []) == []


def test_touching_and_zero_area_rects_change_nothing() -> None:
    above = [Rect(30, -10, 50, 20), Rect(-40, -10, -20, 20), Rect(0, 0, 0, 10), Rect(0, 0, 10, 0)]
    assert visible_parts(TARGET, above) == [TARGET]


def test_centre_window_gives_four_parts() -> None:
    cover = Rect(-5, 0, 10, 10)
    parts = assert_exact(TARGET, [cover])
    assert len(parts) == 4
    assert area(parts) == area([TARGET]) - area([cover])


def test_left_half_covered() -> None:
    parts = assert_exact(TARGET, [Rect(-20, -10, 5, 20)])
    assert area(parts) == 25 * 30


def test_overlapping_covers_not_subtracted_twice() -> None:
    a, b = Rect(-10, -5, 10, 10), Rect(0, 0, 20, 15)
    parts = assert_exact(TARGET, [a, b])
    overlap = 10 * 10
    assert area(parts) == area([TARGET]) - (area([a]) + area([b]) - overlap)


def test_order_and_cover_larger_than_target() -> None:
    covers = [Rect(-30, 0, 0, 5), Rect(-10, -20, 5, 40), Rect(0, 10, 40, 12)]
    expected = area(assert_exact(TARGET, covers))
    for perm in itertools.permutations(covers):
        assert area(assert_exact(TARGET, list(perm))) == expected


def test_area() -> None:
    assert area([]) == 0
    assert area([Rect(0, 0, 3, 4), Rect(-5, -5, -4, -3)]) == 14


def test_inputs_not_mutated() -> None:
    above = [Rect(0, 0, 5, 5)]
    visible_parts(TARGET, above)
    assert above == [Rect(0, 0, 5, 5)]
    assert Rect(-20, -10, 30, 20) == TARGET


def gradient(mode: str, size: tuple[int, int]) -> Image.Image:
    img = Image.new(mode, size)
    for x in range(size[0]):
        for y in range(size[1]):
            v = 1 + x + y * 7
            img.putpixel((x, y), (v, v, v) if mode == "RGB" else (v, v, v, 255))
    return img


def test_masked_crop_copies_keep_and_blanks_rest() -> None:
    img = gradient("RGB", (20, 10))
    out = masked_crop(img, (0, 0), Rect(2, 1, 12, 9), [Rect(4, 2, 6, 4)])
    assert out is not None
    assert out.size == (10, 8)
    assert out.mode == "RGB"
    for x in range(10):
        for y in range(8):
            kept = 4 <= x + 2 < 6 and 2 <= y + 1 < 4
            want = img.getpixel((x + 2, y + 1)) if kept else (0, 0, 0)
            assert out.getpixel((x, y)) == want


def test_masked_crop_rgba_blank_is_transparent_black() -> None:
    img = gradient("RGBA", (10, 10))
    out = masked_crop(img, (0, 0), Rect(0, 0, 10, 10), [Rect(0, 0, 1, 1)])
    assert out is not None
    assert out.mode == "RGBA"
    assert out.getpixel((5, 5)) == (0, 0, 0, 0)
    assert out.getpixel((0, 0)) == img.getpixel((0, 0))


def test_masked_crop_negative_origin_matches_positive() -> None:
    img = gradient("RGB", (30, 12))
    pos = masked_crop(img, (0, 0), Rect(3, 2, 20, 10), [Rect(5, 3, 9, 8), Rect(12, 2, 15, 4)])
    neg = masked_crop(
        img,
        (-2560, 0),
        Rect(-2557, 2, -2540, 10),
        [Rect(-2555, 3, -2551, 8), Rect(-2548, 2, -2545, 4)],
    )
    assert pos is not None
    assert neg is not None
    assert pos.tobytes() == neg.tobytes()
    top = masked_crop(img, (0, -1080), Rect(3, -1078, 20, -1070), [Rect(5, -1077, 9, -1072)])
    assert top is not None
    assert top.getpixel((2, 1)) == img.getpixel((5, 3))


def test_masked_crop_clips_to_image_without_padding() -> None:
    img = gradient("RGB", (10, 10))
    out = masked_crop(img, (0, 0), Rect(-5, 5, 15, 30), [Rect(-5, 5, 15, 30)])
    assert out is not None
    assert out.size == (10, 5)
    assert out.getpixel((3, 0)) == img.getpixel((3, 5))


def test_masked_crop_none_cases() -> None:
    img = gradient("RGB", (10, 10))
    assert masked_crop(img, (0, 0), Rect(0, 0, 10, 10), []) is None
    assert masked_crop(img, (0, 0), Rect(20, 20, 30, 30), [Rect(20, 20, 30, 30)]) is None
    assert masked_crop(img, (0, 0), Rect(0, 0, 5, 5), [Rect(6, 6, 9, 9)]) is None
    assert masked_crop(img, (0, 0), Rect(0, 0, 5, 5), [Rect(50, 0, 60, 5)]) is None


def test_masked_crop_does_not_mutate() -> None:
    img = gradient("RGB", (10, 10))
    before = img.tobytes()
    keep = [Rect(1, 1, 3, 3)]
    masked_crop(img, (0, 0), Rect(0, 0, 10, 10), keep)
    assert img.tobytes() == before
    assert keep == [Rect(1, 1, 3, 3)]
