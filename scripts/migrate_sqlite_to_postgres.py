#!/usr/bin/env python3
"""Copy the local SQLite app database into Postgres.

This is intentionally a small one-shot migration helper, not a replacement for
Alembic. It creates the current SQLModel schema in Postgres and bulk-copies
rows table by table from the SQLite database.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, delete, func, inspect, select, text
from sqlmodel import SQLModel


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app import models  # noqa: E402,F401  register SQLModel tables


DEFAULT_SQLITE = BACKEND / "data" / "isha.db"
DEFAULT_POSTGRES = "postgresql+psycopg://isha:isha@localhost:5432/isha"


def normalize_postgres_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def reset_postgres_sequences(conn) -> None:
    for table in SQLModel.metadata.sorted_tables:
        for col in table.primary_key.columns:
            try:
                is_int_pk = col.type.python_type is int
            except NotImplementedError:
                is_int_pk = False
            if not is_int_pk:
                continue
            seq = conn.execute(
                text("select pg_get_serial_sequence(:table_name, :column_name)"),
                {"table_name": table.name, "column_name": col.name},
            ).scalar()
            if not seq:
                continue
            max_id = conn.execute(select(func.max(col))).scalar() or 0
            if max_id:
                conn.execute(text("select setval(:seq, :val, true)"), {
                    "seq": seq,
                    "val": int(max_id),
                })
            else:
                conn.execute(text("select setval(:seq, 1, false)"), {
                    "seq": seq,
                })


def fill_required_nulls(table, row: dict) -> dict:
    """SQLite can contain NULL in columns the current model treats as required.
    Postgres enforces those constraints, so normalize old NULLs to the same
    scalar defaults the app code expects."""
    out = dict(row)
    for col in table.columns:
        if out.get(col.name) is not None:
            continue
        try:
            typ = col.type.python_type
        except NotImplementedError:
            typ = None
        type_name = col.type.__class__.__name__.lower()
        if typ is str or "string" in type_name or "text" in type_name:
            out[col.name] = ""
            continue
        if col.nullable:
            continue
        elif typ is bool:
            out[col.name] = False
        elif typ is int:
            out[col.name] = 0
        elif typ is float:
            out[col.name] = 0.0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Copy backend/data/isha.db into the Postgres app database.")
    parser.add_argument("--sqlite", default=str(DEFAULT_SQLITE),
                        help="Path to source SQLite DB.")
    parser.add_argument("--postgres", default=os.environ.get(
        "DATABASE_URL", DEFAULT_POSTGRES),
        help="Target Postgres SQLAlchemy URL.")
    parser.add_argument("--replace", action="store_true",
                        help="Delete target table data before copying.")
    args = parser.parse_args()

    sqlite_path = Path(args.sqlite).expanduser().resolve()
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite database not found: {sqlite_path}")

    pg_url = normalize_postgres_url(args.postgres.strip())
    if not pg_url.startswith("postgresql+psycopg://"):
        raise SystemExit("Target must be a Postgres URL.")

    source = create_engine(f"sqlite:///{sqlite_path}")
    target = create_engine(pg_url, pool_pre_ping=True)

    source_tables = set(inspect(source).get_table_names())
    SQLModel.metadata.create_all(target)

    copied: list[tuple[str, int]] = []
    with source.connect() as src, target.begin() as dst:
        if args.replace:
            for table in reversed(SQLModel.metadata.sorted_tables):
                dst.execute(delete(table))

        for table in SQLModel.metadata.sorted_tables:
            if table.name not in source_tables:
                copied.append((table.name, 0))
                continue
            rows = [
                fill_required_nulls(table, dict(row))
                for row in src.execute(select(table)).mappings()
            ]
            if rows:
                dst.execute(table.insert(), rows)
            copied.append((table.name, len(rows)))

        reset_postgres_sequences(dst)

    print(f"Copied {sqlite_path} -> {pg_url}")
    for table_name, count in copied:
        print(f"{table_name}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
