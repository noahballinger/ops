"""Database engine + session helpers.

ONE database for the running app: Postgres. Metabase (Reporting) sits on the
same Postgres, so there is no second source of truth to keep in sync.

Resolution order (see `_resolve_url`):
  1. DATABASE_URL if set   -> used as-is (normalised to the psycopg3 driver).
  2. ISHA_DB_PATH if set   -> SQLite at that path. This exists ONLY for the
     test suite (see tests/conftest.py); the app never sets it.
  3. neither set           -> the local docker Postgres (DEFAULT_POSTGRES).

We deliberately do NOT silently fall back to an ad-hoc SQLite file: a silent
SQLite fallback is exactly what once let the app write to SQLite while Metabase
read an empty Postgres. If Postgres is unreachable we fail loudly instead.

Schema migrations: `_auto_add_missing_columns()` is an ADDITIVE-ONLY helper —
it can add new nullable columns to existing tables, nothing more. It does NOT
handle renames, drops, or type changes. For any of those, write a one-off
script (see scripts/) or introduce Alembic. Adding a field to a model is safe
and needs no manual step.
"""
from __future__ import annotations

import os
from sqlalchemy import event
from sqlmodel import SQLModel, create_engine, Session

# The local back-office Postgres started by docker-compose. Override with
# DATABASE_URL in .env (the app on the host reaches it at localhost:5432).
DEFAULT_POSTGRES = "postgresql+psycopg://isha:isha@localhost:5432/isha"


def _normalize_pg(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _resolve_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return _normalize_pg(url)
    # SQLite is for tests ONLY, and only when explicitly requested.
    sqlite_path = os.environ.get("ISHA_DB_PATH", "").strip()
    if sqlite_path:
        path = os.path.abspath(sqlite_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return f"sqlite:///{path}"
    # Nothing configured: use the local docker Postgres (never a stray SQLite).
    return DEFAULT_POSTGRES


DATABASE_URL = _resolve_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    _engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_con, _):
        # DELETE journal (not WAL) so SQLite works on network / FUSE mounts.
        cur = dbapi_con.cursor()
        cur.execute("PRAGMA journal_mode=DELETE")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
else:
    # Postgres: pooled, with pre-ping so stale connections are recycled.
    _engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5,
                            max_overflow=10)


def backend_info() -> dict:
    """Which database the app is actually using (no secrets)."""
    try:
        host = _engine.url.host or ""
        name = _engine.url.database or ""
    except Exception:
        host, name = "", ""
    return {"backend": "sqlite" if IS_SQLITE else "postgres",
            "dialect": _engine.dialect.name, "host": host, "database": name}


def init_db() -> None:
    from . import models  # noqa: F401  (register tables on the metadata)
    try:
        SQLModel.metadata.create_all(_engine)
    except Exception as e:  # almost always: Postgres isn't up
        if not IS_SQLITE:
            raise RuntimeError(
                "Could not connect to Postgres at "
                f"{_engine.url.render_as_string(hide_password=True)}.\n"
                "Start it with:  docker compose up -d\n"
                "or set DATABASE_URL in .env to your database."
            ) from e
        raise
    _auto_add_missing_columns()
    _create_reporting_views()
    bi = backend_info()
    print(f"[db] using {bi['backend'].upper()} "
          f"({bi['dialect']}{(' @ '+bi['host']) if bi['host'] else ''}"
          f"{('/'+bi['database']) if bi['database'] and not IS_SQLITE else ''})")


# --------------------------------------------------------------------------
# Reporting views for Metabase. These are the STABLE contract the reporting
# tool reads — they flatten the JSON columns and always resolve "current" data
# (the latest good sync batch) so dashboards don't break when internals change.
# Postgres only; skipped on the SQLite test database.
# --------------------------------------------------------------------------
_VIEWS = {
    "v_current_inventory": """
        CREATE OR REPLACE VIEW v_current_inventory AS
        SELECT s.global_sku, p.name, p.category, p.barcode, p.us_sku,
               p.vendor, p.source, p.origin, p.cost, p.retail_price,
               s.on_hand, s.useable_on_hand, s.avg_monthly_sales,
               s.units_sold, s.months_active, s.sell_through,
               s.captured_at, s.batch_id
        FROM inventorysnapshot s
        JOIN product p ON p.global_sku = s.global_sku
        WHERE s.batch_id = (SELECT good_batch_id FROM syncstate
                            ORDER BY id LIMIT 1)
    """,
    "v_order_lines": """
        CREATE OR REPLACE VIEW v_order_lines AS
        SELECT ol.id AS order_line_id, o.id AS order_id, o.name AS order_name,
               o.status AS order_status, o.created_at AS order_created_at,
               ol.global_sku, p.name AS product_name, p.category, p.barcode,
               p.us_sku, p.vendor,
               ol.final_sea_qty, ol.final_air_qty,
               (ol.final_sea_qty + ol.final_air_qty) AS final_total_qty,
               ol.suggested_sea_qty, ol.suggested_air_qty,
               ol.target_moh_used, ol.case_size, ol.method,
               NULLIF(ol.suggestion_json->>'sell_through','')::float    AS sell_through,
               NULLIF(ol.suggestion_json->>'units_sold','')::float      AS units_sold,
               NULLIF(ol.suggestion_json->>'months_active','')::float   AS months_active,
               NULLIF(ol.suggestion_json->>'avg_monthly_sales','')::float AS avg_monthly_sales,
               NULLIF(ol.suggestion_json->>'forecast_mean','')::float    AS forecast_mean,
               NULLIF(ol.suggestion_json->>'on_hand','')::float          AS on_hand,
               NULLIF(ol.suggestion_json->>'current_moh','')::float      AS current_moh,
               NULLIF(ol.suggestion_json->>'unit_cost','')::float        AS unit_cost,
               NULLIF(ol.suggestion_json->>'retail_price','')::float     AS retail_price
        FROM orderline ol
        JOIN "order" o ON o.id = ol.order_id
        LEFT JOIN product p ON p.global_sku = ol.global_sku
    """,
}


def _create_reporting_views() -> None:
    if IS_SQLITE:
        return
    from sqlalchemy import text
    for name, ddl in _VIEWS.items():
        try:
            with _engine.begin() as conn:
                conn.execute(text(ddl))
        except Exception as e:  # never block startup on a view
            print(f"[db] could not (re)create view {name}: {e}")


def _auto_add_missing_columns() -> None:
    """Dev-friendly additive migration: ALTER TABLE ADD COLUMN for any model
    field missing from an existing table, so adding a field never requires
    dropping the DB. Additive only (no drops/renames); columns added nullable.
    Runs on SQLite and Postgres alike."""
    from sqlalchemy import inspect, text
    insp = inspect(_engine)
    tables = set(insp.get_table_names())
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in tables:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            coltype = col.type.compile(_engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            try:
                with _engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception:
                pass  # best-effort; never block startup


def get_session() -> Session:
    return Session(_engine)


def engine():
    return _engine
