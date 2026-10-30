"""Compact binary bundle format for the viewer.

Vendored from h3lab@26c042f (``src/h3lab/binary.py``), with campaign/year/
resolution metadata added to the descriptor.  The layout is unchanged:

    0   4  magic  b"H3LB"
    4   2  uint16 format version
    6   2  uint16 flags
    8   4  uint32 descriptor length
    12  4  uint32 CRC32 of the descriptor
    16  ...   descriptor JSON, padded to a 16-byte boundary
    ...      payload arrays, back to back, in descriptor order

Floats are quantised to uint16 against each field's own observed range.
``MISSING`` (65535) means "no value".  Hexagon outlines are baked into the
bundle, so opening a block is a header parse plus a copy rather than tens of
thousands of H3 calls.

Gzip uses ``mtime=0`` so re-exporting unchanged data is byte-identical.
"""

from __future__ import annotations

import gzip
import json
import struct
import zlib

import numpy as np

MAGIC = b"H3LB"
VERSION = 1
HEADER = 16
ALIGN = 16
MISSING = 65535


def _flatten(values: list, n_cells: int) -> np.ndarray:
    arr = np.empty((len(values), n_cells), dtype=np.float64)
    for i, row in enumerate(values):
        if len(row) != n_cells:
            raise ValueError(
                f"row {i} has {len(row)} values but the block has {n_cells} cells"
            )
        arr[i] = [np.nan if v is None else float(v) for v in row]
    return arr


def _flat_cells(values, n_cells: int) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64).ravel()
    if arr.size != n_cells:
        raise ValueError(
            f"field has {arr.size} values but the block has {n_cells} cells"
        )
    return arr


def _quantise(arr: np.ndarray) -> tuple[np.ndarray, float, float]:
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return np.full(arr.shape, MISSING, dtype=np.uint16), 0.0, 0.0
    lo = float(finite.min())
    hi = float(finite.max())
    if hi <= lo:
        hi = lo + 1.0
    scale = (hi - lo) / (MISSING - 1)
    q = np.rint((np.where(np.isfinite(arr), arr, lo) - lo) / scale)
    q = np.clip(q, 0, MISSING - 1).astype(np.uint16)
    q[~np.isfinite(arr)] = MISSING
    return q, lo, scale


def build_block(block: dict) -> tuple[dict, list[bytes]]:
    n_cells = block["nCells"]
    n_periods = len(block["periods"])
    shape = [n_periods, n_cells]
    fields: list[dict] = []
    chunks: list[bytes] = []

    float_groups = (
        ("band", block.get("band") or {}),
        ("index", block.get("index") or {}),
    )
    for group_name, group in float_groups:
        for name, values in group.items():
            arr = _flatten(values, n_cells)
            q, lo, scale = _quantise(arr)
            fields.append({
                "name": f"{group_name}.{name}",
                "dtype": "u16",
                "shape": shape,
                "min": lo,
                "scale": scale,
            })
            chunks.append(q.tobytes())

    cell_fields = []
    for name, values in (block.get("cell") or {}).items():
        arr = _flat_cells(values, n_cells)
        q, lo, scale = _quantise(arr)
        fields.append({"name": f"cell.{name}", "dtype": "u16",
                       "shape": [n_cells], "min": lo, "scale": scale})
        chunks.append(q.tobytes())
        cell_fields.append(name)

    cellraw_fields = []
    for name, values in (block.get("cellraw") or {}).items():
        arr = np.asarray(values, dtype=np.int64).ravel()
        if arr.size != n_cells:
            raise ValueError(
                f"cellraw field {name!r} has {arr.size} values but the block "
                f"has {n_cells} cells"
            )
        fields.append({"name": f"cellraw.{name}", "dtype": "u16r",
                       "shape": [n_cells], "missing": MISSING})
        chunks.append(np.where((arr < 0) | (arr >= MISSING), MISSING, arr)
                      .astype(np.uint16).tobytes())
        cellraw_fields.append(name)

    vf = _flatten(block["validFraction"], n_cells)
    q, lo, scale = _quantise(vf)
    fields.append({"name": "validFraction", "dtype": "u16", "shape": shape,
                   "min": lo, "scale": scale})
    chunks.append(q.tobytes())

    observed = np.asarray(block["observed"], dtype=bool).astype(np.uint8)
    fields.append({"name": "observed", "dtype": "u8", "shape": shape})
    chunks.append(observed.tobytes())

    n_obs = np.asarray(block["nObs"], dtype=np.uint16)
    fields.append({"name": "nObs", "dtype": "u16r", "shape": shape})
    chunks.append(n_obs.tobytes())

    verts_per_cell = np.asarray(block["_nVerts"], dtype=np.uint16)
    flat = np.asarray(block["_geomXY"], dtype=np.float64)
    nverts_total = int(verts_per_cell.sum())
    coords = flat[: nverts_total * 2].reshape(-1, 2)
    lon_lo, lon_hi = float(coords[:, 0].min()), float(coords[:, 0].max())
    lat_lo, lat_hi = float(coords[:, 1].min()), float(coords[:, 1].max())
    gscale_x = (lon_hi - lon_lo) / (MISSING - 1) or 1.0
    gscale_y = (lat_hi - lat_lo) / (MISSING - 1) or 1.0
    gq = np.empty(nverts_total * 2, dtype=np.uint16)
    gq[0::2] = np.clip(np.rint((coords[:, 0] - lon_lo) / gscale_x), 0, MISSING - 1)
    gq[1::2] = np.clip(np.rint((coords[:, 1] - lat_lo) / gscale_y), 0, MISSING - 1)
    fields.append({"name": "geomVerts", "dtype": "u16r", "shape": [n_cells]})
    chunks.append(verts_per_cell.tobytes())
    fields.append({"name": "geomXY", "dtype": "u16", "shape": [gq.size],
                   "min": 0.0, "scale": 0.0, "lonLo": lon_lo, "latLo": lat_lo,
                   "gscaleX": gscale_x, "gscaleY": gscale_y})
    chunks.append(gq.tobytes())

    change_methods = []
    if block.get("change"):
        change = block["change"]
        methods = list(change.get("methods", []))
        method_idx = np.asarray(change["method"], dtype=np.int64)
        for m in method_idx:
            if m < 0 or m >= len(methods):
                raise ValueError(f"change method index {m} out of range")
        change_methods = methods

        bb = np.asarray(change["break_bin"], dtype=np.int64)
        fields.append({"name": "break_bin", "dtype": "u16r", "shape": [n_cells],
                       "missing": MISSING,
                       "desc": "bin number of the largest break"})
        chunks.append(np.where(bb < 0, MISSING, bb).astype(np.uint16).tobytes())

        bd = np.asarray(change["break_date"], dtype=np.int64)
        fields.append({"name": "break_date", "dtype": "u16r", "shape": [n_cells],
                       "missing": MISSING, "unit": "days since epoch",
                       "desc": "centre date of the break bin"})
        chunks.append(np.where(bd < 0, MISSING, bd).astype(np.uint16).tobytes())

        bm = _flat_cells(change["break_magnitude"], n_cells)
        q, lo, scale = _quantise(bm)
        fields.append({"name": "break_magnitude", "dtype": "u16", "shape": [n_cells],
                       "min": lo, "scale": scale})
        chunks.append(q.tobytes())

        bs = _flat_cells(change["break_score"], n_cells)
        q, lo, scale = _quantise(bs)
        fields.append({"name": "break_score", "dtype": "u16", "shape": [n_cells],
                       "min": lo, "scale": scale})
        chunks.append(q.tobytes())

        no = np.asarray(change["n_obs_used"], dtype=np.uint16)
        fields.append({"name": "n_obs_used", "dtype": "u16r", "shape": [n_cells]})
        chunks.append(no.tobytes())

        fields.append({"name": "method", "dtype": "u16r", "shape": [n_cells],
                       "table": "changeMethods",
                       "desc": "index into the changeMethods table"})
        chunks.append(method_idx.astype(np.uint16).tobytes())

    descriptor = {
        "nCells": n_cells,
        "nPeriods": n_periods,
        "periods": block["periods"],
        "bands": block.get("_bands", []),
        "indices": block.get("_indices", []),
        "ids": block["ids"],
        "lon": block["lon"],
        "lat": block["lat"],
        "areaKm2": block["areaKm2"],
        "fields": fields,
        "missing": MISSING,
    }
    for key in ("campaign", "campaignHash", "aoiHash", "year", "resolution",
                "tier", "generatedBy", "source"):
        if block.get(key) is not None:
            descriptor[key] = block[key]
    if cell_fields:
        descriptor["cells"] = cell_fields
    if cellraw_fields:
        descriptor["cellraws"] = cellraw_fields
    if block.get("epoch"):
        descriptor["epoch"] = block["epoch"]
    if block.get("binDays"):
        descriptor["binDays"] = block["binDays"]
    if change_methods:
        descriptor["changeMethods"] = change_methods
    return descriptor, chunks


def pack(descriptor: dict, chunks: list[bytes]) -> bytes:
    raw = json.dumps(descriptor, separators=(",", ":")).encode("utf-8")
    pad = (-(HEADER + len(raw))) % ALIGN
    out = bytearray()
    out += MAGIC
    out += struct.pack("<HHII", VERSION, 0, len(raw), zlib.crc32(raw) & 0xFFFFFFFF)
    out += raw
    out += b"\x00" * pad
    for c in chunks:
        out += c
        out += b"\x00" * ((-len(out)) % ALIGN)
    return bytes(out)


def read_descriptor(path: str) -> dict:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as fh:
        head = fh.read(HEADER)
        if len(head) < HEADER or head[:4] != MAGIC:
            raise ValueError(f"{path}: not an H3LB block")
        _version, _flags, length, crc = struct.unpack("<HHII", head[4:HEADER])
        raw = fh.read(length)
    if len(raw) != length:
        raise ValueError(f"{path}: truncated descriptor")
    if zlib.crc32(raw) & 0xFFFFFFFF != crc:
        raise ValueError(f"{path}: descriptor CRC mismatch")
    return json.loads(raw.decode("utf-8"))


def read_block(path: str) -> tuple[dict, dict[str, np.ndarray]]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as fh:
        blob = fh.read()
    if len(blob) < HEADER or blob[:4] != MAGIC:
        raise ValueError(f"{path}: not an H3LB block")
    _version, _flags, length, crc = struct.unpack("<HHII", blob[4:HEADER])
    raw = blob[HEADER:HEADER + length]
    if len(raw) != length:
        raise ValueError(f"{path}: truncated descriptor")
    if zlib.crc32(raw) & 0xFFFFFFFF != crc:
        raise ValueError(f"{path}: descriptor CRC mismatch")
    desc = json.loads(raw.decode("utf-8"))

    off = HEADER + length
    off += (-off) % ALIGN
    out: dict[str, np.ndarray] = {}
    for field in desc["fields"]:
        n = 1
        for dim in field["shape"]:
            n *= int(dim)
        dtype = np.uint8 if field["dtype"] == "u8" else np.uint16
        need = n * np.dtype(dtype).itemsize
        if off + need > len(blob):
            raise ValueError(f"{path}: field {field['name']!r} runs past the end")
        out[field["name"]] = np.frombuffer(
            blob, dtype=dtype, count=n, offset=off
        ).reshape(field["shape"])
        off += need
        off += (-off) % ALIGN
    if off != len(blob):
        raise ValueError(f"{path}: {len(blob) - off} trailing bytes")
    return desc, out


def write_block(block: dict, path: str, gz: bool = True) -> dict:
    descriptor, chunks = build_block(block)
    blob = pack(descriptor, chunks)
    n_cells = descriptor["nCells"]
    n_periods = descriptor["nPeriods"]

    if gz:
        payload = gzip.compress(blob, compresslevel=6, mtime=0)
        out = path + ".gz"
    else:
        payload = blob
        out = path
    with open(out, "wb") as fh:
        fh.write(payload)

    return {
        "raw_mb": round(len(blob) / 1e6, 2),
        "served_mb": round(len(payload) / 1e6, 2),
        "cells": n_cells,
        "periods": n_periods,
        "path": out,
    }
