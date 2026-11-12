import sys
sys.path.insert(0, "..")
import harness
import candidate as c
EX = [
    ("What did Minlo say about the chess tournament this morning?", "Un résumé de ma journée d'hier ?"),
    ("Qu'est-ce que j'ai regardé hier ?", "Quick recap of my day today?"),
    ("Qu'est-ce que Sacha a dit sur le raid hier soir ?", "Show me the articles I read about sourdough"),
    ("What did I play on Saturday afternoon?", "Mes appels avec Minlo cette semaine ?"),
]
for prev, new in EX:
    e, s = harness.plan_alone(prev), harness.plan_alone(new)
    print(f"{prev!r} -> {new!r}  R0={c.r0(new, s)}")
    for f in harness.FIELDS:
        if harness.value(s, f) != harness.value(e, f):
            print(f"   {c._line(f, e, s, harness.TZ)}")
