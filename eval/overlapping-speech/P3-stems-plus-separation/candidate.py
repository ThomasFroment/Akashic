"""P3 = C1 per-app stems + call residual (P1, strict exclusion) + C4 overlap-gated separation (P2) on
every output stream.

Capture side: P1's front end, copied unchanged (stem picking by A3 meters, FIR residual with the
modelled leak, validity switch back to today's mix, one Segmenter + Silero VAD per stream, P1's
fixed span_info, FIR fit on one BLAS thread). One change, the strict exclusion rule (P1's
runs_strict_excl, now the default): any system utterance, stem or residual, is dropped while the
meters show an excluded app audible within EXCL_MARGIN_MS of it (today's capture drops such
segments whole).

Worker side, per system utterance of every output stream (each stem, the residual, and the
residual while it carries the fallback mix): P2's gate (pyannote segmentation 3.0 powerset, two
speakers >= 0.5 s, gaps < 0.3 s merged), MossFormer2_SS_16K separation on each gated run +- 1 s
(ONNX on CPU, P2's exported model), outputs tied to the track already talking (gain fit on the
alone context, WeSpeaker cosine when under 3 dB), hybrid tracks (original audio where one talker
is alone), pieces decoded with today's decode and attributed with today's speakers job, each row
stamped with its real start. Ungated utterances take P1's path. Mic: today's pipeline. sep.py is
the frozen copy of P2's candidate the gate and separation come from.

Language split (P3_LANGSPLIT=1, the default; the fix for P1's mixed-day regression): a system row
of 5 s or more whose FR/EN choice is not clear (chosen language < 0.98 of FR+EN) is cut at the one
of its 3 deepest 100 ms energy dips whose two sides get different languages, and each side is
decoded with its own choice. P1 decoded whole call rows holding an English stretch then a French one
in one language and deleted the other (langsplit-check.json).

P3_SHADOW=1 (accuracy driver only): a second back end replays P1 (no separation) on the very same
capture front end, with its own prompts and speaker store; decodes and speaker embeddings of
identical inputs are shared between the two back ends, and each back end's GPU seconds count the
decodes it used. ctx.p3_shadow holds P1's rows; the returned rows are P3's. Off in cost runs.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import sys
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
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import sep  # noqa: E402  (P3's frozen copy of P2's candidate)

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
EXCL_MARGIN_MS = METER_MS
SHADOW = os.environ.get("P3_SHADOW") == "1"
SEPARATE = os.environ.get("P3_SEPARATE", "1") == "1"


def _spike() -> tuple[np.ndarray, np.ndarray, str]:
    p = HERE / "spike.json"  # copy of P1's spike.json (real DT-770 endpoint, 128-tap FIR)
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


LANGSPLIT = os.environ.get("P3_LANGSPLIT", "1") == "1"
LS_MIN_S = 5.0  # rows shorter than this keep one language
LS_EDGE_S = 1.5  # a side shorter than this is not language-identified
LS_CONF = float(os.environ.get("P3_LS_CONF", "0.98"))  # whole-row FR/EN share above which the row keeps one language
LS_POINTS = 3


def _fr_en(ctx, x: np.ndarray) -> tuple[str, float]:
    """Whisper's FR/EN choice on x (as whisper._detect) and the chosen language's share of FR+EN."""
    model = ctx.transcriber._load()
    g = ctx.gpu_s
    with ctx.gpu():
        _b, _p, probs = model.detect_language(x)
    st = getattr(ctx, "p3", None)
    if st is not None:  # language-split GPU, counted apart from the decodes
        st["ls_detect_calls"] = st.get("ls_detect_calls", 0) + 1
        st["ls_gpu_s"] = st.get("ls_gpu_s", 0.0) + ctx.gpu_s - g
    s = {lang: p for lang, p in probs if lang in ("fr", "en")}
    fr, en = s.get("fr", 0.0), s.get("en", 0.0)
    tot = fr + en or 1e-9
    return ("fr", fr / tot) if fr >= en else ("en", en / tot)


def lang_split(ctx, x: np.ndarray) -> tuple[list[tuple[int, int]], dict]:
    """Sample ranges to decode separately: one range unless the row is long, its FR/EN choice is not
    clear, and the two sides of one of its LS_POINTS deepest 100 ms energy dips get different
    languages (then the dip with the clearest pair wins). Whisper picks one language per call, so a
    row holding an English stretch then a French one loses one of them (the mixed-day regression)."""
    n = len(x)
    if n < LS_MIN_S * SR:
        return [(0, n)], {}
    lang, p = _fr_en(ctx, x)
    info = {"p_top": p, "lang": lang}
    if p >= LS_CONF:
        return [(0, n)], info
    f = 320  # 20 ms frames
    e = (x[:n // f * f].astype(np.float64) ** 2).reshape(-1, f).mean(1)
    e = np.convolve(e, np.ones(5) / 5, mode="same")  # 100 ms
    lo, hi = int(LS_EDGE_S * SR / f), len(e) - int(LS_EDGE_S * SR / f)
    if hi <= lo:
        return [(0, n)], info
    order = lo + np.argsort(e[lo:hi])
    pts: list[int] = []
    for k in order:
        if all(abs(int(k) - q) > 25 for q in pts):  # dips at least 0.5 s apart
            pts.append(int(k))
        if len(pts) == LS_POINTS:
            break
    best = None
    for k in pts:
        cut = k * f
        la, pa = _fr_en(ctx, x[:cut])
        lb, pb = _fr_en(ctx, x[cut:])
        if la != lb and (best is None or min(pa, pb) > best[1]):
            best = (cut, min(pa, pb))
    info["tried"] = len(pts)
    if best is None:
        return [(0, n)], info
    info["split"] = best[0]
    return [(0, best[0]), (best[0], n)], info


def _h(x: np.ndarray) -> tuple[int, bytes]:
    return len(x), hashlib.blake2b(np.ascontiguousarray(x).tobytes(), digest_size=16).digest()


class Shared:
    """Results of identical inputs, shared by the two back ends (shadow mode only)."""

    def __init__(self, on: bool) -> None:
        self.on = on
        self.dec: dict = {}
        self.turns: dict = {}


# --- worker side: one back end = prompts, speaker store, rows ------------------------------------


class Backend:
    def __init__(self, name: str, ctx, separate: bool, shared: Shared, own_ctx_store: bool) -> None:
        self.name, self.ctx, self.separate, self.shared = name, ctx, separate, shared
        if own_ctx_store:  # the P3 back end uses the harness context's speaker store
            self.spk, self.store = ctx._spk, ctx.speakers
        else:
            from akashic.storage.speakers import SpeakerStore

            self.spk = core.speaker_db()
            self.store = SpeakerStore(self.spk)
        self.counts_speakers = own_ctx_store
        self.langsplit = LANGSPLIT and own_ctx_store  # the P3 back end only (the shadow stays P1)
        self.out: list[core.Transcript] = []
        self.done: dict[str, list] = {}
        self.last_text: dict[str, str | None] = {}
        self.gpu_s = 0.0
        self.spk_cpu_s = 0.0
        self.n_decode = 0
        self.decode_audio_s: dict[str, float] = {}
        self.rows: dict[str, int] = {}
        self.decodes: list[tuple[int, float, str]] = []  # (start ms, audio s, row kind): per-hour cost model

    def decode(self, samples: np.ndarray, prev, earlier, t_ms: int, kind: str):
        key = (*_h(samples), prev, earlier) if self.shared.on else None
        if key is not None and key in self.shared.dec:
            res, dt = self.shared.dec[key]
        else:
            g = self.ctx.gpu_s
            res = self.ctx.decode(samples, prev, earlier)
            dt = self.ctx.gpu_s - g
            if key is not None:
                self.shared.dec[key] = (res, dt)
        self.gpu_s += dt
        self.n_decode += 1
        self.decode_audio_s[kind] = self.decode_audio_s.get(kind, 0.0) + len(samples) / SR
        self.rows[kind] = self.rows.get(kind, 0) + 1
        self.decodes.append((int(t_ms), len(samples) / SR, kind))
        return res

    def identify(self, samples: np.ndarray):
        from akashic.storage.speakers import MIN_IDENTIFY_MS, NewTurn

        key = _h(samples) if self.shared.on else None
        if key is not None and key in self.shared.turns:
            turns, dt = self.shared.turns[key]
        else:
            c = time.process_time()
            turns = [NewTurn(t.start_ms, t.end_ms, t.speech_ms, self.ctx.voices.embed(t.samples) if t.speech_ms >= MIN_IDENTIFY_MS else None)
                     for t in self.ctx.voices.turns(samples)]
            dt = time.process_time() - c
            if key is not None:
                self.shared.turns[key] = (turns, dt)
        c = time.process_time()
        sid = self.spk.execute("INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms) VALUES ('system', 0, 0, 0)").lastrowid
        self.store.record_turns(sid, turns, 0)
        rows = self.spk.execute("SELECT speaker_id, start_ms, end_ms FROM speaker_turns WHERE segment_id = ? ORDER BY id", (sid,)).fetchall()
        spk = self.spk.execute("SELECT speaker_id FROM audio_segments WHERE id = ?", (sid,)).fetchone()[0]
        dt += time.process_time() - c
        self.spk_cpu_s += dt
        if self.counts_speakers:
            self.ctx.cpu_speakers_s += dt
        return spk, [tuple(r) for r in rows]

    def self_id(self) -> int:
        from akashic.storage.db import transaction

        with transaction(self.spk) as conn:
            return self.store._self_id(conn, 0)

    def row(self, stream, a, b, res, sources, speech_ms, kind, key, separated) -> core.Transcript:
        t = core.Transcript(stream, int(a), int(b), res.text if res else "", res.language if res else None, tuple(sources), speech_ms=int(speech_ms))
        t.p1_kind = kind  # 'mic' / 'stem' / 'residual' (scoring splits)
        t.p3_key = key
        t.p3_sep = separated
        return t

    def emit(self, key: str, stream: str, a: int, b: int, samples: np.ndarray, speech_ms: int, sources: tuple, continues: bool, kind: str) -> None:
        if self.langsplit and stream == "system":
            parts, info = lang_split(self.ctx, samples)
            st = self.ctx.p3
            st["ls_rows"] += 1
            st["ls_detected"] += "p_top" in info
            if len(parts) > 1:
                st["ls_split"] += 1
                st["ls_split_by_kind"][kind] = st["ls_split_by_kind"].get(kind, 0) + 1
                return self.emit_parts(key, a, b, samples, sources, continues, kind, parts)
        done = self.done.setdefault(key, [])
        prev = previous_transcript(done[-50:], a)
        earlier = self.last_text.get(key) if continues else None
        res = self.decode(samples, prev, earlier, a, kind)
        self.last_text[key] = res.text if res else None
        t = self.row(stream, a, b, res, sources, speech_ms, kind, key, False)
        t.p3_prompted, t.p3_continued = prev is not None, earlier is not None
        if res:
            done.append((b, a, res.text))
            if stream == "mic":
                me = self.self_id()
                t.speaker_id, t.turns = me, [(me, 0, b - a)]
            else:
                t.speaker_id, t.turns = self.identify(samples)
        self.out.append(t)

    def emit_parts(self, key, a, b, samples, sources, continues, kind, parts) -> None:
        """A row cut by lang_split: each side decoded (its own FR/EN choice) and attributed apart."""
        done = self.done.setdefault(key, [])
        earlier = self.last_text.get(key) if continues else None
        texts = []
        for i, (s0, s1) in enumerate(parts):
            start = int(a + s0 * 1000 // SR)
            end = int(min(b, a + s1 * 1000 // SR)) if i == len(parts) - 1 else int(a + s1 * 1000 // SR)
            y = samples[s0:s1]
            prev = previous_transcript(done[-50:], start)
            res = self.decode(y, prev, earlier if i == 0 else None, start, kind)
            row = self.row("system", start, max(end, start + 1), res, sources, int((s1 - s0) * 1000 // SR), kind, key, False)
            row.p3_prompted, row.p3_continued, row.p3_lsplit = prev is not None, bool(i == 0 and earlier is not None), True
            if res:
                texts.append(res.text)
                done.append((end, start, res.text))
                row.speaker_id, row.turns = self.identify(y)
            self.out.append(row)
        self.last_text[key] = " ".join(texts) or None

    def drop(self, key: str) -> None:
        self.last_text[key] = None

    def system(self, key: str, u, sources: tuple, kind: str, gate) -> None:
        if not self.separate:
            return self.emit(key, "system", u.start_ms, u.end_ms, u.samples, u.speech_ms, sources, u.continues, kind)
        act, spans = gate
        if not spans:
            return self.emit(key, "system", u.start_ms, u.end_ms, u.samples, u.speech_ms, sources, u.continues, kind)
        st = self.ctx.p3
        w0, c0, a0 = sep.STATS["sep_wall_s"], sep.STATS["sep_cpu_s"], sep.STATS["sep_audio_s"]
        pieces = sep.plan(self.ctx, u.samples, act, spans)
        # separation job: (utterance end ms = when it could start, wall s, CPU core-s, separated audio s)
        st["sep_jobs"].append((int(u.end_ms), sep.STATS["sep_wall_s"] - w0, sep.STATS["sep_cpu_s"] - c0, sep.STATS["sep_audio_s"] - a0))
        if pieces is None:
            st["sep_fallback_utts"] += 1
            return self.emit(key, "system", u.start_ms, u.end_ms, u.samples, u.speech_ms, sources, u.continues, kind)
        st["sep_used_utts"] += 1
        st["sep_used_by_kind"][kind] = st["sep_used_by_kind"].get(kind, 0) + 1
        st["sep_spans"] += [(int(u.start_ms + a * 10), int(u.start_ms + b * 10), key) for a, b in spans]
        done = self.done.setdefault(key, [])
        earlier = self.last_text.get(key) if u.continues else None
        texts = []
        for t, a, b, y, ms in pieces:
            start = int(u.start_ms + a * 10)
            end = int(min(u.end_ms, u.start_ms + b * 10))
            first = a == 0 and t == 0
            prev = previous_transcript(done[-50:], start)
            res = self.decode(y, prev, earlier if first else None, start, kind)
            st["pieces"] += 1
            row = self.row("system", start, max(end, start + 1), res, sources, ms, kind, key, True)
            row.p3_prompted, row.p3_continued = prev is not None, bool(first and earlier is not None)
            if res:
                texts.append(res.text)
                done.append((end, start, res.text))
                row.speaker_id, row.turns = self.identify(y)
            self.out.append(row)
        self.last_text[key] = " ".join(texts) or None

    def summary(self) -> dict:
        return {"gpu_s": self.gpu_s, "spk_cpu_s": self.spk_cpu_s, "n_decode": self.n_decode, "decode_audio_s": self.decode_audio_s, "rows": self.rows,
                "n_out": len(self.out)}


# --- capture side: P1's front end ---------------------------------------------------------------


class Front:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.blocks: dict[int, tuple[frozenset, frozenset, bool]] = {}  # meter block start -> (audible, stems, fallback)
        self.bkeys: list[int] = []  # block starts, increasing (chunks are fed in time order)
        self.h: dict[str, np.ndarray] = {}
        self.stems_prev: frozenset = frozenset()
        self.last_audible: dict[str, int] = {}
        self.fallback_until = -1
        self.block_i = 0
        self.stats = {"dropped": {}, "fallback_blocks": 0, "valid_blocks": 0, "fir_fits": 0,
                      "cpu_fir_s": 0.0, "cpu_residual_seg_s": 0.0, "cpu_stem_seg_s": 0.0, "stem_count_s": {0: 0, 1: 0, 2: 0, 3: 0}, "unstemmed_audible_s": 0,
                      "leak_source": LEAK_SRC, "leak_db_rms_model": float(10 * np.log10(np.mean(10 ** (LEAK_DB / 10))))}

    def span_info(self, a: int, b: int):
        # meter blocks start at chunk starts + k * METER_MS (not aligned to METER_MS multiples):
        # every block that overlaps [a, b)
        aud, full, excl, fb_any = set(), set(), False, False
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
            fb_any |= fb
        return aud, full, excl, fb_any

    def classify(self, key: str, u):
        """(sources, kind) of a system utterance, or (None, drop reason)."""
        if self.span_info(u.start_ms - EXCL_MARGIN_MS, u.end_ms + EXCL_MARGIN_MS)[2]:
            return None, "excluded_audible"  # strict rule: no stored audio near an excluded app's sound
        if key == RESIDUAL:
            _, full, _, fb = self.span_info(u.start_ms, u.end_ms)
            sources = tuple(sorted(full - core.EXCLUDED))
            if not sources:
                return None, "residual_leak_only"
            if set(sources) <= MUSIC_PROCESSES:
                return None, "residual_music"
            return sources, ("fallback" if fb else "residual")
        return (key,), "stem"

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
            keys = sorted(stem_x)
            fit_len = FIT_MS * SR // 1000
            okb = [kb for kb in range(len(fit_ok)) if all(fit_ok[kb:kb + max(1, FIT_MS // METER_MS)]) and (kb + 1) * mb + FIR_TAPS < n]
            for i in [okb[0] * mb] if okb else []:
                act = [e for e in keys if np.any(stem_x[e][i:i + fit_len])]
                if not act:
                    continue
                i0, i1 = max(i, FIR_TAPS), min(i + fit_len, n - FIR_TAPS)
                for e in act:
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
            if key not in feeds:
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


# --- the candidate ---------------------------------------------------------------------------------


def gate_of(ctx, u, key: str, kind: str, sources: tuple):
    """P2's gate on one utterance of one output stream: (activity, gated cell runs); recorded for the
    gate report (stream key, kind, span, the stream's full-level sources, gated spans)."""
    st = ctx.p3
    c = time.process_time()
    act = sep.activity(ctx, u.samples)
    spans = sep.gate(act)
    st["gate_cpu_s"] += time.process_time() - c
    st["gate_utts"] += 1
    st["gate_audio_s"] += len(u.samples) / SR
    if spans:
        st["gated_utts"] += 1
        st["gated_audio_s"] += len(u.samples) / SR
        st["gated_by_kind"][kind] = st["gated_by_kind"].get(kind, 0) + 1
    st["records"].append((key, kind, int(u.start_ms), int(u.start_ms + len(u.samples) * 1000 // SR), list(sources),
                          [(int(u.start_ms + a * 10), int(u.start_ms + b * 10)) for a, b in spans]))
    return act, spans


def process(segments, ctx):
    sep.reset_stats()
    ctx.p3 = {"gate_cpu_s": 0.0, "gate_utts": 0, "gate_audio_s": 0.0, "gated_utts": 0, "gated_audio_s": 0.0, "gated_by_kind": {},
              "sep_used_utts": 0, "sep_fallback_utts": 0, "sep_used_by_kind": {}, "pieces": 0, "sep_spans": [], "records": [], "sep_jobs": [],
              "dropped": {}, "separate": SEPARATE, "shadow": SHADOW,
              "langsplit": LANGSPLIT, "ls_conf": LS_CONF, "ls_rows": 0, "ls_detected": 0, "ls_split": 0, "ls_split_by_kind": {}}
    shared = Shared(SHADOW)
    front = Front(ctx)
    p3 = Backend("p3", ctx, SEPARATE, shared, own_ctx_store=True)
    backs = [p3] + ([Backend("p1", ctx, False, shared, own_ctx_store=False)] if SHADOW else [])
    mic = sorted((s for s in segments if s.stream == "mic"), key=lambda s: s.start_ms)
    mic_text = [{} for _ in backs]
    mi = 0

    def mic_until(t: int) -> None:
        nonlocal mi
        while mi < len(mic) and mic[mi].start_ms < t:
            s = mic[mi]
            mi += 1
            x = ctx.audio(s)
            for b, mt in zip(backs, mic_text, strict=True):
                earlier = mt.get(s.continues_id) if s.continues_id is not None else None
                b.last_text["mic"] = earlier
                n0 = len(b.out)
                b.emit("mic", "mic", s.start_ms, s.end_ms, x, s.speech_ms, s.sources, earlier is not None, "mic")
                mt[s.id] = b.out[n0].text or None if len(b.out) > n0 else None

    if any(s.stream == "system" for s in segments):
        for w0, w1 in capture_windows(segments, ctx.source):
            streams: dict[str, Stream] = {}
            for c0 in range(w0, w1, CHUNK_MS):
                c1 = min(w1, c0 + CHUNK_MS)
                outs = front.chunk(c0, c1, streams)
                if c1 == w1:
                    for key in list(streams):
                        outs += [(key, u) for u in streams.pop(key).finish()]
                outs.sort(key=lambda ku: ku[1].start_ms)
                for key, u in outs:
                    mic_until(u.start_ms)
                    sources, kind = front.classify(key, u)
                    if sources is None:
                        front.stats["dropped"][kind] = front.stats["dropped"].get(kind, 0) + 1
                        for b in backs:
                            b.drop(key)
                        continue
                    gate = gate_of(ctx, u, key, kind, sources) if SEPARATE else None
                    for b in backs:
                        b.system(key, u, sources, kind, gate)
    mic_until(1 << 62)
    ctx.p3["front"] = front.stats
    ctx.p3["backends"] = {b.name: b.summary() for b in backs}
    ctx.p3["decodes"] = p3.decodes
    ctx.p3["sep"] = {k: v for k, v in sep.STATS.items() if not isinstance(v, list)}
    ctx.p3["sep_calls"] = list(sep.STATS["sep_calls"])
    if SHADOW:
        ctx.p3_shadow = backs[1].out
        ctx.p3["decodes_p1"] = backs[1].decodes
    return p3.out
