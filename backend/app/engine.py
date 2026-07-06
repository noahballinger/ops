"""
The suggestion engine -- a PURE, testable function.

Given a per-SKU snapshot (on-hand, sales series, in-transit, product attrs)
plus config, it returns a suggestion: sea qty, air qty, case-rounded
quantities, the month 1-6 projection, the economics, and a human-readable
reason for any air split.  No I/O happens in here.

It reproduces the workbook's SEA-sheet math exactly when demand is flat, and
generalises to a per-month demand forecast (§3a) by projecting in MOH-space
with a per-month demand multiplier.  When the forecast equals the flat
average, every month's multiplier is 1.0 and the result is identical to the
workbook -- so the workbook stays a provable baseline.

Workbook formulas reproduced (SEA sheet):
    OH_mthN = max(0, OH_mth(N-1) - demand_moh_N + incoming_moh_N)   (start = current MOH)
    SEA SHIP (months) = max(0, target - OH_mth6)        col T: =IF(P<J, J-P, 0)
    SEA QTY           = sea_months * monthly_sales       col Q: =T*F
    AIR SHIP (months) = max(0, 3 - OH_mth4)              col U: =IF(N<3, 3-N, 0)
    AIR QTY           = air_months * monthly_sales       col S: =U*F
    round             = CEILING(qty, case)               ORDER LIST cols I/J
Economics (ORDER LIST):
    margin            = retail - cogs                    col Q
    sea/air cost      = round * cogs                     cols K/L
    profit_lost_air   = margin * air_round               col P
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional

from .config import (EngineConfig, SOURCE_DOMESTIC)
from .forecasting import Forecast


def ceil_to_case(qty: float, case: int) -> int:
    """Excel CEILING(qty, case): round qty UP to nearest multiple of case."""
    if qty <= 0:
        return 0
    if case <= 1:
        return int(math.ceil(qty - 1e-9))
    return int(math.ceil((qty - 1e-9) / case) * case)


@dataclass
class ProductInput:
    global_sku: str
    name: str = ""
    us_sku: str = ""
    odoo_ref: str = ""
    category: str = ""
    case_size: Optional[int] = None
    unit_weight: Optional[float] = None
    hsn_code: str = ""
    cost: float = 0.0          # COGS / landed cost (PRICE LIST LC USA)
    retail_price: float = 0.0
    source: str = "IMPORT_SEA"
    expiry_tracked: bool = False
    compliance_flag: str = ""          # free text; non-empty => flagged
    target_moh_override: Optional[float] = None
    case_size_override: Optional[int] = None
    moq: Optional[int] = None          # for DOMESTIC vendors


@dataclass
class SkuSnapshot:
    product: ProductInput
    on_hand: float                       # useable on-hand units
    avg_monthly_sales: float             # sell-through velocity (sold per in-stock month)
    incoming_units_by_month: List[float] # in-transit arriving in proj month 1..H
    forecast: Optional[Forecast] = None  # per-month demand; None => use flat
    units_sold: float = 0.0              # total units sold in the trailing window
    months_active: int = 0               # months the SKU was in stock / selling


@dataclass
class Suggestion:
    global_sku: str
    name: str
    us_sku: str
    category: str
    source: str
    # demand
    avg_monthly_sales: float
    sell_through: float          # sold / (sold + on-hand) over the window, 0..1
    units_sold: float            # total units sold in the trailing window
    months_active: int           # months the SKU was in stock / selling
    forecast_monthly: List[float]
    forecast_mean: float
    baseline_monthly_sales: float
    forecast_method: str
    forecast_confidence: str
    diverges_from_baseline: bool
    # stock / projection
    on_hand: float
    current_moh: float
    incoming_units_by_month: List[float]
    projected_moh: List[float]           # month 1..H
    projected_moh_m4: float
    projected_moh_m6: float
    projected_moh_with_order: List[float]  # coverage IF the suggested order is placed
    forecast_history_months: int           # months of sales backing the forecast
    target_moh: float
    case_size: int
    # quantities (smart forecast)
    suggested_sea_qty: float
    suggested_air_qty: float
    suggested_sea_round: int
    suggested_air_round: int
    # quantities (workbook flat baseline, for comparison)
    baseline_sea_round: int
    baseline_air_round: int
    # economics
    unit_cost: float
    retail_price: float
    margin: float
    air_shipping_cost: float             # round * cogs (sea-equiv goods cost by air)
    profit_lost_by_air: float
    # flags / explainability
    compliance_flag: str
    air_split_reason: str
    notes: List[str] = field(default_factory=list)


def _project_moh(current_moh: float,
                 demand_moh: List[float],
                 incoming_moh: List[float]) -> List[float]:
    """Forward MOH projection. demand_moh[n] defaults to 1.0 (flat) -> exactly
    the workbook's '-1 per month'."""
    oh = current_moh
    out = []
    for n in range(len(incoming_moh)):
        d = demand_moh[n] if n < len(demand_moh) else 1.0
        oh = max(0.0, oh - d + incoming_moh[n])
        out.append(oh)
    return out


def suggest_one(snap: SkuSnapshot, cfg: EngineConfig) -> Suggestion:
    p = snap.product
    avg = snap.avg_monthly_sales
    horizon = cfg.horizon
    case = cfg.case_size_for(p.category, p.case_size_override or p.case_size)
    target = cfg.target_moh_for(p.category, p.target_moh_override)

    # incoming in MOH units (qty / avg monthly sales)
    inc_units = list(snap.incoming_units_by_month) + \
        [0.0] * (horizon - len(snap.incoming_units_by_month))
    inc_units = inc_units[:horizon]
    incoming_moh = [(u / avg if avg > 0 else 0.0) for u in inc_units]

    current_moh = (snap.on_hand / avg) if avg > 0 else 0.0

    # ---- demand multipliers (per-month MOH consumed) --------------------
    if snap.forecast and avg > 0:
        demand_moh = [(m / avg) for m in snap.forecast.monthly[:horizon]]
    else:
        demand_moh = [1.0] * horizon   # flat: identical to the workbook

    proj = _project_moh(current_moh, demand_moh, incoming_moh)
    proj_m4 = proj[min(cfg.air_lead_months, horizon) - 1] if proj else current_moh
    proj_m6 = proj[min(cfg.sea_lead_months, horizon) - 1] if proj else current_moh

    # ---- baseline (workbook flat) projection for comparison -------------
    base_proj = _project_moh(current_moh, [1.0] * horizon, incoming_moh)
    base_m4 = base_proj[min(cfg.air_lead_months, horizon) - 1]
    base_m6 = base_proj[min(cfg.sea_lead_months, horizon) - 1]

    is_domestic = (p.source == SOURCE_DOMESTIC)

    if is_domestic:
        # MOQ-driven: order one MOQ when MOH < trigger, no sea/air split.
        moq = p.moq or 0
        order = moq if current_moh < cfg.domestic_moq_trigger_moh else 0
        sea_qty, air_qty = float(order), 0.0
        base_sea_qty, base_air_qty = float(order), 0.0
        reason = (f"Domestic ({p.category}); MOH {current_moh:.1f} < "
                  f"{cfg.domestic_moq_trigger_moh:g} -> order MOQ {moq}"
                  if order else
                  f"Domestic; MOH {current_moh:.1f} >= trigger, no order")
    else:
        # SEA: refill to target at month 6.
        sea_months = max(0.0, target - proj_m6)
        sea_qty = sea_months * avg
        # AIR: cover near-term floor breach at month 4.
        air_months = max(0.0, cfg.air_nearterm_floor_moh - proj_m4)
        air_qty = air_months * avg
        # baseline equivalents
        base_sea_qty = max(0.0, target - base_m6) * avg
        base_air_qty = max(0.0, cfg.air_nearterm_floor_moh - base_m4) * avg
        if air_qty > 0:
            weeks = round((cfg.air_nearterm_floor_moh - proj_m4) * 4.345)
            reason = (f"Projected MOH at month {cfg.air_lead_months} is "
                      f"{proj_m4:.1f}, below the {cfg.air_nearterm_floor_moh:g}-month "
                      f"floor (~{weeks} wks short); sea container only lands "
                      f"month {cfg.sea_lead_months}. Air-cover the gap.")
        else:
            reason = (f"No air needed: MOH at month {cfg.air_lead_months} "
                      f"({proj_m4:.1f}) stays above the "
                      f"{cfg.air_nearterm_floor_moh:g}-month floor.")

    sea_round = ceil_to_case(sea_qty, case)
    air_round = ceil_to_case(air_qty, case)
    base_sea_round = ceil_to_case(base_sea_qty, case)
    base_air_round = ceil_to_case(base_air_qty, case)

    # ---- "with planned order" projection: re-run the same forward model but
    # add the suggested air arriving at the air-lead month and the suggested
    # sea arriving at the sea-lead month, so the coverage shown reflects the
    # plan the buyer is about to place.
    arrivals = list(incoming_moh)
    ai, si = cfg.air_lead_months - 1, cfg.sea_lead_months - 1
    if avg > 0:
        if 0 <= ai < horizon:
            arrivals[ai] += air_round / avg
        if 0 <= si < horizon:
            arrivals[si] += sea_round / avg
    proj_with_order = _project_moh(current_moh, demand_moh, arrivals)

    margin = p.retail_price - p.cost
    air_ship_cost = air_round * p.cost
    profit_lost = margin * air_round

    fc = snap.forecast
    _units = snap.units_sold or 0.0          # defensive: never None
    _onhand = snap.on_hand or 0.0
    return Suggestion(
        global_sku=p.global_sku, name=p.name, us_sku=p.us_sku,
        category=p.category, source=p.source,
        avg_monthly_sales=round(avg, 3),
        sell_through=round((_units / (_units + _onhand))
                           if (_units + _onhand) > 0 else 0.0, 3),
        units_sold=round(_units, 1),
        months_active=snap.months_active or 0,
        forecast_monthly=[round(m, 2) for m in (fc.monthly if fc else [avg] * horizon)],
        forecast_mean=round(fc.forecast_mean if fc else avg, 3),
        baseline_monthly_sales=round(fc.baseline if fc else avg, 3),
        forecast_method=fc.method if fc else "flat_avg",
        forecast_confidence=fc.confidence if fc else "low",
        diverges_from_baseline=fc.diverges_from_baseline if fc else False,
        on_hand=round(snap.on_hand, 2),
        current_moh=round(current_moh, 3),
        incoming_units_by_month=[round(u, 1) for u in inc_units],
        projected_moh=[round(x, 3) for x in proj],
        projected_moh_m4=round(proj_m4, 3),
        projected_moh_m6=round(proj_m6, 3),
        projected_moh_with_order=[round(x, 3) for x in proj_with_order],
        forecast_history_months=(fc.n_history_months if fc else 0),
        target_moh=target,
        case_size=case,
        suggested_sea_qty=round(sea_qty, 2),
        suggested_air_qty=round(air_qty, 2),
        suggested_sea_round=sea_round,
        suggested_air_round=air_round,
        baseline_sea_round=base_sea_round,
        baseline_air_round=base_air_round,
        unit_cost=round(p.cost, 4),
        retail_price=round(p.retail_price, 2),
        margin=round(margin, 4),
        air_shipping_cost=round(air_ship_cost, 2),
        profit_lost_by_air=round(profit_lost, 2),
        compliance_flag=p.compliance_flag,
        air_split_reason=reason,
        notes=list(fc.notes) if fc else [],
    )


def suggest_all(snapshots: List[SkuSnapshot], cfg: EngineConfig) -> List[Suggestion]:
    return [suggest_one(s, cfg) for s in snapshots]
