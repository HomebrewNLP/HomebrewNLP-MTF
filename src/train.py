"""
Contains functions to create a training loop and log its outputs to tensorboard
"""
import threading
import time
import typing

import mesh_tensorflow as mtf
import numpy as np
import tensorflow.compat.v1 as tf
from tensorflow.compat.v1 import tpu
from tensorflow.python.framework import ops
from tensorflow.python.ops import summary_ops_v2 as summary
from tensorflow.python.tpu import tpu_feed
from tensorflow.python.tpu.ops import tpu_ops

from .dataclass import ModelParameter
from .model import build
from .optimizers import get_optimizer

tf.config.optimizer.set_experimental_options({"layout_optimizer":              True,
                                              "constant_folding":              True,
                                              "shape_optimization":            True,
                                              "remapping":                     True,
                                              "arithmetic_optimization":       True,
                                              "dependency_optimization":       True,
                                              "loop_optimization":             True,
                                              "function_optimization":         True,
                                              "debug_stripper":                True,
                                              "disable_model_pruning":         False,
                                              "scoped_allocator_optimization": True,
                                              "pin_to_host_optimization":      False,
                                              "implementation_selector":       True,
                                              "auto_mixed_precision":          True,
                                              "disable_meta_optimizer":        False,
                                              "min_graph_nodes":               0
                                              })


class CapturedObject(object):
    """A placeholder to capture an object.
    This is useful when we need to capture a Python object in the Tensorflow
    control flow body function and use it outside the control flow.
    """

    def __init__(self):
        self._object = None
        self._captured = False

    def capture(self, o):
        if self._captured:
            raise RuntimeError(
                    'InternalError: Object can capture only once. Please file bug.')

        self._captured = True
        self._object = o

    def get(self):
        if not self._captured:
            raise RuntimeError(
                    'InternalError: Object is not captured properly before `get`. '
                    'Please file bug.')
        return self._object


_NONE_PNUM = None
_NO_DATA = None

HOST_ID_TO_TF_DEVICE = "/job:worker/task:{:d}/device:CPU:0"


def simd_mesh_impl_input_reader(params: ModelParameter, ds_creator):
    """ Handles input pipeline for SimdMeshImpl.
        Input pipeline for the SIMD implementation of MeshTensorflow.
        Note:
          The efficiency is optimized according to the shape of the 0-th tensor:
          params.input_pipeline_shape[0]. We recommand you to put the largest tensor as the
          0-th input.
        """
    input_initializers = []
    infeed_thread: typing.List[typing.Optional[threading.Thread]] = [None]
    enqueue = None

    num_cores = params.mesh_impl.device_assignment.num_replicas

    ordered_ordinals = np.zeros((num_cores,), dtype=np.int32)
    ordered_hosts = np.zeros((num_cores,), dtype=str)
    ordered_host_ids = np.zeros((num_cores,), dtype=np.int32)

    for pnum in range(num_cores):
        physical_pnum = params.mesh_impl.l2p(pnum)
        host_device = params.mesh_impl.device_assignment.host_device(replica=physical_pnum)
        # For MTF, there's always 1 core per replica. So logical_core=0.
        ordered_ordinals[pnum] = params.mesh_impl.device_assignment.tpu_ordinal(replica=physical_pnum, logical_core=0)
        ordered_hosts[pnum] = host_device
        ordered_host_ids[pnum] = int(host_device.lower().split("/task:")[1].split("/device:")[0])
    num_hosts = len(np.unique(ordered_host_ids))
    pnum_maps = []
    batch_size = params.input_pipeline_shape[0].to_integer_list[0]
    for shape in params.input_pipeline_shape:
        # Make sure that the batch size is the same across all input tensors.
        if batch_size != shape.to_integer_list[0]:
            raise ValueError
        s_shape = params.mesh_impl.slice_shape(shape)
        shape_list = [dim_size // s_dim_size for dim_size, s_dim_size in zip(shape.to_integer_list, s_shape)]
        pnum_map = -np.ones(shape_list + [num_cores // np.prod(shape_list)], dtype=np.int32)

        for pnum in range(num_cores):
            coord = tuple([d // s for d, s in zip(params.mesh_impl.slice_begin(shape, pnum), s_shape)])
            pnum_array_ref = pnum_map[coord]
            for idx, value in enumerate(pnum_array_ref):
                if value == -1:
                    pnum_array_ref[idx] = pnum
                    break

        if np.any(pnum_map == -1):
            raise ValueError
        pnum_maps.append(pnum_map)

    # For each sub-batch, we need to know which host should read it.
    hosts_to_hold_ds = [num_hosts - 1]
    if params.run_mode != 'sample':
        hosts_to_hold_ds.clear()
        num_dss_per_host = np.zeros((num_hosts,))
        for sub_batch_pnum_map in pnum_maps[0]:
            host_id = np.argmax(np.sum(np.equal(ordered_host_ids.take(sub_batch_pnum_map.flatten(), 0).reshape(1, -1),
                                                np.arange(num_hosts).reshape(-1, 1)), 1) - num_dss_per_host)
            num_dss_per_host[host_id] -= 0.1 / num_hosts
            hosts_to_hold_ds.append(host_id)
    sub_batch_size = batch_size // len(hosts_to_hold_ds)

    if sub_batch_size * len(hosts_to_hold_ds) != batch_size:
        raise ValueError

    # For each sub-batch, create a SubBatchSlicer object.
    # Get the list of pnums for each input.
    all_laidout_tensors = [[_NO_DATA] * len(params.input_pipeline_shape) for _ in range(num_cores)]
    for sub_batch_i, host_id in enumerate(hosts_to_hold_ds):
        with ops.device(HOST_ID_TO_TF_DEVICE.format(host_id)):
            dset = ds_creator(params, sub_batch_size, sub_batch_i, len(hosts_to_hold_ds))
            ds_iterator = dset.make_initializable_iterator()
            input_initializers.append(ds_iterator.initializer)

            all_input_tensors = ds_iterator.get_next()
            all_sub_batch_pnums = [pnum_map.flatten().tolist() if params.run_mode == 'sample' else
                                   pnum_map[sub_batch_i, ...].flatten().tolist()
                                   for pnum_map in pnum_maps]
            if len(all_input_tensors) != len(params.input_pipeline_shape):
                raise ValueError

            for idx, input_tensor in enumerate(all_input_tensors):
                sub_batch_pnums = all_sub_batch_pnums[idx]
                mtf_input_shape = params.input_pipeline_shape[idx]

                # Initialize the cache for each
                slice_dict = {}

                for pnum in sub_batch_pnums:
                    s_begin = tuple(params.mesh_impl.slice_begin(mtf_input_shape, pnum))
                    if params.run_mode == 'sample':
                        # Always slice from 0 in the first dimension (batch dimension), since
                        # input_tensor a sub-batch tensor.
                        s_begin = list(s_begin)
                        s_begin[0] = 0
                        s_begin = tuple(s_begin)

                    if s_begin in slice_dict:
                        all_laidout_tensors[pnum][idx] = tf_tensor
                        continue
                    tf_tensor = tf.slice(input_tensor, s_begin, params.mesh_impl.slice_shape(mtf_input_shape))

                    slice_dict[s_begin] = tf_tensor
                    all_laidout_tensors[pnum][idx] = tf_tensor

    with ops.device(HOST_ID_TO_TF_DEVICE.format(hosts_to_hold_ds[0])):
        laidout_tensors0 = all_laidout_tensors[0]
        infeed = tpu_feed.InfeedQueue(number_of_tuple_elements=len(laidout_tensors0),
                                      tuple_types=[x.dtype for x in laidout_tensors0],
                                      tuple_shapes=[x.shape for x in laidout_tensors0])
        enqueue = infeed.generate_enqueue_ops(all_laidout_tensors,
                                              tpu_ordinal_function=lambda x: ordered_ordinals[x],
                                              placement_function=lambda x: ordered_hosts[x])

    def _thread_fn(sess):
        time.sleep(1)
        while True:
            sess.run(enqueue)

    def _start(sess: tf.Session):
        """Start running enqueue ops in a thread.
        Args:
          sess: A tf.Session.
        """

        sess.run(input_initializers)
        infeed_thread[0] = threading.Thread(target=_thread_fn, args=(sess,))
        infeed_thread[0].start()

    return infeed, _start


class CheckpointLoaderHook(tf.estimator.SessionRunHook):
    """Load checkpoint right after the session started."""

    def __init__(self, checkpoint_dir):
        self.checkpoint_dir = checkpoint_dir

    def after_create_session(self, session, coord):
        # pylint: disable=protected-access
        saver_collection = tf.get_collection(tf.GraphKeys.SAVERS)
        if saver_collection:
            saver = saver_collection[0]
            check_point = tf.train.latest_checkpoint(self.checkpoint_dir)
            if check_point:
                saver.restore(session, check_point)


def computation_func(params: ModelParameter, input_fn: typing.Callable,
                     session_config, tpu_cluster_resolver, callback_fns):
    captured_hooks = CapturedObject()
    captured_output_dtypes_shapes = CapturedObject()

    def model_fn(*args):
        """
        Create model partitioned graph given example input tensor
        :param features: inputs and targets in dict
        :param mode: training mode
        :param params: serialized dict of ModelParameters instance
        :return: tpu estimator spec
        """

        def _add_summary(tf_loss, value, global_step):
            """Add all summaries."""

            def _host_loss_summary(tf_loss, value, global_step):
                """Add summary.scalar in host side."""
                gs = tf.cast(global_step, tf.int64)

                sum_ops = []

                for key in value.keys():
                    sum_ops.append(summary.scalar(key, value[key], step=gs))
                with tf.control_dependencies(sum_ops):
                    return tf.identity(tf_loss)

            # Cast the global step to tf.int32, since
            # outside_compilation does not support tf.int64.
            tf_loss = tpu.outside_compilation(_host_loss_summary, tf_loss, value, tf.cast(global_step, tf.int32))

            return tf_loss

        # Get global step
        global_step = tf.train.get_or_create_global_step()

        # Construct mtf graph + mesh from params
        graph = mtf.Graph()
        mesh_shape = mtf.convert_to_shape(params.mesh_shape)
        layout_rules = mtf.convert_to_layout_rules(params.layout)

        # Mesh setup
        replica_cache_size = 300 * 1024 * 1024  # 300M per replica.
        worker0_mem = replica_cache_size * 8 * params.num_hosts
        devices_memory_usage = [worker0_mem] + [0] * (params.num_hosts - 1)
        var_placer = mtf.utils.BalancedVariablePlacer(params.cpu_devices, devices_memory_usage)
        mesh_devices = [""] * mesh_shape.size
        # mesh_impl = mtf.simd_mesh_impl.SimdMeshImpl(
        #        mesh_shape, layout_rules, mesh_devices, params.context.device_assignment)

        # Build mtf mesh object
        mesh = mtf.Mesh(graph, "mesh", var_placer)
        params.mesh = mesh

        # Build mtf_features & seq length dict for getting number of microbatches
        # We need to pack inputs into a dict to pass into serialize_training_step
        # params.mode = mode

        frame_input = None
        token_x_input = None
        token_y_input = None
        frame_mask = None
        token_mask = None

        if params.use_video:
            frame_input = mtf.import_laid_out_tensor(mesh, params.mesh_impl.LaidOutTensor([args[0]]),
                                                     params.frame_input_shape, "frame_input")

            if params.use_language:
                token_x_input = mtf.import_laid_out_tensor(mesh, params.mesh_impl.LaidOutTensor([args[1]]),
                                                           params.token_dim_shape, "tkn_src")
                token_y_input = mtf.import_laid_out_tensor(mesh, params.mesh_impl.LaidOutTensor([args[2]]),
                                                           params.token_dim_shape, "tkn_tgt")

                frame_mask = mtf.import_laid_out_tensor(mesh, params.mesh_impl.LaidOutTensor([args[3]]),
                                                        params.frame_mask_shape, "vid_msk")
                token_mask = mtf.import_laid_out_tensor(mesh, params.mesh_impl.LaidOutTensor([args[4]]),
                                                        params.token_dim_shape, "tkn_msk")

        elif params.use_language:

            token_x_input = mtf.import_laid_out_tensor(mesh, params.mesh_impl.LaidOutTensor([args[0]]),
                                                       params.token_dim_shape, "tkn_src")
            token_y_input = mtf.import_laid_out_tensor(mesh, params.mesh_impl.LaidOutTensor([args[1]]),
                                                       params.token_dim_shape, "tkn_tgt")

        if params.run_mode == 'sample' and params.use_autoregressive_sampling:
            sequence_dim = mtf.Dimension("sequence", params.time_patch_size)

            def cond_fn(position):
                is_done = mtf.greater_equal(position, sequence_dim.size)
                is_done = mtf.logical_or(is_done, mtf.greater_equal(position - params.initial_autoregressive_position,
                                                                    sequence_dim))
                is_done = mtf.reduce_sum(is_done)

                return mtf.logical_not(is_done)

            def body_fn(position, frame_input, token_x_input, token_y_input, frame_mask, token_mask, *states):
                with tf.variable_scope('jannet'):
                    if token_mask is None:
                        token_mask = mtf.ones(params.mesh, [], params.dtype)
                    else:
                        token_mask = mtf.cast(token_mask, params.dtype)
                    if frame_mask is None:
                        frame_mask = mtf.ones(params.mesh, [], params.dtype)
                    else:
                        frame_mask = mtf.cast(frame_mask, params.dtype)
                    video_loss, _, frame_out, token_out = build(params,
                                                                frame_input,
                                                                token_x_input,
                                                                token_y_input,
                                                                frame_mask,
                                                                token_mask)

                language_token_per_frame_dim = mtf.Dimension("language_token_per_frame",
                                                             params.language_token_per_frame)

                # (batch, sequence_dim, language_token_patch, token_patch_size, vocab_size) ->
                # (batch, sequence_dim, language_token_per_frame, vocab_size)
                token_out = mtf.reshape(token_out, new_shape=mtf.Shape([params.batch_dim,
                                                                        sequence_dim,
                                                                        language_token_per_frame_dim,
                                                                        params.vocab_dim]))

                # (batch, sequence_dim, language_token_per_frame, vocab_size) ->
                # (batch, sequence_dim, language_token_per_frame)
                token_out: mtf.Tensor = mtf.argmax(token_out, reduced_dim=params.vocab_dim)

                # (language_token_per_frame_dim)
                token_mask_out_range = mtf.range(mesh, language_token_per_frame_dim, dtype=tf.int32)
                # (language_token_per_frame_dim) -> (batch, sequence_dim, language_token_per_frame, vocab_size)
                token_mask_out_range = mtf.broadcast(token_mask_out_range, new_shape=token_out.shape)

                # (batch, sequence_dim, language_token_per_frame) -> (batch, sequence_dim)
                token_mask_out_argmin = mtf.argmax(mtf.negative(token_out), reduced_dim=language_token_per_frame_dim)

                # (batch, sequence_dim) -> (batch, sequence_dim, language_token_per_frame, vocab_size)
                token_mask_out_argmin = mtf.broadcast(token_mask_out_argmin, new_shape=token_out.shape)

                token_mask_out = mtf.less_equal(token_mask_out_range, token_mask_out_argmin)

                # (batch, sequence_dim, language_token_per_frame, vocab_size) ->
                # (batch_dim, sequence_dim, language_token_patch, token_patch_size)
                token_out = mtf.reshape(token_out, new_shape=params.token_dim_shape)
                token_mask_out = mtf.reshape(token_mask_out, new_shape=params.token_dim_shape)

                # (sequence_dim)
                one_hot_sequence = mtf.one_hot(position, sequence_dim, dtype=tf.int32)
                neg_one_hot_sequence = (1 - one_hot_sequence)

                # frame_input = mtf.pad(anonymize(frame_out, sequence_dim),[1, 0], anonymize_dim(sequence_dim)).name * mtf.cast(one_hot_sequence, tf.float32) + frame_input * mtf.cast(neg_one_hot_sequence, tf.float32)
                token_x_input = token_out * one_hot_sequence + token_x_input * neg_one_hot_sequence
                token_mask = token_mask_out * one_hot_sequence + token_mask * neg_one_hot_sequence

                position_out = position + 1

                return [position_out, frame_input, token_x_input, token_y_input, frame_mask, token_mask, video_loss]

            while_loop_inputs = [params.initial_autoregressive_position, frame_input,
                                 token_x_input, token_y_input, frame_mask, token_mask]
            '''

            def cond_fn(position, *states):
                is_done = mtf.greater_equal(position, params.sequence_dim.size)
                is_done = mtf.logical_or(is_done, mtf.greater_equal(position - params.initial_autoregressive_position,
                                                                    params.sequence_dim.size))
                is_done = mtf.reduce_sum(is_done)

                return mtf.logical_not(is_done)

            def body_fn(position, token_x, token_y, *states):
                with tf.variable_scope('jannet'):

                    _, _, frame_out, token_out = build(params,
                                                       mtf.ones(params.mesh, [], tf.float32),
                                                       token_x,
                                                       token_y_input, mtf.ones(params.mesh, [], tf.float32),
                                                       mtf.ones(params.mesh, [], tf.float32))

                # (batch, sequence_dim, 1, vocab_size) ->
                # (batch, sequence_dim, language_token_per_frame)
                _token_out: mtf.Tensor = mtf.argmax(token_out, reduced_dim=params.vocab_dim)

                # (sequence_dim)
                one_hot_sequence = mtf.one_hot(position, output_dim=params.sequence_dim, dtype=tf.int32)
                neg_one_hot_sequence = (1 - one_hot_sequence)

                token_x = _token_out * one_hot_sequence + token_x * neg_one_hot_sequence

                position_out = position + 1

                return [position_out, token_x, token_y]

            while_loop_inputs = [(mtf.zeros(params.mesh, [], tf.int32) + params.initial_autoregressive_position),
                                 token_x_input, token_y_input]

            _, token_out, _ = mtf.while_loop(cond_fn=cond_fn, body_fn=body_fn, inputs=while_loop_inputs)

        else:
            with mtf.utils.outside_all_rewrites(), tf.variable_scope('jannet'):
                if token_mask is None:
                    token_mask = mtf.ones(params.mesh, [], tf.float32)
                else:
                    token_mask = mtf.cast(token_mask, tf.float32)
                if frame_mask is None:
                    frame_mask = mtf.ones(params.mesh, [], tf.float32)
                else:
                    frame_mask = mtf.cast(frame_mask, tf.float32)
                if frame_input is not None:
                    frame_input = mtf.cast(frame_input, tf.float32)
                video_loss, token_loss, frame_out, token_out = build(params,
                                                                     frame_input,
                                                                     token_x_input,
                                                                     token_y_input,
                                                                     frame_mask,
                                                                     token_mask)
                loss = video_loss + token_loss
                video_loss = video_loss * frame_mask.size / mtf.reduce_sum(frame_mask)
                token_loss = token_loss * token_mask.size / mtf.reduce_sum(token_mask)

        if params.run_mode == 'train':
            update_ops = get_optimizer(mesh, loss, params)
        else:  # run_mode == 'sample'

            if params.use_video:
                frame_out = mtf.anonymize(frame_out)

            if params.use_language:
                token_out = mtf.anonymize(token_out)

        total_parameters = 0
        for variable in graph.trainable_variables:
            shape = variable.shape.dims
            variable_parameters = 1

            for dim in shape:
                variable_parameters *= dim.size
            total_parameters += variable_parameters

        print(f"\n\nN TRAINABLE VARS:\n{total_parameters:,}\n\n")
        all_dim_names = []

        for variable in graph.all_variables:
            names = variable.shape.dimension_names
            all_dim_names.append(names)

        # Print all dim names in graph & write to file
        all_dim_names = [item for sublist in all_dim_names for item in sublist]  # Flatten all dims
        unique_dims = list(set(all_dim_names))
        print("ALL DIM NAMES:")
        for dim_name in unique_dims:
            print(dim_name)
        print('\n')

        lowering = mtf.Lowering(graph, {mesh: params.mesh_impl}, autostack=True)

        log_dict = {}

        if params.run_mode == 'train':
            if params.use_video:
                video_loss = lowering.export_to_tf_tensor(video_loss)
                video_loss = tf.cast(video_loss, tf.float32)
                log_dict.update({'video_loss': video_loss})

            if params.use_language:
                token_loss = lowering.export_to_tf_tensor(token_loss)
                token_loss = tf.cast(token_loss, tf.float32)
                log_dict.update({'token_loss': token_loss})

            tf_loss = lowering.export_to_tf_tensor(loss)
            tf_loss = tf.cast(tf_loss, tf.float32)
            tf_loss = _add_summary(tf_loss=tf_loss, value=log_dict, global_step=global_step)

        else:  # run_mode == 'sample'
            predictions = {}
            if params.use_video:
                predictions.update({'frame_out': lowering.export_to_tf_tensor(frame_out)})
                predictions.update({'frame_tgt': args[0]})

            if params.use_language:
                predictions.update({'token_out': lowering.export_to_tf_tensor(token_out)})
                if params.model_mode == 'jannet':
                    predictions.update({'token_tgt': args[2]})
                else:
                    predictions.update({'token_tgt': args[1]})

        if params.run_mode == 'train':

            # Creates train_op
            tf_update_ops = [lowering.lowered_operation(op) for op in update_ops]
            tf_update_ops.append(tf.assign_add(global_step, 1))  # Need to manually increment global_step
            tf.logging.info(f"tf_update_ops: {tf_update_ops}")

            with mtf.utils.outside_all_rewrites():

                hooks = [mtf.MtfRestoreHook(lowering)]
                if params.use_checkpointing:
                    saver = tf.train.Saver(
                            tf.global_variables(),
                            sharded=True,
                            max_to_keep=10,
                            keep_checkpoint_every_n_hours=2,
                            defer_build=False,
                            save_relative_paths=True)
                    tf.add_to_collection(tf.GraphKeys.SAVERS, saver)

                    hooks.append(tf.train.CheckpointSaverHook(
                            params.model_path,
                            save_steps=params.steps_per_checkpoint,
                            saver=saver,
                            listeners=[mtf.MtfCheckpointSaverListener(lowering)]))

                captured_hooks.capture(hooks)

                return tf.group([tf_loss] + tf_update_ops)

        else:  # run_mode == 'sample'

            predictions = [tf.cast(predictions[key], tf.float32) for key in predictions.keys()]
            predictions_dtypes = [pred.dtype for pred in predictions]
            predictions_shapes = [pred.shape for pred in predictions]
            captured_hooks.capture([mtf.MtfRestoreHook(lowering), None])
            captured_output_dtypes_shapes.capture([predictions_dtypes, predictions_shapes])

            return tpu_ops.outfeed_enqueue_tuple(predictions)

    infeed, simd_input_reader = simd_mesh_impl_input_reader(params, input_fn)

    computation = tpu.replicate(computation=model_fn,
                                inputs=[[]] * params.num_cores,
                                infeed_queue=infeed,
                                device_assignment=params.d_assignment)

    if params.run_mode == 'sample':
        output_dtypes, output_shapes = captured_output_dtypes_shapes.get()
        outfeed_dequeue_ops = []

        # Create outfeed_dequeue_ops.
        for host_id in range(params.num_hosts):
            # pylint: disable=protected-access
            with ops.device(HOST_ID_TO_TF_DEVICE.format(host_id)):
                for device_ordinal in range(params.num_cores_per_host):
                    outfeed_dequeue_op = tpu_ops.outfeed_dequeue_tuple(
                            dtypes=output_dtypes,
                            shapes=output_shapes,
                            device_ordinal=device_ordinal)

                    # We don't need output other than from core 0.
                    if outfeed_dequeue_ops:
                        outfeed_dequeue_ops.append([tf.reduce_mean(x) for x in outfeed_dequeue_op])
                    else:
                        outfeed_dequeue_ops.append(outfeed_dequeue_op)

    slice_hook = [hook for hook in captured_hooks.get()]
    ckpt_loader_hook = CheckpointLoaderHook(params.model_path)
    if params.run_mode == 'train':

        step_counter_hook = tf.train.StepCounterHook(every_n_steps=10)
        all_hooks = [ckpt_loader_hook, step_counter_hook] + slice_hook

        # if params.write_summary:
        flush_summary = summary.flush()

        with tf.train.MonitoredTrainingSession(master=tpu_cluster_resolver.master(),
                                               hooks=all_hooks, config=session_config) as sess:
            simd_input_reader(sess)
            summary.initialize(session=sess)

            for i in range(params.current_step, params.train_steps):
                sess.run(computation)
                if (i + 1) % params.summary_flush_interval == 0:
                    sess.run(flush_summary)
                for fn in callback_fns:
                    fn(i)

    else:  # run_mode == 'sample'

        all_hooks = [ckpt_loader_hook, slice_hook[0]]

        with tf.train.MonitoredSession(
                session_creator=tf.train.ChiefSessionCreator(master=tpu_cluster_resolver.master(),
                                                             config=session_config),
                hooks=all_hooks) as sess:

            simd_input_reader(sess)

            while True:
                sess.run(computation)
                out = sess.run(outfeed_dequeue_ops)[0]
                for fn in callback_fns:
                    fn(out)
