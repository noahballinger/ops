"""
Cached-Odoo sync (the skubot pattern).

Instead of fetching Odoo on demand, a background loop periodically snapshots
Odoo into the local SQLite DB; the app then reads from that local cache. This
keeps the tool fast and usable even when Odoo is briefly down, and makes
"freshness" an explicit, observable property.

Three guarantees carried over from skubot:
  1. SELF-HEALING, NOT SELF-DESTRUCTING — a failed or empty Odoo pull never
     overwrites the last good snapshot. We record the failure and keep serving
     the previous cache. (The single most important safety property.)
  2. EXPLICIT STALENESS — `stale_factor × interval`; data older than that is
     flagged stale/degraded so the UI and health checks can react.
  3. READ-ONLY to Odoo — enforced in the client's method allow-list.

The app reads the cache via `latest_cached_pull()`; orders can be built from it
even with Odoo offline.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from .config import EngineConfig, load_config
from .datasources.base import PullResult
from .db import get_session
from .models import InventorySnapshot, InTransit, Product, SyncState
from .service import persist_pull

# Cadence / staleness, configurable via env.
SYNC_SECONDS = int(os.environ.get("ODOO_SYNC_SECONDS", "600"))        # 10 min
STALE_FACTOR = float(os.environ.get("ODOO_SYNC_STALE_FACTOR", "2"))   # 2x interval


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state(session) -> SyncState:
    st = session.get(SyncState, 1)
    if not st:
        st = SyncState(id=1)
        session.add(st)
        session.commit()
        session.refresh(st)
    return st


def run_one_sync(session, datasource, cfg: Optional[EngineConfig] = None) -> SyncState:
    """Pull once and snapshot into the cache, honouring the self-healing rule.

    `datasource` is any object with `.pull()` returning a PullResult (the live
    Odoo client in production; a fake in tests)."""
    cfg = cfg or load_config()
    st = _state(session)
    st.last_attempt_at = _now()
    try:
        pull: PullResult = datasource.pull()
    except Exception as e:
        # Network/auth error -> keep the last good snapshot, mark degraded.
        st.status = "degraded"
        st.last_error = f"pull raised: {e}"
        session.add(st)
        session.commit()
        return st

    # Self-healing guard: refuse to overwrite good data with an empty/failed pull.
    if not pull.products or not pull.snapshots:
        st.status = "degraded"
        st.last_error = ("empty pull (" +
                         (pull.warnings[0] if pull.warnings else "no products") +
                         ") — kept previous cache")
        session.add(st)
        session.commit()
        return st

    # Good pull -> persist a fresh batch and point the cache at it.
    batch_id = persist_pull(session, pull, cfg)
    st.good_batch_id = batch_id
    st.last_success_at = _now()
    st.status = "ok"
    st.source = pull.source
    st.products = len(pull.products)
    st.snapshots = len(pull.snapshots)
    st.in_transit = len(pull.in_transit)
    st.last_error = ""
    session.add(st)
    session.commit()
    _prune_old_batches(session, keep=KEEP_BATCHES)
    return st


# Snapshot batches accumulate one set of rows per sync; keep only the most
# recent few so the cache doesn't grow without bound. Batches referenced by a
# saved order are always kept (the order froze that snapshot).
KEEP_BATCHES = int(os.environ.get("ODOO_KEEP_BATCHES", "3"))


def _prune_old_batches(session, keep: int = 3) -> None:
    from .models import Order, DemandForecast
    batches = [b for b in session.exec(
        select(InventorySnapshot.batch_id).distinct()).all() if b]
    # newest-first by the max snapshot id in each batch
    order_key = {}
    for b in batches:
        mx = session.exec(select(InventorySnapshot.id).where(
            InventorySnapshot.batch_id == b).order_by(
            InventorySnapshot.id.desc())).first()
        order_key[b] = mx or 0
    ranked = sorted(batches, key=lambda b: order_key[b], reverse=True)
    keep_set = set(ranked[:max(keep, 1)])
    # never delete a batch an order depends on, nor the current good batch
    st = session.get(SyncState, 1)
    if st and st.good_batch_id:
        keep_set.add(st.good_batch_id)
    keep_set.update(o.snapshot_batch_id for o in session.exec(
        select(Order)).all() if o.snapshot_batch_id)
    drop = [b for b in batches if b not in keep_set]
    if not drop:
        return
    for model in (InventorySnapshot, InTransit, DemandForecast):
        if hasattr(model, "batch_id"):
            for row in session.exec(select(model).where(
                    model.batch_id.in_(drop))).all():
                session.delete(row)
    session.commit()


def latest_cached_pull(session) -> Optional[PullResult]:
    """Reconstruct a PullResult from the last GOOD cached snapshot, so an order
    can be built from cache without touching Odoo."""
    st = _state(session)
    if not st.good_batch_id:
        return None
    bid = st.good_batch_id
    snaps = session.exec(select(InventorySnapshot).where(
        InventorySnapshot.batch_id == bid)).all()
    if not snaps:
        return None
    skus = {s.global_sku for s in snaps}
    products = session.exec(select(Product)).all()
    intransit = session.exec(select(InTransit).where(InTransit.batch_id == bid)).all()
    pull = PullResult(source="odoo_cache")
    pull.products = [{
        "global_sku": p.global_sku, "us_sku": p.us_sku,
        "odoo_internal_ref": p.odoo_internal_ref, "name": p.name,
        "category": p.category, "case_size": p.case_size,
        "unit_weight": p.unit_weight, "hsn_code": p.hsn_code, "cost": p.cost,
        "retail_price": p.retail_price, "compliance_flag": p.compliance_flag,
        "source": p.source, "expiry_tracked": p.expiry_tracked, "moq": p.moq,
        "target_moh_override": p.target_moh_override, "vendor": p.vendor,
    } for p in products if p.global_sku in skus]
    pull.snapshots = [{
        "global_sku": s.global_sku, "on_hand": s.on_hand,
        "useable_on_hand": s.useable_on_hand,
        "avg_monthly_sales": s.avg_monthly_sales,
        "units_sold": s.units_sold, "months_active": s.months_active,
        "monthly_sales_series": s.monthly_sales_series, "source": s.source,
    } for s in snaps]
    pull.in_transit = [{
        "global_sku": it.global_sku, "quantity": it.quantity,
        "expected_arrival_month": it.expected_arrival_month,
        "shipment_label": it.shipment_label,
    } for it in intransit]
    return pull, bid


def cache_status(session) -> dict:
    """Freshness + health for /api/odoo/status and health checks."""
    st = _state(session)
    age = None
    stale = None
    if st.last_success_at:
        last = st.last_success_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = (_now() - last).total_seconds()
        stale = age > (STALE_FACTOR * SYNC_SECONDS)
    return {
        "status": st.status,
        "last_attempt_at": st.last_attempt_at.isoformat() if st.last_attempt_at else None,
        "last_success_at": st.last_success_at.isoformat() if st.last_success_at else None,
        "good_batch_id": st.good_batch_id,
        "cached_products": st.products,
        "cached_snapshots": st.snapshots,
        "cached_in_transit": st.in_transit,
        "age_seconds": round(age) if age is not None else None,
        "is_stale": stale,
        "sync_interval_seconds": SYNC_SECONDS,
        "stale_factor": STALE_FACTOR,
        "last_error": st.last_error,
        "healthy": st.status == "ok" and not stale,
    }
