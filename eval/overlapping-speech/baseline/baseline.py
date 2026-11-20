"""Baseline: today's pipeline replayed on the stored segments.

transcribe job (pipeline/transcribe.TranscribeHandler.run): decode_segment with the previous
transcript of the same stream within 60 s as prompt context and the continued segment's transcript
for the overlap trim; an empty result stores no text and queues no speakers job. speakers job
(pipeline/speakers.SpeakersHandler.run): mic -> the user; system -> pyannote turns + WeSpeaker
embeddings -> SpeakerStore.record_turns (one author only when every turn names the same voice).
"""

from __future__ import annotations

from akashic.pipeline.transcribe import PREVIOUS_WITHIN_MS

from core import Transcript


def previous_transcript(done: list[tuple[int, int, str]], before_ms: int) -> str | None:
    """AudioStore.previous_transcript over the transcripts made so far."""
    best = None
    for end, start, text in done:
        if before_ms - PREVIOUS_WITHIN_MS <= end <= before_ms and start < before_ms and (best is None or end > best[0]):
            best = (end, text)
    return best[1] if best else None


def process(segments, ctx):
    out = []
    text_of: dict[int, str | None] = {}
    done: dict[str, list] = {"mic": [], "system": []}
    for seg in segments:
        samples = ctx.audio(seg)
        earlier = text_of.get(seg.continues_id) if seg.continues_id is not None else None
        res = ctx.decode(samples, previous_transcript(done[seg.stream][-50:], seg.start_ms), earlier)
        text_of[seg.id] = res.text if res else None
        t = Transcript(seg.stream, seg.start_ms, seg.end_ms, res.text if res else "", res.language if res else None, seg.sources, speech_ms=seg.speech_ms)
        if res:
            done[seg.stream].append((seg.end_ms, seg.start_ms, res.text))
            if seg.stream == "mic":
                me = ctx.self_id()
                t.speaker_id, t.turns = me, [(me, 0, seg.end_ms - seg.start_ms)]
            else:
                t.speaker_id, t.turns = ctx.identify(samples)
        out.append(t)
    return out
