# Copyright 2024 Advanced Micro Devices, Inc.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

# Given an input dispatch, this code modifies the hyperparameters
# in the code and runs it.

from iree.compiler import ir  # type: ignore
from iree.compiler.dialects import iree_codegen  # type: ignore

from .common import *
from .dispatch_constraints import *
from .dispatch_parser import *

ROOT_OP_ATTR_NAME = "root_op"


def get_placeholder_spec(context: ir.Context) -> ir.Module:
    spec_text = f"""
        module attributes {{ transform.with_named_sequence }} {{
            transform.named_sequence
            @__kernel_config(%variant_op: !transform.any_op {{transform.readonly}}) -> !transform.any_op
                attributes {{ iree_codegen.tuning_spec_entrypoint }} {{
                transform.yield %variant_op : !transform.any_op
            }}
        }}
        """
    return ir.Module.parse(spec_text, context)


# TODO(Max191): Use python bindings to build the transform dialect spec module
# instead of using string formatting.
def build_td_spec(
    context: ir.Context,
    op: ir.Operation,
    config_list: list[common.TuningConfiguration],
    func_name: str,
) -> ir.Module:
    bbargs = []
    # The `root_op` attribute will prevent matching of ops without the attr in
    # the resulting TD spec matcher if it is not removed, so we remove it here.
    # After removing, we must add it back, since the op is connected to the
    # input module, which gets used for all candidates.
    # TODO(Max191): Find a cleaner way to do this without removing and adding
    # back the attribute.
    has_root_attr = ROOT_OP_ATTR_NAME in op.opview.attributes
    if has_root_attr:
        assert isinstance(
            op.opview.attributes[ROOT_OP_ATTR_NAME], ir.UnitAttr
        ), f"expected '{ROOT_OP_ATTR_NAME}' attr to be a unit attr"
    if has_root_attr:
        del op.opview.attributes[ROOT_OP_ATTR_NAME]
    # Get the root op string for formatting the final spec.
    root_operation = str(op)
    if has_root_attr:
        op.opview.attributes[ROOT_OP_ATTR_NAME] = ir.UnitAttr.get(op.context)

    # Get the names ssa names of operands to make sure they match in the
    # template after string formatting.
    captured_values: set[ir.Value] = set()
    for operand in op.operands:
        if operand in captured_values:
            # TODO(Max191): Remove this warning when the transform for the
            # `cast_compatible_dag_from_root` op fixes a bug in the matching
            # logic that causes failure to match when the same operand is
            # repeated. For now, still avoid adding duplicate SSA values to
            # prevent parsing failure.
            logging.warning(
                f"Root op has repeated operand. This can cause failure to match in the resulting TD spec at compile time."
            )
            continue
        ssa_name = operand.get_name()
        operand_type = operand.type
        bbargs.append(f"{ssa_name}: {operand_type}")
        captured_values.add(operand)
    bbargs_str = ", ".join(bbargs)

    config_lines = []
    yield_vars = []
    for i, config in enumerate(config_list):
        config_var = f"%{config.name}_{i}"
        config_lines.append(
            f"{config_var} = transform.param.constant {config.configuration} -> !transform.any_param"
        )
        yield_vars.append(config_var)
    config_block = "\n                ".join(config_lines)
    yield_list = ", ".join(["%cont"] + yield_vars)
    yield_types = ", ".join(
        ["!transform.any_op"] + ["!transform.any_param"] * len(yield_vars)
    )

    annotation_args = ", ".join(
        f"%cfg_{i}: !transform.any_param {{transform.readonly}}"
        for i in range(len(config_list))
    )
    annotation_lines = "\n".join(
        f'                transform.annotate %op "{config.name}" = %cfg_{i} : !transform.any_op, !transform.any_param'
        for i, config in enumerate(config_list)
    )

    spec_text = f"""\
        module attributes {{ transform.with_named_sequence, iree_codegen.tuning_spec_with_default_entrypoint }} {{
        // Annotation Transform
        transform.named_sequence @apply_op_config(%op: !transform.any_op {{transform.readonly}}, {annotation_args}) {{
        {annotation_lines}
            transform.yield
        }}

        // Custom Op Matcher
        transform.named_sequence @{func_name}(%cont: !transform.any_op {{transform.readonly}})
            -> ({yield_types}) {{
            %ins, %outs = transform.iree.match.cast_compatible_dag_from_root %cont {{
              ^bb0({bbargs_str}):
              {root_operation}
            }} : (!transform.any_op) -> (!transform.any_value, !transform.any_value)
            {config_block}
            transform.yield {yield_list} : {yield_types}
        }}

        // Entry Point
        transform.named_sequence
        @__kernel_config(%variant_op: !transform.any_op {{transform.consumed}}) -> !transform.any_op
            attributes {{ iree_codegen.tuning_spec_entrypoint }} {{
            %res = transform.foreach_match in %variant_op
                @{func_name} -> @apply_op_config
            : (!transform.any_op) -> !transform.any_op
            transform.yield %res : !transform.any_op
        }}
    }}"""
    return ir.Module.parse(spec_text, context)
