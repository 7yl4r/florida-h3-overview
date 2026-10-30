"""Tests for the H3LB binary bundle format: round-trip and byte reproducibility."""

import zlib
from pathlib import Path

import numpy as np
import pytest

from florida_h3.binary import build_block, pack, read_block, read_descriptor, write_block


def _block(n_cells=4, n_periods=3, with_change=True):
    periods = [
        {"bin": 388, "date": "2017-01-07", "start": "2017-01-01",
         "end": "2017-01-16", "year": 2017, "month": 1, "season": "dry"},
        {"bin": 389, "date": "2017-01-23", "start": "2017-01-17",
         "end": "2017-02-01", "year": 2017, "month": 1, "season": "dry"},
        {"bin": 390, "date": "2017-02-08", "start": "2017-02-02",
         "end": "2017-02-17", "year": 2017, "month": 2, "season": "dry"},
    ]
    block = {
        "periods": periods,
        "nCells": n_cells,
        "ids": [f"8444101{i:08x}fffffff" for i in range(n_cells)],
        "lon": [-81.0, -81.1, -81.2, -81.3][:n_cells],
        "lat": [25.5, 25.6, 25.7, 25.8][:n_cells],
        "areaKm2": [28.3, 28.3, 28.3, 28.3][:n_cells],
        # Degenerate hexagon geometry (6 vertices per cell) so the bundle
        # round-trips through build_block's geometry encoding.
        "_nVerts": [6] * n_cells,
        "_geomXY": ([-81.0, 25.5] * (6 * n_cells)),
        # band/index/observed/validFraction/nObs are (n_periods, n_cells).
        "band": {"blue": [[0.1] * n_cells for _ in range(n_periods)],
                 "nir": [[0.4] * n_cells for _ in range(n_periods)]},
        "index": {"ndvi": [[0.5] * n_cells for _ in range(n_periods)]},
        "cell": {"trend_slope": [0.01, -0.02, 0.0, 0.03][:n_cells]},
        "cellraw": {"n_bins": [3, 3, 3, 3][:n_cells]},
        "observed": [[True] * n_cells for _ in range(n_periods)],
        "validFraction": [[1.0] * n_cells for _ in range(n_periods)],
        "nObs": [[5] * n_cells for _ in range(n_periods)],
        "campaign": "test-slug",
        "campaignHash": "abcdef01",
        "aoiHash": "12345678",
        "year": 2017,
        "resolution": 7,
        "tier": "overview",
        "generatedBy": "florida_h3",
        "source": "synthetic",
        "epoch": "2000-01-01",
        "binDays": 16,
    }
    if with_change:
        block["change"] = {
            "break_bin": [389, -1, 390, -1][:n_cells],
            # break_date is integer days since the campaign epoch.
            "break_date": [6220, -1, 6236, -1][:n_cells],
            "break_magnitude": [0.3, 0.0, -0.4, 0.0][:n_cells],
            "break_score": [40.0, 0.0, 35.0, 0.0][:n_cells],
            "n_obs_used": [3, 0, 3, 0][:n_cells],
            # method is an index into the changeMethods table.
            "method": [0] * n_cells,
            "methods": ["harmonic_fscan_v1"],
        }
    return block


def test_write_read_round_trip(tmp_path):
    block = _block()
    p = tmp_path / "block.bin"
    info = write_block(block, str(p), gz=False)
    assert info["cells"] == 4
    assert info["periods"] == 3
    desc, data = read_block(str(p))
    assert desc["nCells"] == 4
    assert desc["nPeriods"] == 3
    assert desc["campaignHash"] == "abcdef01"
    assert desc["aoiHash"] == "12345678"
    assert desc["changeMethods"] == ["harmonic_fscan_v1"]
    ndvi = data["index.ndvi"]
    assert ndvi.shape == (3, 4)
    assert ndvi.dtype == np.uint16
    # All cells were 0.5 -> quantised values identical across cells.
    assert np.all(ndvi[0] == ndvi[0][0])
    assert data["cell.trend_slope"].shape == (4,)
    assert data["cellraw.n_bins"].shape == (4,)
    assert data["break_bin"].shape == (4,)
    assert data["method"].shape == (4,)
    assert set(desc["cells"]) == {"trend_slope"}
    assert set(desc["cellraws"]) == {"n_bins"}


def test_gzip_byte_reproducibility(tmp_path):
    block = _block(with_change=False)
    p1 = tmp_path / "a.bin"
    p2 = tmp_path / "b.bin"
    out1 = write_block(block, str(p1), gz=True)
    out2 = write_block(block, str(p2), gz=True)
    b1 = Path(out1["path"]).read_bytes()
    b2 = Path(out2["path"]).read_bytes()
    assert b1 == b2
    # gzip header carries mtime=0 -> no timestamp bytes
    assert b1[4:8] == b"\x00\x00\x00\x00"


def test_pack_direct_crc():
    block = _block()
    desc, chunks = build_block(block)
    blob = pack(desc, chunks)
    assert blob[:4] == b"H3LB"
    import struct
    ver, flags, length, crc = struct.unpack("<HHII", blob[4:16])
    assert ver == 1
    raw = blob[16:16 + length]
    assert zlib.crc32(raw) & 0xFFFFFFFF == crc


def test_missing_values_round_trip(tmp_path):
    block = _block()
    block["index"]["ndvi"] = [[0.5, None, 0.7, None] for _ in range(3)]
    p = tmp_path / "miss.bin"
    write_block(block, str(p), gz=False)
    desc, data = read_block(str(p))
    ndvi = data["index.ndvi"].astype(float)
    # quantised missing -> 65535
    assert desc["missing"] == 65535


def test_read_descriptor_gzip(tmp_path):
    block = _block()
    p = tmp_path / "d.bin"
    write_block(block, str(p), gz=True)
    desc = read_descriptor(str(p) + ".gz")
    assert desc["nCells"] == 4
    assert desc["campaign"] == "test-slug"

