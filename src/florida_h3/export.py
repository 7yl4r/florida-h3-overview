"""Year product export: res-7 overview, res-8 region shards, manifest.

Vendored from h3lab@26c042f (``src/h3lab/export_region.py``) and adapted to
the campaign/year product tree.  A year product is self-contained:

    product/2017/manifest.json
    product/2017/overview_res7.bin.gz
    product/2017/region_<parent>_res8.bin.gz

The manifest is derived from the files on disk, not remembered: every entry is
read back out of the bundle it describes, including its descriptor shape, byte
size and content hash.  A manifest that disagreed with its bundles would be
worse than no manifest at all.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import numpy as np

from . import bins as binmod
from .binary import read_descriptor, write_block
from .campaign import Campaign
from .constants import BAND_INFO, BANDS, INDICES
from .features import compute_indices
from .shard import compute_content_hash, group_cells_by_region

ROUND = {"band": 4, "index": 4, "other": 4}
SCHEMA_VERSION = 1


def _clean(arr: np.ndarray, nd: int) -> list:
    a = np.asarray(arr, dtype=np.float64)
    a = np.where(np.isfinite(a), np.round(a, nd), None)
    return a.tolist()


def _round4(values) -> np.ndarray:
    return np.round(np.asarray(values, dtype=np.float64).ravel(), 4)


def periods_for(campaign: Campaign, bins_list) -> list[dict]:
    out = []
    for b in bins_list:
        centre = campaign.bin_centre(int(b))
        out.append({
            "bin": int(b),
            "date": centre.isoformat(),
            "start": campaign.bin_start(int(b)).isoformat(),
            "end": campaign.bin_end(int(b)).isoformat(),
            "year": centre.year,
            "month": centre.month,
            "season": binmod.season_of(centre),
        })
    return out


def build_block_from_reduced(campaign: Campaign, grid, periods, composites_res,
                             change_data=None, cell_fields=None,
                             cellraw_fields=None, tier="overview",
                             year: int | None = None) -> dict:
    n_cells = len(grid)
    block = {
        "periods": periods,
        "nCells": n_cells,
        "ids": [str(c) for c in grid.cells],
        "lon": np.round(grid.centroids_lonlat[:, 0], 6).tolist(),
        "lat": np.round(grid.centroids_lonlat[:, 1], 6).tolist(),
        "areaKm2": np.round(grid.areas_km2, 6).tolist(),
        "band": {},
        "index": {},
        "observed": [],
        "validFraction": [],
        "nObs": [],
        "campaign": campaign.slug,
        "campaignHash": campaign.hash,
        "aoiHash": campaign.aoi_hash,
        "year": int(year) if year is not None else None,
        "resolution": int(grid.resolution),
        "tier": tier,
        "generatedBy": "florida_h3",
        "source": "Landsat Collection 2 Level-2 surface reflectance",
        "epoch": campaign.epoch.isoformat(),
        "binDays": campaign.bin_days,
    }

    for reduced in composites_res:
        refl = reduced["refl"]
        valid = reduced.get("observed", np.ones(n_cells, dtype=bool))
        feats = compute_indices(refl, campaign.index_specs, campaign.indices)
        for band in BANDS:
            block["band"].setdefault(band, []).append(
                _clean(refl[band], ROUND["band"])
            )
        for name in INDICES:
            block["index"].setdefault(name, []).append(
                _clean(feats[name], ROUND["index"])
            )
        block["observed"].append([bool(v) for v in valid])
        vf = reduced.get("valid_fraction",
                         np.ones(n_cells, dtype=np.float32))
        block["validFraction"].append(_clean(vf, 3))
        nobs = reduced.get("n_obs", np.ones(n_cells, dtype=np.int32))
        block["nObs"].append([int(v) for v in nobs])

    block["_bands"] = list(BANDS)
    block["_indices"] = list(INDICES)
    add_geometry(block, grid)

    if cell_fields:
        block["cell"] = {k: list(v) for k, v in cell_fields.items()}
    if cellraw_fields:
        block["cellraw"] = {k: list(v) for k, v in cellraw_fields.items()}
    if change_data is not None:
        block["change"] = _change_block(change_data, campaign)
    return block


def add_geometry(block: dict, grid) -> None:
    """Bake the hexagon outlines into a block, in (lng, lat) order."""
    import h3

    nverts, flat = [], []
    for cell in grid.cells:
        ring = h3.cell_to_boundary(str(cell))
        nverts.append(len(ring))
        for lat, lng in ring:
            flat.append(lng)
            flat.append(lat)
    block["_nVerts"] = nverts
    block["_geomXY"] = flat



def _change_block(change_data: dict, campaign: Campaign) -> dict:
    methods = sorted({str(m) for m in change_data["method"]})
    method_index = {m: i for i, m in enumerate(methods)}
    method_idx = np.array(
        [method_index[str(m)] for m in change_data["method"]], dtype=np.int64
    )

    break_date = change_data.get("break_date")
    if break_date is None or len(break_date) == 0:
        break_days = np.full(len(change_data["break_bin"]), -1, dtype=np.int64)
    else:
        break_days = np.array(
            [(date.fromisoformat(str(d)) - campaign.epoch).days if str(d) else -1
             for d in break_date], dtype=np.int64
        )

    return {
        "break_bin": [int(x) for x in change_data["break_bin"]],
        "break_date": break_days,
        "break_magnitude": _round4(change_data["break_magnitude"]),
        "break_score": _round4(change_data["break_score"]),
        "n_obs_used": [int(x) for x in change_data["n_obs_used"]],
        "method": method_idx,
        "methods": methods,
    }


def _hash_of(path: str | Path) -> str:
    try:
        with open(path, "rb") as fh:
            return compute_content_hash(fh.read())
    except OSError:
        return ""


def _composites_for(composites_by_bin, periods, res, indices=None):
    out = []
    for period in periods:
        b = period["bin"]
        for (bin_num, r, reduced) in composites_by_bin:
            if bin_num == b and r == res:
                out.append((period, _subset_reduced(reduced, indices)
                            if indices is not None else reduced))
                break
    return out


def _subset_reduced(reduced, indices):
    idx = np.asarray(indices)
    out = {
        "refl": {b: reduced["refl"][b][idx] for b in reduced["refl"]},
        "observed": reduced["observed"][idx],
        "valid_fraction": reduced.get(
            "valid_fraction", np.ones(len(idx), dtype=np.float32)
        )[idx],
        "n_obs": reduced.get("n_obs", np.ones(len(idx), dtype=np.int32))[idx],
    }
    return out


def _subset_change(change: dict, indices) -> dict:
    idx = np.asarray(indices)
    out = {}
    for key in ("break_bin", "break_date", "break_magnitude", "break_score",
                "n_obs_used", "method"):
        if key in change:
            out[key] = np.asarray(change[key])[idx]
    return out


def _subset_cell_fields(fields: dict | None, indices):
    if not fields:
        return None
    idx = np.asarray(indices)
    return {k: np.asarray(v)[idx] for k, v in fields.items()}


def export_year(campaign: Campaign, grids, composites_by_bin, bins_list,
                year: int, out_dir: str | Path, gz: bool = True,
                change_by_res: dict | None = None,
                cell_fields_by_res: dict | None = None,
                cellraw_fields_by_res: dict | None = None):
    """Write a year's overview, region shards and manifest."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    periods = periods_for(campaign, bins_list)

    overview = None
    if campaign.overview_res in grids:
        overview = export_overview(
            campaign, grids[campaign.overview_res], composites_by_bin,
            periods, out_dir, gz, year,
            change=(change_by_res or {}).get(campaign.overview_res),
            cell_fields=(cell_fields_by_res or {}).get(campaign.overview_res),
            cellraw_fields=(cellraw_fields_by_res or {}).get(campaign.overview_res),
        )

    regions = []
    if campaign.detail_res is not None and campaign.detail_res in grids:
        regions = export_region_shards(
            campaign, grids[campaign.detail_res], composites_by_bin,
            periods, out_dir, gz, year,
            change=(change_by_res or {}).get(campaign.detail_res),
            cell_fields=(cell_fields_by_res or {}).get(campaign.detail_res),
            cellraw_fields=(cellraw_fields_by_res or {}).get(campaign.detail_res),
        )

    manifest = write_year_manifest(
        campaign, grids, bins_list, year, out_dir, gz,
        overview=overview, regions=regions,
    )
    return {"overview": overview, "regions": regions, "manifest": manifest}


def export_overview(campaign: Campaign, grid, composites_by_bin, periods,
                    out_dir: Path, gz: bool, year: int, change=None,
                    cell_fields=None, cellraw_fields=None):
    pairs = _composites_for(composites_by_bin, periods, campaign.overview_res)
    if not pairs:
        return None
    used_periods = [p for p, _ in pairs]
    comps = [r for _, r in pairs]
    block = build_block_from_reduced(
        campaign, grid, used_periods, comps, change_data=change,
        cell_fields=cell_fields, cellraw_fields=cellraw_fields,
        tier="overview", year=year,
    )
    path = out_dir / f"overview_res{campaign.overview_res}.bin"
    info = write_block(block, str(path), gz=gz)
    return {
        "file": os.path.basename(info["path"]),
        "res": campaign.overview_res,
        "nCells": len(grid),
        "nPeriods": len(comps),
        "bytes": int(os.path.getsize(info["path"])),
        "hash": _hash_of(info["path"]),
    }


def export_region_shards(campaign: Campaign, grid, composites_by_bin, periods,
                         out_dir: Path, gz: bool, year: int, change=None,
                         cell_fields=None, cellraw_fields=None):
    groups = group_cells_by_region(grid, campaign.region_res)
    shards = []
    for parent in sorted(groups):
        indices = groups[parent]
        sub_grid = grid.subset(np.array(indices))
        pairs = _composites_for(composites_by_bin, periods,
                                campaign.detail_res, indices)
        if not pairs:
            continue
        used_periods = [p for p, _ in pairs]
        comps = [r for _, r in pairs]
        sub_change = _subset_change(change, indices) if change is not None else None
        block = build_block_from_reduced(
            campaign, sub_grid, used_periods, comps, change_data=sub_change,
            cell_fields=_subset_cell_fields(cell_fields, indices),
            cellraw_fields=_subset_cell_fields(cellraw_fields, indices),
            tier="detail", year=year,
        )
        path = out_dir / f"region_{parent}_res{campaign.detail_res}.bin"
        info = write_block(block, str(path), gz=gz)
        lon = sub_grid.centroids_lonlat[:, 0]
        lat = sub_grid.centroids_lonlat[:, 1]
        shards.append({
            "region": parent,
            "res": campaign.detail_res,
            "nCells": len(sub_grid),
            "nPeriods": len(comps),
            "file": os.path.basename(info["path"]),
            "bytes": int(os.path.getsize(info["path"])),
            "hash": _hash_of(info["path"]),
            "lon": round(float(lon.mean()), 4),
            "lat": round(float(lat.mean()), 4),
            "bbox": [round(float(lon.min()), 4), round(float(lat.min()), 4),
                     round(float(lon.max()), 4), round(float(lat.max()), 4)],
        })
    return shards


def write_year_manifest(campaign: Campaign, grids, bins_list, year: int,
                        out_dir: Path, gz: bool, overview=None,
                        regions=None) -> dict:
    from .binary import read_descriptor

    out_dir = Path(out_dir)
    periods = periods_for(campaign, bins_list)
    suffix = ".bin.gz" if gz else ".bin"

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "year",
        "name": campaign.name,
        "slug": campaign.slug,
        "year": int(year),
        "campaign": campaign.to_dict(),
        "campaign_hash": campaign.hash,
        "aoi_hash": campaign.aoi_hash,
        "bbox": campaign.bbox,
        "crs": campaign.crs,
        "pixel_size": campaign.pixel_size,
        "detail_pixel_size": campaign.detail_pixel_size,
        "epoch": campaign.epoch.isoformat(),
        "bin_days": campaign.bin_days,
        "bands": list(campaign.bands),
        "band_info": campaign.band_info,
        "indices": list(campaign.indices),
        "index_specs": campaign.index_specs,
        "periods": periods,
        "region_res": campaign.region_res,
        "overview": None,
        "regions": [],
        "events": campaign.events,
        "source": "Landsat Collection 2 Level-2 surface reflectance (NASA/USGS)",
        "via": "Microsoft Planetary Computer STAC",
        "note": (
            "Surface reflectance and spectral indices only. No land-cover "
            "classification is applied anywhere in this pipeline."
        ),
    }
    if campaign.overview_res in grids:
        manifest["aoiAreaKm2"] = round(campaign.area_km2, 2)

    if overview is None:
        path = out_dir / f"overview_res{campaign.overview_res}{suffix}"
        if path.exists():
            desc = read_descriptor(str(path))
            overview = {
                "file": path.name,
                "res": campaign.overview_res,
                "nCells": int(desc["nCells"]),
                "nPeriods": int(desc["nPeriods"]),
                "bytes": int(path.stat().st_size),
                "hash": _hash_of(path),
            }
    if overview is not None:
        manifest["overview"] = overview

    if regions is None:
        regions = []
        if campaign.detail_res is not None and campaign.detail_res in grids:
            groups = group_cells_by_region(grids[campaign.detail_res],
                                           campaign.region_res)
            for parent in sorted(groups):
                path = out_dir / f"region_{parent}_res{campaign.detail_res}{suffix}"
                if not path.exists():
                    continue
                desc = read_descriptor(str(path))
                sub = grids[campaign.detail_res].subset(np.array(groups[parent]))
                lon = sub.centroids_lonlat[:, 0]
                lat = sub.centroids_lonlat[:, 1]
                regions.append({
                    "region": parent,
                    "res": campaign.detail_res,
                    "nCells": int(desc["nCells"]),
                    "nPeriods": int(desc["nPeriods"]),
                    "file": path.name,
                    "bytes": int(path.stat().st_size),
                    "hash": _hash_of(path),
                    "lon": round(float(lon.mean()), 4),
                    "lat": round(float(lat.mean()), 4),
                    "bbox": [round(float(lon.min()), 4), round(float(lat.min()), 4),
                             round(float(lon.max()), 4), round(float(lat.max()), 4)],
                })
    manifest["regions"] = regions

    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, sort_keys=True, separators=(",", ":"))
    return manifest
