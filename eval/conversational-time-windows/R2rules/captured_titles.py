"""Simulated captured-title fixture for the R2rules gazetteer (dev only, synthetic).

The harness has no capture database, so this module stands in for the distinct titles that
Akashic would have captured (media_sessions.title, episode titles, window/page titles).

PUBLIC_TITLES: real titles that contain a French or English time word (TV shows, songs, albums,
games, films, books, YouTube channels / podcasts), listed from public knowledge at dev time.
It is a stand-in for "the user watched/heard/played popular media"; it was not filtered against
any question set. Note: it naturally contains famous titles that also appear in phrases.jsonl and
heldout2.jsonl (e.g. "Saturday Night Live", "The Tonight Show", "Weekend at Bernie's"), so the
public-only score on those two seen sets is optimistic; only an unseen set measures it.

set_titles(path): the switch that simulates "the user captured the titles asked about". For every
time_free question of the scored set, it extracts the title-like n-gram around each time word with
a fixed rule (see the docstring) and returns those n-grams. It reads only the question text.

Production (not built here): the titles come from one read-only query over media_sessions /
episodes at process start, kept in RAM only (never written to disk), and rebuilt per process when
`PRAGMA data_version` (or the process's own write counter, since data_version does not change for
the connection's own commits) differs from the value seen at the last build: capture appends,
retention and deletions therefore take effect on the next question in every reader process.
"""

from __future__ import annotations

PUBLIC_TITLES: tuple[str, ...] = (
    # --- English TV shows / series
    "Saturday Night Live", "SNL Weekend Update", "Weekend Update", "The Tonight Show",
    "The Tonight Show Starring Jimmy Fallon", "The Tonight Show with Jay Leno",
    "Tonight Show with Johnny Carson", "Late Night with Seth Meyers",
    "Late Night with Conan O'Brien", "Late Night with David Letterman",
    "Last Week Tonight", "Last Week Tonight with John Oliver", "The Late Late Show",
    "The Morning Show", "Morning Joe", "This Morning", "The Today Show", "Today Show",
    "CBS Sunday Morning", "CBS Mornings", "Sunday Night Football", "Monday Night Football",
    "Thursday Night Football", "Monday Night Raw", "Friday Night SmackDown",
    "Saturday Night's Main Event", "Friday Night Lights", "Friday Night Dinner",
    "Wednesday", "Wednesday Addams", "The Night Agent", "The Night Manager", "The Night Of",
    "Night Court", "Midnight Mass", "The Midnight Club", "Midnight Diner",
    "Midnight Diner: Tokyo Stories", "The Midnight Gospel", "Midnight Sun", 
    "Sunday Night Baseball", "Saturday Night Takeaway", "Ant and Dec's Saturday Night Takeaway",
    "Friday Night Live", "Sunday Night at the Palladium", "Monday Mornings",
    "Sunday Morning Live", "Saturday Kitchen", "Sunday Brunch", "Good Morning Britain",
    "Breakfast at Tiffany's", "Twin Peaks: The Return", "The Evening Show",
    "PBS NewsHour Weekend", "Weekend Edition", "Weekend Edition Sunday", "Morning Edition",
    "Evening News", "NBC Nightly News", "ABC World News Tonight", "World News Tonight",
    "CBS Evening News", "The Tonight Show Band", "Night Gallery", "The Night Stalker",
    "Kolchak: The Night Stalker", "After Midnight", "Midnight Caller", "The Night Shift",
    "Midnight, Texas", 
    # --- English films
    "Saturday Night Fever", "Weekend at Bernie's", "Weekend at Bernie's II", "The Lost Weekend",
    "Midnight in Paris", "Midnight Cowboy", "Midnight Express", "Midnight Run",
    "Midnight Special", "Midnight in the Garden of Good and Evil", "Before Midnight",
    "A Hard Day's Night", "Night of the Living Dead", "Night at the Museum",
    "A Night at the Opera", "A Night at the Roxbury", "The Night Before",
    "The Night Before Christmas", "Night on Earth", "Night Moves", "Nightcrawler",
    "Freaky Friday", "His Girl Friday", "Friday the 13th", "Friday the 13th Part 2",
    "Friday", "Next Friday", "Friday After Next", "Any Given Sunday", "Never on Sunday",
    "Bloody Sunday", "Sunday Bloody Sunday", "Tuesdays with Morrie", "Ruby Tuesday",
    "The Man Who Was Thursday", "The Thursday Murder Club", "Monday", "Blue Monday",
    "The Day After Tomorrow", "Tomorrow Never Dies", "The Evening Star", "One Night in Miami",
    "One Night Only", "Last Night in Soho", "Last Night", "Tonight and Every Night",
    "Night and the City", "In the Heat of the Night", "The Night of the Hunter",
    "Night of the Demon", "The Long Good Friday", "Sunday in the Park with George",
    "Saturday Night and Sunday Morning", "Late Night", "Late Night with the Devil",
    "Midnight Sky", "The Midnight Sky", "Morning Glory", "Good Morning, Vietnam",
    "Wednesday Morning", "Tuesday", "Monday Morning", "Afternoon Delight",
    "Dog Day Afternoon", "Night Swim", "Night Train", "Night Shift", "All Night Long",
    "Evening", "Before Sunrise", "Before Sunset", "The Night Watch", "Night Watch",
    "Twelfth Night", "A Midsummer Night's Dream", "Fright Night", "Prom Night",
    "Silent Night, Deadly Night", "Date Night", "Game Night", "Movie Night",
    "Big Night", "Boogie Nights", "Night Hunter", "Midnight Madness", "Weekend",
    "Lost Weekend", "Wild Weekend", "The Weekend Away", "Long Weekend",
    # --- English songs / albums / artists
    "Saturday Night", "Saturday Night's Alright for Fighting", "Saturday in the Park",
    "Sunday Morning", "Sunday Girl", "Lazy Sunday", "Sunday Candy", "Manic Monday",
    "Monday Monday", "Rainy Days and Mondays", "I Don't Like Mondays", "Ruby Tuesday",
    "Tuesday's Gone", "Wednesday Morning, 3 A.M.", "Friday I'm in Love",
    "Friday on My Mind", "Last Friday Night", "Thank God It's Friday", "Friday Night",
    "Saturday Sun", "Sunday Best", "Lazing on a Sunday Afternoon", "Morning Has Broken",
    "(What's the Story) Morning Glory?", "Morning Train", "Morning Song", "Morning Star",
    "Tonight Tonight", "Tonight, Tonight", "Tonight I'm Lovin' You", "Tonight's the Night",
    "In the Air Tonight", "Not Tonight", "Tonight I Celebrate My Love", 
    "Night Fever", "Night Moves", "Nights in White Satin", "Night Changes", "One More Night",
    "Last Night a DJ Saved My Life", "All Night Long (All Night)", "Midnight City",
    "Midnight Rain", "Midnight Train to Georgia", "Midnight Rider", "Midnights",
    "Midnight Memories", "Midnight Sky", "Midnight Oil", "After Midnight", "Midnight Blue",
    "Summer Nights", "Starry Starry Night", "Starry Night", "Yesterday", "Yesterday Once More",
    "Yesterdays", "Today", "Weekend", "The Weekend", "Weekend Wars",
    "Weekend in New England", "Wild Weekend", "Saturday Love", "Sunday Sunday",
    "Wednesday Night Prayer Meeting", "Thursday's Child", "Friday Night Lights",
    "Morning After", "The Morning After", "Mornin' Train", "Good Morning Good Morning",
    "Evening Falls", "Midnight Special", "Tonight We Ride", "Tonight Belongs to You",
    "Tuesday Afternoon",
    "Afternoon Delight", "Blue Morning, Blue Day", "Night Owl", "Night and Day",
    "After Hours", "Late Night Talking", "Late Night Feelings", "One Night in Bangkok",
    "Tonight Is the Night", "Midnight Hour", "In the Midnight Hour", "Thursday",
    # --- games
    "Five Nights at Freddy's", "Five Nights at Freddy's: Security Breach", "Night in the Woods",
    "Friday Night Funkin'", "Monday Night Combat", "Midnight Club", "Midnight Club 3",
    "Midnight Club: Los Angeles", "Marvel's Midnight Suns", "Midnight Suns",
    "Midnight Fight Express", "Midnight Ghost Hunt", "Midnight Protocol", "Night Trap",
    "Saturday Morning RPG", "Sunday Gold", "Tonight We Riot", "Late Shift", "Night School",
    "The Night of the Rabbit", "Night of the Consumers", "Dead by Daylight", "Dying Light",
    "Elden Ring Nightreign", "Nightreign", "Night City", "Night King", "Into the Night",
    "Morning Star", "The Evening Star", "Friday the 13th: The Game",
    "Until Dawn", 
    # --- English podcasts / YouTube channels / press
    "Morning Brew", "Morning Brew Daily", "Night Mind",
    "Evening Standard", "Manchester Evening News",
    "London Evening Standard", "The Morning Call", "Sunday Times", "The Sunday Times",
    "Sunday Telegraph", 
    "Monday Morning Podcast", "Morning Wire",
    "Weekend Warrior", "Friday Night Fights", "Sunday Service", "Night Vale",
    "Welcome to Night Vale", 
    # --- French TV / radio / press
    "Les 12 coups de midi", "Les Douze Coups de midi", "Midi Libre", "Midi en France",
    "Le Soir", "Soir 3", "Le Grand Soir", "Ce soir ou jamais",
    "Le Matin", "Les Matins de France Culture", "Les Matins", "Matin Première",
    "Le Matin des magiciens", "La Matinale", "Télématin", "Bonjour la France",
    "Aujourd'hui madame", "Aujourd'hui en France", "Le Journal de 20h", "Le 13h",
    "Vendredi tout est permis", "Vendredi 13", "Samedi soir sur la Terre",
    "Samedi soir, dimanche matin", "Dimanche Martin", "Dimanche en politique",
    "Le Lundi au soleil", "Mardi gras", "Jeudi Investigation", "Mercredi",
    "Week-end à Zuydcoote", "Le Week-end", "Les Nuits de France Culture", "Nuit blanche",
    "La Nuit américaine", "La Nuit des rois", "Nuit et Brouillard", "La Nuit du chasseur",
    "Les Nuits de la pleine lune", "Les Nuits fauves", "Une nuit au musée",
    "Une nuit à l'opéra", "Nuit de folie", "Début de soirée", 
    "Les Soirées de Paris", "Minuit, Chrétiens", "Minuit chrétiens", "Minuit à Paris",
    "Minuit dans le jardin du bien et du mal", "Minuit moins le quart", "Hier encore",
    "Hier, aujourd'hui, demain", "Demain nous appartient", "Le Jour d'après",
    "La Nuit des longs couteaux", "Le Soir d'un grand jour", "Nuit debout", "Nuits sonores",
    "La Soirée de l'étrange", 
    "Midi les zouzous", "Mardi Cinéma",
    "Dimanche à la campagne", "Un dimanche à la campagne",
    "Vendredi ou la Vie sauvage", "Vendredi ou les Limbes du Pacifique",
    "La Soirée de l'Eurovision", 
    # --- French songs / films / books / games
    "Je t'attends ce soir", "Ce soir je serai la plus belle pour aller danser",
    "Hier encore j'avais vingt ans", "Samedi soir", "Dimanche", 
    "Nuit magique", "Nuit de Chine", "Les Nuits d'une demoiselle", 
    "Tous les matins du monde", 
    
    "Le Dimanche des Rameaux", "Mardi soir",
    "Les Visiteurs du soir", "Belle de jour", "Nuit d'ivresse",
    "La Nuit de Varennes", "La Nuit du carrefour", "Les Matins infidèles",
    "Midi Minuit", "Minuit Express",
    # --- additions (real titles)
    "Saturday Morning", "Sunday Morning Coming Down", "Sunday Kind of Love", "Gloomy Sunday",
    "Tuesday Night Music Club", "Morning Phase", "Morning View", "Night Visions",
    "Midnight Marauders", "Saturday Night Wrist", "Night Time, My Time", "Midnight Vultures",
    "Sunday Morning Call", "Tonight You Belong to Me", "Midnight Confessions",
    "Midnight at the Oasis", "Late in the Evening", "Morning Dew", "Mr. Saturday Night",
    "Night Call", "Midnight Murder Club", "Night of the Comet", "Night of the Creeps",
    "Night of the Lepus", "The Night Clerk", "Midnight Lace", "Tuesday, After Christmas",
    "Sunday Lovers", "Ma nuit chez Maud", "La Nuit de l'iguane", "Les Nuits de Cabiria",
    "Une nuit en enfer", "La Nuit du 12", "Minuit, quai de Bercy",
    "Les Dimanches de Ville d'Avray", "La Nuit des héros", "Saturday Night Special",
    "Late Night Alumni", "Night Owls", "Thursday Night Smackdown", "Sunday Night Heat",
    "Mondays at Racine", "Wednesday's Child",
)


def set_titles(path, tokenize, time_tokens, function_words, media_nouns):  # noqa: ANN001, ANN201
    """Title n-grams of the time_free questions of a scored set (the "user captured them" switch).

    Rule, fixed before scoring: split the question at punctuation and tokenize each piece like
    the gazetteer does; for each token that is a time word, take the n-gram from that token to
    the nearest following content word (at most two prepositions/articles in between: "weekend at
    bernie's"); if there is none, the nearest preceding content word the same way ("coups de
    midi"), unless it is a generic media noun ("episode", "video", "song", ...), which names
    the kind of item, not its title. Questions without such an n-gram contribute nothing (a
    single-word title such as "Wednesday" cannot be represented, as in production).
    """
    import json
    import re

    def content(w):  # noqa: ANN001, ANN202
        return w not in function_words and w not in time_tokens

    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if rec.get("category") != "time_free":
                continue
            for chunk in re.split(r"[,;:?!.()\"«»]", rec["question"]):
                out += _chunk_grams(tokenize(chunk), content, time_tokens, media_nouns)
    return out


# function words allowed inside a title n-gram ("weekend at bernie's", "coups de midi")
_LINKS = frozenset("at in of on the de du des la le les a au aux en".split())


def _chunk_grams(words, content, time_tokens, media_nouns):  # noqa: ANN001, ANN202
    out = []
    for i, w in enumerate(words):
        if w not in time_tokens:
            continue
        gram = None
        for j in (i + 1, i + 2, i + 3):
            if j < len(words) and content(words[j]):
                if all(x in _LINKS for x in words[i + 1 : j]):
                    gram = words[i : j + 1]
                break
        if gram is None:
            for j in (i - 1, i - 2):
                if j >= 0 and content(words[j]):
                    if words[j] not in media_nouns and all(x in _LINKS for x in words[j + 1 : i]):
                        gram = words[j : i + 1]
                    break
        if gram is not None:
            out.append(" ".join(gram))
    return out
