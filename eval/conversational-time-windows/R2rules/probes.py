"""Iteration-2 probes for R2rules (dev only): new questions written by the candidate's author.

They share the author's style, so they only catch regressions and over-reach (false windows,
refuter cases); they are not an accuracy estimate. Labels use make_phrases.gold unchanged.

    python probes.py        (prints failures; env CTW_GAZ as for the harness)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from make_phrases import (  # noqa: E402
    AFTER_MIDNIGHT, CLK, NOWS, RECENT, REF, WEEK, WEEKEND, AGO_D, AGO_H, D, gold, now_at,
)  # fmt: skip

spec = importlib.util.spec_from_file_location("r2cand", HERE / "candidate.py")
cand = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cand)

PROBES: list[tuple[str, tuple | None]] = [
    # refuter controls
    ("Ce soir Minlo m'a parlé d'un jeu, c'était lequel ?", D("today", "evening")),
    ("Ce Week-end, Léa m'a montré quoi ?", WEEKEND("this")),
    ("Le jeu dont Sacha a parlé hier, c'était quoi ?", D("yesterday")),
    ("Qu'est-ce que Minlo a dit sur le jeu ce matin ?", D("today", "morning")),
    ("C'était quoi l'épisode de Last Week Tonight hier soir ?", D("yesterday", "evening")),
    # informal / variants not in the seen sets
    ("Ce mat Léa m'a envoyé quel lien ?", D("today", "morning")),
    ("Yesterday arvo, which stream was I watching?", D("yesterday", "afternoon")),
    ("What did Sacha say this aft?", D("today", "afternoon")),
    ("Tonite, who was in the call?", D("today", "evening")),
    ("Avant-hier dans la nuit, on jouait à quoi ?", D("daybefore", "night_fr")),
    ("Y'a 2h, Minlo m'a dit quoi ?", AGO_H(2)),
    ("Toute à l'heure, j'écoutais quoi ?", RECENT),
    ("Minuit passé, qui était encore en vocal ?", AFTER_MIDNIGHT),
    ("Vers 10h du soir, on regardait quoi ?", CLK(22, 0)),
    ("Around 11 in the evening, what was playing?", CLK(23, 0)),
    ("Le WE dernier, j'ai joué à quoi ?", WEEKEND("last")),
    ("Over the wknd, which videos did I watch?", WEEKEND("last")),
    ("Earlier this week, what did Minlo recommend?", WEEK("this")),
    ("Plus tôt ce matin, Léa m'a dit quoi ?", D("today", "morning")),
    ("Il y a trois jours, on a parlé de quoi ?", AGO_D(3)),
    ("Hier vers 1h du mat, qui parlait ?", CLK(1, 0, "yesterday")),
    ("La nuit dernière vers 2h, j'écoutais quoi ?", CLK(2, 0, "yesterday")),
    ("Around lunch, who called me?", D("today", "noon")),
    ("Lundi mat', j'ai lu quoi ?", D("wd:mon", "morning")),
    ("What did I watch on Sunday night?", D("wd:sun", "night_en")),
    ("Samedi soir tard, on était combien ?", D("wd:sat", "evening")),
    # two-anchor shapes
    ("That clip from this morning, did Léa watch it last night?", D("yesterday", "night_en")),
    ("Which song did Sacha send yesterday that I played this morning?", D("yesterday")),
    ("Le stream de ce mat', Léa en a parlé hier soir, c'était quoi ?", D("today", "morning")),
    ("Quand Minlo a appelé hier soir, je regardais quoi ?", D("yesterday", "evening")),
    # time-free: titles / names / topics (must give no window)
    ("Did I watch the tonight show monologue?", None),
    ("Sacha a revu la nuit américaine ?", None),
    ("Qu'est-ce que Léa pense de midnight club ?", None),
    ("J'ai écouté quel remix de minuit chrétiens ?", None),
    ("Which episode of wednesday did Sacha mention?", None),
    ("Did I finish wednesday?", None),
    ("C'était quoi le jeu dont Minlo parlait ?", None),
    ("What did Léa say about the after party?", None),
    ("C'est quoi ce soi-disant leak dont Sacha parlait ?", None),
    ("Qu'est-ce que Minlo a dit sur le mode nuit ?", None),
    ("What did we decide, we were talking about Valorant?", None),
    ("Le lun de miel de Léa, c'était où ?", None),
    ("Qui chantait Saturday Night Fever dans la playlist ?", None),
    ("Which Friday Night Funkin mod did Sacha show me?", None),
    ("C'était quoi la vidéo sur les nuits blanches ?", None),
    ("What did Minlo say about night mode on Discord?", None),
    ("Did Sacha talk about the morning routine video?", None),
    ("Qu'est-ce que Léa a dit sur le film Vendredi 13 ?", None),
]


def main() -> int:
    fails = 0
    for q, meaning in PROBES:
        bad = []
        for label in NOWS:
            now = now_at(REF, label)
            try:
                g = gold(meaning, now)
            except AssertionError:
                continue
            w = cand.resolve_question(q, now, None)
            if g is None:
                ok = w is None
            else:
                tol = 30 * 60
                ok = (
                    w is not None
                    and (w[0] - g[0]).total_seconds() <= tol
                    and (g[1] - w[1]).total_seconds() <= tol
                    and (w[1] - w[0]) <= 2 * (g[1] - g[0])
                )
            if not ok:
                bad.append((label, w and [x.isoformat()[5:16] for x in w], g and [x.isoformat()[5:16] for x in g]))
        if bad:
            fails += 1
            print("FAIL", q, bad[:2])
    print(f"probes failing: {fails}/{len(PROBES)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
