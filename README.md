# Isha Life — USA Import Ordering & Shipment-Tracking Tool (Part 1)

An internal web tool that replaces the manual `USA INV CHK.xlsx` workflow for
ordering large quarterly shipments of consumer goods from India to the USA.
It looks at current stock and sales velocity, **projects forward across the
4–6 month lead time**, and tells the buyer **what to order** and **how to ship
it** (sea vs. air, splitting a SKU across both when needed), then **exports a
clean CSV and Excel** order for the India team and customs broker.

Every suggested quantity is an **editable default** with the inputs visible.
The buyer stays in control; the tool augments their judgment.

> **Status:** Part 1 only. The Part 2 order-lifecycle / email-ingestion seams
> are defined in the schema and API shape but intentionally carry no logic
> yet (see *Part 2 seam* below).

---

## What it does

1. **Pulls data** — live from Odoo 19 (read-only, see below) *or* from the
   same export files the buyer downloads today. Per SKU: on-hand, trailing
   monthly sales, cost, retail, case size, category, mappings, and in-transit
   quantities by arrival month.
2. **Forecasts demand** per future month across the horizon (not a flat
   average) and runs the workbook's forward inventory projection net of
   in-transit stock against that demand.
3. **Suggests** a sea quantity (refill to target MOH at month 6), an air
   quantity (cover the near-term floor breach at month 4), and case-rounds
   both — plus the workbook flat-average **baseline** for comparison.
4. **Review screen** — filterable/sortable table; every quantity editable;
   the system suggestion shown beside the override; air-split reasoning shown
   in plain language; compliance flags surfaced and filterable.
5. **Named orders** ("Q3 2026") freeze the input snapshot + finalized
   quantities.
6. **Export** — one click produces CSV **and** `.xlsx` mirroring the workbook's
   `ORDER LIST` columns.

---

## The math (reproduced from the workbook, then improved)

The workbook (`SEA` sheet) is the spec of record. The engine reproduces it
**exactly** and generalises it to a per-month forecast.

```
current MOH      = useable_on_hand / avg_monthly_sales
OH_month_N (MOH) = max(0, OH_month_(N-1) − demand_moh_N + incoming_moh_N)   # start = current MOH
sea_months       = max(0, target_MOH − OH_month_6)        # SEA sheet col T
sea_qty          = sea_months × avg_monthly_sales         # col Q
air_months       = max(0, 3 − OH_month_4)                 # col U (3-month floor)
air_qty          = air_months × avg_monthly_sales         # col S
sea_round/air_round = CEILING(qty, case_size)             # ORDER LIST I/J
margin           = retail − COGS                          # ORDER LIST Q
profit_lost_air  = margin × air_round                     # ORDER LIST P
```

`demand_moh_N` is `forecast_units_month_N / avg_monthly_sales`. When the
forecast equals the flat average (the workbook's `annual ÷ 12`), every
multiplier is `1.0` and the result is **identical to the workbook** — so the
workbook stays a provable baseline. With real monthly history the same
projection runs on better numbers; the sea/air logic is unchanged.

**Verification.** `backend/tests/test_workbook_parity.py` drives the engine
with the workbook's own inputs and asserts the sea/air quantities match the
workbook's computed values within rounding across **every** fully-numeric
`SEA` row (281 rows reproduced exactly). Run it with `ISHA_WORKBOOK` set.

### Forecasting (§3a)
A pure, swappable module (`forecasting.py`):
- `< 6` useable months → flat average (baseline), flagged **low confidence**.
- `6–23` months → recent level + gentle trend (no seasonality).
- `≥ 24` months → multiplicative **seasonal indices + trend**.
- **Stock-out months** (zero sales while out of stock) are excluded from the
  fit and flagged — a stockout is not zero demand.
- Each SKU carries a confidence band and a **divergence flag** when the
  forecast departs far from the flat baseline, so the buyer can sanity-check
  before trusting the smarter number. The baseline is always shown alongside.

> The workbook's `SALES` sheet only stores a flat `annual ÷ 12` figure, so the
> **workbook import path forecasts at baseline (low confidence)**. To unlock
> seasonal forecasting, either use the live Odoo pull (monthly `read_group`)
> or supply a long-format monthly CSV: `global_sku,year,month,units[,is_stockout]`.

### Category rules
- **BLOOM** (Ecocert organic cosmetics): expiry-sensitive, case size 32.
- **DOMESTIC** (e.g. Botanie soap): MOQ-driven, no sea/air — order one MOQ
  when MOH < 4.
- **CLOTHING**: per-size SKUs, packs of 8.
- **Expiry items**: useable-stock logic (months-till-expiry − 1.5-month buffer).
- All thresholds live in `backend/app/config.py` and are overridable via
  `backend/app/config_overrides.json` (no code change).

---

## Running it (one command)

```bash
./run.sh
```

This creates a venv, installs deps, runs the test suite, and serves the app at
**http://localhost:8000**. Open it, create an order by uploading the workbook,
review/override, and export.

Manual equivalent:
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q tests/
uvicorn app.main:app --port 8000
```

Seed/inspect from the workbook on the command line:
```bash
python scripts/seed_from_workbook.py "/path/to/Copy of USA INV CHK.xlsx"
```

### Reporting with Metabase Open Source

Metabase runs in development via Docker Compose:

```bash
docker compose up -d
```

Open **http://localhost:3000** and complete Metabase's first-run setup. The
Metabase app metadata is stored in a Docker volume, separate from this app's
database.

Postgres is the one database the app runs on. Set this in `.env` (the app on
the host reaches Postgres at `localhost`):

```
DATABASE_URL=postgresql+psycopg://isha:isha@localhost:5432/isha
```

If `DATABASE_URL` is unset the app defaults to the same local docker Postgres.
It will **not** silently fall back to SQLite — if Postgres is unreachable it
fails loudly and tells you to run `docker compose up -d`. (SQLite is used only
by the test suite, which forces it via `backend/tests/conftest.py`.)

One-time import of any old SQLite data:

```bash
python3 scripts/migrate_sqlite_to_postgres.py --replace
```

In Metabase, add a database connection with **Host: `db`** (the compose service
name — not `localhost`, since Metabase runs inside Docker), Port `5432`,
Database `isha`, user/pass `isha`/`isha`.

Then point dashboards at the two reporting **views** (kept stable for Metabase,
JSON columns already flattened):

- `v_current_inventory` — latest synced on-hand, sales, `units_sold`,
  `months_active`, `sell_through`, joined to product details.
- `v_order_lines` — every order line with product info and the demand metrics
  pulled out of `suggestion_json`.

### Database maintenance (you shouldn't need to think about this)

- **Schema changes**: adding a field to a model is automatic — startup runs an
  additive migration (`_auto_add_missing_columns`) that adds new nullable
  columns. It does **not** do renames, drops, or type changes; for those write
  a one-off script (see `scripts/`) or add Alembic.
- **Snapshots don't pile up**: each sync writes one batch; the sync prunes to
  the most recent few (`ODOO_KEEP_BATCHES`, default 3), always keeping any
  batch an order depends on.
- **One cache layer**: the DB snapshot batch *is* the cache. The old per-read
  disk cache is off by default (`ODOO_DISK_CACHE=1` to re-enable). Safe to
  delete the old files: `rm -rf backend/data/odoo_cache/*`.
- **Tests never touch Postgres**: run them with `./test.sh`; `./run.sh` only
  serves.

### The three input modes (graceful degradation — all first-class)
1. **Upload the workbook** (`.xlsx`) — reads the same sheets pasted today.
2. **Upload the discrete Odoo exports** — INV + SALES (+ optional price list /
   in-transit / monthly-sales CSV).
3. **Live Odoo pull** (read-only) — configured server-side; see below.

Modes 1 & 2 give **identical downstream behavior** to a live pull, and are the
way to demo without credentials. If a live pull fails, the file path is always
available.

---

## Odoo 19 integration — JSON over the web endpoints, **read-only**

There is **no External API** (no API key, no XML-RPC). We authenticate as a
normal user and exchange JSON over the same endpoints the Odoo web client uses
(`backend/app/datasources/odoo_json.py`):

- `POST /web/session/authenticate` → session cookie (held in memory only).
- `POST /web/dataset/call_kw/<model>/<method>` → JSON reads.
- Reads only: `stock.quant`/`product.product` (on-hand), `sale.report` via
  `read_group` grouped by month (sales history), `product.template/product`
  (mappings/cost/price/case), incoming `stock.move` (in-transit).
- Model/field names are confirmed against the live instance with `fields_get`
  rather than hard-coded (Odoo deployments customise these).

**Hard safety rules, enforced in code:**
- **Strictly read-only.** Only `search_read / read_group / read / fields_get /
  search / search_count` are permitted; any write-style method raises
  `OdooReadOnlyError` *before* a request is built
  (`tests/test_odoo_readonly.py`). The tool never calls create/write/unlink.
- **Credentials** are entered at runtime, held only in the server session,
  **never logged, never written to disk, never placed in URLs**.
- **Respectful reads**: explicit "refresh" (no polling), paged large reads,
  throttled, and **every read is disk-cached** for a TTL.

### Configuring the live connection & cache
Credentials and cache settings come from environment variables (read at
runtime, never written to the repo). Copy `.env.example` → `.env` (gitignored);
`run.sh` loads it automatically.

```
ODOO_BASE_URL, ODOO_DB, ODOO_LOGIN, ODOO_PASSWORD   # connection (required)
ODOO_WAREHOUSE, ODOO_SALES_MODEL                    # optional
ODOO_CACHE_DIR            # default backend/data/odoo_cache
ODOO_CACHE_TTL_SECONDS    # how long a cached read stays fresh (default 3600 = 1h)
ODOO_PAGE_SIZE            # rows per paged read (default 2000)
ODOO_THROTTLE_SECONDS     # delay between calls (default 0.2)
```

### Cached Odoo DB — background sync (skubot pattern)
The robust way to use Odoo: a **background service snapshots Odoo into the
local SQLite cache on a cadence, and the app reads from that cache** — so it's
fast and keeps working even if Odoo is briefly unreachable.

```bash
python run_sync.py        # snapshots Odoo every ODOO_SYNC_SECONDS (default 600s)
```

Three guarantees (adopted from the skubot project):
- **Self-healing, not self-destructing** — a failed or empty Odoo pull *never*
  overwrites the last good snapshot; the app keeps serving it and the state
  goes `degraded`.
- **Explicit staleness** — cache older than `ODOO_SYNC_STALE_FACTOR ×
  ODOO_SYNC_SECONDS` is flagged `is_stale` / `healthy:false`.
- **Read-only** — enforced by the client's method allow-list.

Build an order from the cache (works offline): `POST /api/orders/from-cache`
`{name}`. Trigger a snapshot by hand: `POST /api/odoo/sync`. The `SyncState`
row tracks `last_success_at`, the good snapshot's `batch_id`, counts, and the
last error.

### Endpoints
- `GET  /api/odoo/status` — connection config + **cache freshness/health**
  (`sync`: status, last success, age, `is_stale`, `healthy`) + per-read cache
  state. No secrets returned.
- `POST /api/odoo/sync` — run one Odoo→cache snapshot now (self-healing).
- `POST /api/orders/from-cache` `{name}` — order from the latest good snapshot
  (the normal live path; works even if Odoo is down).
- `POST /api/orders/from-odoo` `{name[, refresh, sales_months]}` — on-demand
  live pull straight into an order (`refresh:true` bypasses the per-read cache).
- `POST /api/odoo/test` — validate the connection (env, or a posted
  `{base_url, db, login, password}` that is **not** stored).
- `POST /api/odoo/refresh` — clear the per-read cache.

Two cache layers exist: the **background snapshot** (the `SyncState`/local-DB
cache the app reads from) and a lower-level **per-read disk cache** (keyed by
model+method+args, TTL `ODOO_CACHE_TTL_SECONDS`). Refresh is always explicit —
the tool never polls.

---

## Data model (designed for Part 2)

`backend/app/models.py` (SQLite via SQLModel):
`Product` (PK = **Global SKU**, the one join key), `InventorySnapshot`
(on-hand + monthly sales series), `DemandForecast`, `InTransit`, `Order`
(immutable origin snapshot), `OrderLine` (suggested vs. final qty, projection
JSON for audit).

**Part 2 seams — tables defined, logic absent:** `ShipmentLeg` (one order fans
out into many legs, reusing the workbook's `Q3 / Q3 ADD / Q3 ADD AIR`
labelling), `OrderLineEvent` (append-only log with the full event type set +
email-provenance fields), `EmailThread` / `EmailMessage`.

---

## Project layout
```
backend/
  app/
    config.py          # business-rule thresholds (workbook-derived, overridable)
    forecasting.py     # PURE per-month demand forecaster
    engine.py          # PURE projection + sea/air split + economics
    resolver.py        # Global SKU <-> US SKU <-> Odoo ref
    models.py          # SQLModel tables (Part 1 + Part 2 seams)
    service.py         # pull -> forecast -> suggest -> persist
    export.py          # CSV / XLSX (ORDER LIST parity)
    main.py            # FastAPI routes; serves the UI
    datasources/
      base.py          # DataSource interface (PullResult contract)
      file_import.py    # workbook + discrete-export parsing
      odoo_json.py     # read-only, cached Odoo JSON client
  tests/               # engine parity, forecaster, Odoo read-only
frontend/index.html    # React review UI (served by FastAPI, zero build step)
scripts/seed_from_workbook.py
run.sh
```

> **Frontend note.** To keep "runs with one command" literally true and avoid a
> Node build, the React UI is a single file loaded via CDN and served by
> FastAPI. It is real React. To move to a Vite/JSX build later, lift the
> `App`/`Review` components into `frontend/src` — the API contract is unchanged.

---

## Acceptance criteria — status
- ✅ Reproduces the workbook's sea/air quantities within rounding (281 SEA
  rows, automated parity test).
- ✅ Buyer can override any quantity; the export reflects overrides.
- ✅ CSV **and** XLSX export, opening cleanly with the agreed columns.
- ✅ Compliance-flagged / domestic / expiry SKUs handled per category rules;
  flagged items are surfaced and filterable, never silently dropped.
- ✅ Credentials never touch logs/repo/URLs; Odoo access is read-only.
- ✅ Test suite covers the suggestion engine + forecaster against known rows.

---

## Open questions for the client (§11) & assumptions made

Proceeded with sensible defaults; please confirm:

1. **Odoo** — exact instance URL, database name, login, hosting (Online /
   Odoo.sh / self-hosted), and whether an SSO/proxy sits in front of
   `/web/session/authenticate` + `/web/dataset/call_kw`.
2. **Which warehouse/location is "US on-hand"**, and the sales-reporting model
   (`sale.report` assumed; `sale.order.line` is the fallback). A sample of
   each export used today would let us pin column names.
3. **Target MOH per category** — confirm the workbook's values (default 8;
   ACCESSORY/CLOTHING 6; some CONX/INCENSE/TEMPLE/YOGA up to 10–12). And how
   much sales history exists (drives forecast quality).
4. **Exact export column set** the India supplier and broker need (we mirror
   `ORDER LIST`; `UNIT WEIGHT` and per-leg air-freight rate are currently
   blank/manual — confirm sources).
5. **Who logs in and where it's hosted** (laptop / internal server / cloud) —
   drives the credentials/security posture.
6. **Confirm read-only Odoo access is acceptable** (it is enforced).

**Assumptions:** on-hand = Odoo *Free To Use* quantity; the order universe is
the workbook's curated category sheets (SEA/AIR/BLOOM/DOMESTIC/CLOTHING), not
every SKU with sales; in-transit `MONTH` integers map to projection months 1–6;
COGS = price list `LC USA`; expiry buffer = 1.5 months.

---

## Part 2 seam (DO NOT BUILD YET) — and a safety note to carry forward

After Part 1 is approved, order-lifecycle tracking will ingest the India email
threads, parse them into structured `OrderLineEvent`s (quantity change,
substitution, discontinuation, method change, split, availability
confirmation) with an exact supporting quote + confidence, and let the buyer
replay an order from its single origin through every split. **Human-in-the-loop
confirmation will be mandatory — parsed events are proposals, never
auto-applied.**

> **Security (flagged now for Part 2):** email bodies are **untrusted input**.
> When the LLM parses them, their content is treated strictly as *data to
> extract events from*, never as instructions to act on. An email saying "go
> ahead and reorder everything" is a fact to record for the buyer's review,
> not a command the tool executes. Email access will be read-only and scoped
> to order-related threads.

Part 1 already shapes for this: `Order` is an immutable origin snapshot +
append-only event log; `ShipmentLeg`s let one order fan out; the email tables
exist (empty); each `OrderLine` stores origin/current qty + method so a later
timeline UI can reconstruct state without a schema change.
```
