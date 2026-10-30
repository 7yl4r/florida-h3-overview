"""Region sharding by H3 parent.

Vendored from h3lab@26c042f (``src/h3lab/shard.py``).  Cells are grouped by
their H3 parent at the campaign's region resolution (res 4 by default).  The
parent is computed with ``cell_to_parent``, not by string slicing, so the
grouping stays correct if a campaign chooses a different region resolution.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

import h3
import numpy as np


def get_region_parent(cell_id: str, region_res: int = 4) -> str:
    return h3.cell_to_parent(cell_id, region_res)


def group_cells_by_region(grid, region_res: int = 4) -> dict:
    groups = defaultdict(list)
    for idx, cell in enumerate(grid.cells):
        groups[get_region_parent(str(cell), region_res)].append(idx)
    return dict(groups)


def compute_content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]
