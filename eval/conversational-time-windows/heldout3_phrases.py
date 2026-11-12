"""Third held-out set: fresh FR/EN/mixed time questions -> heldout3.jsonl.

Written independently of phrases.jsonl's wording and of every candidate. Meanings use the
annotation vocabulary and span arithmetic of make_phrases.py unchanged (README "Labelling
conventions"); only questions those conventions settle are labelled (see heldout3_README.md for
what was left out). Persona: French/English night-owl gamer typing to a memory app, about half
ordinary phrasing, half informal (apocopes, SMS spellings, missing accents, typos, code-switching).

Run: python heldout3_phrases.py   (writes heldout3.jsonl next to this file, checks that no
question collides with phrases.jsonl after lowercasing and stripping punctuation)
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter

from make_phrases import (
    AFTER_MIDNIGHT,
    AGO_D,
    AGO_H,
    AGO_MIN,
    CLK,
    HERE,
    LAST_D,
    LAST_H,
    NOWS,
    RECENT,
    REF,
    WEEK,
    WEEKEND,
    WEEKEND_REFS,
    D,
    describe,
    gold,
    now_at,
)

# (question, lang, category, meaning, main time text copied from the question)
SINGLE: list[tuple] = [
    # --- today ---------------------------------------------------------------------------------
    ("Qu'est-ce que j'ai fait auj sur le PC ?", "fr", "today", D("today"), "auj"),
    ("ajd j'ai regardé quoi sur youtube", "fr", "today", D("today"), "ajd"),
    ("what did i do today besides gaming", "en", "today", D("today"), "today"),
    ("Did Léa send me anything today?", "en", "today", D("today"), "today"),
    ("c quoi les vidéos que j'ai watch today", "mixed", "today", D("today"), "today"),
    ("plus tot aujourd'hui minlo a parlé d'un mod, lequel ?", "fr", "today", D("today"), "plus tot aujourd'hui"),
    ("what was that site i was on earlier today, le truc sur les claviers ?", "mixed", "today", D("today"), "earlier today"),
    # --- this morning --------------------------------------------------------------------------
    ("Ce mat j'ai regardé quelle vidéo ?", "fr", "morning", D("today", "morning"), "Ce mat"),
    ("Qu'est-ce que Sacha a dit ce matin sur le tournoi ?", "fr", "morning", D("today", "morning"), "ce matin"),
    ("What did I read this morning about the new GPU drivers?", "en", "morning", D("today", "morning"), "this morning"),
    ("this mornin minlo sent me a clip, which one", "en", "morning", D("today", "morning"), "this mornin"),
    ("c'était quoi le podcast this morning ?", "mixed", "morning", D("today", "morning"), "this morning"),
    ("In the morning call, what did Léa say about her exam?", "en", "morning", D("today", "morning"), "In the morning call"),
    ("Dans la matinée Minlo m'a parlé de quel anime ?", "fr", "morning", D("today", "morning"), "Dans la matinée"),
    # --- this afternoon ------------------------------------------------------------------------
    ("cet aprem on a parlé de quoi avec léa", "fr", "afternoon", D("today", "afternoon"), "cet aprem"),
    ("Qu'est-ce que j'ai lu cet après-midi sur Reddit ?", "fr", "afternoon", D("today", "afternoon"), "cet après-midi"),
    ("What did Minlo stream this afternoon?", "en", "afternoon", D("today", "afternoon"), "this afternoon"),
    ("what was i browsing this afternon", "en", "afternoon", D("today", "afternoon"), "this afternon"),
    ("c quoi la vid que j'ai regardée this afternoon", "mixed", "afternoon", D("today", "afternoon"), "this afternoon"),
    ("Cet apres-midi Sacha m'a envoyé quel lien ?", "fr", "afternoon", D("today", "afternoon"), "Cet apres-midi"),
    # --- noon / lunch --------------------------------------------------------------------------
    ("A midi j'ai regardé quoi en mangeant ?", "fr", "noon", D("today", "noon"), "A midi"),
    ("pdt la pause dej, c'était quoi la vidéo ?", "fr", "noon", D("today", "noon"), "pdt la pause dej"),
    ("What did Sacha say at lunchtime about the raid?", "en", "noon", D("today", "noon"), "at lunchtime"),
    ("around noon what was i listening to", "en", "noon", CLK(12), "around noon"),
    ("le clip de ce midi c'était quoi, the one with the cat ?", "mixed", "noon", D("today", "noon"), "ce midi"),
    # --- this evening / tonight ----------------------------------------------------------------
    ("Ce soir Léa a spoil quel film ?", "mixed", "evening", D("today", "evening"), "Ce soir"),
    ("ce soir g regardé quoi", "fr", "evening", D("today", "evening"), "ce soir"),
    ("What did we play tonight with Minlo?", "en", "evening", D("today", "evening"), "tonight"),
    ("wat did sacha say tonite about the new season", "en", "evening", D("today", "evening"), "tonite"),
    ("This evening I had a tab open about mechanical keyboards, which one?", "en", "evening", D("today", "evening"), "This evening"),
    ("Dans la soirée on a regardé quoi sur Twitch ?", "fr", "evening", D("today", "evening"), "Dans la soirée"),
    ("le stream tonight c'était qui ?", "mixed", "evening", D("today", "evening"), "tonight"),
    ("what was Léa talking about ce soir on Discord", "mixed", "evening", D("today", "evening"), "ce soir"),
    # --- night ---------------------------------------------------------------------------------
    ("Qu'est-ce que Minlo racontait cette nuit en vocal ?", "fr", "night", D("cette_nuit", "night_fr"), "cette nuit"),
    ("cte nuit j'ai regardé quoi", "fr", "night", D("cette_nuit", "night_fr"), "cte nuit"),
    ("La nuit derniere Sacha m'a montré quel jeu ?", "fr", "night", D("yesterday", "night_fr"), "La nuit derniere"),
    ("what did i watch last nite", "en", "night", D("yesterday", "night_en"), "last nite"),
    ("Last night, which song did Minlo put on?", "en", "night", D("yesterday", "night_en"), "Last night"),
    ("what was Sacha ranting about late last night?", "en", "night", D("yesterday", "night_fr"), "late last night"),
    ("la vidéo de last night, c'était quoi déjà ?", "mixed", "night", D("yesterday", "night_en"), "last night"),
    ("qu'est-ce qu'on a dit cette nuit in the call", "mixed", "night", D("cette_nuit", "night_fr"), "cette nuit"),
    # --- yesterday and its parts ---------------------------------------------------------------
    ("yday minlo a dit quoi sur l'update", "mixed", "yesterday", D("yesterday"), "yday"),
    ("Qu'est-ce que j'ai acheté hier sur Steam ?", "fr", "yesterday", D("yesterday"), "hier"),
    ("hier soir on a queue sur quel jeu", "mixed", "yesterday", D("yesterday", "evening"), "hier soir"),
    ("What did Sacha send me yesterday afternoon?", "en", "yesterday", D("yesterday", "afternoon"), "yesterday afternoon"),
    ("Hier aprem j'ai lu un article sur les SSD, which one ?", "mixed", "yesterday", D("yesterday", "afternoon"), "Hier aprem"),
    ("yesterday morning which video was on my second screen", "en", "yesterday", D("yesterday", "morning"), "yesterday morning"),
    ("c quoi le jeu qu'on a test yesterday ?", "mixed", "yesterday", D("yesterday"), "yesterday"),
    ("Hier midi j'ai écouté quel podcast ?", "fr", "yesterday", D("yesterday", "noon"), "Hier midi"),
    ("what did Léa say hier soir sur Discord", "mixed", "yesterday", D("yesterday", "evening"), "hier soir"),
    ("Yesterday evening, what anime was I watching?", "en", "yesterday", D("yesterday", "evening"), "Yesterday evening"),
    # --- day before yesterday ------------------------------------------------------------------
    ("Avant hier on a parlé de quoi avec Léa ?", "fr", "daybefore", D("daybefore"), "Avant hier"),
    ("What did I watch the day before yesterday on Twitch?", "en", "daybefore", D("daybefore"), "the day before yesterday"),
    ("avant-hier soir minlo m'a envoyé quoi", "fr", "daybefore", D("daybefore", "evening"), "avant-hier soir"),
    ("c'était quoi le trailer the day before yesterday ?", "mixed", "daybefore", D("daybefore"), "the day before yesterday"),
    # --- midnight ------------------------------------------------------------------------------
    ("Vers minuit on écoutait quoi ?", "fr", "midnight", CLK(0), "Vers minuit"),
    ("what was on my screen around midnight", "en", "midnight", CLK(0), "around midnight"),
    ("after midnight what did minlo and i play", "en", "midnight", AFTER_MIDNIGHT, "after midnight"),
    ("apres minuit j'ai regardé quoi sur youtube", "fr", "midnight", AFTER_MIDNIGHT, "apres minuit"),
    ("last night around midnight, c'était quoi la vidéo ?", "mixed", "midnight", CLK(0, 0, "yesterday"), "last night around midnight"),
    # --- earlier / tout à l'heure ----------------------------------------------------------------
    ("tt à l'heure Sacha a dit quoi sur le patch ?", "fr", "earlier", RECENT, "tt à l'heure"),
    ("Qu'est-ce que j'ai lu tout a l'heure sur le forum ?", "fr", "earlier", RECENT, "tout a l'heure"),
    ("what was that video I had open earlier?", "en", "earlier", RECENT, "earlier"),
    ("Minlo said something a while ago about a sale, what was it?", "en", "earlier", RECENT, "a while ago"),
    ("y a un moment Léa parlait d'un resto, c'était lequel ?", "fr", "earlier", RECENT, "y a un moment"),
    ("c quoi le lien que Sacha a envoyé earlier ?", "mixed", "earlier", RECENT, "earlier"),
    # --- weekend -------------------------------------------------------------------------------
    ("Qu'est-ce qu'on a fait ce week-end avec Minlo ?", "fr", "weekend", WEEKEND("this"), "ce week-end"),
    ("ce we j'ai joué à quoi", "fr", "weekend", WEEKEND("this"), "ce we"),
    ("What did I watch over the weekend?", "en", "weekend", WEEKEND("this"), "over the weekend"),
    ("what did sacha and i play last wkend", "en", "weekend", WEEKEND("last"), "last wkend"),
    ("le week-end dernier j'ai regardé quel film ?", "fr", "weekend", WEEKEND("last"), "le week-end dernier"),
    ("this weekend j'ai binge quelle série ?", "mixed", "weekend", WEEKEND("this"), "this weekend"),
    ("le we dernier, what did Léa recommend ?", "mixed", "weekend", WEEKEND("last"), "le we dernier"),
    # --- weekdays ------------------------------------------------------------------------------
    ("Samedi soir on a watch quel film ?", "mixed", "weekday", D("wd:sat", "evening"), "Samedi soir"),
    ("dimanche matin j'ai scroll quoi sur reddit", "mixed", "weekday", D("wd:sun", "morning"), "dimanche matin"),
    ("what did minlo say on sat night", "en", "weekday", D("wd:sat", "night_en"), "on sat night"),
    ("Friday evening, on a try quel jeu ?", "mixed", "weekday", D("wd:fri", "evening"), "Friday evening"),
    ("jeudi dernier sacha m'a parlé de quel game ?", "mixed", "weekday", D("wd:thu"), "jeudi dernier"),
    ("What did I watch last Wednesday?", "en", "weekday", D("wd:wed"), "last Wednesday"),
    ("lundi aprem j'ai watch quoi ?", "mixed", "weekday", D("wd:mon", "afternoon"), "lundi aprem"),
    ("on Sunday night c'était quoi le film ?", "mixed", "weekday", D("wd:sun", "night_en"), "on Sunday night"),
    # --- clock times ---------------------------------------------------------------------------
    ("Vers 22h30 on parlait de quoi ?", "fr", "clock", CLK(22, 30), "Vers 22h30"),
    ("what was i watching around 10pm", "en", "clock", CLK(22), "around 10pm"),
    ("vers 2h du mat j'écoutais quoi", "fr", "clock", CLK(2), "vers 2h du mat"),
    ("Hier vers 21h, Léa m'a dit quoi ?", "fr", "clock", CLK(21, 0, "yesterday"), "Hier vers 21h"),
    ("at 4pm yesterday what was on my screen", "en", "clock", CLK(16, 0, "yesterday"), "at 4pm yesterday"),
    ("ce matin vers 8h j'ai lu quoi", "fr", "clock", CLK(8, 0, "today"), "ce matin vers 8h"),
    ("last night around 2am, what was Minlo saying?", "en", "clock", CLK(2, 0, "yesterday"), "last night around 2am"),
    ("ce soir vers 21h c'était quoi le son ?", "fr", "clock", CLK(21, 0, "today"), "ce soir vers 21h"),
    ("around 5pm j'ai regardé quelle vidéo ?", "mixed", "clock", CLK(17), "around 5pm"),
    ("c'était quoi la vidéo vers 23h, the one about Factorio ?", "mixed", "clock", CLK(23), "vers 23h"),
    # --- relative ranges -----------------------------------------------------------------------
    ("y a 2h Minlo parlait de quoi ?", "fr", "relative", AGO_H(2), "y a 2h"),
    ("What did Sacha say 3 hours ago?", "en", "relative", AGO_H(3), "3 hours ago"),
    ("in the last hour what did i listen to", "en", "relative", LAST_H(1), "in the last hour"),
    ("ces 2 derniers jours j'ai regardé quoi, any new trailers ?", "mixed", "relative", LAST_D(2), "ces 2 derniers jours"),
    ("What did I watch 4 days ago?", "en", "relative", AGO_D(4), "4 days ago"),
    ("la semaine derniere léa m'a recommandé quelle série", "fr", "relative", WEEK("last"), "la semaine derniere"),
    ("y a genre 20 min sacha a dit quoi", "fr", "relative", AGO_MIN(20), "y a genre 20 min"),
    ("what did I watch this week, genre les vidéos de speedrun ?", "mixed", "relative", WEEK("this"), "this week"),
    ("in the last 2 hours qu'est-ce que Minlo a dit ?", "mixed", "relative", LAST_H(2), "in the last 2 hours"),
    ("il y a 3 jours, what game did I download?", "mixed", "relative", AGO_D(3), "il y a 3 jours"),
]  # fmt: skip

# Two anchors: (question, lang, meaning of the MAIN clause, main time text, connector, order)
# connector introduces the secondary time: "rel" (que/qui/dont/ce que/that), "when" (quand/when),
# "none" (comma, reduced relative, preposition). order: where the main anchor sits.
TWO: list[tuple] = [
    # --- main anchor first ---------------------------------------------------------------------
    ("Quel son j'ai écouté ce matin que Sacha m'a envoyé hier soir ?", "fr", D("today", "morning"), "ce matin", "rel", "first"),
    ("what vid did i watch last night that minlo linked on discord yday", "en", D("yesterday", "night_en"), "last night", "rel", "first"),
    ("C quoi la vidéo que j'ai regardée cet aprem dont Léa parlait hier ?", "fr", D("today", "afternoon"), "cet aprem", "rel", "first"),
    ("Who was talking in the call tonight when I opened the clip from this morning?", "en", D("today", "evening"), "tonight", "when", "first"),
    ("Ce soir vers 22h on a parlé de quoi, c'était le jeu que Minlo a acheté samedi ?", "fr", CLK(22, 0, "today"), "Ce soir vers 22h", "rel", "first"),
    ("what did sacha say at lunch about the build i was theorycrafting yday", "en", D("today", "noon"), "at lunch", "none", "first"),
    ("J'ai play quoi ce we que Minlo m'avait conseillé vendredi ?", "mixed", WEEKEND("this"), "ce we", "rel", "first"),
    ("Did Léa mention the trailer this afternoon, the one we saw in the Nintendo Direct last night?", "en", D("today", "afternoon"), "this afternoon", "none", "first"),
    ("Qu'est-ce que Minlo a dit y a 2h sur le patch qui est sorti hier ?", "fr", AGO_H(2), "y a 2h", "rel", "first"),
    ("Quelle vidéo j'ai mate hier aprem, celle que Sacha m'a envoyée dimanche ?", "fr", D("yesterday", "afternoon"), "hier aprem", "rel", "first"),
    ("c'était quoi le stream que j'ai lurk tonight, Minlo l'avait partagé ce matin ?", "mixed", D("today", "evening"), "tonight", "none", "first"),
    ("What song was playing around 1am, the one Sacha shared on Sunday?", "en", CLK(1), "around 1am", "none", "first"),
    ("Hier soir quand on jouait à Helldivers, Minlo a parlé de quel film qu'il avait vu samedi ?", "fr", D("yesterday", "evening"), "Hier soir", "rel", "first"),
    ("last nite in the discord call, which game did Sacha say he played on Saturday?", "en", D("yesterday", "night_en"), "last nite", "none", "first"),
    ("ce mat j'ai lu un thread reddit, sacha l'avait link hier soir, c'etait quoi ?", "mixed", D("today", "morning"), "ce mat", "none", "first"),
    ("What did I buy on Steam yesterday afternoon that Minlo recommended last week?", "en", D("yesterday", "afternoon"), "yesterday afternoon", "rel", "first"),
    ("Quel épisode j'ai regardé dimanche soir que Léa m'avait conseillé la semaine dernière ?", "fr", D("wd:sun", "evening"), "dimanche soir", "rel", "first"),
    ("Qu'est-ce que Sacha a dit vers 23h quand je lui ai parlé du clip de cet aprem ?", "fr", CLK(23), "vers 23h", "when", "first"),
    ("what was minlo saying 20 mins ago about the vid i sent him this morning", "en", AGO_MIN(20), "20 mins ago", "none", "first"),
    ("J'ai écouté quoi après minuit, le podcast que Minlo m'a conseillé hier ?", "fr", AFTER_MIDNIGHT, "après minuit", "rel", "first"),
    ("On Friday I watched a speedrun Sacha linked the week before, which one was it?", "en", D("wd:fri"), "On Friday", "none", "first"),
    ("tonite we played a map Léa found yday, whats it called", "en", D("today", "evening"), "tonite", "none", "first"),
    ("c quoi le jeu que j'ai lancé ce soir dont Minlo m'a parlé ce midi", "fr", D("today", "evening"), "ce soir", "rel", "first"),
    ("j'ai check quel site ce matin, celui que Sacha m'a link last night ?", "mixed", D("today", "morning"), "ce matin", "rel", "first"),
    ("what did Léa say ce midi about the trailer qu'on a vu hier soir ?", "mixed", D("today", "noon"), "ce midi", "rel", "first"),
    ("yesterday evening j'ai joué à un indie, Sacha l'avait mentionné ce we, c'était lequel ?", "mixed", D("yesterday", "evening"), "yesterday evening", "none", "first"),
    ("c'était quoi le son tonight, celui que Léa a share hier ?", "mixed", D("today", "evening"), "tonight", "rel", "first"),
    ("on a parlé de quoi vers 22h with Sacha, le truc qu'il avait vu samedi ?", "mixed", CLK(22), "vers 22h", "rel", "first"),
    ("Sunday night j'ai regardé un docu que Léa m'a conseillé vendredi, c'était lequel ?", "mixed", D("wd:sun", "night_en"), "Sunday night", "rel", "first"),
    # --- main anchor last ----------------------------------------------------------------------
    ("La vidéo que Sacha m'a envoyée ce matin, je l'ai regardée quand cet aprem ?", "fr", D("today", "afternoon"), "cet aprem", "rel", "last"),
    ("The clip Minlo posted yesterday, did I watch it tonight?", "en", D("today", "evening"), "tonight", "none", "last"),
    ("Le jeu que Léa a acheté ce we, on y a joué hier soir ?", "fr", D("yesterday", "evening"), "hier soir", "rel", "last"),
    ("The song that Sacha shared on Saturday, did I play it after midnight?", "en", AFTER_MIDNIGHT, "after midnight", "rel", "last"),
    ("Le tuto que Minlo m'a conseillé avant hier, je l'ai maté ce mat ?", "fr", D("today", "morning"), "ce mat", "rel", "last"),
    ("the patch notes that dropped this morning, what did Léa say about them y a 1h ?", "mixed", AGO_H(1), "y a 1h", "rel", "last"),
    ("Quand Sacha a parlé du tournoi ce midi, j'avais regardé la VOD hier soir ?", "fr", D("yesterday", "evening"), "hier soir", "when", "last"),
    ("When Léa mentioned that anime last weekend, had I already started it on Friday?", "en", D("wd:fri"), "on Friday", "when", "last"),
    ("The map Minlo was raving about yday, did we play it 2 hrs ago?", "en", AGO_H(2), "2 hrs ago", "none", "last"),
    ("La playlist lo-fi que Léa m'a passée hier, elle tournait cte nuit vers 3h ?", "fr", CLK(3, 0, "cette_nuit"), "cte nuit vers 3h", "rel", "last"),
    ("the vod sacha sent this morning, did i finish it earlier?", "en", RECENT, "earlier", "none", "last"),
    ("le build que Minlo m'a montré lundi, je l'ai try ce soir ?", "mixed", D("today", "evening"), "ce soir", "rel", "last"),
    ("That reddit thread Léa linked this morning, did I read it at lunch?", "en", D("today", "noon"), "at lunch", "none", "last"),
    ("Ce que Sacha m'a recommandé dimanche, je l'ai regardé hier ou pas ?", "fr", D("yesterday"), "hier", "rel", "last"),
    ("the game Minlo talked about hier soir, on y a joué this afternoon ?", "mixed", D("today", "afternoon"), "this afternoon", "none", "last"),
    ("La vidéo dont Minlo parlait ce matin, I watched it last night right ?", "mixed", D("yesterday", "night_en"), "last night", "rel", "last"),
]  # fmt: skip

# Time-free: (question, lang, kind) with kind "title" (time word inside a title or name of a
# show, song, game, channel, film, book, newspaper) or "nontemporal" (time-like word used for
# something that is not a time: a format, a setting, a region, a product).
FREE: list[tuple] = [
    # --- titles --------------------------------------------------------------------------------
    ("Qu'est-ce que Léa pensait de Mercredi saison 2 ?", "fr", "title"),
    ("la série mercredi, sacha a kiffé ou pas ?", "fr", "title"),
    ("did minlo like the wednesday season 2 finale", "en", "title"),
    ("C'était quoi l'extrait de Ce soir ou jamais avec le philosophe ?", "fr", "title"),
    ("j'ai regardé quel replay de telematin sur la Switch 2 ?", "fr", "title"),
    ("What did Sacha say about Elden Ring Nightreign?", "en", "title"),
    ("le jeu nightreign, minlo l'a trouvé comment, worth it ?", "mixed", "title"),
    ("Which remix of Midnight City did Léa send me?", "en", "title"),
    ("c quoi le son nightcall de kavinsky que sacha a mis en boucle ?", "fr", "title"),
    ("What did Minlo think of Midnight Mass?", "en", "title"),
    ("what did Sacha think of The Morning Show, il a continué ?", "mixed", "title"),
    ("Qu'est-ce que Léa a dit sur l'invité de Vivement dimanche ?", "fr", "title"),
    ("C'était quoi la bande-annonce de Vendredi 13 que Minlo m'a montrée ?", "fr", "title"),
    ("Did I watch the Friday Night Lights pilot?", "en", "title"),
    ("which sunday bloody sunday live version did sacha share", "en", "title"),
    ("c'était quoi la cover de manic monday sur youtube", "mixed", "title"),
    ("Friday I'm in Love, c'était dans quelle playlist de Léa ?", "mixed", "title"),
    ("What was the Monday Morning Podcast episode Minlo quoted?", "en", "title"),
    ("J'ai lu quoi dans Le Soir sur la Belgique ?", "fr", "title"),
    ("C'était quoi le reportage de Soir 3 sur les JO ?", "fr", "title"),
    ("Which Persona 4 video about the Midnight Channel did I watch?", "en", "title"),
    ("Qu'est-ce que Minlo a dit sur Tonight We Riot ?", "fr", "title"),
    ("What did Léa say about the album After Hours?", "en", "title"),
    ("c quoi la chaine night mind que sacha m'a conseillée", "fr", "title"),
    ("Which video essay on Until Dawn did I watch?", "en", "title"),
    ("Qu'est-ce que Léa a dit du film Minuit dans le jardin du bien et du mal ?", "fr", "title"),
    ("La Nuit des temps de Barjavel, j'ai lu quel résumé ?", "fr", "title"),
    ("what did minlo say abt days gone", "en", "title"),
    ("c'était quoi le meme avec tonight tonight des smashing pumpkins", "mixed", "title"),
    ("What did Sacha think of Yesterday, the Beatles movie?", "en", "title"),
    ("What was the Nightcrawler scene Minlo kept quoting?", "en", "title"),
    ("what did sacha say about the midnight rain lyrics", "en", "title"),
    ("j'ai regardé quel extrait de Un après-midi de chien ?", "fr", "title"),
    ("the Saturday Night Fever dance scene, c'était dans quelle vidéo ?", "mixed", "title"),
    # --- time-like words used non-temporally ---------------------------------------------------
    ("Où j'ai trouvé le fichier midi de la musique de Zelda ?", "fr", "nontemporal"),
    ("C'était où le gîte dans le Midi que Léa m'a montré ?", "fr", "nontemporal"),
    ("What was the 3am challenge video Minlo laughed at?", "en", "nontemporal"),
    ("C'était quoi la vidéo 24h dans un Ikea ?", "fr", "nontemporal"),
    ("Quelle table de nuit j'ai regardée sur le site d'Ikea ?", "fr", "nontemporal"),
    ("Which page did I read about the Windows night light setting?", "en", "nontemporal"),
    ("What did Sacha say about the day one patch?", "en", "nontemporal"),
    ("that morning routine vid from the productivity youtuber, whats it called", "en", "nontemporal"),
    ("Le cycle jour nuit dans Minecraft, j'avais vu quelle vidéo là-dessus ?", "fr", "nontemporal"),
    ("c'était quel site pour les billets last minute ?", "mixed", "nontemporal"),
    ("What did Minlo say about night vision goggles in Tarkov?", "en", "nontemporal"),
    ("the weekly reset in destiny, sacha en a dit quoi ?", "mixed", "nontemporal"),
    ("J'ai regardé quelle robe de soirée pour le mariage de ma cousine ?", "fr", "nontemporal"),
    ("what did léa say abt the night shift at her job", "en", "nontemporal"),
    ("le menu du midi à 12 balles, c'était quel resto ?", "fr", "nontemporal"),
    ("the late night snack recipe minlo sent, c'était quoi déjà ?", "mixed", "nontemporal"),
    ("le weekend pass du festival, il coûtait combien ?", "mixed", "nontemporal"),
    ("le day one patch, minlo en pensait quoi ?", "mixed", "nontemporal"),
]  # fmt: skip


def norm(q: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces (collision check only)."""
    q = unicodedata.normalize("NFKD", q.lower())
    q = "".join(c for c in q if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", q).split())


def rows() -> list[dict]:
    out: list[dict] = []
    for q, lang, cat, meaning, main in SINGLE:
        out.append({"question": q, "lang": lang, "category": cat, "meaning": meaning,
                    "main_time_text": main, "connector": None})  # fmt: skip
    for q, lang, meaning, main, conn, order in TWO:
        out.append({"question": q, "lang": lang, "category": "two_anchor", "meaning": meaning,
                    "main_time_text": main, "connector": conn, "anchor_order": order})  # fmt: skip
    for q, lang, kind in FREE:
        out.append({"question": q, "lang": lang, "category": "time_free", "meaning": None,
                    "main_time_text": None, "connector": None, "time_free_kind": kind})  # fmt: skip
    return out


def main() -> None:
    base = HERE / "phrases.jsonl"
    with base.open(encoding="utf-8") as fh:  # question texts only, for the collision check
        existing = {norm(json.loads(line)["question"]) for line in fh}
    recs = rows()
    seen: set[str] = set()
    for r in recs:
        n = norm(r["question"])
        assert n not in existing, f"collides with phrases.jsonl: {r['question']}"
        assert n not in seen, f"duplicate: {r['question']}"
        seen.add(n)
        if r["main_time_text"] is not None:
            assert r["main_time_text"] in r["question"], r
    out = HERE / "heldout3.jsonl"
    n_cases = 0
    with out.open("w", encoding="utf-8") as fh:
        for i, r in enumerate(recs):
            meaning = r.pop("meaning")
            refs = WEEKEND_REFS if meaning and meaning[0] == "WEEKEND" else (REF,)
            labels = []
            for ref in refs:
                for label in NOWS:
                    now = now_at(ref, label)
                    g = gold(meaning, now)
                    labels.append(
                        {
                            "ref_date": ref.isoformat(),
                            "now": now.isoformat(),
                            "now_label": label,
                            "gold": None if g is None else [g[0].isoformat(), g[1].isoformat()],
                        }
                    )
            n_cases += len(labels)
            rec = {"id": f"h3_{i:03d}", **r, "split": "heldout3", "meaning": describe(meaning),
                   "labels": labels}  # fmt: skip
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"{len(recs)} questions, {n_cases} cases -> {out}")
    print("lang:", dict(Counter(r["lang"] for r in recs)))
    print("category:", dict(Counter(r["category"] for r in recs)))
    two = [r for r in recs if r["category"] == "two_anchor"]
    print("two_anchor connector:", dict(Counter(r["connector"] for r in two)),
          "order:", dict(Counter(r["anchor_order"] for r in two)))  # fmt: skip
    print("time_free kind:", dict(Counter(r.get("time_free_kind") for r in recs if r["category"] == "time_free")))


if __name__ == "__main__":
    main()
