import sys
sys.path.insert(0, "..")
import harness
import candidate as c
EX = [
    ("What did Minlo say about the camping trip on Sunday?", "What did he say about the tent?"),
    ("What did Léa tell me about the project yesterday?", "Did she mention the deadline too?"),
    ("Qu'est-ce que Minlo a dit sur le boulot vendredi ?", "Et au sujet des vacances ?"),
    ("What did Sacha say about the raid yesterday evening?", "Il a dit quoi about the new map ?"),
    ("What did Sacha say about the festival on Saturday?", "Where is it happening?"),
    ("Qu'est-ce que Minlo a dit sur le tournoi d'échecs hier ?", "Ça commence à quelle heure, ce truc ?"),
    ("What did I play yesterday evening?", "Something else?"),
    ("What did Sacha tell me about his exam this morning?", "What did I do next?"),
    ("Qu'est-ce que Sacha a dit pendant l'appel vendredi ?", "Et Minlo, il a dit quoi ce jour-là ?"),
    ("What did Minlo say about the outage on Monday?", "Did Léa hear about it too?"),
    ("Qu'est-ce que Sacha a dit sur le speedrun lundi ?", "Il l'a regardé quand ?"),
    ("Which sites did I browse about headphones yesterday?", "Did I order them in the end?"),
]
for prev, new in EX:
    e, s = harness.plan_alone(prev), harness.plan_alone(new)
    print(f"{prev!r} -> {new!r}  R0={c.r0(new, s)}")
    for f in harness.FIELDS:
        if harness.value(s, f) != harness.value(e, f):
            print(f"   {c._line(f, e, s, harness.TZ)}")
