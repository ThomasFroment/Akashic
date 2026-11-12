"""Hand-annotated FR/EN time questions -> phrases.jsonl (gold spans per "now").

Each question carries a hand-written *meaning* (which period the user means), written from the
question alone. ``gold`` turns that meaning into a concrete [start, end) for a given "now" using
the conventions in README.md (night-owl logical day, part-of-day spans, clipping to now). The
meaning is the label; the span arithmetic is mechanical so that ~1,100 cases stay consistent.

Run: python make_phrases.py   (writes phrases.jsonl next to this file)
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Paris")
REF = date(2026, 11, 10)  # Tuesday
WEEKEND_REFS = (date(2026, 11, 10), date(2026, 11, 9), date(2026, 11, 8))  # Tue, Mon, Sun
NOWS = ("09:00", "14:00", "23:30", "00:40", "03:00")
DAY_START = 5  # logical day boundary (night owl): before 05:00 it is still "yesterday"
PARTS = {  # hours of the logical day D (>= 24 means D+1)
    "day": (5, 29),
    "morning": (6, 12),
    "noon": (12, 14),
    "afternoon": (12, 18),
    "evening": (18, 26),
    "night_fr": (22, 30),
    "night_en": (18, 30),  # EN "last night" / "Saturday night": evening and night
}
H = timedelta(hours=1)
M = timedelta(minutes=1)


# --- meaning constructors (the annotation vocabulary) ----------------------------------------
def D(which: str, part: str = "day") -> tuple:  # noqa: N802
    return ("D", which, part)


def CLK(h: int, m: int = 0, which: str | None = None) -> tuple:  # noqa: N802
    return ("CLK", h, m, which)


RECENT = ("RECENT",)
AFTER_MIDNIGHT = ("AFTER_MIDNIGHT",)


def AGO_H(n: int) -> tuple:  # noqa: N802
    return ("AGO_H", n)


def AGO_MIN(n: int) -> tuple:  # noqa: N802
    return ("AGO_MIN", n)


def LAST_H(n: int) -> tuple:  # noqa: N802
    return ("LAST_H", n)


def LAST_MIN(n: int) -> tuple:  # noqa: N802
    return ("LAST_MIN", n)


def LAST_D(n: int) -> tuple:  # noqa: N802
    return ("LAST_D", n)


def AGO_D(n: int) -> tuple:  # noqa: N802
    return ("AGO_D", n)


def WEEKEND(which: str) -> tuple:  # noqa: N802
    return ("WEEKEND", which)


def WEEK(which: str) -> tuple:  # noqa: N802
    return ("WEEK", which)


WD = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# (question, lang, category, meaning, main time text copied from the question, connector)
# connector only for two_anchor: "rel" (that/que/qui/dont/which), "when" (when/quand), "none".
P: list[tuple] = [
    # --- today -------------------------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé aujourd'hui ?", "fr", "today", D("today"), "aujourd'hui"),
    ("What videos did I watch today?", "en", "today", D("today"), "today"),
    ("C'était quoi le jeu auquel j'ai joué aujourd'hui ?", "fr", "today", D("today"), "aujourd'hui"),
    ("Did Minlo mention Elden Ring today?", "en", "today", D("today"), "today"),
    ("Qu'est-ce que Léa m'a dit aujourd'hui sur Discord ?", "fr", "today", D("today"), "aujourd'hui"),
    ("What did I listen to today on Spotify?", "en", "today", D("today"), "today"),
    ("J'ai watch quoi today sur YouTube ?", "mixed", "today", D("today"), "today"),
    ("Plus tôt aujourd'hui, Sacha parlait de quoi ?", "fr", "today", D("today"), "Plus tôt aujourd'hui"),
    ("What was I doing earlier today in VS Code?", "en", "today", D("today"), "earlier today"),
    ("Résume ma journée d'aujourd'hui", "fr", "today", D("today"), "aujourd'hui"),
    # --- this morning ------------------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé ce matin ?", "fr", "morning", D("today", "morning"), "ce matin"),
    ("What did Minlo say this morning about the patch notes?", "en", "morning", D("today", "morning"), "this morning"),
    ("C'était quoi la vidéo de ce matin sur le speedrun ?", "fr", "morning", D("today", "morning"), "ce matin"),
    ("Which game did I play this morning?", "en", "morning", D("today", "morning"), "this morning"),
    ("Ce matin, Sacha m'a envoyé quel lien sur Discord ?", "fr", "morning", D("today", "morning"), "Ce matin"),
    ("What was the song I heard this morning?", "en", "morning", D("today", "morning"), "this morning"),
    ("Qu'est-ce que Léa disait ce matin pendant l'appel ?", "fr", "morning", D("today", "morning"), "ce matin"),
    ("Le truc que j'ai lu ce matin dans Zen, c'était quoi ?", "fr", "morning", D("today", "morning"), "ce matin"),
    ("J'ai vu quoi this morning sur Twitch ?", "mixed", "morning", D("today", "morning"), "this morning"),
    ("Dans la matinée, j'ai regardé quel tuto ?", "fr", "morning", D("today", "morning"), "Dans la matinée"),
    # --- this afternoon ----------------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé cet aprèm ?", "fr", "afternoon", D("today", "afternoon"), "cet aprèm"),
    ("Qu'est-ce que Minlo a dit cet après-midi ?", "fr", "afternoon", D("today", "afternoon"), "cet après-midi"),
    ("What video did I watch this afternoon?", "en", "afternoon", D("today", "afternoon"), "this afternoon"),
    ("Cet aprem j'ai joué à quoi ?", "fr", "afternoon", D("today", "afternoon"), "Cet aprem"),
    ("Which tab was I reading this afternoon about Rust?", "en", "afternoon", D("today", "afternoon"), "this afternoon"),
    ("C'était quoi la musique de cet après-midi ?", "fr", "afternoon", D("today", "afternoon"), "cet après-midi"),
    ("Did Sacha call me this afternoon?", "en", "afternoon", D("today", "afternoon"), "this afternoon"),
    ("J'ai watch quoi cet aprèm ?", "mixed", "afternoon", D("today", "afternoon"), "cet aprèm"),
    ("What did Léa say in the afternoon call?", "en", "afternoon", D("today", "afternoon"), "in the afternoon"),
    ("Dans l'après-midi, on parlait de quel jeu avec Minlo ?", "fr", "afternoon", D("today", "afternoon"), "Dans l'après-midi"),
    # --- noon / lunch --------------------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé ce midi ?", "fr", "noon", D("today", "noon"), "ce midi"),
    ("Qu'est-ce que Sacha a dit à midi ?", "fr", "noon", D("today", "noon"), "à midi"),
    ("What did I watch at noon?", "en", "noon", CLK(12), "at noon"),
    ("What video did I watch during lunch?", "en", "noon", D("today", "noon"), "during lunch"),
    ("Ce midi en mangeant, j'ai regardé quelle vidéo ?", "fr", "noon", D("today", "noon"), "Ce midi"),
    ("Around noon, what was Minlo talking about?", "en", "noon", CLK(12), "Around noon"),
    ("À midi j'ai écouté quel podcast ?", "fr", "noon", D("today", "noon"), "À midi"),
    ("C'était quoi la vidéo at lunch ?", "mixed", "noon", D("today", "noon"), "at lunch"),
    ("Vers midi, j'étais sur quel site ?", "fr", "noon", CLK(12), "Vers midi"),
    ("What was I watching at lunchtime?", "en", "noon", D("today", "noon"), "at lunchtime"),
    # --- this evening / tonight -----------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé ce soir ?", "fr", "evening", D("today", "evening"), "ce soir"),
    ("What did Minlo say tonight?", "en", "evening", D("today", "evening"), "tonight"),
    ("Ce soir on a joué à quoi avec Sacha ?", "fr", "evening", D("today", "evening"), "Ce soir"),
    ("What video did I watch this evening?", "en", "evening", D("today", "evening"), "this evening"),
    ("C'était quoi le stream de ce soir ?", "fr", "evening", D("today", "evening"), "ce soir"),
    ("Qu'est-ce que Léa a raconté ce soir sur Discord ?", "fr", "evening", D("today", "evening"), "ce soir"),
    ("Which game did we play tonight?", "en", "evening", D("today", "evening"), "tonight"),
    ("J'ai écouté quoi tonight ?", "mixed", "evening", D("today", "evening"), "tonight"),
    ("Dans la soirée, j'ai regardé quelle vidéo ?", "fr", "evening", D("today", "evening"), "Dans la soirée"),
    ("What was that song from this evening?", "en", "evening", D("today", "evening"), "this evening"),
    ("Ce soir Minlo m'a parlé d'un jeu, c'était lequel ?", "fr", "evening", D("today", "evening"), "Ce soir"),
    ("During the evening, what did I browse?", "en", "evening", D("today", "evening"), "During the evening"),
    # --- night ---------------------------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé cette nuit ?", "fr", "night", D("cette_nuit", "night_fr"), "cette nuit"),
    ("What did I watch last night?", "en", "night", D("yesterday", "night_en"), "last night"),
    ("Qu'est-ce que Minlo a dit la nuit dernière ?", "fr", "night", D("yesterday", "night_fr"), "la nuit dernière"),
    ("What did Minlo say last night about the raid?", "en", "night", D("yesterday", "night_en"), "last night"),
    ("Cette nuit, on a joué à quoi ?", "fr", "night", D("cette_nuit", "night_fr"), "Cette nuit"),
    ("Which video was I watching last night?", "en", "night", D("yesterday", "night_en"), "last night"),
    ("C'était quoi la musique de cette nuit ?", "fr", "night", D("cette_nuit", "night_fr"), "cette nuit"),
    ("La nuit dernière j'ai regardé quoi sur YouTube ?", "fr", "night", D("yesterday", "night_fr"), "La nuit dernière"),
    ("J'ai watch quoi last night ?", "mixed", "night", D("yesterday", "night_en"), "last night"),
    ("Last night Sacha mentioned a game, which one?", "en", "night", D("yesterday", "night_en"), "Last night"),
    ("Qu'est-ce que Léa a dit cette nuit sur Discord ?", "fr", "night", D("cette_nuit", "night_fr"), "cette nuit"),
    ("What was on my screen late last night?", "en", "night", D("yesterday", "night_fr"), "late last night"),
    # --- yesterday and its parts ---------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé hier ?", "fr", "yesterday", D("yesterday"), "hier"),
    ("What video did I watch yesterday?", "en", "yesterday", D("yesterday"), "yesterday"),
    ("Qu'est-ce que Minlo a dit hier soir ?", "fr", "yesterday", D("yesterday", "evening"), "hier soir"),
    ("What did I watch yesterday afternoon?", "en", "yesterday", D("yesterday", "afternoon"), "yesterday afternoon"),
    ("Hier aprèm j'ai joué à quoi ?", "fr", "yesterday", D("yesterday", "afternoon"), "Hier aprèm"),
    ("C'était quoi la vidéo d'hier après-midi ?", "fr", "yesterday", D("yesterday", "afternoon"), "hier après-midi"),
    ("What did Sacha say yesterday morning?", "en", "yesterday", D("yesterday", "morning"), "yesterday morning"),
    ("Hier matin, j'ai lu quel article ?", "fr", "yesterday", D("yesterday", "morning"), "Hier matin"),
    ("What did Léa tell me yesterday evening?", "en", "yesterday", D("yesterday", "evening"), "yesterday evening"),
    ("Hier soir j'ai regardé quoi ?", "fr", "yesterday", D("yesterday", "evening"), "Hier soir"),
    ("J'ai joué à quoi yesterday evening ?", "mixed", "yesterday", D("yesterday", "evening"), "yesterday evening"),
    ("What was the video from yesterday about the new patch?", "en", "yesterday", D("yesterday"), "yesterday"),
    ("Hier midi, j'ai regardé quelle vidéo ?", "fr", "yesterday", D("yesterday", "noon"), "Hier midi"),
    ("Qu'est-ce qu'on s'est dit hier dans la soirée avec Minlo ?", "fr", "yesterday", D("yesterday", "evening"), "hier dans la soirée"),
    ("Yesterday at lunch, what was I watching?", "en", "yesterday", D("yesterday", "noon"), "Yesterday at lunch"),
    ("Hier aprem, Sacha m'a parlé de quoi ?", "fr", "yesterday", D("yesterday", "afternoon"), "Hier aprem"),
    # --- day before yesterday ------------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé avant-hier ?", "fr", "daybefore", D("daybefore"), "avant-hier"),
    ("What did Minlo say the day before yesterday?", "en", "daybefore", D("daybefore"), "the day before yesterday"),
    ("Avant-hier soir on a joué à quoi ?", "fr", "daybefore", D("daybefore", "evening"), "Avant-hier soir"),
    ("C'était quoi la vidéo d'avant-hier ?", "fr", "daybefore", D("daybefore"), "avant-hier"),
    ("J'ai watch quoi the day before yesterday ?", "mixed", "daybefore", D("daybefore"), "the day before yesterday"),
    ("Avant hier aprèm, Léa a dit quoi ?", "fr", "daybefore", D("daybefore", "afternoon"), "Avant hier aprèm"),
    # --- midnight ------------------------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé à minuit ?", "fr", "midnight", CLK(0), "à minuit"),
    ("What was Minlo saying around midnight?", "en", "midnight", CLK(0), "around midnight"),
    ("Hier soir vers minuit, c'était quoi la vidéo ?", "fr", "midnight", CLK(0, 0, "yesterday"), "Hier soir vers minuit"),
    ("What song was playing at midnight last night?", "en", "midnight", CLK(0, 0, "yesterday"), "at midnight last night"),
    ("Vers minuit, Sacha parlait de quoi ?", "fr", "midnight", CLK(0), "Vers minuit"),
    ("Après minuit, j'ai regardé quoi ?", "fr", "midnight", AFTER_MIDNIGHT, "Après minuit"),
    ("Who was in the call past midnight?", "en", "midnight", AFTER_MIDNIGHT, "past midnight"),
    ("J'ai watch quoi vers minuit ?", "mixed", "midnight", CLK(0), "vers minuit"),
    # --- earlier / tout à l'heure ----------------------------------------------------------------
    ("Qu'est-ce que Minlo a dit tout à l'heure ?", "fr", "earlier", RECENT, "tout à l'heure"),
    ("What was that video I watched earlier?", "en", "earlier", RECENT, "earlier"),
    ("Tout à l'heure, j'ai lu un article sur Linux, c'était lequel ?", "fr", "earlier", RECENT, "Tout à l'heure"),
    ("What did Sacha say a while ago about the tournament?", "en", "earlier", RECENT, "a while ago"),
    ("C'était quoi la musique de tout à l'heure ?", "fr", "earlier", RECENT, "tout à l'heure"),
    ("Earlier, Léa mentioned a game, which one?", "en", "earlier", RECENT, "Earlier"),
    ("Le lien que Sacha a envoyé tout à l'heure, c'était quoi ?", "fr", "earlier", RECENT, "tout à l'heure"),
    ("J'ai watch quoi tout à l'heure ?", "mixed", "earlier", RECENT, "tout à l'heure"),
    ("Il y a un moment, Minlo parlait d'un film, lequel ?", "fr", "earlier", RECENT, "Il y a un moment"),
    # --- weekend -------------------------------------------------------------------------------
    ("Qu'est-ce que j'ai joué ce week-end ?", "fr", "weekend", WEEKEND("this"), "ce week-end"),
    ("What did I watch this weekend?", "en", "weekend", WEEKEND("this"), "this weekend"),
    ("What games did I play last weekend?", "en", "weekend", WEEKEND("last"), "last weekend"),
    ("Le week-end dernier, Minlo m'a parlé de quel jeu ?", "fr", "weekend", WEEKEND("last"), "Le week-end dernier"),
    ("C'était quoi la vidéo de ce weekend sur Zelda ?", "fr", "weekend", WEEKEND("this"), "ce weekend"),
    ("Over the weekend, what did Sacha say about the tournament?", "en", "weekend", WEEKEND("this"), "Over the weekend"),
    ("J'ai watch quoi ce week-end ?", "mixed", "weekend", WEEKEND("this"), "ce week-end"),
    ("Pendant le week-end, on a parlé de quoi avec Léa ?", "fr", "weekend", WEEKEND("this"), "Pendant le week-end"),
    ("What music did I listen to last weekend?", "en", "weekend", WEEKEND("last"), "last weekend"),
    ("Qu'est-ce que j'ai regardé le week-end dernier ?", "fr", "weekend", WEEKEND("last"), "le week-end dernier"),
    # --- weekdays ------------------------------------------------------------------------------
    ("Qu'est-ce que Minlo a dit mardi soir ?", "fr", "weekday", D("wd:tue", "evening"), "mardi soir"),
    ("What did I watch last Tuesday afternoon?", "en", "weekday", D("wd:tue", "afternoon"), "last Tuesday afternoon"),
    ("Lundi soir, on a joué à quoi ?", "fr", "weekday", D("wd:mon", "evening"), "Lundi soir"),
    ("What did Sacha say on Saturday night?", "en", "weekday", D("wd:sat", "night_en"), "on Saturday night"),
    ("Samedi soir j'ai regardé quelle vidéo ?", "fr", "weekday", D("wd:sat", "evening"), "Samedi soir"),
    ("Dimanche aprèm, Léa m'a parlé de quoi ?", "fr", "weekday", D("wd:sun", "afternoon"), "Dimanche aprèm"),
    ("What video did I watch on Friday?", "en", "weekday", D("wd:fri"), "on Friday"),
    ("Vendredi soir, c'était quoi le stream ?", "fr", "weekday", D("wd:fri", "evening"), "Vendredi soir"),
    ("What did Minlo say on Monday morning?", "en", "weekday", D("wd:mon", "morning"), "on Monday morning"),
    ("Jeudi, j'ai lu quoi sur le nouveau GPU ?", "fr", "weekday", D("wd:thu"), "Jeudi"),
    ("C'était quoi la vidéo de mercredi soir ?", "fr", "weekday", D("wd:wed", "evening"), "mercredi soir"),
    ("J'ai joué à quoi on Sunday ?", "mixed", "weekday", D("wd:sun"), "on Sunday"),
    # --- clock times ---------------------------------------------------------------------------
    ("Qu'est-ce que j'ai regardé vers 22h ?", "fr", "clock", CLK(22), "vers 22h"),
    ("What was Minlo saying at 3pm?", "en", "clock", CLK(15), "at 3pm"),
    ("Hier vers 15h, j'ai regardé quoi ?", "fr", "clock", CLK(15, 0, "yesterday"), "Hier vers 15h"),
    ("Vers 21h30, Sacha a dit quoi ?", "fr", "clock", CLK(21, 30), "Vers 21h30"),
    ("What video was on around 10pm yesterday?", "en", "clock", CLK(22, 0, "yesterday"), "around 10pm yesterday"),
    ("À 14h j'étais sur quelle page ?", "fr", "clock", CLK(14), "À 14h"),
    ("At 9am, which app was I using?", "en", "clock", CLK(9), "At 9am"),
    ("Hier soir vers 23h, on parlait de quoi ?", "fr", "clock", CLK(23, 0, "yesterday"), "Hier soir vers 23h"),
    ("Cette nuit vers 2h, j'ai regardé quoi ?", "fr", "clock", CLK(2, 0, "cette_nuit"), "Cette nuit vers 2h"),
    ("Last night around 1am, what was Léa saying?", "en", "clock", CLK(1, 0, "yesterday"), "Last night around 1am"),
    ("J'ai watch quoi vers 20h ?", "mixed", "clock", CLK(20), "vers 20h"),
    ("Ce matin vers 10h, Minlo a dit quoi ?", "fr", "clock", CLK(10, 0, "today"), "Ce matin vers 10h"),
    ("Around 6pm, what was I watching?", "en", "clock", CLK(18), "Around 6pm"),
    # --- relative ranges -----------------------------------------------------------------------
    ("Qu'est-ce que Minlo a dit il y a 2 heures ?", "fr", "relative", AGO_H(2), "il y a 2 heures"),
    ("What was I watching 2 hours ago?", "en", "relative", AGO_H(2), "2 hours ago"),
    ("Dans la dernière heure, qu'est-ce que Sacha a dit ?", "fr", "relative", LAST_H(1), "Dans la dernière heure"),
    ("What did Léa say in the last hour?", "en", "relative", LAST_H(1), "in the last hour"),
    ("Il y a une heure, j'écoutais quoi ?", "fr", "relative", AGO_H(1), "Il y a une heure"),
    ("What did I watch in the past 3 hours?", "en", "relative", LAST_H(3), "in the past 3 hours"),
    ("Il y a 20 minutes, Minlo parlait de quoi ?", "fr", "relative", AGO_MIN(20), "Il y a 20 minutes"),
    ("Ces trois derniers jours, j'ai joué à quoi ?", "fr", "relative", LAST_D(3), "Ces trois derniers jours"),
    ("What videos did I watch 3 days ago?", "en", "relative", AGO_D(3), "3 days ago"),
    ("Cette semaine, Sacha m'a parlé de quel jeu ?", "fr", "relative", WEEK("this"), "Cette semaine"),
    ("What did I watch last week about Hollow Knight?", "en", "relative", WEEK("last"), "last week"),
    ("J'ai watch quoi in the last 30 minutes ?", "mixed", "relative", LAST_MIN(30), "in the last 30 minutes"),
    # --- two anchors: main-clause anchor FIRST -------------------------------------------------
    ("What video did I watch yesterday afternoon that Minlo told me about this morning?", "en", "two_anchor", D("yesterday", "afternoon"), "yesterday afternoon", "rel"),
    ("Quelle vidéo j'ai regardée hier aprèm que Minlo m'a conseillée ce matin ?", "fr", "two_anchor", D("yesterday", "afternoon"), "hier aprèm", "rel"),
    ("What did Sacha say last night about the game I played this afternoon?", "en", "two_anchor", D("yesterday", "night_en"), "last night", "none"),
    ("Qu'est-ce que Léa a dit hier soir sur la vidéo que j'ai regardée ce matin ?", "fr", "two_anchor", D("yesterday", "evening"), "hier soir", "rel"),
    ("Which game did I play this weekend that Sacha mentioned on Friday?", "en", "two_anchor", WEEKEND("this"), "this weekend", "rel"),
    ("C'était quoi la vidéo de ce matin dont Minlo parlait hier soir ?", "fr", "two_anchor", D("today", "morning"), "ce matin", "rel"),
    ("What did Minlo say this morning about the video I watched last night?", "en", "two_anchor", D("today", "morning"), "this morning", "none"),
    ("Qu'est-ce que Sacha a raconté ce midi sur le match qu'on a joué hier soir ?", "fr", "two_anchor", D("today", "noon"), "ce midi", "rel"),
    ("Which video did I watch tonight that Léa sent me yesterday?", "en", "two_anchor", D("today", "evening"), "tonight", "rel"),
    ("What did I watch at noon that Minlo recommended last weekend?", "en", "two_anchor", CLK(12), "at noon", "rel"),
    ("Qu'est-ce que j'ai regardé vers 22h que Sacha m'avait envoyé avant-hier ?", "fr", "two_anchor", CLK(22), "vers 22h", "rel"),
    ("Qu'est-ce que Minlo a dit tout à l'heure à propos de la vidéo d'hier soir ?", "fr", "two_anchor", RECENT, "tout à l'heure", "none"),
    ("What did Léa say earlier about the stream from yesterday?", "en", "two_anchor", RECENT, "earlier", "none"),
    ("Quelle vidéo j'ai vue ce matin qui parlait du jeu de samedi soir ?", "fr", "two_anchor", D("today", "morning"), "ce matin", "rel"),
    ("Which video did I watch yesterday morning which Sacha talked about on Sunday?", "en", "two_anchor", D("yesterday", "morning"), "yesterday morning", "rel"),
    ("Qu'est-ce que Sacha a dit cette nuit quand on regardait la vidéo de ce midi ?", "fr", "two_anchor", D("cette_nuit", "night_fr"), "cette nuit", "when"),
    ("What did Minlo say last night when we were talking about the video from this morning?", "en", "two_anchor", D("yesterday", "night_en"), "last night", "when"),
    ("Qu'est-ce que Léa a dit hier soir quand je lui ai montré la vidéo de ce matin ?", "fr", "two_anchor", D("yesterday", "evening"), "hier soir", "when"),
    ("J'ai watch quoi yesterday evening que Minlo m'a recommandé this morning ?", "mixed", "two_anchor", D("yesterday", "evening"), "yesterday evening", "rel"),
    ("What did Sacha say on Monday morning about the game we played this weekend?", "en", "two_anchor", D("wd:mon", "morning"), "on Monday morning", "none"),
    ("Qu'est-ce que Minlo a dit hier aprèm quand on jouait à Valorant ce matin ?", "fr", "two_anchor", D("yesterday", "afternoon"), "hier aprèm", "when"),
    ("What video did I watch yesterday afternoon Minlo told me about this morning?", "en", "two_anchor", D("yesterday", "afternoon"), "yesterday afternoon", "none"),
    ("La vidéo regardée hier soir, Minlo en a parlé ce matin, c'était laquelle ?", "fr", "two_anchor", D("yesterday", "evening"), "hier soir", "none"),
    ("The game I played this afternoon, Sacha talked about it last night, what was it?", "en", "two_anchor", D("today", "afternoon"), "this afternoon", "none"),
    ("Le stream de ce soir, Léa l'a mentionné hier, c'était quoi ?", "fr", "two_anchor", D("today", "evening"), "ce soir", "none"),
    ("The song from last night, Minlo sent it this morning, what's the title?", "en", "two_anchor", D("yesterday", "night_en"), "last night", "none"),
    ("Ce midi j'ai regardé une vidéo, Sacha me l'avait conseillée hier soir, laquelle ?", "fr", "two_anchor", D("today", "noon"), "Ce midi", "none"),
    ("Yesterday evening I watched a video Minlo mentioned this morning, which one?", "en", "two_anchor", D("yesterday", "evening"), "Yesterday evening", "none"),
    ("Cet aprèm j'ai joué à un jeu, Léa en parlait hier soir, c'était quoi ?", "fr", "two_anchor", D("today", "afternoon"), "Cet aprèm", "none"),
    ("J'ai watch une vidéo this morning, Minlo l'avait recommandée yesterday, c'était laquelle ?", "mixed", "two_anchor", D("today", "morning"), "this morning", "none"),
    ("Ce midi, Sacha a dit quoi sur la vidéo que j'ai regardée hier soir ?", "fr", "two_anchor", D("today", "noon"), "Ce midi", "rel"),
    ("What did Léa say at lunch about the video I watched last night?", "en", "two_anchor", D("today", "noon"), "at lunch", "none"),
    # --- two anchors: main-clause anchor LAST --------------------------------------------------
    ("Of the videos Minlo mentioned this morning, which did I watch yesterday afternoon?", "en", "two_anchor", D("yesterday", "afternoon"), "yesterday afternoon", "rel"),
    ("Le jeu dont Sacha a parlé ce matin, j'y ai joué hier soir ?", "fr", "two_anchor", D("yesterday", "evening"), "hier soir", "rel"),
    ("Le jeu auquel j'ai joué cet aprèm, Sacha m'en avait parlé hier ?", "fr", "two_anchor", D("yesterday"), "hier", "rel"),
    ("The video Minlo sent this morning, did I watch it last night?", "en", "two_anchor", D("yesterday", "night_en"), "last night", "none"),
    ("La vidéo que Léa m'a envoyée hier, je l'ai regardée ce soir ?", "fr", "two_anchor", D("today", "evening"), "ce soir", "rel"),
    ("The game that Sacha mentioned on Friday, did we play it this weekend?", "en", "two_anchor", WEEKEND("this"), "this weekend", "rel"),
    ("Le morceau que Sacha a partagé avant-hier, je l'ai écouté cette nuit ?", "fr", "two_anchor", D("cette_nuit", "night_fr"), "cette nuit", "rel"),
    ("The tutorial that Léa recommended yesterday, did I watch it this afternoon?", "en", "two_anchor", D("today", "afternoon"), "this afternoon", "rel"),
    ("Le jeu qu'on a lancé hier soir, Léa en a parlé ce midi ?", "fr", "two_anchor", D("today", "noon"), "ce midi", "rel"),
    ("That video I watched last night, what did Minlo say about it this afternoon?", "en", "two_anchor", D("today", "afternoon"), "this afternoon", "none"),
    ("La vidéo d'hier aprèm, qu'est-ce que Sacha en a dit ce soir ?", "fr", "two_anchor", D("today", "evening"), "ce soir", "none"),
    ("The game from this weekend, what did Léa say about it yesterday evening?", "en", "two_anchor", D("yesterday", "evening"), "yesterday evening", "none"),
    ("Le stream de samedi soir, qu'est-ce que Minlo en a dit tout à l'heure ?", "fr", "two_anchor", RECENT, "tout à l'heure", "none"),
    ("The song I heard this morning, did Sacha send it to me last night?", "en", "two_anchor", D("yesterday", "night_en"), "last night", "none"),
    ("Ce que j'ai regardé ce matin, Minlo m'en a parlé quand hier soir ?", "fr", "two_anchor", D("yesterday", "evening"), "hier soir", "rel"),
    ("La vidéo d'avant-hier, j'en ai parlé avec Léa hier soir ?", "fr", "two_anchor", D("yesterday", "evening"), "hier soir", "none"),
    ("Quand Minlo m'a montré ce clip ce matin, je l'avais déjà vu hier ?", "fr", "two_anchor", D("yesterday"), "hier", "when"),
    ("When Sacha brought up that game this afternoon, had I played it last night?", "en", "two_anchor", D("yesterday", "night_en"), "last night", "when"),
    ("The video that Minlo talked about yesterday, did I finish it this morning?", "en", "two_anchor", D("today", "morning"), "this morning", "rel"),
    ("La musique qui passait hier soir, Sacha l'a citée ce matin ?", "fr", "two_anchor", D("today", "morning"), "ce matin", "rel"),
    ("The clip from yesterday, which one did I rewatch earlier today?", "en", "two_anchor", D("today"), "earlier today", "none"),
    ("Le tuto dont Léa parlait avant-hier, je l'ai regardé hier aprèm ?", "fr", "two_anchor", D("yesterday", "afternoon"), "hier aprèm", "rel"),
    # --- time-free questions with time-like topic words -----------------------------------------
    ("What did Minlo say about the Night King?", "en", "time_free", None, None),
    ("Which Weekend Update sketch did I watch?", "en", "time_free", None, None),
    ("C'était quoi la vidéo sur Midnight Club ?", "fr", "time_free", None, None),
    ("Qu'est-ce que Sacha pensait de Five Nights at Freddy's ?", "fr", "time_free", None, None),
    ("What did Léa say about Night City in Cyberpunk?", "en", "time_free", None, None),
    ("Which Saturday Night Live clip did Minlo send me?", "en", "time_free", None, None),
    ("C'était quoi l'épisode de Last Week Tonight sur l'IA ?", "fr", "time_free", None, None),
    ("Did I watch the Tonight Show interview with that actor?", "en", "time_free", None, None),
    ("Qu'est-ce que Minlo disait sur Midnight Suns ?", "fr", "time_free", None, None),
    ("J'ai regardé quoi sur la chaîne Le Matin ?", "fr", "time_free", None, None),
    ("Which cover of Yesterday did Sacha share?", "en", "time_free", None, None),
    ("Qu'est-ce que Léa a dit sur Les 12 coups de midi ?", "fr", "time_free", None, None),
    ("What was the Good Morning America segment about?", "en", "time_free", None, None),
    ("Qu'est-ce que Minlo a dit à propos de Dead by Daylight ?", "fr", "time_free", None, None),
    ("Did Sacha mention Night in the Woods?", "en", "time_free", None, None),
    ("C'était quoi l'article du Midi Libre que j'ai lu ?", "fr", "time_free", None, None),
    ("What did I read about the film Minuit à Paris?", "mixed", "time_free", None, None),
    ("Qu'est-ce que Léa a dit de la chanson Hier encore ?", "fr", "time_free", None, None),
    ("Which episode of The Night Agent did I watch?", "en", "time_free", None, None),
    ("C'était quoi la vidéo sur Monday Night Raw ?", "mixed", "time_free", None, None),
    ("What did Minlo say about Friday Night Funkin?", "en", "time_free", None, None),
    ("Qu'est-ce que Sacha a dit sur le mode nuit de Discord ?", "fr", "time_free", None, None),
    ("What was the Evening Standard article Léa sent?", "en", "time_free", None, None),
    ("C'était quoi la playlist Midnight Lofi que Minlo m'a passée ?", "mixed", "time_free", None, None),
    ("Did I watch A Hard Day's Night?", "en", "time_free", None, None),
    ("J'ai regardé quel extrait de Vendredi tout est permis ?", "fr", "time_free", None, None),
    ("C'est quoi la vidéo Morning routine que j'ai regardée ?", "mixed", "time_free", None, None),
    ("What did Léa say about The Weeknd's new album?", "en", "time_free", None, None),
    ("Which Night Owl podcast episode did Sacha recommend?", "en", "time_free", None, None),
    ("Qu'est-ce que Minlo a dit sur le jeu Afternoon Tea Party ?", "mixed", "time_free", None, None),
]  # fmt: skip


# --- gold arithmetic (README "Conventions") ---------------------------------------------------
def at(d: date, h: int, m: int = 0) -> datetime:
    extra, h = divmod(h, 24)
    return datetime.combine(d + timedelta(days=extra), time(h, m), tzinfo=TZ)


def logical_today(now: datetime) -> date:
    return now.date() if now.hour >= DAY_START else now.date() - timedelta(days=1)


def _day(which: str, now: datetime) -> date:
    lt = logical_today(now)
    if which == "today":
        return lt
    if which == "yesterday":
        return lt - timedelta(days=1)
    if which == "daybefore":
        return lt - timedelta(days=2)
    if which == "cette_nuit":  # the night in progress before 05:00, else the past night
        return lt if now.hour < DAY_START else lt - timedelta(days=1)
    if which.startswith("wd:"):  # most recent such calendar day strictly before calendar today
        k = WD[which[3:]]
        back = (now.date().weekday() - k) % 7 or 7
        return now.date() - timedelta(days=back)
    raise ValueError(which)


def _clip(s: datetime, e: datetime, now: datetime) -> tuple[datetime, datetime]:
    e = min(e, now)
    if s >= e:
        msg = f"empty or future gold span {s}..{e} at {now}"
        raise AssertionError(msg)
    return s, e


def gold(meaning: tuple | None, now: datetime) -> tuple[datetime, datetime] | None:  # noqa: C901, PLR0911, PLR0912
    if meaning is None:
        return None
    kind = meaning[0]
    if kind == "D":
        _, which, part = meaning
        d = _day(which, now)
        a, b = PARTS[part]
        if which == "today" and at(d, a) >= now:  # not started yet: the most recent one
            d -= timedelta(days=1)
        return _clip(at(d, a), at(d, b), now)
    if kind == "CLK":
        _, h, m, which = meaning
        hh = h + 24 if h < DAY_START else h  # small hours belong to the evening before
        if which is not None:
            d = _day(which, now)
            t = at(d, hh, m)
            if which in ("today", "cette_nuit") and t - 30 * M >= now:  # not reached yet
                t = at(d - timedelta(days=1), hh, m)
        else:  # most recent occurrence that has started
            t = next(
                at(now.date() + timedelta(days=k), h, m)
                for k in (0, -1, -2)
                if at(now.date() + timedelta(days=k), h, m) - 30 * M < now
            )
        return _clip(t - 30 * M, t + 30 * M, now)
    if kind == "AFTER_MIDNIGHT":
        return _clip(at(now.date(), 0), at(now.date(), 6), now)
    if kind == "RECENT":
        return now - 3 * H, now
    if kind == "AGO_H":
        c = now - meaning[1] * H
        return c - 30 * M, c + 30 * M
    if kind == "AGO_MIN":
        c = now - meaning[1] * M
        return c - 10 * M, c + 10 * M
    if kind == "LAST_H":
        return now - meaning[1] * H, now
    if kind == "LAST_MIN":
        return now - meaning[1] * M, now
    if kind == "LAST_D":
        return now - timedelta(days=meaning[1]), now
    if kind == "AGO_D":
        d = logical_today(now) - timedelta(days=meaning[1])
        return _clip(at(d, 5), at(d, 29), now)
    if kind == "WEEKEND":
        lt = logical_today(now)
        if lt.weekday() >= 5:  # inside a weekend
            sat = lt - timedelta(days=lt.weekday() - 5)
            if meaning[1] == "this":
                return _clip(at(sat, 5), now, now)
            sat -= timedelta(days=7)
        else:
            sat = lt - timedelta(days=(lt.weekday() - 5) % 7)
        return _clip(at(sat, 5), at(sat + timedelta(days=2), 5), now)
    if kind == "WEEK":
        lt = logical_today(now)
        monday = lt - timedelta(days=lt.weekday())
        if meaning[1] == "this":
            return _clip(at(monday, 5), now, now)
        return _clip(at(monday - timedelta(days=7), 5), at(monday, 5), now)
    raise ValueError(kind)


def describe(meaning: tuple | None) -> str | None:
    if meaning is None:
        return None
    return ":".join(str(x) for x in meaning if x is not None)


def now_at(ref: date, label: str) -> datetime:
    h, m = map(int, label.split(":"))
    return datetime.combine(ref, time(h, m), tzinfo=TZ)


def split_of(question: str, category: str, rank: dict[str, list[str]]) -> str:
    members = rank[category]
    return "dev" if members.index(question) < round(0.6 * len(members)) else "heldout"


def main() -> None:
    seen: set[str] = set()
    rank: dict[str, list[str]] = {}
    for q, _, cat, *_ in P:
        assert q not in seen, q
        seen.add(q)
        rank.setdefault(cat, []).append(q)
    for cat in rank:
        rank[cat].sort(key=lambda q: hashlib.sha1(q.encode()).hexdigest())
    out = HERE / "phrases.jsonl"
    n_cases = 0
    with out.open("w", encoding="utf-8") as fh:
        for i, row in enumerate(P):
            q, lang, cat, meaning, main = row[:5]
            connector = row[5] if len(row) > 5 else None  # noqa: PLR2004
            if main is not None:
                assert main in q, (q, main)
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
            rec = {
                "id": f"q{i:03d}",
                "question": q,
                "lang": lang,
                "category": cat,
                "split": split_of(q, cat, rank),
                "meaning": describe(meaning),
                "main_time_text": main,
                "connector": connector,
                "labels": labels,
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    counts: dict[str, int] = {}
    for _, _, cat, *_ in P:
        counts[cat] = counts.get(cat, 0) + 1
    print(f"{len(P)} questions, {n_cases} cases -> {out}")
    print(counts)


if __name__ == "__main__":
    main()
