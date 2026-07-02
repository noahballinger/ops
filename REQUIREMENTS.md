# Isha Life — International Ordering Web Application
## Requirements Specification

**Status:** Draft for review
**Owner:** Isha Life USA operations
**Last updated:** 2026-06-27

---

## 1. Purpose

Isha Life replenishes its US and Canada inventory on a recurring basis: a large quarterly import order from India, and more frequent restocking orders from domestic US vendors. Today this is run manually out of a spreadsheet, with quantities decided by hand from exported Odoo data.

This document specifies a web application that replaces that manual workflow. The system shall pull live inventory and sales data from Odoo, compute defensible reorder quantities, and let authorized staff review, adjust, and place orders — while giving leadership a reporting layer over the same data. The application is intended to grow into the operational home for all reordering, with controlled access for a wider set of users.

## 2. Background & Constraints

- The current source of truth is an Odoo 19 ERP instance plus a recurring spreadsheet ("USA INV CHK") that encodes the ordering math.
- Odoo access must be **strictly read-only**. The application shall never create, modify, or delete records in Odoo. Ordering output leaves the system as emails and files only.
- The business does not want to upload spreadsheets manually; data shall come from Odoo automatically.
- The application is operated as an internal back-office tool. It is not a public, multi-tenant product.

## 3. Goals & Non-Goals

**Goals**
- Replace the manual quarterly spreadsheet with a guided, auditable ordering flow.
- Produce reorder suggestions that improve on the spreadsheet by accounting for sales velocity, lead times, and shipping mode.
- Support both the India import channel and the US vendor channel, with room for additional channels (e.g. Canada).
- Provide controlled, per-person access so non-specialist staff can place orders for only the lists they are responsible for.
- Provide a reporting layer (Metabase) over the same data without bespoke report-building.

**Non-Goals (for the current phase)**
- Writing back to Odoo or acting as a system of record for inventory.
- Full order-lifecycle tracking with carrier integration (planned; see §13).
- A public API or external customer access.

## 4. Users & Roles

The system shall support two roles:

- **Administrator** — manages the master product lists, manages users and their access, and can order from any list. The **first user to sign in becomes an administrator automatically**; administrators may promote others. A configured allow-list of admin emails shall always resolve as administrators, so the team can never be locked out.
- **Member** — can place orders only for the lists explicitly assigned to them. Members cannot change the contents of any list and cannot manage users.

A user who has signed in but has not yet been granted any list is considered **pending** (see §11.4).

## 5. Authentication

- Sign-in shall use **Google ("Sign in with Google")**. There is no separate application password.
- Sessions are server-side and expire after a period of inactivity.
- The application shall record each signed-in user (email, display name, role, active flag).

## 6. Data Integration (Odoo)

- The system shall connect to Odoo over its standard web JSON endpoints using a service login, with **no external API key** and **no write-capable methods** (the client shall refuse anything other than read operations).
- Connection details (URL, database, credentials) shall be supplied via environment configuration and never written to the repository, logged, or placed in URLs.
- The system shall pull: the product catalogue (codes, names, categories, barcodes, cost, price, country of origin), on-hand inventory, incoming/in-transit stock, and sales history.
- **Country-of-origin handling:** products whose origin is known and not India shall be excluded from the India import list; snacks/grocery categories are treated as domestically sourced.

### 6.1 Cached sync (resilience)

- The system shall periodically snapshot Odoo into its own database and serve the application from that snapshot, so the tool remains fast and usable when Odoo is briefly unavailable.
- The sync shall be **self-healing**: a failed or empty pull must never overwrite the last good snapshot. Failures are recorded and the previous snapshot continues to serve.
- Data freshness shall be observable (a header indicator showing last sync time and a stale/degraded state).
- Snapshots shall be pruned automatically to a small number of recent batches, with any snapshot referenced by a saved order always retained.

## 7. Sales & Demand

- Sales shall be derived from **confirmed POS sales and confirmed sale orders** over a trailing window (default 24 months). Draft quotations and cancelled orders shall be excluded.
- Online sales are captured at the sale-order line; delivery/stock-move references (e.g. `III/OUT`) shall **not** be counted separately, to avoid double-counting fulfilled sale orders. (An audit confirmed every outgoing delivery traces to a sale or POS order, so no demand is missed.)
- Demand velocity shall use a **sell-through basis**: units sold averaged over the months a product was actually in stock, so stock-outs do not artificially depress demand. The system shall persist, per product, `units sold`, `months active` (in-stock months), and a `sell-through` rate.

## 8. Order Suggestion Engine

The engine shall reproduce the spreadsheet's intent and improve on it. It shall:

- Project months-of-stock-on-hand forward across a planning horizon, incorporating incoming/in-transit quantities.
- Forecast per-future-month demand using explainable methods scaled to the available history (flat average for sparse data, moving-average with trend for moderate history, seasonal indices for longer history), excluding stock-out months from the fit.
- Split a recommended quantity between **sea** and **air** based on lead times and near-term coverage needs.
- Round to case sizes and respect minimum order quantities.
- Show its working (current and projected coverage, the basis for any air split, confidence) so a buyer can review rather than trust blindly.

Suggestions are frozen onto an order at creation so the order remains an auditable snapshot.

## 9. Reorder Lists (Master Lists)

- A **master list** defines the products ordered on a given channel (e.g. India import, US vendor), each optionally carrying vendor, lead time, and MOQ.
- The master list shall be the single control over what the Odoo pull considers in scope; editing the list shall keep the pull's scope in sync automatically.
- **Administrators** shall be able to add products (searching the synced catalogue), remove products, and pause/resume a product without removing it.
- Editing master lists is an administrator-only capability.

## 10. Ordering Flows

The application shall provide, per channel:

- **India import** — a review surface listing suggested products with editable quantities, leading to order placement.
- **US vendor** — restocking organized by item, leading to placement; vendor purchase emails are generated per vendor.
- **Track** — a place to view placed orders and their status/timeline.

On placement, the system shall generate the appropriate purchase email(s) and record the order. Email recipients are configurable.

## 11. Access Control

### 11.1 Per-list access
- Each member shall be granted access to specific lists. Administrators implicitly have access to all lists.
- Access shall be **enforced on the backend**: order endpoints reject any request for a list the user has not been granted, not merely hide it in the UI.

### 11.2 Visibility
- Members shall see **only** the lists they can order from. Lists they cannot order shall not appear anywhere in their interface (no cards, no order-box buttons, no placeholders).
- Administrators see all lists plus planning placeholders for future channels.

### 11.3 User administration
- Administrators shall have a dedicated screen to add users (by email), set role (member/admin), activate/deactivate, set per-list access via simple toggles, and remove users.
- A user shall not be able to delete their own account.

### 11.4 Pending users & onboarding
- When a brand-new user signs in with no access, the system shall **email a coordinator** (configurable recipients; default to a named operations address with a CC) so the account can be configured.
- A pending user shall see a friendly waiting screen explaining that a coordinator is configuring their account. The screen shall include a small diversion (a Snake mini-game) and shall **automatically admit the user** as soon as access is granted (polling plus a manual "check access" action).

## 12. Dashboard & Navigation

- The home screen shall be a dashboard of widgets the user can rearrange.
- A primary **Ordering** widget shall present the lists the current user may order from (plus Track). Its contents reflect the user's access.
- An **Items Reorder** widget shall link to the Reorder Lists area.
- Navigation shall be route-based so views are linkable and back/forward works.

## 13. Reporting

- Application data shall live in **PostgreSQL**, and **Metabase** (open source) shall read from the same database for dashboards and ad-hoc reporting.
- The system shall expose **stable reporting views** that flatten internal structures (e.g. current inventory with demand metrics; order lines with product and demand detail), so dashboards remain stable as internal tables evolve.
- Metabase shall connect to the database directly; reporting shall not require changes to the application.

## 14. Non-Functional Requirements

- **Architecture:** A Python/FastAPI backend with a typed ORM; a single-file browser front end (no build step) served by the backend.
- **Database:** PostgreSQL is the one operational database. The application shall not silently fall back to a local file database; if the database is unreachable it shall fail with a clear, actionable message. Schema additions (new columns) shall apply automatically on startup; structural changes (renames/drops/type changes) are handled by explicit migration scripts.
- **Caching:** A single cache layer (the database snapshot). Redundant per-read disk caching shall be disabled by default.
- **Security & safety:** Odoo access is read-only; credentials are never persisted or logged. Access is enforced server-side. The test suite shall run only against a disposable database and shall be structurally prevented from touching production data.
- **Operations:** Serving and testing shall be separate commands (a launch shall never run destructive tests). Database and reporting services shall be provided via container configuration.
- **UX:** A clean, professional, muted ("shades-of-white") visual theme, with light and dark modes.

## 15. Deferred / Future Phases

The following are anticipated and the design shall not preclude them:

1. **Limited (curated) lists.** Named subsets of a master list assigned to specific people, who may order from their subset but cannot change it. The access model already represents lists generically, so curated lists slot in as additional grantable lists with no rework.
2. **Additional channels.** Canada (US→Canada transfers) and further modules, surfaced through the same list/access machinery.
3. **Full order lifecycle.** Shipment legs (sea/air, partial shipments), an append-only event timeline, and ingestion of supplier email replies to update order state — with per-user "read/acknowledged" tracking driving notification badges.
4. **Messaging integrations.** Supplier/coordinator messaging (e.g. WhatsApp) behind the existing provider abstraction.
5. **De-provisioning notifications.** Alerting when an existing user loses all access.
6. **Production hardening of access.** Tightening administrator gating, credentials, and network exposure ahead of any non-back-office deployment.

## 16. Acceptance Criteria (summary)

- A buyer can generate, review, adjust, and place a quarterly India order and a US vendor order, sourced entirely from Odoo with no manual spreadsheet upload.
- Reorder quantities reflect confirmed sales on a sell-through basis and show their working.
- An administrator can edit master lists and manage users and per-list access; members see and can order only their assigned lists, enforced server-side.
- A new user is emailed about, sees a waiting screen, and is admitted automatically once granted access.
- Metabase reports run against the application's PostgreSQL database via stable views.
- The application runs on PostgreSQL with no silent fallback; tests cannot touch production data.
