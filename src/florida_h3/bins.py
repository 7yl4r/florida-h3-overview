"""Continuous 16-day bin model.

The time axis is a continuous grid anchored at the campaign epoch, not a
calendar-anchored set of seasons.  Sixteen days do not divide a year, so
``bin_index % 23`` has no seasonal meaning; season is always a property of the
date (see :func:`is_wet_season_date`).

The defaults match the frozen h3lab pilot (epoch 2000-01-01, 16-day bins) but
every function accepts an explicit epoch and bin length, because a campaign is
allowed to choose a different time axis.
"""

from __future__ import annotations

from datetime import date, timedelta

EPOCH = date(2000, 1, 1)
BIN_DAYS = 16


def _as_date(d: date | str) -> date:
    if isinstance(d, date):
        return d
    text = str(d)
    if "T" in text:
        text = text.split("T")[0]
    return date.fromisoformat(text)


def bin_index(d: date | str, epoch: date = EPOCH,
              bin_days: int = BIN_DAYS) -> int:
    return (_as_date(d) - epoch).days // bin_days


def bin_start(b: int, epoch: date = EPOCH, bin_days: int = BIN_DAYS) -> date:
    return epoch + timedelta(days=int(b) * bin_days)


def bin_end(b: int, epoch: date = EPOCH, bin_days: int = BIN_DAYS) -> date:
    """Inclusive end of the bin."""
    return bin_start(b, epoch, bin_days) + timedelta(days=bin_days - 1)


def bin_centre(b: int, epoch: date = EPOCH, bin_days: int = BIN_DAYS) -> date:
    return epoch + timedelta(days=int(b) * bin_days + bin_days // 2)


def bins_for_year(year: int, epoch: date = EPOCH,
                  bin_days: int = BIN_DAYS) -> list[int]:
    """All bins that overlap the calendar year.

    A 16-day bin can straddle 1 January, so the first and last bin of a year
    may also appear in the neighbouring year's product.  That is intentional:
    a year is a build partition, not a disjoint slice of the continuous time
    axis.  Whole-record derived layers deduplicate bins before fitting.
    """
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    b0 = bin_index(start, epoch, bin_days)
    b1 = bin_index(end, epoch, bin_days)
    return list(range(b0, b1 + 1))


def bins_centred_in_year(year: int, epoch: date = EPOCH,
                         bin_days: int = BIN_DAYS) -> list[int]:
    """Bins whose centre date falls inside the calendar year."""
    out = []
    for b in bins_for_year(year, epoch, bin_days):
        if bin_centre(b, epoch, bin_days).year == year:
            out.append(b)
    return out


def is_wet_season_date(d: date | str) -> bool:
    """True for May through October, the Florida growing season."""
    return 5 <= _as_date(d).month <= 10


def is_dry_season_date(d: date | str) -> bool:
    return not is_wet_season_date(d)


def season_of(d: date | str) -> str:
    return "wet" if is_wet_season_date(d) else "dry"
