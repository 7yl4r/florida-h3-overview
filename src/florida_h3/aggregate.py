"""Bin a mosaic into H3 cells.

Vendored from h3lab@26c042f (``src/h3lab/aggregate.py``).  The mosaic is
already a per-pixel temporal median, so this is a spatial mean over the pixels
that belong to each cell.  Reflectance scaling is applied here, at sampling
time, exactly where the pilot applies it.
"""

from __future__ import annotations

import numpy as np

from .constants import BANDS, SR_OFFSET, SR_SCALE


def to_reflectance(dn: np.ndarray) -> np.ndarray:
    return dn.astype(np.float32) * np.float32(SR_SCALE) + np.float32(SR_OFFSET)


def aggregate_mosaic(mosaic_bands: dict, labels: np.ndarray, n_cells: int,
                     nobs: np.ndarray | None = None,
                     bands: tuple[str, ...] = BANDS,
                     scale: bool = True) -> dict:
    """Bin mosaic band arrays into hex cells.

    A pixel counts as observed only if it is valid in *every* band, so a
    partially observed pixel cannot contribute a half-empty spectrum to a cell
    mean.  ``valid_fraction`` is the observed fraction of the cell's pixels.
    """
    valid = None
    for band in bands:
        if band in mosaic_bands:
            arr = mosaic_bands[band]
            valid = np.isfinite(arr) if valid is None else valid & np.isfinite(arr)
    if valid is None:
        valid = np.ones(labels.shape, dtype=bool)

    valid_flat = valid.ravel()
    lab = labels.ravel()
    lab_valid = lab[valid_flat]
    counts = np.bincount(lab_valid, minlength=n_cells + 1)[1:]
    n_total = np.bincount(lab, minlength=n_cells + 1)[1:]

    bands_out = {}
    for band in bands:
        if band not in mosaic_bands:
            bands_out[band] = np.full(n_cells, np.nan, dtype=np.float32)
            continue
        vals = mosaic_bands[band].ravel()[valid_flat]
        if scale:
            vals = to_reflectance(vals)
        sums = np.bincount(lab_valid, weights=vals.astype(np.float64),
                           minlength=n_cells + 1)[1:]
        with np.errstate(invalid="ignore", divide="ignore"):
            bands_out[band] = np.where(
                counts > 0, sums / np.maximum(counts, 1), np.nan
            ).astype(np.float32)

    if nobs is None:
        n_obs = np.ones(n_cells, dtype=np.int32)
    else:
        n_flat = np.nan_to_num(nobs.ravel()[valid_flat], nan=0.0)
        n_sum = np.bincount(lab_valid, weights=n_flat.astype(np.float64),
                            minlength=n_cells + 1)[1:]
        with np.errstate(invalid="ignore", divide="ignore"):
            n_obs = np.where(counts > 0, n_sum / np.maximum(counts, 1), 0)
        n_obs = np.rint(n_obs).astype(np.int32)

    return {
        "bands": bands_out,
        "n_valid": counts.astype(np.int32),
        "n_total": n_total.astype(np.int32),
        "n_obs": n_obs,
    }
