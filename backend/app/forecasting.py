"""
Demand forecasting -- a PURE, testable module.

The workbook collapses a year of sales into a flat monthly average
(annual / 12).  That throws away trend and seasonality, which is exactly the
information worth keeping when the goods we order now land 4-6 months out.

This module produces a *per-future-month* demand forecast across the horizon,
using classical, explainable methods sized for ~12-36 monthly data points:

  * < 6 useable months           -> flat average (workbook baseline), low conf
  * 6-23 useable months          -> moving-average level + (optional) trend
  * >= 24 useable months         -> multiplicative seasonal indices + trend

Stockout handling: a month with zero (or suppressed) sales because the item
was out of stock is NOT zero demand.  Such months are excluded from the
estimation of level / trend / seasonality and flagged.

The forecaster has no I/O.  It takes a `MonthlySalesSeries` plus a
`ForecastConfig` and returns a `Forecast`.  Swap or tune the method here
without touching the projection / sea-air logic.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional

from .config import ForecastConfig


@dataclass
class MonthPoint:
    year: int
    month: int          # 1-12
    units: float
    is_stockout: bool = False   # True => suppressed demand, exclude from fit

    @property
    def ord(self) -> int:
        return self.year * 12 + (self.month - 1)


@dataclass
class MonthlySalesSeries:
    """Trailing monthly sales for one SKU (oldest -> newest)."""
    points: List[MonthPoint] = field(default_factory=list)

    def useable(self) -> List[MonthPoint]:
        return [p for p in self.points if not p.is_stockout]

    @property
    def n_useable(self) -> int:
        return len(self.useable())


@dataclass
class Forecast:
    monthly: List[float]            # expected units for each future month
    method: str                     # "flat_avg" | "moving_avg_trend" | "seasonal_trend"
    baseline: float                 # workbook flat average (units / month)
    avg_monthly: float              # denominator used to convert units<->MOH
    confidence: str                 # "high" | "medium" | "low"
    n_history_months: int
    low_data: bool
    uncertainty_pct: float          # rough +/- band as a fraction
    diverges_from_baseline: bool    # forecast mean vs baseline gap large?
    notes: List[str] = field(default_factory=list)

    @property
    def forecast_mean(self) -> float:
        return sum(self.monthly) / len(self.monthly) if self.monthly else 0.0


def _linear_trend(xs: List[float], ys: List[float]) -> tuple[float, float]:
    """Ordinary least squares slope & intercept."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0, my
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    intercept = my - slope * mx
    return slope, intercept


def forecast_demand(series: MonthlySalesSeries,
                    horizon: int,
                    cfg: ForecastConfig,
                    first_future: Optional[date] = None) -> Forecast:
    """Return a per-month demand forecast across `horizon` months.

    `first_future` is the calendar month of forecast month #1 (used for
    seasonal indexing).  Defaults to the month after the last data point.
    """
    pts = series.useable()
    n = len(pts)
    notes: List[str] = []

    n_stockout = len(series.points) - n
    if n_stockout:
        notes.append(f"{n_stockout} stock-out/suppressed month(s) excluded from the fit")

    # Baseline = sell-through velocity: average over IN-STOCK (selling) months
    # only, so stock-out months don't deflate demand. (Stock-out/suppressed
    # points are excluded above.)
    baseline = (sum(p.units for p in pts) / n) if n else 0.0
    avg_monthly = baseline  # denominator for MOH conversions

    # Determine the calendar month for each future step.
    if first_future is None:
        if pts:
            last = pts[-1]
            y, m = last.year, last.month + 1
        else:
            today = date.today()
            y, m = today.year, today.month
        while m > 12:
            y += 1
            m -= 12
        first_future = date(y, m, 1)

    future_months = []
    fy, fm = first_future.year, first_future.month
    for _ in range(horizon):
        future_months.append((fy, fm))
        fm += 1
        if fm > 12:
            fm = 1
            fy += 1

    # ---- Method selection ------------------------------------------------
    if n < cfg.low_confidence_months:
        monthly = [baseline] * horizon
        method = "flat_avg"
        confidence = "low"
        low_data = True
        notes.append(f"only {n} month(s) of history -> fell back to flat average")
        cv = 0.0
    else:
        # de-trend on useable points (ordinal time -> units)
        xs = [float(p.ord) for p in pts]
        ys = [p.units for p in pts]
        slope, intercept = _linear_trend(xs, ys)

        # coefficient of variation for uncertainty + confidence
        mean = statistics.mean(ys)
        sd = statistics.pstdev(ys) if n > 1 else 0.0
        cv = (sd / mean) if mean else 0.0

        has_season = n >= cfg.min_months_for_seasonal
        if has_season:
            # multiplicative seasonal indices on de-trended residual ratios
            seasonal = _seasonal_indices(pts, slope, intercept, cfg.seasonal_period)
            method = "seasonal_trend"
            monthly = []
            for (yy, mm) in future_months:
                ordv = yy * 12 + (mm - 1)
                level = slope * ordv + intercept
                idx = seasonal.get(mm, 1.0)
                monthly.append(max(0.0, level * idx))
            confidence = "high" if cv < 0.5 else "medium"
        else:
            # level (recent moving average) + gentle trend, no seasonality
            window = pts[-min(6, n):]
            level0 = sum(p.units for p in window) / len(window)
            method = "moving_avg_trend"
            # anchor the trend at the last observed ordinal
            last_ord = pts[-1].ord
            monthly = []
            for (yy, mm) in future_months:
                ordv = yy * 12 + (mm - 1)
                val = level0 + slope * (ordv - last_ord)
                monthly.append(max(0.0, val))
            confidence = "medium" if n >= cfg.min_months_for_trend else "low"
        low_data = n < cfg.min_months_for_trend

    # Uncertainty band: grows with CV and shrinks with history.
    uncertainty = min(0.9, (cv if n else 1.0) / math.sqrt(max(n, 1)) + 0.05)

    fmean = sum(monthly) / len(monthly) if monthly else 0.0
    diverges = baseline > 0 and abs(fmean - baseline) / baseline >= cfg.divergence_flag_pct
    if diverges:
        notes.append(
            f"forecast mean {fmean:.1f}/mo diverges {(fmean-baseline)/baseline*100:+.0f}% "
            f"from flat baseline {baseline:.1f}/mo")

    return Forecast(
        monthly=[round(v, 4) for v in monthly],
        method=method,
        baseline=round(baseline, 4),
        avg_monthly=round(avg_monthly, 4),
        confidence=confidence,
        n_history_months=n,
        low_data=low_data,
        uncertainty_pct=round(uncertainty, 3),
        diverges_from_baseline=diverges,
        notes=notes,
    )


def _seasonal_indices(pts: List[MonthPoint], slope: float, intercept: float,
                      period: int) -> dict[int, float]:
    """Multiplicative seasonal index per calendar month = mean(actual/level)."""
    ratios: dict[int, List[float]] = {}
    for p in pts:
        level = slope * p.ord + intercept
        if level > 0:
            ratios.setdefault(p.month, []).append(p.units / level)
    idx = {m: (sum(v) / len(v)) for m, v in ratios.items() if v}
    # normalise so indices average ~1 across the months we have
    if idx:
        avg = sum(idx.values()) / len(idx)
        if avg > 0:
            idx = {m: v / avg for m, v in idx.items()}
    return idx
