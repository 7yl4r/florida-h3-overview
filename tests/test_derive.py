"""End-to-end test of derive.py against a small synthetic h3 cache."""

import json

import numpy as np
import pytest

from florida_h3.bins import bins_for_year
from florida_h3.campaign import Campaign
from florida_h3.composite import save_composite
from florida_h3.derive import write_derived
from florida_h3.grid import build_grids
from florida_h3.binary import read_block

# A small AOI entirely inside South Florida so the res-7 grid is tiny.
AOI_GEOJSON = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [-81.10, 25.45], [-80.90, 25.45], [-80.90, 25.60],
                [-81.10, 25.60], [-81.10, 25.45],
            ]],
        },
    }],
}

CAMPAIGN_JSON = {
    "schema_version": 1,
    "name": "synthetic test",
    "slug": "test-synthetic",
    "description": "tiny AOI for derive tests",
    "aoi": {"path": "test.geojson", "buffer_km": 0},
    "analysis": {"crs": "EPSG:3086", "pixel_size": 90, "detail_pixel_size": 30},
    "resolutions": {"overview": 7, "detail": 8, "region_res": 4},
    "years": {"start": 2017, "end": 2017, "build": [2017]},
    "temporal": {"epoch": "2000-01-01", "bin_days": 16},
    "sources": {
        "stac_endpoint": "https://example.com/stac",
        "collection": "landsat-c2-l2",
        "platforms": ["landsat-8"],
    },
    "processing": {"min_valid_fraction": 0.25, "max_scene_cloud": 85.0,
                   "max_pixel_cloud": 90.0, "max_scenes_per_composite": 12,
                   "composite_stat": "median"},
    "output": {"root": "data/florida"},
    "events": [],
}


@pytest.fixture
def synth(tmp_path):
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    (campaigns / "test.geojson").write_text(json.dumps(AOI_GEOJSON))
    (campaigns / "test.json").write_text(json.dumps(CAMPAIGN_JSON, indent=2))
    c = Campaign.load(str(campaigns / "test.json"))
    grids = build_grids(c, resolutions=(7,))
    assert len(grids[7]) > 0, "synthetic AOI holds no res-7 cells"
    n_cells = len(grids[7])
    bins_ = bins_for_year(2017)
    # Save a short series with a trend and a step break halfway.
    mid = len(bins_) // 2
    for i, b in enumerate(bins_[:10]):
        trend = 0.02 * i
        step = 0.15 if i >= mid else 0.0
        ndvi = np.clip(0.6 + trend + step + 0.01 * np.arange(n_cells), -0.9, 0.9)
        nir = 0.5 * (1 + ndvi) / 2
        red = 0.5 * (1 - ndvi) / 2
        reduced = {
            "refl": {"blue": np.full(n_cells, 0.1, np.float32),
                     "green": np.full(n_cells, 0.1, np.float32),
                     "red": red.astype(np.float32),
                     "nir": nir.astype(np.float32),
                     "swir1": np.full(n_cells, 0.1, np.float32),
                     "swir2": np.full(n_cells, 0.1, np.float32)},
            "valid_fraction": np.ones(n_cells, np.float32),
            "n_obs": np.full(n_cells, 3, np.int32),
            "observed": np.full(n_cells, True),
        }
        save_composite(c.h3_path(2017, 7, b), 7, 2017, b, reduced)
    return c, grids[7], bins_[:10]


def test_derive_manifest(synth):
    c, grid, bins_ = synth
    manifest = write_derived(c, resolutions=(7,), years=[2017])
    assert manifest["campaign_hash"] == c.hash
    assert manifest["aoi_hash"] == c.aoi_hash
    assert manifest["overview"]["nCells"] == len(grid)
    assert manifest["overview"]["res"] == 7
    # The manifest records every bin overlapping the campaign year.
    assert manifest["bins"] == sorted(int(b) for b in bins_for_year(2017))
    # The overview bundle only carries the bins actually cached.
    assert manifest["overview"]["nBins"] == len(bins_)
    # The manifest records the full method spec string.
    assert any(m.startswith("harmonic_fscan_v1") for m in manifest["methods"])
    assert [a["key"] for a in manifest["analysis"]] == [
        "trend", "trendz", "amp", "phase", "r2", "anom",
    ]
    assert (c.derived_dir / "manifest.json").exists()


def test_derive_bundle_fields(synth):
    c, grid, _ = synth
    write_derived(c, resolutions=(7,), years=[2017])
    path = c.derived_dir / "derived_res7.bin.gz"
    desc, data = read_block(str(path))
    assert desc["tier"] == "derived"
    assert desc["resolution"] == 7
    assert desc["nCells"] == len(grid)
    # Per-cell derived fields.
    for field in ("cell.trend_slope", "cell.trend_intercept", "cell.ndvi_mean",
                  "cell.ndvi_std", "cell.observed_fraction"):
        assert field in data, f"missing {field}"
    # Cellraw counts.
    assert "cellraw.n_obs_total" in data
    assert "cellraw.n_bins" in data
    # Whole-record trend/season assessment, one field per index.
    for field in ("cell.trend.ndvi", "cell.trendz.ndvi", "cell.amp.ndvi",
                  "cell.phase.ndvi", "cell.r2.ndvi", "cell.anom.ndvi",
                  "cell.mean.ndvi", "cell.std.ndvi"):
        assert field in data, f"missing {field}"
    assert "cellraw.anomp.ndvi" in data
    assert desc["cells"] and desc["cellraws"]
    # Change fields on the single pseudo-period.
    assert "break_bin" in data
    assert "break_score" in data
    assert "method" in data
    assert desc["changeMethods"]
    assert data["observed"].shape == (1, len(grid))
