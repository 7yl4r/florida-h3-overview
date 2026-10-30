"""Tests for the continuous 16-day bin model."""

from datetime import date

import pytest

from florida_h3 import bins


def test_bin_index_round_trip():
    d = date(2017, 6, 15)
    b = bins.bin_index(d)
    assert bins.bin_start(b) <= d <= bins.bin_end(b)
    assert bins.bin_centre(b).year == 2017


def test_bins_for_year_2017():
    # 16-day bins anchored 2000-01-01: 2017 carries bins 388-410 (23 bins).
    bs = bins.bins_for_year(2017)
    assert bs == list(range(388, 411))
    assert len(bs) == 23


def test_bins_straddle_year_boundary():
    # The final bin of 2017 also overlaps 2018-01 (starts 2017-12-30).
    b2017 = bins.bins_for_year(2017)
    b2018 = bins.bins_for_year(2018)
    assert set(b2017) & set(b2018), "boundary bins must overlap"


def test_bin_start_end_centre():
    b = 388
    s = bins.bin_start(b)
    e = bins.bin_end(b)
    c = bins.bin_centre(b)
    assert s <= c <= e
    assert (e - s).days == 15  # inclusive 16-day bin


def test_season_wet_dry():
    assert bins.is_wet_season_date("2017-07-04")
    assert not bins.is_wet_season_date("2017-01-15")
    assert bins.season_of(date(2017, 9, 10)) == "wet"
