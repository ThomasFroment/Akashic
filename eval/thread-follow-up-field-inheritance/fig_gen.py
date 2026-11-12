import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, sys
plt.style.use("dark_background")
sets=["seen (tuned)","heldout2 (110)","heldout3 (165)"]
data={"today (follow_up)":[0.700,0.555,0.618],"C1 cue gate (rules)":[1.000,0.900,0.903],
"C2 C1 + LLM on undecided cells":[1.000,0.900,None],"C3 single shift bit":[0.788,None,0.636],
"C5 inverted routing (LLM)":[0.971,0.982,0.927],"C6 relation tagger + closed form (LLM)":[0.965,0.982,0.933],
"C6 rule-only fallback":[0.971,0.927,0.903]}
fig,ax=plt.subplots(figsize=(9,5))
for k,v in data.items():
    xs=[i for i,y in enumerate(v) if y is not None]; ys=[y for y in v if y is not None]
    ax.plot(xs,ys,marker="o",label=k)
ax.axhline(0.95,ls="--",color="w",lw=1); ax.text(2.05,0.952,"95 %",fontsize=9)
ax.set_xticks(range(3)); ax.set_xticklabels(sets); ax.set_ylim(0.5,1.02)
ax.set_ylabel("lowest per-field accuracy"); ax.set_title("Accuracy on seen vs fresh question sets (C1/C2 tuned on pairs+heldout split; C5/C6 tuned on pairs+heldout2)")
ax.title.set_fontsize(9); ax.legend(fontsize=8,loc="lower left"); fig.tight_layout(); fig.savefig(sys.argv[1],dpi=110)
