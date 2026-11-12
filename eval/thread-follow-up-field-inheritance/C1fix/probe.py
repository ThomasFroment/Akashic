"""Hand-written paraphrase probes (not from any pair file): print S, E and the candidate."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import harness  # noqa: E402

_, fn, _ = harness.load_candidate(HERE)
mod = fn.__globals__

A = "What did Minlo say about Elden Ring this morning?"
V = "What video did I watch yesterday afternoon?"
L = "Qu'est-ce que Léa a dit sur la bande-annonce de Zelda lundi ?"
G = "À quel jeu j'ai joué hier soir ?"
PROBES = [
    (V, "Give me more info."),
    (A, "Tell me more about the DLC."),
    (A, "Can you elaborate?"),
    (V, "Raconte-moi la suite."),
    (A, "Explique-moi plus."),
    (A, "Did he tell me more about the patch?"),
    (A, "Est-ce qu'il a donné plus de détails ?"),
    (V, "And on Friday?"),
    (V, "And on the weekend?"),
    (A, "On en a parlé quand ?"),
    (A, "Where is it?"),
    (A, "C'est où ?"),
    (L, "Did she watch it?"),
    (L, "Il l'a regardée quand ?"),
    (A, "And Léa, what did she think?"),
    (A, "Et Léa, elle en pense quoi ?"),
    (A, "Et du côté de Léa ?"),
    (A, "What did I watch yesterday?"),
    (A, "Did he mention his team?"),
    (L, "Elle a parlé de sa note ?"),
    (V, "Who uploaded it?"),
    (A, "Dis-m'en davantage sur le DLC."),
    (V, "Je l'ai regardée combien de temps ?"),
    (G, "Je l'ai fini ?"),
    (A, "What did Sacha say about Valorant on Friday?"),
    (A, "Quand est-ce que c'était ?"),
    (A, "When was it?"),
    (G, "Et on Saturday ?"),
    (A, "On a parlé de quoi avec Sacha hier ?"),
    # self-contained negatives: nothing should be inherited
    (A, "Tell me what Léa said about the trip on Monday."),
    (A, "Tell me more about what Sacha said yesterday about Valorant."),
    (V, "Qu'est-ce que j'ai écouté hier soir ?"),
    (V, "Did I talk with Sacha on Friday?"),
    (A, "C'était quoi la vidéo que j'ai regardée hier ?"),
    (V, "Explain what I did this morning."),
    (A, "Which video did Sacha send me yesterday?"),
    (V, "What did Minlo say about his exam on Monday?"),
]
F = ("people", "kinds", "keywords", "time")
for prev, q in PROBES:
    e, s = harness.plan_alone(prev), harness.plan_alone(q)
    out = fn(q, prev, e, s, harness.NAMES, harness.NOW)
    print(f"{prev!r} -> {q!r}  cue={ {k for k, v in mod['LAST']['cue'].items() if v} }")
    for f in F:
        sv, ev, cv = (harness.value(p, f) for p in (s, e, out))
        tag = "S=E" if sv == ev else ("own" if cv == sv else "inh" if cv == ev else "oth")
        print(f"    {f:8} {tag}  S={harness.show(sv)} E={harness.show(ev)} C={harness.show(cv)}")
