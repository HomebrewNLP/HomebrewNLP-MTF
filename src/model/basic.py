import math
import typing

import mesh_tensorflow as mtf
import tensorflow as tf

from .activation import activate
from .backend import get_var, linear, orthogonal_var
from .embedding import gather_embed
from .normalization import norm
from ..dataclass import BlockArgs
from ..mtf_wrapper import (dropout as utils_dropout, sigmoid, exp, reduce_max, reduce_sum, einsum, reciprocal, reshape,
                           multiply, stop_gradient)
from ..utils_mtf import linear_shapes, anonymize_shape, unbind, replace_dim, anonymize_dim

ATTENTION_DIM = typing.NamedTuple("AttentionDim", (('index', int), ('dim', mtf.Dimension)))

tf1 = tf.compat.v1


def rezero(args: BlockArgs) -> mtf.Tensor:
    return args.tensor * get_var(args, [], tf.constant_initializer(0))


def dropout(args: BlockArgs) -> mtf.Tensor:
    keep = 1
    for extra in args.name_extras:
        if extra.startswith('dropout_rate'):
            keep = 1 - float(extra[len('dropout_rate'):])
    return utils_dropout(args.tensor, args.params.train, keep)


def wrapped_linear(args: BlockArgs) -> mtf.Tensor:
    return linear(args, *linear_shapes(args))


def mixture_of_experts(args: BlockArgs) -> mtf.Tensor:
    old, new = linear_shapes(args)
    gate = linear(args, old, [args.params.expert_dim])
    gate -= mtf.stop_gradient(reduce_max(gate, reduced_dim=args.params.expert_dim))
    gate = exp(gate)
    return einsum([reciprocal(reduce_sum(gate, reduced_dim=args.params.expert_dim)), args.tensor, gate,
                   orthogonal_var(args, old + new + [args.params.expert_dim])],
                  output_shape=args.tensor.shape - old + new)


def activated_linear(args: BlockArgs, prefix: str) -> mtf.Tensor:
    args = args([a[len(prefix):] for a in args if a.startswith(prefix)])
    feed_forward_fn = mixture_of_experts if 'mixture_of_experts' in args else wrapped_linear
    out = dropout(args(activate(args(feed_forward_fn(args)))))
    if 'glu' in args or 'glu_add' in args:
        out = multiply(out, sigmoid(feed_forward_fn(args)))
    if 'glu_add' in args:
        out += activate(args(feed_forward_fn(args)))
    if 'norm' in args:
        out = norm(args(out))
    return out


def activated_linear_in(args: BlockArgs) -> mtf.Tensor:
    return activated_linear(args, 'in:')


def activated_linear_out(args: BlockArgs) -> mtf.Tensor:
    return activated_linear(args, 'out:')


def feed_forward(args: BlockArgs) -> mtf.Tensor:
    return activated_linear_out(args(activated_linear_in(args)))


def group_linear(args: BlockArgs) -> mtf.Tensor:
    anonymous_key = anonymize_shape(args.params.feature_dims, args.params.key_dim)
    return reshape(linear(args('group'), args.params.feature_dims, anonymous_key), args.tensor.shape)


def sum_heads(args: BlockArgs) -> mtf.Tensor:
    return reduce_sum(args.tensor, reduced_dim=args.params.head_dim)


def transpose_sequence_features(args: BlockArgs) -> mtf.Tensor:
    assert args.params.features_per_head == args.params.sequence_length, "ToDo: Support other shapes"
    tensor = mtf.rename_dimension(args.tensor, args.params.sequence_dim.name, "intermediate")
    tensor = mtf.rename_dimension(tensor, args.params.key_dim.name, args.params.sequence_dim.name)
    tensor = mtf.rename_dimension(tensor, "intermediate", args.params.key_dim.name)
    return mtf.transpose(tensor, args.tensor.shape)


def reduced_half_linear(args: BlockArgs) -> mtf.Tensor:
    return group_linear(args(reduce_sum(args.tensor, reduced_dim=args.params.head_dim)))


def product_key_memory(args: BlockArgs) -> mtf.Tensor:
    anonymous_key = anonymize_dim(args.params.key_dim)
    features = [args.params.pkm_dim, anonymous_key]
    assignment = linear(args, linear_shapes(args).old, [args.params.head_dim] + features)
    assignment = replace_dim(assignment, args.params.key_dim, anonymous_key)  # No-op. Just for MTF propagation
    assignment = norm(args(assignment), features)
    assignment = mtf.cast(assignment, tf.float64)
    normalizer = reduce_max(assignment, reduced_dim=args.params.key_dim)
    normalizer = reduce_sum(normalizer, reduced_dim=args.params.pkm_dim)
    assignment -= stop_gradient(normalizer)
    assignment = exp(assignment)
    normalizer = reduce_sum(assignment, output_shape=assignment.shape - [args.params.key_dim])
    normalizer = einsum(unbind(normalizer, args.params.pkm_dim), output_shape=normalizer.shape - args.params.pkm_dim)

    val, idx = mtf.top_1(assignment, args.params.key_dim)
    idx = mtf.einsum([mtf.cast(exp(math.log(args.params.features_per_head) *
                                   mtf.range(normalizer.mesh, args.params.pkm_dim, dtype=normalizer.dtype)),
                               tf.int32), idx], output_shape=idx.shape - args.params.pkm_dim)
    val = einsum(unbind(val, args.params.pkm_dim), output_shape=val.shape - args.params.pkm_dim) / normalizer
    val = mtf.cast(val, args.params.variable_dtype.activation_dtypex)
    out = gather_embed(args(idx), [args.params.product_key_value_dim] + args.params.feature_dims,
                       [args.params.head_dim])
    return out * val


def feed_forward_product_key_memory(args: BlockArgs) -> mtf.Tensor:
    return product_key_memory(args(activated_linear_in(args)))


def bottleneck_group_linear(args: BlockArgs) -> mtf.Tensor:
    args = args(activated_linear_in(args))
    args.name_extras.extend(['group', 'mid:group', 'out:group'])
    args = args(activated_linear(args, 'mid:'))
    return activated_linear_out(args)
