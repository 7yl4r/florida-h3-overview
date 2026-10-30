"""Change-point detection over a per-cell NDVI series.

Vendored from h3lab@26c042f (``src/h3lab/change.py``).  The method is unchanged:
a harmonic regression with a linear trend and a step-regressor F-scan, with the
threshold calibrated by a simulated AR(1) null.  The campaign supplies the
epoch and bin length, and a multi-year series is fitted in continuous time.

This is deliberately a screening layer.  The stored fields record the algorithm
name, version and parameters so a value can be reproduced; the numbers are not
a substitute for event characterisation.
"""

from __future__ import annotations

from datetime import date

import numpy as np

from .bins import BIN_DAYS, EPOCH, bin_centre

YEAR_DAYS = 365.25
METHOD_ID = "harmonic_fscan_v1"

DEFAULT_ALPHA = 0.01
DEFAULT_AR = 0.4
DEFAULT_N_SIM = 200
DEFAULT_SEED = 20260101
NULL_SIGMA = 0.03
NULL_AMPLITUDE = 0.35
NULL_LEVEL = 0.45

_THRESHOLD_CACHE: dict = {}


def _design_matrix(t_days: np.ndarray, n_harmonics: int,
                   trend: bool = True) -> np.ndarray:
    cols = [np.ones_like(t_days)]
    if trend:
        cols.append(t_days / YEAR_DAYS)
    for h in range(1, n_harmonics + 1):
        w = 2.0 * np.pi * h * t_days / YEAR_DAYS
        cols.append(np.sin(w))
        cols.append(np.cos(w))
    return np.column_stack(cols)


def _fscan(t_days: np.ndarray, Y: np.ndarray, n_harmonics: int,
           trend: bool = True):
    n, n_cells = Y.shape
    X = _design_matrix(t_days, n_harmonics, trend)
    p = X.shape[1]
    df = n - p - 1
    if df <= 0:
        z = np.zeros(n_cells)
        return np.zeros(n_cells, dtype=np.int64), z, z

    gram_inv = np.linalg.pinv(X.T @ X)
    beta = gram_inv @ (X.T @ Y)
    resid = Y - X @ beta
    rss_base = np.einsum("ij,ij->j", resid, resid)
    suffix = np.cumsum(Y[::-1], axis=0)[::-1]

    best_f = np.full(n_cells, -np.inf)
    best_k = np.zeros(n_cells, dtype=np.int64)
    best_step = np.zeros(n_cells)

    for k in range(2, n - 1):
        v = X[k:].sum(axis=0)
        d = (n - k) - float(v @ gram_inv @ v)
        if d <= 1e-9:
            continue
        c = suffix[k] - v @ beta
        ssr = (c * c) / d
        rss_step = rss_base - ssr
        with np.errstate(divide="ignore", invalid="ignore"):
            f = np.where(rss_step > 0.0, ssr / (rss_step / df),
                         np.where(ssr > 0.0, np.inf, 0.0))
        f = np.where(np.isfinite(f) | (f == np.inf), f, -np.inf)
        f = np.where(rss_step >= 0.0, f, -np.inf)
        better = f > best_f
        if np.any(better):
            best_f = np.where(better, f, best_f)
            best_k = np.where(better, k, best_k)
            best_step = np.where(better, c / d, best_step)
    return best_k, best_f, best_step


def _null_threshold(t_days: np.ndarray, n_obs: int, alpha: float, ar: float,
                    n_sim: int, seed: int, n_harmonics: int,
                    trend: bool = True) -> float:
    key = (n_obs, t_days.tobytes(), alpha, ar, n_sim, seed, n_harmonics, trend)
    hit = _THRESHOLD_CACHE.get(key)
    if hit is not None:
        return hit

    rng = np.random.default_rng(seed)
    t = np.asarray(t_days, dtype=float)[:n_obs]
    phase = rng.uniform(0.0, 2.0 * np.pi, n_sim)
    noise = np.zeros((n_obs, n_sim))
    for i in range(1, n_obs):
        noise[i] = ar * noise[i - 1] + rng.normal(0.0, NULL_SIGMA, n_sim)
    Y = np.empty((n_obs, n_sim))
    for i in range(n_obs):
        Y[i] = (NULL_LEVEL
                + NULL_AMPLITUDE * np.sin(2.0 * np.pi * t[i] / YEAR_DAYS
                                          + phase)
                + noise[i])

    _, f, _ = _fscan(t_days, Y, n_harmonics, trend)
    thr = float(np.quantile(f, 1.0 - alpha))
    _THRESHOLD_CACHE[key] = thr
    return thr


def detect_breaks(ndvi_series: np.ndarray, bins_list=None,
                  epoch: date = EPOCH, bin_days: int = BIN_DAYS,
                  min_obs: int = 8, n_harmonics: int = 2, trend: bool = True,
                  alpha: float = DEFAULT_ALPHA, ar: float = DEFAULT_AR,
                  n_sim: int = DEFAULT_N_SIM, seed: int = DEFAULT_SEED,
                  min_score: float | None = None):
    ndvi_series = np.asarray(ndvi_series, dtype=np.float64)
    if ndvi_series.ndim == 1:
        ndvi_series = ndvi_series.reshape(-1, 1)
    n_periods, n_cells = ndvi_series.shape

    if bins_list is None:
        bins_list = list(range(n_periods))
    bins_list = [int(b) for b in bins_list]

    break_bin = np.full(n_cells, -1, dtype=np.int32)
    break_date = np.full(n_cells, "", dtype=object)
    break_magnitude = np.full(n_cells, np.nan, dtype=np.float32)
    break_score = np.full(n_cells, np.nan, dtype=np.float32)
    n_obs_used = np.zeros(n_cells, dtype=np.int32)

    method_spec = (f"{METHOD_ID}(harmonics={n_harmonics},trend={int(trend)},"
                   f"min_obs={min_obs},scan=step-dummy,fstat=partial-F,"
                   f"null=ar1(phi={ar}),nsim={n_sim},seed={seed},"
                   f"alpha={alpha})")
    method = np.array([method_spec] * n_cells, dtype=object)

    t_all = np.array(
        [(bin_centre(b, epoch, bin_days) - epoch).days for b in bins_list],
        dtype=float,
    )
    centres = [bin_centre(b, epoch, bin_days).isoformat() for b in bins_list]

    finite = np.isfinite(ndvi_series)
    n_obs_all = finite.sum(axis=0)
    n_obs_used[:] = n_obs_all

    masks = (finite.astype(np.int64)
             * (1 << np.arange(n_periods))[:, None]).sum(axis=0)

    min_fit = max(min_obs, _design_matrix(t_all, n_harmonics, trend).shape[1] + 2)
    thresholds: dict = {}
    bin_arr = np.asarray(bins_list, dtype=np.int64)

    for mask in np.unique(masks):
        cells = np.flatnonzero(masks == mask)
        rows = np.flatnonzero((mask & (1 << np.arange(n_periods))) != 0)
        n_obs = rows.size
        if n_obs < min_fit:
            continue
        t = t_all[rows]
        Y = ndvi_series[rows][:, cells]

        best_k, best_f, best_step = _fscan(t, Y, n_harmonics, trend)

        if min_score is not None:
            thr = float(min_score)
        else:
            thr = _null_threshold(t, n_obs, alpha, ar, n_sim, seed,
                                  n_harmonics, trend)
        thresholds[(int(n_obs), t.tobytes())] = thr

        hit = best_f >= thr
        if not np.any(hit):
            continue
        period = rows[best_k]
        break_bin[cells] = np.where(hit, bin_arr[period], -1).astype(np.int32)
        texts = [centres[p] for p in period]
        for cell, ok, text in zip(cells, hit, texts):
            if ok:
                break_date[cell] = text
        break_magnitude[cells] = np.where(hit, best_step, np.nan).astype(np.float32)
        break_score[cells] = np.where(hit, best_f, np.nan).astype(np.float32)

    return {
        "break_bin": break_bin,
        "break_date": break_date,
        "break_magnitude": break_magnitude,
        "break_score": break_score,
        "n_obs_used": n_obs_used,
        "method": method,
        "method_spec": method_spec,
        "bins": bins_list,
        "thresholds": thresholds,
    }


def linear_trend(series: np.ndarray, bins_list=None, epoch: date = EPOCH,
                 bin_days: int = BIN_DAYS) -> tuple[np.ndarray, np.ndarray]:
    """Per-cell linear NDVI trend, in index units per year.

    Returns ``(slope, intercept)``.  Cells with fewer than three observations
    get NaN.  This is a description of the record, not a forecast.
    """
    series = np.asarray(series, dtype=np.float64)
    if series.ndim == 1:
        series = series.reshape(-1, 1)
    n_periods, n_cells = series.shape
    if bins_list is None:
        bins_list = list(range(n_periods))
    t = np.array([(bin_centre(int(b), epoch, bin_days) - epoch).days
                  for b in bins_list], dtype=float) / YEAR_DAYS

    finite = np.isfinite(series)
    count = finite.sum(axis=0)
    t_mean = np.where(count > 0,
                      np.nansum(np.where(finite, t[:, None], np.nan), axis=0)
                      / np.maximum(count, 1), np.nan)
    y_mean = np.where(count > 0, np.nansum(series, axis=0)
                      / np.maximum(count, 1), np.nan)
    dt = np.where(finite, t[:, None] - t_mean, 0.0)
    dy = np.where(finite, series - y_mean, 0.0)
    den = np.sum(dt * dt, axis=0)
    num = np.sum(dt * dy, axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        slope = np.where((count >= 3) & (den > 0), num / den, np.nan)
    intercept = y_mean - slope * t_mean
    return slope.astype(np.float32), intercept.astype(np.float32)
