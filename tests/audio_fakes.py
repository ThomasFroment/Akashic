"""Fakes for the audio recorder: scripted devices, a content-based voice detector, a clock."""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import TYPE_CHECKING

import numpy as np

from akashic.capture.audio import AudioSettings, StreamRecorder, Utterance
from akashic.capture.ports import AudioDevice, AudioError
from akashic.capture.vad import WINDOW

if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import NDArray

    from akashic.capture.ports import AudioInput, AudioKind

RATE = 16_000
WINDOW_MS = WINDOW * 1000 // RATE
SETTINGS = AudioSettings(preroll_ms=96, hangover_ms=320, min_speech_ms=128)
T0 = 1_700_000_000_000
MIC = AudioDevice("mic-1", "Fake Mic")
SYSTEM = AudioDevice("spk-1", "Fake Speakers")
DEVICES = {"mic": MIC, "system": SYSTEM}


def speech(windows: int, amplitude: float = 0.3) -> NDArray[np.float32]:
    """A loud constant signal: the fake detector calls it speech."""
    return np.full(windows * WINDOW, amplitude, dtype=np.float32)


def silence(windows: int) -> NDArray[np.float32]:
    return np.zeros(windows * WINDOW, dtype=np.float32)


class FakeDetector:
    """Speech where the signal is loud, silence where it isn't. Counts resets."""

    def __init__(self) -> None:
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def probability(self, window: NDArray[np.float32]) -> float:
        return 0.9 if float(np.abs(window).max()) > 0.05 else 0.0


class FakeInput:
    """Plays a script: arrays are blocks, None is "nothing arrived", an exception is raised.

    ``realtime`` makes a block take as long to read as it lasts, like a real device."""

    def __init__(
        self, script: list[NDArray[np.float32] | AudioError | None], *, realtime: bool = False
    ) -> None:
        self.script = script
        self.realtime = realtime
        self.closed = False

    def read(self, timeout_s: float) -> NDArray[np.float32] | None:
        if not self.script:
            time.sleep(min(timeout_s, 0.01))
            return None
        item = self.script.pop(0)
        if isinstance(item, AudioError):
            raise item
        if item is not None and self.realtime:
            time.sleep(len(item) / RATE)
        return item

    def close(self) -> None:
        self.closed = True


class FakeDevices:
    def __init__(self) -> None:
        self.devices: dict[str, AudioDevice | None] = {
            "mic": MIC,
            "system": SYSTEM,
        }
        self.scripts: dict[str, list[list[NDArray[np.float32] | AudioError | None]]] = {
            "mic": [],
            "system": [],
        }
        self.opened: list[tuple[str, FakeInput]] = []
        self.realtime = False
        self.open_error: AudioError | None = None
        self.lock = threading.Lock()

    def find(self, kind: AudioKind, name_contains: str) -> AudioDevice | None:
        del name_contains
        return self.devices[kind]

    def names(self, kind: AudioKind) -> list[str]:
        device = self.devices[kind]
        return [device.name] if device else []

    def open(self, kind: AudioKind, device: AudioDevice) -> AudioInput:
        del device
        with self.lock:
            if self.open_error is not None:
                raise self.open_error
            script = self.scripts[kind].pop(0) if self.scripts[kind] else []
            stream = FakeInput(script, realtime=self.realtime)
            self.opened.append((kind, stream))
            return stream


class FakeSessions:
    """``names`` None is a failed enumeration (unknown), not silence; ``error`` is raised.

    A bare frozenset is what the fake system device ("spk-1") is playing."""

    def __init__(
        self,
        names: frozenset[str] | dict[str, frozenset[str]] | None = frozenset(),
        error: Exception | None = None,
    ) -> None:
        self.names = names
        self.error = error
        self.refreshes: list[bool] = []

    def active_processes(self, *, refresh: bool = False) -> dict[str, frozenset[str]] | None:
        self.refreshes.append(refresh)
        if self.error is not None:
            raise self.error
        if self.names is None or isinstance(self.names, dict):
            return self.names
        return {SYSTEM.device_id: self.names}


class Clock:
    def __init__(self, now_ms: int = 1_000_000) -> None:
        self.now = now_ms

    def __call__(self) -> int:
        return self.now


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class RecorderRig:
    """``realtime`` rigs run threads against a device that delivers audio at its own speed and
    the wall clock; the others are driven by hand with a fake clock."""

    def __init__(
        self,
        kind: AudioKind = "mic",
        settings: AudioSettings = SETTINGS,
        *,
        realtime: bool = False,
        on_device: Callable[[str | None], None] = lambda _id: None,
    ) -> None:
        self.devices = FakeDevices()
        self.devices.realtime = realtime
        self.detector = FakeDetector()
        self.clock = Clock(T0)
        self.out: list[tuple[str, Utterance]] = []
        self.out_devices: list[AudioDevice] = []
        self.stats: Counter[str] = Counter()
        self.recorder = StreamRecorder(
            kind=kind,
            devices=self.devices,
            detector=self.detector,
            settings=settings,
            sink=self._sink,
            clock_ms=wall_clock_ms if realtime else self.clock,
            stats=self.stats,
            on_device=on_device,
        )

    def _sink(self, kind: AudioKind, utterance: Utterance, device: AudioDevice) -> None:
        self.out.append((kind, utterance))
        self.out_devices.append(device)

    def deliver(self, samples: np.ndarray, block_windows: int = 4) -> None:
        step = block_windows * 512
        for i in range(0, len(samples), step):
            block = samples[i : i + step]
            self.clock.now += len(block) * 1000 // RATE
            self.recorder._consume(block, self.clock.now, DEVICES[self.recorder.kind])
