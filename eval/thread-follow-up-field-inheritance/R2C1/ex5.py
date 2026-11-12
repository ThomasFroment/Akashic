import sys
sys.path.insert(0, "..")
import harness
import candidate as c
EX = [
    ("What music did I listen to yesterday?", "And this morning?"),
    ("Qu'est-ce que j'ai regardé ce matin ?", "Et samedi soir ?"),
    ("Qu'est-ce que Minlo a dit sur le raid hier ?", "Et à propos du festival ?"),
    ("What did Sacha say about the tournament yesterday?", "Et about the prize ?"),
    ("Quelle série j'ai regardée hier soir ?", "Qui a réalisé cette série ?"),
    ("What did Minlo say about the invoice on Friday?", "Did he share the spreadsheet with me?"),
    ("Qu'est-ce que Léa a dit sur la série mercredi ?", "Et elle, elle en pensait quoi du dernier épisode ?"),
    ("Qu'est-ce que Sacha a dit sur son exam ce matin ?", "Juste après, je faisais quoi ?"),
]
for prev, new in EX:
    e, s = harness.plan_alone(prev), harness.plan_alone(new)
    print(f"{prev!r} -> {new!r}  R0={c.r0(new, s)}")
    for f in harness.FIELDS:
        if harness.value(s, f) != harness.value(e, f):
            print(f"   {c._line(f, e, s, harness.TZ)}")
