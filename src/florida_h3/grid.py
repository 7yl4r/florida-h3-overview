"""Campaign-driven H3 grids and the analysis raster.

Differences from the vendored h3lab grid module:

* the AOI is a polygon/multipolygon, not a bbox;
* the analysis CRS and pixel size come from the campaign;
* the analysis raster is aligned to the CRS origin so two builds of the same
  campaign produce the same transform;
* the label raster and the AOI mask are built from the same projected geometry
  the mosaic uses.

Cells are selected with H3's own polygon rasteriser in ``contain="full"`` mode.
That is the same rule as a per-cell shapely ``contains`` test -- keep a cell
only when its whole hexagon is inside the AOI -- but it runs in C and is fast
enough for a 340k-cell res-8 grid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import h3
import numpy as np
from shapely.geometry import MultiPolygon, Polygon, mapping
from shapely.ops import transform

from .campaign import Campaign


@dataclass
class HexGrid:
    resolution: int
    cells: np.ndarray                 # (N,) H3 cell ids as strings
    centroids_lonlat: np.ndarray      # (N, 2) (lng, lat)
    centroids_proj: np.ndarray        # (N, 2) in campaign CRS
    index_of: dict                    # cell id -> row index
    areas_km2: np.ndarray             # (N,)
    aoi_geom: object
    crs: str
    pixel_size: int

    def __len__(self) -> int:
        return len(self.cells)

    @property
    def bbox(self) -> list[float]:
        lon, lat = self.centroids_lonlat[:, 0], self.centroids_lonlat[:, 1]
        return [float(lon.min()), float(lat.min()),
                float(lon.max()), float(lat.max())]

    def subset(self, mask) -> "HexGrid":
        sel = np.asarray(mask)
        keep = np.flatnonzero(sel) if sel.dtype == bool else sel.astype(np.int64).ravel()
        return HexGrid(
            resolution=self.resolution,
            cells=self.cells[keep],
            centroids_lonlat=self.centroids_lonlat[keep],
            centroids_proj=self.centroids_proj[keep],
            index_of={str(self.cells[i]): k for k, i in enumerate(keep)},
            areas_km2=self.areas_km2[keep],
            aoi_geom=self.aoi_geom,
            crs=self.crs,
            pixel_size=self.pixel_size,
        )


def _project(geom, crs: str):
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    return transform(tr.transform, geom)


def _centroids_proj(cells: np.ndarray, crs: str) -> np.ndarray:
    from pyproj import Transformer

    ll = np.array([h3.cell_to_latlng(c) for c in cells], dtype=float)  # lat, lng
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x, y = tr.transform(ll[:, 1], ll[:, 0])
    return np.column_stack([x, y])


def _h3_shape(geom):
    """Convert a shapely Polygon/MultiPolygon to an H3 LatLng shape.

    H3 expects (lat, lng) order; shapely stores (lng, lat).
    """
    from h3.api.basic_str import LatLngMultiPoly, LatLngPoly

    polys = [geom] if isinstance(geom, Polygon) else list(geom.geoms)
    shapes = []
    for poly in polys:
        if poly.is_empty:
            continue
        outer = [(lat, lng) for lng, lat in poly.exterior.coords]
        holes = [[(lat, lng) for lng, lat in ring.coords]
                 for ring in poly.interiors]
        shapes.append(LatLngPoly(outer, *holes))
    if not shapes:
        raise ValueError("AOI has no polygonal parts")
    return shapes[0] if len(shapes) == 1 else LatLngMultiPoly(*shapes)


def cells_within_polygon(geom, resolution: int) -> list[str]:
    """H3 cells at ``resolution`` whose whole hexagon lies inside ``geom``."""
    return list(h3.h3shape_to_cells_experimental(
        _h3_shape(geom), resolution, contain="full"
    ))


def build_grids(campaign: Campaign,
                resolutions: tuple[int, ...] | None = None,
                ) -> dict[int, HexGrid]:
    resolutions = tuple(resolutions or campaign.resolutions)
    grids: dict[int, HexGrid] = {}
    for res in sorted(resolutions):
        cells = cells_within_polygon(campaign.aoi, res)
        if not cells:
            raise ValueError(
                f"no res-{res} cells fully inside the AOI "
                f"({campaign.aoi_hash})"
            )
        cents = np.array([h3.cell_to_latlng(c) for c in cells], dtype=float)
        cents = np.column_stack([cents[:, 1], cents[:, 0]])  # lng, lat
        cells_arr = np.array(cells, dtype=object)
        grids[res] = HexGrid(
            resolution=res,
            cells=cells_arr,
            centroids_lonlat=cents,
            centroids_proj=_centroids_proj(cells_arr, campaign.crs),
            index_of={str(c): i for i, c in enumerate(cells_arr)},
            areas_km2=np.array(
                [h3.cell_area(c, unit="km^2") for c in cells_arr], dtype=float
            ),
            aoi_geom=campaign.aoi,
            crs=campaign.crs,
            pixel_size=campaign.pixel_size_for(res),
        )
    return grids


def analysis_grid(campaign: Campaign, resolution: int | None = None):
    """Canonical raster for the campaign AOI.

    Returns ``(transform, (height, width), (minx, miny, maxx, maxy))`` in the
    campaign CRS.  The origin is snapped to a multiple of the pixel size so the
    same campaign always produces the same grid.
    """
    pixel_size = campaign.pixel_size_for(
        resolution if resolution is not None else campaign.overview_res
    )
    projected = _project(campaign.aoi, campaign.crs)
    minx, miny, maxx, maxy = projected.bounds

    minx = math.floor(minx / pixel_size) * pixel_size
    maxy = math.ceil(maxy / pixel_size) * pixel_size
    width = int(math.ceil((maxx - minx) / pixel_size))
    height = int(math.ceil((maxy - miny) / pixel_size))
    maxx = minx + width * pixel_size
    miny = maxy - height * pixel_size

    from rasterio.transform import from_origin

    return (from_origin(minx, maxy, pixel_size, pixel_size),
            (height, width), (minx, miny, maxx, maxy))


def _projected_geometry(campaign: Campaign):
    return _project(campaign.aoi, campaign.crs)


def build_label_raster(grid: HexGrid, target=None, campaign: Campaign | None = None):
    """Rasterise a grid into an integer label image on the analysis grid.

    Labels are ``row index + 1``; 0 means "no cell".  The transform is the
    campaign's canonical analysis grid, so a mosaic read once can be binned
    into every resolution with the same pixel array.
    """
    from rasterio.features import rasterize
    from pyproj import Transformer

    if target is None:
        if campaign is None:
            raise ValueError("target or campaign is required")
        target = analysis_grid(campaign, grid.resolution)
    transform_, (h, w), bounds = target
    tr = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)

    shapes = []
    for i, cell in enumerate(grid.cells):
        ring = [(lng, lat) for lat, lng in h3.cell_to_boundary(str(cell))]
        xy = [tr.transform(lng, lat) for lng, lat in ring]
        shapes.append({"type": "Polygon", "coordinates": [xy]})

    labels = rasterize(
        ((shape, i + 1) for i, shape in enumerate(shapes)),
        out_shape=(h, w),
        transform=transform_,
        fill=0,
        dtype="int32",
        all_touched=False,
    )
    return labels, transform_, bounds


def build_aoi_mask(campaign: Campaign, target=None) -> np.ndarray:
    """Boolean raster: True inside the campaign AOI."""
    from rasterio.features import rasterize

    if target is None:
        target = analysis_grid(campaign)
    transform_, (h, w), _ = target
    geom = _projected_geometry(campaign)
    mask = rasterize(
        [(mapping(geom), 1)],
        out_shape=(h, w),
        transform=transform_,
        fill=0,
        dtype="uint8",
        all_touched=False,
    )
    return mask.astype(bool)


def grid_summary(campaign: Campaign, grids: dict[int, HexGrid]) -> dict:
    transform_, (h, w), bounds = analysis_grid(campaign)
    out = {
        "campaign": campaign.slug,
        "aoi_hash": campaign.aoi_hash,
        "aoi_area_km2": campaign.area_km2,
        "crs": campaign.crs,
        "pixel_size": campaign.pixel_size,
        "analysis_grid": {
            "width": int(w),
            "height": int(h),
            "pixels": int(w) * int(h),
            "bounds": [round(float(v), 3) for v in bounds],
            "transform": [round(float(v), 6) for v in tuple(transform_)[:6]],
        },
        "resolutions": {},
    }
    for res, grid in sorted(grids.items()):
        out["resolutions"][str(res)] = {
            "cells": len(grid),
            "pixel_size": grid.pixel_size,
            "mean_area_km2": round(float(grid.areas_km2.mean()), 6),
            "bbox": [round(v, 6) for v in grid.bbox],
        }
    return out
