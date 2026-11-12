r"""Dev-only, run with ..\onnx_venv (the `onnx` package lives only there):
derive WeSpeaker ResNet34-LM with one extra graph output, the pooling input (tensor '343',
(B, 256, 10, T') before TSTP), same weights, and dump the head (Gemm W, b, mean_vec, eps) to npz.

    ..\onnx_venv\Scripts\python.exe -I candidates\R2span_derive.py
-> data\models_derived\resnet34_LM_framemap.onnx (+ .sha256), data\models_derived\resnet34_LM_head.npz
"""
import hashlib
import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import helper, numpy_helper, TensorProto

HERE = Path(__file__).resolve().parents[1]
SRC = HERE / "data/models/wespeaker-voxceleb-resnet34-lm/voxceleb_resnet34_LM.onnx"
PIN_SRC = "7bb2f06e"  # app pin prefix (README)
DST = HERE / "data/models_derived/resnet34_LM_framemap.onnx"
HEAD = HERE / "data/models_derived/resnet34_LM_head.npz"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    s = sha(SRC)
    assert s.startswith(PIN_SRC), s
    m = onnx.load(str(SRC))
    g = m.graph
    inits = {i.name: numpy_helper.to_array(i) for i in g.initializer}
    consts = {}
    for n in g.node:
        if n.op_type == "Constant":
            consts[n.output[0]] = numpy_helper.to_array(n.attribute[0].t)
    gemm = next(n for n in g.node if n.op_type == "Gemm")
    sub = next(n for n in g.node if n.op_type == "Sub" and n.output[0] == "embs")
    add_eps = next(n for n in g.node if n.op_type == "Add" and n.output[0] == "359")
    sub_n = next(n for n in g.node if n.op_type == "Sub" and n.output[0] == "356")
    W, b = inits[gemm.input[1]], inits[gemm.input[2]]
    mean_vec = inits[sub.input[1]]
    eps = consts[add_eps.input[1]]
    ddof = consts[sub_n.input[1]]
    print("W", W.shape, "b", b.shape, "mean_vec", mean_vec.shape, "eps", eps, "ddof", ddof)
    np.savez(HEAD, W=W, b=b, mean_vec=mean_vec, eps=np.float64(eps), ddof=np.float64(ddof))
    vi = helper.make_tensor_value_info("framemap", TensorProto.FLOAT, ["B", 256, 10, "Tp"])
    ident = helper.make_node("Identity", ["343"], ["framemap"], name="framemap_out")
    g.node.append(ident)
    g.output.append(vi)
    onnx.checker.check_model(m)
    onnx.save(m, str(DST))
    d = sha(DST)
    (DST.with_suffix(".onnx.sha256")).write_text(f"{d}  {DST.name}\nsource {s}\n", encoding="utf-8")
    print("derived", DST, d)


if __name__ == "__main__":
    main()
