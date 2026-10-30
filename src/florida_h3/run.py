"""Command-line runner for a Florida campaign build.

    python -m florida_h3.run --campaign campaigns/florida-south.json \
        --year 2017 --stage grid,stac,mosaic,h3,product,derive,catalog,validate

Stages run in a fixed order and each one leaves its result on disk, so an
interrupted build resumes where it stopped and a single stage can be re-run in
isolation.  The network-bound stage is ``mosaic``; everything after it is
arithmetic over files already on disk.

    grid     -> cache/grid/grid.json
    stac     -> cache/<year>/stac/<year>.json
    mosaic   -> cache/<year>/mosaic/bin<N>/mosaic_bin<N>_<band>.tif
    h3       -> cache/<year>/h3/res<R>/bin<N>.npz
    product  -> product/<year>/manifest.json + bundles
    derive   -> product/derived/manifest.json + bundles
    catalog  -> catalog.json
    validate -> checks the catalog against the files on disk

The year is a build partition, not a disjoint slice of the time axis: the first
and last 16-day bin of a year can overlap the neighbouring year.  The cache
deduplicates bins when a whole-record layer is derived.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
import rasterio

from . import bins as binmod
from . import mosaic as mosaic_mod
from . import stac as stac_mod
from .aggregate import aggregate_mosaic
from .campaign import Campaign
from .catalog import verify_catalog, write_catalog
from .composite import load_composite, save_composite
from .derive import write_derived
from .export import export_year
from .grid import analysis_grid, build_grids, build_label_raster, grid_summary

STAGES = ("grid", "stac", "mosaic", "h3", "product", "derive", "catalog",
          "validate")
STAGE_ORDER = {name: i for i, name in enumerate(STAGES)}


def _log(msg: str) -> None:
    print(msg, flush=True)


class CampaignBuild:
    """One campaign/year build, one method per stage."""

    def __init__(self, campaign: Campaign, year: int, *, force: bool = False,
                 gz: bool = True, workers: int = 12, log=_log):
        self.campaign = campaign
        self.year = int(year)
        self.force = bool(force)
        self.gz = bool(gz)
        self.workers = int(workers)
        self.log = log
        self.bins_list = campaign.bins_for_year(self.year)
        self._grids = None
        self._labels = None

    # -- grids --------------------------------------------------------------
    def grids(self):
        if self._grids is None:
            self.log("[grid] building H3 grids")
            grids = build_grids(self.campaign)
            # One analysis raster for every resolution.  The mosaic stage
            # builds that raster once; the label images differ only in which
            # cell owns each pixel.  Detail resolution at 30 m needs a second
            # mosaic pass, which is deliberately a separate build.
            target = analysis_grid(self.campaign, self.campaign.overview_res)
            labels = {res: build_label_raster(grid, target)[0]
                      for res, grid in grids.items()}
            self._grids = grids
            self._labels = labels
        return self._grids, self._labels

    def stage_grid(self) -> dict:
        grids, _ = self.grids()
        summary = grid_summary(self.campaign, grids)
        path = self.campaign.grid_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(summary, fh, sort_keys=True, separators=(",", ":"))
        for res, entry in summary["resolutions"].items():
            self.log(f"  res {res}: {entry['cells']:,} cells")
        self.log(f"  raster: {summary['analysis_grid']['width']:,} x "
                 f"{summary['analysis_grid']['height']:,} px "
                 f"at {summary['pixel_size']} m")
        return summary

    # -- stage: stac --------------------------------------------------------
    def stage_stac(self) -> dict:
        path = self.campaign.stac_cache_path(self.year)
        if path.exists() and not self.force:
            with path.open(encoding="utf-8") as fh:
                payload = json.load(fh)
            self.log(f"[stac] cached: {path}")
            return payload

        self.log(f"[stac] enumerating {len(self.bins_list)} bins for {self.year}")
        scenes_by_bin = {}
        for b in self.bins_list:
            scenes = stac_mod.find_scenes_for_bin(self.campaign, b)
            if scenes:
                scenes_by_bin[b] = scenes
                self.log(f"  bin {b} ({self.campaign.bin_start(b)}): "
                         f"{len(scenes)} scenes")
        payload = stac_mod.scenes_payload(self.campaign, self.year, scenes_by_bin)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
        self.log(f"  bins with scenes: {len(scenes_by_bin)}/{len(self.bins_list)}")
        return payload

    def load_scenes(self) -> dict:
        path = self.campaign.stac_cache_path(self.year)
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as fh:
            return stac_mod.scenes_from_payload(json.load(fh))

    # -- stage: mosaic ------------------------------------------------------
    def _cached_mosaic(self, dest: Path, b: int) -> dict | None:
        paths = {band: dest / f"mosaic_bin{b}_{band}.tif"
                 for band in self.campaign.bands}
        paths["nobs"] = dest / f"nobs_bin{b}.tif"
        if not all(p.exists() for p in paths.values()):
            return None
        transform, (h, w), _ = analysis_grid(self.campaign,
                                             self.campaign.overview_res)
        for p in paths.values():
            try:
                with rasterio.open(p) as src:
                    if src.width != w or src.height != h:
                        return None
                    src.read(1, window=rasterio.windows.Window(w - 1, h - 1, 1, 1))
            except Exception:  # noqa: BLE001 - unusable cache entry
                return None
        return {k: str(v) for k, v in paths.items()}

    def stage_mosaic(self) -> dict:
        scenes_by_bin = self.load_scenes()
        if not scenes_by_bin:
            raise RuntimeError(
                f"no scene cache at {self.campaign.stac_cache_path(self.year)}; "
                f"run the stac stage first"
            )
        self.log(f"[mosaic] building {self.year} mosaics "
                 f"at {self.campaign.pixel_size} m")
        out = {}
        for b in self.bins_list:
            scenes = scenes_by_bin.get(b)
            if not scenes:
                self.log(f"  bin {b}: no scenes, skipping")
                continue
            dest = self.campaign.mosaic_dir(self.year, b)
            cached = self._cached_mosaic(dest, b)
            if cached is not None and not self.force:
                self.log(f"  bin {b}: mosaic cached")
                out[b] = cached
                continue
            t0 = time.time()
            out[b] = mosaic_mod.build_mosaic_bin(
                self.campaign, b, scenes, dest,
                workers=self.workers, log=self.log,
            )
            self.log(f"  bin {b}: mosaic in {time.time() - t0:.0f}s")
        return out

    # -- stage: h3 ----------------------------------------------------------
    def _sample_bin(self, b: int, paths: dict):
        grids, labels = self.grids()
        mosaic_bands = {}
        for band in self.campaign.bands:
            p = paths.get(band)
            if p and os.path.exists(p):
                mosaic_bands[band] = mosaic_mod.read_mosaic_band(p)
        nobs_px = None
        if paths.get("nobs") and os.path.exists(paths["nobs"]):
            nobs_px = mosaic_mod.read_mosaic_nobs(paths["nobs"])

        out = []
        for res, grid in grids.items():
            agg = aggregate_mosaic(mosaic_bands, labels[res], len(grid),
                                   nobs=nobs_px)
            valid_fraction = (agg["n_valid"].astype(np.float32)
                              / np.maximum(agg["n_total"].astype(np.float32), 1))
            observed = ((valid_fraction >= self.campaign.min_valid_fraction)
                        & (agg["n_obs"] > 0))
            for band in self.campaign.bands:
                agg["bands"][band] = np.where(observed, agg["bands"][band], np.nan)
            reduced = {
                "refl": agg["bands"],
                "valid_fraction": valid_fraction.astype(np.float32),
                "n_obs": agg["n_obs"],
                "observed": observed,
            }
            out.append((b, res, reduced))
        return out

    def stage_h3(self) -> list:
        scenes_by_bin = self.load_scenes()
        if not scenes_by_bin:
            raise RuntimeError("run the stac stage first")
        grids, _ = self.grids()
        self.log("[h3] aggregating mosaics into cells")
        built = []
        for b in self.bins_list:
            if b not in scenes_by_bin:
                continue
            cache_paths = {res: self.campaign.h3_path(self.year, res, b)
                           for res in grids}
            if all(p.exists() for p in cache_paths.values()) and not self.force:
                for res in sorted(grids):
                    _, refl, sup = load_composite(str(cache_paths[res]))
                    built.append((b, res, {"refl": refl, **sup}))
                self.log(f"  bin {b}: cached")
                continue

            dest = self.campaign.mosaic_dir(self.year, b)
            paths = self._cached_mosaic(dest, b)
            if paths is None:
                raise RuntimeError(
                    f"bin {b}: no mosaic in {dest}; run the mosaic stage first"
                )
            sampled = self._sample_bin(b, paths)
            for bin_num, res, reduced in sampled:
                path = cache_paths[res]
                path.parent.mkdir(parents=True, exist_ok=True)
                save_composite(str(path), res, self.year, bin_num, reduced)
            built.extend(sampled)
            self.log(f"  bin {b}: {len(sampled)} resolutions aggregated")
        return built

    def load_composites(self) -> list:
        grids, _ = self.grids()
        out = []
        for b in self.bins_list:
            for res in sorted(grids):
                path = self.campaign.h3_path(self.year, res, b)
                if not path.exists():
                    continue
                _, refl, sup = load_composite(str(path))
                out.append((b, res, {"refl": refl, **sup}))
        return out

    # -- stage: product -----------------------------------------------------
    def stage_product(self) -> dict:
        grids, _ = self.grids()
        composites = self.load_composites()
        if not composites:
            raise RuntimeError("no h3 cache; run the h3 stage first")
        out_dir = self.campaign.year_product(self.year)
        self.log(f"[product] writing {out_dir}")
        info = export_year(
            self.campaign, grids, composites, self.bins_list, self.year,
            out_dir, gz=self.gz,
        )
        self.log(f"  overview: {info['overview']}")
        self.log(f"  regions: {len(info['regions'])} shards")
        return info

    # -- stage: derive ------------------------------------------------------
    def stage_derive(self) -> dict:
        self.log("[derive] building whole-record layers")
        manifest = write_derived(self.campaign, years=[self.year], gz=self.gz)
        self.log(f"  overview: {manifest['overview']}")
        self.log(f"  regions: {len(manifest['regions'])} shards")
        return manifest

    # -- stage: catalog -----------------------------------------------------
    def stage_catalog(self) -> dict:
        self.log("[catalog] writing catalog")
        catalog = write_catalog(self.campaign)
        self.log(f"  years: {', '.join(sorted(catalog['years'])) or 'none'}")
        return catalog

    # -- stage: validate ----------------------------------------------------
    def stage_validate(self) -> list:
        self.log("[validate] checking catalog and bundles")
        errors = verify_catalog(self.campaign)
        if errors:
            for error in errors:
                self.log(f"  ! {error}")
            raise SystemExit(f"{len(errors)} validation error(s)")
        self.log("  ok")
        return errors

    # -- driver -------------------------------------------------------------
    def run(self, stages=None) -> dict:
        stages = list(stages or STAGES)
        unknown = [s for s in stages if s not in STAGE_ORDER]
        if unknown:
            raise ValueError(f"unknown stage(s): {', '.join(unknown)}")
        result = {}
        for name in STAGES:
            if name not in stages:
                continue
            result[name] = getattr(self, f"stage_{name}")()
        return result


def parse_stages(spec: str | None) -> list[str]:
    if not spec:
        return list(STAGES)
    return [s.strip() for s in spec.split(",") if s.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--campaign", default="campaigns/florida-south.json",
                    help="campaign JSON (default: %(default)s)")
    ap.add_argument("--year", type=int, default=None,
                    help="year to build (default: first campaign build year)")
    ap.add_argument("--stage", default="",
                    help=f"comma-separated stages ({','.join(STAGES)})")
    ap.add_argument("--force", action="store_true",
                    help="ignore cached scenes, mosaics and composites")
    ap.add_argument("--workers", type=int, default=12,
                    help="concurrent scene reads per mosaic bin")
    ap.add_argument("--no-gzip", action="store_true",
                    help="write uncompressed .bin bundles")
    args = ap.parse_args(argv)

    campaign = Campaign.load(args.campaign)
    years = [args.year] if args.year is not None else campaign.build_years
    if not years:
        raise SystemExit("campaign has no build years")

    stages = parse_stages(args.stage)
    for year in years:
        print(f"\n=== {campaign.name} · {year} ===", flush=True)
        build = CampaignBuild(
            campaign, year, force=args.force, gz=not args.no_gzip,
            workers=args.workers,
        )
        build.run(stages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
