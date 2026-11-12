"""Offline round-trip check of the R2rules variant table (dev only, no model, no network).

Each (variant, canonical) pair is substituted into 20 FR/EN carrier questions and resolved at
5 "now" times (Tue 2026-11-10 09:00, 14:00, 23:30, 00:40, 03:00): the variant passes when it gives
the same window as its canonical form in all 100 cases and the canonical form gives a window in
at least one. Sources: hand-written informal / SMS / apocope forms (FR and EN, written at dev
time from general knowledge of chat usage; the local LLM was not used because the GPU was full),
and mechanical variants (hyphen / space / apostrophe / accent spellings). The table is static:
nothing is learned at runtime.

Also runs FALSE_FOLDS: questions where a folded or abbreviated form must NOT create a window.

    python verify_variants.py          (env CTW_GAZ is irrelevant here; it runs with "off")
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ["CTW_GAZ"] = "off"
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("r2cand", HERE / "candidate.py")
cand = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cand)

TZ = ZoneInfo("Europe/Paris")
NOWS = [datetime(2026, 11, 10, h, m, tzinfo=TZ) for h, m in ((9, 0), (14, 0), (23, 30), (0, 40), (3, 0))]
CARRIERS = [
    "{T}, j'ai regardé quoi ?",
    "Qu'est-ce que Minlo a dit {T} ?",
    "J'ai joué à quoi {T} avec Sacha ?",
    "{T} Léa m'a parlé d'un jeu, c'était lequel ?",
    "C'était quoi la vidéo de {T} ?",
    "Le jeu dont Sacha parlait, on y a joué {T} ?",
    "Qui m'a appelé {T} sur Discord ?",
    "{T}, on parlait de quoi avec Minlo ?",
    "Quelle musique j'écoutais {T} ?",
    "Léa a dit quoi {T} quand on jouait ?",
    "What did I watch {T}?",
    "{T}, who pinged me on Discord?",
    "Which game did we play {T}?",
    "What was the video from {T} about?",
    "Did Sacha mention the patch {T}?",
    "{T} Minlo told me about a game, which one?",
    "What song was playing {T} when Léa called?",
    "Who was in the voice chat {T}?",
    "The clip Minlo sent, did I watch it {T}?",
    "What did Léa say {T} about the trailer?",
]

# (variant, canonical, kind)
VARIANTS = [
    # day skeletons / spellings
    ("ajd", "aujourd'hui", "skeleton"), ("Ajd", "Aujourd'hui", "skeleton"),
    ("auj", "aujourd'hui", "skeleton"), ("aujd", "aujourd'hui", "skeleton"),
    ("ojd", "aujourd'hui", "skeleton"), ("aujourdhui", "aujourd'hui", "mechanical"),
    ("2day", "today", "sms"), ("so far today", "today", "phrase"),
    ("yday", "yesterday", "skeleton"), ("ystd", "yesterday", "skeleton"),
    ("yest", "yesterday", "skeleton"), ("ystrdy", "yesterday", "skeleton"),
    ("avanthier", "avant-hier", "mechanical"), ("avant hier", "avant-hier", "mechanical"),
    # head words
    ("this arvo", "this afternoon", "informal"), ("yesterday arvo", "yesterday afternoon", "informal"),
    ("around lunch time", "around lunchtime", "mechanical"),
    ("around lunch-time", "around lunchtime", "mechanical"),
    ("this mornin'", "this morning", "informal"), ("tonite", "tonight", "sms"),
    ("2nite", "tonight", "sms"), ("2night", "tonight", "sms"),
    ("cet aprèm", "cet après-midi", "informal"), ("cet aprem", "cet après-midi", "informal"),
    ("cette aprem", "cet après-midi", "informal"), ("dans l'aprem", "dans l'après-midi", "informal"),
    ("hier aprèm", "hier après-midi", "informal"), ("hier apres midi", "hier après-midi", "mechanical"),
    ("pendant le déj", "pendant le déjeuner", "informal"), ("à la pause déj", "pendant le déjeuner", "informal"),
    ("vers le déj", "pendant le déjeuner", "governor"),
    ("around lunch", "at lunch", "governor"), ("around lunchtime", "at lunchtime", "governor"),
    # apocope folding (part-of-day heads)
    ("ce mat'", "ce matin", "apocope"), ("ce mat", "ce matin", "apocope"),
    ("Ce mat'", "Ce matin", "apocope"), ("ce mati", "ce matin", "apocope"),
    ("hier mat'", "hier matin", "apocope"), ("hier mat", "hier matin", "apocope"),
    ("lundi mat'", "lundi matin", "apocope"), ("dimanche mat", "dimanche matin", "apocope"),
    ("cette matinée", "ce matin", "spelling"), ("le mat'", "le matin", "apocope"),
    ("this morn", "this morning", "apocope"), ("yesterday morn", "yesterday morning", "apocope"),
    ("this aft", "this afternoon", "apocope"), ("yesterday aft", "yesterday afternoon", "apocope"),
    ("this evenin'", "this evening", "apocope"), ("hier soirée", "hier soir", "spelling"),
    # weekend initials / abbreviations
    ("ce WE", "ce week-end", "initials"), ("ce we", "ce week-end", "initials"),
    ("ce W-E", "ce week-end", "initials"), ("le WE dernier", "le week-end dernier", "initials"),
    ("this WE", "this weekend", "initials"), ("ce Week-end", "ce week-end", "case"),
    ("ce week end", "ce week-end", "mechanical"), ("ce weekend", "ce week-end", "mechanical"),
    ("last wknd", "last weekend", "sms"), ("over the wknd", "over the weekend", "sms"),
    ("this wknd", "this weekend", "sms"), ("ce wkend", "ce week-end", "sms"),
    ("this past weekend", "last weekend", "phrase"), ("le week-end passé", "le week-end dernier", "phrase"),
    # night / late modifier
    ("hier soir tard", "la nuit dernière", "modifier"), ("tard hier soir", "la nuit dernière", "modifier"),
    ("hier soir assez tard", "la nuit dernière", "modifier"),
    ("hier tard dans la soirée", "la nuit dernière", "modifier"),
    ("hier tard le soir", "la nuit dernière", "modifier"),
    ("late yesterday evening", "late last night", "modifier"),
    ("late last nite", "late last night", "sms"), ("last nite", "last night", "sms"),
    ("hier dans la nuit", "la nuit dernière", "phrase"),
    ("avant-hier dans la nuit", "la nuit d'avant-hier", "phrase"),
    # recent
    ("toute à l'heure", "tout à l'heure", "misspelling"), ("tt à l'heure", "tout à l'heure", "sms"),
    ("tout à lheure", "tout à l'heure", "mechanical"), ("tout-à-l'heure", "tout à l'heure", "mechanical"),
    ("y'a un moment", "il y a un moment", "informal"), ("ya un moment", "il y a un moment", "sms"),
    ("a bit ago", "a while ago", "informal"), ("a little bit ago", "a while ago", "informal"),
    ("earlier on", "earlier", "informal"),
    # ago
    ("y'a 2h", "il y a 2h", "informal"), ("y a 2 heures", "il y a 2 heures", "informal"),
    ("ya 3h", "il y a 3h", "sms"), ("il y a 20 mn", "il y a 20 minutes", "abbrev"),
    ("2 hrs ago", "2 hours ago", "abbrev"), ("y'a une demi-heure", "il y a une demi-heure", "informal"),
    ("a half hour ago", "half an hour ago", "phrase"), ("il y a une demi heure", "il y a une demi-heure", "mechanical"),
    # after midnight
    ("passé minuit", "après minuit", "phrase"), ("minuit passé", "après minuit", "phrase"),
    ("passed midnight", "past midnight", "misspelling"),
    # clock
    ("vers les 22h", "vers 22h", "governor"), ("at around 10pm", "at 10pm", "governor"),
    ("à 22:30", "à 22h30", "mechanical"), ("vers 10h du soir", "vers 22h", "half"),
    ("vers 1h du mat", "vers 1h", "half"), ("vers 1h du mat'", "vers 1h", "half"),
    ("3h du matin", "3h", "half"), ("vers 3h de la nuit", "vers 3h", "half"),
    ("à 4h de l'aprem", "à 16h", "half"), ("10 at night", "10pm", "half"),
    ("3 in the afternoon", "3pm", "half"), ("8 in the morning", "8am", "half"),
    ("around 11 in the evening", "around 11pm", "half"), ("sur le coup de 22h", "vers 22h", "governor"),
    # earlier + deictic
    ("earlier this week", "this week", "modifier"), ("plus tôt cette semaine", "cette semaine", "modifier"),
    ("earlier this evening", "this evening", "modifier"), ("plus tôt ce matin", "ce matin", "modifier"),
]

# Folded/abbreviated look-alikes that must give no window (or not the folded meaning).
FALSE_FOLDS = [
    "Qu'est-ce que Minlo a dit sur le jeu ?",
    "C'est quoi ce jeu dont Léa parlait ?",
    "Sacha a parlé de ce soi-disant leak ?",
    "Who was the mat guy in the video?",
    "Did Léa say we should play Valorant?",
    "What did we decide, we were talking about the patch?",
    "C'est quoi le mat de la vidéo ?",
    "What did Minlo say about the after party?",
    "C'était quoi le lun de miel dont Sacha parlait ?",
    "Qu'est-ce que Sacha a dit sur le ton de Léa ?",
    "Did Minlo mention the eve of the release?",
    "What was the arvo thing Léa mentioned?",
    "Did I watch the today show clip?",
    "C'était quoi l'épisode de wednesday que Léa a cité ?",
    "Did I finish wednesday?",
    "J'ai écouté quelle version de minuit chrétiens ?",
    "Sacha a revu la nuit américaine ?",
    "Who sang midnight city in the playlist?",
    "Sacha a parlé de la nuit blanche de Léa ?",
]


def main() -> int:
    ok, bad = [], []
    for var, canon, kind in VARIANTS:
        diffs, any_window = 0, False
        for c in CARRIERS:
            for now in NOWS:
                a = cand.resolve_question(c.format(T=var), now, None)
                b = cand.resolve_question(c.format(T=canon), now, None)
                any_window |= b is not None
                diffs += a != b
        (ok if diffs == 0 and any_window else bad).append((var, canon, kind, diffs, any_window))
    print(f"variants verified: {len(ok)}/{len(VARIANTS)}")
    for v in bad:
        print("  REJECTED", v)
    kinds: dict[str, int] = {}
    for v in ok:
        kinds[v[2]] = kinds.get(v[2], 0) + 1
    print("  by kind:", kinds)
    print(f"apocope table (generated from part heads): {len(cand.APOCOPE)} prefixes")
    ff = 0
    for q in FALSE_FOLDS:
        w = [cand.resolve_question(q, now, None) for now in NOWS]
        if any(x is not None for x in w):
            ff += 1
            print("  FALSE FOLD", q, w[0])
    print(f"false folds: {ff}/{len(FALSE_FOLDS)}")
    return 1 if bad or ff else 0


if __name__ == "__main__":
    sys.exit(main())
