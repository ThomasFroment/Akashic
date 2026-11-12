import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, sys
plt.style.use("dark_background")
out=sys.argv[1]
sets=["phrases (234 q, tuned)","heldout2 (145 q)","heldout3 (198 q, fresh)"]
d={"today (rules)":[0.1605,0.179,0.190],"C1 logical-day table (rules)":[0.903,0.720,0.684],
"C3 anchor lexer, fixed (rules)":[1.000,0.830,0.753],"C7 slot grammar + variants, gazetteer off":[1.000,0.987,0.884],
"C7 + captured-title gazetteer (simulated)":[1.000,1.000,0.926],"C8 model slots on C3 (model)":[0.996,0.975,0.881],
"C9 model slots on C7 (model)":[None,None,0.931]}
fig,ax=plt.subplots(figsize=(9.5,5))
for k,v in d.items():
    xs=[i for i,y in enumerate(v) if y is not None]; ys=[y for y in v if y is not None]
    ax.plot(xs,ys,marker="o",label=k)
ax.axhline(0.95,ls="--",color="w",lw=1); ax.text(2.05,0.955,"95 %",fontsize=9)
ax.set_xticks(range(3)); ax.set_xticklabels(sets); ax.set_ylim(0,1.05); ax.set_ylabel("covers ∧ tight pass rate")
ax.set_title("Pass rate on tuned vs fresh question sets (C3/C7/C8 were tuned on phrases + heldout2)",fontsize=9)
ax.legend(fontsize=8,loc="lower right"); fig.tight_layout(); fig.savefig(out+"/fig-generalisation.png",dpi=110)
# retrieval figure
fig,ax=plt.subplots(figsize=(8,4))
labels=["line recall","in-window recall","episode recall"]
gt=[0.779,0.999,0.923]; c5=[1.0,1.0,1.0]
import numpy as np; x=np.arange(3)
ax.bar(x-0.18,gt,0.36,label="today's start-time filter (gold windows)",color="#d95f2b")
ax.bar(x+0.18,c5,0.36,label="C5 overlap + session closure (gold windows)",color="#3b8be0")
for i,(a,b) in enumerate(zip(gt,c5)): ax.text(i-0.18,a+0.01,f"{a:.3f}",ha="center",fontsize=8); ax.text(i+0.18,b+0.01,f"{b:.3f}",ha="center",fontsize=8)
ax.set_xticks(x); ax.set_xticklabels(labels); ax.set_ylim(0,1.12); ax.legend(fontsize=8,loc="lower right")
ax.set_title("Retrieval fixture, 14 questions: which lines of the intended call survive the window (outside lines: 12 for both)",fontsize=9)
fig.tight_layout(); fig.savefig(out+"/fig-retrieval.png",dpi=110)
