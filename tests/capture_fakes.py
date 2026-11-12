"""In-memory stand-ins for the platform ports, shared by the capture tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from akashic.capture.ports import Rect, TextBlock, WindowInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from akashic.capture.ports import BrowserFamily, MediaState

SCREEN = Rect(0, 0, 800, 600)


def blank(color: str = "white", size: tuple[int, int] = (800, 600)) -> Image.Image:
    return Image.new("RGB", size, color)


def with_text_block(
    image: Image.Image, box: tuple[int, int, int, int] = (100, 100, 400, 200)
) -> Image.Image:
    """Paint vertical bars that look like a block of text to the text-region detector."""
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = box
    for x in range(left, right, 24):
        draw.rectangle((x, top, x + 5, bottom), fill="black")
    return image


def window(
    hwnd: int = 1,
    process: str = "notepad.exe",
    title: str = "Notes",
    rect: Rect = SCREEN,
) -> WindowInfo:
    return WindowInfo(hwnd, process, title, rect)


@dataclass
class FakeScreen:
    rects: list[Rect] = field(default_factory=lambda: [SCREEN])
    frames: dict[int, Image.Image] = field(default_factory=dict[int, Image.Image])
    grabs: int = 0
    fail: bool = False
    unavailable: bool = False
    on_grab: Callable[[], None] | None = None

    def monitors(self) -> list[Rect]:
        return self.rects

    def grab(self, rect: Rect) -> Image.Image:
        if self.fail:
            msg = "grab failed"
            raise OSError(msg)
        self.grabs += 1
        index = self.rects.index(rect)
        return self.frames.get(index, blank()).copy()

    def grab_window(self, window: WindowInfo) -> Image.Image | None:
        if self.on_grab:
            self.on_grab()
        if self.unavailable:
            return None
        monitor = next((r for r in self.rects if r.contains(*window.rect.center)), None)
        if monitor is None:
            return None
        image = self.grab(monitor)
        rect = window.rect.translate(-monitor.left, -monitor.top)
        return image.crop((rect.left, rect.top, rect.right, rect.bottom))


@dataclass
class FakeWindows:
    focused: WindowInfo | None = None
    at: dict[tuple[int, int], WindowInfo] = field(default_factory=dict[tuple[int, int], WindowInfo])
    cursor: tuple[int, int] = (10, 10)
    locked: bool = False
    last_input: int = 0

    def focused_window(self) -> WindowInfo | None:
        return self.focused

    def inspect_window(self, hwnd: int) -> WindowInfo | None:
        return next((w for w in [self.focused, *self.at.values()] if w and w.hwnd == hwnd), None)

    def window_at(self, x: int, y: int) -> WindowInfo | None:
        return self.at.get((x, y))

    def cursor_pos(self) -> tuple[int, int]:
        return self.cursor

    def is_locked(self) -> bool:
        return self.locked

    def last_input_ms(self) -> int:
        return self.last_input


@dataclass
class FakeUia:
    """Plain strings in ``texts`` are shorthand for content blocks; ``read_text`` always returns
    ``TextBlock``s, as the production UIA reader does."""

    password: bool | None = False  # None: UI Automation failed, the state is unknown
    texts: dict[int, Sequence[TextBlock | str] | None] = field(
        default_factory=dict[int, "Sequence[TextBlock | str] | None"]
    )
    urls: dict[int, str | None] = field(default_factory=dict[int, str | None])
    captions: dict[int, str | None] = field(default_factory=dict[int, str | None])
    text_reads: list[int] = field(default_factory=list[int])

    def focused_is_password(self) -> bool | None:
        return self.password

    def read_text(self, window: WindowInfo, max_nodes: int) -> list[TextBlock] | None:
        del max_nodes
        self.text_reads.append(window.hwnd)
        parts = self.texts.get(window.hwnd)
        if parts is None:
            return None
        return [TextBlock(p) if isinstance(p, str) else p for p in parts]

    def browser_caption(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        del family
        # A window's caption is its title unless a test says otherwise; None is "unreadable".
        return self.captions.get(window.hwnd, window.title)

    def read_url(self, window: WindowInfo, family: BrowserFamily) -> str | None:
        del family
        return self.urls.get(window.hwnd)


@dataclass
class FakeOcr:
    text: str = "recognized words"
    calls: int = 0

    def recognize(self, image: Image.Image) -> str:
        del image
        self.calls += 1
        return self.text


@dataclass
class FakeMedia:
    states: list[MediaState] = field(default_factory=list["MediaState"])
    names: dict[str, str] = field(default_factory=dict[str, str])
    fail: bool = False

    def sessions(self) -> list[MediaState]:
        if self.fail:
            msg = "media unavailable"
            raise OSError(msg)
        return list(self.states)

    def app_name(self, app_id: str) -> str | None:
        return self.names.get(app_id)


LONG_TEXT = [TextBlock("A line of text that is long enough to count as real window text.")]

HOSTED_TITLES = {
    "keepass.exe": "KeePass",
    "keepassxc.exe": "KeePassXC",
    "bitwarden.exe": "Bitwarden",
    "1password.exe": "1Password",
    "lastpass.exe": "LastPass",
    "dashlane.exe": "Dashlane",
    "enpass.exe": "Enpass",
    "nordpass.exe": "NordPass",
    "proton pass.exe": "Proton Pass",
    "protonpass.exe": "ProtonPass",
    "roboform.exe": "RoboForm Password Manager",
    "keeper.exe": "Keeper Password Manager",
}

AUMID = "308046B0AF4A39CB"  # a browser's app id, as Windows reports it for media sessions
