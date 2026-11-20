"""P1 = C1 per-app streams, the call as the residual.

Capture side (simulated from the harness player logs, ctx.process_stream):
- Stem picking (A3 per-session meter, polled per METER_MS, sticky for STICKY_MS): up to MAX_STEMS
  audible apps that are not voice apps get their own process-loopback include client. Excluded
  apps are picked first (they must be subtracted), then apps already stemmed, then the loudest
  non-music apps, then music. A newly picked app's first OPEN_MS stay in the residual.
- Residual: endpoint loopback minus each stem through a FIR_TAPS-tap FIR (lstsq, joint over the
  stems, refitted per stem every REFIT_MS on FIT_MS where the meters show only stemmed apps audible, held
  otherwise; applied by FFT convolution). The simulated streams are not sample-exact against the
  stored audio, so the residual is modelled: the sim mix minus the fitted stems (= the voice apps
  and the unstemmed apps exactly) plus a leak of each stem at the per-block level measured on the
  real DT-770 endpoint (spike.json, 128-tap held FIR at 16 kHz), as an intelligible copy (worst
  case for ASR).
- Validity switch: per BLOCK_MS block where only stemmed apps sound, the residual-to-mix depth
  (from the same spike's per-block depths) above VALID_DB switches back to today's mix for at
  least HOLD_MS (stems muted meanwhile so no speech is transcribed twice).
- Streams: every stem except music and excluded apps, and the residual, run Akashic's own
  Segmenter + Silero VAD and are transcribed separately (decode_segment, prompt context per
  stream); rows carry their real start time. The excluded app's stem is dropped whole (never
  stored); a residual segment is dropped while an excluded app is audible (its leak could sit in
  it) and when no full-level source is audible in it (it would hold only stem leaks). Residual
  segments made only of music are dropped as today. Speakers job per row (production store).
- Mic: today's pipeline on the stored mic segments.
"""

from __future__ import annotations

import bisect
import json
import time
from pathlib import Path

import numpy as np
from scipy.signal import oaconvolve
from threadpoolctl import threadpool_limits  # present in the env (scikit-learn dep); capture runs the fit on one thread

import core
from akashic.capture.audio import MUSIC_PROCESSES, AudioSettings, Segmenter
from akashic.capture.ports import VOICE_APPS
from akashic.capture.vad import WINDOW, SileroVad
from akashic.pipeline.transcribe import PREVIOUS_WITHIN_MS

HERE = Path(__file__).resolve().parent
SR = core.SR
MAX_STEMS = 3
METER_MS = 1000
STICKY_MS = 30_000
OPEN_MS = 100
AUDIBLE_PEAK = 0.001  # platform/win/audio._AUDIBLE_PEAK
BLOCK_MS = 500
VALID_DB = -25.0
HOLD_MS = 10_000
CHUNK_MS = 60_000
FIR_TAPS, FIR_CEN = 128, 16
REFIT_MS, FIT_MS = 60_000, 1_000
DELTA = np.eye(FIR_TAPS)[FIR_CEN]
OFF_REASONS = {"paused", "stopped", "crashed", "stalled", "locked", "audio_unavailable", "disabled", "device_missing", "device_failed", "ui_open"}
RESIDUAL = "residual"


def _spike() -> tuple[np.ndarray, np.ndarray, str]:
    p = HERE / "spike.json"
    d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    r = d.get("dt770", {}).get("16000", {}).get(f"fir{FIR_TAPS}", {})
    leak = r.get("leak_held_fir_db", {}).get("per_block")
    depth = r.get("depth_alone_db", {}).get("per_block")
    if leak and depth:
        return np.array(leak, float), np.array(depth, float), "dt770-16k-fir128"
    return np.array([-40.0]), np.array([-40.0]), "default-40"


LEAK_DB, DEPTH_DB, LEAK_SRC = _spike()


def previous_transcript(done: list[tuple[int, int, str]], before_ms: int) -> str | None:
    best = None
    for end, start, text in done:
        if before_ms - PREVIOUS_WITHIN_MS <= end <= before_ms and start < before_ms and (best is None or end > best[0]):
            best = (end, text)
    return best[1] if best else None


def capture_windows(segments, source) -> list[tuple[int, int]]:
    """When Akashic was recording: the span of the given segments minus capture-off gaps."""
    a = min(s.start_ms for s in segments) - 5_000
    b = max(s.end_ms for s in segments) + 5_000
    if hasattr(source, "mixes"):  # synthetic set: one window per mixture slot
        return [(m.t0, m.t0 + m.length_ms + 2_000) for m in source.mixes if a <= m.t0 + 1_000 and m.t0 < b]
    off = []
    conn = getattr(source, "conn", None)
    if conn is not None:
        for kind, reason, g0, g1 in conn.execute("SELECT kind, reason, start_ms, end_ms FROM capture_gaps"):
            if kind in ("all", "system") and reason in OFF_REASONS:
                off.append((g0, g1 if g1 is not None else b))
    wins = [(a, b)]
    for g0, g1 in sorted(off):
        nxt = []
        for w0, w1 in wins:
            if g1 <= w0 or g0 >= w1:
                nxt.append((w0, w1))
                continue
            if g0 > w0:
                nxt.append((w0, g0))
            if g1 < w1:
                nxt.append((g1, w1))
        wins = nxt
    return [(w0, w1) for w0, w1 in wins if w1 - w0 >= 1_000]


class Stream:
    def __init__(self, key: str) -> None:
        self.key = key
        self.vad = SileroVad()
        self.vad.reset()
        self.seg = Segmenter(AudioSettings())

    def feed(self, x: np.ndarray, t0: int) -> list:
        out = []
        for i in range(0, len(x) - WINDOW + 1, WINDOW):
            w = x[i:i + WINDOW]
            p = self.vad.probability(w) if w.any() else 0.0
            out += self.seg.feed(t0 + i * 1000 // SR, w, p)
        return out

    def finish(self) -> list:
        out = self.seg.finish()
        self.vad.reset()
        self.seg.reset()
        return out


class P1:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.out: list[core.Transcript] = []
        self.done: dict[str, list] = {}
        self.last_text: dict[str, str | None] = {}
        self.blocks: dict[int, tuple[frozenset, frozenset, bool]] = {}  # meter block start -> (audible, stems, fallback)
        self.bkeys: list[int] = []  # block starts, increasing (chunks are fed in time order)
        self.h: dict[str, np.ndarray] = {}
        self.stems_prev: frozenset = frozenset()
        self.last_audible: dict[str, int] = {}
        self.fallback_until = -1
        self.block_i = 0
        self.stats = {"n_decode": 0, "decode_audio_s": {}, "rows": {}, "dropped": {}, "fallback_blocks": 0, "valid_blocks": 0, "fir_fits": 0,
                      "cpu_fir_s": 0.0, "cpu_residual_seg_s": 0.0, "cpu_stem_seg_s": 0.0, "stem_count_s": {0: 0, 1: 0, 2: 0, 3: 0}, "unstemmed_audible_s": 0,
                      "leak_source": LEAK_SRC, "leak_db_rms_model": float(10 * np.log10(np.mean(10 ** (LEAK_DB / 10))))}
        ctx.p1 = self.stats

    # --- transcription ---------------------------------------------------------------------
    def emit(self, key: str, stream: str, a: int, b: int, samples: np.ndarray, speech_ms: int, sources: tuple, continues: bool) -> None:
        done = self.done.setdefault(key, [])
        prev = previous_transcript(done[-50:], a)
        earlier = self.last_text.get(key) if continues else None
        res = self.ctx.decode(samples, prev, earlier)
        self.stats["n_decode"] += 1
        kind = "mic" if stream == "mic" else ("residual" if key == RESIDUAL else "stem")
        self.stats["decode_audio_s"][kind] = self.stats["decode_audio_s"].get(kind, 0) + len(samples) / SR
        self.stats["rows"][kind] = self.stats["rows"].get(kind, 0) + 1
        self.last_text[key] = res.text if res else None
        t = core.Transcript(stream, a, b, res.text if res else "", res.language if res else None, tuple(sources), speech_ms=speech_ms)
        t.p1_kind = kind  # read by p1_run.py (which rows can hold an excluded app's sound)
        if res:
            done.append((b, a, res.text))
            if stream == "mic":
                me = self.ctx.self_id()
                t.speaker_id, t.turns = me, [(me, 0, b - a)]
            else:
                t.speaker_id, t.turns = self.ctx.identify(samples)
        self.out.append(t)

    def drop(self, key: str, why: str) -> None:
        self.last_text[key] = None
        self.stats["dropped"][why] = self.stats["dropped"].get(why, 0) + 1

    def span_info(self, a: int, b: int):
        # meter blocks start at chunk starts + k * METER_MS (not aligned to METER_MS multiples):
        # every block that overlaps [a, b)
        aud, full, excl = set(), set(), False
        i = max(0, bisect.bisect_right(self.bkeys, a) - 1)
        while i < len(self.bkeys) and self.bkeys[i] < b:
            t = self.bkeys[i]
            i += 1
            if t + METER_MS <= a:
                continue
            audible, stems, fb = self.blocks[t]
            aud |= audible
            full |= {e for e in audible if fb or e in VOICE_APPS or e not in stems}
            excl |= bool(audible & core.EXCLUDED)
        return aud, full, excl

    def system_utterance(self, key: str, u) -> None:
        if key == RESIDUAL:
            _, full, excl = self.span_info(u.start_ms, u.end_ms)
            if excl:
                return self.drop(key, "residual_excluded_audible")
            sources = tuple(sorted(full - core.EXCLUDED))
            if not sources:
                return self.drop(key, "residual_leak_only")
            if set(sources) <= MUSIC_PROCESSES:
                return self.drop(key, "residual_music")
        else:
            sources = (key,)
        self.emit(key, "system", u.start_ms, u.end_ms, u.samples, u.speech_ms, sources, u.continues)

    # --- capture simulation ----------------------------------------------------------------
    def pick(self, t: int, audible: dict[str, float]) -> frozenset:
        for e, lv in audible.items():
            if lv >= AUDIBLE_PEAK:
                self.last_audible[e] = t
        recent = [e for e, s in self.last_audible.items() if t - s <= STICKY_MS and e not in VOICE_APPS]
        def rank(e):
            return (0 if e in core.EXCLUDED else 1, 0 if e in self.stems_prev else 1, 1 if e in MUSIC_PROCESSES else 0, -audible.get(e, 0.0))
        return frozenset(sorted(recent, key=rank)[:MAX_STEMS])

    def chunk(self, t0: int, t1: int, streams: dict[str, Stream]) -> list[tuple[str, object]]:
        ctx = self.ctx
        n = (t1 - t0) * SR // 1000
        exes = ctx.processes(t0, t1)
        x = {e: ctx.process_stream(e, t0, t1) for e in exes}
        x = {e: v for e, v in x.items() if v.any()}
        mix = np.zeros(n, np.float32)
        for v in x.values():
            mix += v
        mb = METER_MS * SR // 1000
        bb = BLOCK_MS * SR // 1000
        masks = {e: np.zeros(n, np.float32) for e in x}
        fb_mask = np.zeros(n, bool)
        fit_ok = []
        for k, i in enumerate(range(0, n, mb)):
            t = t0 + k * METER_MS
            lv = {e: float(np.max(np.abs(v[i:i + mb]))) for e, v in x.items()}
            stems = self.pick(t, lv)
            audible = frozenset(e for e, v in lv.items() if v >= AUDIBLE_PEAK)
            # validity (only where the meters show stemmed apps alone): the measured depth sequence
            only_stems = bool(audible) and not (audible - stems)
            if only_stems and stems & audible:
                for _ in range(2):
                    d = DEPTH_DB[self.block_i % len(DEPTH_DB)]
                    self.block_i += 1
                    self.stats["valid_blocks"] += 1
                    if d > VALID_DB:
                        self.fallback_until = max(self.fallback_until, t + HOLD_MS)
            fb = t < self.fallback_until
            self.stats["fallback_blocks"] += fb
            if t not in self.blocks:
                bisect.insort(self.bkeys, t)
            self.blocks[t] = (audible, stems, fb)
            self.stats["stem_count_s"][len(stems & audible) if not fb else 0] += 1
            self.stats["unstemmed_audible_s"] += bool(audible - stems - VOICE_APPS)
            fit_ok.append(only_stems and not fb)
            for e in stems:
                if e in masks and not fb:
                    j = i + (OPEN_MS * SR // 1000 if e not in self.stems_prev else 0)
                    masks[e][j:i + mb] = 1.0
            fb_mask[i:i + mb] = fb
            self.stems_prev = stems
        stem_x = {e: x[e] * masks[e] for e in x if masks[e].any()}
        with ctx.capture_cpu(), threadpool_limits(1):
            c = time.process_time()
            # FIR refit once per REFIT_MS (= the chunk), on the first FIT_MS where only stemmed apps
            # sound (held otherwise)
            keys = sorted(stem_x)
            fit_len = FIT_MS * SR // 1000
            okb = [kb for kb in range(len(fit_ok)) if all(fit_ok[kb:kb + max(1, FIT_MS // METER_MS)]) and (kb + 1) * mb + FIR_TAPS < n]
            for i in [okb[0] * mb] if okb else []:
                act = [e for e in keys if np.any(stem_x[e][i:i + fit_len])]
                if not act:
                    continue
                i0, i1 = max(i, FIR_TAPS), min(i + fit_len, n - FIR_TAPS)
                for e in act:  # per stem (a joint 3 x 128-column lstsq cost 30-340 % of a core): mix minus the others' held FIR
                    tgt = mix[i0:i1].astype(np.float64)
                    for o in act:
                        if o != e:
                            tgt -= np.convolve(stem_x[o][i0 - FIR_TAPS:i1 + FIR_TAPS].astype(np.float64), self.h.get(o, DELTA))[FIR_TAPS + FIR_CEN:FIR_TAPS + FIR_CEN + i1 - i0]
                    col = np.stack([stem_x[e][i0 - (j - FIR_CEN):i1 - (j - FIR_CEN)] for j in range(FIR_TAPS)], axis=1).astype(np.float64)
                    self.h[e], *_ = np.linalg.lstsq(col, tgt, rcond=None)
                    self.stats["fir_fits"] += 1
            resid = mix.astype(np.float64)
            for e, s in stem_x.items():
                h = self.h.get(e)
                if h is None:
                    h = self.h[e] = DELTA
                resid -= oaconvolve(s.astype(np.float64), h)[FIR_CEN:FIR_CEN + n]
            self.stats["cpu_fir_s"] += time.process_time() - c
        # modelled leak: each stem at the measured per-block level, as a copy (see module doc)
        for q, (e, s) in enumerate(sorted(stem_x.items())):
            for i in range(0, n, bb):
                if s[i:i + bb].any():
                    g = 10 ** (LEAK_DB[(self.block_i + 7 * q + i // bb) % len(LEAK_DB)] / 20)
                    resid[i:i + bb] += g * s[i:i + bb]
        resid = resid.astype(np.float32)
        if fb_mask.any():
            resid[fb_mask] = mix[fb_mask]
        outs: list[tuple[str, object]] = []
        feeds = {RESIDUAL: resid}
        feeds.update({e: s for e, s in stem_x.items() if e not in MUSIC_PROCESSES and e not in core.EXCLUDED})
        for key, st in streams.items():
            if key not in feeds:  # the stream went silent: close what it held (VAD kept for later)
                outs += [(key, u) for u in st.finish()]
        for key, s in feeds.items():
            st = streams.get(key)
            if st is None:
                if not s.any():
                    continue
                st = streams[key] = Stream(key)
            with ctx.capture_cpu():
                c = time.process_time()
                outs += [(key, u) for u in st.feed(s, t0)]
                self.stats["cpu_residual_seg_s" if key == RESIDUAL else "cpu_stem_seg_s"] += time.process_time() - c
        return outs


def process(segments, ctx):
    p = P1(ctx)
    mic = sorted((s for s in segments if s.stream == "mic"), key=lambda s: s.start_ms)
    mic_text: dict[int, str | None] = {}
    mi = 0

    def mic_until(t: int) -> None:
        nonlocal mi
        while mi < len(mic) and mic[mi].start_ms < t:
            s = mic[mi]
            mi += 1
            earlier = mic_text.get(s.continues_id) if s.continues_id is not None else None
            p.last_text["mic"] = earlier
            n0 = len(p.out)
            p.emit("mic", "mic", s.start_ms, s.end_ms, ctx.audio(s), s.speech_ms, s.sources, earlier is not None)
            mic_text[s.id] = p.out[n0].text or None if len(p.out) > n0 else None

    if any(s.stream == "system" for s in segments):
        for w0, w1 in capture_windows(segments, ctx.source):
            streams: dict[str, Stream] = {}
            for c0 in range(w0, w1, CHUNK_MS):
                c1 = min(w1, c0 + CHUNK_MS)
                outs = p.chunk(c0, c1, streams)
                if c1 == w1:
                    for key in list(streams):
                        outs += [(key, u) for u in streams.pop(key).finish()]
                outs.sort(key=lambda ku: ku[1].start_ms)
                for key, u in outs:
                    mic_until(u.start_ms)
                    p.system_utterance(key, u)
    mic_until(1 << 62)
    return p.out
