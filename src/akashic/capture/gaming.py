"""Fullscreen-game detection (pure logic; the platform only reports window geometry).

A game is the foreground window when it covers a whole monitor and either Windows reports a
Direct3D exclusive-fullscreen app, or it has no caption bar (borderless) and is not a program that
merely goes fullscreen: browsers (F11, video), media players, meeting and remote-desktop clients,
the desktop shell. Both lists are configurable and win over the heuristic: ``games`` forces
detection for a process that does have a frame, ``not_games`` overrides even D3D evidence.

Leaving the game takes a short grace period so alt-tab flicker and notification windows don't
split one session into many.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from akashic.capture.ports import BROWSERS, FRAME_HOST

if TYPE_CHECKING:
    from akashic.capture.ports import Rect, WindowInfo

# Programs that are fullscreen without being a game.
DEFAULT_NOT_GAMES: Final = (
    *BROWSERS,
    # shell
    "explorer.exe",
    FRAME_HOST,
    "searchhost.exe",
    "shellexperiencehost.exe",
    "lockapp.exe",
    # media players
    "vlc.exe",
    "mpv.exe",
    "mpvnet.exe",
    "mpc-hc64.exe",
    "mpc-hc.exe",
    "mpc-be64.exe",
    "mpc-be.exe",
    "potplayermini64.exe",
    "potplayermini.exe",
    "potplayer64.exe",
    "potplayer.exe",
    "smplayer.exe",
    "kmplayer.exe",
    "kmplayer64.exe",
    "microsoft.media.player.exe",
    "video.ui.exe",
    "wmplayer.exe",
    # presentation
    "powerpnt.exe",
    # meetings and chat
    "teams.exe",
    "ms-teams.exe",
    "zoom.exe",
    "discord.exe",
    # remote desktop and VMs
    "mstsc.exe",
    "msrdc.exe",
    "vmconnect.exe",
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
    if window.process in settings.not_games:
        return False
    if window.exclusive_fullscreen:
        return True
    return not window.has_frame and window.process not in DEFAULT_NOT_GAMES


@dataclass(frozen=True, slots=True)
class Transition:
    ended: Game | None = None
    ended_ms: int = 0  # wall clock of the last tick that saw the ended game in front
    started: Game | None = None


class GameMode:
    """Tracks whether a game is in the foreground. Feed it the focused window every tick."""

    def __init__(self, settings: GameSettings) -> None:
        self._settings = settings
        self.current: Game | None = None
        self._last_seen = 0.0
        self._last_seen_ms = 0  # wall clock of the same tick

    @property
    def active(self) -> bool:
        return self.current is not None

    def observe(
        self, window: WindowInfo | None, monitors: list[Rect], now: float, ts_ms: int
    ) -> Transition:
        game = (
            Game(window.process, window.title)
            if window is not None and is_game_window(window, monitors, self._settings)
            else None
        )
        if game is not None:
            ended_ms = self._last_seen_ms
            self._last_seen, self._last_seen_ms = now, ts_ms
            if self.current is None:
                self.current = game
                return Transition(started=game)
            if self.current.process != game.process:  # straight from one game to another
                ended, self.current = self.current, game
                return Transition(ended=ended, ended_ms=ended_ms, started=game)
            return Transition()
        if self.current is not None and now - self._last_seen >= self._settings.end_grace_s:
            ended, self.current = self.current, None
            return Transition(ended=ended, ended_ms=self._last_seen_ms)
        return Transition()

    def stop(self) -> Transition:
        """Forget the current game (pause, shutdown). Returns its end so the caller can close its
        row."""
        ended, self.current = self.current, None
        return Transition(ended=ended, ended_ms=self._last_seen_ms)
