"""Spectral indices, computed once from composite surface reflectance.

Vendored from h3lab@26c042f (``src/h3lab/features.py``).  The ratio guard is
kept exactly: both bands must be above the noise floor, the denominator must
not cancel, and the two bands must have the same sign.  That last rule is what
bounds a ratio index to [-1, 1] over dark water.
"""

from __future__ import annotations

import numpy as np

from .constants import BANDS, INDEX_SPECS, INDICES

RATIO_EPS = 0.01


def _ratio(a: np.ndarray, c: np.ndarray) -> np.ndarray:
    num = a - c
    den = a + c
    ok = ((np.abs(den) > RATIO_EPS)
          & (np.abs(a) > RATIO_EPS)
          & (np.abs(c) > RATIO_EPS)
          & (np.sign(a) == np.sign(c)))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(ok & np.isfinite(num), num / np.where(ok, den, 1.0), np.nan)


def compute_indices(refl: dict[str, np.ndarray],
                    specs: dict | None = None,
                    indices: tuple[str, ...] | None = None) -> dict[str, np.ndarray]:
    specs = specs or INDEX_SPECS
    indices = indices or INDICES
    out: dict[str, np.ndarray] = {}
    for name in indices:
        spec = specs[name]
        if spec["kind"] == "ratio":
            a, c = spec["bands"]
            out[name] = _ratio(refl[a], refl[c]).astype(np.float32)
        elif spec["kind"] == "savi":
            a, c = spec["bands"]
            L = float(spec.get("L", 0.2))
            num = (refl[a] - refl[c]) * (1.0 + L)
            den = (refl[a] + refl[c]) + L * (1.0 - refl[c])
            ok = np.abs(den) > RATIO_EPS
            with np.errstate(invalid="ignore", divide="ignore"):
                out[name] = np.where(
                    ok & np.isfinite(num), num / np.where(ok, den, 1.0), np.nan
                ).astype(np.float32)
        else:  # pragma: no cover - guarded by constants
            raise ValueError(f"unknown index kind {spec['kind']!r} for {name!r}")
    return out


def valid_indices(refl: dict[str, np.ndarray],
                  idx: dict[str, np.ndarray],
                  bands: tuple[str, ...] = BANDS,
                  indices: tuple[str, ...] = INDICES) -> np.ndarray:
    ok = np.isfinite(refl[bands[0]])
    for band in bands[1:]:
        ok &= np.isfinite(refl[band])
    for name in indices:
        ok &= np.isfinite(idx[name])
    return ok


def spectral_angle(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    num = np.sum(a * b, axis=0)
    na = np.sqrt(np.sum(a * a, axis=0))
    nb = np.sqrt(np.sum(b * b, axis=0))
    with np.errstate(invalid="ignore", divide="ignore"):
        cos = np.where((na > 1e-9) & (nb > 1e-9), num / (na * nb), np.nan)
    return np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))).astype(np.float32)
