"""
US vendor ordering — frequent, per-vendor purchase orders (distinct from the
quarterly India import). Items come from the Order List (channel = US_VENDOR),
quantities are suggested as a simple reorder-to-target off the cached stock &
sales, and placing reuses the existing per-vendor email path.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlmodel import select

from .config import EngineConfig, load_config
from .engine import ceil_to_case
from .models import Vendor, OrderListItem, Product, Order, OrderLine
from . import catalog
from .service import place_order, log_event


def _us_target_months(session) -> float:
    try:
        return float(catalog.get_settings(session).get("us_target_months") or 2)
    except Exception:
        return 2.0


def _cache_maps(session):
    """sku -> (on_hand, avg_monthly_sales) from the latest good Odoo snapshot."""
    from .sync import latest_cached_pull
    cached = latest_cached_pull(session)
    if not cached:
        return {}, {}
    pull, _ = cached
    stock = {s["global_sku"]: s for s in pull.snapshots}
    prods = {p["global_sku"]: p for p in pull.products}
    return stock, prods


def vendor_overview(session) -> List[dict]:
    """US vendors with their count of active US_VENDOR order-list items."""
    counts = {}
    for it in session.exec(select(OrderListItem).where(
            OrderListItem.channel == "US_VENDOR", OrderListItem.active == True)).all():  # noqa: E712
        if it.vendor_id:
            counts[it.vendor_id] = counts.get(it.vendor_id, 0) + 1
    out = []
    for v in session.exec(select(Vendor)).all():
        if v.kind == "US" or v.id in counts:
            out.append({"id": v.id, "name": v.name, "contact_email": v.contact_email,
                        "items": counts.get(v.id, 0)})
    out.sort(key=lambda r: r["name"].lower())
    return out


def vendor_items(session, vendor_id: int, cfg: Optional[EngineConfig] = None) -> dict:
    cfg = cfg or load_config()
    v = session.get(Vendor, vendor_id)
    if not v:
        return {"error": "vendor not found"}
    target = _us_target_months(session)
    stock, prods = _cache_maps(session)
    items = []
    for it in session.exec(select(OrderListItem).where(
            OrderListItem.channel == "US_VENDOR", OrderListItem.vendor_id == vendor_id,
            OrderListItem.active == True)).all():  # noqa: E712
        p = prods.get(it.global_sku) or {}
        prod = session.get(Product, it.global_sku)
        st = stock.get(it.global_sku, {})
        on_hand = float(st.get("on_hand", 0) or 0)
        avg = float(st.get("avg_monthly_sales", 0) or 0)
        moh = (on_hand / avg) if avg > 0 else (999.0 if on_hand > 0 else 0.0)
        case = (prod.case_size if prod else 1) or 1
        raw = max(0.0, target - moh) * avg
        qty = ceil_to_case(raw, case)
        if it.moq and 0 < qty < it.moq:
            qty = it.moq
        items.append({
            "global_sku": it.global_sku,
            "name": (prod.name if prod else "") or p.get("name", ""),
            "us_sku": (prod.us_sku if prod else "") or "",
            "on_hand": round(on_hand), "avg_monthly_sales": round(avg, 2),
            "moh": round(moh, 2), "case_size": case, "moq": it.moq,
            "suggested_qty": qty,
        })
    items.sort(key=lambda r: (-(r["suggested_qty"]), r["name"]))
    return {"vendor": {"id": v.id, "name": v.name, "contact_email": v.contact_email,
                       "target_months": target}, "items": items}


def all_items(session, cfg: Optional[EngineConfig] = None) -> dict:
    """Every active US_VENDOR order-list item across all vendors (item-first),
    with vendor, cached stock and a suggested reorder-to-target quantity."""
    cfg = cfg or load_config()
    target = _us_target_months(session)
    stock, _ = _cache_maps(session)
    vendors = {v.id: v for v in session.exec(select(Vendor)).all()}
    items = []
    for it in session.exec(select(OrderListItem).where(
            OrderListItem.channel == "US_VENDOR", OrderListItem.active == True)).all():  # noqa: E712
        prod = session.get(Product, it.global_sku)
        st = stock.get(it.global_sku, {})
        on_hand = float(st.get("on_hand", 0) or 0)
        avg = float(st.get("avg_monthly_sales", 0) or 0)
        moh = (on_hand / avg) if avg > 0 else (999.0 if on_hand > 0 else 0.0)
        case = (prod.case_size if prod else 1) or 1
        qty = ceil_to_case(max(0.0, target - moh) * avg, case)
        if it.moq and 0 < qty < it.moq:
            qty = it.moq
        v = vendors.get(it.vendor_id)
        items.append({
            "global_sku": it.global_sku, "name": (prod.name if prod else ""),
            "us_sku": (prod.us_sku if prod else ""), "category": (prod.category if prod else ""),
            "vendor_id": it.vendor_id, "vendor_name": v.name if v else "(no vendor)",
            "vendor_email": v.contact_email if v else "",
            "on_hand": round(on_hand), "moh": round(moh, 2), "case_size": case,
            "moq": it.moq, "suggested_qty": qty})
    items.sort(key=lambda r: (r["vendor_name"].lower(), -(r["suggested_qty"]), r["name"]))
    return {"target_months": target, "items": items}


def create_and_place_all(session, lines: dict, name: str = "") -> dict:
    """Create ONE US order from {sku: qty} spanning vendors, then place it —
    place_order groups lines by vendor and emails each separately."""
    lines = {k: int(q) for k, q in (lines or {}).items() if int(q or 0) > 0}
    if not lines:
        return {"error": "no quantities to order"}
    name = name or f"US Orders · {datetime.utcnow().date().isoformat()}"
    order = Order(name=name, status="draft", config_json={"channel": "US_VENDOR"})
    session.add(order); session.commit(); session.refresh(order)
    for sku, qty in lines.items():
        prod = session.get(Product, sku)
        session.add(OrderLine(order_id=order.id, global_sku=sku,
                              suggested_sea_qty=qty, final_sea_qty=qty, origin_sea_qty=qty,
                              case_size=(prod.case_size if prod else 1) or 1, method="vendor",
                              suggestion_json={"us_sku": prod.us_sku if prod else "",
                                               "name": prod.name if prod else ""}))
    session.commit()
    log_event(session, order.id, "created",
              f"US vendor order: {len(lines)} items.", actor="buyer")
    res = place_order(session, order.id)
    res["order_id"] = order.id; res["order_name"] = name
    return res


def create_and_place(session, vendor_id: int, lines: dict, name: str = "") -> dict:
    """Create a US vendor order from {sku: qty} and place it (emails the vendor)."""
    v = session.get(Vendor, vendor_id)
    if not v:
        return {"error": "vendor not found"}
    lines = {k: int(q) for k, q in (lines or {}).items() if int(q or 0) > 0}
    if not lines:
        return {"error": "no quantities to order"}
    name = name or f"{v.name} · {datetime.utcnow().date().isoformat()}"
    order = Order(name=name, status="draft", config_json={"channel": "US_VENDOR",
                                                          "vendor_id": vendor_id})
    session.add(order); session.commit(); session.refresh(order)
    for sku, qty in lines.items():
        prod = session.get(Product, sku)
        session.add(OrderLine(order_id=order.id, global_sku=sku,
                              suggested_sea_qty=qty, final_sea_qty=qty,
                              origin_sea_qty=qty, case_size=(prod.case_size if prod else 1) or 1,
                              method="vendor",
                              suggestion_json={"us_sku": prod.us_sku if prod else "",
                                               "name": prod.name if prod else ""}))
    session.commit()
    log_event(session, order.id, "created",
              f"US vendor order for {v.name}: {len(lines)} items.", actor="buyer")
    result = place_order(session, order.id)
    result["order_id"] = order.id
    result["order_name"] = name
    return result
