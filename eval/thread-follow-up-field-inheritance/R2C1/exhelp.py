"""Print heuristic plans (S, E) and R0 routing for the prompt's hand-written examples."""
import sys
sys.path.insert(0, "..")
import harness  # noqa: E402
import candidate as c  # noqa: E402

EX = [
    ("What did Sacha say about the dungeon update yesterday evening?", "Show me everything about the Kubernetes talk"),
    ("Qu'est-ce que j'ai regardé hier soir sur Twitch ?", "Montre-moi mes notes sur la recette de lasagnes"),
    ("What did I watch on YouTube this morning?", "Et mardi ?"),
    ("What did Minlo say about the keyboard he bought last night?", "Did I answer him?"),
    ("Qu'est-ce que Léa a raconté sur le marché de Noël dimanche ?", "Ok, détaille un peu stp"),
    ("What did Minlo tell me about the chess tournament this morning?", "give me the long version"),
    ("Qu'est-ce que Sacha a dit sur le patch de Deadlock ce midi ?", "Il a parlé de quoi d'autre ?"),
    ("What did I read about chess openings on Sunday?", "À part ça, j'ai lu quoi ?"),
    ("What did Minlo say during the call on Friday night?", "And right after that, what did I watch?"),
    ("Qu'est-ce que Minlo a dit pendant l'appel hier matin ?", "Le même jour, j'ai regardé quelles vidéos ?"),
    ("Qu'est-ce que Léa m'a dit sur la panne de wifi lundi ?", "J'ai cherché des tutos là-dessus après ?"),
    ("What did Sacha say about the dungeon update yesterday?", "What about Minlo?"),
    ("Qu'est-ce que Sacha a dit sur le marché de Noël hier ?", "Et Léa, what did she think?"),
    ("What did Minlo tell me about the chess tournament this morning?", "What did he say about the prize money?"),
    ("Qu'est-ce que Léa a dit sur le déménagement samedi ?", "Elle a dit quoi sur le budget ?"),
    ("What did I watch about volcanoes?", "C'était quand, ça ?"),
    ("Qu'est-ce que j'ai acheté sur Amazon hier ?", "Ça valait le coup ?"),
    ("What did Sacha say about the server crash this morning?", "did he fix the backups too"),
    ("What did I watch on YouTube yesterday afternoon?", "and what was the song in it"),
    ("Qu'est-ce que Minlo a dit hier sur le raid ?", "Is that new mouse I ordered here yet?"),
]
for prev, new in EX:
    e, s = harness.plan_alone(prev), harness.plan_alone(new)
    r0 = c.r0(new, s)
    print(f"{prev!r} -> {new!r}  R0={r0}")
    for f in harness.FIELDS:
        sv, ev = harness.value(s, f), harness.value(e, f)
        if sv != ev:
            print(f"   {c._line(f, e, s, harness.TZ)}")
