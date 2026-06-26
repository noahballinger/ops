# Isha Life Ops Platform — Roadmap & Backlog

> Persistent record so future sessions don't lose context. The order-generation
> tool (India→US/CA, US vendor, US→CA) is **Piece 1** of a larger ops platform.

## Current state (audit — see README for detail)
- **Stack:** FastAPI (Python 3.10) backend; SQLite via SQLModel; frontend is a
  single self-contained vanilla-JS page (`frontend/index.html`) served by
  FastAPI — no build step, no React. Runs locally with one command.
- **Persistence:** SQLite file (`backend/data/isha.db`). Queryable by BI tools
  but see "Reporting" note below.
- **Data source:** read-only Odoo 19 over web JSON endpoints + a background
  sync (`run_sync.py`) snapshotting into SQLite (self-healing cache).
- **Models today:** Product, InventorySnapshot, DemandForecast, InTransit,
  Order, OrderLine, ShipmentLeg (seam), OrderLineEvent (seam), EmailThread /
  EmailMessage (seams), SyncState.
- **Hardcoded / to-make-dynamic:** category→target-MOH map, India ref regex
  `^[A-Z]{2}\d{10}$`, `III/STOCK` warehouse, "snacks=domestic" rule, the
  `order_list_skus.json` allowlist. These belong in editable config/DB tables.

## Shared foundations (build once, well)
- **Product/SKU + flexible tag system** — attribute/tag model (size, weight,
  gold/silver, bloom, camphor, certs…), NOT fixed columns. Underlies Order List,
  SKU Bot, New Product, Photo Mgmt.
- **Vendor** — promote to a first-class table (currently a string field).
- **People (staff/volunteers)** — new model; used by Scheduling + SKU Bot texting.
  Must support **two volunteer pools**: a **stable** list (same person, same
  slot, week over week) and a **rotating** list (cycle through slots, tracking
  whose turn is next + fair distribution). Plan: one `Person` table + a
  `pool_type` field, with assignment logic split per pool (stable = fixed
  assignment rows; rotating = a cursor/round-robin with a "last served" stamp).
  Revisit as separate models only if rotating fairness state gets complex.
- **Messaging** — a `MessageProvider` interface + `MessageLog` table is a shared
  foundation (used by SKU Bot texting + order-change notifications). Stub now,
  WhatsApp later.
- **Orders with history** — Order = immutable origin snapshot + append-only
  event log (OrderLineEvent seam already exists).

## Full backlog
### 1. Order Management
- Generate orders: India→US/CA, US vendors, US→CA  *(✅ India path built)*
- Track changes to placed orders + timeline view
- Email to place/track orders; summarize what changed
- Order List Management — master list of what we order, from whom
- Product-level tags (size, weight, gold, silver, bloom, camphor, certs…)
### 2. Scheduling
- Floor / warehouse / Connie's schedule; program calendar
- Constraints: physical limitations + experience level (checkout, warehouse, jaggery…)
### 3. SKU Bot
- Product lookups, label printing, inventory counts
- Text volunteers their schedules; handle "can't make it"
- Notify about order changes; request items for next transfer; replen rules
- **OOS updates for incoming products** (high-demand from Floor/CS)
### 4. Project Management
- Step-by-step processes with an owner (POC) + info to execute
- Reminders/follow-ups; status feedback; visual flags ("red dots")
### 5. New Product
- Auto-create US SKUs (with review); Global SKUs for US-sourced (form → email)
- Auto-update Order List; loop in photo management
### 6. Reporting
- Metabase on top of our data
### 7. Product Photo Management
- What's not photographed; naming convention; sidecar text file per photo;
  upload interface; storage in Google Drive (no server-side files)

## Integration decisions (LOCKED)
- **Messaging = WhatsApp Business API** (not SMS/Twilio), for volunteer texting
  and order-change notifications. Meta business verification is in progress, so
  real creds connect later. **Build behind a `MessageProvider` interface now**
  with a **stub provider** (logs to console + writes to a `MessageLog` table
  viewable in the UI) so SKU-Bot texting logic is built/tested today; the real
  WhatsApp client swaps in later without touching callers.
- **Google Drive = OAuth** (not service account). Personal Google account for
  now; must move to org Workspace later **without a rebuild** → store
  `{refresh_token, target_folder_id}` as swappable config; migration = "revoke,
  reauth, repoint folder."
- **One Google OAuth app covers BOTH** Drive scopes and Gmail-send scopes (same
  Gmail account) — single consent, not two integrations.
- **Outbound email = Gmail API** (OAuth, same app as Drive) from a dedicated
  app Gmail address. (Send via Gmail API, not raw SMTP.)
- **Hosting = back-office PC**, self-hosted (unchanged). WhatsApp's inbound
  webhook needs public HTTPS → use a **tunnel (Cloudflare Tunnel preferred,
  Tailscale Funnel alt)**; decide before the SKU-Bot/WhatsApp phase. Google
  OAuth uses a loopback redirect, so Drive/email need no public exposure.
- **Database = Postgres now** (migrating this phase). Unblocks Metabase (item 6)
  and concurrent access; SQLite remains a dev fallback via `DATABASE_URL`.

## Phased plan (proposed)
- **Phase A — Foundations: ✅ DONE** — Vendor + Product/Tag (EAV) models,
  OrderListItem master list + UI, allowlist bridge, Postgres support,
  additive auto-migration, MessageLog foundation.
- **Phase B — Order lifecycle: ✅ DONE** — OrderLineEvent logging (created /
  quantity_changed / discontinued) + Timeline UI; outbound email behind an
  EmailProvider interface (Gmail API verified, stub fallback) + Google OAuth
  (combined Gmail+Drive scopes); placement & change-summary emails.
- **Phase C — New Product:** US/Global SKU creation w/ review → updates Order
  List → triggers email; feeds Photo Mgmt.
- **Phase D — People + Scheduling:** People model; floor/warehouse/Connie
  schedules + constraints.
- **Phase E — SKU Bot:** lookups/labels/counts; SMS (schedules, OOS, change
  notifications); replen.
- **Phase F — Project Management:** processes, owners, reminders, red-dot flags.
- **Phase G — Photo Management:** Drive upload, naming, sidecar, gaps view.
- **Phase H — Reporting:** Postgres migration (if not earlier) + Metabase.

### Dependencies
- A precedes everything (Product/Tag + Vendor are shared).
- B needs A (Order List) + the email integration decision.
- C needs A + email; E (SMS) and G (Drive) need their integration decisions.
- H (Metabase) wants Postgres — decide the DB move during/after A.

## Recommended next slice
**Order List Management + Product Tags (1.4 / 1.5)** — confirmed by the audit:
it's the shared data model the most modules depend on, and it upgrades the two
weakest current spots (Vendor-as-string, fixed Product columns).
