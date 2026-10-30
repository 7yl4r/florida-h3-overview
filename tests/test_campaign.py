"""Tests for campaign loading and AOI identity."""

from florida_h3.campaign import Campaign

CAMPAIGN = "campaigns/florida-south.json"


def test_campaign_loads():
    c = Campaign.load(CAMPAIGN)
    assert c.name == "Southern Florida and Florida EEZ"
    assert c.slug == "florida-south"
    assert c.crs == "EPSG:3086"
    assert c.pixel_size == 90
    assert c.detail_pixel_size == 30
    assert c.resolutions == (7, 8)
    assert c.year_start == 2017 and c.year_end == 2017
    assert c.build_years == [2017]
    assert c.bin_days == 16
    assert c.epoch.isoformat() == "2000-01-01"


def test_campaign_hash_and_area():
    c = Campaign.load(CAMPAIGN)
    assert c.hash == "bdd5aac9a63baa6f"
    assert c.aoi_hash == "84627e8785367888"
    assert c.area_km2 == 239655.23


def test_campaign_summary():
    c = Campaign.load(CAMPAIGN)
    s = c.summary()
    assert "239,655 km" in s
    assert "EPSG:3086 @ 90 m" in s
    assert "res (7, 8)" in s
    assert "2017-2017" in s


def test_campaign_bin_partition():
    c = Campaign.load(CAMPAIGN)
    bins = c.bins_for_year(2017)
    assert bins == list(range(388, 411))
    assert c.bin_start(388).year == 2016 or c.bin_start(388).year == 2017
    assert c.bin_centre(388).year == 2017


def test_campaign_paths_relative_to_repo_root(campaign):
    c = campaign
    assert c.data_root.name == "florida"
    assert c.catalog_path.name == "catalog.json"
    assert c.year_product(2017).name == "2017"
    assert c.derived_dir.name == "derived"

