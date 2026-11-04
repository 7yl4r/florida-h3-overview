#!/usr/bin/env python3
"""Coarsen the imported South Florida demo to lower H3 resolutions.

The demo imported by ``tools/import_demo.py`` is res-7 overview plus res-8
region shards.  That detail tier is far too heavy for a browser: every visible
shard decodes to ~20 MB, and a statewide view asks for all 72 of them at once.

This tool rebuilds the demo product from its existing res-7 overview bundle:

    overview_res6.bin.gz              res-6 statewide overview
    region_<res4 parent>_res7.bin.gz  res-7 regional shards

Values are aggregated from child cells to their parent (mean for bands,
indices and validFraction; logical OR for observed; sum for nObs and area).
The res-7 shards are subsets of the same res-7 cells grouped by res-4 parent,
so they inherit the overview's values exactly.

Example::

    PYTHONPATH=src python tools/coarsen_demo.py
"""

from __future__ import annotations

import argparse
import json
import re
import warnings
from pathlib import Path

import h3
import numpy as np

from florida_h3.binary import MISSING, read_block, write_block
from florida_h3.analysis import SPECS as ANALYSIS_SPECS, analyse
from florida_h3.campaign import Campaign
from florida_h3.catalog import write_catalog
from florida_h3.change import detect_breaks
from florida_h3.export import _change_block
from florida_h3.shard import compute_content_hash, get_region_parent


def _field(desc: dict, name: str) -> dict:
    return next(f for f in desc["fields"] if f["name"] == name)


def _dequant(desc: dict, arrays: dict, name: str) -> np.ndarray:
    """Turn a quantised u16 field back into floats, restoring NaN for missing."""
    raw = arrays[name]
    info = _field(desc, name)
    if info["dtype"] != "u16":
        return raw.astype(np.float32)
    values = (
        raw.astype(np.float32) * np.float32(info.get("scale", 0.0))
        + np.float32(info.get("min", 0.0))
    )
    values[raw == MISSING] = np.nan
    return values


def _rows(values: np.ndarray, ndigits: int) -> list[list]:
    out = []
    for row in values:
        out.append([
            None if not np.isfinite(v) else round(float(v), ndigits)
            for v in row
        ])
    return out


def _boundary_geometry(cells: list[str]) -> tuple[list[int], list[float]]:
    nverts: list[int] = []
    flat: list[float] = []
    for cell in cells:
        ring = h3.cell_to_boundary(cell)
        nverts.append(len(ring))
        for lat, lng in ring:
            flat.append(float(lng))
            flat.append(float(lat))
    return nverts, flat


def _analysis_fields(fields, indices, periods, observed):
    """Trend/season/residual assessment for one output block."""
    index_values = {
        name: fields[f"index.{name}"]
        for name in indices
        if f"index.{name}" in fields
    }
    return analyse(periods, index_values, observed)


def _change_for(fields, periods, campaign: Campaign):
    """Recompute the break layer from the block's own NDVI series.

    The imported res-7 overview carried the change fields only on its res-8
    region shards, so the coarsened product lost them.  Computing the layer
    here keeps the demo's change mode working at both resolutions.
    """
    ndvi = fields.get("index.ndvi")
    if ndvi is None:
        return None
    bins = [int(p["bin"]) for p in periods]
    change = detect_breaks(
        ndvi, bins, campaign.epoch, campaign.bin_days
    )
    return _change_block(change, campaign)


def _make_block(ids, lon, lat, area, fields, observed, valid_fraction,
                n_obs, periods, meta: dict, res: int, tier: str,
                analysis=None, change=None) -> dict:
    block = {
        "periods": periods,
        "nCells": len(ids),
        "ids": list(ids),
        "lon": [round(float(v), 6) for v in lon],
        "lat": [round(float(v), 6) for v in lat],
        "areaKm2": [round(float(v), 6) for v in area],
        "band": {},
        "index": {},
        "observed": observed.astype(bool).tolist(),
        "validFraction": _rows(valid_fraction, 3),
        "nObs": n_obs.astype(int).tolist(),
        "resolution": int(res),
        "tier": tier,
        "year": meta.get("year"),
        "epoch": meta.get("epoch"),
        "binDays": meta.get("binDays"),
        "source": meta.get("source"),
        "generatedBy": "florida_h3.coarsen_demo",
    }
    for name, values in fields.items():
        group, short = name.split(".", 1)
        block[group][short] = _rows(values, 4)
    if analysis:
        block["cell"], block["cellraw"] = analysis
    if change:
        block["change"] = change
    nverts, flat = _boundary_geometry(list(ids))
    block["_nVerts"] = nverts
    block["_geomXY"] = flat
    block["_bands"] = list(meta["bands"])
    block["_indices"] = list(meta["indices"])
    return block


def _entry(path: Path, res: int, n_cells: int, n_periods: int,
           lon: np.ndarray, lat: np.ndarray, bbox: bool = False,
           region: str | None = None) -> dict:
    entry = {
        "res": int(res),
        "nCells": int(n_cells),
        "nPeriods": int(n_periods),
        "file": path.name,
        "bytes": int(path.stat().st_size),
        "hash": compute_content_hash(path.read_bytes()),
    }
    if region is not None:
        entry["region"] = region
    if bbox:
        entry["lon"] = round(float(lon.mean()), 4)
        entry["lat"] = round(float(lat.mean()), 4)
        entry["bbox"] = [
            round(float(lon.min()), 4), round(float(lat.min()), 4),
            round(float(lon.max()), 4), round(float(lat.max()), 4),
        ]
    return entry


def coarsen(src_dir: Path, overview_res: int, detail_res: int,
            region_res: int, source_name: str | None,
            campaign_path: Path) -> dict:
    manifest_path = src_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    campaign = Campaign.load(campaign_path)

    source = src_dir / (source_name or manifest["overview"]["file"])
    if not source.exists():
        raise SystemExit(f"source overview bundle not found: {source}")

    desc, arrays = read_block(str(source))
    source_res = h3.get_resolution(desc["ids"][0])
    if source_res != detail_res:
        raise SystemExit(
            f"source overview is res {source_res} but detail target is "
            f"{detail_res}; re-import the demo or pass --detail-res {source_res}"
        )

    ids = list(desc["ids"])
    periods = desc["periods"]
    n_cells = len(ids)
    n_periods = len(periods)
    lon = np.asarray(desc["lon"], dtype=np.float64)
    lat = np.asarray(desc["lat"], dtype=np.float64)
    area = np.asarray(desc["areaKm2"], dtype=np.float64)

    field_names = [f["name"] for f in desc["fields"] if "." in f["name"]]
    fields = {name: _dequant(desc, arrays, name) for name in field_names}
    observed = np.asarray(arrays["observed"], dtype=bool)
    valid_fraction = _dequant(desc, arrays, "validFraction")
    n_obs = np.asarray(arrays["nObs"], dtype=np.int32)
    meta = {
        "bands": desc.get("bands", []),
        "indices": desc.get("indices", []),
        "year": manifest.get("periods", [{}])[0].get("year"),
        "epoch": desc.get("epoch"),
        "binDays": desc.get("binDays"),
        "source": desc.get("source"),
    }

    written: set[str] = set()

    # --- overview: aggregate res-7 children up to the target parent res ---
    parent_of = np.array([h3.cell_to_parent(cell, overview_res) for cell in ids])
    parents = sorted(set(parent_of.tolist()))
    index_of = {parent: i for i, parent in enumerate(parents)}
    child_of = np.array([index_of[p] for p in parent_of])
    n_parents = len(parents)

    agg_fields = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for name, values in fields.items():
            agg = np.full((n_periods, n_parents), np.nan, dtype=np.float32)
            for gi in range(n_parents):
                sel = child_of == gi
                agg[:, gi] = np.nanmean(values[:, sel], axis=1)
            agg_fields[name] = agg
        agg_vf = np.full((n_periods, n_parents), np.nan, dtype=np.float32)
        for gi in range(n_parents):
            sel = child_of == gi
            agg_vf[:, gi] = np.nanmean(valid_fraction[:, sel], axis=1)

    agg_obs = np.zeros((n_periods, n_parents), dtype=bool)
    agg_nobs = np.zeros((n_periods, n_parents), dtype=np.int32)
    for gi in range(n_parents):
        sel = child_of == gi
        agg_obs[:, gi] = observed[:, sel].any(axis=1)
        agg_nobs[:, gi] = n_obs[:, sel].sum(axis=1)

    ov_lon = np.empty(n_parents, dtype=np.float64)
    ov_lat = np.empty(n_parents, dtype=np.float64)
    ov_area = np.empty(n_parents, dtype=np.float64)
    for gi, parent in enumerate(parents):
        la, lo = h3.cell_to_latlng(parent)
        ov_lat[gi] = la
        ov_lon[gi] = lo
        ov_area[gi] = h3.cell_area(parent, unit="km^2")

    overview_block = _make_block(
        parents, ov_lon, ov_lat, ov_area, agg_fields, agg_obs, agg_vf,
        agg_nobs, periods, meta, overview_res, "overview",
        analysis=_analysis_fields(agg_fields, meta["indices"], periods, agg_obs),
        change=_change_for(agg_fields, periods, campaign),
    )
    info = write_block(
        overview_block, str(src_dir / f"overview_res{overview_res}.bin")
    )
    overview_path = Path(info["path"])
    written.add(overview_path.name)

    # --- detail: group the same res-7 cells by res-4 parent ---
    groups: dict[str, list[int]] = {}
    for idx, cell in enumerate(ids):
        groups.setdefault(get_region_parent(cell, region_res), []).append(idx)

    regions = []
    for parent in sorted(groups):
        sel = np.asarray(groups[parent], dtype=np.int64)
        sub_fields = {name: values[:, sel] for name, values in fields.items()}
        block = _make_block(
            [ids[i] for i in sel], lon[sel], lat[sel], area[sel], sub_fields,
            observed[:, sel], valid_fraction[:, sel], n_obs[:, sel], periods,
            meta, detail_res, "region",
            analysis=_analysis_fields(sub_fields, meta["indices"], periods,
                                      observed[:, sel]),
            change=_change_for(sub_fields, periods, campaign),
        )
        info = write_block(
            block, str(src_dir / f"region_{parent}_res{detail_res}.bin")
        )
        path = Path(info["path"])
        written.add(path.name)
        regions.append(_entry(
            path, detail_res, len(sel), n_periods, lon[sel], lat[sel],
            bbox=True, region=parent,
        ))

    # --- manifest ---
    # Running this tool twice should not keep appending the resolution suffix.
    base_name = re.sub(
        r"(?:\s*\(res \d+ overview / res \d+ detail\))+$", "",
        manifest.get("name", "South Florida Regional"),
    )
    manifest["name"] = (
        f"{base_name} (res {overview_res} overview / res {detail_res} detail)"
    )
    manifest["overview_res"] = int(overview_res)
    manifest["detail_res"] = int(detail_res)
    manifest["region_res"] = int(region_res)
    manifest["overview"] = _entry(
        overview_path, overview_res, n_parents, n_periods, ov_lon, ov_lat,
    )
    manifest["regions"] = regions
    manifest["analysis"] = ANALYSIS_SPECS
    manifest["analysis_note"] = (
        "Whole-record trend + wet/dry season fit per index; amp is half the "
        "fitted swing, phase is the peak season centre, anom is the largest "
        "residual in units of the cell's own residual scatter."
    )
    manifest["note"] = (
        f"Coarsened demo: res {overview_res} overview + res {detail_res} "
        f"region shards aggregated from the res {source_res} overview to keep "
        "browser memory bounded."
    )
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=1) + "\n", encoding="utf-8"
    )

    # --- sidecar ---
    sidecar = src_dir / "demo.json"
    if sidecar.exists():
        demo = json.loads(sidecar.read_text(encoding="utf-8"))
    else:
        demo = {"kind": "demo", "year": meta["year"]}
    demo["resolution_note"] = (
        f"overview res {overview_res}; region detail res {detail_res}; "
        f"aggregated from res {source_res} overview"
    )
    sidecar.write_text(json.dumps(demo, sort_keys=True, indent=2) + "\n",
                       encoding="utf-8")

    # --- drop bundles that the new manifest does not reference ---
    for path in sorted(src_dir.glob("overview_res*.bin.gz")):
        if path.name not in written:
            path.unlink()
    for path in sorted(src_dir.glob("region_*_res*.bin.gz")):
        if path.name not in written:
            path.unlink()

    write_catalog(campaign)

    return {
        "overview": manifest["overview"],
        "regions": len(regions),
        "overview_cells": n_parents,
        "region_cells_total": int(sum(r["nCells"] for r in regions)),
        "removed_source": not source.exists(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path,
                        default=Path("data/florida/demo/2017"))
    parser.add_argument("--campaign", type=Path,
                        default=Path("campaigns/florida-south.json"))
    parser.add_argument("--overview-res", type=int, default=6)
    parser.add_argument("--detail-res", type=int, default=7)
    parser.add_argument("--region-res", type=int, default=4)
    parser.add_argument("--source", default=None,
                        help="source overview file name (default: manifest overview)")
    args = parser.parse_args()

    summary = coarsen(
        args.src, args.overview_res, args.detail_res, args.region_res,
        args.source, args.campaign,
    )
    ov = summary["overview"]
    print(f"overview : {ov['file']}  res {ov['res']}  "
          f"{ov['nCells']:,} cells  {ov['bytes']/1e6:.1f} MB")
    print(f"regions  : {summary['regions']} shards at res "
          f"{args.detail_res}  ({summary['region_cells_total']:,} cells)")
    campaign = Campaign.load(args.campaign)
    print(f"catalog  : {campaign.catalog_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
