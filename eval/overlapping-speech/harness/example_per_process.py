"""Example candidate (API check, not a proposal): oracle per-process split of each stored system
segment. For every stored system segment, each simulated process stream sounding in it is
transcribed on its own (same decode as production) and attributed with the speakers job; excluded
processes are dropped; music-only processes are skipped as capture does. Mic segments: baseline.

    uv run python eval/overlapping-speech/harness/run.py eval/overlapping-speech/harness/example_per_process.py <out>
"""

from __future__ import annotations

from akashic.capture.audio import MUSIC_PROCESSES

import core

SKIP = MUSIC_PROCESSES | core.EXCLUDED | {"qualitygame.exe"}


def process(segments, ctx):
    out = []
    for seg in segments:
        x = ctx.audio(seg)
        if seg.stream == "mic":
            res = ctx.decode(x)
            t = core.Transcript("mic", seg.start_ms, seg.end_ms, res.text if res else "", res.language if res else None, seg.sources, speech_ms=seg.speech_ms)
            if res:
                me = ctx.self_id()
                t.speaker_id, t.turns = me, [(me, 0, seg.end_ms - seg.start_ms)]
            out.append(t)
            continue
        end = seg.start_ms + len(x) * 1000 // core.SR
        for exe in ctx.processes(seg.start_ms, end):
            if exe in SKIP:
                continue
            y = ctx.process_stream(exe, seg.start_ms, end)
            if not y.any():
                continue
            res = ctx.decode(y)
            t = core.Transcript("system", seg.start_ms, seg.end_ms, res.text if res else "", res.language if res else None, (exe,), speech_ms=seg.speech_ms)
            if res:
                t.speaker_id, t.turns = ctx.identify(y)
            out.append(t)
    return out
