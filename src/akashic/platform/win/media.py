# pyright: basic
"""Windows media sessions (GlobalSystemMediaTransportControls) and AUMID -> app name.

Native check (docs/CAPTURE.md): works from unpackaged Python in ~13 ms; YouTube in Zen publishes
status, title and channel. The source app is an opaque AUMID, resolved to a display name through
the Start-menu app list (Shell.Application, ~0.4 s once, no subprocess).

Only ever called from the media thread: the asyncio loop and the COM objects belong to it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import TYPE_CHECKING, Final

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as Manager,
)

from akashic.capture.ports import MediaState

if TYPE_CHECKING:
    from collections.abc import Callable

    from akashic.capture.ports import PlaybackState

log = logging.getLogger(__name__)

_STATES: dict[str, PlaybackState] = {"playing": "playing", "paused": "paused"}
# The app list is read again for a name it did not have (an app installed or started since, or a
# listing that failed), but not more often than this: it takes ~0.4 s.
NAMES_RELOAD_S: Final = 60.0


class WindowsMedia:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._manager: Manager | None = None
        self._names: dict[str, str] | None = None
        self._clock = clock
        self._names_loaded_at = float("-inf")

    def sessions(self) -> list[MediaState]:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(self._read())

    async def _read(self) -> list[MediaState]:
        if self._manager is None:
            self._manager = await Manager.request_async()
        found: list[MediaState] = []
        for session in self._manager.get_sessions():
            try:
                props = await session.try_get_media_properties_async()
                if props is None:
                    continue
                status = session.get_playback_info().playback_status.name.casefold()
                found.append(
                    MediaState(
                        app_id=session.source_app_user_model_id,
                        title=props.title or "",
                        artist=props.artist or "",
                        state=_STATES.get(status, "stopped"),
                    )
                )
            except OSError:  # the session closed while we were reading it
                continue
        return found

    def app_name(self, app_id: str) -> str | None:
        """Display name, or None when the app list does not have it (yet). A failed listing is
        not remembered: the next miss after ``NAMES_RELOAD_S`` lists again."""
        name = (self._names or {}).get(app_id)
        if name is None and self._clock() - self._names_loaded_at >= NAMES_RELOAD_S:
            self._names_loaded_at = self._clock()
            loaded = self._load_names()
            if loaded is not None:
                self._names = loaded
            name = (self._names or {}).get(app_id)
        return name

    @staticmethod
    def _load_names() -> dict[str, str] | None:
        sys.coinit_flags = 0  # type: ignore[attr-defined]  # MTA, shared with WinRT
        names: dict[str, str] = {}
        try:
            import comtypes.client  # noqa: PLC0415 - COM mode must be set first

            shell = comtypes.client.CreateObject("Shell.Application", dynamic=True)
            for item in shell.NameSpace("shell:AppsFolder").Items():
                names[str(item.Path)] = str(item.Name)
        except Exception as exc:  # noqa: BLE001 - names are a nicety; AUMIDs still work
            log.warning("could not list installed apps: %s", type(exc).__name__)
            return None
        return names
