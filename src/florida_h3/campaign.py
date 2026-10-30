"""Campaign configuration: the first-class input to every Florida build.

A campaign is a JSON file plus a GeoJSON AOI.  Nothing in the pipeline reads a
hard-coded bounding box or a module-level resolution constant.  That is the
main structural difference from the frozen h3lab pilot: the same code can build
the southern-Florida/EEZ campaign, a smaller demo campaign, or a future
statewide campaign without edits.

Paths in the campaign are resolved in a predictable way:

* ``aoi.path`` is relative to the campaign JSON file;
* ``output.root`` is relative to the repository root (the parent of
  ``campaigns/``).

The campaign carries a stable hash of its normalised AOI geometry.  The catalog
and every year manifest repeat that hash, so a product can always be checked
against the polygon it was built from.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from shapely import set_precision, to_wkb
from shapely.geometry import mapping, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid

from . import bins as binmod
from .constants import BAND_INFO, BANDS, INDEX_SPECS, INDICES


SCHEMA_VERSION = 1


class CampaignError(ValueError):
    """Raised when a campaign file is missing or internally inconsistent."""


def _repo_root(campaign_path: Path) -> Path:
    """The repository root, i.e. the parent of the ``campaigns`` directory."""
    for parent in campaign_path.parents:
        if (parent / "campaigns").is_dir() or (parent / ".git").exists():
            return parent
    return campaign_path.parent.parent


def _geometry_from_geojson(path: Path):
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    kind = data.get("type")
    if kind == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data.get("features", [])]
        if not geoms:
            raise CampaignError(f"{path}: empty FeatureCollection")
        geom = unary_union(geoms)
    elif kind == "Feature":
        geom = shape(data["geometry"])
    else:
        geom = shape(data)
    geom = make_valid(geom).buffer(0)
    if geom.is_empty:
        raise CampaignError(f"{path}: empty AOI geometry")
    return geom


def _project(geom, crs: str):
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    return transform(tr.transform, geom)


def _unproject(geom, crs: str):
    from pyproj import Transformer

    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    return transform(tr.transform, geom)


def _geodesic_km2(geom) -> float:
    from pyproj import Geod

    area, _ = Geod(ellps="WGS84").geometry_area_perimeter(geom)
    return abs(area) / 1e6


@dataclass(frozen=True)
class Campaign:
    """A validated campaign configuration."""

    path: Path
    root: Path
    raw: dict
    aoi: Any
    aoi_hash: str
    area_km2: float

    # -- construction ------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike) -> "Campaign":
        path = Path(path).resolve()
        if not path.exists():
            raise CampaignError(f"campaign not found: {path}")
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        cls._validate_raw(raw, path)

        aoi_path = (path.parent / raw["aoi"]["path"]).resolve()
        if not aoi_path.exists():
            raise CampaignError(f"{path}: AOI not found: {aoi_path}")
        aoi = _geometry_from_geojson(aoi_path)

        crs = raw["analysis"]["crs"]
        buffer_km = float(raw["aoi"].get("buffer_km", 0) or 0)
        if buffer_km:
            buffered = _project(aoi, crs).buffer(buffer_km * 1000.0)
            aoi = make_valid(_unproject(buffered, crs)).buffer(0)
        aoi = aoi.simplify(float(raw["aoi"].get("simplify_deg", 0) or 0),
                           preserve_topology=True)
        if not aoi.is_valid:
            aoi = make_valid(aoi)
        if aoi.is_empty:
            raise CampaignError(f"{path}: AOI is empty after buffering")

        root = _repo_root(path)
        # Snap to a micrometre before hashing so a rebuild that only moves a
        # vertex by floating-point noise still produces the same AOI identity.
        stable_aoi = set_precision(aoi, 1e-6)
        aoi_hash = hashlib.sha256(
            to_wkb(stable_aoi, output_dimension=2)
        ).hexdigest()[:16]
        return cls(
            path=path,
            root=root,
            raw=raw,
            aoi=aoi,
            aoi_hash=aoi_hash,
            area_km2=round(_geodesic_km2(aoi), 2),
        )

    @staticmethod
    def _validate_raw(raw: dict, path: Path) -> None:
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise CampaignError(
                f"{path}: unsupported schema_version "
                f"{raw.get('schema_version')!r}; expected {SCHEMA_VERSION}"
            )
        for key in ("name", "slug", "aoi", "analysis", "resolutions",
                    "years", "temporal", "sources", "output"):
            if key not in raw:
                raise CampaignError(f"{path}: missing required key {key!r}")
        if "path" not in raw["aoi"]:
            raise CampaignError(f"{path}: aoi.path is required")
        if "crs" not in raw["analysis"]:
            raise CampaignError(f"{path}: analysis.crs is required")
        if "overview" not in raw["resolutions"]:
            raise CampaignError(f"{path}: resolutions.overview is required")
        if "start" not in raw["years"] or "end" not in raw["years"]:
            raise CampaignError(f"{path}: years.start and years.end are required")
        if raw["years"]["start"] > raw["years"]["end"]:
            raise CampaignError(f"{path}: years.start is after years.end")
        if int(raw["temporal"].get("bin_days", 0)) <= 0:
            raise CampaignError(f"{path}: temporal.bin_days must be positive")
        try:
            date.fromisoformat(raw["temporal"].get("epoch", ""))
        except ValueError as exc:
            raise CampaignError(f"{path}: invalid temporal.epoch") from exc

    # -- identity ----------------------------------------------------------
    @property
    def name(self) -> str:
        return str(self.raw["name"])

    @property
    def slug(self) -> str:
        return str(self.raw["slug"])

    @property
    def description(self) -> str:
        return str(self.raw.get("description", ""))

    @property
    def crs(self) -> str:
        return str(self.raw["analysis"]["crs"])

    @property
    def pixel_size(self) -> int:
        return int(self.raw["analysis"]["pixel_size"])

    @property
    def detail_pixel_size(self) -> int:
        return int(self.raw["analysis"].get("detail_pixel_size",
                                            self.pixel_size))

    @property
    def overview_res(self) -> int:
        return int(self.raw["resolutions"]["overview"])

    @property
    def detail_res(self) -> int | None:
        value = self.raw["resolutions"].get("detail")
        return int(value) if value is not None else None

    @property
    def region_res(self) -> int:
        return int(self.raw["resolutions"].get("region_res", 4))

    @property
    def resolutions(self) -> tuple[int, ...]:
        values = [self.overview_res]
        if self.detail_res is not None:
            values.append(self.detail_res)
        return tuple(sorted(set(values)))

    def pixel_size_for(self, resolution: int) -> int:
        if self.detail_res is not None and resolution >= self.detail_res:
            return self.detail_pixel_size
        return self.pixel_size

    @property
    def year_start(self) -> int:
        return int(self.raw["years"]["start"])

    @property
    def year_end(self) -> int:
        return int(self.raw["years"]["end"])

    @property
    def build_years(self) -> list[int]:
        values = self.raw["years"].get("build")
        if values:
            return [int(y) for y in values]
        return list(range(self.year_start, self.year_end + 1))

    @property
    def epoch(self) -> date:
        return date.fromisoformat(self.raw["temporal"]["epoch"])

    @property
    def bin_days(self) -> int:
        return int(self.raw["temporal"]["bin_days"])

    @property
    def platforms(self) -> tuple[str, ...]:
        return tuple(self.raw["sources"]["platforms"])

    @property
    def stac_endpoint(self) -> str:
        return str(self.raw["sources"]["stac_endpoint"])

    @property
    def collection(self) -> str:
        return str(self.raw["sources"]["collection"])

    @property
    def min_valid_fraction(self) -> float:
        return float(self.raw["processing"].get("min_valid_fraction", 0.25))

    @property
    def max_scene_cloud(self) -> float:
        return float(self.raw["processing"].get("max_scene_cloud", 85.0))

    @property
    def max_pixel_cloud(self) -> float:
        return float(self.raw["processing"].get("max_pixel_cloud", 90.0))

    @property
    def max_scenes_per_composite(self) -> int:
        return int(self.raw["processing"].get("max_scenes_per_composite", 12))

    @property
    def composite_stat(self) -> str:
        return str(self.raw["processing"].get("composite_stat", "median"))

    @property
    def events(self) -> list[dict]:
        return list(self.raw.get("events", []))

    @property
    def bands(self) -> tuple[str, ...]:
        return BANDS

    @property
    def indices(self) -> tuple[str, ...]:
        return INDICES

    @property
    def index_specs(self) -> dict:
        return {k: dict(v) for k, v in INDEX_SPECS.items()}

    @property
    def band_info(self) -> dict:
        return {k: dict(v) for k, v in BAND_INFO.items() if k in BANDS}

    # -- geometry ----------------------------------------------------------
    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(minx, miny, maxx, maxy) in EPSG:4326."""
        return tuple(float(v) for v in self.aoi.bounds)

    @property
    def bbox(self) -> list[float]:
        return list(self.bounds)

    @property
    def geometry(self) -> dict:
        return mapping(self.aoi)

    # -- temporal ----------------------------------------------------------
    def bins_for_year(self, year: int) -> list[int]:
        return binmod.bins_for_year(year, self.epoch, self.bin_days)

    def bin_start(self, b: int) -> date:
        return binmod.bin_start(b, self.epoch, self.bin_days)

    def bin_end(self, b: int) -> date:
        return binmod.bin_end(b, self.epoch, self.bin_days)

    def bin_centre(self, b: int) -> date:
        return binmod.bin_centre(b, self.epoch, self.bin_days)

    # -- paths -------------------------------------------------------------
    @property
    def data_root(self) -> Path:
        return (self.root / self.raw["output"]["root"]).resolve()

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "cache"

    @property
    def product_dir(self) -> Path:
        return self.data_root / "product"

    @property
    def derived_dir(self) -> Path:
        return self.product_dir / "derived"

    @property
    def catalog_path(self) -> Path:
        return self.data_root / "catalog.json"

    def year_cache(self, year: int) -> Path:
        return self.cache_dir / str(year)

    def year_product(self, year: int) -> Path:
        return self.product_dir / str(year)

    def stac_cache_path(self, year: int) -> Path:
        return self.year_cache(year) / "stac" / f"{year}.json"

    def mosaic_dir(self, year: int, b: int) -> Path:
        return self.year_cache(year) / "mosaic" / f"bin{b:03d}"

    def h3_path(self, year: int, resolution: int, b: int) -> Path:
        return self.year_cache(year) / "h3" / f"res{resolution}" / f"bin{b:03d}.npz"

    def grid_cache_path(self) -> Path:
        return self.cache_dir / "grid" / "grid.json"

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict:
        """A stable, JSON-safe description used by manifests and the catalog."""
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "aoi": {
                "path": self.raw["aoi"]["path"],
                "hash": self.aoi_hash,
                "area_km2": self.area_km2,
                "bbox": [round(v, 6) for v in self.bounds],
                "boundary_lat": self.raw["aoi"].get("boundary_lat"),
                "buffer_km": self.raw["aoi"].get("buffer_km", 0),
            },
            "analysis": {
                "crs": self.crs,
                "pixel_size": self.pixel_size,
                "detail_pixel_size": self.detail_pixel_size,
            },
            "resolutions": {
                "overview": self.overview_res,
                "detail": self.detail_res,
                "region_res": self.region_res,
            },
            "years": {
                "start": self.year_start,
                "end": self.year_end,
                "build": self.build_years,
            },
            "temporal": {
                "epoch": self.epoch.isoformat(),
                "bin_days": self.bin_days,
            },
            "sources": {
                "stac_endpoint": self.stac_endpoint,
                "collection": self.collection,
                "platforms": list(self.platforms),
            },
            "processing": {
                "min_valid_fraction": self.min_valid_fraction,
                "max_scene_cloud": self.max_scene_cloud,
                "max_pixel_cloud": self.max_pixel_cloud,
                "max_scenes_per_composite": self.max_scenes_per_composite,
                "composite_stat": self.composite_stat,
            },
            "bands": list(self.bands),
            "indices": list(self.indices),
            "index_specs": self.index_specs,
            "band_info": self.band_info,
            "events": self.events,
        }

    @property
    def hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def summary(self) -> str:
        return (
            f"{self.name} ({self.slug}) · {self.area_km2:,.0f} km² · "
            f"{self.crs} @ {self.pixel_size} m · res {self.resolutions} · "
            f"{self.year_start}-{self.year_end}"
        )
