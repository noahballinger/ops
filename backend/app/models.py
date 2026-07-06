"""
Persistence model (SQLModel / SQLite).

Part 1 entities are fully used.  The Part 2 seams (ShipmentLeg,
OrderLineEvent, EmailThread, EmailMessage) are defined now -- so order
lifecycle / email ingestion drops in later without a schema rewrite -- but
carry no logic yet.

Design intent (carried from the spec):
  * An Order is an IMMUTABLE origin snapshot + an append-only event log.
  * One Order can fan out into many ShipmentLegs (sea/air, "Q3 ADD AIR" ...),
    mirroring the workbook's INC INV labelling convention.
  * OrderLine stores origin qty, current qty and method so a later timeline
    UI can reconstruct state without schema changes.
"""
import json
from datetime import datetime, date
from typing import List, Optional

from sqlmodel import SQLModel, Field, Relationship, Column, JSON, Text


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------
class Product(SQLModel, table=True):
    global_sku: str = Field(primary_key=True)        # the one join key
    us_sku: str = ""
    barcode: str = ""
    odoo_internal_ref: str = ""
    name: str = ""
    category: str = ""
    case_size: int = 1
    unit_weight: Optional[float] = None
    hsn_code: str = ""
    cost: float = 0.0                                 # COGS / landed (LC USA)
    retail_price: float = 0.0
    compliance_flag: str = ""                         # free text; "" => clear
    source: str = "IMPORT_SEA"                        # IMPORT_SEA|IMPORT_AIR_ELIGIBLE|DOMESTIC
    expiry_tracked: bool = False
    moq: Optional[int] = None                         # domestic vendors
    target_moh_override: Optional[float] = None
    vendor: str = ""
    origin: str = ""                                  # country of origin of goods
    odoo_id: Optional[int] = None                     # Odoo product.product id (for deep-links)


# --------------------------------------------------------------------------
# Point-in-time inputs
# --------------------------------------------------------------------------
class InventorySnapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    global_sku: str = Field(index=True)
    captured_at: datetime = Field(default_factory=datetime.utcnow)
    on_hand: float = 0.0
    useable_on_hand: Optional[float] = None           # post-expiry
    # trailing monthly sales time series used for forecasting:
    #   [{"year":2025,"month":6,"units":120.0,"is_stockout":false}, ...]
    monthly_sales_series: list = Field(default_factory=list, sa_column=Column(JSON))
    avg_monthly_sales: float = 0.0
    # Demand metrics persisted as flat columns so Metabase can chart them
    # directly (rather than re-deriving from the JSON series). Sell-through =
    # units_sold / (units_sold + on_hand); velocity basis = months_active.
    units_sold: float = 0.0
    months_active: int = 0
    sell_through: float = 0.0
    source: str = "file_import"                        # odoo_live | file_import
    batch_id: str = Field(default="", index=True)      # groups one refresh


class DemandForecast(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    global_sku: str = Field(index=True)
    snapshot_id: Optional[int] = Field(default=None, foreign_key="inventorysnapshot.id")
    monthly: list = Field(default_factory=list, sa_column=Column(JSON))  # per-month units
    method: str = "flat_avg"
    confidence: str = "low"
    low_data: bool = False
    baseline: float = 0.0                              # flat-average comparison
    uncertainty_pct: float = 0.0
    diverges_from_baseline: bool = False
    notes: list = Field(default_factory=list, sa_column=Column(JSON))
    batch_id: str = Field(default="", index=True)      # groups one refresh (for pruning)


class InTransit(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    global_sku: str = Field(index=True)
    quantity: float = 0.0
    expected_arrival_month: int = 0                    # projection month 1..6
    shipment_label: str = ""                           # e.g. "Q3 ADD AIR"
    batch_id: str = Field(default="", index=True)


# --------------------------------------------------------------------------
# Orders (immutable origin + append-only events)
# --------------------------------------------------------------------------
class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)                      # e.g. "Q3 2026"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "draft"                              # draft|finalized|placed
    snapshot_batch_id: str = ""                        # the input snapshot it froze
    config_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    lines: List["OrderLine"] = Relationship(back_populates="order")


class OrderLine(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    global_sku: str = Field(index=True)
    # system suggestions (frozen at order creation)
    suggested_sea_qty: int = 0
    suggested_air_qty: int = 0
    baseline_sea_qty: int = 0
    baseline_air_qty: int = 0
    # buyer overrides -> what actually gets exported
    final_sea_qty: int = 0
    final_air_qty: int = 0
    target_moh_used: float = 0.0
    case_size: int = 1
    # explainability / audit
    projection_json: list = Field(default_factory=list, sa_column=Column(JSON))
    suggestion_json: dict = Field(default_factory=dict, sa_column=Column(JSON))
    # origin/current method tracking for the Part-2 timeline
    origin_sea_qty: int = 0
    origin_air_qty: int = 0
    method: str = "sea"                                # primary method
    order: Optional[Order] = Relationship(back_populates="lines")


# --------------------------------------------------------------------------
# Part 2 seams -- tables defined, logic intentionally absent.
# --------------------------------------------------------------------------
class ShipmentLeg(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    method: str = "sea"                                # sea|air
    label: str = ""                                    # "Q3" / "Q3 ADD" / "Q3 ADD AIR"
    status: str = "planned"
    expected_arrival: Optional[date] = None
    actual_arrival: Optional[date] = None
    line_quantities: dict = Field(default_factory=dict, sa_column=Column(JSON))


class OrderLineEvent(SQLModel, table=True):
    """Append-only lifecycle log. Empty in Part 1."""
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    order_line_id: Optional[int] = Field(default=None, foreign_key="orderline.id")
    type: str = ""   # quantity_changed|substituted|discontinued|method_changed|
                     # split_created|availability_confirmed|shipped|arrived
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    actor: str = ""
    note: str = Field(default="", sa_column=Column(Text))
    # email-ingestion provenance (filled in Part 2)
    source_message_id: str = ""
    source_quote: str = Field(default="", sa_column=Column(Text))
    parsed_by: str = ""
    confidence: Optional[float] = None
    confirmed_by: str = ""
    confirmed_at: Optional[datetime] = None


class EmailThread(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: Optional[int] = Field(default=None, foreign_key="order.id", index=True)
    subject: str = ""
    participants: list = Field(default_factory=list, sa_column=Column(JSON))
    thread_key: str = Field(default="", index=True)


class EmailMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    thread_id: Optional[int] = Field(default=None, foreign_key="emailthread.id", index=True)
    message_id: str = Field(default="", index=True)
    sender: str = ""
    received_at: Optional[datetime] = None
    body: str = Field(default="", sa_column=Column(Text))


# --------------------------------------------------------------------------
# Phase A foundations: Vendor (first-class), flexible Product tags (EAV),
# and the Order List master list (what we order, from whom, on which channel).
# --------------------------------------------------------------------------
class Vendor(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    kind: str = "INDIA"            # INDIA | US | CANADA | OTHER
    country: str = ""
    contact_name: str = ""
    contact_email: str = ""
    notes: str = Field(default="", sa_column=Column(Text))
    active: bool = True


class ProductTag(SQLModel, table=True):
    """Flexible key/value attribute on a product (size, weight, gold/silver,
    bloom, camphor, cert numbers, …). Many rows per SKU; values are free text."""
    id: Optional[int] = Field(default=None, primary_key=True)
    global_sku: str = Field(index=True)
    key: str = Field(index=True)
    value: str = ""


class OrderListItem(SQLModel, table=True):
    """Master list: a product we order, from a vendor, on a channel. Replaces
    the static order_list_skus.json allowlist (which is regenerated from the
    active rows here so the Odoo pull stays in sync)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    global_sku: str = Field(index=True, unique=True)
    vendor_id: Optional[int] = Field(default=None, foreign_key="vendor.id", index=True)
    channel: str = "INDIA_IMPORT"   # INDIA_IMPORT | US_VENDOR | US_TO_CANADA
    active: bool = True
    lead_time_days: Optional[int] = None
    moq: Optional[int] = None
    notes: str = Field(default="", sa_column=Column(Text))


# --------------------------------------------------------------------------
# Messaging (shared foundation): provider-agnostic outbound log. The stub
# provider writes here so SKU-Bot texting is testable now; WhatsApp later.
# --------------------------------------------------------------------------
class EventAck(SQLModel, table=True):
    """Per-user acknowledgement ('clocked') of a timeline event. One row per
    (user, event). Unacknowledged events drive the badge count, per user."""
    user: str = Field(primary_key=True)
    event_id: int = Field(primary_key=True)
    acked_at: datetime = Field(default_factory=datetime.utcnow)


class AppSetting(SQLModel, table=True):
    """Simple key/value app settings (shared, server-side) — e.g. email
    recipients for order placement."""
    key: str = Field(primary_key=True)
    value: str = Field(default="", sa_column=Column(Text))


# --------------------------------------------------------------------------
# Users & per-list access. Sign-in is Google; AppUser records who may use the
# app and their role. ListAccess grants a user the right to ORDER FROM a list
# (list_key is a channel like INDIA_IMPORT today, a limited-list id later).
# Admins implicitly have access to every list.
# --------------------------------------------------------------------------
class AppUser(SQLModel, table=True):
    email: str = Field(primary_key=True)          # lower-cased Google email
    name: str = ""
    role: str = "member"                          # admin | member
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ListAccess(SQLModel, table=True):
    # LEGACY (pre-groups direct grants). Retained so existing rows don't error;
    # access is now resolved through Groups. Not written by the app anymore.
    email: str = Field(primary_key=True)
    list_key: str = Field(primary_key=True)       # e.g. INDIA_IMPORT | US_VENDOR
    granted_at: datetime = Field(default_factory=datetime.utcnow)


# --------------------------------------------------------------------------
# Groups. The main admin creates a Group, assigns it master lists (GroupList),
# and names a group admin. The group admin manages members (GroupMember) and
# can carve a per-member Sublist (a subset of a list's SKUs) for each member.
# A user's orderable lists = the lists of every group they belong to.
# --------------------------------------------------------------------------
class Group(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    admin_email: str = ""                          # the group admin
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class GroupList(SQLModel, table=True):
    """A master list assigned to a group (by the main admin)."""
    group_id: int = Field(primary_key=True)
    list_key: str = Field(primary_key=True)        # INDIA_IMPORT | US_VENDOR | ...


class GroupMember(SQLModel, table=True):
    group_id: int = Field(primary_key=True)
    email: str = Field(primary_key=True)
    added_at: datetime = Field(default_factory=datetime.utcnow)


class Sublist(SQLModel, table=True):
    """A single SKU a group admin has assigned to one member within a list.
    If a member has ANY sublist rows for (group, list), they may order only
    those SKUs from that list; otherwise they get the whole group list."""
    group_id: int = Field(primary_key=True)
    email: str = Field(primary_key=True)
    list_key: str = Field(primary_key=True)
    global_sku: str = Field(primary_key=True)


class MessageLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    provider: str = "stub"          # stub | whatsapp
    to_number: str = ""
    person_id: Optional[int] = None
    body: str = Field(default="", sa_column=Column(Text))
    kind: str = ""                  # schedule | oos | order_change | ...
    status: str = "logged"          # logged | sent | failed
    created_at: datetime = Field(default_factory=datetime.utcnow)
    error: str = ""


# --------------------------------------------------------------------------
# Cached-Odoo sync state (skubot pattern).
# One row tracks the background sync loop: when it last ran/succeeded, the
# batch_id of the last GOOD snapshot the app should read from, and the current
# health.  A failed/empty pull updates last_attempt_at + error/status but
# leaves good_batch_id pointing at the last good snapshot -- self-healing.
# --------------------------------------------------------------------------
class SyncState(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    last_attempt_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    good_batch_id: str = ""             # batch_id of last successful snapshot
    status: str = "never"               # never|ok|degraded
    source: str = "odoo_live"
    products: int = 0
    snapshots: int = 0
    in_transit: int = 0
    last_error: str = Field(default="", sa_column=Column(Text))
