"""Audio recording: two streams, voice detection, energy gate, source attribution, storage.

    microphone --+                              +--> writer: Opus + row + transcribe job
                 +--> recorder --> segmenter --+
    system loop -+   (VAD per 32 ms window)     +--> dropped: too short / too quiet / excluded

Only speech is kept. Segments are cut after ``hangover_ms`` of silence, or at ``max_segment_ms``
with ``overlap_ms`` of overlap into the next one so no word is split. Every segment carries the
wall-clock time its first sample was heard; nothing downstream may use processing time.

Paused means nothing is captured: devices are closed (the microphone indicator goes off) and any
half-recorded segment is dropped from memory. Stopping capture or a device change or failure keeps
the half-recorded segment by closing it early; an unexpected fault drops it. Process exclusions
apply to the processes that were making sound: a segment during which an excluded process was
audible is dropped, in memory.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import numpy as np

from akashic.capture.health import DEVICE_DOWN_CODES, DEVICE_DOWN_S
from akashic.capture.ports import AudioError
from akashic.capture.vad import WINDOW
from akashic.config import Audio
from akashic.storage import db
from akashic.storage.audio import NewSegment
from akashic.storage.opus import SAMPLE_RATE, encode_opus

if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import NDArray

    from akashic.capture.exclusions import Exclusions
    from akashic.capture.ports import (
        AudioDevice,
        AudioDevices,
        AudioInput,
        AudioKind,
        AudioSessions,
    )
    from akashic.capture.vad import SpeechDetector
    from akashic.storage.audio import AudioStore

log = logging.getLogger(__name__)

WINDOW_MS: Final = WINDOW * 1000 // SAMPLE_RATE  # 32
TAIL_MS: Final = 200  # silence kept after the last speech window
READ_TIMEOUT_S: Final = 0.1
RETRY_S: Final = 5.0  # between attempts to open a missing device
FAULT_LOG_S: Final = 300.0  # per exception type and stream, so a repeating fault cannot flood
DEVICE_LIST_S: Final = 10.0  # how often the list of selectable devices is refreshed
DEVICE_CHECK_S: Final = 5.0  # how often to ask whether the default device changed
REANCHOR_MS: Final = 100  # clock error beyond which window timestamps are re-anchored
SAMPLER_INTERVAL_S: Final = 1.0
SAMPLER_KEEP_MS: Final = 300_000
WRITER_QUEUE: Final = 64
WRITE_RETRY_S: Final = (0.5, 1.0, 2.0, 4.0, 8.0)  # waits after a busy database, per segment
# System sound made only by these is music, never worth transcribing. Browser music (YouTube
# Music, Deezer web) cannot be told apart from a video by sound, so it is kept.
MUSIC_PROCESSES: Final = frozenset(
    {
        "spotify.exe", "deezer.exe", "itunes.exe", "applemusic.exe", "tidal.exe",
        "foobar2000.exe", "musicbee.exe", "aimp.exe",
    }
)  # fmt: skip


VAD_CONTINUE_RATIO: Final = 0.7  # a segment stays open at a lower bar than the one that opens it
_DEFAULTS: Final = Audio()


@dataclass(frozen=True, slots=True)
class AudioSettings:
    vad_start: float = _DEFAULTS.vad_threshold  # speech probability that opens a segment
    vad_continue: float = _DEFAULTS.vad_threshold * VAD_CONTINUE_RATIO  # keeps it open
    # RMS of the speech windows below which a segment is noise
    energy_floor: float = _DEFAULTS.energy_floor
    hangover_ms: int = _DEFAULTS.silence_ms  # silence that ends a segment
    preroll_ms: int = 300  # audio kept from before the VAD fired
    min_speech_ms: int = _DEFAULTS.min_speech_ms
    max_segment_ms: int = 30_000  # Whisper's window
    overlap_ms: int = 2_000
    # microphone volume multiplier (the microphone stream only)
    mic_gain: float = _DEFAULTS.mic_gain
    mic_device: str = _DEFAULTS.mic_device  # name fragment; empty means the default device
    system_device: str = _DEFAULTS.system_device


@dataclass(frozen=True, slots=True)
class Utterance:
    start_ms: int
    end_ms: int
    samples: NDArray[np.float32]
    speech_ms: int
    continues: bool  # starts with the overlap of the previous utterance of this stream


@dataclass(frozen=True, slots=True)
class Drop:
    """A candidate segment the gates threw away, with the numbers that decided it."""

    ts_ms: int
    reason: str  # "short" or "quiet"
    rms: float  # of the speech windows
    speech_ms: int


DROPS_KEPT: Final = 20


class Segmenter:
    """Turns (timestamp, window, speech probability) into utterances. Pure: no clock, no IO."""

    def __init__(
        self, settings: AudioSettings, stats: Counter[str] | None = None, label: str = ""
    ) -> None:
        self._s = settings
        self._label = label
        self.drops: deque[Drop] = deque(maxlen=DROPS_KEPT)
        self._stats = stats if stats is not None else Counter[str]()
        self._preroll: deque[tuple[int, NDArray[np.float32]]] = deque(
            maxlen=max(1, settings.preroll_ms // WINDOW_MS)
        )
        self._reset_open()

    def _reset_open(self) -> None:
        self._open = False
        self._start_ms = 0
        self._windows: list[NDArray[np.float32]] = []
        self._speech: list[bool] = []
        self._seeded = 0  # windows copied from the previous utterance (overlap)
        self._continues = False
        self._last_speech_end_ms = 0

    def reset(self) -> None:
        """Drop everything in memory."""
        self._preroll.clear()
        self._reset_open()

    def finish(self) -> list[Utterance]:
        """Close an open utterance now, without waiting for the hangover."""
        return self._close() if self._open else []

    def feed(self, ts_ms: int, window: NDArray[np.float32], probability: float) -> list[Utterance]:
        done: list[Utterance] = []
        if self._open and ts_ms - self._last_speech_end_ms >= self._s.hangover_ms:
            done.extend(self._close())
        if not self._open:
            if probability >= self._s.vad_start:
                self._begin(ts_ms, window)
            else:
                self._preroll.append((ts_ms, window))
            return done
        self._windows.append(window)
        is_speech = probability >= self._s.vad_continue
        self._speech.append(is_speech)
        if is_speech:
            self._last_speech_end_ms = ts_ms + WINDOW_MS
        if len(self._windows) * WINDOW_MS >= self._s.max_segment_ms:
            done.extend(self._cut())
        return done

    def flush(self, now_ms: int) -> list[Utterance]:
        """Close an open utterance whose silence has lasted long enough (no audio is arriving)."""
        if self._open and now_ms - self._last_speech_end_ms >= self._s.hangover_ms:
            return self._close()
        return []

    def _begin(self, ts_ms: int, window: NDArray[np.float32]) -> None:
        earlier = [(t, w) for t, w in self._preroll if ts_ms - t <= self._s.preroll_ms]
        self._preroll.clear()
        self._open = True
        self._start_ms = earlier[0][0] if earlier else ts_ms
        self._windows = [w for _, w in earlier] + [window]
        self._speech = [False] * len(earlier) + [True]
        self._seeded = 0
        self._continues = False
        self._last_speech_end_ms = ts_ms + WINDOW_MS

    def _drop(self, reason: str, rms: float, speech_ms: int) -> None:
        self._stats[f"dropped_{reason}"] += 1
        self.drops.append(Drop(self._start_ms, reason, rms, speech_ms))
        # Numbers only: this is how the energy floor gets tuned from real speech levels.
        log.info(
            "dropped a %s %s segment: speech %d ms, level %.4f (floor %.4f)",
            self._label, reason, speech_ms, rms, self._s.energy_floor,
        )  # fmt: skip

    def _utterance(self, count: int) -> Utterance | None:
        """The first ``count`` windows as an utterance, or None when it gets gated out."""
        windows, speech = self._windows[:count], self._speech[:count]
        fresh = sum(speech[self._seeded :])
        voiced = [w for w, s in zip(windows, speech, strict=True) if s]
        rms = float(np.sqrt(np.mean(np.square(np.concatenate(voiced))))) if voiced else 0.0
        if fresh * WINDOW_MS < self._s.min_speech_ms:
            self._drop("short", rms, sum(speech) * WINDOW_MS)
            return None
        if rms < self._s.energy_floor:
            self._drop("quiet", rms, sum(speech) * WINDOW_MS)  # Whisper hallucinates on silence
            return None
        return Utterance(
            start_ms=self._start_ms,
            end_ms=self._start_ms + count * WINDOW_MS,
            samples=np.concatenate(windows),
            speech_ms=sum(speech) * WINDOW_MS,
            continues=self._continues,
        )

    def _close(self) -> list[Utterance]:
        last = max((i for i, s in enumerate(self._speech) if s), default=-1)
        count = min(len(self._windows), last + 1 + TAIL_MS // WINDOW_MS)
        utterance = self._utterance(count) if last >= 0 else None
        self._reset_open()
        return [utterance] if utterance else []

    def _cut(self) -> list[Utterance]:
        """The utterance reached the maximum length: emit it and start the next one with the
        last ``overlap_ms`` of audio, so a word on the boundary appears whole in one of them."""
        utterance = self._utterance(len(self._windows))
        keep = self._s.overlap_ms // WINDOW_MS
        tail_windows, tail_speech = self._windows[-keep:], self._speech[-keep:]
        self._start_ms = self._start_ms + (len(self._windows) - len(tail_windows)) * WINDOW_MS
        self._windows, self._speech = tail_windows, tail_speech
        self._seeded = len(tail_windows)
        self._continues = utterance is not None
        return [utterance] if utterance else []


class SourceSampler:
    """Polls which processes are making sound and remembers the last few minutes."""

    def __init__(
        self,
        sessions: AudioSessions,
        clock_ms: Callable[[], int],
        interval_s: float = SAMPLER_INTERVAL_S,
        keep_ms: int = SAMPLER_KEEP_MS,
    ) -> None:
        self._sessions = sessions
        self._clock_ms = clock_ms
        self._interval_s = interval_s
        self._keep_ms = keep_ms
        # None marks a poll that failed: nobody knows what was audible then.
        self._samples: deque[tuple[int, frozenset[str] | None]] = deque()
        self._lock = threading.Lock()

    def sample(self) -> None:
        now = self._clock_ms()
        names: frozenset[str] | None
        try:
            names = self._sessions.active_processes()
        except Exception as exc:  # noqa: BLE001 - a failed poll is an unknown, never "silence"
            log.warning("audio source sampling failed: %s", type(exc).__name__)
            names = None
        with self._lock:
            self._samples.append((now, names))
            while self._samples and self._samples[0][0] < now - self._keep_ms:
                self._samples.popleft()

    def between(self, start_ms: int, end_ms: int) -> tuple[str, ...]:
        """Processes heard during the span, give or take one polling interval."""
        return self.check(start_ms, end_ms)[0]

    def check(self, start_ms: int, end_ms: int) -> tuple[tuple[str, ...], bool]:
        """Processes heard during the span and whether that is the whole story. The span is
        not ``complete`` when a poll inside it failed or when no poll succeeded inside it
        (the sampler was stalled or the samples are gone): what was audible is unknown."""
        slack = int(self._interval_s * 1000)
        found: set[str] = set()
        succeeded = failed = False
        with self._lock:
            for ts, names in self._samples:
                if not start_ms - slack <= ts <= end_ms + slack:
                    continue
                if names is None:
                    failed = True
                else:
                    succeeded = True
                    found.update(names)
        return tuple(sorted(found)), succeeded and not failed

    def run(self, stop: threading.Event, paused: threading.Event) -> None:
        while not stop.is_set():
            if not paused.is_set():
                try:
                    self.sample()
                except Exception as exc:  # noqa: BLE001 - attribution is best effort
                    log.warning("audio source sampling failed: %s", type(exc).__name__)
            stop.wait(self._interval_s)


class SegmentWriter:
    """Encodes and stores utterances off the recording threads, so a slow disk or encoder can
    never make a device buffer overflow."""

    def __init__(
        self,
        store: AudioStore,
        sampler: SourceSampler,
        exclusions: Exclusions,
        clock_ms: Callable[[], int],
        stats: Counter[str],
    ) -> None:
        self._store = store
        self._sampler = sampler
        self._exclusions = exclusions
        self._clock_ms = clock_ms
        self._stats = stats
        self._queue: queue.Queue[tuple[AudioKind, Utterance]] = queue.Queue(WRITER_QUEUE)
        self._last_id: dict[str, int | None] = {}
        self._stop = threading.Event()

    def submit(self, kind: AudioKind, utterance: Utterance) -> None:
        try:
            self._queue.put_nowait((kind, utterance))
        except queue.Full:
            self._stats["dropped_backlog"] += 1
            log.warning("audio writer is behind; dropped a %s segment", kind)

    def run(self, stop: threading.Event) -> None:
        """Write until told to stop, then finish what is already queued."""
        self._stop = stop
        while True:
            try:
                kind, utterance = self._queue.get(timeout=0.2)
            except queue.Empty:
                if stop.is_set():
                    return
                continue
            try:
                self.write(kind, utterance)
            except Exception as exc:  # noqa: BLE001 - one bad segment must not stop recording
                self._stats["write_failed"] += 1
                self._last_id[kind] = None
                log.warning("storing a %s segment failed: %s", kind, type(exc).__name__)

    def write(self, kind: AudioKind, utterance: Utterance) -> int | None:
        sources, complete = self._sampler.check(utterance.start_ms, utterance.end_ms)
        if any(self._exclusions.check_process(name) for name in sources):
            self._stats["dropped_excluded"] += 1
            self._last_id[kind] = None
            return None
        if not complete:  # unknown sources: nothing unclear is kept, the RAM copy is dropped
            self._stats["dropped_unknown_sources"] += 1
            self._last_id[kind] = None
            return None
        if kind == "system" and sources and set(sources) <= MUSIC_PROCESSES:
            self._stats["dropped_music"] += 1  # song lyrics: the media session logs the song
            self._last_id[kind] = None
            return None
        segment = NewSegment(
            stream=kind,
            start_ms=utterance.start_ms,
            end_ms=utterance.end_ms,
            speech_ms=utterance.speech_ms,
            sources=sources,
            continues_id=self._last_id.get(kind) if utterance.continues else None,
        )
        segment_id = self._add_with_retry(segment, encode_opus(utterance.samples))
        self._last_id[kind] = segment_id
        self._stats[f"segments_{kind}"] += 1
        return segment_id

    def _add_with_retry(self, segment: NewSegment, opus: bytes) -> int:
        """Store the segment, waiting out a busy database. Once stopping, the current attempt is
        the last one."""
        for delay in WRITE_RETRY_S:
            try:
                return self._store.add(segment, opus, self._clock_ms())
            except Exception as exc:
                if not db.is_busy(exc):
                    raise
                if self._stop.wait(delay):
                    raise
                self._stats["write_retried"] += 1
        return self._store.add(segment, opus, self._clock_ms())


class StreamRecorder:
    """Reads one device, finds speech, hands utterances to ``sink``. One thread per stream."""

    def __init__(  # noqa: PLR0913 - wiring: each argument is a distinct collaborator
        self,
        *,
        kind: AudioKind,
        devices: AudioDevices,
        detector: SpeechDetector,
        settings: AudioSettings,
        sink: Callable[[AudioKind, Utterance], None],
        clock_ms: Callable[[], int],
        stats: Counter[str],
    ) -> None:
        self.kind: AudioKind = kind
        self._devices = devices
        self._detector = detector
        self._settings = settings
        self._wanted = settings.mic_device if kind == "mic" else settings.system_device
        self._sink = sink
        self._clock_ms = clock_ms
        self._stats = stats
        self.segmenter = Segmenter(settings, stats, kind)
        self._input: AudioInput | None = None
        self._device: AudioDevice | None = None
        self._pending = np.zeros(0, dtype=np.float32)
        self._pending_start_ms = 0
        self._next_device_check = 0.0
        self._warned = False
        self._fault_logged: dict[str, float] = {}  # exception type -> monotonic time last logged
        self._level_max = 0.0  # loudest 32 ms window since the last snapshot()
        self._speech_sq = 0.0  # mean square of the windows the detector called speech
        self._speech_windows = 0
        self._probability_max = 0.0
        self._ok_at = float("-inf")  # monotonic time of the last completed device read

    def down_s(self, now: float, since: float) -> float:
        """Seconds without a successful read, counted from the later of that and ``since``."""
        return now - max(self._ok_at, since)

    def snapshot(self) -> dict[str, object]:
        """Plain numbers for the Debug page; resets the since-last-time maxima."""
        out: dict[str, object] = {
            "device": self._device.name if self._device else None,
            "level_max": round(self._level_max, 4),
            "speech_probability_max": round(self._probability_max, 2),
            # The number the energy floor is compared with: level of what was heard as speech.
            "speech_level": (
                round((self._speech_sq / self._speech_windows) ** 0.5, 4)
                if self._speech_windows
                else None
            ),
            "drops": [
                {
                    "ts_ms": d.ts_ms,
                    "reason": d.reason,
                    "level": round(d.rms, 4),
                    "speech_ms": d.speech_ms,
                }
                for d in list(self.segmenter.drops)
            ],
        }
        self._level_max = 0.0
        self._probability_max = 0.0
        self._speech_sq = 0.0
        self._speech_windows = 0
        return out

    def run(self, stop: threading.Event, paused: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self._iterate(stop, paused)
                except Exception as exc:  # noqa: BLE001 - a recorder must outlive any one fault
                    self._recover_from_fault(type(exc).__name__, stop)
        finally:
            try:
                self._release(keep=not paused.is_set())
            except Exception as exc:  # noqa: BLE001 - shutdown must not print the message
                self._log_fault(type(exc).__name__)

    def _iterate(self, stop: threading.Event, paused: threading.Event) -> None:
        if paused.is_set():
            self._release(keep=False)
            stop.wait(0.2)
        elif self._input is None:
            if not self._acquire():
                stop.wait(RETRY_S)
        else:
            self._step()

    def _recover_from_fault(self, name: str, stop: threading.Event) -> None:
        self._stats[f"loop_error_{self.kind}"] += 1
        self._log_fault(name)
        with contextlib.suppress(Exception):  # _release clears its state even if close() raises
            self._release(keep=False)
        stop.wait(RETRY_S)

    def _log_fault(self, name: str) -> None:
        now = time.monotonic()
        if now - self._fault_logged.get(name, float("-inf")) >= FAULT_LOG_S:
            self._fault_logged[name] = now
            log.warning("%s audio loop iteration failed: %s", self.kind, name)

    def _acquire(self) -> bool:
        device = self._devices.find(self.kind, self._wanted)
        if device is None:
            self._warn_once(f"no {self.kind} audio device found")
            return False
        try:
            self._input = self._devices.open(self.kind, device)
        except AudioError as exc:
            self._warn_once(f"cannot open the {self.kind} audio device: {exc}")
            return False
        self._device = device
        self._warned = False
        self._pending = np.zeros(0, dtype=np.float32)
        self._detector.reset()
        self.segmenter.reset()
        self._next_device_check = time.monotonic() + DEVICE_CHECK_S
        log.info("recording %s from %r", self.kind, device.name)
        return True

    def _warn_once(self, message: str) -> None:
        if not self._warned:
            log.warning(message)
            self._warned = True

    def _release(self, *, keep: bool) -> None:
        """Close the device. The open utterance is handed to the sink when ``keep`` is set (stop,
        device change, device failure) and dropped otherwise (pause, fault)."""
        try:
            if keep:
                for utterance in self.segmenter.finish():
                    self._sink(self.kind, utterance)
        finally:
            try:
                if self._input is not None:
                    self._input.close()
            finally:
                self._input = None
                self._device = None
                self._pending = np.zeros(0, dtype=np.float32)
                self.segmenter.reset()

    def _step(self) -> None:
        assert self._input is not None  # noqa: S101 - narrowing for the type checker
        try:
            block = self._input.read(READ_TIMEOUT_S)
        except AudioError as exc:
            self._warn_once(f"{self.kind} audio device failed: {exc}")
            self._release(keep=True)
            return
        now = self._clock_ms()
        if block is None:
            for utterance in self.segmenter.flush(now):
                self._sink(self.kind, utterance)
        else:
            self._consume(block, now)
        self._ok_at = time.monotonic()
        if time.monotonic() >= self._next_device_check:
            self._next_device_check = time.monotonic() + DEVICE_CHECK_S
            self._follow_default_device()

    def _follow_default_device(self) -> None:
        current = self._devices.find(self.kind, self._wanted)
        if current is not None and self._device is not None and current != self._device:
            log.info("%s audio device changed; reopening", self.kind)
            self._release(keep=True)

    def _consume(self, block: NDArray[np.float32], now_ms: int) -> None:
        """Cut ``block`` into VAD windows stamped with the time they were heard."""
        if self.kind == "mic" and self._settings.mic_gain != 1.0:
            block = np.clip(block * np.float32(self._settings.mic_gain), -1.0, 1.0)
        total = len(self._pending) + len(block)
        expected_start = now_ms - total * 1000 // SAMPLE_RATE
        if len(self._pending) == 0 or abs(expected_start - self._pending_start_ms) > REANCHOR_MS:
            self._pending_start_ms = expected_start  # first block, a gap, or clock drift
        self._pending = np.concatenate([self._pending, block]) if len(self._pending) else block
        cursor = 0
        while len(self._pending) - cursor >= WINDOW:
            window = self._pending[cursor : cursor + WINDOW]
            ts = self._pending_start_ms + cursor * 1000 // SAMPLE_RATE
            cursor += WINDOW
            probability = self._detector.probability(window) if window.any() else 0.0
            square = float(np.mean(np.square(window)))
            self._level_max = max(self._level_max, square**0.5)
            if probability >= self._settings.vad_start:
                self._speech_sq += square
                self._speech_windows += 1
            self._probability_max = max(self._probability_max, probability)
            for utterance in self.segmenter.feed(ts, window, probability):
                self._sink(self.kind, utterance)
        self._pending = self._pending[cursor:].copy()
        self._pending_start_ms += cursor * 1000 // SAMPLE_RATE


class AudioCapture:
    """Owns the recorder, sampler and writer threads for both streams."""

    def __init__(  # noqa: PLR0913 - wiring: each argument is a distinct collaborator
        self,
        *,
        devices: AudioDevices,
        sessions: AudioSessions,
        detector_factory: Callable[[], SpeechDetector],
        store: AudioStore,
        exclusions: Exclusions,
        settings: AudioSettings,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self.stats: Counter[str] = Counter()
        sampler = SourceSampler(sessions, clock_ms)
        self._sampler = sampler
        self._writer = SegmentWriter(store, sampler, exclusions, clock_ms, self.stats)
        self._energy_floor = settings.energy_floor
        self._devices = devices
        self._names: dict[str, list[str]] = {}
        self._names_at = float("-inf")
        self._recorders = [
            StreamRecorder(
                kind=kind,
                devices=devices,
                detector=detector_factory(),
                settings=settings,
                sink=self._writer.submit,
                clock_ms=clock_ms,
                stats=self.stats,
            )
            for kind in ("mic", "system")
        ]

    def _available(self) -> dict[str, list[str]]:
        """Names of the devices that can be picked in Settings (looked up every 10 s at most)."""
        if time.monotonic() - self._names_at >= DEVICE_LIST_S:
            self._names_at = time.monotonic()
            self._names = {kind: self._devices.names(kind) for kind in ("mic", "system")}
        return self._names

    def problems(self, now: float, since: float) -> tuple[str, ...]:
        """The down code of each recorder that has read nothing for too long."""
        return tuple(
            DEVICE_DOWN_CODES[r.kind]
            for r in self._recorders
            if r.down_s(now, since) >= DEVICE_DOWN_S
        )

    def snapshot(self) -> dict[str, object]:
        """Counters, device names, levels and recent drops, for the Debug page."""
        return {
            "stats": dict(sorted(self.stats.items())),
            "energy_floor": self._energy_floor,
            "available": self._available(),
            "streams": {r.kind: r.snapshot() for r in self._recorders},
        }

    def run(self, stop: threading.Event, paused: threading.Event) -> None:
        """Record until ``stop`` is set, then finish writing what is queued."""
        writer_stop = threading.Event()
        threads = [
            threading.Thread(target=self._sampler.run, args=(stop, paused), name="audio-sources"),
            *(
                threading.Thread(target=r.run, args=(stop, paused), name=f"audio-{r.kind}")
                for r in self._recorders
            ),
        ]
        writer = threading.Thread(target=self._writer.run, args=(writer_stop,), name="audio-writer")
        writer.start()
        for t in threads:
            t.start()
        try:
            stop.wait()
        finally:
            for t in threads:
                t.join(timeout=10)
            writer_stop.set()
            writer.join(timeout=30)
