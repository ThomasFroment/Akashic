import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt, numpy as np, sys
plt.style.use("dark_background")
names=["today","C1 two-key","C5 contrast","C5 fusion","C7 decode-stats","C3 CTC key"]
recall=[.727,.826,.881,.921,.921,.949]; rej=[.344,.308,.344,.368,.375,.043]
frr=[.0086,.0086,.0086,.0086,.0086,.0035]; unclear=[.168,.175,.713,.080,.098,.220]
x=np.arange(len(names)); fig,ax=plt.subplots(1,2,figsize=(12,4.3))
ax[0].bar(x-0.2,recall,0.4,label="caught (reject or unclear)",color="#3b8be0"); ax[0].bar(x+0.2,rej,0.4,label="removed (reject)",color="#d95f2b")
ax[0].axhline(0.9,ls="--",color="w",lw=1); ax[0].set_xticks(x); ax[0].set_xticklabels(names,rotation=20,fontsize=8); ax[0].set_ylim(0,1.05); ax[0].legend(fontsize=8); ax[0].set_title("Hallucination recall (test, n=253)",fontsize=10)
for i,v in enumerate(recall): ax[0].text(i-0.2,v+0.01,f"{v:.2f}",ha="center",fontsize=7)
ax[1].bar(x-0.2,[f*100 for f in frr],0.4,label="real speech rejected (%)",color="#e5645f"); ax[1].bar(x+0.2,[u*100 for u in unclear],0.4,label="real speech flagged unclear (%)",color="#d9a13b")
ax[1].axhline(1,ls="--",color="w",lw=1); ax[1].set_xticks(x); ax[1].set_xticklabels(names,rotation=20,fontsize=8); ax[1].set_yscale("symlog",linthresh=1); ax[1].legend(fontsize=8); ax[1].set_title("Cost on real speech (test, n=1157; dashed = 1%)",fontsize=10)
fig.tight_layout(); fig.savefig(sys.argv[1]+"/fig-tradeoff.png",dpi=110)
