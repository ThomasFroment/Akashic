"""Dev-only (onnx_venv): print the tail of the WeSpeaker ResNet34-LM graph (pooling + head)."""
import sys
import onnx
from onnx import numpy_helper
p = sys.argv[1]
m = onnx.load(p)
g = m.graph
print("inputs", [(i.name, [d.dim_value or d.dim_param for d in i.type.tensor_type.shape.dim]) for i in g.input])
print("outputs", [(o.name, [d.dim_value or d.dim_param for d in o.type.tensor_type.shape.dim]) for o in g.output])
inits = {i.name: i for i in g.initializer}
print("n nodes", len(g.node))
for n in g.node[-40:]:
    shapes = [tuple(inits[x].dims) if x in inits else x for x in n.input]
    print(n.op_type, n.name, shapes, "->", list(n.output), [(a.name, onnx.helper.get_attribute_value(a)) for a in n.attribute if a.name in ("axes", "keepdims", "transA", "transB", "unbiased", "perm", "alpha", "beta")])
