"""STAC scene enumeration for a campaign.

Vendored from h3lab@26c042f (``src/h3lab/stac.py``) and adapted so the AOI,
platforms, cloud thresholds and collection all come from the campaign.

Planetary Computer signs asset URLs with SAS tokens that expire after about
45 minutes.  Scene records therefore store *unsigned* blob URLs; every read is
signed at read time through :data:`SIGNER`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import rasterio
from shapely.geometry import box
from shapely.ops import transform
from shapely.validation import make_valid

from .campaign import Campaign
from .constants import BAND_ASSETS, BANDS, QA_PIXEL_ASSETS

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    VSI_CACHE="TRUE",
    VSI_CACHE_SIZE="134217728",
    GDAL_INGESTED_BYTES_AT_OPEN="32768",
)


@dataclass
class Scene:
    item_id: str
    datetime: object
    platform: str
    cloud_cover: float
    wrs_path: str
    wrs_row: str
    assets: dict
    qa_href: str
    bounds: tuple
    crs: str
    coverage: float

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "datetime": self.datetime.isoformat() if self.datetime else None,
            "platform": self.platform,
            "cloud_cover": self.cloud_cover,
            "wrs_path": self.wrs_path,
            "wrs_row": self.wrs_row,
            "assets": dict(self.assets),
            "qa_href": self.qa_href,
            "bounds": list(self.bounds),
            "crs": self.crs,
            "coverage": self.coverage,
        }

    @staticmethod
    def from_dict(d: dict) -> "Scene":
        dt = d.get("datetime")
        if dt:
            dt = datetime.fromisoformat(dt)
        return Scene(
            item_id=d["item_id"],
            datetime=dt,
            platform=d["platform"],
            cloud_cover=float(d["cloud_cover"]),
            wrs_path=d["wrs_path"],
            wrs_row=d["wrs_row"],
            assets=dict(d["assets"]),
            qa_href=d["qa_href"],
            bounds=tuple(d["bounds"]),
            crs=d["crs"],
            coverage=float(d["coverage"]),
        )


class UrlSigner:
    TTL = 1200.0

    def __init__(self, ttl: float | None = None):
        self.ttl = self.TTL if ttl is None else ttl
        self._cache: dict = {}

    def sign(self, url: str) -> str:
        now = time.time()
        hit = self._cache.get(url)
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        signed = url
        try:
            import planetary_computer as pc

            signed = pc.sign_url(url)
        except Exception:  # noqa: BLE001 - signing is best effort
            pass
        self._cache[url] = (now, signed)
        return signed


SIGNER = UrlSigner()


def unsigned_url(href: str) -> str:
    if ".blob.core.windows.net" not in href:
        return href
    return href.split("?", 1)[0]


def _client(campaign: Campaign):
    import planetary_computer as pc
    from pystac_client import Client

    return Client.open(campaign.stac_endpoint, modifier=pc.sign_inplace)


def _asset_for(item, names):
    for name in names:
        if name in item.assets:
            return item.assets[name].href
    return None


def _platform_matches(value: str, wanted: tuple[str, ...]) -> bool:
    value = (value or "").lower().replace(" ", "-")
    return any(value == w.lower().replace(" ", "-") for w in wanted)


def _scene_intersects_aoi(item, campaign: Campaign) -> Scene | None:
    """Open a scene header and test true intersection with the campaign AOI."""
    from pyproj import Transformer

    red = _asset_for(item, ("red",))
    if red is None:
        return None
    try:
        with rasterio.open(SIGNER.sign(red), **GDAL_ENV) as src:
            bounds = src.bounds
            crs = str(src.crs)
    except Exception:
        return None

    try:
        tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        aoi_scene = transform(tr.transform, campaign.aoi)
        aoi_scene = make_valid(aoi_scene)
    except Exception:
        return None
    if aoi_scene.is_empty or aoi_scene.area <= 0:
        return None

    scene_poly = box(bounds.left, bounds.bottom, bounds.right, bounds.top)
    try:
        inter = scene_poly.intersection(aoi_scene)
    except Exception:
        return None
    if inter.is_empty:
        return None
    frac = inter.area / aoi_scene.area
    if frac <= 0.0:
        return None

    assets = {}
    for band, names in BAND_ASSETS.items():
        href = _asset_for(item, names)
        if href is None:
            return None
        assets[band] = unsigned_url(href)
    qa_href = _asset_for(item, QA_PIXEL_ASSETS)
    if qa_href is None:
        return None

    dt = item.datetime
    return Scene(
        item_id=item.id,
        datetime=dt,
        platform=item.properties.get("platform", "?"),
        cloud_cover=float(item.properties.get("eo:cloud_cover") or 0.0),
        wrs_path=str(item.properties.get("landsat:wrs_path", "?")),
        wrs_row=str(item.properties.get("landsat:wrs_row", "?")),
        assets=assets,
        qa_href=unsigned_url(qa_href),
        bounds=tuple(bounds),
        crs=crs,
        coverage=float(frac),
    )


def find_scenes_for_bin(campaign: Campaign, bin_num: int,
                        platforms: tuple[str, ...] | None = None) -> list[Scene]:
    """Enumerate every scene intersecting the AOI inside one 16-day bin."""
    platforms = tuple(platforms or campaign.platforms)
    start = campaign.bin_start(bin_num).strftime("%Y-%m-%dT00:00:00Z")
    end = campaign.bin_end(bin_num).strftime("%Y-%m-%dT23:59:59Z")

    cat = _client(campaign)
    search = cat.search(
        collections=[campaign.collection],
        bbox=campaign.bbox,
        datetime=f"{start}/{end}",
        query={"eo:cloud_cover": {"lte": campaign.max_scene_cloud}},
    )
    scenes = []
    for item in search.items():
        if not _platform_matches(item.properties.get("platform", ""),
                                 platforms):
            continue
        scene = _scene_intersects_aoi(item, campaign)
        if scene is not None:
            scenes.append(scene)

    # Deterministic order, independent of the STAC server's response order.
    scenes.sort(key=lambda s: (
        s.datetime.isoformat() if s.datetime else "",
        s.platform,
        s.item_id,
    ))
    return scenes


def scenes_payload(campaign: Campaign, year: int,
                   scenes_by_bin: dict[int, list[Scene]]) -> dict:
    return {
        "version": 1,
        "campaign": campaign.slug,
        "campaign_hash": campaign.hash,
        "aoi_hash": campaign.aoi_hash,
        "year": int(year),
        "bbox": campaign.bbox,
        "platforms": list(campaign.platforms),
        "bins": {
            str(b): [s.to_dict() for s in v]
            for b, v in sorted(scenes_by_bin.items())
        },
    }


def scenes_from_payload(payload: dict) -> dict[int, list[Scene]]:
    return {
        int(b): [Scene.from_dict(d) for d in v]
        for b, v in payload.get("bins", {}).items()
    }
