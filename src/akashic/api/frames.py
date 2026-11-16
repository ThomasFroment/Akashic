"""Frames in the API: one frame with its windows, and its image (whole, or one window masked)."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, HTTPException, Query, Response
from PIL import Image

from akashic.capture import occlusion
from akashic.capture.ports import Rect
from akashic.search import keyword
from akashic.storage import monitors
from akashic.storage.crypto import CryptoError

if TYPE_CHECKING:
    from sqlite3 import Connection

    from akashic.api.server import Services
    from akashic.storage.frames import StoredWindow

log = logging.getLogger("api")

type Json = dict[str, object]

MAX_THUMB_PX: Final = 1920


@dataclass(frozen=True, slots=True)
class _Located:
    """A painted app window of a stored frame, with what lay in front of it and the monitor."""

    window: Rect
    in_front: list[Rect]
    monitor: Rect


@dataclass(frozen=True, slots=True)
class _Crop:
    origin: tuple[int, int]
    bounds: Rect
    visible: list[Rect]


def _find_image(
    services: Services, conn: Connection, frame_id: int, window_id: int | None
) -> tuple[str, _Located | None]:
    """The stored image path, and for a window the geometry of its crop, or raise 404."""
    if window_id is None:
        image_path = services.frames.image_path(frame_id)
        if image_path is None:
            raise HTTPException(404, "no image for this frame")
        return image_path, None
    stored = services.frames.get(frame_id)
    if stored is None:
        raise HTTPException(404, "no such frame")
    if stored.image_path is None:
        raise HTTPException(404, "no image for this frame")
    row = next((r for r in stored.windows if r.id == window_id), None)
    if row is None or row.role != "app" or not row.painted:
        raise HTTPException(404, "no painted app window with this id in the frame")
    monitor = monitors.monitor_rect(conn, stored.monitor_id, stored.ts_ms)
    if monitor is None:
        raise HTTPException(404, "no layout covers this frame's monitor")
    located = _Located(
        Rect(row.left, row.top, row.right, row.bottom),
        [Rect(r.left, r.top, r.right, r.bottom) for r in stored.windows if r.z < row.z],
        Rect(*monitor),
    )
    return stored.image_path, located


def _crop_of(located: _Located) -> _Crop:
    bounds = located.window.intersect(located.monitor)
    visible = [] if bounds is None else occlusion.visible_parts(bounds, located.in_front)
    if bounds is None or not visible:
        raise HTTPException(404, "the window is not visible in this frame")
    return _Crop((located.monitor.left, located.monitor.top), bounds, visible)


def _render(webp: bytes, width: int | None, crop: _Crop | None) -> bytes | None:
    """The image, cropped and masked if ``crop``, fitted to ``(width, 4 * width)`` if ``width``.

    None when the crop shows nothing of the image.
    """
    with Image.open(io.BytesIO(webp)) as img:
        shown = (
            img
            if crop is None
            else occlusion.masked_crop(img, crop.origin, crop.bounds, crop.visible)
        )
        if shown is None:
            return None
        if width:
            shown.thumbnail((width, width * 4))
        out = io.BytesIO()
        shown.save(out, "WEBP", quality=70)
        return out.getvalue()


def _window_json(window: StoredWindow) -> Json:
    """A window of a frame; hidden and shell rows are content-free. The hwnd is never sent."""
    rect = [window.left, window.top, window.right, window.bottom]
    if window.role != "app":
        return {"id": window.id, "role": window.role, "rect": rect, "z": window.z}
    return {
        "id": window.id,
        "role": window.role,
        "process": window.process,
        "title": window.title,
        "url": window.url,
        "rect": rect,
        "z": window.z,
        "focused": window.focused,
        "painted": window.painted,
        "text": window.text,
        "text_source": window.text_source,
    }


def frame_routes(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/frames/{frame_id}")
    def frame(frame_id: int) -> Json:  # pyright: ignore[reportUnusedFunction]
        with services.db() as conn:
            stored = services.frames.get(frame_id)
            if stored is None:
                raise HTTPException(404, "no such frame")
            prev_id, next_id = keyword.neighbors(conn, frame_id)
        return {
            "id": stored.id,
            "ts_ms": stored.ts_ms,
            "monitor_id": stored.monitor_id,
            "has_image": stored.image_path is not None,
            "prev_id": prev_id,
            "next_id": next_id,
            "windows": [_window_json(w) for w in stored.windows],
        }

    @router.get("/frames/{frame_id}/image")
    def frame_image(  # pyright: ignore[reportUnusedFunction]
        frame_id: int,
        w: Annotated[int | None, Query(ge=16, le=MAX_THUMB_PX)] = None,
        window: Annotated[int | None, Query(ge=1)] = None,
    ) -> Response:
        # The generation keeps a render begun before a purge out of the cleared cache; the path
        # in the key keeps a reused id off old pixels. A purge during the file read can 409.
        with services.db() as conn:
            image_path, located = _find_image(services, conn, frame_id, window)
            cache_key = (frame_id, image_path, window or 0, w or 0)
            generation = services.thumb_generation()
        crop = None if located is None else _crop_of(located)
        cached = services.thumb(cache_key) if w else None
        if cached is not None:
            return Response(cached, media_type="image/webp")
        try:
            webp = services.frames.image(image_path)
        except FileNotFoundError as exc:
            raise HTTPException(404, "image file is missing") from exc
        except CryptoError as exc:
            log.warning("frame %d image failed to decrypt", frame_id)
            raise HTTPException(500, "image could not be decrypted") from exc
        if crop is None and not w:
            return Response(webp, media_type="image/webp")
        rendered = _render(webp, w, crop)
        if rendered is None:
            raise HTTPException(404, "the window is not visible in this frame")
        if w:
            services.put_thumb(cache_key, rendered, generation)
        return Response(rendered, media_type="image/webp")

    return router
