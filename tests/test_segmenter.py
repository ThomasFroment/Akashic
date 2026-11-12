"""Segmenter: speech runs become utterances; short, quiet and over-long ones are handled."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

from audio_fakes import WINDOW_MS, silence, speech

from akashic.capture.audio import AudioSettings, Segmenter, Utterance
from akashic.capture.vad import WINDOW

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

SETTINGS = AudioSettings(
    preroll_ms=96,  # 3 windows
    hangover_ms=320,  # 10 windows
    min_speech_ms=128,  # 4 windows
    max_segment_ms=3_200,  # 100 windows
    overlap_ms=640,  # 20 windows
)


class Rig:
    """Feeds the segmenter whole runs of windows, keeping a running timestamp."""

    def __init__(self, settings: AudioSettings = SETTINGS) -> None:
        self.stats: Counter[str] = Counter()
        self.seg = Segmenter(settings, self.stats)
        self.ts = 0
        self.out: list[Utterance] = []

    def feed(self, samples: NDArray[np.float32], probability: float | None = None) -> None:
        for i in range(0, len(samples), WINDOW):
            window = samples[i : i + WINDOW]
            p = probability if probability is not None else (0.9 if window.max() > 0.05 else 0.0)
            self.out.extend(self.seg.feed(self.ts, window, p))
            self.ts += WINDOW_MS


def test_speech_between_silences_is_one_utterance_with_preroll_and_tail() -> None:
    rig = Rig()
    rig.feed(silence(5))
    rig.feed(speech(10))
    rig.feed(silence(15))
    (utterance,) = rig.out
    assert utterance.start_ms == 2 * WINDOW_MS  # 3 windows of pre-roll before speech at window 5
    assert utterance.speech_ms == 10 * WINDOW_MS
    assert len(utterance.samples) == (3 + 10 + 6) * WINDOW  # pre-roll, speech, 200 ms tail
    assert utterance.end_ms == utterance.start_ms + 19 * WINDOW_MS
    assert not utterance.continues


def test_utterance_waits_for_the_hangover() -> None:
    rig = Rig()
    rig.feed(speech(10))
    rig.feed(silence(9))
    assert rig.out == []  # not yet 320 ms of silence
    rig.feed(silence(2))
    assert len(rig.out) == 1


def test_speech_resuming_within_the_hangover_stays_one_utterance() -> None:
    rig = Rig()
    rig.feed(speech(6))
    rig.feed(silence(5))
    rig.feed(speech(6))
    rig.feed(silence(15))
    assert len(rig.out) == 1
    assert rig.out[0].speech_ms == 12 * WINDOW_MS


def test_too_short_speech_is_dropped() -> None:
    rig = Rig()
    rig.feed(speech(2))
    rig.feed(silence(15))
    assert rig.out == []
    assert rig.stats["dropped_short"] == 1


def test_quiet_speech_is_dropped_by_the_energy_gate() -> None:
    rig = Rig()
    rig.feed(speech(10, amplitude=0.01), probability=0.9)  # the detector fires, the level is low
    rig.feed(silence(15), probability=0.0)
    assert rig.out == []
    assert rig.stats["dropped_quiet"] == 1


def test_start_threshold_is_higher_than_the_continue_threshold() -> None:
    rig = Rig()
    rig.feed(speech(10), probability=0.4)  # between the thresholds: never starts
    rig.feed(silence(15), probability=0.0)
    assert rig.out == []
    rig.feed(speech(1), probability=0.9)  # starts
    rig.feed(speech(10), probability=0.4)  # continues
    rig.feed(silence(15), probability=0.0)
    assert len(rig.out) == 1
    assert rig.out[0].speech_ms == 11 * WINDOW_MS


def test_a_gap_in_the_timeline_closes_the_utterance() -> None:
    rig = Rig()
    rig.feed(speech(10))
    rig.ts += 5_000  # the device delivered nothing for 5 s
    rig.feed(silence(1))
    assert len(rig.out) == 1


def test_flush_closes_after_the_hangover_when_no_audio_arrives() -> None:
    rig = Rig()
    rig.feed(speech(10))
    end = rig.ts
    assert rig.seg.flush(end + 100) == []
    (utterance,) = rig.seg.flush(end + 400)
    assert utterance.speech_ms == 10 * WINDOW_MS
    assert rig.seg.flush(end + 10_000) == []  # nothing open any more


def test_reset_drops_the_open_utterance() -> None:
    rig = Rig()
    rig.feed(speech(10))
    rig.seg.reset()
    rig.feed(silence(20))
    assert rig.out == []


def test_long_speech_is_cut_with_overlap_and_the_next_one_continues() -> None:
    rig = Rig()
    rig.feed(speech(120))  # 3.84 s; maximum is 3.2 s
    rig.feed(silence(15))
    first, second = rig.out
    assert not first.continues
    assert len(first.samples) == 100 * WINDOW
    assert second.continues
    assert second.start_ms == first.start_ms + 80 * WINDOW_MS  # starts 640 ms before the cut
    assert second.start_ms < first.end_ms  # overlap
    assert first.end_ms - second.start_ms == 20 * WINDOW_MS


def test_a_continuation_with_no_new_speech_is_not_emitted() -> None:
    rig = Rig()
    rig.feed(speech(100))  # cut exactly at the maximum
    rig.feed(silence(15))
    (only,) = rig.out
    assert not only.continues
    assert rig.stats["dropped_short"] == 1  # the overlap alone adds nothing


def test_a_dropped_first_part_does_not_make_the_next_a_continuation() -> None:
    rig = Rig(replace(SETTINGS, energy_floor=0.9))  # everything counts as quiet
    rig.feed(speech(120))
    rig.feed(silence(15))
    assert rig.out == []
    assert rig.stats["dropped_quiet"] >= 1
