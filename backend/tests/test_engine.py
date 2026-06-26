"""
Engine tests against HAND-CHECKED rows pulled directly from the workbook's
SEA sheet.  Each fixture carries the workbook's own computed projection
(OH MTH 1..6), SEA QTY and AIR QTY; we assert our pure engine reproduces them
within rounding.  This is the acceptance criterion: "reproduces the workbook's
suggested sea/air quantities ... within rounding".
"""
import math
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import EngineConfig
from app.engine import (ProductInput, SkuSnapshot, suggest_one, ceil_to_case,
                        _project_moh)

# fixtures: (name, monthly_sales, current_MOH, target, incoming_MOH[6],
#            expected OH MTH1..6, expected sea_qty, expected air_qty)
SEA_FIXTURES = [
    ("Devi-Car-Hanging", 67.25, 0.1635687732, 6.0,
     [0, 3.1822, 0, 5.3383, 0.5204, 0],
     [0, 2.1822, 1.1822, 5.5204, 5.0409, 4.0409], 131.75, 0),
    ('Adiyogi miniature 2" car stand - Black', 50.4167, 0.0, 6.0,
     [0, 2.8165, 0, 5.6331, 0.595, 0],
     [0, 1.8165, 0.8165, 5.4496, 5.0446, 4.0446], 98.5833, 0),
    ("Jasmine Orient Solid Perfume", 82.5, 0.0121212121, 8.0,
     [0, 10.3758, 0, 1.7939, 0, 0],
     [0, 9.3758, 8.3758, 9.1697, 8.1697, 7.1697], 68.5, 0),
    ("Copper Devi Pendant", 30.0, 0.0, 10.0,
     [0, 0, 0, 0.0333, 10, 0],
     [0, 0, 0, 0, 9, 8], 60, 90),
    ("Instant Sanjeevini 20 kg Bag", 6.0, 0.0, 8.0,
     [0, 0, 0, 0, 0, 0],
     [0, 0, 0, 0, 0, 0], 48, 18),
]


def _mk(monthly, moh, target, inc_moh, category="TEST"):
    on_hand = moh * monthly
    inc_units = [m * monthly for m in inc_moh]
    p = ProductInput(global_sku="X", category=category, cost=2.0,
                     retail_price=5.0, target_moh_override=target)
    return SkuSnapshot(product=p, on_hand=on_hand, avg_monthly_sales=monthly,
                       incoming_units_by_month=inc_units, forecast=None)


def test_projection_matches_workbook():
    cfg = EngineConfig()
    for (name, mon, moh, target, inc, exp_proj, exp_sea, exp_air) in SEA_FIXTURES:
        inc_moh = inc
        proj = _project_moh(moh, [1.0] * 6, inc_moh)
        for got, want in zip(proj, exp_proj):
            assert abs(got - want) < 0.01, f"{name}: proj {got} != {want}"


def test_sea_air_quantities_match_workbook():
    cfg = EngineConfig()
    for (name, mon, moh, target, inc, exp_proj, exp_sea, exp_air) in SEA_FIXTURES:
        s = suggest_one(_mk(mon, moh, target, inc), cfg)
        assert abs(s.suggested_sea_qty - exp_sea) < 1.0, \
            f"{name}: sea {s.suggested_sea_qty} != {exp_sea}"
        assert abs(s.suggested_air_qty - exp_air) < 1.0, \
            f"{name}: air {s.suggested_air_qty} != {exp_air}"


def test_ceiling_to_case():
    assert ceil_to_case(131.75, 1) == 132
    assert ceil_to_case(98.58, 1) == 99
    assert ceil_to_case(60, 60) == 60
    assert ceil_to_case(61, 60) == 120
    assert ceil_to_case(0, 32) == 0
    assert ceil_to_case(1, 32) == 32


def test_air_split_reason_present_when_air_ordered():
    cfg = EngineConfig()
    # Copper Devi Pendant: stocks out -> air
    s = suggest_one(_mk(30.0, 0.0, 10.0, [0, 0, 0, 0.0333, 10, 0]), cfg)
    assert s.suggested_air_round > 0
    assert "floor" in s.air_split_reason.lower()


def test_forecast_flat_equals_baseline():
    """When forecast == flat avg, smart result must equal workbook baseline."""
    from app.forecasting import Forecast
    cfg = EngineConfig()
    mon = 100.0
    fc = Forecast(monthly=[mon] * 6, method="flat_avg", baseline=mon,
                  avg_monthly=mon, confidence="low", n_history_months=0,
                  low_data=True, uncertainty_pct=0.5, diverges_from_baseline=False)
    p = ProductInput(global_sku="X", category="TEST", target_moh_override=8.0,
                     cost=1, retail_price=3)
    snap = SkuSnapshot(product=p, on_hand=0, avg_monthly_sales=mon,
                       incoming_units_by_month=[0] * 6, forecast=fc)
    s = suggest_one(snap, cfg)
    assert s.suggested_sea_round == s.baseline_sea_round
