"""Environment flags that keep bundled ML libraries from reaching the network.

Applied by every process at startup and passed to children. Runtime HTTP clients and servers
use loopback-only configuration; development tests may access public network resources.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import MutableMapping

OFFLINE_ENV: Final = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "DO_NOT_TRACK": "1",
}


def apply_offline_env(env: MutableMapping[str, str]) -> None:
    env.update(OFFLINE_ENV)
