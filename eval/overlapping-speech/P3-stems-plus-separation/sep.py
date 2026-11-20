"""Copy of P2-gated-separation/candidate.py (frozen for P3; P3 imports gate, activity, plan from it).
Changes: MODEL points at P2's exported ONNX (reused, not copied); _separate also records process CPU
seconds (core-seconds) per call in STATS["sep_cpu_s"].

P2 = C4 overlap-gated separation on today's streams (no capture change).

Per stored system segment, before transcription:
1. gate: pyannote segmentation 3.0 (the speakers job's model, same 10 s windows) on the segment;
   frames where its powerset names two speakers, runs >= MIN_OVL_MS (gaps < MERGE_GAP_MS merged);
2. separate: each gated run +- CONTEXT_MS (merged into windows) through MossFormer2_SS_16K (ONNX,
   onnxruntime CPU; P2_SEP=cuda for the GPU variant), outputs scaled by a least-squares gain fit to
   the mixture;
3. hybrid tracks: two tracks over the whole segment. Inside a window, overlap frames take each
   speaker's separated output; other frames route the ORIGINAL audio to the track whose separated
   output dominates there (100 ms smoothed energy); 20 ms crossfades. Outside windows the original
   goes to the track that owns the neighbouring context. Window outputs are tied to tracks by the
   gain fit on the alone context before the window (dominant output = the track already talking),
   WeSpeaker cosine against that context when the energy ratio is under 3 dB;
4. pieces: each track is cut where the other track took over; a piece with < MIN_SPEECH_MS of
   Silero speech or a speech RMS more than QUIET_DB under the segment's (silent-output
   hallucination) is handed back to the other track as original audio. When fewer than two
   tracks keep a piece, the segment takes today's path bit for bit;
5. each piece is decoded with today's decode (loudness, prompt, hallucination filter) and attributed
   with today's speakers job, as a row stamped with its real start time.
Ungated segments and mic segments take today's path (baseline.py) bit for bit.

STATS (module level) collects gate and separation figures for the report; numbers only.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

import core
from core import Transcript

HERE = Path(__file__).resolve().parent
MODEL = HERE.parent / "P2-gated-separation" / "model" / "mossformer2_ss_16k.onnx"
SR = core.SR
CELL = 160  # 10 ms grid
MIN_OVL_MS = int(os.environ.get("P2_MIN_OVL_MS", 500))
MERGE_GAP_MS = 300
CONTEXT_MS = 1000
XFADE = 320  # 20 ms
SMOOTH_MS = 300
MIN_RUN_MS = 300  # a routing run shorter than this joins its neighbours
MIN_SPEECH_MS = 300
QUIET_DB = 20.0
PIECE_GAP_MS = int(os.environ.get("P2_PIECE_GAP_MS", 1500))
TIE_DB = 3.0
SEP_DEVICE = os.environ.get("P2_SEP", "cpu")

STATS: dict = {}


def reset_stats() -> None:
    STATS.clear()
    STATS.update({"system_segments": 0, "system_audio_s": 0.0, "gated_segments": 0, "gated_audio_s": 0.0, "used_segments": 0,
                  "fallback_segments": 0, "sep_windows": 0, "sep_audio_s": 0.0, "sep_wall_s": 0.0, "sep_calls": [],
                  "gated_spans": [], "sep_spans": [], "seg_spans": [], "pieces": 0, "pieces_dropped": 0, "tie_wespeaker": 0,
                  "whisper_calls": 0, "sep_on_gpu": SEP_DEVICE == "cuda", "sep_cpu_s": 0.0})


reset_stats()

# --- today's path (baseline.py, verbatim in effect) --------------------------------------------


def previous_transcript(done: list[tuple[int, int, str]], before_ms: int) -> str | None:
    from akashic.pipeline.transcribe import PREVIOUS_WITHIN_MS

    best = None
    for end, start, text in done:
        if before_ms - PREVIOUS_WITHIN_MS <= end <= before_ms and start < before_ms and (best is None or end > best[0]):
            best = (end, text)
    return best[1] if best else None


def today(seg, samples, ctx, done, text_of) -> Transcript:
    earlier = text_of.get(seg.continues_id) if seg.continues_id is not None else None
    res = ctx.decode(samples, previous_transcript(done[seg.stream][-50:], seg.start_ms), earlier)
    STATS["whisper_calls"] += 1
    text_of[seg.id] = res.text if res else None
    t = Transcript(seg.stream, seg.start_ms, seg.end_ms, res.text if res else "", res.language if res else None, seg.sources, speech_ms=seg.speech_ms)
    if res:
        done[seg.stream].append((seg.end_ms, seg.start_ms, res.text))
        if seg.stream == "mic":
            me = ctx.self_id()
            t.speaker_id, t.turns = me, [(me, 0, seg.end_ms - seg.start_ms)]
        else:
            t.speaker_id, t.turns = ctx.identify(samples)
    return t


# --- gate ------------------------------------------------------------------------------------


def activity(ctx, x: np.ndarray) -> np.ndarray:
    """Speakers active per 10 ms cell (0..3) from pyannote segmentation 3.0, the speakers job's windows."""
    from akashic.pipeline.speakers import MIN_WINDOW_MS, WINDOW, decode_window

    seg_sess = ctx.voices._load()[0]
    n = len(x) // CELL
    out = np.zeros(n, np.int8)
    for start in range(0, len(x), WINDOW):
        chunk = x[start:start + WINDOW]
        if len(chunk) * 1000 < MIN_WINDOW_MS * SR:
            continue
        padded = np.zeros(WINDOW, np.float32)
        padded[:len(chunk)] = chunk
        logits = seg_sess.run(None, {"input_values": padded[None, None, :]})[0][0]
        act = decode_window(np.asarray(logits, np.float32)).sum(axis=1)
        per = WINDOW / len(act)
        c0, c1 = start // CELL, min(n, (start + len(chunk)) // CELL)
        centers = ((np.arange(c0, c1) * CELL + CELL // 2 - start) / per).astype(int)
        out[c0:c1] = act[np.clip(centers, 0, len(act) - 1)]
    return out


def runs(mask: np.ndarray) -> list[tuple[int, int]]:
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1), strict=True))


def merge_runs(rs, gap):
    out = []
    for a, b in rs:
        if out and a - out[-1][1] < gap:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def gate(act: np.ndarray) -> list[tuple[int, int]]:
    rs = merge_runs(runs(act >= 2), MERGE_GAP_MS // 10)
    return [(a, b) for a, b in rs if b - a >= MIN_OVL_MS // 10]


# --- separation ------------------------------------------------------------------------------

_SESS = None


def separator():
    global _SESS
    if _SESS is None:
        import onnxruntime as ort

        o = ort.SessionOptions()
        o.log_severity_level = 3
        # every window has its own length: no per-shape cuDNN search, arena grown to what is asked
        cuda = ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC", "arena_extend_strategy": "kSameAsRequested"})
        prov = [cuda, "CPUExecutionProvider"] if SEP_DEVICE == "cuda" else ["CPUExecutionProvider"]
        if SEP_DEVICE == "cuda":
            ort.preload_dlls()
        _SESS = ort.InferenceSession(str(MODEL), o, providers=prov)
    return _SESS


def separate(ctx, xw: np.ndarray) -> np.ndarray:
    """(2, len) separated outputs, each scaled by the least-squares fit of the mixture."""
    key = (len(xw), hash(xw.tobytes())) if CACHE and SEP_DEVICE != "cuda" else None
    if key in _CACHE:  # cost runs only: CPU separation is not GPU time, so later reps reuse it
        return _CACHE[key]
    o = _separate(ctx, xw)
    if key is not None:
        _CACHE[key] = o
    return o


CACHE = os.environ.get("P2_SEP_CACHE") == "1"
_CACHE: dict = {}


def _separate(ctx, xw: np.ndarray) -> np.ndarray:
    n = len(xw)
    m = max(4000, -(-n // 8) * 8)
    xp = np.zeros(m, np.float32)
    xp[:n] = xw
    scale = np.float32(10 ** (-25 / 20) / (np.sqrt(np.mean(xw.astype(np.float64) ** 2)) + 1e-8))
    sess = separator()
    t = time.perf_counter()
    c = time.process_time()
    if SEP_DEVICE == "cuda":
        with ctx.gpu():
            y = sess.run(None, {"mix": (xp * scale)[None]})[0]
    else:
        y = sess.run(None, {"mix": (xp * scale)[None]})[0]
    dt = time.perf_counter() - t
    STATS["sep_cpu_s"] += time.process_time() - c
    STATS["sep_wall_s"] += dt
    STATS["sep_audio_s"] += n / SR
    STATS["sep_calls"].append((n / SR, dt))
    y = y[:, :n].astype(np.float64) / float(scale)
    g, *_ = np.linalg.lstsq(y.T, xw.astype(np.float64), rcond=None)
    return (y * g[:, None]).astype(np.float32)


# --- hybrid tracks -------------------------------------------------------------------------------


def cell_energy(x: np.ndarray, n: int) -> np.ndarray:
    e = (x[:n * CELL].astype(np.float64) ** 2).reshape(n, CELL).mean(1)
    k = SMOOTH_MS // 10
    return np.convolve(e, np.ones(k) / k, mode="same")


def tie_by_voice(ctx, context: np.ndarray, outs: np.ndarray) -> int | None:
    """Index of the output whose voice is closer (WeSpeaker cosine) to the context audio."""
    if len(context) < SR:
        return None
    try:
        ref = ctx.voices.embed(context)
        cos = [float(np.dot(ref, ctx.voices.embed(o))) for o in outs]
    except ValueError:
        return None
    STATS["tie_wespeaker"] += 1
    return int(np.argmax(cos))


def build_tracks(ctx, x: np.ndarray, act: np.ndarray, spans: list[tuple[int, int]]):
    """Per-cell sources for two tracks: wx[k] (original weight), wo[k] (separated weight), plus the
    separated signals. Returns (wx (2, n), wo (2, n), sep (2, len))."""
    n = len(act)
    ctxc = CONTEXT_MS // 10
    wins = merge_runs([(max(0, a - ctxc), min(n, b + ctxc)) for a, b in spans], 1)
    wx = np.zeros((2, n), np.float32)
    wo = np.zeros((2, n), np.float32)
    sep = np.zeros((2, len(x)), np.float32)
    owner, prev_end = 0, 0
    for a, b in wins:
        wx[owner, prev_end:a] = 1.0  # outside: the track that owns the context
        o = separate(ctx, x[a * CELL:min(len(x), b * CELL)])
        STATS["sep_windows"] += 1
        m = b - a
        e = np.stack([cell_energy(o[i], m) if len(o[i]) >= m * CELL else np.pad(cell_energy(o[i], len(o[i]) // CELL), (0, m - len(o[i]) // CELL)) for i in range(2)])
        ov = act[a:b] >= 2
        first = int(np.argmax(ov)) if ov.any() else m
        last = m - int(np.argmax(ov[::-1])) if ov.any() else 0
        lead = e[:, :first].sum(1)
        trail = e[:, last:].sum(1)
        # which output continues the owner's voice: gain fit on the alone context before the window
        ctx_e, ctx_audio = (lead, x[max(0, (a - 300) * CELL):a * CELL + first * CELL]) if first > 0 else (trail, x[b * CELL:(b + 300) * CELL])
        hi, lo = max(ctx_e), min(ctx_e)
        k = int(np.argmax(ctx_e))
        if hi <= lo * 10 ** (TIE_DB / 10):
            v = tie_by_voice(ctx, ctx_audio, o)
            k = k if v is None else v
        perm = (k, 1 - k) if owner == 0 else (1 - k, k)  # perm[track] = output index
        so = o[list(perm)]
        sep[:, a * CELL:a * CELL + so.shape[1]] = so
        es = e[list(perm)]
        dom = min_runs(np.argmax(es, axis=0), MIN_RUN_MS // 10)
        for t in range(2):
            wo[t, a:b] = ov
            wx[t, a:b] = (~ov) & (dom == t)
        # after the window, the track dominating the trailing context owns the audio
        owner = int(np.argmax(es[:, max(last, m - 30):].sum(1))) if m - last > 0 else int(np.argmax(es[:, -30:].sum(1)))
        prev_end = b
    wx[owner, prev_end:] = 1.0
    return wx, wo, sep


def min_runs(lab: np.ndarray, k: int) -> np.ndarray:
    """Labels with runs shorter than k cells replaced by the previous run's label (the first by the next)."""
    lab = lab.copy()
    while True:
        d = np.flatnonzero(np.diff(lab)) + 1
        edges = np.concatenate([[0], d, [len(lab)]])
        short = [(a, b) for a, b in zip(edges[:-1], edges[1:], strict=True) if b - a < k and len(edges) > 2]
        if not short:
            return lab
        a, b = min(short, key=lambda r: r[1] - r[0])
        lab[a:b] = lab[a - 1] if a > 0 else lab[b]


def render(x, sep, wx, wo, t) -> np.ndarray:
    n = wx.shape[1]
    up = lambda w: np.convolve(np.repeat(w, CELL), np.ones(XFADE) / XFADE, mode="same").astype(np.float32)
    L = n * CELL
    y = up(wx[t]) * x[:L] + up(wo[t]) * sep[t, :L]
    return np.concatenate([y, x[L:] * np.float32(wx[t, -1] if n else 1.0)])


def track_pieces(wx, wo, t) -> list[tuple[int, int]]:
    return merge_runs(runs((wx[t] + wo[t]) > 0), PIECE_GAP_MS // 10)


_VAD = None


def speech_cells(y: np.ndarray) -> tuple[int, float]:
    """(speech ms, RMS over speech windows) of a piece by Silero VAD."""
    global _VAD
    from akashic.capture.vad import WINDOW, SileroVad

    if _VAD is None:
        _VAD = SileroVad()
    _VAD.reset()
    ms, sq, k = 0, 0.0, 0
    for i in range(0, len(y) - WINDOW + 1, WINDOW):
        w = y[i:i + WINDOW]
        if w.any() and _VAD.probability(w) >= 0.5:
            ms += WINDOW * 1000 // SR
            sq += float(np.sum(w.astype(np.float64) ** 2))
            k += WINDOW
    return ms, (sq / k) ** 0.5 if k else 0.0


def plan(ctx, x, act, spans):
    """Kept pieces [(track, a_cell, b_cell, audio, speech_ms)] or None (today's path)."""
    wx, wo, sep = build_tracks(ctx, x, act, spans)
    sp = act >= 1
    ref = np.sqrt(np.mean(x[:len(sp) * CELL].reshape(-1, CELL)[sp].astype(np.float64) ** 2)) if sp.any() else np.sqrt(np.mean(x.astype(np.float64) ** 2))
    for _round in range(2):
        moved = False
        kept = []
        for t in range(2):
            y = render(x, sep, wx, wo, t)
            for a, b in track_pieces(wx, wo, t):
                seg_y = y[a * CELL:b * CELL if b < wx.shape[1] else len(y)]
                ms, rms = speech_cells(seg_y)
                if ms < MIN_SPEECH_MS or rms < ref * 10 ** (-QUIET_DB / 20):
                    STATS["pieces_dropped"] += 1
                    if _round == 0:  # hand the region back to the other track as original audio
                        wx[1 - t, a:b] = np.maximum(wx[1 - t, a:b], (wx[t, a:b] + wo[t, a:b]) > 0)
                        wo[1 - t, a:b] = np.where((wx[t, a:b] + wo[t, a:b]) > 0, 0, wo[1 - t, a:b])
                        wx[t, a:b] = 0
                        wo[t, a:b] = 0
                        moved = True
                    continue
                kept.append((t, a, b, seg_y, ms))
        if not moved:
            break
    if len({k[0] for k in kept}) < 2:
        return None
    return sorted(kept, key=lambda k: k[1])


# --- the candidate ---------------------------------------------------------------------------------


def process(segments, ctx):
    out = []
    text_of: dict[int, str | None] = {}
    done: dict[str, list] = {"mic": [], "system": []}
    for seg in segments:
        x = ctx.audio(seg)
        if seg.stream != "system":
            out.append(today(seg, x, ctx, done, text_of))
            continue
        STATS["system_segments"] += 1
        STATS["system_audio_s"] += len(x) / SR
        STATS["seg_spans"].append((seg.start_ms, seg.start_ms + len(x) * 1000 // SR))
        act = activity(ctx, x)
        spans = gate(act)
        if not spans:
            out.append(today(seg, x, ctx, done, text_of))
            continue
        STATS["gated_segments"] += 1
        STATS["gated_audio_s"] += len(x) / SR
        STATS["gated_spans"] += [(seg.start_ms + a * 10, seg.start_ms + b * 10) for a, b in spans]
        pieces = plan(ctx, x, act, spans)
        if pieces is None:
            STATS["fallback_segments"] += 1
            out.append(today(seg, x, ctx, done, text_of))
            continue
        STATS["used_segments"] += 1
        STATS["sep_spans"] += [(seg.start_ms + a * 10, seg.start_ms + b * 10) for a, b in spans]
        earlier = text_of.get(seg.continues_id) if seg.continues_id is not None else None
        texts = []
        for t, a, b, y, ms in pieces:
            start = int(seg.start_ms + a * 10)  # plain ints: sqlite stores numpy ints as blobs
            end = int(min(seg.end_ms, seg.start_ms + b * 10))
            first = a == 0 and t == 0
            res = ctx.decode(y, previous_transcript(done["system"][-50:], start), earlier if first else None)
            STATS["whisper_calls"] += 1
            STATS["pieces"] += 1
            row = Transcript("system", start, max(end, start + 1), res.text if res else "", res.language if res else None, seg.sources, speech_ms=ms)
            if res:
                texts.append(res.text)
                done["system"].append((end, start, res.text))
                row.speaker_id, row.turns = ctx.identify(y)
            out.append(row)
        text_of[seg.id] = " ".join(texts) or None
    return out
