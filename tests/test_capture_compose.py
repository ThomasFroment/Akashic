"""Monitor composition: painter's order, blanks for excluded/pending, visibility facts."""

from __future__ import annotations

from PIL import Image

from akashic.capture.compose import Composite, Layer, LayerKind, compose
from akashic.capture.ports import Rect

MONITOR = Rect(-100, 10, -60, 30)  # 40x20, negative desktop coordinates
RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
BLANK = (0, 0, 0)


def solid(rect: Rect, colour: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (rect.width, rect.height), colour)


def frame(rect: Rect, colour: tuple[int, int, int], *, listed: bool = True) -> Layer:
    return Layer(rect, "frame", listed=listed, image=solid(rect, colour))


def other(rect: Rect, kind: LayerKind) -> Layer:
    return Layer(rect, kind, listed=True, image=None)


def pixel(result: Composite, desktop_x: int, desktop_y: int) -> tuple[int, int, int]:
    assert result.image is not None
    value = result.image.getpixel((desktop_x - MONITOR.left, desktop_y - MONITOR.top))
    assert isinstance(value, tuple)
    return (int(value[0]), int(value[1]), int(value[2]))


def test_image_is_rgb_monitor_size_with_offset_mapping() -> None:
    result = compose(MONITOR, [frame(Rect(-90, 15, -70, 25), RED)])
    assert result.image is not None
    assert result.image.mode == "RGB"
    assert result.image.size == (40, 20)
    assert pixel(result, -90, 15) == RED
    assert pixel(result, -71, 24) == RED
    assert pixel(result, -91, 15) == BLANK
    assert pixel(result, -70, 15) == BLANK


def test_later_layer_wins_whatever_kind() -> None:
    full = MONITOR
    inner = Rect(-90, 15, -70, 25)
    result = compose(MONITOR, [frame(full, RED), frame(inner, GREEN)])
    assert pixel(result, -80, 20) == GREEN
    assert pixel(result, -95, 12) == RED
    result = compose(MONITOR, [frame(inner, GREEN), frame(full, RED)])
    assert pixel(result, -80, 20) == RED


def test_frame_image_cropped_to_rect_and_top_left_aligned() -> None:
    rect = Rect(-90, 15, -80, 25)
    image = Image.new("RGB", (30, 30), BLUE)
    image.paste(RED, (0, 0, 1, 1))
    result = compose(MONITOR, [Layer(rect, "frame", listed=True, image=image)])
    assert pixel(result, -90, 15) == RED
    assert pixel(result, -89, 15) == BLUE
    assert pixel(result, -80, 15) == BLANK  # image pixels outside the rect dropped


def test_small_image_leaves_rest_of_rect_blank() -> None:
    rect = Rect(-90, 15, -70, 25)
    small = Image.new("RGB", (5, 5), RED)
    result = compose(MONITOR, [Layer(rect, "frame", listed=True, image=small)])
    assert pixel(result, -86, 19) == RED
    assert pixel(result, -85, 15) == BLANK
    assert pixel(result, -90, 20) == BLANK


def test_frame_partly_off_monitor_is_clipped() -> None:
    rect = Rect(-120, 0, -90, 20)
    image = Image.new("RGB", (30, 20), BLUE)
    image.paste(RED, (20, 10, 21, 11))  # desktop (-100, 10), monitor origin
    result = compose(MONITOR, [Layer(rect, "frame", listed=True, image=image)])
    assert pixel(result, -100, 10) == RED
    assert pixel(result, -99, 10) == BLUE
    assert pixel(result, -90, 10) == BLANK


def test_excluded_and_pending_blank_what_is_below() -> None:
    area = Rect(-90, 15, -70, 25)
    for kind in ("excluded", "pending"):
        layers = [frame(MONITOR, RED), frame(Rect(-95, 12, -65, 28), GREEN), other(area, kind)]
        result = compose(MONITOR, layers)
        assert pixel(result, -80, 20) == BLANK
        assert pixel(result, -94, 13) == GREEN
        assert pixel(result, -99, 11) == RED


def test_blank_clipped_to_monitor() -> None:
    layers = [frame(MONITOR, RED), other(Rect(-120, 0, -95, 15), "pending")]
    result = compose(MONITOR, layers)
    assert pixel(result, -100, 10) == BLANK
    assert pixel(result, -95, 10) == RED
    assert result.image is not None
    assert result.image.size == (40, 20)


def test_unlisted_frame_painted_but_not_counted() -> None:
    bar = Rect(-100, 25, -60, 30)
    layers = [other(Rect(-100, 10, -60, 25), "excluded"), frame(bar, GREEN, listed=False)]
    result = compose(MONITOR, layers)
    assert result.only_hidden
    assert result.image is None


def test_has_hidden_only_for_excluded_layers() -> None:
    area = Rect(-90, 15, -70, 25)
    visible = compose(MONITOR, [frame(MONITOR, RED), other(area, "excluded")])
    assert visible.has_hidden
    assert not visible.only_hidden
    assert not compose(MONITOR, [frame(MONITOR, RED), other(area, "pending")]).has_hidden


def test_only_hidden_private_window_with_unlisted_taskbar() -> None:
    layers = [
        frame(MONITOR, RED, listed=False),
        other(Rect(-100, 10, -60, 28), "excluded"),
        frame(Rect(-100, 28, -60, 30), BLUE, listed=False),
    ]
    result = compose(MONITOR, layers)
    assert result.has_hidden
    assert result.only_hidden
    assert result.image is None
    assert not result.black


def test_excluded_with_visible_pending_is_not_only_hidden() -> None:
    layers = [
        other(Rect(-100, 10, -80, 30), "excluded"),
        other(Rect(-80, 10, -60, 30), "pending"),
    ]
    result = compose(MONITOR, layers)
    assert result.has_hidden
    assert not result.only_hidden
    assert result.image is not None


def test_excluded_with_visible_listed_frame_is_not_only_hidden() -> None:
    layers = [
        other(Rect(-100, 10, -80, 30), "excluded"),
        frame(Rect(-80, 10, -60, 30), RED),
    ]
    result = compose(MONITOR, layers)
    assert result.has_hidden
    assert not result.only_hidden
    assert not result.black


def test_black_video_next_to_excluded_area_is_black() -> None:
    layers = [
        frame(Rect(-100, 10, -80, 30), BLANK),
        other(Rect(-80, 10, -60, 30), "excluded"),
    ]
    result = compose(MONITOR, layers)
    assert result.image is not None
    assert result.black
    assert not result.only_hidden


def test_black_follows_is_black_of_whole_composite() -> None:
    assert not compose(MONITOR, [frame(MONITOR, RED)]).black
    assert compose(MONITOR, [frame(MONITOR, BLANK)]).black


def test_only_pending_or_no_layers_is_black() -> None:
    for layers in ([other(MONITOR, "pending")], []):
        result = compose(MONITOR, layers)
        assert result.black
        assert not result.has_hidden
        assert not result.only_hidden
        assert result.image is not None
        assert result.image.size == (40, 20)


def test_off_monitor_or_zero_size_layers_paint_nothing() -> None:
    layers = [
        frame(MONITOR, RED),
        other(Rect(0, 0, 50, 50), "excluded"),
        other(Rect(-90, 15, -90, 25), "excluded"),
        other(Rect(-80, 20, -80, 20), "pending"),
    ]
    result = compose(MONITOR, layers)
    assert not result.black
    assert pixel(result, -90, 20) == RED


def test_inputs_not_modified() -> None:
    image = Image.new("RGB", (40, 20), RED)
    before = image.tobytes()
    layers = [
        Layer(MONITOR, "frame", listed=True, image=image),
        other(Rect(-90, 15, -70, 25), "excluded"),
    ]
    compose(MONITOR, layers)
    assert image.tobytes() == before
    assert len(layers) == 2
