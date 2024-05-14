from typing import Tuple, Union

import numpy as np
import torch

from .flow_patch import FlowPatch

NUMPY_TORCH = Union[np.ndarray, torch.Tensor]
FLOAT_TORCH = Union[float, torch.Tensor]


def is_torch(args):
    return isinstance(args, torch.Tensor)


def is_numpy(args):
    return isinstance(args, np.ndarray)


def nt_max(array: NUMPY_TORCH, dim: int) -> NUMPY_TORCH:
    """max function compatible for numpy ndarray and torch tensor.

    Args:
        array (NUMPY_TORCH):

    Returns:
        NUMPY_TORCH: _description_
    """
    if is_numpy(array):
        return array.max(axis=dim)
    elif is_torch(array):
        return torch.max(array, dim).values


def nt_min(array: NUMPY_TORCH, dim: int) -> NUMPY_TORCH:
    """Min function compatible for numpy ndarray and torch tensor.

    Args:
        array (NUMPY_TORCH):

    Returns:
        NUMPY_TORCH: _description_
    """
    if is_numpy(array):
        return array.min(axis=dim)
    elif is_torch(array):
        return torch.min(array, dim).values
