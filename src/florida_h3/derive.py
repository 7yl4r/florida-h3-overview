"""Whole-record derived layers.

Observations are partitioned by year.  Change points, trend, stability and a
per-cell summary cannot be computed correctly one year at a time, so they are
built here from the accumulated ``h3/<year>/res<R>/bin<B>.npz`` cache after the
years exist.  The mosaics are not touched: everything in this module is
arithmetic over the small per-cell cache.

The output is a pair of H3LB bundles per resolution: one overview bundle for
the campaign's overview resolution and one shard per H3 region at the detail
resolution.  They use the same container as the year products, with a single
pseudo-period and per-cell fields:

    cell.trend_slope        NDVI units per year
    cell.trend_intercept    NDVI at the campaign epoch
    cell.ndvi_mean          record mean NDVI
    cell.ndvi_std           record standard deviation (stability proxy)
    cell.observed_fraction  mean fraction of bins with an observation
    cellraw.n_obs_total     summed scene count per cell
    cellraw.n_bins          bins contributing to the summary
    change.*                the harmonic F-scan break fields

Derived layers are a screening aid, not event characterisation.  The manifest
records the method string and the bin list so a value can be reproduced.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from . import bins as binmod
from .analysis import SPECS as ANALYSIS_SPECS, analyse
from .binary import read_descriptor, write_block
from .campaign import Campaign
from .composite import load_composite
from .export import _change_block, add_geometry, periods_for
from .features import _ratio, compute_indices
from .grid import build_grids
from .shard import compute_content_hash, group_cells_by_region

SCHEMA_VERSION = 1


def _hash_of(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            return compute_content_hash(fh.read())
    except OSError:
        return ""


def _year_for_bin(campaign: Campaign, res: int, b: int,
                  years: list[int]) -> tuple[int, Path] | None:
    for year in years:
        path = campaign.h3_path(year, res, b)
        if path.exists():
            return year, path
    return None


def cached_bins(campaign: Campaign, res: int,
                years: list[int] | None = None) -> list[tuple[int, int, Path]]:
    """Every cached ``(year, bin, path)`` for a resolution, one per bin.

    A bin that straddles a year boundary can be cached in both years.  The
    first year wins so the series has exactly one value per bin.
    """
    years = list(years or campaign.build_years)
    seen: dict[int, tuple[int, int, Path]] = {}
    for year in years:
        for b in campaign.bins_for_year(year):
            if b in seen:
                continue
            path = campaign.h3_path(year, res, b)
            if path.exists():
                seen[b] = (year, b, path)
    return [seen[b] for b in sorted(seen)]


def load_series(campaign: Campaign, grid, years: list[int] | None = None) -> dict:
    """Load the per-bin cache for one grid into a dense record.

    Returns a dict with ``bins`` (sorted bin numbers), ``ndvi`` and every
    campaign index shaped ``(n_bins, n_cells)``, the band arrays, observed
    flags, valid fraction, scene counts and the period descriptors the
    trend/season fit needs.  Bins with no cache entry are simply absent; a
    missing bin is not the same as a bin with no valid pixels, and neither is
    invented here.
    """
    entries = cached_bins(campaign, grid.resolution, years)
    if not entries:
        raise RuntimeError(
            f"no h3 cache for res {grid.resolution} under {campaign.cache_dir}"
        )

    n_cells = len(grid)
    used_bins: list[int] = []
    refl: dict[str, list[np.ndarray]] = {b: [] for b in campaign.bands}
    observed: list[np.ndarray] = []
    valid_fraction: list[np.ndarray] = []
    n_obs: list[np.ndarray] = []

    for _year, b, path in entries:
        meta, bands, sup = load_composite(str(path))
        if int(meta.get("res", grid.resolution)) != grid.resolution:
            raise RuntimeError(f"{path}: cached resolution does not match grid")
        length = len(bands[campaign.bands[0]])
        if length != n_cells:
            raise RuntimeError(
                f"{path}: {length} cells but the res-{grid.resolution} grid "
                f"has {n_cells}; the AOI or H3 version changed"
            )
        used_bins.append(int(b))
        for band in campaign.bands:
            refl[band].append(np.asarray(bands[band], dtype=np.float32))
        observed.append(np.asarray(sup["observed"], dtype=bool))
        valid_fraction.append(np.asarray(sup["valid_fraction"], dtype=np.float32))
        n_obs.append(np.asarray(sup["n_obs"], dtype=np.int32))

    ndvi = _ratio(np.stack(refl["nir"]), np.stack(refl["red"]))
    refl_by_band = {k: np.stack(v) for k, v in refl.items()}
    indices = compute_indices(
        refl_by_band, campaign.index_specs, campaign.indices
    )
    return {
        "bins": used_bins,
        "refl": refl_by_band,
        "indices": {k: np.asarray(v, dtype=np.float64)
                    for k, v in indices.items()},
        "ndvi": ndvi.astype(np.float32),
        "observed": np.stack(observed),
        "valid_fraction": np.stack(valid_fraction),
        "n_obs": np.stack(n_obs),
        "periods": periods_for(campaign, used_bins),
    }


def compute_derived(campaign: Campaign, grid,
                    years: list[int] | None = None) -> dict:
    """Compute the whole-record summary for one resolution."""
    from .change import detect_breaks, linear_trend

    series = load_series(campaign, grid, years)
    bins_list = series["bins"]
    ndvi = series["ndvi"]

    slope, intercept = linear_trend(
        ndvi, bins_list, campaign.epoch, campaign.bin_days
    )
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(ndvi, axis=0).astype(np.float32)
        std = np.nanstd(ndvi, axis=0).astype(np.float32)

    change = detect_breaks(
        ndvi, bins_list, campaign.epoch, campaign.bin_days
    )
    analysis_cell, analysis_raw = analyse(
        series["periods"], series["indices"], series["observed"]
    )

    n_obs_total = np.nansum(series["n_obs"], axis=0).astype(np.int64)
    n_bins = np.isfinite(ndvi).sum(axis=0).astype(np.int64)
    observed_fraction = np.mean(series["observed"], axis=0).astype(np.float32)

    return {
        "bins": bins_list,
        "trend_slope": slope,
        "trend_intercept": intercept,
        "ndvi_mean": mean,
        "ndvi_std": std,
        "observed_fraction": observed_fraction,
        "n_obs_total": n_obs_total,
        "n_bins": n_bins,
        "change": change,
        "analysis_cell": analysis_cell,
        "analysis_raw": analysis_raw,
    }


def _derived_block(campaign: Campaign, grid, derived: dict) -> dict:
    """Build one H3LB block carrying per-cell derived fields."""
    n_cells = len(grid)
    periods = [{
        "bin": -1,
        "date": "",
        "start": "",
        "end": "",
        "year": None,
        "month": None,
        "season": "record",
    }]
    block = {
        "periods": periods,
        "nCells": n_cells,
        "ids": [str(c) for c in grid.cells],
        "lon": np.round(grid.centroids_lonlat[:, 0], 6).tolist(),
        "lat": np.round(grid.centroids_lonlat[:, 1], 6).tolist(),
        "areaKm2": np.round(grid.areas_km2, 6).tolist(),
        "band": {},
        "index": {},
        "observed": [[True] * n_cells],
        "validFraction": [[1.0] * n_cells],
        "nObs": [[int(v) for v in derived["n_obs_total"]]],
        "campaign": campaign.slug,
        "campaignHash": campaign.hash,
        "aoiHash": campaign.aoi_hash,
        "year": None,
        "resolution": int(grid.resolution),
        "tier": "derived",
        "generatedBy": "florida_h3",
        "source": "Landsat Collection 2 Level-2 surface reflectance",
        "epoch": campaign.epoch.isoformat(),
        "binDays": campaign.bin_days,
        "cell": {
            "trend_slope": np.asarray(derived["trend_slope"], dtype=np.float64),
            "trend_intercept": np.asarray(derived["trend_intercept"], dtype=np.float64),
            "ndvi_mean": np.asarray(derived["ndvi_mean"], dtype=np.float64),
            "ndvi_std": np.asarray(derived["ndvi_std"], dtype=np.float64),
            "observed_fraction": np.asarray(derived["observed_fraction"], dtype=np.float64),
            **{k: np.asarray(v, dtype=np.float64)
               for k, v in derived["analysis_cell"].items()},
        },
        "cellraw": {
            "n_obs_total": np.asarray(derived["n_obs_total"], dtype=np.int64),
            "n_bins": np.asarray(derived["n_bins"], dtype=np.int64),
            **{k: np.asarray(v, dtype=np.int64)
               for k, v in derived["analysis_raw"].items()},
        },
    }
    add_geometry(block, grid)
    block["change"] = _change_block(derived["change"], campaign)
    return block


def _subset_derived(derived: dict, indices: np.ndarray) -> dict:
    out = dict(derived)
    for key in ("trend_slope", "trend_intercept", "ndvi_mean", "ndvi_std",
                "observed_fraction", "n_obs_total", "n_bins"):
        out[key] = np.asarray(derived[key])[indices]
    change = dict(derived["change"])
    for key in ("break_bin", "break_date", "break_magnitude", "break_score",
                "n_obs_used", "method"):
        change[key] = np.asarray(change[key])[indices]
    out["change"] = change
    for key in ("analysis_cell", "analysis_raw"):
        out[key] = {
            name: np.asarray(values)[indices]
            for name, values in derived[key].items()
        }
    return out


def _bundle_entry(path: Path, res: int, n_cells: int, n_bins: int,
                  region: str | None = None) -> dict:
    desc = read_descriptor(str(path))
    entry = {
        "file": path.name,
        "res": int(res),
        "nCells": int(n_cells),
        "nPeriods": int(desc["nPeriods"]),
        "nBins": int(n_bins),
        "bytes": int(path.stat().st_size),
        "hash": _hash_of(path),
    }
    if region is not None:
        entry["region"] = region
    return entry


def write_derived(campaign: Campaign, resolutions: tuple[int, ...] | None = None,
                  years: list[int] | None = None, gz: bool = True) -> dict:
    """Write the derived manifest and its bundles.  Returns the manifest."""
    resolutions = tuple(resolutions or campaign.resolutions)
    out_dir = campaign.derived_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    # write_block appends ".gz" itself when gz=True, so the path stem ends in
    # ".bin" and the on-disk file becomes "derived_res7.bin.gz".
    suffix = ".bin"

    grids = build_grids(campaign, resolutions)
    files: list[dict] = []
    overview = None
    regions: list[dict] = []
    methods: set[str] = set()

    for res in sorted(resolutions):
        grid = grids[res]
        derived = compute_derived(campaign, grid, years)
        methods.update(str(m) for m in derived["change"]["method"])

        if res == campaign.overview_res:
            block = _derived_block(campaign, grid, derived)
            path = out_dir / f"derived_res{res}{suffix}"
            info = write_block(block, str(path), gz=gz)
            overview = _bundle_entry(Path(info["path"]), res, len(grid),
                                     len(derived["bins"]))
            files.append(overview)
            continue

        groups = group_cells_by_region(grid, campaign.region_res)
        for parent in sorted(groups):
            indices = np.array(groups[parent])
            sub_grid = grid.subset(indices)
            sub = _subset_derived(derived, indices)
            block = _derived_block(campaign, sub_grid, sub)
            path = out_dir / f"derived_region_{parent}_res{res}{suffix}"
            info = write_block(block, str(path), gz=gz)
            entry = _bundle_entry(Path(info["path"]), res, len(sub_grid),
                                  len(sub["bins"]), region=parent)
            lon = sub_grid.centroids_lonlat[:, 0]
            lat = sub_grid.centroids_lonlat[:, 1]
            entry.update({
                "lon": round(float(lon.mean()), 4),
                "lat": round(float(lat.mean()), 4),
                "bbox": [round(float(lon.min()), 4), round(float(lat.min()), 4),
                         round(float(lon.max()), 4), round(float(lat.max()), 4)],
            })
            regions.append(entry)
            files.append(entry)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "derived",
        "name": campaign.name,
        "slug": campaign.slug,
        "campaign": campaign.to_dict(),
        "campaign_hash": campaign.hash,
        "aoi_hash": campaign.aoi_hash,
        "bbox": campaign.bbox,
        "epoch": campaign.epoch.isoformat(),
        "bin_days": campaign.bin_days,
        "bands": list(campaign.bands),
        "indices": list(campaign.indices),
        "years": sorted(int(y) for y in (years or campaign.build_years)),
        "bins": sorted({int(b) for y in (years or campaign.build_years)
                        for b in campaign.bins_for_year(y)}),
        "analysis": ANALYSIS_SPECS,
        "methods": sorted(methods),
        "overview": overview,
        "regions": regions,
        "files": files,
        "note": (
            "Whole-record screening layers: harmonic F-scan breaks, linear "
            "NDVI trend, and record stability. Not event characterisation."
        ),
    }
    manifest_path = out_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, sort_keys=True, separators=(",", ":"))
    return manifest
