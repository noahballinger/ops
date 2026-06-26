"""Tests for the pure demand forecaster."""
import math
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import ForecastConfig
from app.forecasting import (MonthPoint, MonthlySalesSeries, forecast_demand)


def _series(values, start_year=2024, start_month=1, stockouts=None):
    stockouts = stockouts or set()
    pts = []
    y, m = start_year, start_month
    for i, v in enumerate(values):
        pts.append(MonthPoint(y, m, v, is_stockout=(i in stockouts)))
        m += 1
        if m > 12:
            m = 1; y += 1
    return MonthlySalesSeries(points=pts)


def test_sparse_history_falls_back_to_baseline():
    s = _series([10, 12, 8])  # 3 months
    fc = forecast_demand(s, 6, ForecastConfig())
    assert fc.method == "flat_avg"
    assert fc.confidence == "low"
    assert fc.low_data
    assert all(abs(x - 10.0) < 1e-6 for x in fc.monthly)


def test_flat_series_baseline_equals_forecast():
    s = _series([100] * 18)
    fc = forecast_demand(s, 6, ForecastConfig())
    assert abs(fc.baseline - 100) < 1e-6
    assert all(abs(x - 100) < 1.0 for x in fc.monthly)
    assert not fc.diverges_from_baseline


def test_seasonality_detected_with_two_years():
    # strong December peak, 24 months
    base = [50, 50, 60, 60, 70, 70, 80, 80, 70, 90, 120, 200]
    s = _series(base + base, start_month=1)
    fc = forecast_demand(s, 6, ForecastConfig(), )
    assert fc.method == "seasonal_trend"
    assert fc.n_history_months == 24
    # the forecast should not be a flat line
    assert max(fc.monthly) - min(fc.monthly) > 5


def test_stockout_month_excluded():
    # a zero caused by stockout shouldn't drag the mean to ~0
    vals = [100, 100, 100, 0, 100, 100, 100, 100, 100, 100, 100, 100]
    s_with_flag = _series(vals, stockouts={3})
    fc = forecast_demand(s_with_flag, 6, ForecastConfig())
    assert fc.baseline > 95   # zero excluded
    assert any("stock-out" in n or "stockout" in n for n in fc.notes)


def test_trend_increases_forecast():
    vals = list(range(50, 50 + 18 * 3, 3))  # rising
    s = _series(vals)
    fc = forecast_demand(s, 6, ForecastConfig())
    assert fc.forecast_mean > fc.baseline  # upward trend lifts forward demand


def test_divergence_flag():
    vals = list(range(20, 20 + 18 * 6, 6))  # steep rise
    s = _series(vals)
    fc = forecast_demand(s, 6, ForecastConfig())
    assert fc.diverges_from_baseline
