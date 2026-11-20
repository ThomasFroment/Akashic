"""Shared pieces of the overlapping-speech harness: run loading, re-rendered sources, the candidate
context, the scoring adapter.

A candidate is a module with

    process(segments, ctx) -> list[Transcript]

`segments` are the stored audio segments of one run (or of the synthetic set), sorted by start:
`Segment(id, stream, start_ms, end_ms, speech_ms, sources, continues_id)`. `ctx` is a `Ctx`:

- ctx.audio(seg)                   decoded 16 kHz float32 of a stored segment (what Whisper got)
- ctx.transcribe(samples, prompt)  faster-whisper large-v3 as Akashic loads it (float16, beam 5,
                                   FR/EN choice); GPU time is accounted
- ctx.decode(samples, previous, overlap)  pipeline/transcribe.decode_segment (loudness, prompt,
                                   continuation trim, hallucination filter) over ctx.transcribe
- ctx.voices                       pipeline/speakers.OnnxVoiceModels (pyannote seg 3.0 + WeSpeaker)
- ctx.identify(samples)            today's speakers job on samples: turns + embeddings + the
                                   production SpeakerStore (profiles persist across the run) ->
                                   (segment speaker_id or None, [(speaker_id, start_ms, end_ms)]) offsets in ms
- ctx.self_id()                    the user's speaker id (mic stream)
- ctx.processes(t0, t1)            names of the simulated per-process streams with sound in [t0, t1)
- ctx.process_stream(exe, t0, t1)  SIMULATED process-loopback audio of one process over [t0, t1)
                                   (wall ms), re-rendered from the harness's own player logs; marks
                                   the candidate as using simulated capture
- ctx.mic_stream(t0, t1)           the microphone stream re-rendered the same way
- ctx.segment(samples, t0_ms)      Akashic's capture Segmenter + Silero VAD over a stream -> list of
                                   (start_ms, end_ms, samples, speech_ms, continues)
- ctx.capture_cpu()                context manager: CPU seconds inside count as capture-side cost

A `Transcript` is one stored row: stream, start_ms, end_ms, text ('' = Whisper heard nothing),
language, sources (exe names), speaker_id (None = unknown/mixed), turns [(speaker_id, start_ms,
end_ms) offsets from start_ms], speech_ms. The scoring adapter writes them into an in-memory
SQLite with the store's column names and runs the unchanged score.m7 / score.m8_m9.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import itertools
import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "eval" / "quality"))
sys.path.insert(0, str(REPO / "src"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import assets  # noqa: E402
import score  # noqa: E402
from common import LOCAL, RESULTS  # noqa: E402

SR = 16_000
RUNS = ("20261119-standard-s21-target-1818", "20261119-day-s31-target-0547")
SHORT = {RUNS[0]: "std", RUNS[1]: "day"}
MODELS = LOCAL / "models-home" / "models"
APP_EXE = {"chrome": "chrome.exe", "edge": "msedge.exe", "zen": "zen.exe", "assistant": "msedge.exe"}
LABEL_EXE = {"call": "discord.exe", "music": "spotify.exe", "game": "qualitygame.exe", "own": "mic.exe", "elsewhere": "discord.exe"}
EXCLUDED = {"qualityvault.exe"}  # the harness's Akashic config excludes it
NOT_RECORDED = {"own", "elsewhere"}  # played on CABLE Input (the mic) and on a third endpoint


@dataclass(frozen=True)
class Segment:
    id: int
    stream: str
    start_ms: int
    end_ms: int
    speech_ms: int
    sources: tuple[str, ...]
    continues_id: int | None = None


@dataclass
class Transcript:
    stream: str
    start_ms: int
    end_ms: int
    text: str
    language: str | None = None
    sources: tuple[str, ...] = ()
    speaker_id: int | None = None
    turns: list = field(default_factory=list)  # (speaker_id, start_ms, end_ms) offsets from start_ms
    speech_ms: int = 0


# --- sources: re-rendering what each harness process played -------------------------------------


@functools.cache
def bank(name: str) -> np.ndarray:
    return assets.load_bank(name)


@functools.cache
def video_audio(path: str) -> np.ndarray:
    return assets.decode(Path(path))


class Track:
    """What one source played: items (wall start ms, samples, gain), sorted by start."""

    def __init__(self, label: str, exe: str, endpoint: str) -> None:
        self.label, self.exe, self.endpoint = label, exe, endpoint
        self.items: list[tuple[int, np.ndarray, float]] = []

    def finish(self) -> None:
        self.items.sort(key=lambda x: x[0])
        self.starts = np.array([a for a, _, _ in self.items], dtype=np.int64)
        self.ends = np.array([a + len(x) * 1000 // SR for a, x, _ in self.items], dtype=np.int64)
        self.maxlen = max((len(x) * 1000 // SR for _, x, _ in self.items), default=0)

    def render(self, t0: int, t1: int, out: np.ndarray | None = None, scale: float = 1.0) -> np.ndarray:
        n = (t1 - t0) * SR // 1000
        out = np.zeros(n, np.float32) if out is None else out
        if not self.items:
            return out
        lo = np.searchsorted(self.starts, t0 - self.maxlen - 1)
        hi = np.searchsorted(self.starts, t1)
        for i in range(lo, hi):
            a, x, g = self.items[i]
            if self.ends[i] <= t0:
                continue
            s = round((a - t0) * SR / 1000)  # sample offset of the item in out
            i0, i1 = max(0, -s), min(len(x), n - s)
            if i1 > i0:
                out[s + i0:s + i1] += x[i0:i1] * np.float32(g * scale)
        return out

    def active(self, t0: int, t1: int) -> bool:
        return bool(np.any((self.starts < t1) & (self.ends > t0)))


class Run:
    def __init__(self, run_id: str) -> None:
        self.id = run_id
        self.dir = RESULTS / run_id
        self.local = LOCAL / "runs" / run_id
        self.truth = score.Truth(self.dir)
        self.paths, _key, self.conn, self.media = score.open_store(run_id)
        rows = self.conn.execute("SELECT id, stream, start_ms, end_ms, speech_ms, sources, continues_id, audio_path, transcript, speaker_id, language "
                                 "FROM audio_segments ORDER BY start_ms, id").fetchall()
        self.segments = [Segment(r[0], r[1], r[2], r[3], r[4], tuple(json.loads(r[5])), r[6]) for r in rows]
        self.audio_path = {r[0]: r[7] for r in rows}
        self.stored = {r[0]: {"transcript": r[8] or "", "speaker_id": r[9], "language": r[10]} for r in rows}
        self._tracks: dict[str, Track] | None = None
        self.calib: dict | None = None

    def audio(self, seg: Segment) -> np.ndarray:
        from akashic.storage.opus import decode_opus

        return decode_opus(self.media.read(self.audio_path[seg.id]))

    # tracks ---------------------------------------------------------------------------------

    def tracks(self) -> dict[str, Track]:
        if self._tracks is None:
            self._tracks = self._build_tracks()
        return self._tracks

    def _build_tracks(self) -> dict[str, Track]:
        tr = self.truth
        items = {}
        for sched in self.local.glob("schedule-*.json"):
            for it in json.loads(sched.read_text(encoding="utf-8"))["items"]:
                items[it["id"]] = it
        out: dict[str, Track] = {}
        self.video_clip_exe: dict[int, str] = {}
        for label, log in tr.players.items():
            exe = "qualityvault.exe" if label.startswith("canary") else LABEL_EXE.get(label, label + ".exe")
            endpoint = "cable" if label == "own" else ("third" if label == "elsewhere" else "default")
            t = out.setdefault(label, Track(label, exe, endpoint))
            for e in log:
                it = items.get(e.get("id"))
                if e.get("type") != "start" or not it:
                    continue
                x = bank(it["bank"])[it["off"]:it["off"] + it["n"]]
                t.items.append((e["start_wall_ms"], x, it.get("gain", 1.0)))
        # videos: the mp4 the browser played, placed at every play stretch (score.video_utterances)
        built = {e["video"]: e for e in tr.pages if e.get("type") == "video_built"}
        for vid in built:
            evs = sorted((e for e in tr.pages if e.get("type") == "media" and e["s"] is not None and e.get("ev") in ("play", "pause", "ended", "tick")
                          and tr.pages_video(e) == vid), key=lambda e: e["wall_ms"])
            mp4 = self.local / "media" / f"{vid}.mp4"
            if not evs or not mp4.is_file():
                continue
            audio = video_audio(str(mp4))
            playing, last_tick, stretches = None, None, []
            for e in evs:
                if e["ev"] == "play":
                    playing = (e["wall_ms"], e["t"], e["w"])
                elif e["ev"] == "tick":
                    last_tick = e
                elif playing:
                    stretches.append((*playing, e["wall_ms"]))
                    playing = None
            if playing and last_tick and last_tick["wall_ms"] > playing[0]:
                stretches.append((*playing, last_tick["wall_ms"]))
            for a_wall, a_media, w, b_wall in stretches:
                app = tr.windows.get(w.split("#")[0], {}).get("app")
                exe = APP_EXE.get(app, f"{app}.exe")
                off = a_wall - a_media * 1000
                for c in built[vid].get("plan", []):
                    self.video_clip_exe[round(off + c["t_ms"])] = exe
                t = out.setdefault(f"video:{exe}", Track(f"video:{exe}", exe, "default"))
                i0 = round(a_media * SR)
                t.items.append((a_wall, audio[i0:i0 + (b_wall - a_wall) * SR // 1000], 1.0))
        for t in out.values():
            t.finish()
        return out

    def utterance_proc(self, u: dict) -> str:
        """The process that played a truth utterance (exe without .exe), or 'elsewhere'/'mic'."""
        if u["source"] == "video":
            self.tracks()
            t0 = u["t0"]
            exe = next((self.video_clip_exe[k] for k in range(t0 - 2, t0 + 3) if k in self.video_clip_exe), "video.exe")
            return exe.removesuffix(".exe")
        return {"call": "discord", "own": "mic", "elsewhere": "elsewhere"}.get(u["source"], u["source"])

    def recorded_tracks(self, stream: str = "system") -> list[Track]:
        want = "cable" if stream == "mic" else "default"
        return [t for t in self.tracks().values() if t.endpoint == want]

    # simulated per-process capture -------------------------------------------------------------

    def processes(self, t0: int, t1: int) -> list[str]:
        return sorted({t.exe for t in self.recorded_tracks() if t.active(t0 - self.lag(t.label, t0), t1 - self.lag(t.label, t0))})

    def process_stream(self, exe: str, t0: int, t1: int) -> np.ndarray:
        """What process loopback of `exe` on the recorded endpoint would have heard over [t0, t1)."""
        out = np.zeros((t1 - t0) * SR // 1000, np.float32)
        for t in self.recorded_tracks():
            if t.exe == exe:
                lag = self.lag(t.label, t0)
                t.render(t0 - lag, t1 - lag, out, self.gain(t.label))
        return out

    def mic_stream(self, t0: int, t1: int) -> np.ndarray:
        out = np.zeros((t1 - t0) * SR // 1000, np.float32)
        for t in self.recorded_tracks("mic"):
            lag = self.lag(t.label, t0)
            t.render(t0 - lag, t1 - lag, out, self.gain(t.label))
        return out

    # calibration of the re-render against the stored audio -------------------------------------

    def calibration(self) -> dict:
        """Per stored segment and active source: clock lag (stored(t) ~ render(t - lag)) and gain; the
        share of stored band energy the re-render explains. Cached in harness/cache/."""
        if self.calib is None:
            path = HERE / "cache" / f"calib-{self.id}.json"
            if path.is_file():
                self.calib = json.loads(path.read_text(encoding="utf-8"))
            else:
                self.calib = calibrate(self)
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps(self.calib), encoding="utf-8")
        return self.calib

    def lag(self, label: str, t: int | None = None) -> int:
        """Lag of a source near wall time t: median of the segments within LOCAL_LAG_MS where it was
        the loudest source (the day run drifts by tens of ms), else its run median. A source never
        loudest (the game bed) takes the lag of the other player.py sources: same waveOut path."""
        c = self.calibration()
        if not hasattr(self, "_local"):
            per = defaultdict(list)
            for x in c["segments"]:
                if x["explained"] is not None and x["explained"] > 0.5 and x["levels"]:
                    k = int(np.argmax(x["levels"]))
                    per[x["labels"][k]].append((x["t"], x["lags"][k]))
            self._local = {k: (np.array([a for a, _ in v]), np.array([b for _, b in v])) for k, v in per.items()}
        lags = c["lag"]
        if label not in lags:
            same_path = [k for k in lags if not k.startswith("video:") and k != "own"]
            return round(float(np.median([self.lag(k, t) for k in same_path]))) if same_path else 0
        if t is not None and label in self._local:
            ts, vs = self._local[label]
            near = np.abs(ts - t) <= LOCAL_LAG_MS
            if near.sum() >= 3:
                return round(float(np.median(vs[near])))
        return round(lags[label])

    def gain(self, label: str) -> float:
        return self.calibration()["gain"].get(label, 1.0)


LAG_SEARCH_MS = 1500
LOCAL_LAG_MS = 900_000
NBANDS = 24


def band_power(x: np.ndarray) -> np.ndarray:
    """(bands, frames) power on 32 ms frames, log-spaced bands 100 Hz - 8 kHz."""
    from scipy.signal import stft

    _, _, z = stft(x, SR, nperseg=512, noverlap=256)
    p = np.abs(z) ** 2
    edges = np.unique(np.geomspace(3, 257, NBANDS + 1).astype(int))
    return np.stack([p[a:b].sum(0) for a, b in itertools.pairwise(edges)])


def calibrate(run: Run) -> dict:
    """Per segment: lag of each active source by waveform cross-correlation against the stored audio,
    then gains by non-negative least squares on band powers (the loopback path jitters by a fraction
    of a millisecond, so waveform least squares underestimates gains)."""
    from numpy.fft import irfft, rfft
    from scipy.optimize import nnls

    out = []
    pad = LAG_SEARCH_MS
    p = pad * SR // 1000
    for seg in run.segments:
        x = run.audio(seg).astype(np.float64)
        t0, t1 = seg.start_ms, seg.start_ms + len(x) * 1000 // SR
        act = [t for t in run.recorded_tracks(seg.stream) if t.active(t0 - pad, t1 + pad)]
        rec = {"id": seg.id, "stream": seg.stream, "t": t0, "labels": [], "lags": [], "gains": [], "levels": [], "explained": None}
        out.append(rec)
        if not act:
            continue
        nfft = 1 << (len(x) + len(x) + 2 * p - 1).bit_length()
        fx = np.conj(rfft(x, nfft))
        cols = []
        for t in act:
            w = t.render(t0 - pad, t1 + pad).astype(np.float64)
            if not w.any():
                continue
            cc = irfft(rfft(w, nfft) * fx, nfft)[:2 * p + 1]
            k = int(np.argmax(cc))
            rec["labels"].append(t.label)
            rec["lags"].append((p - k) * 1000 / SR)
            cols.append(w[k:k + len(x)])
        if not cols:
            continue
        px = band_power(x)
        pk = [band_power(c) for c in cols]
        m = min(px.shape[1], *(q.shape[1] for q in pk))
        a = np.stack([q[:, :m].ravel() for q in pk], axis=1)
        g2, _ = nnls(a, px[:, :m].ravel())
        res = px[:, :m].ravel() - a @ g2
        rec["gains"] = [float(np.sqrt(v)) for v in g2]
        rec["levels"] = [float(np.sqrt(np.mean(c ** 2))) for c in cols]
        rec["explained"] = 1 - float(np.sum(np.abs(res)) / max(np.sum(px[:, :m]), 1e-12))
    gains, lags = defaultdict(list), defaultdict(list)
    for s in out:
        if s["explained"] is not None and s["explained"] > 0.5:
            for lab, lg, gv, lev in zip(s["labels"], s["lags"], s["gains"], s["levels"], strict=True):
                if lev > 1e-3 and gv > 0:
                    gains[lab].append(gv)
                    if len(s["labels"]) == 1 or lev == max(s["levels"]):
                        lags[lab].append(lg)
    return {"segments": out, "gain": {k: float(np.median(v)) for k, v in gains.items()},
            "lag": {k: float(np.median(v)) for k, v in lags.items()},
            "lag_mad": {k: float(np.median(np.abs(np.array(v) - np.median(v)))) for k, v in lags.items()},
            "n": {k: len(v) for k, v in gains.items()}}


# --- the candidate context ---------------------------------------------------------------------


class Ctx:
    def __init__(self, source, transcriber, voices, vocabulary: tuple[str, ...] = ()) -> None:
        self.source = source  # a Run or a synthetic.Set (same audio/process_stream interface)
        self.transcriber = transcriber
        self.voices = voices
        self.vocabulary = vocabulary
        self.gpu_s = 0.0
        self.cpu_capture_s = 0.0
        self.cpu_speakers_s = 0.0
        self.used_sim = False
        self.audio_s_decoded = 0.0
        self._spk = speaker_db()
        from akashic.storage.speakers import SpeakerStore

        self.speakers = SpeakerStore(self._spk)
        self._vad = None

    # audio
    def audio(self, seg: Segment) -> np.ndarray:
        return self.source.audio(seg)

    def processes(self, t0: int, t1: int) -> list[str]:
        self.used_sim = True
        return self.source.processes(t0, t1)

    def process_stream(self, exe: str, t0: int, t1: int) -> np.ndarray:
        self.used_sim = True
        return self.source.process_stream(exe, t0, t1)

    def mic_stream(self, t0: int, t1: int) -> np.ndarray:
        self.used_sim = True
        return self.source.mic_stream(t0, t1)

    # whisper
    def transcribe(self, samples: np.ndarray, prompt: str | None):
        t = time.perf_counter()
        try:
            return self.transcriber.transcribe(samples, prompt)
        finally:
            self.gpu_s += time.perf_counter() - t
            self.audio_s_decoded += len(samples) / SR

    def decode(self, samples: np.ndarray, previous: str | None = None, overlap: str | None = None):
        from akashic.pipeline.transcribe import decode_segment

        return decode_segment(self, samples, self.vocabulary, previous, overlap)

    # speakers
    def identify(self, samples: np.ndarray):
        from akashic.storage.speakers import MIN_IDENTIFY_MS, NewTurn

        c = time.process_time()
        turns = [NewTurn(t.start_ms, t.end_ms, t.speech_ms, self.voices.embed(t.samples) if t.speech_ms >= MIN_IDENTIFY_MS else None)
                 for t in self.voices.turns(samples)]
        sid = self._spk.execute("INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms) VALUES ('system', 0, 0, 0)").lastrowid
        self.speakers.record_turns(sid, turns, 0)
        rows = self._spk.execute("SELECT speaker_id, start_ms, end_ms FROM speaker_turns WHERE segment_id = ? ORDER BY id", (sid,)).fetchall()
        spk = self._spk.execute("SELECT speaker_id FROM audio_segments WHERE id = ?", (sid,)).fetchone()[0]
        self.cpu_speakers_s += time.process_time() - c
        return spk, [tuple(r) for r in rows]

    def self_id(self) -> int:
        with __import__("akashic.storage.db", fromlist=["transaction"]).transaction(self._spk) as conn:
            return self.speakers._self_id(conn, 0)

    # capture-side simulation
    def segment(self, samples: np.ndarray, t0_ms: int):
        from akashic.capture.audio import AudioSettings, Segmenter
        from akashic.capture.vad import WINDOW, SileroVad

        with self.capture_cpu():
            if self._vad is None:
                self._vad = SileroVad()
            self._vad.reset()
            seg = Segmenter(AudioSettings())
            out = []
            for i in range(0, len(samples) - WINDOW + 1, WINDOW):
                w = samples[i:i + WINDOW]
                p = self._vad.probability(w) if w.any() else 0.0
                out += seg.feed(t0_ms + i * 1000 // SR, w, p)
            out += seg.finish()
        return [(u.start_ms, u.end_ms, u.samples, u.speech_ms, u.continues) for u in out]

    @contextlib.contextmanager
    def gpu(self):
        """Wall time inside counts as GPU work (wrap any extra GPU model; synchronize before leaving)."""
        t = time.perf_counter()
        try:
            yield
        finally:
            self.gpu_s += time.perf_counter() - t

    @contextlib.contextmanager
    def capture_cpu(self):
        c = time.process_time()
        try:
            yield
        finally:
            self.cpu_capture_s += time.process_time() - c


def speaker_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.executescript(SCHEMA)
    return conn


SCHEMA = """
CREATE TABLE audio_segments (id INTEGER PRIMARY KEY, stream TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
  speech_ms INTEGER NOT NULL, sources TEXT NOT NULL DEFAULT '[]', transcript TEXT, speaker_id INTEGER, language TEXT);
CREATE TABLE speakers (id INTEGER PRIMARY KEY, name TEXT, is_self INTEGER NOT NULL DEFAULT 0, vec_sum BLOB,
  vec_n INTEGER NOT NULL DEFAULT 0, created_ms INTEGER NOT NULL);
CREATE TABLE speaker_turns (id INTEGER PRIMARY KEY, segment_id INTEGER NOT NULL, speaker_id INTEGER, start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL, speech_ms INTEGER NOT NULL, embedding BLOB, in_profile INTEGER NOT NULL DEFAULT 0);
"""


def scoring_db(transcripts: list[Transcript]) -> sqlite3.Connection:
    """The candidate's rows in the store's column names: what score.m7 / m8_m9 read."""
    conn = speaker_db()
    for t in sorted(transcripts, key=lambda t: (t.start_ms, t.stream)):
        sid = conn.execute("INSERT INTO audio_segments (stream, start_ms, end_ms, speech_ms, sources, transcript, speaker_id, language) VALUES (?,?,?,?,?,?,?,?)",
                           (t.stream, t.start_ms, t.end_ms, t.speech_ms, json.dumps(sorted(t.sources)), t.text or None, t.speaker_id, t.language)).lastrowid
        for spk, a, b in t.turns:
            conn.execute("INSERT INTO speaker_turns (segment_id, speaker_id, start_ms, end_ms, speech_ms) VALUES (?,?,?,?,0)", (sid, spk, a, b))
    return conn


# --- models ------------------------------------------------------------------------------------


def load_models():
    from akashic.pipeline.speakers import OnnxVoiceModels, model_paths
    from akashic.pipeline.whisper import WhisperTranscriber

    tr = WhisperTranscriber(MODELS / "faster-whisper-large-v3", idle_unload_s=1e9)
    seg, emb = model_paths(MODELS)
    return tr, OnnxVoiceModels(seg, emb, idle_unload_s=1e9)


def load_candidate(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- VRAM sampling -----------------------------------------------------------------------------


class VramPeak:
    """Peak GPU memory taken while inside, sampled every 200 ms with nvidia-smi (the driver's tool).
    Windows (WDDM) reports no per-process figure, so it is the device's used memory minus what was
    in use on entry: run it with nothing else loading the GPU (the worker and llama-server stopped).
    Models must load inside (Whisper loads on first use)."""

    def __init__(self) -> None:
        self.peak_mib = 0
        self.start_mib = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def used_mib() -> int:
        import subprocess

        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=0x08000000).stdout
        return int(out.split()[0])

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak_mib = max(self.peak_mib, self.used_mib() - self.start_mib)
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(0.2)

    def __enter__(self):
        self.start_mib = self.used_mib()
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join()
