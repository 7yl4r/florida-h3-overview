"""Per-cell trend, season and residual assessment of the whole record.

This is the Florida port of ``h3lab.analysis``.  The model is the same
identifiable trend-plus-season fit:

    y = a + b (t - mean t) + s * wet

where ``t`` is decimal years and ``wet`` is the wet-season indicator.  The
original h3lab fit used the integer year; Florida's demo is a single 2017
record, where every composite shares the same integer year and the slope would
be unidentifiable.  Decimal years keep the units (index units per year) and
make the fit work for both a one-year record and a multi-year build.

``trend`` is ``b``, ``amp`` is ``|s|/2`` (half the fitted wet-dry swing),
``phase`` is the month at the centre of the season the swing peaks in, ``r2``
is the share of the record explained by trend plus season, and ``anom`` is the
largest residual in units of the cell's own residual scatter.  The period of
that residual is exported separately as a raw integer so the biography can
name the composite.

Cells with fewer than ``MIN_FIT_OBS`` usable composites are left missing rather
than fitted to noise.
"""

from __future__ import annotations

from datetime import date

import numpy as np

# A level, a slope and a seasonal offset: below eight composites the residual
# scale has too few degrees of freedom to say anything.
MIN_FIT_OBS = 8

# Month at the centre of each composite's window: wet is May-Oct (centre
# ~1 Aug); dry is Nov-Apr (centre ~1 Jan).
SEASON_PEAK_MONTH = {"wet": 8, "dry": 1}

# The assessment layers the viewer builds from the exported per-cell fields.
# Ranges are fixed display spans in index units so the ramp and legend cannot
# disagree, and so two resolutions stay comparable.
SPECS = [
    {"key": "trend", "label": "trend", "unit": "/yr", "range": [-0.015, 0.015],
     "ramp": "diff", "rank": "asc", "rankLabel": "steepest decline",
     "desc": "Least-squares slope of the index through the record, in index "
             "units per year. Negative is browning/loss, positive is "
             "greening/recovery."},
    {"key": "trendz", "label": "trend significance", "unit": "\u03c3",
     "range": [-4, 4], "ramp": "diff", "rank": "asc",
     "rankLabel": "steepest significant decline",
     "desc": "Trend divided by its own standard error. A large number means "
             "the slope is well constrained by the record, not merely large."},
    {"key": "amp", "label": "seasonal amplitude", "unit": "",
     "range": [0, 0.25], "ramp": "veg", "rank": "desc",
     "rankLabel": "largest seasonal swing",
     "desc": "Half the fitted wet-to-dry swing. A cell with a big number has "
             "a strong seasonal cycle; a flat one has none."},
    {"key": "phase", "label": "seasonal peak", "unit": "", "range": [1, 12],
     "ramp": "phase", "cyclic": True, "rank": "desc",
     "rankLabel": "latest seasonal peak",
     "desc": "Season the fitted swing peaks in, drawn as a month: August for "
             "a wet-season peak, January for a dry-season one. The composites "
             "are six months apart, so a precise date is not resolvable."},
    {"key": "r2", "label": "seasonal fit R\u00b2", "unit": "",
     "range": [0, 1], "ramp": "frac", "rank": "asc",
     "rankLabel": "least predictable record",
     "desc": "Share of the record explained by trend plus season. Low values "
             "are erratic cells: disturbance, regime change, or too few good "
             "composites."},
    {"key": "anom", "label": "largest anomaly", "unit": "\u03c3",
     "range": [0, 4], "ramp": "fire", "rank": "desc",
     "rankLabel": "largest single anomaly",
     "desc": "Largest residual from the fitted trend and season, in units of "
             "the cell's own residual scatter. Hover a cell for the composite."},
]

METRICS = ("trend", "trendz", "amp", "phase", "r2", "anom")


def _period_time(p: dict) -> float:
    """Decimal year for one period.

    The date is preferred because it is exact for bins that straddle a year
    boundary.  Month is only a fallback for hand-written test periods.
    """
    raw = p.get("date")
    if raw:
        try:
            d = date.fromisoformat(str(raw)[:10])
            start = date(d.year, 1, 1)
            end = date(d.year + 1, 1, 1)
            return d.year + (d - start).days / (end - start).days
        except ValueError:
            pass
    year = float(p.get("year") or 0)
    month = float(p.get("month") or 6)
    return year + (month - 0.5) / 12.0


def _wet(p: dict) -> float:
    season = p.get("season")
    if season in ("wet", "dry"):
        return 1.0 if season == "wet" else 0.0
    month = int(p.get("month") or 0)
    return 1.0 if 5 <= month <= 10 else 0.0


def _design(periods: list[dict]) -> np.ndarray:
    """Rows of [1, t', wet] with decimal time centred for conditioning."""
    t = np.array([_period_time(p) for p in periods], dtype=np.float64)
    wet = np.array([_wet(p) for p in periods], dtype=np.float64)
    return np.column_stack([np.ones_like(t), t - t.mean(), wet])


def fit_series(values, periods, observed=None) -> dict:
    """Fit one index through time, per cell.

    ``values`` is ``(nPeriods, nCells)``; missing values and periods not marked
    observed are dropped cell by cell.  Returns a dict of ``(nCells,)`` arrays:
    trend, trendz, amp, phase (month), r2, anom (|z|), anom_p (period index,
    -1 when none).
    """
    y = np.asarray(values, dtype=np.float64)
    n_periods, n_cells = y.shape
    X = _design(periods)
    k = X.shape[1]

    finite = np.isfinite(y)
    if observed is not None:
        finite &= np.asarray(observed, dtype=bool)

    # Normal equations accumulated per cell, skipping missing composites.
    # Accumulating in the period loop is vectorised across cells and avoids a
    # Python-level solve per cell.
    xtx = np.zeros((n_cells, k, k), dtype=np.float64)
    xty = np.zeros((n_cells, k), dtype=np.float64)
    for p in range(n_periods):
        m = finite[p]
        if not m.any():
            continue
        x = X[p]
        xtx[m] += np.outer(x, x)
        xty[m] += x * y[p][m, None]

    # pinv, not solve: a cell with a short or degenerate record yields a
    # singular normal matrix, and the result is masked out below anyway.
    xtxi = np.linalg.pinv(xtx)
    beta = np.einsum("cij,cj->ci", xtxi, xty)
    fitted = X @ beta.T

    resid = np.where(finite, y - fitted, 0.0)
    ss_res = np.einsum("pc,pc->c", resid, resid)
    n = finite.sum(axis=0)
    mean = np.divide(np.where(finite, y, 0.0).sum(axis=0), n,
                     out=np.full(n_cells, np.nan), where=n > 0)
    ss_tot = np.where(finite, (y - mean) ** 2, 0.0).sum(axis=0)

    df = n - k
    ok = n >= MIN_FIT_OBS
    with np.errstate(invalid="ignore", divide="ignore"):
        sigma2 = np.divide(ss_res, df, out=np.full(n_cells, np.nan),
                           where=df > 0)
        sigma = np.sqrt(np.clip(sigma2, 0.0, None))
        se = np.sqrt(np.clip(sigma2 * xtxi[:, 1, 1], 0.0, None))

        trend = beta[:, 1]
        trendz = np.divide(trend, se, out=np.full(n_cells, np.nan),
                           where=se > 1e-12)
        swing = beta[:, 2]                       # wet minus dry
        amp = np.abs(swing) / 2.0
        peak_month = np.where(swing >= 0.0, SEASON_PEAK_MONTH["wet"],
                              SEASON_PEAK_MONTH["dry"]).astype(np.float64)
        r2 = np.divide(ss_res, ss_tot, out=np.full(n_cells, np.nan),
                       where=ss_tot > 1e-12)
        r2 = 1.0 - r2

    # The largest departure from the cell's own fitted habit.
    with np.errstate(invalid="ignore", divide="ignore"):
        absz = np.divide(np.abs(resid), sigma[None, :],
                         out=np.full_like(resid, np.inf),
                         where=sigma[None, :] > 1e-12)
    absz = np.where(finite, absz, -np.inf)
    anom_p = np.argmax(absz, axis=0)
    anom = absz[anom_p, np.arange(n_cells)]
    flat = sigma < 1e-12
    anom = np.where(np.isfinite(anom) & ~flat, anom, np.nan)
    anom_p = np.where(np.isfinite(anom), anom_p, -1)

    out = {
        "trend": trend,
        "trendz": trendz,
        "amp": amp,
        "phase": peak_month,
        "r2": r2,
        "anom": anom,
    }
    for key in out:
        out[key] = np.where(ok, out[key], np.nan)
    # A peak with no swing is not a season.
    out["phase"] = np.where(ok & (amp > 1e-6), out["phase"], np.nan)
    out["anom_p"] = np.where(ok, anom_p, -1).astype(np.int64)
    return out


def analyse(periods, index_values, observed) -> tuple[dict, dict]:
    """Assessment fields for one block.

    Returns ``(cell, cellraw)``: quantised float fields keyed
    ``metric.index`` and raw integer fields keyed ``name.index``.  Both are per
    cell, so they follow the change-layer shape rather than the period-major
    shape.  The record mean and standard deviation ship alongside the fit so
    the "record mean" and "stability" layers do not need a separate source.
    """
    cell: dict[str, np.ndarray] = {}
    cellraw: dict[str, np.ndarray] = {}
    obs = np.asarray(observed, dtype=bool)

    for name, values in index_values.items():
        y = np.asarray(values, dtype=np.float64)
        fit = fit_series(y, periods, obs)
        for metric in METRICS:
            cell[f"{metric}.{name}"] = fit[metric]
        cellraw[f"anomp.{name}"] = fit["anom_p"]

        finite = np.isfinite(y) & obs
        n = finite.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            mean = np.divide(np.where(finite, y, 0.0).sum(axis=0), n,
                             out=np.full(y.shape[1], np.nan), where=n > 0)
        cell[f"mean.{name}"] = mean
        # Population standard deviation, matching the old stability layer.
        dev = np.where(finite, y - mean[None, :], 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            var = np.divide((dev * dev).sum(axis=0), n,
                            out=np.full(y.shape[1], np.nan), where=n > 0)
        cell[f"std.{name}"] = np.sqrt(np.clip(var, 0.0, None))

    return cell, cellraw
