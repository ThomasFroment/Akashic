"""Per segment of a kept smoke store: detected vs truth language, and WER of French single-language segments.

    uv run python eval/quality/spikes/segment_language_check.py <run-id> [<run-id> ...]

Numbers and language labels only."""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path("eval/quality").resolve()))
import score  # noqa: E402

for rid in sys.argv[1:]:
    tr = score.Truth(score.RESULTS / rid)
    _, _, conn, _ = score.open_store(rid)
    utts = tr.utterances()
    rows = conn.execute("SELECT stream, start_ms, end_ms, transcript, language FROM audio_segments ORDER BY start_ms").fetchall()
    agree = Counter()
    fr_detail = []
    for stream, a, b, text, lang in rows:
        over = [u for u in utts if u["stream"] == stream and u["t0"] < b and u["t1"] > a]
        speech = [u for u in over if u.get("kind") != "tts"]
        langs = {u["lang"] for u in speech}
        dur = round((b - a) / 1000, 1)
        if len(langs) == 1 and len(speech) == len(over):
            truth = langs.pop()
            agree[(truth, lang)] += 1
            if truth == "fr":
                ref = score.fold(" ".join(u["text"] for u in speech))
                hyp = score.fold(text or "")
                fr_detail.append((dur, lang, round(score.lev(ref.split(), hyp.split()) / max(1, len(ref.split())), 2), len(speech)))
        else:
            agree[("multi/tts", lang)] += 1
    print(rid, "truth->detected:", dict(agree))
    print("  FR single-language segments (dur s, detected, WER, utterances):", fr_detail)
