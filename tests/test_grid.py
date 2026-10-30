"""Tests for the polygon H3 grids and analysis raster."""

import numpy as np

from florida_h3.campaign import Campaign
from florida_h3.grid import build_grids, grid_summary

CAMPAIGN = "campaigns/florida-south.json"


def test_build_grids_counts():
    c = Campaign.load(CAMPAIGN)
    grids = build_grids(c, resolutions=(7, 8))
    assert set(grids) == {7, 8}
    assert len(grids[7]) == 48472
    assert len(grids[8]) == 341671


def test_grid_centroids_and_areas():
    c = Campaign.load(CAMPAIGN)
    g = build_grids(c, resolutions=(7,))[7]
    assert g.crs == "EPSG:3086"
    assert g.pixel_size == 90
    assert g.centroids_lonlat.shape == (len(g), 2)
    assert g.areas_km2.shape == (len(g),)
    assert (g.areas_km2 > 0).all()
    assert len(g.index_of) == len(g)


def test_res6_grid():
    c = Campaign.load(CAMPAIGN)
    g = build_grids(c, resolutions=(6,))[6]
    assert len(g) == 6790


def test_grid_summary():
    c = Campaign.load(CAMPAIGN)
    grids = build_grids(c, resolutions=(7,))
    s = grid_summary(c, grids)
    assert s["campaign"] == "florida-south"
    assert s["aoi_hash"] == c.aoi_hash
    assert s["resolutions"]["7"]["cells"] == 48472


def test_bbox_matches_aoi():
    c = Campaign.load(CAMPAIGN)
    grids = build_grids(c, resolutions=(7, 8))
    w, s, e, n = c.bounds
    for g in grids.values():
        gw, gs, ge, gn = g.bbox
        assert gw >= w - 0.02 and gs >= s - 0.02
        assert ge <= e + 0.02 and gn <= n + 0.02
