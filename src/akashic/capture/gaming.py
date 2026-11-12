"""Fullscreen-game detection (pure logic; the platform only reports window geometry).

A game is the foreground window when it covers a whole monitor, has no caption bar (borderless
or exclusive fullscreen) and is not a program that merely goes fullscreen: browsers (F11, video),
media players, the desktop shell. Both lists are configurable, and ``games`` in the config forces
detection for a process that does have a frame.

Leaving the game takes a short grace period so alt-tab flicker and notification windows don't
split one session into many.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.capture.ports import BROWSERS

if TYPE_CHECKING:
    from akashic.capture.ports import Rect, WindowInfo

# Programs that are fullscreen without being a game.
DEFAULT_NOT_GAMES: Final = (
    *BROWSERS,
    "explorer.exe",
    "applicationframehost.exe",
    "searchhost.exe",
    "shellexperiencehost.exe",
    "lockapp.exe",
    "vlc.exe",
    "mpv.exe",
    "mpc-hc64.exe",
    "mpc-hc.exe",
    "potplayermini64.exe",
    "wmplayer.exe",
    "powerpnt.exe",
    "teams.exe",
    "zoom.exe",
    "discord.exe",
)


@dataclass(frozen=True, slots=True)
class GameSettings:
    interval_s: float = 60.0  # one screenshot per this many seconds while a game is in front
    end_grace_s: float = 5.0  # how long a non-game foreground window must last to end a game
    heartbeat_s: float = 10.0  # how often the open game row's end time is refreshed
    games: tuple[str, ...] = ()  # always treated as games when fullscreen (lower-case .exe)
    not_games: tuple[str, ...] = ()  # added to DEFAULT_NOT_GAMES


@dataclass(frozen=True, slots=True)
class Game:
    process: str
    title: str


def covers_monitor(window: Rect, monitors: list[Rect]) -> Rect | None:
    """The monitor the window completely covers, if any."""
    for monitor in monitors:
        if (
            window.left <= monitor.left
            and window.top <= monitor.top
            and window.right >= monitor.right
            and window.bottom >= monitor.bottom
        ):
            return monitor
    return None


def is_game_window(window: WindowInfo, monitors: list[Rect], settings: GameSettings) -> bool:
    if not window.process or covers_monitor(window.rect, monitors) is None:
        return False
    if window.process in settings.games:
        return True
    return not window.has_frame and window.process not in (*DEFAULT_NOT_GAMES, *settings.not_games)


@dataclass(frozen=True, slots=True)
class Transition:
    ended: Game | None = None
    started: Game | None = None


class GameMode:
    """Tracks whether a game is in the foreground. Feed it the focused window every tick."""

    def __init__(self, settings: GameSettings) -> None:
        self._settings = settings
        self.current: Game | None = None
        self._last_seen = 0.0

    @property
    def active(self) -> bool:
        return self.current is not None

    def observe(self, window: WindowInfo | None, monitors: list[Rect], now: float) -> Transition:
        game = (
            Game(window.process, window.title)
            if window is not None and is_game_window(window, monitors, self._settings)
            else None
        )
        if game is not None:
            self._last_seen = now
            if self.current is None:
                self.current = game
                return Transition(started=game)
            if self.current.process != game.process:  # straight from one game to another
                ended, self.current = self.current, game
                return Transition(ended=ended, started=game)
            return Transition()
        if self.current is not None and now - self._last_seen >= self._settings.end_grace_s:
            ended, self.current = self.current, None
            return Transition(ended=ended)
        return Transition()

    def stop(self) -> Game | None:
        """Forget the current game (pause, shutdown). Returns it so the caller can close its row."""
        ended, self.current = self.current, None
        return ended
