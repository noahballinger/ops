"""Database engine + session helpers.

DATABASE_URL drives the backend:
  * Postgres (production):  postgresql+psycopg://user:pass@host:5432/isha
  * SQLite  (dev fallback): sqlite:///abs/path/isha.db   (default if unset)

We migrated to Postgres so Metabase (Reporting) can sit on the data and to
support concurrent reads/writes; SQLite stays available for quick local dev.
"""
from __future__ import annotations

import os
from sqlalchemy import event
from sqlmodel import SQLModel, create_engine, Session


def _resolve_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # normalise common forms to the psycopg3 driver
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    # SQLite fallback (dev)
    path = os.path.abspath(os.environ.get(
        "ISHA_DB_PATH",
        os.path.join(os.path.dirname(__file__), "..", "data", "isha.db")))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return f"sqlite:///{path}"


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


def init_db() -> None:
    from . import models  # noqa: F401  (register tables on the metadata)
    SQLModel.metadata.create_all(_engine)
    _auto_add_missing_columns()


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
