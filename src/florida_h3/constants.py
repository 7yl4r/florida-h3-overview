"""Sensor and product constants shared by the Florida pipeline.

These are not campaign settings.  They describe Landsat Collection 2 Level-2
and the spectral indices the product carries; a campaign may override the
platform list or the pixel size, but the band definitions and index formulas
have exactly one implementation here.
"""

from __future__ import annotations

BANDS = ("blue", "green", "red", "nir", "swir1", "swir2")
NIR_BAND = "nir"

# Landsat 4-7 (TM/ETM+) expose "nir"; Landsat 8-9 (OLI-2) expose "nir08".
BAND_ASSETS = {
    "blue": ("blue", "coastal"),
    "green": ("green",),
    "red": ("red",),
    "nir": ("nir08", "nir"),
    "swir1": ("swir16",),
    "swir2": ("swir22",),
}

BAND_INFO = {
    "blue": {"label": "blue", "nm": 482, "range": (450, 515)},
    "green": {"label": "green", "nm": 562, "range": (525, 600)},
    "red": {"label": "red", "nm": 655, "range": (630, 680)},
    "nir": {"label": "NIR", "nm": 865, "range": (845, 885)},
    "swir1": {"label": "SWIR1", "nm": 1610, "range": (1565, 1655)},
    "swir2": {"label": "SWIR2", "nm": 2200, "range": (2105, 2295)},
}

# Landsat Collection 2 Level-2 scaling: reflectance = DN * 0.0000275 - 0.2.
SR_SCALE = 0.0000275
SR_OFFSET = -0.2

QA_PIXEL_ASSETS = ("qa_pixel",)

INDEX_SPECS = {
    "ndvi": {
        "label": "NDVI",
        "kind": "ratio",
        "bands": ("nir", "red"),
        "range": (-1, 1),
        "domain": "greenness",
        "desc": "Normalised Difference Vegetation Index (McBeth & Jordan)",
    },
    "ndwi": {
        "label": "NDWI",
        "kind": "ratio",
        "bands": ("green", "nir"),
        "range": (-1, 1),
        "domain": "open water",
        "desc": "Normalised Difference Water Index (McFeeters 1996)",
    },
    "mndwi": {
        "label": "MNDWI",
        "kind": "ratio",
        "bands": ("green", "swir1"),
        "range": (-1, 1),
        "domain": "open water",
        "desc": "Modified NDWI, less sensitive to atmospheric noise (Xu 2006)",
    },
    "ndmi": {
        "label": "NDMI",
        "kind": "ratio",
        "bands": ("nir", "swir1"),
        "range": (-1, 1),
        "domain": "canopy moisture",
        "desc": "Normalised Difference Moisture Index",
    },
    "nbr": {
        "label": "NBR",
        "kind": "ratio",
        "bands": ("red", "swir2"),
        "range": (-1, 1),
        "domain": "burn / bare substrate",
        "desc": "Normalised Burn Ratio (Key & Benson)",
    },
    "savi": {
        "label": "SAVI",
        "kind": "savi",
        "bands": ("nir", "red"),
        "L": 0.2,
        "range": (-1.5, 1.5),
        "domain": "greenness, soil adjusted",
        "desc": "Soil-Adjusted Vegetation Index, L=0.2",
    },
}

INDICES = tuple(INDEX_SPECS)
