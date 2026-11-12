"""Hand probes (not scored): phrasings outside phrases.jsonl, printed for inspection."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import harness  # noqa: E402

mod = harness.load(HERE / "candidate.py", "c3fix")
now = datetime(2026, 11, 10, 14, 0, tzinfo=ZoneInfo("Europe/Paris"))  # Tuesday
PROBES = [
    "Samedi, j'ai regardé quoi vers 22h ?",
    "J'ai regardé Le Journal de 20h hier ?",
    "Qu'est-ce que j'ai regardé pendant la pause de midi ?",
    "Les 2 vidéos de ce matin, c'était quoi ?",
    "Qu'est-ce que Léa a dit sur les 2 vidéos de ce matin ?",
    "What did I watch over lunch?",
    "The game I played over the weekend, did Sacha mention it yesterday?",
    "Did I watch the video from last night that Minlo sent?",
    "Qu'est-ce que Léa a dit hier soir, quand on jouait à Valorant ce matin ?",
    "Hier, vers 22h, qu'est-ce que j'ai regardé ?",
    'What did Minlo say about "Monday morning" yesterday?',
    "C'était quoi la vidéo d'hier soir dont Minlo parlait ce matin ?",
    "Which video from this morning did Léa mention last night?",
    "Le podcast d'hier, Minlo l'a cité ce matin, c'était lequel ?",
    "The clip Sacha sent yesterday, what was it called?",
    "Ce soir, Minlo a parlé de la vidéo d'hier, laquelle ?",
    "Qu'est-ce que j'ai regardé sur la chaîne de Léa hier soir ?",
    "What did Minlo say about the Late Show from yesterday?",
    "Hier soir j'ai regardé Le Grand Journal du matin, c'était sur quoi ?",
    "What video did I watch during the afternoon yesterday?",
    "Over the weekend, what did Léa say?",
    "Qu'est-ce que j'ai regardé hier, et Minlo ce matin ?",
]
for q in PROBES:
    w = mod.resolve_question(q, now, None)
    print(f"{q!r}\n   -> {None if w is None else (w[0].strftime('%a %d %H:%M'), w[1].strftime('%a %d %H:%M'))}")
