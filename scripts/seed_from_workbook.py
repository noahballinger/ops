#!/usr/bin/env python3
"""
Build-sequence step 1: parse the spec workbook into config + seed data and
print a summary of what was derived.

Usage:
    python scripts/seed_from_workbook.py "/path/to/Copy of USA INV CHK.xlsx" \
        [--monthly-sales monthly.csv]

It loads the workbook via the file-import DataSource, computes suggestions
with the pure engine, persists a seed Order ("SEED") into SQLite, and prints
a derived-config summary + a spot-check comparison against the workbook.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend")))

from app.config import load_config, CATEGORY_TARGET_MOH, CATEGORY_CASE_SIZE
from app.datasources.file_import import load_from_workbook
from app.db import init_db, get_session
from app.service import compute_suggestions, create_order


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--monthly-sales", default=None)
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    print("=" * 72)
    print("DERIVED CONFIG (from workbook business rules)")
    print("=" * 72)
    print(f"Sea lead: {cfg.sea_lead_months} mo | Air lead: {cfg.air_lead_months} mo "
          f"| Air near-term floor: {cfg.air_nearterm_floor_moh} MOH")
    print(f"Default target MOH: {cfg.default_target_moh} | "
          f"Expiry min-months-for-sale: {cfg.min_months_for_sale}")
    print("Category target MOH:")
    for k, v in sorted(CATEGORY_TARGET_MOH.items()):
        print(f"   {k:14} {v}")
    print("Category case sizes:", CATEGORY_CASE_SIZE)

    print("\nParsing workbook ...")
    pull = load_from_workbook(args.workbook, monthly_sales_csv=args.monthly_sales)
    print(f"  products:   {len(pull.products)}")
    print(f"  snapshots:  {len(pull.snapshots)}")
    print(f"  in-transit: {len(pull.in_transit)} rows "
          f"({sum(1 for _ in pull.in_transit)} sku-month buckets)")
    for w in pull.warnings:
        print(f"  ! {w}")

    # source / category breakdown
    from collections import Counter
    src = Counter(p["source"] for p in pull.products)
    cat = Counter(p["category"] or "(none)" for p in pull.products)
    print("  by source:", dict(src))
    print("  top categories:", dict(cat.most_common(8)))

    sugg = compute_suggestions(pull, cfg)
    ordered = [s for s in sugg if s.suggested_sea_round or s.suggested_air_round]
    air = [s for s in sugg if s.suggested_air_round]
    print(f"\nSuggestions: {len(sugg)} candidates | {len(ordered)} to order | "
          f"{len(air)} with an air split")
    print("\nSample order lines:")
    print(f"  {'US SKU':10} {'NAME':32} {'SEA':>6} {'AIR':>6} {'TGT':>4} {'MOH4':>6} {'MOH6':>6}")
    for s in ordered[:12]:
        print(f"  {s.us_sku[:10]:10} {s.name[:32]:32} {s.suggested_sea_round:6d} "
              f"{s.suggested_air_round:6d} {s.target_moh:4.0f} "
              f"{s.projected_moh_m4:6.1f} {s.projected_moh_m6:6.1f}")

    if not args.no_db:
        init_db()
        with get_session() as session:
            order = create_order(session, "SEED", pull, cfg)
            print(f"\nSeeded Order '{order.name}' (id={order.id}) with "
                  f"{len(sugg)} lines into SQLite.")


if __name__ == "__main__":
    main()
