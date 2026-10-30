"""Tests for catalog build/verify, including the imported demo product."""

import json
from pathlib import Path

import pytest

from florida_h3.campaign import Campaign
from florida_h3.catalog import build_catalog, load_catalog, verify_catalog, write_catalog

CAMPAIGN = "campaigns/florida-south.json"
CATALOG_DATA = Path("data/florida/catalog.json")

requires_demo_data = pytest.mark.skipif(
    not CATALOG_DATA.exists(),
    reason="demo data not present (data/ is gitignored and local-only)",
)


@requires_demo_data
def test_build_catalog_structure():
    c = Campaign.load(CAMPAIGN)
    cat = build_catalog(c)
    assert cat["kind"] == "catalog"
    assert cat["slug"] == "florida-south"
    assert cat["campaign_hash"] == c.hash
    assert cat["aoi_hash"] == c.aoi_hash
    assert cat["aoi_geojson"]["type"] == "Polygon"
    assert "2017" in cat["years"]
    entry = cat["years"]["2017"]
    assert entry["demo"] is True
    assert entry["stale"] is False
    assert entry["periods"] == 23
    assert entry["regions"] == 72
    assert entry["overview"]["nCells"] == 18115
    assert entry["overview"]["res"] == 7


@requires_demo_data
def test_verify_catalog_passes_on_demo_data():
    c = Campaign.load(CAMPAIGN)
    assert verify_catalog(c) == []


@requires_demo_data
def test_verify_catalog_catches_tampered_hash():
    c = Campaign.load(CAMPAIGN)
    cat = load_catalog(c.catalog_path)
    cat["campaign_hash"] = "0000000000000000"
    c.catalog_path.write_text(json.dumps(cat, sort_keys=True, separators=(",", ":")))
    try:
        errors = verify_catalog(c)
        assert any("campaign hash does not match" in e for e in errors)
    finally:
        write_catalog(c)
