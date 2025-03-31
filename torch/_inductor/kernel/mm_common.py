# mypy: allow-untyped-defs
import logging
from typing import Any

import sympy

import torch
from torch._inductor.select_algorithm import realize_inputs, SymbolicGridFn
from torch._inductor.virtualized import V

from .. import config as inductor_config
from ..codegen.wrapper import PythonWrapperCodegen
from ..ir import ChoiceCaller, Layout
from ..utils import get_num_sms, TMA_DESCRIPTOR_SIZE, use_aten_gemm_kernels


log = logging.getLogger(__name__)


def should_fallback_to_aten(choices: list[ChoiceCaller]) -> bool:
    if len(choices) == 0 and not use_aten_gemm_kernels():
        if inductor_config.autotune_fallback_to_aten:
            log.warning(
                "No choices for GEMM, using ATen backend as fallback. "
                "This behavior is being deprecated. Please add include Aten in max_autotune_gemm_backends."
            )
            return True
        else:
            log.warning(
                "No choices for GEMM, chose not to fallback to ATen backend. "
                "To temporarily change this behavior, set autotune_fallback_to_aten to True "
                "via TORCHINDUCTOR_AUTOTUNE_FALLBACK_TO_ATEN=1, but this knob is being deprecated. "
                "The long term fix is to include Aten in max_autotune_gemm_backends."
            )
            return False
    return False


@SymbolicGridFn
def mm_grid(m, n, meta, *, cdiv):
    """
    The CUDA grid size for matmul triton templates.
    """
    return (cdiv(m, meta["BLOCK_M"]) * cdiv(n, meta["BLOCK_N"]), 1, 1)


@SymbolicGridFn
def persistent_mm_grid(M: int, N: int, meta: dict[str, Any], *, cdiv, min):
    """Defines the grid for persistent kernels."""
    return (
        min(meta["NUM_SMS"], cdiv(M, meta["BLOCK_M"]) * cdiv(N, meta["BLOCK_N"])),
        1,
        1,
    )


def acc_type(dtype):
    if dtype in (torch.float16, torch.bfloat16):
        return "tl.float32"
    return f"tl.{dtype}".replace("torch.", "")


def mm_options(config, sym_m, sym_n, sym_k, layout):
    """
    Common options to matmul triton templates.
    """
    even_k_symbolic = (
        # it isn't worth guarding on this
        sympy.gcd(sym_k, config.kwargs["BLOCK_K"]) == config.kwargs["BLOCK_K"]
    )
    allow_tf32 = torch.backends.cuda.matmul.allow_tf32 and (
        not inductor_config.force_same_precision
        or ((sym_m % 16) == 0 and (sym_n % 16) == 0 and (sym_k % 8) == 0)
    )
    return dict(
        GROUP_M=8,
        EVEN_K=even_k_symbolic,
        ALLOW_TF32=allow_tf32,
        ACC_TYPE=acc_type(layout.dtype),
        num_stages=config.num_stages,
        num_warps=config.num_warps,
        **config.kwargs,
    )


def persistent_mm_options(mat1, mat2):
    return dict(
        A_ROW_MAJOR=not mat1.layout.is_transposed(),
        B_ROW_MAJOR=not mat2.layout.is_transposed(),
        NUM_SMS=get_num_sms(),
        TMA_SIZE=TMA_DESCRIPTOR_SIZE,
    )


def mm_args(
    mat1,
    mat2,
    *others,
    layout=None,
    out_dtype=None,
    use_4x2_dim=False,
    mat2_transposed=False,
):
    """
    Common arg processing for mm,bmm,addmm,etc
    """
    mat1, mat2 = realize_inputs(mat1, mat2)
    *b1, m, k1 = mat1.get_size()
    if mat2_transposed:
        *b2, n, k2 = mat2.get_size()
    else:
        *b2, k2, n = mat2.get_size()
    b = [V.graph.sizevars.guard_equals(a, b) for a, b in zip(b1, b2)]
    if use_4x2_dim:
        k2 = k2 * 2
    k = V.graph.sizevars.guard_equals(k1, k2)
    if layout is None:
        from torch._inductor.ir import FixedLayout

        if out_dtype is None:
            out_dtype = mat1.get_dtype()

        layout = FixedLayout(
            mat1.get_device(),
            out_dtype,
            [*b, m, n],
        )
    else:
        assert out_dtype is None, "out_dtype is ignored if layout is specified."
    from ..lowering import expand

    others = [realize_inputs(expand(x, layout.size)) for x in others]

    return [m, n, k, layout, mat1, mat2, *others]


def mm_config_kwargs(device, exclude_condition):
    if device == "cpu":
        return {
            "scale": 0.5,
            "exclude": exclude_condition,
        }
    return {}


def addmm_epilogue(dtype, alpha, beta):
    def epilogue(acc, bias):
        if alpha != 1:
            acc = V.ops.mul(acc, V.ops.constant(alpha, dtype))
        if beta != 1:
            bias = V.ops.mul(bias, V.ops.constant(beta, dtype))
        return V.ops.add(acc, bias)

    return epilogue


def _is_static_problem(layout: Layout) -> tuple[bool, bool]:
    """
    Check if input tensors and output layout have static shapes and non-zero sizes.

    Args:
        layout: Output layout object with a 'size' attribute.

    Returns:
        Tuple[bool, bool]: (is_static, is_nonzero)
            is_static: True if all shapes are statically known
            is_nonzero: True if all dimensions are non-zero
    """
    static_shape = True
    static_size = PythonWrapperCodegen.statically_known_list_of_ints_or_none(
        layout.size
    )
    if static_size is None:
        nonzero = True
        for s in layout.size:
            sz = PythonWrapperCodegen.statically_known_int_or_none(s)
            if sz is not None and sz == 0:
                nonzero = False
                break
        return False, nonzero
    numel = 1
    for dim in static_size:
        numel *= dim
    nonzero = numel > 0
    return static_shape, nonzero
