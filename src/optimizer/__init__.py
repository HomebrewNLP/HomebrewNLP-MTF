"""
Stores custom optimizer classes as well as a custom optimizer creation utility as a handy wrapper
b"""

import typing

import mesh_tensorflow as mtf
import numpy as np
import tensorflow as tf2

from .backend import import_mtf, import_float, get_var, variable
from .context import OptimizerCtx
from .gradients import gradients
from .learning_rate import get_learning_rate
from .optimizers import OPTIMIZERS
from ..dataclass import ModelParameter
from ..mtf_wrapper import (cast, constant_float, constant_scalar, einsum, equal, greater_equal, mod,
                           reduce_sum, assign, assign_sub,
                           add, multiply, scoped, assign_add, identity, zeros_like, negative, rsqrt_eps,
                           optimizer_scalar, reciprocal)
from ..utils_mtf import feature_dims_used, to_fp32, get_fan_in

tf = tf2.compat.v1
zeros = tf.zeros_initializer()


def gradient_accumulation(ctx: OptimizerCtx):
    ctx.update_ops.append(assign_add(ctx.grad_buffer, add(ctx.grad, identity(ctx.grad_buffer))))


def update(ctx: OptimizerCtx):
    params = ctx.params
    update_ops = ctx.update_ops
    learning_rate = ctx.learning_rate

    var = ctx.var
    if ctx.grad_buffer is not None:
        ctx.grad = identity(ctx.grad_buffer.value)
        ctx.update_ops.append(assign(ctx.grad_buffer, zeros_like(ctx.grad)))

    for opt in params.optimizer.split('-'):
        opt, *args = opt.split(':')
        ctx.grad = scoped(opt, OPTIMIZERS[opt], ctx, *args)

    if 'rezero' in var.name:
        ctx.grad = multiply(params.rezero_lr_multiplier, ctx.grad)

    features_used = feature_dims_used(params, var)
    large_tensor = features_used and len(var.shape.dims) > len(params.feature_dims)
    large_tensor |= not features_used and len(var.shape.dims) >= 2  # not norm or rezero + scalable catch-all
    large_tensor &= var.shape.size > 1  # not rezero
    large_tensor &= "embed" not in var.name  # not input/output embedding, position embedding, attention map bias
    large_tensor &= "input" not in var.name or "lang_in" in var.name or "vid_in" in var.name  # not input
    large_tensor &= "output" not in var.name or "lang_out" in var.name or "vid_out" in var.name  # not output

    if large_tensor and params.weight_decay > 0:
        ctx.grad = add(ctx.grad, einsum([optimizer_scalar(params, params.weight_decay),
                                         cast(var.value, params.optimizer_calculation_dtype), learning_rate],
                                        output_shape=var.shape))

    if not large_tensor or not params.weight_standardisation:
        update_ops.append(assign_sub(var, ctx.grad))
        return

    val: mtf.Tensor = var.value - ctx.grad
    fan_in_size = np.prod([d.size for d in get_fan_in(params, var)])
    size = np.prod([d.size for d in var.shape.dims])
    max_fan = max(fan_in_size, size // fan_in_size)
    variance = ((1 - 1 / max_fan) / size ** 2 + 1 / max_fan - 2 / size + 1 / max_fan)
    if params.scale_by_depth:
        variance *= params.n_blocks
    val = einsum([val, rsqrt_eps(einsum([val, val, optimizer_scalar(params, 1 / val.size)], output_shape=[])),
                  optimizer_scalar(params, variance ** 0.5)], output_shape=var.shape)
    update_ops.append(assign(var, val))


def get_optimizer(loss_list: typing.List[mtf.Tensor], params: ModelParameter, manual_step: tf.Tensor, fn: str
                  ) -> typing.Tuple[typing.Tuple[mtf.Tensor, typing.List[mtf.Assign], typing.List[mtf.Tensor]],
                                    tf.Tensor, typing.Dict]:
    """
    Creates optimizing and update/training operations.
    :param loss_list: Final scalar loss of the model
    :param params: ModelParameter instance
    :param manual_step: manually incremented global_step variable to account for grad accumulation
    :param fn: whether to "accumulate" gradients or "update" parameters.
    :return: scalar learning rate, update operations, gradients

    there is no check for "update". you can just call it "oijhiojio" and it'll still work. just make sure it's not
    called "accumulate".
    """

    dtype = params.optimizer_calculation_dtype
    update_ops = []

    learning_rate_ctx = get_learning_rate(params, loss_list, update_ops)
    learning_rate = import_mtf(params, learning_rate_ctx.learning_rate, "learning_rate")

    step = cast(equal(mod(tf.cast(manual_step + 1, dtype),
                          import_mtf(params, params.grad_accumulation * 1., "grad_accum")),
                      import_mtf(params, 0., "zero")), dtype)
    neg_step = negative(step)
    mstep = add(1, neg_step)
    beta1 = add(1, multiply(neg_step, import_mtf(params, 1 - params.opt_beta1, "beta1")))
    beta2 = add(1, multiply(neg_step, import_mtf(params, 1 - params.opt_beta2, "beta2")))

    debug_gradients_dict = {}
    first_grad = {}
    loss_1__loss_1 = loss_1__loss_2 = loss_2__loss_2 = 0
    mgda = params.multi_loss_strategy == "mgda"

    if mgda:
        loss_1__loss_1 = constant_float(params, 0, shape=[params.head_dim])
        loss_1__loss_2 = constant_float(params, 0, shape=[params.head_dim])
        loss_2__loss_2 = constant_float(params, 0, shape=[params.head_dim])

    for loss_idx, loss in enumerate(loss_list):
        if mgda and loss_idx == 2:
            v1v1 = reduce_sum(loss_1__loss_1, output_shape=[])
            v1v2 = reduce_sum(loss_1__loss_2, output_shape=[])
            v2v2 = reduce_sum(loss_2__loss_2, output_shape=[])
            min_gamma = 0.001
            gamma = multiply(constant_float(params, value=(1 - min_gamma), shape=[]),
                             to_fp32(greater_equal(v1v2, v1v1)))
            gamma = add(gamma,
                        einsum([constant_float(params, value=min_gamma, shape=[]), to_fp32(greater_equal(v1v2, v2v2)),
                                to_fp32(equal(gamma, 0))], output_shape=[]))
            gamma = add(gamma,
                        einsum([optimizer_scalar(params, -1),
                                to_fp32(equal(gamma, 0)),
                                add(v1v2, negative(v2v2)),
                                reciprocal(add(v1v1, v2v2) - multiply(-2, v1v2))],
                               output_shape=[]))

            loss = add(multiply(loss_list[0], gamma), multiply(loss_list[1], (1 - gamma)))

        operations = loss.graph.operations
        xs = [x.outputs[0] for x in params.mesh.graph.trainable_variables]
        tensor_to_var = dict(zip(xs, params.mesh.graph.trainable_variables))
        loss_grad = constant_scalar(params, 1.0)
        downstream = set(xs)

        for op in operations:
            if op.has_gradient and (set(op.inputs) & downstream):
                downstream |= set(op.outputs)

        tensor_to_gradient: typing.Dict[mtf.Tensor, typing.List[int, int, mtf.Tensor]] = {loss: [0, 0, loss_grad]}

        with tf.variable_scope(loss.graph.captured_variable_scope):
            for op in operations[::-1]:
                grad_outputs = []
                for out in op.outputs:
                    if out not in tensor_to_gradient:
                        grad_outputs.append(None)
                        continue

                    grad_list: typing.Tuple[int, int, mtf.Tensor] = tensor_to_gradient[out]
                    grad_outputs.append(grad_list[2])
                    grad_list[0] += 1

                    if grad_list[0] == len(grad_list[2].operation.inputs):
                        del tensor_to_gradient[out]

                if not op.has_gradient or not any(grad_outputs) or not (set(op.inputs) & downstream):
                    continue
                ctx = OptimizerCtx(op, grad_outputs, downstream, tensor_to_gradient, tensor_to_var, params,
                                   loss_idx, update_ops, debug_gradients_dict, loss_list, first_grad, loss_1__loss_1,
                                   loss_1__loss_2, loss_2__loss_2, mstep, step, dtype, beta1, beta2,
                                   learning_rate)
                for _ in gradients(ctx):
                    full_name = f'{tf.get_variable_scope().name}/f"{ctx.var.name}/{params.optimizer}/grad_accumulation'
                    if fn == "accumulate" or full_name in params.mesh.graph.name_to_variable:
                        ctx.grad_buffer = variable(params, ctx.var, "grad_accumulation", ctx.var.shape)
                    scoped(fn, gradient_accumulation if fn == "accumulate" else update, ctx)
    return params.mesh.graph.trainable_variables[0].graph.combine_assignments(update_ops), \
           learning_rate_ctx.learning_rate, debug_gradients_dict