"""
Phase A service layer: Vendors, the Order List master list, and Product tags.

The Order List is the source of truth for "what we reorder, from whom". The
Odoo pull's coarse allowlist file (order_list_skus.json) is *regenerated* from
the active rows here whenever the list changes, so the live sync stays in sync
with the master list without the data source needing DB access.
"""
from __future__ import annotations

import json
import os
from typing import List, Optional

from sqlmodel import select, delete

from .models import Vendor, ProductTag, OrderListItem, Product, AppSetting

# Known settings + defaults (so the UI always has a shape to render).
SETTING_DEFAULTS = {
    "india_order_recipients": "",   # csv of emails the India PO goes to
    "email_cc": "",                 # csv cc'd on all order emails
    "email_signature": "Isha Life USA Ordering",
}


def get_settings(session) -> dict:
    out = dict(SETTING_DEFAULTS)
    for row in session.exec(select(AppSetting)).all():
        out[row.key] = row.value
    return out


def set_settings(session, data: dict) -> dict:
    for k, v in data.items():
        row = session.get(AppSetting, k) or AppSetting(key=k)
        row.value = "" if v is None else str(v)
        session.add(row)
    session.commit()
    return get_settings(session)

ALLOWLIST_PATH = os.path.join(os.path.dirname(__file__), "..", "data",
                              "order_list_skus.json")


# ---------------------------------------------------------------- allowlist
def export_allowlist(session) -> int:
    """Rewrite order_list_skus.json from the active Order List rows."""
    skus = sorted({r.global_sku for r in session.exec(
        select(OrderListItem).where(OrderListItem.active == True)).all()})  # noqa: E712
    with open(ALLOWLIST_PATH, "w") as fh:
        json.dump(skus, fh)
    # bust the datasource's in-memory cache if loaded
    try:
        from .datasources import odoo_json
        odoo_json._ALLOWLIST_CACHE = set(skus)
    except Exception:
        pass
    return len(skus)


def seed_order_list_from_json(session) -> int:
    """One-time seed: if the Order List is empty, populate it from the existing
    allowlist JSON (channel INDIA_IMPORT, active)."""
    if session.exec(select(OrderListItem).limit(1)).first():
        return 0
    if not os.path.exists(ALLOWLIST_PATH):
        return 0
    with open(ALLOWLIST_PATH) as fh:
        skus = json.load(fh)
    n = 0
    for sku in skus:
        session.add(OrderListItem(global_sku=sku, channel="INDIA_IMPORT", active=True))
        n += 1
    session.commit()
    return n


# ---------------------------------------------------------------- vendors
def list_vendors(session) -> List[dict]:
    return [{"id": v.id, "name": v.name, "kind": v.kind, "country": v.country,
             "contact_name": v.contact_name, "contact_email": v.contact_email,
             "notes": v.notes, "active": v.active}
            for v in session.exec(select(Vendor).order_by(Vendor.name)).all()]


def upsert_vendor(session, data: dict) -> Vendor:
    v = session.get(Vendor, data["id"]) if data.get("id") else None
    if not v:
        v = Vendor(name=data.get("name", ""))
    for f in ("name", "kind", "country", "contact_name", "contact_email", "notes", "active"):
        if f in data:
            setattr(v, f, data[f])
    session.add(v); session.commit(); session.refresh(v)
    return v


# ---------------------------------------------------------------- tags
def get_tags(session, global_sku: str) -> List[dict]:
    return [{"id": t.id, "key": t.key, "value": t.value} for t in session.exec(
        select(ProductTag).where(ProductTag.global_sku == global_sku)).all()]


def set_tags(session, global_sku: str, tags: List[dict]) -> List[dict]:
    """Replace all tags for a SKU with the supplied list of {key,value}."""
    session.exec(delete(ProductTag).where(ProductTag.global_sku == global_sku))
    for t in tags:
        k = (t.get("key") or "").strip()
        if k:
            session.add(ProductTag(global_sku=global_sku, key=k,
                                   value=(t.get("value") or "").strip()))
    session.commit()
    return get_tags(session, global_sku)


# ---------------------------------------------------------------- order list
def list_order_list(session) -> List[dict]:
    vendors = {v.id: v.name for v in session.exec(select(Vendor)).all()}
    products = {p.global_sku: p for p in session.exec(select(Product)).all()}
    # group tags per sku
    tags: dict = {}
    for t in session.exec(select(ProductTag)).all():
        tags.setdefault(t.global_sku, []).append({"key": t.key, "value": t.value})
    out = []
    for it in session.exec(select(OrderListItem)).all():
        p = products.get(it.global_sku)
        out.append({
            "id": it.id, "global_sku": it.global_sku,
            "name": p.name if p else "", "category": p.category if p else "",
            "us_sku": p.us_sku if p else "", "origin": p.origin if p else "",
            "vendor_id": it.vendor_id, "vendor_name": vendors.get(it.vendor_id, ""),
            "channel": it.channel, "active": it.active,
            "lead_time_days": it.lead_time_days, "moq": it.moq, "notes": it.notes,
            "tags": tags.get(it.global_sku, []),
        })
    out.sort(key=lambda r: (not r["active"], r["channel"], r["name"] or r["global_sku"]))
    return out


def upsert_order_list_item(session, data: dict) -> OrderListItem:
    it = None
    if data.get("id"):
        it = session.get(OrderListItem, data["id"])
    if not it and data.get("global_sku"):
        it = session.exec(select(OrderListItem).where(
            OrderListItem.global_sku == data["global_sku"])).first()
    if not it:
        it = OrderListItem(global_sku=data["global_sku"])
    for f in ("vendor_id", "channel", "active", "lead_time_days", "moq", "notes"):
        if f in data:
            setattr(it, f, data[f])
    session.add(it); session.commit(); session.refresh(it)
    export_allowlist(session)
    return it


def delete_order_list_item(session, global_sku: str) -> bool:
    it = session.exec(select(OrderListItem).where(
        OrderListItem.global_sku == global_sku)).first()
    if not it:
        return False
    session.delete(it); session.commit()
    export_allowlist(session)
    return True
