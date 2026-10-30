"""Shared fixtures for the test suite."""

from pathlib import Path

import pytest

from florida_h3.campaign import Campaign


@pytest.fixture
def campaign():
    return Campaign.load("campaigns/florida-south.json")


@pytest.fixture
def repo_root():
    return Path(__file__).resolve().parent.parent
