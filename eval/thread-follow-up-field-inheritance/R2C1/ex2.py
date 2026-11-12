import sys
sys.path.insert(0, "..")
import harness
import candidate as c
EX = [
    ("What did Minlo say about the chess tournament this morning?", "Résumé de ma semaine stp"),
    ("Qu'est-ce que j'ai regardé hier ?", "Quick recap de ma journée ?"),
    ("What did Léa say about the wedding yesterday?", "And last week?"),
    ("Qu'est-ce que Léa a dit sur sa nouvelle guitare samedi ?", "Elle l'a achetée où ?"),
    ("What did I read about chess openings on Sunday?", "Ensuite j'ai lu des articles là-dessus ?"),
    ("What did I read about chess openings on Sunday?", "Then did I watch a video about that?"),
    ("J'ai regardé quoi ce matin ?", "Et sinon, what did Minlo say about the raid yesterday?"),
    ("What stream did I watch last night?", "What was that stream talking about?"),
    ("Qu'est-ce que j'ai regardé sur YouTube dimanche ?", "C'était qui, le youtubeur ?"),
    ("What did Sacha say about the server crash?", "when did that happen?"),
    ("Qu'est-ce que Minlo a dit sur le raid hier soir ?", "Il a dit autre chose ?"),
    ("What did I play on Saturday afternoon?", "and after that?"),
]
for prev, new in EX:
    e, s = harness.plan_alone(prev), harness.plan_alone(new)
    print(f"{prev!r} -> {new!r}  R0={c.r0(new, s)}")
    for f in harness.FIELDS:
        if harness.value(s, f) != harness.value(e, f):
            print(f"   {c._line(f, e, s, harness.TZ)}")
