"""Harness self-test, not a candidate: keep each distinct line of a window once (exact match), linked
to its first frame, spanning first..last sighting; one FTS row per window and title. Checks that the
metrics move the expected way (M3.2 -> 0, drop ~0, size down, crowding down)."""

from __future__ import annotations

from shared import KeptLine, StoredText


def process(history, ctx):
    first: dict[str, KeptLine] = {}
    for o in history:
        doc = ("win", ctx.hwnd, o.title)
        for ln in o.text.split("\n"):
            s = ln.strip()
            if not s:
                continue
            kl = first.get(s)
            if kl is None:
                first[s] = KeptLine(s, o.frame_id, o.ts_ms, o.ts_ms, doc)
            else:
                kl.last_ts = o.ts_ms
    return StoredText(list(first.values()))


def step(state, obs, ctx):
    """Write-path self-test: one keyed hash per stored text (like #101's HMAC)."""
    import hashlib
    import hmac

    seen = state or set()
    seen.add(hmac.new(b"k" * 32, obs.text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest())
    return seen
