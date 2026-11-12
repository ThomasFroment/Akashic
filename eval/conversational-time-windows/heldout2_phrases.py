"""Second held-out set: new hand-annotated FR/EN time questions -> heldout2.jsonl.

Same schema, meaning vocabulary and gold arithmetic as make_phrases.py (imported, unchanged), so
labels follow README.md "Labelling conventions" exactly. Meanings were written from each
question's wording alone, before running any resolver. Every record has split "heldout2".

Extra fields (ignored by harness.py, used for analysis):
- two_anchor: ``main_pos`` ("first" / "last": where the main anchor sits), ``connector_word``
  (que/qui/dont/that/which/quand/when/pendant que/après que/while/after or None) and
  ``final_no_time`` (the last clause asks without a time: "c'était quoi ?", "which one was it?").
- time_free: ``title_case`` ("lower" / "upper": how the time-like title is written).

The script refuses to write if a question collides with phrases.jsonl or with another new
question after lowercasing and stripping punctuation, and prints the closest existing questions.

Run: python heldout2_phrases.py   (writes heldout2.jsonl next to this file)
"""

from __future__ import annotations

import difflib
import json
import unicodedata
from pathlib import Path

from make_phrases import (
    AFTER_MIDNIGHT,
    CLK,
    NOWS,
    RECENT,
    REF,
    WEEKEND,
    WEEKEND_REFS,
    AGO_D,
    AGO_H,
    AGO_MIN,
    D,
    LAST_D,
    LAST_H,
    LAST_MIN,
    P,
    WEEK,
    describe,
    gold,
    now_at,
)

HERE = Path(__file__).resolve().parent

# Single-anchor questions: (question, lang, category, meaning, main time text)
S: list[tuple] = [
    # --- today -----------------------------------------------------------------------------
    ("Ajd j'ai passé combien de temps sur Valorant ?", "fr", "today", D("today"), "Ajd"),
    ("So far today, which channels have I watched on Twitch?", "en", "today", D("today"), "So far today"),
    ("Aujourd'hui, j'ai skip quelles vidéos de Minlo ?", "mixed", "today", D("today"), "Aujourd'hui"),
    ("Who did I talk to on Discord earlier today?", "en", "today", D("today"), "earlier today"),
    # --- morning -----------------------------------------------------------------------------
    ("Ce mat', j'ai lu quel article sur Steam ?", "fr", "morning", D("today", "morning"), "Ce mat'"),
    ("Qu'est-ce que j'ai codé en matinée ?", "fr", "morning", D("today", "morning"), "en matinée"),
    ("Who was in the voice channel this morning?", "en", "morning", D("today", "morning"), "this morning"),
    ("Ce matin, Minlo m'a share quel meme ?", "mixed", "morning", D("today", "morning"), "Ce matin"),
    # --- afternoon ---------------------------------------------------------------------------
    ("Cette aprem, Léa m'a parlé de quel resto ?", "fr", "afternoon", D("today", "afternoon"), "Cette aprem"),
    ("Qu'est-ce que j'ai téléchargé dans l'aprem ?", "fr", "afternoon", D("today", "afternoon"), "dans l'aprem"),
    ("Which PR was I reviewing this afternoon?", "en", "afternoon", D("today", "afternoon"), "this afternoon"),
    ("Cet aprèm, on a ranked avec Sacha sur quelle map ?", "mixed", "afternoon", D("today", "afternoon"), "Cet aprèm"),
    # --- noon / lunch ------------------------------------------------------------------------
    ("Pendant le déj, j'ai regardé quoi sur YouTube ?", "fr", "noon", D("today", "noon"), "Pendant le déj"),
    ("Around lunchtime, who pinged me on Discord?", "en", "noon", D("today", "noon"), "Around lunchtime"),
    ("Who was I talking to during lunch?", "en", "noon", D("today", "noon"), "during lunch"),
    ("À midi, Sacha m'a send quel lien ?", "mixed", "noon", D("today", "noon"), "À midi"),
    # --- evening -----------------------------------------------------------------------------
    ("Ce soir, qui m'a appelé sur Discord ?", "fr", "evening", D("today", "evening"), "Ce soir"),
    ("Which streamer was I watching tonight?", "en", "evening", D("today", "evening"), "tonight"),
    ("This evening, what did Léa post in the group chat?", "en", "evening", D("today", "evening"), "This evening"),
    ("Tonight, Minlo a dit quoi sur le nouveau patch ?", "mixed", "evening", D("today", "evening"), "Tonight"),
    # --- night -------------------------------------------------------------------------------
    ("Hier soir tard, on était combien dans le vocal ?", "fr", "night", D("yesterday", "night_fr"), "Hier soir tard"),
    ("Dans la nuit, Sacha m'a envoyé quel message ?", "fr", "night", D("cette_nuit", "night_fr"), "Dans la nuit"),
    ("Late last night, which game was I playing?", "en", "night", D("yesterday", "night_fr"), "Late last night"),
    ("Who joined the call last night?", "en", "night", D("yesterday", "night_en"), "last night"),
    ("Last night, Léa m'a recommandé quelle série ?", "mixed", "night", D("yesterday", "night_en"), "Last night"),
    ("Tard hier soir, Minlo parlait de quoi ?", "fr", "night", D("yesterday", "night_fr"), "Tard hier soir"),
    ("Cette nuit, j'ai grind quel jeu ?", "mixed", "night", D("cette_nuit", "night_fr"), "Cette nuit"),
    # --- yesterday ---------------------------------------------------------------------------
    ("Hier aprem, j'ai lu quel thread Reddit ?", "fr", "yesterday", D("yesterday", "afternoon"), "Hier aprem"),
    ("Hier mat', Léa m'a dit quoi au téléphone ?", "fr", "yesterday", D("yesterday", "morning"), "Hier mat'"),
    ("Yesterday around lunchtime, who sent me that Steam link?", "en", "yesterday", D("yesterday", "noon"), "Yesterday around lunchtime"),
    ("Which videos did Sacha send me yesterday?", "en", "yesterday", D("yesterday"), "yesterday"),
    ("Hier, j'ai binge-watch quelle série ?", "mixed", "yesterday", D("yesterday"), "Hier"),
    ("Yesterday afternoon, who was streaming Valorant?", "en", "yesterday", D("yesterday", "afternoon"), "Yesterday afternoon"),
    # --- day before yesterday ----------------------------------------------------------------
    ("What was Minlo playing the night before last?", "en", "daybefore", D("daybefore", "night_en"), "the night before last"),
    ("Avant-hier matin, j'ai installé quel mod ?", "fr", "daybefore", D("daybefore", "morning"), "Avant-hier matin"),
    ("Avant-hier, Sacha m'a parlé de quel game ?", "mixed", "daybefore", D("daybefore"), "Avant-hier"),
    # --- midnight ----------------------------------------------------------------------------
    ("Passé minuit, on parlait de quoi avec Minlo ?", "fr", "midnight", AFTER_MIDNIGHT, "Passé minuit"),
    ("After midnight, which video did I put on?", "en", "midnight", AFTER_MIDNIGHT, "After midnight"),
    ("Hier vers minuit, Léa a dit quoi ?", "fr", "midnight", CLK(0, 0, "yesterday"), "Hier vers minuit"),
    ("Who was speaking at midnight?", "en", "midnight", CLK(0), "at midnight"),
    # --- earlier -----------------------------------------------------------------------------
    ("Il y a un moment, j'ai ouvert quel PDF ?", "fr", "earlier", RECENT, "Il y a un moment"),
    ("A while ago, which song was playing?", "en", "earlier", RECENT, "A while ago"),
    ("Earlier, Minlo m'a montré quel site ?", "mixed", "earlier", RECENT, "Earlier"),
    ("Tout à l'heure, qui m'a écrit sur Discord ?", "fr", "earlier", RECENT, "Tout à l'heure"),
    # --- weekend -----------------------------------------------------------------------------
    ("Ce WE, Minlo m'a montré quel speedrun ?", "fr", "weekend", WEEKEND("this"), "Ce WE"),
    ("Over the weekend, who did I play Minecraft with?", "en", "weekend", WEEKEND("this"), "Over the weekend"),
    ("Last weekend, which album was on repeat?", "en", "weekend", WEEKEND("last"), "Last weekend"),
    ("Le weekend dernier, Léa m'a recommandé quel podcast tech ?", "fr", "weekend", WEEKEND("last"), "Le weekend dernier"),
    ("Pendant le weekend, j'ai stream quoi ?", "mixed", "weekend", WEEKEND("this"), "Pendant le weekend"),
    # --- weekdays ----------------------------------------------------------------------------
    ("Mercredi dernier, Sacha m'a montré quelle vidéo ?", "fr", "weekday", D("wd:wed"), "Mercredi dernier"),
    ("Dans la nuit de samedi à dimanche, on a joué à quoi ?", "fr", "weekday", D("wd:sat", "night_fr"), "Dans la nuit de samedi à dimanche"),
    ("What did Léa say on Thursday evening?", "en", "weekday", D("wd:thu", "evening"), "on Thursday evening"),
    ("On Sunday night, who was in the call?", "en", "weekday", D("wd:sun", "night_en"), "On Sunday night"),
    ("Samedi aprem, Minlo m'a parlé de quel film ?", "fr", "weekday", D("wd:sat", "afternoon"), "Samedi aprem"),
    ("Dimanche matin, j'ai watch quel replay ?", "mixed", "weekday", D("wd:sun", "morning"), "Dimanche matin"),
    ("On Monday, which tabs did I have open?", "en", "weekday", D("wd:mon"), "On Monday"),
    ("Jeudi midi, Léa m'a envoyé quel lien ?", "fr", "weekday", D("wd:thu", "noon"), "Jeudi midi"),
    # --- clock times -------------------------------------------------------------------------
    ("Vers 1h du mat, qui parlait dans le vocal ?", "fr", "clock", CLK(1), "Vers 1h du mat"),
    ("À 17h30, qui m'a appelé ?", "fr", "clock", CLK(17, 30), "À 17h30"),
    ("Around 11pm, what was Sacha talking about?", "en", "clock", CLK(23), "Around 11pm"),
    ("At 2am, which video was playing?", "en", "clock", CLK(2), "At 2am"),
    ("Hier vers 18h, j'étais in a call avec qui ?", "mixed", "clock", CLK(18, 0, "yesterday"), "Hier vers 18h"),
    ("Ce soir vers 20h, on regardait quel stream avec Léa ?", "fr", "clock", CLK(20, 0, "today"), "Ce soir vers 20h"),
    ("Yesterday at 4pm, who was I talking to?", "en", "clock", CLK(16, 0, "yesterday"), "Yesterday at 4pm"),
    ("Vers 3h du matin, j'écoutais quel track ?", "mixed", "clock", CLK(3), "Vers 3h du matin"),
    # --- relative ----------------------------------------------------------------------------
    ("Il y a une demi-heure, Sacha m'a dit quoi ?", "fr", "relative", AGO_MIN(30), "Il y a une demi-heure"),
    ("Il y a 3h, j'étais sur quel jeu ?", "fr", "relative", AGO_H(3), "Il y a 3h"),
    ("In the last two hours, which videos did I watch?", "en", "relative", LAST_H(2), "In the last two hours"),
    ("What was Léa saying 45 minutes ago?", "en", "relative", AGO_MIN(45), "45 minutes ago"),
    ("Il y a 4 jours, Minlo m'a parlé de quoi ?", "fr", "relative", AGO_D(4), "Il y a 4 jours"),
    ("Over the past two days, which streams did I watch?", "en", "relative", LAST_D(2), "Over the past two days"),
    ("Earlier this week, Léa m'a conseillé quel livre ?", "mixed", "relative", WEEK("this"), "Earlier this week"),
    ("La semaine dernière, j'ai regardé quels tutos Blender ?", "fr", "relative", WEEK("last"), "La semaine dernière"),
    ("Ces 10 dernières minutes, Minlo a dit quoi about the raid ?", "mixed", "relative", LAST_MIN(10), "Ces 10 dernières minutes"),
]  # fmt: skip

# Two-anchor questions: (question, lang, meaning of the MAIN clause, main time text,
#                        connector, connector_word, main_pos, final_no_time)
# connector as in make_phrases: "rel" (que/qui/dont/that/which), "when" (quand/when and the
# other temporal subordinators: pendant que/après que/while/after), "none" (no subordinator).
A: list[tuple] = [
    # --- FR, main anchor first --------------------------------------------------------------
    ("Quel morceau j'ai écouté cette nuit que Léa m'avait fait découvrir samedi ?", "fr", D("cette_nuit", "night_fr"), "cette nuit", "rel", "que", "first", False),
    ("C'était quoi la vidéo de cet aprèm qui parlait du patch annoncé hier ?", "fr", D("today", "afternoon"), "cet aprèm", "rel", "qui", "first", False),
    ("Qu'est-ce que j'ai lu ce matin dont Sacha parlait déjà hier soir ?", "fr", D("today", "morning"), "ce matin", "rel", "dont", "first", False),
    ("Qu'est-ce que Minlo a dit ce midi quand je lui ai parlé du stream d'hier soir ?", "fr", D("today", "noon"), "ce midi", "when", "quand", "first", False),
    ("Qu'est-ce que Léa racontait ce soir pendant que je regardais la vidéo que Sacha m'a envoyée ce midi ?", "fr", D("today", "evening"), "ce soir", "when", "pendant que", "first", False),
    ("Qu'est-ce que j'ai regardé hier soir après que Minlo m'a parlé de ce jeu hier aprèm ?", "fr", D("yesterday", "evening"), "hier soir", "when", "après que", "first", False),
    ("Quel jeu on a lancé avant-hier soir que Sacha avait recommandé vendredi ?", "fr", D("daybefore", "evening"), "avant-hier soir", "rel", "que", "first", False),
    ("Qu'est-ce que j'ai écouté vers 1h du mat que Léa m'avait partagé hier ?", "fr", CLK(1), "vers 1h du mat", "rel", "que", "first", False),
    ("Qu'est-ce que Léa disait cette aprem quand on débattait du build de ce mat' ?", "fr", D("today", "afternoon"), "cette aprem", "when", "quand", "first", False),
    ("Cet aprem j'ai regardé un tuto, Léa me l'avait envoyé avant-hier, c'était lequel ?", "fr", D("today", "afternoon"), "Cet aprem", "none", None, "first", True),
    ("Hier soir j'ai écouté un album que Sacha avait partagé samedi, c'était lequel ?", "fr", D("yesterday", "evening"), "Hier soir", "rel", "que", "first", True),
    ("La musique de cette nuit, Sacha l'a citée ce midi, c'était quoi ?", "fr", D("cette_nuit", "night_fr"), "cette nuit", "none", None, "first", True),
    ("Le jeu de dimanche aprèm, Minlo m'en a reparlé hier, c'était lequel ?", "fr", D("wd:sun", "afternoon"), "dimanche aprèm", "none", None, "first", True),
    # --- FR, main anchor last ---------------------------------------------------------------
    ("Le podcast que Léa m'a conseillé dimanche, je l'ai écouté ce mat' ?", "fr", D("today", "morning"), "ce mat'", "rel", "que", "last", False),
    ("La série dont Minlo parlait vendredi, j'ai regardé quel épisode cette nuit ?", "fr", D("cette_nuit", "night_fr"), "cette nuit", "rel", "dont", "last", False),
    ("Le mec qui streamait hier soir, Sacha en a reparlé ce midi ?", "fr", D("today", "noon"), "ce midi", "rel", "qui", "last", False),
    ("Quand Léa a cité ce film hier aprèm, est-ce que je l'avais regardé samedi soir ?", "fr", D("wd:sat", "evening"), "samedi soir", "when", "quand", "last", False),
    ("La vidéo d'hier soir, je l'ai finie ce matin ?", "fr", D("today", "morning"), "ce matin", "none", None, "last", False),
    ("Le morceau de cette nuit, Sacha me l'a renvoyé tout à l'heure ?", "fr", RECENT, "tout à l'heure", "none", None, "last", False),
    ("Le trailer de ce midi, Minlo l'a commenté ce soir ?", "fr", D("today", "evening"), "ce soir", "none", None, "last", False),
    ("Après que Sacha m'a envoyé le lien ce matin, j'ai regardé quoi ce midi ?", "fr", D("today", "noon"), "ce midi", "when", "après que", "last", False),
    ("Le replay de vendredi, je l'ai fini dimanche matin ?", "fr", D("wd:sun", "morning"), "dimanche matin", "none", None, "last", False),
    # --- EN, main anchor first --------------------------------------------------------------
    ("Which song did I play late last night that Léa shared on Saturday?", "en", D("yesterday", "night_fr"), "late last night", "rel", "that", "first", False),
    ("What article did I read during lunch which Minlo had linked yesterday?", "en", D("today", "noon"), "during lunch", "rel", "which", "first", False),
    ("What was Sacha saying this evening when I showed him the clip from this morning?", "en", D("today", "evening"), "this evening", "when", "when", "first", False),
    ("What did Léa say around lunchtime while I was watching the trailer Minlo sent yesterday?", "en", D("today", "noon"), "around lunchtime", "when", "while", "first", False),
    ("What did I watch on Saturday afternoon after Sacha told me about it on Friday?", "en", D("wd:sat", "afternoon"), "on Saturday afternoon", "when", "after", "first", False),
    ("Which map did we play over the weekend that Minlo complained about this morning?", "en", WEEKEND("this"), "over the weekend", "rel", "that", "first", False),
    ("Which video did I watch 2 hours ago that Léa recommended last night?", "en", AGO_H(2), "2 hours ago", "rel", "that", "first", False),
    ("What game did we play the night before last that Léa streamed last weekend?", "en", D("daybefore", "night_en"), "the night before last", "rel", "that", "first", False),
    ("What was I watching around 11pm which Sacha had recommended at lunch?", "en", CLK(23), "around 11pm", "rel", "which", "first", False),
    ("Last Friday night I watched a movie Minlo had suggested, which one was it?", "en", D("wd:fri", "night_en"), "Last Friday night", "none", None, "first", True),
    ("That podcast from yesterday morning, Léa brought it up tonight, what was it called?", "en", D("yesterday", "morning"), "yesterday morning", "none", None, "first", True),
    ("The video from the night before last, Minlo mentioned it at lunch, what was it?", "en", D("daybefore", "night_en"), "the night before last", "none", None, "first", True),
    # --- EN, main anchor last ---------------------------------------------------------------
    ("The trailer that Minlo posted yesterday, did I watch it during lunch?", "en", D("today", "noon"), "during lunch", "rel", "that", "last", False),
    ("The build which Sacha recommended on Sunday, did I try it late last night?", "en", D("yesterday", "night_fr"), "late last night", "rel", "which", "last", False),
    ("When Léa mentioned that album this morning, had I already heard it over the weekend?", "en", WEEKEND("this"), "over the weekend", "when", "when", "last", False),
    ("The clip from this morning, did Minlo send it again an hour ago?", "en", AGO_H(1), "an hour ago", "none", None, "last", False),
    ("The stream from Friday night, what did Sacha say about it this afternoon?", "en", D("today", "afternoon"), "this afternoon", "none", None, "last", False),
    ("The song that played around midnight, did Sacha ask about it earlier?", "en", RECENT, "earlier", "rel", "that", "last", False),
    # --- mixed ------------------------------------------------------------------------------
    ("C'était quoi le podcast que j'ai écouté this morning que Sacha m'a conseillé yesterday ?", "mixed", D("today", "morning"), "this morning", "rel", "que", "first", False),
    ("Qu'est-ce que Minlo a dit last night quand on regardait le replay de samedi ?", "mixed", D("yesterday", "night_en"), "last night", "when", "quand", "first", False),
    ("J'ai joué à quoi hier aprem that Sacha told me about on Friday ?", "mixed", D("yesterday", "afternoon"), "hier aprem", "rel", "that", "first", False),
    ("C'était quoi the video I watched tonight dont Léa parlait at lunch ?", "mixed", D("today", "evening"), "tonight", "rel", "dont", "first", False),
    ("What did Minlo say ce soir pendant que je jouais au jeu de cet aprem ?", "mixed", D("today", "evening"), "ce soir", "when", "pendant que", "first", False),
    ("La track de tonight, Sacha l'avait postée yesterday, c'était quoi ?", "mixed", D("today", "evening"), "tonight", "none", None, "first", True),
    ("Yesterday evening j'ai joué à un jeu que Minlo a streamé dimanche, c'était lequel ?", "mixed", D("yesterday", "evening"), "Yesterday evening", "rel", "que", "first", True),
    ("La vidéo de cette nuit, Minlo l'a partagée sur Discord this afternoon ?", "mixed", D("today", "afternoon"), "this afternoon", "none", None, "last", False),
    ("Le jeu que Léa a mentionné on Sunday, on y a joué hier soir tard ?", "mixed", D("yesterday", "night_fr"), "hier soir tard", "rel", "que", "last", False),
    ("Quand Minlo a parlé du patch this afternoon, j'avais déjà lu les notes ce mat' ?", "mixed", D("today", "morning"), "ce mat'", "when", "quand", "last", False),
    ("After Sacha sent me the link this afternoon, j'ai regardé quoi ce soir ?", "mixed", D("today", "evening"), "ce soir", "when", "after", "last", False),
    ("The vidéo que Léa a postée hier, je l'ai watch around lunchtime ?", "mixed", D("today", "noon"), "around lunchtime", "rel", "que", "last", False),
]  # fmt: skip

# Time-free questions (gold null): (question, lang, title_case of the time-like title)
T: list[tuple] = [
    ("Qui a gagné aux 12 coups de midi dans l'extrait que Léa m'a envoyé ?", "fr", "lower"),
    ("J'ai écouté quelle version de minuit chrétiens ?", "fr", "lower"),
    ("C'était qui le chanteur de Minuit, Chrétiens dans la playlist de Léa ?", "fr", "upper"),
    ("J'ai vu quel épisode de demain nous appartient ?", "fr", "lower"),
    ("Qu'est-ce que disait l'article du journal Le Soir sur Ubisoft ?", "fr", "upper"),
    ("Le film Vendredi 13 dont Sacha parlait, c'était lequel de la saga ?", "fr", "upper"),
    ("Qu'est-ce que l'invité des Matins de France Culture a dit sur l'IA ?", "fr", "upper"),
    ("Did Minlo send me a clip from the tonight show?", "en", "lower"),
    ("Which scene from weekend at bernie's did Sacha quote?", "en", "lower"),
    ("Which remix of SZA's The Weekend did Léa send?", "en", "upper"),
    ("What did Léa think of the day after tomorrow movie?", "en", "lower"),
    ("Which episode of Wednesday did Minlo talk about?", "en", "upper"),
    ("What did Sacha say about Elden Ring Nightreign?", "en", "upper"),
    ("Did I finish the After Hours album?", "en", "upper"),
    ("J'ai regardé quel épisode de wednesday ?", "mixed", "lower"),
    ("C'était quoi le meilleur moment de the morning show selon Léa ?", "mixed", "lower"),
    ("Sacha a dit quoi sur Saturday Night Fever ?", "mixed", "upper"),
    ("C'était quoi le premier son de after hours que Minlo m'a passé ?", "mixed", "lower"),
    ("Le film Before Midnight, Léa l'a trouvé comment ?", "mixed", "upper"),
]  # fmt: skip


def norm(q: str) -> str:
    kept = "".join(" " if unicodedata.category(c).startswith("P") else c for c in q.lower())
    return " ".join(kept.split())


def records() -> list[dict]:
    rows: list[dict] = []
    for q, lang, cat, meaning, main in S:
        rows.append({"question": q, "lang": lang, "category": cat, "meaning": meaning,
                     "main_time_text": main, "connector": None})  # fmt: skip
    for q, lang, meaning, main, conn, word, pos, final in A:
        rows.append({"question": q, "lang": lang, "category": "two_anchor", "meaning": meaning,
                     "main_time_text": main, "connector": conn, "connector_word": word,
                     "main_pos": pos, "final_no_time": final})  # fmt: skip
    for q, lang, case in T:
        rows.append({"question": q, "lang": lang, "category": "time_free", "meaning": None,
                     "main_time_text": None, "connector": None, "title_case": case})  # fmt: skip
    return rows


def check_collisions(rows: list[dict]) -> None:
    old = {norm(p[0]): p[0] for p in P}
    seen: dict[str, str] = {}
    for r in rows:
        n = norm(r["question"])
        assert n not in old, f"collides with phrases.jsonl: {r['question']!r} ~ {old[n]!r}"
        assert n not in seen, f"duplicate in heldout2: {r['question']!r} ~ {seen[n]!r}"
        seen[n] = r["question"]
        main = r["main_time_text"]
        if main is not None:
            assert main in r["question"], (r["question"], main)
    # closest existing question per new one (for a manual paraphrase review)
    olds = list(old)
    close = []
    for r in rows:
        n = norm(r["question"])
        best = max(olds, key=lambda o: difflib.SequenceMatcher(None, n, o).ratio())
        close.append((difflib.SequenceMatcher(None, n, best).ratio(), r["question"], old[best]))
    close.sort(reverse=True)
    print("closest existing questions (ratio >= 0.75):")
    for ratio, new, existing in close:
        if ratio >= 0.75:  # noqa: PLR2004
            print(f"  {ratio:.2f}  {new}  <->  {existing}")


def main() -> None:
    rows = records()
    check_collisions(rows)
    out = HERE / "heldout2.jsonl"
    n_cases = 0
    with out.open("w", encoding="utf-8") as fh:
        for i, r in enumerate(rows):
            meaning = r.pop("meaning")
            refs = WEEKEND_REFS if meaning and meaning[0] == "WEEKEND" else (REF,)
            labels = []
            for ref in refs:
                for label in NOWS:
                    now = now_at(ref, label)
                    g = gold(meaning, now)
                    labels.append({"ref_date": ref.isoformat(), "now": now.isoformat(),
                                   "now_label": label,
                                   "gold": None if g is None else [g[0].isoformat(), g[1].isoformat()]})  # fmt: skip
            n_cases += len(labels)
            rec = {"id": f"h2_{i:03d}", "question": r.pop("question"), "lang": r.pop("lang"),
                   "category": r.pop("category"), "split": "heldout2",
                   "meaning": describe(meaning), "main_time_text": r.pop("main_time_text"),
                   "connector": r.pop("connector"), **r, "labels": labels}  # fmt: skip
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    by_cat: dict[str, int] = {}
    by_lang: dict[str, int] = {}
    for r in records():
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
        by_lang[r["lang"]] = by_lang.get(r["lang"], 0) + 1
    two = A
    print(f"{len(rows)} questions, {n_cases} cases -> {out}")
    print("by category:", by_cat)
    print("by lang:", by_lang)
    print("two_anchor main_pos:", {p: sum(a[6] == p for a in two) for p in ("first", "last")},
          "connector:", {c: sum(a[4] == c for a in two) for c in ("rel", "when", "none")},
          "final_no_time:", sum(a[7] for a in two))  # fmt: skip
    print("time_free title_case:", {c: sum(t[2] == c for t in T) for c in ("lower", "upper")})


if __name__ == "__main__":
    main()
