"""Plain data types and the Protocols that ``platform/win`` implements.

Core capture code depends only on these, so it runs against fakes on any OS. Platform code only
reports facts (window geometry, text, "this field is a password"); core code decides what to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    from numpy.typing import NDArray
    from PIL import Image

type Trigger = Literal["focus", "title", "input", "idle", "game"]
type PlaybackState = Literal["playing", "paused", "stopped"]
type BrowserFamily = Literal["chromium", "firefox"]

# Browsers whose address bar we can read (AGENTS.md: Chrome, Brave; Firefox, Zen).
BROWSERS: Final[dict[str, BrowserFamily]] = {
    "chrome.exe": "chromium",
    "brave.exe": "chromium",
    "msedge.exe": "chromium",
    "firefox.exe": "firefox",
    "zen.exe": "firefox",
}


@dataclass(frozen=True, slots=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def intersect(self, other: Rect) -> Rect | None:
        left, top = max(self.left, other.left), max(self.top, other.top)
        right, bottom = min(self.right, other.right), min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return None
        return Rect(left, top, right, bottom)

    def translate(self, dx: int, dy: int) -> Rect:
        return Rect(self.left + dx, self.top + dy, self.right + dx, self.bottom + dy)


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    process: str  # lower-case executable name, e.g. "chrome.exe"; "" when unknown
    title: str
    rect: Rect  # screen coordinates (physical pixels)
    # False for borderless windows (no caption bar), which is how fullscreen games look.
    has_frame: bool = True

    @property
    def key(self) -> tuple[int, str]:
        """Identity for "is this still the same window showing the same thing"."""
        return self.hwnd, self.title


class ScreenGrabber(Protocol):
    def monitors(self) -> list[Rect]:
        """Monitor rectangles in virtual-screen coordinates, in a stable order."""
        ...

    def grab(self, rect: Rect) -> Image.Image:
        """One RGB frame of ``rect``, taken now. Never streams; called only on demand."""
        ...

    def grab_window(self, window: WindowInfo) -> Image.Image | None:
        """Only this window's pixels, or None if unavailable; never fall back to a monitor."""
        ...


class WindowSource(Protocol):
    def focused_window(self) -> WindowInfo | None: ...

    def inspect_window(self, hwnd: int) -> WindowInfo | None:
        """Fresh identity/geometry for a still-visible window; None if it has gone away."""
        ...

    def window_at(self, x: int, y: int) -> WindowInfo | None:
        """The top-level window under a screen point (used for non-focused monitors)."""
        ...

    def cursor_pos(self) -> tuple[int, int]: ...

    def last_input_ms(self) -> int:
        """A millisecond tick that changes whenever the user types, clicks, scrolls or moves
        the mouse. Compare for inequality only (it wraps). Reports when, never what."""
        ...

    def is_locked(self) -> bool:
        """True while the session is locked or the secure desktop is showing."""
        ...


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    rect: Rect | None = None
    content: bool = True  # False for buttons, tabs, menus and other interface controls


class TextSource(Protocol):
    """UI Automation. Every method returns quietly empty when the target vanished."""

    def focused_is_password(self) -> bool | None:
        """True when the focused element is a password field. False when it is not or when
        nothing has focus. None when it could not be determined (UI Automation failed): callers
        must treat that as "maybe", never as "no"."""
        ...

    def read_text(self, window: WindowInfo, max_nodes: int) -> Sequence[TextBlock] | None:
        """Visible blocks with roles/geometry, or None on failure."""
        ...

    def browser_caption(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        """The browser frame's own accessible name, or None when it can't be found. Chromium
        browsers write their private-mode marker here ("Page - Google Chrome (Incognito)") and
        not into the Win32 window title."""
        ...

    def read_url(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        """The omnibox/URL bar's text, or None when it can't be found (an app or kiosk window
        has none; an in-page input is never taken for it)."""
        ...


class OcrEngine(Protocol):
    def recognize(self, image: Image.Image) -> str: ...


@dataclass(frozen=True, slots=True)
class MediaState:
    """One media session as the OS publishes it right now."""

    app_id: str  # opaque AUMID, e.g. "Chrome" or "F0DC299D809B9700"
    title: str
    artist: str
    state: PlaybackState


class MediaSource(Protocol):
    def sessions(self) -> list[MediaState]:
        """All current media sessions (empty when there are none or the OS can't be asked)."""
        ...

    def app_name(self, app_id: str) -> str | None:
        """Display name of the app behind an AUMID ("Zen"), or None when unknown."""
        ...


type AudioKind = Literal["mic", "system"]


class AudioError(Exception):
    """An audio device could not be opened or died while recording (unplugged, in use...)."""


@dataclass(frozen=True, slots=True)
class AudioDevice:
    device_id: str  # opaque endpoint id
    name: str  # friendly name, e.g. "Mic In (Elgato Wave:3)"


class AudioInput(Protocol):
    """One open recording stream, already converted to mono float32 at 16 kHz."""

    def read(self, timeout_s: float) -> NDArray[np.float32] | None:
        """Whatever audio arrived, or None when nothing did within ``timeout_s``. Loopback
        devices deliver nothing while nothing plays. Raises AudioError when the device is gone."""
        ...

    def close(self) -> None: ...


class AudioDevices(Protocol):
    def find(self, kind: AudioKind, name_contains: str) -> AudioDevice | None:
        """The default device of ``kind`` (``name_contains`` empty) or the first active one
        whose name contains the text (case-insensitive). "system" devices are playback
        endpoints, recorded through loopback."""
        ...

    def names(self, kind: AudioKind) -> list[str]:
        """Friendly names of every active device of `kind`, for the Settings page."""
        ...

    def open(self, kind: AudioKind, device: AudioDevice) -> AudioInput: ...


class AudioSessions(Protocol):
    def active_processes(self) -> frozenset[str] | None:
        """Lower-case executable names of processes making sound right now (any playback
        device). Empty when nothing is audible; None when the OS could not be asked, which is
        not the same thing: nobody knows what was playing."""
        ...


@dataclass(frozen=True, slots=True)
class AudioPlatform:
    """What the audio recorder needs from the OS, bundled for wiring."""

    devices: AudioDevices
    sessions: AudioSessions


@dataclass(frozen=True, slots=True)
class CapturePlatform:
    """Everything the capture process needs from the OS, bundled for wiring."""

    screen: ScreenGrabber
    windows: WindowSource
    uia: TextSource
    ocr: OcrEngine
    media: MediaSource
