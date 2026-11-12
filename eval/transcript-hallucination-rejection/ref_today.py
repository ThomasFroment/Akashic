"""Reference: what the app does today (pipeline/transcribe.py:209-211, search/hybrid.py:436-437).

Reject when Whisper returned no text or the whole normalised text is a known hallucination;
"unclear" when confidence < UNCLEAR_BELOW; otherwise keep the text unchanged.
"""


def judge(clip_cache: dict, helpers) -> dict:  # noqa: ANN001
    text = clip_cache["text"]
    repo = helpers.repo
    if not text or repo.is_hallucination(text):
        return {"status": "reject", "text": "", "extra_decodes": 0, "extra_ms": 0.0}
    status = "unclear" if repo.is_unclear(clip_cache["confidence"]) else "keep"
    return {"status": status, "text": text, "extra_decodes": 0, "extra_ms": 0.0}
