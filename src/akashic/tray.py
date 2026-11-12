"""System tray icon: status, open the UI, pause/resume, lock, quit.

Runs pystray's message loop on the calling (main) thread. Everything it does is delegated to
callbacks, so no capture or security logic lives here. Manual smoke test: tests/manual/tray.md.
"""

from __future__ import annotations

import logging
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pystray
from PIL import Image, ImageDraw, ImageFilter

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("tray")

ACCENT: Final = (201, 100, 66, 255)  # the UI's light-theme accent
GREY: Final = (140, 136, 126, 255)
SIZE: Final = 64
LOGO: Final = Path(__file__).resolve().parent / "ui" / "logo.png"

LABELS: Final = {
    "recording": "Recording",
    "gaming": "Recording (game mode)",
    "backlog": "Recording (catching up on transcripts)",
    "paused": "Paused",
    "starting": "Starting…",
    "stopped": "Stopped",
}


@cache
def _logo_alpha() -> Image.Image:
    """The app's logo (the same mask the web UI colours), as an alpha channel of SIZE pixels."""
    with Image.open(LOGO) as logo:
        alpha = logo.convert("RGBA").getchannel("A").resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    return alpha.filter(ImageFilter.MaxFilter(3))  # bolder: a tray icon is shown at 16-24 px


def make_icon(state: str) -> Image.Image:
    """The logo: terracotta while recording, grey with a pause sign when paused, faded terracotta
    while starting or stopped."""
    alpha = _logo_alpha()
    colour = ACCENT if state != "paused" else GREY
    if state not in {"recording", "gaming", "backlog", "paused"}:
        alpha = alpha.point([v * 60 // 100 for v in range(256)])
    img = Image.new("RGBA", (SIZE, SIZE), colour)
    img.putalpha(alpha)
    if state == "paused":  # in the empty centre of the logo
        draw = ImageDraw.Draw(img)
        draw.rectangle((24, 22, 29, 42), fill=GREY)
        draw.rectangle((35, 22, 40, 42), fill=GREY)
    return img


class Tray:
    def __init__(
        self,
        *,
        state: Callable[[], str],
        open_ui: Callable[[], None],
        toggle_pause: Callable[[], None],
        quit_app: Callable[[], None],
        lock_app: Callable[[], None] = lambda: None,
    ) -> None:
        self._state = state
        self._quit_app = quit_app
        self._lock_app = lock_app
        self._shown = state()

        def toggle_label(_item: pystray.MenuItem) -> str:
            return "Resume" if self._state() == "paused" else "Pause"

        def status_label(_item: pystray.MenuItem) -> str:
            return LABELS.get(self._state(), self._state())

        menu = pystray.Menu(
            pystray.MenuItem("Open Akashic", open_ui, default=True),
            pystray.MenuItem(toggle_label, toggle_pause),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(status_label, None, enabled=False),
            pystray.MenuItem("Lock", self.lock),
            pystray.MenuItem("Quit", self.quit),
        )
        self._icon = pystray.Icon("akashic", make_icon(self._shown), "Akashic", menu)

    def quit(self) -> None:
        self._quit_app()
        self._icon.stop()

    def lock(self) -> None:
        """Stop everything and forget the key; the app asks for the passphrase again."""
        self._lock_app()
        self._icon.stop()

    def refresh(self) -> None:
        """Redraw the icon if the state changed. Safe to call from any thread."""
        state = self._state()
        if state != self._shown:
            self._shown = state
            self._icon.icon = make_icon(state)
            self._icon.title = f"Akashic: {LABELS.get(state, state)}"
            self._icon.update_menu()

    def run(self) -> None:
        self._icon.run()

    def stop(self) -> None:
        self._icon.stop()
