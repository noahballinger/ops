"""
Cached-Odoo sync tests — the skubot guarantees:
  * a good pull populates the cache;
  * a FAILED pull (raises) keeps the last good snapshot (self-healing);
  * an EMPTY pull keeps the last good snapshot (self-healing);
  * staleness is computed from stale_factor x interval.
Uses an in-memory FakeOdoo — no network.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

os.environ.setdefault("ISHA_DB_PATH", "/tmp/isha_sync_test.db")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlmodel import select, delete
from app.db import init_db, get_session
from app.datasources.base import PullResult
from app.models import SyncState, InventorySnapshot, InTransit, Product
from app import sync as syncmod


class FakeOdoo:
    """Returns a canned PullResult, or raises, or returns empty — on demand."""
    def __init__(self, mode="good"):
        self.mode = mode

    def pull(self):
        if self.mode == "raise":
            raise ConnectionError("Odoo unreachable")
        if self.mode == "empty":
            r = PullResult(source="odoo_live")
            r.warnings.append("sale.report returned nothing")
            return r
        r = PullResult(source="odoo_live")
        r.products = [{"global_sku": "AA1", "us_sku": "EX1", "name": "Widget",
                       "category": "ACCESSORY", "case_size": 1, "cost": 2.0,
                       "retail_price": 5.0, "source": "IMPORT_SEA",
                       "odoo_internal_ref": "AA1", "hsn_code": "", "vendor": "",
                       "expiry_tracked": False, "moq": None,
                       "compliance_flag": "", "unit_weight": None,
                       "target_moh_override": None}]
        r.snapshots = [{"global_sku": "AA1", "on_hand": 100,
                        "useable_on_hand": 100, "avg_monthly_sales": 50.0,
                        "monthly_sales_series": [], "source": "odoo_live"}]
        r.in_transit = [{"global_sku": "AA1", "quantity": 60,
                         "expected_arrival_month": 2, "shipment_label": "Q3"}]
        return r


def _clean(s):
    for m in (SyncState, InventorySnapshot, InTransit, Product):
        s.exec(delete(m))
    s.commit()


def setup_function():
    init_db()
    with get_session() as s:
        _clean(s)


def test_good_pull_populates_cache():
    with get_session() as s:
        st = syncmod.run_one_sync(s, FakeOdoo("good"))
        assert st.status == "ok"
        assert st.good_batch_id
        assert st.products == 1 and st.snapshots == 1
        cached = syncmod.latest_cached_pull(s)
        assert cached is not None
        pull, bid = cached
        assert pull.products[0]["global_sku"] == "AA1"
        assert pull.in_transit[0]["quantity"] == 60


def test_failed_pull_keeps_last_good_snapshot():
    """Self-healing: a raising pull must NOT wipe the cache."""
    with get_session() as s:
        good = syncmod.run_one_sync(s, FakeOdoo("good"))
        good_batch = good.good_batch_id
        st = syncmod.run_one_sync(s, FakeOdoo("raise"))
        assert st.status == "degraded"
        assert st.good_batch_id == good_batch          # unchanged
        assert "raise" in st.last_error.lower() or "unreachable" in st.last_error.lower()
        # cache still serves the previous good data
        pull, bid = syncmod.latest_cached_pull(s)
        assert bid == good_batch and pull.products[0]["global_sku"] == "AA1"


def test_empty_pull_keeps_last_good_snapshot():
    with get_session() as s:
        good = syncmod.run_one_sync(s, FakeOdoo("good"))
        good_batch = good.good_batch_id
        st = syncmod.run_one_sync(s, FakeOdoo("empty"))
        assert st.status == "degraded"
        assert st.good_batch_id == good_batch          # unchanged
        pull, bid = syncmod.latest_cached_pull(s)
        assert bid == good_batch


def test_staleness_flag():
    with get_session() as s:
        syncmod.run_one_sync(s, FakeOdoo("good"))
        status = syncmod.cache_status(s)
        assert status["is_stale"] is False and status["healthy"] is True
        # backdate last success beyond stale_factor x interval
        st = s.get(SyncState, 1)
        st.last_success_at = datetime.now(timezone.utc) - timedelta(
            seconds=syncmod.STALE_FACTOR * syncmod.SYNC_SECONDS + 60)
        s.add(st); s.commit()
        status = syncmod.cache_status(s)
        assert status["is_stale"] is True and status["healthy"] is False
