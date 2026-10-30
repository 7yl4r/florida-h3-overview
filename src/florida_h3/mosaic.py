"""Per-bin campaign mosaic builder.

Vendored from h3lab@26c042f (``src/h3lab/mosaic.py``).  The science is
unchanged: a per-pixel temporal median over the scenes that cover each pixel,
on the campaign's analysis grid, stored as raw uint16 digital numbers.  The
changes are campaign-driven CRS/pixel size/AOI masking and a smaller default
block so the wide Florida Albers grid stays inside a bounded memory budget.

Remote reads are re-signed on demand because Planetary Computer SAS tokens
expire after about 45 minutes.  Outputs are written to a temporary name and
renamed, so an interrupted build cannot be mistaken for a cache hit.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window

from .campaign import Campaign
from .grid import analysis_grid, build_aoi_mask
from .stac import SIGNER

GDAL_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    VSI_CACHE="TRUE",
    VSI_CACHE_SIZE="134217728",
    GDAL_INGESTED_BYTES_AT_OPEN="32768",
    GDAL_HTTP_TIMEOUT="180",
    GDAL_HTTP_MAX_RETRY="2",
    GDAL_HTTP_RETRY_DELAY="2",
)

QA_BAD_MASK = (
    (1 << 0)   # fill
    | (1 << 1)   # dilated cloud
    | (1 << 2)   # cirrus
    | (1 << 3)   # cloud
    | (1 << 4)   # cloud shadow
)
QA_SNOWICE_SHIFT = 5
QA_SNOWICE_MASK = 0b11
QA_SNOWICE_BAD = (0b01, 0b11)

BLOCK = 512
REOPEN_AFTER = 1200.0


class _SceneReader:
    def __init__(self, scene, transform, shape, crs):
        self.scene = scene
        self.transform = transform
        self.shape = shape
        self.crs = crs
        self.grid_window = None
        self._vrt_cache: dict = {}
        self._opened_at = 0.0
        self._qa_mask = None

    def _open(self, key, href):
        src = rasterio.open(SIGNER.sign(href), **GDAL_ENV)
        vrt = WarpedVRT(
            src,
            crs=CRS.from_string(self.crs),
            transform=self.transform,
            width=self.shape[1],
            height=self.shape[0],
            resampling=Resampling.nearest,
            src_nodata=src.nodata if src.nodata is not None else 0,
            nodata=0,
        )
        self._vrt_cache[key] = (src, vrt)
        self._opened_at = time.time()

    def close(self):
        for src, vrt in self._vrt_cache.values():
            for obj in (vrt, src):
                try:
                    obj.close()
                except Exception:
                    pass
        self._vrt_cache.clear()

    def _vrt(self, key, href):
        entry = self._vrt_cache.get(key)
        if entry is not None and time.time() - self._opened_at < REOPEN_AFTER:
            return entry[1]
        if entry is not None:
            self.close()
        self._open(key, href)
        return self._vrt_cache[key][1]

    def read_band(self, band: str, col0: int, row0: int, col1: int, row1: int):
        href = self.scene.assets.get(band)
        if href is None:
            return None
        window = Window(col0, row0, col1 - col0, row1 - row0)
        for attempt in range(2):
            try:
                vrt = self._vrt(f"b:{band}", href)
                return vrt.read(1, window=window)
            except Exception as exc:  # noqa: BLE001
                self.close()
                if attempt == 1:
                    raise RuntimeError(
                        f"read failed {self.scene.item_id} {band}: {exc}"
                    ) from exc
        return None

    def read_qa_mask(self):
        if self._qa_mask is not None:
            return self._qa_mask
        href = self.scene.qa_href
        if href is None:
            return None
        col0, row0, col1, row1 = self.grid_window
        window = Window(col0, row0, col1 - col0, row1 - row0)
        for attempt in range(2):
            try:
                vrt = self._vrt("qa", href)
                qa = vrt.read(1, window=window).astype(np.uint32)
                break
            except Exception as exc:  # noqa: BLE001
                self.close()
                if attempt == 1:
                    raise RuntimeError(
                        f"read failed {self.scene.item_id} qa_pixel: {exc}"
                    ) from exc
        bad = (qa & QA_BAD_MASK) != 0
        snowice = (qa >> QA_SNOWICE_SHIFT) & QA_SNOWICE_MASK
        bad = bad | np.isin(snowice, QA_SNOWICE_BAD)
        self._qa_mask = ~bad
        return self._qa_mask


def _grid_window(scene, transform, shape, crs):
    left, bottom, right, top = scene.bounds
    if scene.crs == crs:
        minx, maxx, miny, maxy = left, right, bottom, top
    else:
        from pyproj import Transformer

        tr = Transformer.from_crs(scene.crs, crs, always_xy=True)
        xs, ys = tr.transform([left, right, left, right],
                              [bottom, bottom, top, top])
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)

    inv = ~transform
    c0, r_top = inv * (minx, maxy)
    c1, r_bottom = inv * (maxx, miny)
    col0 = max(int(np.floor(c0)), 0)
    row0 = max(int(np.floor(r_top)), 0)
    col1 = min(int(np.ceil(c1)), shape[1])
    row1 = min(int(np.ceil(r_bottom)), shape[0])
    if col1 <= col0 or row1 <= row0:
        return None
    return (col0, row0, col1, row1)


def _median_stack(stack: np.ndarray) -> np.ndarray:
    finite = np.isfinite(stack)
    k = np.count_nonzero(finite, axis=-1)
    srt = np.sort(stack, axis=-1)
    lo_idx = np.maximum((k - 1) // 2, 0)
    hi_idx = k // 2
    lo = np.take_along_axis(srt, lo_idx[..., None], axis=-1)[..., 0]
    hi = np.take_along_axis(srt, hi_idx[..., None], axis=-1)[..., 0]
    return 0.5 * (lo + hi)


def _open_out(path, transform, w, h, dtype, crs, nodata=0):
    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 1,
        "dtype": dtype,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "predictor": 2,
        "zlevel": 6,
    }
    return rasterio.open(path, "w", **profile)


def _read_footprint(reader, band: str):
    col0, row0, col1, row1 = reader.grid_window
    raw = reader.read_band(band, col0, row0, col1, row1)
    if raw is None:
        return None
    return col0, col1, raw


def build_mosaic_bin(campaign: Campaign, bin_num: int, scenes, out_dir,
                     block: int = BLOCK, workers: int = 12, log=print):
    """Build one bin's mosaics. Returns a dict of output paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    target = analysis_grid(campaign, campaign.overview_res)
    transform, (h, w), _ = target
    aoi_mask = build_aoi_mask(campaign, target)

    readers = []
    for scene in scenes:
        reader = _SceneReader(scene, transform, (h, w), campaign.crs)
        win = _grid_window(scene, transform, (h, w), campaign.crs)
        if win is None:
            continue
        reader.grid_window = win
        readers.append(reader)
    if not readers:
        raise ValueError(f"bin {bin_num}: no scene intersects the analysis grid")

    nobs = np.zeros((h, w), dtype=np.uint8)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        qa_masks = list(ex.map(lambda r: r.read_qa_mask(), readers))
    for reader, mask in zip(readers, qa_masks):
        if mask is None:
            continue
        col0, row0, col1, row1 = reader.grid_window
        nobs[row0:row1, col0:col1] += mask.astype(np.uint8)

    paths = {}
    try:
        for band in campaign.bands:
            out_path = out_dir / f"mosaic_bin{bin_num}_{band}.tif"
            tmp_path = out_dir / f".mosaic_bin{bin_num}_{band}.part.tif"
            dst = _open_out(tmp_path, transform, w, h, "uint16",
                            campaign.crs)
            t_read = t_cpu = 0.0
            try:
                t0 = time.time()
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    parts = list(ex.map(lambda r: _read_footprint(r, band),
                                        readers))
                t_read = time.time() - t0
                t0 = time.time()
                for row0 in range(0, h, block):
                    row1 = min(row0 + block, h)
                    stack = np.full((row1 - row0, w, len(readers)), np.nan,
                                    dtype=np.float32)
                    for i, part in enumerate(parts):
                        if part is None:
                            continue
                        col0, col1, raw = part
                        gcol0, grow0, gcol1, grow1 = readers[i].grid_window
                        ir0 = max(row0, grow0)
                        ir1 = min(row1, grow1)
                        if ir0 >= ir1:
                            continue
                        sub = raw[ir0 - grow0:ir1 - grow0, :].astype(np.float32)
                        sub[sub <= 0] = np.nan
                        mask = qa_masks[i][ir0 - grow0:ir1 - grow0, :]
                        sub[~mask] = np.nan
                        stack[ir0 - row0:ir1 - row0, col0:col1, i] = sub
                        del sub
                    med = _median_stack(stack)
                    out = np.where(np.isfinite(med), med, 0.0)
                    out[~aoi_mask[row0:row1, :]] = 0.0
                    dst.write(np.rint(out).astype(np.uint16), 1,
                              window=Window(0, row0, w, row1 - row0))
                    del stack, med, out
                t_cpu = time.time() - t0
            finally:
                dst.close()
            os.replace(tmp_path, out_path)
            paths[band] = str(out_path)
            if log is not None:
                log(f"    {band}: read {t_read:.0f}s, median+write {t_cpu:.0f}s")

        nobs[~aoi_mask] = 0
        nobs_path = out_dir / f"nobs_bin{bin_num}.tif"
        nobs_tmp = out_dir / f".nobs_bin{bin_num}.part.tif"
        dst = _open_out(nobs_tmp, transform, w, h, "uint8", campaign.crs)
        try:
            dst.write(nobs, 1)
        finally:
            dst.close()
        os.replace(nobs_tmp, nobs_path)
        paths["nobs"] = str(nobs_path)
    finally:
        for reader in readers:
            reader.close()

    return paths


def read_mosaic_band(path: str) -> np.ndarray:
    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float32)
    arr[arr <= 0] = np.nan
    return arr


def read_mosaic_nobs(path: str) -> np.ndarray:
    with rasterio.open(path) as src:
        arr = src.read(1).astype(np.float32)
    arr[arr <= 0] = np.nan
    return arr
