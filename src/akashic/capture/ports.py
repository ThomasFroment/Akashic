"""Plain data types and the Protocols that ``platform/win`` implements.

Core capture code depends only on these, so it runs against fakes on any OS. Platform code only
reports facts (window geometry, text); core code decides what to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    import numpy as np
    from numpy.typing import NDArray
    from PIL import Image

type Trigger = Literal["focus", "title", "input", "idle", "game", "window"]
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
FRAME_HOST: Final = "applicationframehost.exe"  # hosts every UWP window; the app behind is unknown
# Lower-case executable names whose sound means people are talking to each other.
VOICE_APPS: Final = frozenset(
    {
        "discord.exe", "discordptb.exe", "discordcanary.exe", "teams.exe", "ms-teams.exe",
        "zoom.exe", "skype.exe", "telegram.exe", "whatsapp.exe", "teamspeak.exe",
        "ts3client_win64.exe", "mumble.exe", "guilded.exe", "slack.exe", "signal.exe",
    }
)  # fmt: skip


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
    # True when the OS reports a Direct3D exclusive-fullscreen app (set on the focused window
    # only; not part of window identity).
    exclusive_fullscreen: bool = field(default=False, compare=False)

    @property
    def key(self) -> tuple[int, str]:
        """Identity for "is this still the same window showing the same thing"."""
        return self.hwnd, self.title


class WindowSource(Protocol):
    def focused_window(self) -> WindowInfo | None: ...

    def cursor_pos(self) -> tuple[int, int]: ...

    def last_input_ms(self) -> int:
        """A millisecond tick that changes whenever the user types, clicks, scrolls or moves
        the mouse. Compare for inequality only (it wraps). Reports when, never what."""
        ...

    def is_locked(self) -> bool:
        """True while the session is locked or the secure desktop is showing."""
        ...


type WindowChange = Literal["shown", "hidden", "destroyed", "moved", "title", "focus"]


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    key: str  # hash of the DisplayConfig device path; never enumeration order or LUID
    name: str
    rect: Rect  # virtual-screen physical pixels
    scale: float  # DPI / 96
    primary: bool


type EntryRole = Literal["app", "hidden", "shell"]


@dataclass(frozen=True, slots=True)
class Entry:
    """One window of a monitor picture, as the screen writer stores it. ``z`` 0 is the front.
    Only app entries carry content; hidden and shell entries are stored as a rectangle."""

    role: EntryRole
    hwnd: int | None
    rect: Rect
    z: int
    process: str | None = None
    title: str | None = None
    url: str | None = None
    focused: bool = False
    painted: bool = False  # the composite shows this window
    image: Image.Image | None = field(default=None, compare=False)  # the window's full capture


@dataclass(frozen=True, slots=True)
class TopWindow:
    """One top-level window as the OS reports it. Rects are virtual-screen physical pixels.

    Unknown values: ``process == ""``, ``ghost_of is None`` (only meaningful when ``is_ghost``),
    ``foreign_children is None``. ``owner is None`` (no owner, or unreadable) gets the
    ownerless-popup rule, which fails closed.
    """

    hwnd: int
    pid: int
    process: str  # lower-case executable name
    # ApplicationFrameHost window whose hosted app was not found; process stays that host.
    uwp_unresolved: bool
    class_name: str
    title: str
    rect: Rect
    minimized: bool
    cloaked: bool
    owner: int | None
    popup: bool
    tool: bool
    alpha: int | None  # constant alpha 0-255 when layered with LWA_ALPHA, else None
    is_ghost: bool
    ghost_of: int | None  # hwnd of the hung window this ghost replaces
    # Lower-case exe names of child windows owned by other processes; () means none.
    foreign_children: tuple[str, ...] | None
    has_frame: bool


@dataclass(frozen=True, slots=True)
class WindowEvent:
    hwnd: int
    change: WindowChange
    at: float  # time.monotonic() seconds


@dataclass(frozen=True, slots=True)
class WindowChanges:
    events: tuple[WindowEvent, ...]
    healthy: bool


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    seq: int  # one counter shared by all captures; higher means newer
    at: float  # time.monotonic() seconds
    image: Image.Image  # RGB, native window size


class Displays(Protocol):
    def monitors(self) -> list[MonitorInfo] | None:
        """Active monitors sorted by (left, top); a path without a matching HMONITOR, or an
        HMONITOR without a path, is left out. None when the OS could not be asked: any
        non-zero code (ACCESS_DENIED, GEN_FAILURE, NOT_SUPPORTED, INVALID_PARAMETER...), an
        EnumDisplayMonitors failure, or ERROR_INSUFFICIENT_BUFFER on all 5 tries."""
        ...


class WindowFacts(Protocol):
    def windows(self) -> list[TopWindow] | None:
        """The IsWindowVisible top-level windows, top of the z-order first. Minimized and
        cloaked windows are included and flagged. None when the OS could not be asked."""
        ...


class WindowEvents(Protocol):
    """Window change notifications. Never raises."""

    def start(self) -> None: ...

    def drain(self) -> WindowChanges:
        """Events since the last drain, oldest first. Mapping: show, UNCLOAKED (0x8018) and
        MINIMIZEEND (0x17) -> "shown"; hide, CLOAKED (0x8017) and MINIMIZESTART (0x16) ->
        "hidden"; destroy -> "destroyed"; LOCATIONCHANGE on OBJID_WINDOW and REORDER (0x8004) ->
        "moved"; NAMECHANGE on a top-level window (OBJID_WINDOW or OBJID_CLIENT) -> "title";
        foreground -> "focus".
        ``healthy`` is False when events may have been lost; the first drain after the event
        hook is reinstalled is still unhealthy."""
        ...

    def close(self) -> None: ...


class WindowCaptures(Protocol):
    """Per-window pixel streams. Never raises: a failure is a False ``start`` or an hwnd in
    ``failed()``, and both count as a capture failure. Device loss restarts every capture."""

    def start(self, hwnd: int, min_interval_ms: int) -> bool:
        """Begin capturing ``hwnd``, at most one frame per ``min_interval_ms``."""
        ...

    def stop(self, hwnd: int) -> None:
        """Stop capturing and discard the frame. Idempotent; unknown hwnds are ignored."""
        ...

    def latest_seq(self, hwnd: int) -> int | None:
        """Sequence of the newest frame, or None when none has arrived or it is not started."""
        ...

    def grab(self, hwnd: int) -> CapturedFrame | None:
        """A copy of the newest frame taken at call time, or None when there is none."""
        ...

    def failed(self) -> frozenset[int]:
        """The started hwnds whose capture has failed."""
        ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    rect: Rect | None = None
    content: bool = True  # False for buttons, tabs, menus and other interface controls


class TextSource(Protocol):
    """UI Automation. Every method returns quietly empty when the target vanished."""

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
    def active_processes(self, *, refresh: bool = False) -> dict[str, frozenset[str]] | None:
        """Lower-case executable names of processes making sound right now, per playback
        endpoint, keyed by ``AudioDevice.device_id``. An endpoint without a session manager is
        left out; an empty set is silence; None means the OS could not be asked, which is not
        the same thing: nobody knows what was playing. ``refresh`` re-reads the endpoints now."""
        ...


@dataclass(frozen=True, slots=True)
class AudioPlatform:
    """What the audio recorder needs from the OS, bundled for wiring."""

    devices: AudioDevices
    sessions: AudioSessions


@dataclass(frozen=True, slots=True)
class CapturePlatform:
    """Everything the capture process needs from the OS, bundled for wiring."""

    displays: Displays
    windows: WindowSource
    facts: WindowFacts
    events: WindowEvents
    captures: WindowCaptures
    uia: TextSource
    ocr: OcrEngine | None  # None: no recognizer is installed, frames get no OCR text
    media: MediaSource
