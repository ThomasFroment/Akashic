# pyright: basic
"""Windows implementations of the capture ports. Reports facts; never makes policy decisions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from akashic.capture.ports import AudioPlatform, CapturePlatform
from akashic.platform.win.audio import WindowsAudioDevices, WindowsAudioSessions
from akashic.platform.win.media import WindowsMedia
from akashic.platform.win.screen import GdiScreen, set_dpi_aware
from akashic.platform.win.uia import WindowsUia
from akashic.platform.win.windows import Win32Windows

if TYPE_CHECKING:
    from PIL import Image

    from akashic.capture.ports import OcrEngine

log = logging.getLogger(__name__)


class _NoOcr:
    """Used when no Windows OCR recognizer is installed: frames just get no OCR text."""

    def recognize(self, image: Image.Image) -> str:
        del image
        return ""


def build_capture_platform(ocr_language: str) -> CapturePlatform:
    """Create the real implementations. Call from the capture process's main thread."""
    set_dpi_aware()
    ocr: OcrEngine
    try:
        from akashic.platform.win.ocr import WindowsOcr  # noqa: PLC0415 - imports WinRT lazily

        ocr = WindowsOcr(ocr_language)
    except Exception as exc:  # noqa: BLE001 - OCR is optional; capture must still run
        log.warning("OCR unavailable (%s); frames without UIA text get no text", type(exc).__name__)
        ocr = _NoOcr()
    return CapturePlatform(
        screen=GdiScreen(),
        windows=Win32Windows(),
        uia=WindowsUia(),
        ocr=ocr,
        media=WindowsMedia(),
    )


def build_audio_platform() -> AudioPlatform:
    """Microphone and loopback devices, and the "who is making sound" probe."""
    return AudioPlatform(devices=WindowsAudioDevices(), sessions=WindowsAudioSessions())
