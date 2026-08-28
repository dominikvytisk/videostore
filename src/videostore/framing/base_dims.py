from __future__ import annotations

from videostore.modulation.base import usable_dims


def block_grid_shape(width: int, height: int, block_size: int) -> tuple[int, int]:
    """Returns (rows, cols) of the block grid a ModulationScheme with this
    block_size produces over the usable (cropped) region of width x height."""
    uw, uh = usable_dims(width, height, block_size)
    return uh // block_size, uw // block_size
