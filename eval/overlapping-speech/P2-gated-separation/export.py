"""Export MossFormer2_SS_16K to ONNX (runs in the throwaway export-venv, never the project env).

    export-venv/Scripts/python.exe -I export.py

Model code: ClearerVoice-Studio (Apache-2.0), read from _resources/references (not copied). Two
export-only patches, checked against the unpatched model in eager mode before export:
- the attention always pads to a multiple of the group size and always masks (the original skips
  both when no padding is needed: same numbers, but a shape-dependent branch);
- rotary embedding as a pure function of the sequence length (the library caches by Python ints).
Writes model/mossformer2_ss_16k.onnx (+ .data) and model/torch_out.npz (eager outputs on the test
mixtures, for the onnxruntime check in the project env).
"""

from __future__ import annotations

import sys
import types
from argparse import Namespace
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

HERE = Path(__file__).resolve().parent
CV = HERE.parents[2] / "_resources" / "references" / "ClearerVoice-Studio" / "clearvoice" / "clearvoice"
sys.path.insert(0, str(CV))
sys.modules["torchinfo"] = types.SimpleNamespace(summary=None)

from models.mossformer2_ss import mossformer2_block as blk  # noqa: E402
from models.mossformer2_ss.mossformer2 import MossFormer2_SS_16K  # noqa: E402

ARGS = Namespace(encoder_embedding_dim=512, mossformer_sequence_dim=512, num_mossformer_layer=24, encoder_kernel_size=16, num_spks=2)


def rotate_half(x):
    x = x.reshape(*x.shape[:-1], -1, 2)
    x1, x2 = x.unbind(dim=-1)
    return torch.stack((-x2, x1), dim=-1).flatten(-2)


def rotary(rot, t):
    """rotary_embedding_torch.RotaryEmbedding.rotate_queries_or_keys for (b, n, d), seq_dim -2."""
    n = t.shape[-2]
    pos = torch.arange(n, device=t.device, dtype=t.dtype)
    freqs = pos[:, None] * rot.freqs[None, :].to(t.dtype)
    freqs = freqs.repeat_interleave(2, dim=-1)  # '... n -> ... (n r)', r=2
    r = freqs.shape[-1]
    mid = t[..., :r]
    return torch.cat((mid * freqs.cos() + rotate_half(mid) * freqs.sin(), t[..., r:]), dim=-1)


def cal_attention(self, x, quad_q, lin_q, quad_k, lin_k, v, u, mask=None):
    b, n, g = x.shape[0], x.shape[-2], self.group_size
    quad_q, lin_q, quad_k, lin_k = (rotary(self.rotary_pos_emb, t) for t in (quad_q, lin_q, quad_k, lin_k))
    padding = (g - n % g) % g
    quad_q, quad_k, lin_q, lin_k, v, u = (F.pad(t, (0, 0, 0, padding), value=0.0) for t in (quad_q, quad_k, lin_q, lin_k, v, u))
    m = F.pad(torch.ones((b, n), device=x.device, dtype=torch.bool), (0, padding), value=False)
    grp = lambda t: t.reshape(b, -1, g, t.shape[-1])  # 'b (g n) d -> b g n d', n=group
    quad_q, quad_k, lin_q, lin_k, v, u = map(grp, (quad_q, quad_k, lin_q, lin_k, v, u))
    m = m.reshape(b, -1, 1, g)
    sim = torch.einsum("...id,...jd->...ij", quad_q, quad_k) / g
    attn = F.relu(sim) ** 2
    attn = attn.masked_fill(~m, 0.0)
    quad_out_v = torch.einsum("...ij,...jd->...id", attn, v)
    quad_out_u = torch.einsum("...ij,...jd->...id", attn, u)
    lin_kv = torch.einsum("bgnd,bgne->bde", lin_k, v) / n
    lin_out_v = torch.einsum("bgnd,bde->bgne", lin_q, lin_kv)
    lin_ku = torch.einsum("bgnd,bgne->bde", lin_k, u) / n
    lin_out_u = torch.einsum("bgnd,bde->bgne", lin_q, lin_ku)
    fold = lambda t: t.reshape(b, -1, t.shape[-1])[:, :n]
    return fold(quad_out_v + lin_out_v), fold(quad_out_u + lin_out_u)


class Wrap(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, x):  # (1, T) -> (2, T)
        return torch.cat(self.m(x), dim=0)


def load() -> torch.nn.Module:
    model = MossFormer2_SS_16K(ARGS).model
    ck = torch.load(HERE / "model" / "last_best_checkpoint.pt", map_location="cpu", weights_only=True)
    sd = ck.get("model", ck)
    state = model.state_dict()
    missing = [k for k in state if k not in sd and "module." + k not in sd]
    for k in state:
        state[k] = sd.get(k, sd.get("module." + k, state[k]))
    model.load_state_dict(state)
    print(f"checkpoint keys {len(sd)} model keys {len(state)} missing {len(missing)} params {sum(p.numel() for p in model.parameters()) / 1e6:.1f} M")
    return model.eval()


def norm(x):
    return x * (10 ** (-25 / 20) / (np.sqrt(np.mean(x ** 2)) + 1e-8))


def main() -> None:
    torch.set_grad_enabled(False)
    model = load()
    data = np.load(HERE / "model" / "test_mix.npz")
    mixes = {k: norm(data[k]).astype(np.float32) for k in data.files if k.startswith("mix")}
    ref = dict(np.load(HERE / "model" / "torch_out.npz")) if (HERE / "model" / "torch_out.npz").is_file() else {k: torch.cat(model(torch.from_numpy(x)[None]), 0).numpy() for k, x in mixes.items()}  # unpatched
    blk.FLASH_ShareA_FFConvM.cal_attention = cal_attention
    for k, x in mixes.items():
        y = torch.cat(model(torch.from_numpy(x)[None]), 0).numpy()
        print(f"{k}: patched vs original eager max abs diff {np.abs(y - ref[k]).max():.2e} (out rms {np.sqrt(np.mean(ref[k] ** 2)):.3f})")
    np.savez(HERE / "model" / "torch_out.npz", **ref)
    w = Wrap(model).eval()
    ex = torch.from_numpy(mixes["mix0"][: len(mixes["mix0"]) // 8 * 8])[None]
    T = 8 * torch.export.Dim("k", min=500, max=2000 * 60)
    out = HERE / "model" / "mossformer2_ss_16k.onnx"
    torch.onnx.export(w, (ex,), str(out), dynamo=True, input_names=["mix"], output_names=["sources"],
                      dynamic_shapes={"x": {1: T}}, opset_version=18, external_data=True)
    print("exported", out, out.stat().st_size)


if __name__ == "__main__":
    main()
