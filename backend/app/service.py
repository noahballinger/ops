"""
Service layer -- the only place I/O (DB) and the pure engine meet.

Flow:  DataSource.pull()  ->  build SkuSnapshots  ->  forecast each SKU
       ->  suggest_all()  ->  persist + return rows for the review screen.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Dict, List, Optional

from sqlmodel import select

from .config import EngineConfig, load_config
from .datasources.base import PullResult
from .engine import (ProductInput, SkuSnapshot, Suggestion, suggest_all,
                     suggest_one)
from .forecasting import (Forecast, MonthlySalesSeries, MonthPoint,
                          forecast_demand)
from .models import (DemandForecast, InTransit, InventorySnapshot, Order,
                     OrderLine, Product, OrderLineEvent)


def log_event(session, order_id: int, type: str, note: str = "",
              global_sku: str = "", order_line_id=None, actor: str = "system") -> None:
    """Append an immutable lifecycle event (the timeline is built from these)."""
    session.add(OrderLineEvent(order_id=order_id, order_line_id=order_line_id,
                               type=type, note=note, actor=actor,
                               source_quote=global_sku))
    session.commit()


# --------------------------------------------------------------------------
# Pull -> engine inputs
# --------------------------------------------------------------------------
def _forecast_for(snap_dict: dict, cfg: EngineConfig) -> Optional[Forecast]:
    series = snap_dict.get("monthly_sales_series") or []
    if not series:
        # No monthly granularity -> build a flat forecast from the avg so the
        # review screen still shows baseline + low-confidence consistently.
        avg = snap_dict.get("avg_monthly_sales", 0.0)
        if avg <= 0:
            return None
        return Forecast(monthly=[avg] * cfg.horizon, method="flat_avg",
                        baseline=avg, avg_monthly=avg, confidence="low",
                        n_history_months=0, low_data=True, uncertainty_pct=0.5,
                        diverges_from_baseline=False,
                        notes=["No monthly history; using flat annual/12 baseline."])
    mss = MonthlySalesSeries(points=[
        MonthPoint(year=int(p["year"]), month=int(p["month"]),
                   units=float(p["units"]), is_stockout=bool(p.get("is_stockout")))
        for p in series])
    return forecast_demand(mss, cfg.horizon, cfg.forecast)


def build_inputs(pull: PullResult, cfg: EngineConfig
                 ) -> tuple[List[SkuSnapshot], Dict[str, Forecast]]:
    prod_by_sku = {p["global_sku"]: p for p in pull.products}
    # in-transit -> per-sku list indexed by arrival month 1..horizon
    inc: Dict[str, List[float]] = {}
    for it in pull.in_transit:
        g = it["global_sku"]
        arr = inc.setdefault(g, [0.0] * cfg.horizon)
        m = int(it.get("expected_arrival_month", 0))
        if 1 <= m <= cfg.horizon:
            arr[m - 1] += float(it.get("quantity", 0.0))

    snaps: List[SkuSnapshot] = []
    forecasts: Dict[str, Forecast] = {}
    snap_by_sku = {sd["global_sku"]: sd for sd in pull.snapshots}  # dedupe
    for g, sd in snap_by_sku.items():
        p = prod_by_sku.get(g)
        if not p:
            continue
        fc = _forecast_for(sd, cfg)
        if fc:
            forecasts[g] = fc
        pi = ProductInput(
            global_sku=g, name=p.get("name", ""), us_sku=p.get("us_sku", ""),
            odoo_ref=p.get("odoo_internal_ref", ""), category=p.get("category", ""),
            case_size=p.get("case_size") or None, unit_weight=p.get("unit_weight"),
            hsn_code=p.get("hsn_code", ""), cost=p.get("cost", 0.0),
            retail_price=p.get("retail_price", 0.0),
            source=p.get("source", "IMPORT_SEA"),
            expiry_tracked=p.get("expiry_tracked", False),
            compliance_flag=p.get("compliance_flag", ""),
            target_moh_override=p.get("target_moh_override"),
            moq=p.get("moq"))
        snaps.append(SkuSnapshot(
            product=pi, on_hand=sd.get("useable_on_hand", sd.get("on_hand", 0.0)),
            avg_monthly_sales=sd.get("avg_monthly_sales", 0.0),
            incoming_units_by_month=inc.get(g, [0.0] * cfg.horizon),
            forecast=fc,
            units_sold=sd.get("units_sold", sum(d.get("units", 0) for d in (sd.get("monthly_sales_series") or []))),
            months_active=sd.get("months_active", len(sd.get("monthly_sales_series") or []))))
    return snaps, forecasts


def compute_suggestions(pull: PullResult, cfg: Optional[EngineConfig] = None
                        ) -> List[Suggestion]:
    cfg = cfg or load_config()
    snaps, _ = build_inputs(pull, cfg)
    return suggest_all(snaps, cfg)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------
def persist_pull(session, pull: PullResult, cfg: EngineConfig) -> str:
    batch_id = uuid.uuid4().hex[:12]
    snaps, forecasts = build_inputs(pull, cfg)
    # upsert products (dedupe within the batch: last row wins, and reuse the
    # same object so two rows with the same global_sku never double-insert)
    existing = {p.global_sku: p for p in session.exec(select(Product)).all()}
    seen_products = {}
    for p in pull.products:
        g = p["global_sku"]
        obj = existing.get(g)
        if obj is None:
            obj = seen_products.get(g) or Product(global_sku=g)
        seen_products[g] = obj
        # keep an existing buyer-set compliance flag if the source has none
        flag = p.get("compliance_flag") or (obj.compliance_flag if obj else "")
        obj.us_sku = p.get("us_sku", ""); obj.odoo_internal_ref = p.get("odoo_internal_ref", "")
        obj.barcode = p.get("barcode", "")
        obj.name = p.get("name", ""); obj.category = p.get("category", "")
        obj.case_size = p.get("case_size") or 1; obj.unit_weight = p.get("unit_weight")
        obj.hsn_code = p.get("hsn_code", ""); obj.cost = p.get("cost", 0.0)
        obj.retail_price = p.get("retail_price", 0.0); obj.compliance_flag = flag
        obj.source = p.get("source", "IMPORT_SEA"); obj.expiry_tracked = p.get("expiry_tracked", False)
        obj.moq = p.get("moq"); obj.target_moh_override = p.get("target_moh_override")
        obj.vendor = p.get("vendor", "")
        obj.origin = p.get("origin", ""); obj.odoo_id = p.get("odoo_id")
        session.add(obj)
    # snapshots + forecasts
    for sd in pull.snapshots:
        series = sd.get("monthly_sales_series") or []
        units_sold = sd.get("units_sold", sum(d.get("units", 0) for d in series))
        months_active = sd.get("months_active", len(series))
        on_hand = sd.get("on_hand", 0.0) or 0.0
        sell_through = round(units_sold / (units_sold + on_hand), 4) \
            if (units_sold + on_hand) > 0 else 0.0
        snap = InventorySnapshot(
            global_sku=sd["global_sku"], on_hand=sd.get("on_hand", 0.0),
            useable_on_hand=sd.get("useable_on_hand"),
            monthly_sales_series=series,
            avg_monthly_sales=sd.get("avg_monthly_sales", 0.0),
            units_sold=units_sold, months_active=months_active,
            sell_through=sell_through,
            source=sd.get("source", "file_import"), batch_id=batch_id)
        session.add(snap)
        fc = forecasts.get(sd["global_sku"])
        if fc:
            session.add(DemandForecast(
                global_sku=sd["global_sku"], monthly=fc.monthly, method=fc.method,
                confidence=fc.confidence, low_data=fc.low_data, baseline=fc.baseline,
                uncertainty_pct=fc.uncertainty_pct,
                diverges_from_baseline=fc.diverges_from_baseline, notes=fc.notes,
                batch_id=batch_id))
    for it in pull.in_transit:
        session.add(InTransit(
            global_sku=it["global_sku"], quantity=it.get("quantity", 0.0),
            expected_arrival_month=int(it.get("expected_arrival_month", 0)),
            shipment_label=it.get("shipment_label", ""), batch_id=batch_id))
    session.commit()
    return batch_id


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
def create_order(session, name: str, pull: PullResult, cfg: EngineConfig,
                 batch_id: Optional[str] = None) -> Order:
    # If batch_id is supplied the snapshot was already persisted (e.g. by the
    # background Odoo sync) -- don't re-persist, just read it.
    if batch_id is None:
        batch_id = persist_pull(session, pull, cfg)
    suggestions = compute_suggestions(pull, cfg)
    order = Order(name=name, status="draft", snapshot_batch_id=batch_id,
                  config_json={"sea_lead_months": cfg.sea_lead_months,
                               "air_lead_months": cfg.air_lead_months,
                               "air_nearterm_floor_moh": cfg.air_nearterm_floor_moh})
    session.add(order)
    session.commit()
    session.refresh(order)
    for s in suggestions:
        line = OrderLine(
            order_id=order.id, global_sku=s.global_sku,
            suggested_sea_qty=s.suggested_sea_round, suggested_air_qty=s.suggested_air_round,
            baseline_sea_qty=s.baseline_sea_round, baseline_air_qty=s.baseline_air_round,
            final_sea_qty=s.suggested_sea_round, final_air_qty=s.suggested_air_round,
            origin_sea_qty=s.suggested_sea_round, origin_air_qty=s.suggested_air_round,
            target_moh_used=s.target_moh, case_size=s.case_size,
            method="air" if s.suggested_air_round and not s.suggested_sea_round else "sea",
            projection_json=s.projected_moh, suggestion_json=_sugg_to_dict(s))
        session.add(line)
    session.commit()
    n = sum(1 for s in suggestions if s.suggested_sea_round or s.suggested_air_round)
    log_event(session, order.id, "created",
              f"Order created from {pull.source}: {len(suggestions)} candidates, "
              f"{n} to order.", actor="buyer")
    return order


def update_override(session, order_id: int, global_sku: str,
                    final_sea_qty: Optional[int] = None,
                    final_air_qty: Optional[int] = None) -> Optional[OrderLine]:
    line = session.exec(select(OrderLine).where(
        OrderLine.order_id == order_id, OrderLine.global_sku == global_sku)).first()
    if not line:
        return None
    changes = []
    if final_sea_qty is not None and int(final_sea_qty) != line.final_sea_qty:
        changes.append(f"sea {line.final_sea_qty}→{int(final_sea_qty)}")
        line.final_sea_qty = int(final_sea_qty)
    if final_air_qty is not None and int(final_air_qty) != line.final_air_qty:
        changes.append(f"air {line.final_air_qty}→{int(final_air_qty)}")
        line.final_air_qty = int(final_air_qty)
    session.add(line)
    session.commit()
    if changes:
        log_event(session, order_id, "quantity_changed", "; ".join(changes),
                  global_sku=global_sku, order_line_id=line.id, actor="buyer")
    return line


def export_rows(session, order_id: int) -> List[dict]:
    order = session.get(Order, order_id)
    if not order:
        return []
    rows = []
    for line in session.exec(select(OrderLine).where(OrderLine.order_id == order_id)).all():
        p = session.get(Product, line.global_sku)
        if line.final_sea_qty == 0 and line.final_air_qty == 0:
            continue   # only export lines actually being ordered
        s = line.suggestion_json or {}
        rows.append({
            "us_sku": p.us_sku if p else "", "name": p.name if p else "",
            "global_sku": line.global_sku, "category": p.category if p else "",
            "final_sea_qty": line.final_sea_qty, "final_air_qty": line.final_air_qty,
            "unit_weight": p.unit_weight if p else None,
            "unit_cost": p.cost if p else 0.0, "retail_price": p.retail_price if p else 0.0,
            "margin": s.get("margin", (p.retail_price - p.cost) if p else 0.0),
            "hsn_code": p.hsn_code if p else "",
            "air_shipping_cost": round((p.cost if p else 0.0) * line.final_air_qty, 2),
            "profit_lost_by_air": round(s.get("margin", 0.0) * line.final_air_qty, 2),
            "target_moh": line.target_moh_used, "case_size": line.case_size,
            "compliance_flag": p.compliance_flag if p else "",
            "source": p.source if p else ""})
    rows.sort(key=lambda r: (r["category"], r["us_sku"]))
    return rows


def _sugg_to_dict(s: Suggestion) -> dict:
    from dataclasses import asdict
    return asdict(s)


def place_order(session, order_id: int) -> dict:
    """Mark an order placed and send the order emails: one India PO to the
    configured recipients, plus a separate per-vendor email (item name + qty)
    for any US-vendor lines. Falls back to the stub mailer until Gmail is set."""
    from .models import OrderListItem, Vendor, AppSetting  # noqa
    from . import catalog
    from .mailer import (get_provider, compose_order_email, compose_vendor_email)
    order = session.get(Order, order_id)
    if not order:
        return {"error": "order not found"}
    settings = catalog.get_settings(session)
    cc = [e.strip() for e in (settings.get("email_cc") or "").split(",") if e.strip()]

    # join lines -> product + order-list (channel/vendor)
    oli = {r.global_sku: r for r in session.exec(select(OrderListItem)).all()}
    vendors = {v.id: v for v in session.exec(select(Vendor)).all()}
    india_rows, by_vendor = [], {}
    for line in session.exec(select(OrderLine).where(OrderLine.order_id == order_id)).all():
        qty = line.final_sea_qty + line.final_air_qty
        if qty <= 0:
            continue
        p = session.get(Product, line.global_sku)
        item = oli.get(line.global_sku)
        channel = item.channel if item else "INDIA_IMPORT"
        row = {"us_sku": p.us_sku if p else "", "name": p.name if p else "",
               "global_sku": line.global_sku, "hsn_code": p.hsn_code if p else "",
               "case_size": line.case_size, "final_sea_qty": line.final_sea_qty,
               "final_air_qty": line.final_air_qty, "qty": qty,
               "vendor": vendors.get(item.vendor_id).name if (item and item.vendor_id in vendors) else ""}
        if channel == "US_VENDOR" and item and item.vendor_id in vendors:
            by_vendor.setdefault(item.vendor_id, []).append(row)
        else:
            india_rows.append(row)

    provider = get_provider()
    sent = []

    if india_rows:
        recips = [e.strip() for e in (settings.get("india_order_recipients") or "").split(",") if e.strip()]
        subject, html = compose_order_email(order.name, india_rows)
        if recips:
            for to in recips:
                res = provider.send(session, to, subject, html, kind="order_placement")
                sent.append({"to": to, "kind": "india", "sent": res.get("sent")})
        else:
            sent.append({"to": "(no india recipients set)", "kind": "india", "sent": False})

    for vid, rows in by_vendor.items():
        v = vendors[vid]
        subject, html = compose_vendor_email(order.name, v.name, rows)
        if v.contact_email:
            res = provider.send(session, v.contact_email, subject, html, kind="order_vendor")
            sent.append({"to": v.contact_email, "kind": "vendor:"+v.name, "sent": res.get("sent")})
        else:
            sent.append({"to": "(no email for "+v.name+")", "kind": "vendor:"+v.name, "sent": False})

    order.status = "placed"
    session.add(order); session.commit()
    log_event(session, order_id, "placed",
              f"Order placed; {len(sent)} email(s) dispatched.", actor="buyer")
    return {"status": "placed", "emails": sent, "provider": provider.name}
