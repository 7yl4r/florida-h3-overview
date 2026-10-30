#!/usr/bin/env python3
"""Build the Florida-south campaign AOI from public boundary sources.

The AOI is deliberately a polygon, not a bounding box:

    southern Florida land
  + a 10 km coastal buffer (state waters and a little breathing room)
  + the part of the U.S. EEZ within 200 nautical miles of Florida
  all clipped to the southern-half boundary at 27.4 degrees north.

The 200 nm buffer is computed in Florida Albers (EPSG:3086) and then
intersected with the U.S. EEZ polygon.  That intersection is what keeps the
AOI out of Bahamian and Cuban waters: a 200 nm buffer of Florida alone would
include waters that are not Florida's EEZ.

Inputs are the Census 2024 1:500k state boundary and the U.S. EEZ feature
service.  They are not committed; the generated campaign AOI is.

Usage:

    python tools/make_aoi.py \
        --florida-shp /tmp/flh3/cb_2024_us_state_500k.shp \
        --eez-geojson /tmp/flh3/us_eez.geojson \
        --out campaigns/florida-south.geojson
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from pyproj import Geod, Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid


SOUTHERN_BOUNDARY = 27.4
COASTAL_BUFFER_M = 10_000
EEZ_BUFFER_M = 200 * 1852  # 200 nautical miles in metres
SIMPLIFY_DEG = 0.002       # ~200 m; far below a res-7/8 hexagon


def _read_florida(shp_path: str):
    import shapefile

    reader = shapefile.Reader(shp_path)
    for i, rec in enumerate(reader.records()):
        if rec["STUSPS"] == "FL":
            return shape(reader.shape(i).__geo_interface__)
    raise SystemExit(f"no Florida feature in {shp_path}")


def _read_us_eez(path: str):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for feat in data["features"]:
        props = feat.get("properties") or {}
        if props.get("eez") == "United States Exclusive Economic Zone":
            return make_valid(shape(feat["geometry"]))
    raise SystemExit(f"no contiguous U.S. EEZ feature in {path}")


def _geodesic_km2(geom) -> float:
    area, _ = Geod(ellps="WGS84").geometry_area_perimeter(geom)
    return abs(area) / 1e6


def build_aoi(florida, us_eez) -> tuple:
    south = box(-180.0, 0.0, -60.0, SOUTHERN_BOUNDARY)
    to_albers = Transformer.from_crs("EPSG:4326", "EPSG:3086", always_xy=True)
    to_wgs84 = Transformer.from_crs("EPSG:3086", "EPSG:4326", always_xy=True)

    florida_south = make_valid(florida.intersection(south)).buffer(0)
    florida_albers = transform(to_albers.transform, florida_south)

    coastal = transform(
        to_wgs84.transform, florida_albers.buffer(COASTAL_BUFFER_M)
    )
    eez_buffer = transform(
        to_wgs84.transform, florida_albers.buffer(EEZ_BUFFER_M)
    )

    us_eez_south = make_valid(us_eez.intersection(south)).buffer(0)
    florida_eez = make_valid(us_eez_south.intersection(eez_buffer)).buffer(0)

    aoi = unary_union([florida_south, coastal, florida_eez])
    aoi = make_valid(aoi.intersection(south)).buffer(0)
    aoi = aoi.simplify(SIMPLIFY_DEG, preserve_topology=True)
    if not aoi.is_valid:
        aoi = make_valid(aoi)
    return aoi, florida_south, coastal, florida_eez


def main() -> None:
    global SOUTHERN_BOUNDARY

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--florida-shp", required=True)
    ap.add_argument("--eez-geojson", required=True)
    ap.add_argument("--out", default="campaigns/florida-south.geojson")
    ap.add_argument("--boundary", type=float, default=SOUTHERN_BOUNDARY)
    args = ap.parse_args()

    SOUTHERN_BOUNDARY = args.boundary

    florida = _read_florida(args.florida_shp)
    us_eez = _read_us_eez(args.eez_geojson)
    aoi, land, coastal, eez = build_aoi(florida, us_eez)

    feature = {
        "type": "Feature",
        "properties": {
            "name": "Southern Florida and Florida EEZ",
            "description": (
                "Southern Florida land plus a 10 km coastal buffer and the "
                "U.S. EEZ within 200 nm of Florida, clipped south of "
                f"{SOUTHERN_BOUNDARY} N."
            ),
            "boundary_lat": SOUTHERN_BOUNDARY,
            "coastal_buffer_m": COASTAL_BUFFER_M,
            "eez_buffer_nm": 200,
            "area_km2": round(_geodesic_km2(aoi), 2),
            "land_area_km2": round(_geodesic_km2(land), 2),
            "eez_area_km2": round(_geodesic_km2(eez), 2),
            "sources": [
                "U.S. Census Bureau 2024 cartographic boundary, 1:500,000",
                "U.S. EEZ (NOAA/ArcGIS Living Atlas feature service)",
            ],
            "generated": date.today().isoformat(),
        },
        "geometry": mapping(aoi),
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": [feature]}, fh,
                  sort_keys=True, separators=(",", ":"))
        fh.write("\n")

    print(f"wrote {args.out}")
    print(f"  AOI area      {_geodesic_km2(aoi):>12,.1f} km2")
    print(f"  land          {_geodesic_km2(land):>12,.1f} km2")
    print(f"  EEZ           {_geodesic_km2(eez):>12,.1f} km2")
    print(f"  bounds        {tuple(round(v, 4) for v in aoi.bounds)}")


if __name__ == "__main__":
    main()
