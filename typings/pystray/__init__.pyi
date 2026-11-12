# Minimal stubs for the parts of pystray that akashic.tray uses (the package ships none).

from collections.abc import Callable
from typing import Any, Final

from PIL.Image import Image

class MenuItem:
    def __init__(
        self,
        text: str | Callable[[MenuItem], str],
        action: Callable[..., object] | None,
        *,
        default: bool = False,
        enabled: bool = True,
    ) -> None: ...

class Menu:
    SEPARATOR: Final[MenuItem]
    def __init__(self, *items: MenuItem) -> None: ...

class Icon:
    icon: Image
    title: str
    def __init__(self, name: str, icon: Image, title: str, menu: Menu) -> None: ...
    def run(self, setup: Callable[[Icon], Any] | None = None) -> None: ...
    def stop(self) -> None: ...
    def update_menu(self) -> None: ...
