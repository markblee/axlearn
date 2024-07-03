# Copyright © 2023 Apple Inc.
#
# Some of the code in this file is adapted from:
#
# tensorflow/lingvo:
# Copyright 2021 The TensorFlow Authors. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License").

"""A generic pipeline layer.

https://arxiv.org/abs/1811.06965

Adapted from:
https://github.com/tensorflow/lingvo/blob/2d46faf8/lingvo/jax/layers/pipeline.py

A pipeline layer consists a stack of N identical sub layers, where
  * The variables are stacked across layers. Each stacked variable has shape [N, ...].
  * The inputs are divided into M microbatches and have shape [M, ...].
  * The processing happens in a loop consisting of M+N-1 steps.
    In each step 0 <= t < M+N-1, microbatch 0 <= m < M will be processed by layer (t - m)
    if 0 <= t - m < N.
    Or, expressed in layer-parallel terms, layers will process microbatch slice [t:t-N:-1] at step t
    (assuming that we pad the microbatches with N - 1 dummy microbatches at both ends).
"""

import dataclasses
import functools
from typing import Callable, NamedTuple, Optional, Tuple, Union

import jax.ad_checkpoint
from jax import numpy as jnp
from jax.sharding import PartitionSpec

from axlearn.common import param_init
from axlearn.common.base_layer import BaseLayer, FactorizationSpec, NestedParameterSpec
from axlearn.common.config import REQUIRED, InstantiableConfig, Required, config_class
from axlearn.common.module import Module, NestedTensor, Tensor, child_context, new_output_collection
from axlearn.common.utils import (
    Nested,
    NestedPartitionSpec,
    VDict,
    get_or_none,
    shapes,
    split_prng_key,
    with_sharding_constraint,
)


def transpose_to_pipeline_stage_inputs(x: Tensor, partition_spec: Optional[PartitionSpec] = None):
    """Transposes `x` from the 'layer-major' layout to the 'pipeline-major' layout.

    Args:
        x: A Tensor of shape [N, M, ...], where x[i, j] represents layerwise inputs for pipeline
            layer[i] and microbatch[j].
        partition_spec: The partition spec for x.

    Returns:
        A Tensor of shape [M + N - 1, N, ...], where x'[t, i] represents the layerwise inputs for
        timestep[t] and layer[i]: x'[i + j, i] == x[i, j].
    """
    n, m = x.shape[:2]
    # [N, M + N, ...].
    x = jnp.pad(x, [(0, 0), (0, n)] + [(0, 0)] * (x.ndim - 2))
    # [N * (M + N), ...].
    x = jnp.reshape(x, [-1] + list(x.shape[2:]))
    # [N * (M + N - 1), ...].
    x = x[:-n]
    # [N, M + N - 1, ...].
    x = jnp.reshape(x, [n, m + n - 1] + list(x.shape[1:]))
    # Apply sharding constraints at the first opportunity after reshapes
    # (i.e. when the input is first in the right shape for the constraint again).
    if partition_spec is not None:
        x = with_sharding_constraint(x, partition_spec)
    # [M + N - 1, N, ...].
    x = jnp.transpose(x, [1, 0] + list(range(2, x.ndim)))
    return x


def transpose_from_pipeline_stage_outputs(
    x: Tensor, partition_spec: Optional[PartitionSpec] = None
):
    """Transposes `x` from the 'pipeline-major' layout to the 'layer-major' layout.

    Args:
        x: A Tensor of shape [M + N - 1, N, ...], where x[t, i] represents the layerwise outputs of
            timestep[t] and layer[i].
        partition_spec: The partition spec for x' (layer-major).

    Returns:
        A Tensor of shape [N, M, ...], where x'[i, j] represents layerwise outputs of pipeline
        layer[i] and microbatch[j]: x'[i, j] == x[i + j, i].
    """
    t, n = x.shape[:2]
    m = t - n + 1
    # [N, M+N-1, ...].
    x = jnp.transpose(x, [1, 0] + list(range(2, x.ndim)))
    # [N * (M+N-1), ...].
    x = jnp.reshape(x, [-1] + list(x.shape[2:]))
    # [N * (M+N), ...].
    x = jnp.pad(x, [(0, n)] + [(0, 0)] * (x.ndim - 1))
    # [N, M+N, ...].
    x = jnp.reshape(x, [n, m + n] + list(x.shape[1:]))
    # Apply sharding constraints at the first opportunity after reshapes
    # (i.e. when the input is first in the right shape for the constraint again).
    if partition_spec is not None:
        x = with_sharding_constraint(x, partition_spec)
    # [N, M, ...].
    x = x[:, :m]
    return x


class Pipeline(BaseLayer):
    """https://arxiv.org/abs/1811.06965."""

    @config_class
    class Config(BaseLayer.Config):
        layer: Required[InstantiableConfig] = REQUIRED  # The config for the sub layer.
        num_layers: Required[int] = REQUIRED  # Repeat layers specified in `layer` this many times.
        num_microbatches: Required[int] = REQUIRED

    def __init__(self, cfg: Config, *, parent: Optional[Module]):
        super().__init__(cfg, parent=parent)
        cfg = self.config
        self._num_stages = cfg.num_layers
        self._num_microbatches = cfg.num_microbatches
        self._add_child("layer", cfg.layer)

    def create_parameter_specs_recursively(self) -> NestedParameterSpec:
        cfg: Pipeline.Config = self.config
        specs = VDict(**super().create_parameter_specs_recursively())

        def transform_factorization_spec(
            spec: Optional[FactorizationSpec],
        ) -> Optional[FactorizationSpec]:
            if spec is None:
                return None
            return FactorizationSpec(axes=[None] + list(spec.axes))

        return jax.tree_util.tree_map(
            lambda spec: dataclasses.replace(
                spec,
                shape=(cfg.num_layers, *spec.shape),
                mesh_axes=PartitionSpec("pipeline", *spec.mesh_axes),
                factorization=transform_factorization_spec(spec.factorization),
                fan_axes=param_init.maybe_prepend_axis(
                    spec.fan_axes, axis_type=param_init.FanAxes.AxisType.BATCH_AXIS
                ),
            ),
            specs,
        )

    def initialize_parameters_recursively(
        self,
        prng_key: Union[Tensor, VDict],
        *,
        prebuilt: Optional[NestedTensor] = None,
    ) -> NestedTensor:
        def init(prng_key_i, prebuilt_i):
            return VDict(
                layer=self.layer.initialize_parameters_recursively(
                    prng_key_i, prebuilt=get_or_none(prebuilt_i, "layer")
                )
            )

        cfg: Pipeline.Config = self.config
        return jax.vmap(init)(split_prng_key(prng_key, cfg.num_layers).keys, prebuilt)

    class Output(NamedTuple):
        carry: NestedTensor
        ys: NestedTensor

    def _run(
        self,
        fn: Callable[[NestedTensor, NestedTensor], NestedTensor],
        carry: Optional[NestedTensor] = None,
        *,
        xs: Optional[NestedTensor] = None,
        carry_partition_spec: Optional[NestedPartitionSpec] = None,
        xs_partition_spec: Optional[NestedPartitionSpec] = None,
        ys_partition_spec: Optional[NestedPartitionSpec] = None,
    ):
        """Invokes 'fn' for each sub-layer with inputs already with the microbatch axis.

        Args:
            fn: A function with args (carry, x) returning a dict(carry=..., y=...).
            carry: A nested tensor for the iterative input of the 0'th sub-layer.
                It must have shape [M, microbatch_size, ...].
            xs: A nested tensor with separate inputs for each sub-layer, where each leaf value T is
                a tensor of shape [cfg.num_layers, M, microbatch_size, ...] and T[i, j, ...]
                represents layer-wise inputs of microbatch j to the i'th sub-layer.
            carry_partition_spec: Partition spec for the carry tensors.
                If None, tensors will be replicated.
            xs_partition_spec: Partition spec for the input xs tensors. If None, tensors will be
                replicated except for sharding along the "pipeline" mesh axis.
            ys_partition_spec: Partition spec for the output ys tensors. If None, tensors will be
                replicated except for sharding along the "pipeline" mesh axis.

        Returns:
            A dict with the following keys:
            - carry: A nested tensor with the same structure as the input carry representing the
                iterative output of the last sub-layer.
            - ys: A nested tensor where each leaf value T is a tensor of shape
                [cfg.num_layers, M, microbatch_size, ...] and T[i, ...] represents layer-wise output
                from the i'th sub-layer.
        """
        self.vlog(1, "carry=%s xs=%s", shapes(carry), shapes(xs))

        carry_leaves = jax.tree_util.tree_leaves(carry)
        if not carry_leaves:
            raise ValueError("Expected at least one input leaf.")
        if carry_leaves[0].ndim < 2:
            raise ValueError(
                "Expected leaves to have shape `[num_microbatches, microbatch_size, ...]`; "
                f"instead, found {carry_leaves[0].shape}."
            )

        # Number of microbatches.
        m = carry_leaves[0].shape[0]
        # Number of pipeline stages.
        n = self._num_stages

        if carry is None:
            carry = {}
            carry_partition_spec = {}
        if carry_partition_spec is None:
            carry_partition_spec = jax.tree_util.tree_map(
                lambda x: PartitionSpec(*[PartitionSpec.UNCONSTRAINED for _ in x.shape]), carry
            )
        if xs is None:
            xs = {}
            xs_partition_spec = {}
        if xs_partition_spec is None:
            xs_partition_spec = jax.tree_util.tree_map(
                lambda x: PartitionSpec(
                    "pipeline", *[PartitionSpec.UNCONSTRAINED for _ in x.shape[1:]]
                ),
                xs,
            )

        def pad_carry(v_carry: Tensor, partition_spec: PartitionSpec):
            """Pads input from [M, microbatch_size, ...] to [M, N, microbatch_size, ...].

            We pad explicitly instead of broadcasting along N to avoid gradient accumulation in the
            backward pass (only the first stage produces non-zero gradients.)
            """
            v_carry = jnp.pad(
                jnp.expand_dims(v_carry, 1), [(0, 0), (0, n - 1)] + [(0, 0)] * (v_carry.ndim - 1)
            )
            return with_sharding_constraint(v_carry, partition_spec)

        # Leaves are of shape [M, N, microbatch_size, ...].
        # TODO(markblee): For M % N == 0, we can shard the M dim over 'streams', e.g:
        # https://github.com/google/praxis/blob/c41477c601fea125ae58f136f139758c34d121b8/praxis/layers/pipeline.py#L140-L149
        per_stage_inputs = jax.tree_util.tree_map(pad_carry, carry, carry_partition_spec)

        # Transpose from "layer-major" [N, M, ...] to "pipeline-major" [N + M - 1, N, ...].
        #
        # Note: for efficient decoding we may want to skip transposes and keep decoding states in
        # the "pipeline-major" form (i.e., in the shape of [N + M - 1, N, ...]).
        #
        # To be investigated in the future.
        padded_xs = jax.tree_util.tree_map(
            transpose_to_pipeline_stage_inputs, xs, xs_partition_spec
        )
        self.vlog(2, "padded_xs=%s", shapes(padded_xs))

        def stack_and_reshape(*keys):
            keys = jnp.stack(keys)
            return jnp.reshape(keys, [m + n - 1, n] + list(keys.shape[1:]))

        prng_keys = jax.random.split(self.prng_key, (m + n - 1) * n)
        prng_keys = jax.tree_util.tree_map(stack_and_reshape, *prng_keys)

        layer_output_collection = new_output_collection()
        with child_context("layer", output_collection=layer_output_collection) as layer_context:

            def vmap_fn(
                state_n: Tensor, prng_key_tn: jax.random.PRNGKey, carry_tn: Tensor, x_tn: Tensor
            ):
                """Invokes fn for one microbatch and one stage.

                Args:
                    state_n: The parameters of the n'th layer.
                    prng_key_tn: The PRNG key for the v_carry'th timestep and n'th layer.
                    carry_tn: The carry input for the v_carry'th timestep and n'th layer.
                    x_tn: The xs input for the v_carry'th timestep and n'th layer.

                Returns:
                    dict(
                        carry=<carry output>,
                        y=<layerwise output>,
                        output_collection=<auxiliary outputs>,
                    ).
                """
                output_collection_tn = new_output_collection()
                with child_context(
                    "iter",
                    module=layer_context.module,
                    state=state_n,
                    prng_key=prng_key_tn,
                    output_collection=output_collection_tn,
                ):
                    carry_tn, y_tn = fn(carry_tn, x_tn)
                self.vlog(3, "output_collection_tn=%s", shapes(output_collection_tn))
                return dict(carry=carry_tn, y=y_tn, output_collection=output_collection_tn)

            @functools.partial(
                jax.ad_checkpoint.checkpoint,
                prevent_cse=False,
                policy=jax.checkpoint_policies.nothing_saveable,
            )
            def scan_fn(
                carry_in: NestedTensor,
                xs_t: Tuple[NestedTensor, NestedTensor],
            ):
                """Processes timestep `t` in the pipeline (in parallel across pipeline stages).

                Args:
                    carry_in: A NestedTensor containing loop state carried across scan iterations.
                    xs_t: A tuple (prng_key_t, x_t). Each is a NestedTensor with leaves of shape
                        [N, ...] or [1, ...].

                Returns:
                    (carry_out, ys_t), where:
                    - `carry_out` will be used as `carry_in` in the next scan iteration, and thus
                        has the same structure and shape as `carry_in`.
                    - `ys_t` is dict(carry=..., y=..., output_collection=...) and will be stacked as
                        `ys` after scan is done.
                        Note that `carry` does not necessarily have the same structure as
                        `carry_out`, and represents the stage-wise carry output from `fn` with
                        leaves of shape [N, ...]. While only last-stage outputs are needed, we
                        retain [N, ...] for consistent sharding.
                        `y` is a `NestedTensor` representing the stage-wise output of `fn` with
                        leaves of shape [N, ...].
                        `output_collection` is an `OutputCollection` representing the auxiliary
                        outputs of `fn` with leaves of shape [N, ...].
                """

                # Input state.
                t = carry_in["t"]
                carry_output_t_1 = carry_in["carry_output_t_1"]
                per_stage_inputs = carry_in["per_stage_inputs"]

                # Per-timestep inputs. Each leaf tensor has shape [N, ...] or [1, ...].
                prng_key_t, x_t = xs_t

                # Compute vmap inputs. When t >= m, we feed dummy inputs to the pipeline until the
                # pipeline is flushed. Note that at the end of all iterations we only extract the
                # last-stage outputs from the stacked vmap outputs.
                # Leaves are of shape [N, ...] representing per-stage inputs.
                vmap_in = self._compute_carry_input(per_stage_inputs, carry_output_t_1, t=t)

                # Use stop_gradient for invalid (bubble) microbatch iterations. This jnp.where will
                # be optimized away by XLA, but in the backward pass it will be masking with zeros.
                state = jax.tree_util.tree_map(
                    lambda x: jnp.where(self._is_valid_stage(x, t=t), x, jax.lax.stop_gradient(x)),
                    layer_context.state,
                )

                # Parallel processing along the N axis.
                vmap_out = jax.vmap(vmap_fn)(state, prng_key_t, vmap_in, x_t)
                self.vlog(3, "vmap_out.output_collection=%s", shapes(vmap_out["output_collection"]))

                # Output state.
                carry_out = dict(
                    t=t + 1,
                    carry_output_t_1=vmap_out["carry"],
                    per_stage_inputs=per_stage_inputs,
                )
                # TODO(markblee): Consider slicing out just the last-stage outputs of vmap_out.
                # Note that vmap outputs are typically sharded over stages and may incur extra
                # communication per-iteration (e.g. from broadcasting last stage outputs).
                return carry_out, vmap_out

            state_t0 = dict(
                # Current loop iteration.
                t=jnp.array(0, dtype=jnp.int32),
                # [N, microbatch_size, ...].
                carry_output_t_1=jax.tree_util.tree_map(
                    lambda x: jnp.zeros((n,) + x.shape[1:], dtype=jnp.bfloat16), carry
                ),
                # [M, N, microbatch_size, ...].
                per_stage_inputs=per_stage_inputs,
            )
            self.vlog(
                2,
                "state_t0=%s prng_keys=%s padded_xs=%s",
                shapes(state_t0),
                shapes(prng_keys),
                shapes(padded_xs),
            )
            _, scan_ys = jax.lax.scan(scan_fn, init=state_t0, xs=(prng_keys, padded_xs))

            def extract_outputs(x: Tensor, partition_spec: PartitionSpec) -> Tensor:
                # Extract the last-stage outputs at each iteration from the stacked carry. Note
                # that the initial N-1 iterations constitute a pipeline bubble where we don't have
                # any meaningful last-stage outputs yet.
                # Use lax.slice to guarantee the gradient is a pad.
                x = jnp.squeeze(
                    jax.lax.slice(x, [n - 1, x.shape[1] - 1] + [0] * (x.ndim - 2), x.shape), 1
                )
                return with_sharding_constraint(x, partition_spec)

            final_carry = jax.tree_util.tree_map(
                extract_outputs, scan_ys.pop("carry"), carry_partition_spec
            )

            ys = scan_ys["y"]
            if ys_partition_spec is None:
                ys_partition_spec = jax.tree_util.tree_map(
                    lambda x: PartitionSpec(
                        "pipeline", *[PartitionSpec.UNCONSTRAINED for _ in x.shape[1:]]
                    ),
                    ys,
                )
            # Transpose from pipeline-major [N + M - 1, N, ...] back to layer-major [N, M, ...].
            ys = jax.tree_util.tree_map(
                transpose_from_pipeline_stage_outputs, ys, ys_partition_spec
            )
            self.vlog(3, "scan_ys.output_collection=%s", shapes(scan_ys["output_collection"]))
            layer_output_collection.update(
                jax.tree_util.tree_map(
                    transpose_from_pipeline_stage_outputs, scan_ys["output_collection"]
                )
            )
            self.vlog(3, "layer_output_collection=%s", shapes(layer_output_collection))

        this_output_collection = self.get_invocation_context().output_collection
        layer_output = this_output_collection.add_child("layer")
        layer_output.module_outputs.update(**layer_output_collection.module_outputs)
        layer_output.state_updates.update(**layer_output_collection.state_updates)
        self.vlog(3, "this_output_collection=%s", shapes(this_output_collection))

        # Each summary value in `layer_output_collection` has shape (N, M, ...). For example,
        # if a repeated layer outputs a scalar summary value, it will have shape [N, M].
        # Below we split the stacked values and output them separately under scope
        # "layer{i}/microbatch{j}" so that scalar summaries can be handled correctly.
        for i in range(n):
            layer_i_output = this_output_collection.add_child(f"layer{i}")
            for j in range(m):
                microbatch_j_output = layer_i_output.add_child(f"microbatch{j}")
                microbatch_j_output.summaries.update(
                    **jax.tree_util.tree_map(
                        lambda x, i=i, j=j: x[i, j], layer_output_collection.summaries
                    )
                )
        return self.Output(carry=final_carry, ys=ys)

    def _compute_carry_input(
        self,
        per_stage_inputs: Nested[Tensor],
        carry_output_t_1: Nested[Tensor],
        *,
        t: Tensor,
    ) -> Nested[Tensor]:
        """Computes the carry input for timestep `t`.

        Args:
            per_stage_inputs: A nested Tensor with leaves v_input_t of shape [M, N, ...].
                per_stage_inputs[t, 0] == microbatch[t] if t < M, otherwise dummy values.
            carry_output_t_1: A nested Tensor with leaves of shape [N, ...], representing carry
                output of timestep {t-1}.
            t: A scalar representing current timestep.

        Returns:
            A nested Tensor with leaves of shape [N, ...]:
            - Stage 0 input will be per_stage_inputs[t % M, :1], that is, microbatch[t] if t < M;
            - Stage 1..N-1 inputs will be v_carry_output_t_1[:N-1], that is, the outputs of stages
                0..N-2 from iteration t-1.
        """
        m = self._num_microbatches

        def select_state_or_input(v_input_t: Tensor, v_carry_output_t_1: Tensor) -> Tensor:
            # Select the current microbatch index.
            v_input_t = v_input_t[t % m]
            # Shift-right t-1 vmap outputs along the N dim, such that outputs from prior stages are
            # fed into subsequent stages.
            ndim = v_carry_output_t_1.ndim
            padding = [(1, 0)] + [(0, 0)] * (ndim - 1)
            # Use lax.slice to guarantee the gradient is a pad.
            v_carry_output_t_1 = jax.lax.slice(
                jnp.pad(v_carry_output_t_1, padding), [0] * ndim, v_carry_output_t_1.shape
            )
            # v_carry_input_t[0, ...] = v_input_t[0, ...].
            # v_carry_input_t[n, ...] = v_carry_output_t_1[n, ...] for n > 0.
            return jnp.where(
                # For operation semantics of iota, see:
                # https://openxla.org/xla/operation_semantics#iota
                jax.lax.broadcasted_iota("int32", v_carry_output_t_1.shape, 0) == 0,
                v_input_t,
                v_carry_output_t_1,
            )

        return jax.tree_util.tree_map(select_state_or_input, per_stage_inputs, carry_output_t_1)

    def _is_valid_stage(self, per_stage_values: Tensor, *, t: Tensor) -> Tensor:
        """Returns a mask indicating whether per-stage values correspond to valid microbatches.

        Args:
            per_stage_values: A Tensor of shape [N, ...].
            t: A scalar representing current timestep.

        Returns:
            A mask of shape [N, 1, ...] broadcastable to `per_stage_values`. 1's indicate valid
            stages, 0's otherwise.
        """

        if per_stage_values.shape[0] != self._num_stages:
            raise ValueError(
                f"Leading dim {per_stage_values.shape[0]} does not match "
                f"number of stages {self._num_stages}."
            )
        stage_id = jnp.arange(self._num_stages, dtype=jnp.int32)
        mask = jnp.logical_and(stage_id <= t, t - stage_id < self._num_microbatches)
        return jnp.reshape(mask, (self._num_stages,) + (1,) * (per_stage_values.ndim - 1))

    def _to_microbatches(self, inputs):
        """Reshapes inputs from [batch_size, ...] to [M, microbatch_size, ...]."""

        def reshape_and_transpose(x: Tensor):
            # Keep batch partitioning along the 'microbatch_size' dim.
            x = jnp.reshape(x, [-1, self._num_microbatches] + list(x.shape[1:]))
            return jnp.transpose(x, [1, 0] + list(range(2, x.ndim)))

        return jax.tree_util.tree_map(reshape_and_transpose, inputs)

    # pylint: disable-next=no-self-use
    def _from_microbatches(self, inputs):
        def transpose_and_reshape(x: Tensor):
            x = jnp.transpose(x, [1, 0] + list(range(2, x.ndim)))
            return jnp.reshape(x, [-1] + list(x.shape[2:]))

        return jax.tree_util.tree_map(transpose_and_reshape, inputs)
