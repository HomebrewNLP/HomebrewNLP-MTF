import typing

import mesh_tensorflow as mtf
import tensorflow as tf

from .activation import activate_util
from .backend import communicating_linear, get_attention_dim, linear_from_features
from .basic import embed, dropout
from ..dataclass import ModelParameter
from ..mtf_wrapper import einsum
from ..utils_mtf import anonymize, anonymize_dim
from ..utils_core import random_name

ATTENTION_DIM = typing.NamedTuple("AttentionDim", (('index', int), ('dim', mtf.Dimension)))

tf1 = tf.compat.v1


def tf_softmax(x, masked, dim, dim_index, anonymous_dim_index):
    if masked:
        arange = tf.range(0, dim.size)
        msk = tf.reshape(arange, (1, dim.size)) > tf.reshape(arange, (dim.size, 1))
        msk = tf.cast(msk, x.dtype)
        msk = (msk * 3e38) * 2
        shape = [1] * len(x.shape)
        shape[dim_index] = dim.size
        shape[anonymous_dim_index] = dim.size
        msk = tf.reshape(msk, shape)
        x -= msk
    e = tf.exp(x - tf.reduce_max(x, anonymous_dim_index, True))
    return e / tf.reduce_sum(e, anonymous_dim_index, True)


class SoftmaxBackward(mtf.Operation):
    def __init__(self, x: mtf.Tensor, dy: mtf.Tensor, dim: mtf.Dimension, masked: bool):
        super().__init__([x, dy], name=random_name("softmax_backward"))
        self._outputs = [mtf.Tensor(self, x.shape, x.dtype)]
        self.dim = dim
        self.shape: mtf.Shape = x.shape
        self.masked = masked

    def lower(self, lowering):
        mesh_impl = lowering.mesh_impl(self)
        dim_index = self.shape.dims.index(self.dim)
        dim_index = self.shape.dims.index(self.dim)
        anonymous_dim_index = self.shape.dims.index(anonymize_dim(self.dim))
        masked = self.masked
        dim = self.dim

        def slicewise_fn(x, y):
            s = tf_softmax(x, masked, dim, dim_index, anonymous_dim_index)
            dims = ''.join(chr(ord('a') + i) for i in range(len(x.shape)))
            sdims = dims[:anonymous_dim_index] + 'z' + dims[anonymous_dim_index + 1:]
            return s * y - tf.einsum(f"{dims},{sdims},{sdims}->{dims}", s, s, y)

        y = mesh_impl.slicewise(slicewise_fn, lowering.tensors[self.inputs[0]], lowering.tensors[self.inputs[1]])
        lowering.set_tensor_lowering(self.outputs[0], y)


class SoftmaxForward(mtf.Operation):
    def __init__(self, x: mtf.Tensor, dim: mtf.Dimension, masked: bool):
        super().__init__([x], name=random_name("softmax_forward"))
        self._outputs = [mtf.Tensor(self, x.shape, x.dtype)]
        self.dim = dim
        self.shape: mtf.Shape = x.shape
        self.masked = masked

    def gradient(self, grad_ys):
        return SoftmaxBackward(self.inputs[0], grad_ys[0], self.dim, self.masked).outputs

    def lower(self, lowering):
        mesh_impl = lowering.mesh_impl(self)
        dim_index = self.shape.dims.index(self.dim)
        anonymous_dim_index = self.shape.dims.index(anonymize_dim(self.dim))
        masked = self.masked
        dim = self.dim

        def slicewise_fn(x):
            return tf_softmax(x, masked, dim, dim_index, anonymous_dim_index)

        y = mesh_impl.slicewise(slicewise_fn, lowering.tensors[self.inputs[0]])
        lowering.set_tensor_lowering(self.outputs[0], y)


def attention(params: ModelParameter, block_input: mtf.Tensor, name_extras: typing.List[str]):
    idx, dim = get_attention_dim(params, block_input)
    params.attention_idx += 1
    base = dropout(params, activate_util(name_extras, linear_from_features(params, block_input)), name_extras)
    linear = 'linear' in name_extras
    masked = idx in params.masked_attention_dimensions

    key = 0
    if 'embedded' in name_extras or 'context' in name_extras:
        key = communicating_linear(params, base) * dim.size ** -0.5
    if 'embedded' in name_extras or 'positional' in name_extras:
        key += embed(params, [dim] + params.feature_dims)
    val = communicating_linear(params, base)
    qry = communicating_linear(params, base)
    if 'activate_val' in name_extras:
        val = activate_util(name_extras, val)
    if 'activate_key' in name_extras:
        key = activate_util(name_extras, key)
    if 'activate_qry' in name_extras:
        qry = activate_util(name_extras, qry)
    val_dim = params.key_dim if linear else dim
    key = anonymize(key, dim)
    val = anonymize(val, val_dim)
    inputs = [qry, anonymize(key, [params.key_dim] * linear + [dim] * (masked or not linear))]
    lgt = einsum(inputs, reduced_dims=[dim if linear else params.key_dim])
    return einsum(SoftmaxForward(lgt, dim, masked).outputs + [val], block_input.shape)