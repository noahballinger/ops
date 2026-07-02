#!/usr/bin/env python3
"""Audit: are there III/OUT outgoing deliveries NOT backed by a sale/POS order?

If online sales always create a sale.order, then counting sale.order.line (as
the engine does) already captures them and III/OUT deliveries would all be
linked. This script checks that assumption against the live instance:

  * pulls outgoing ("III/OUT") deliveries that are DONE in the trailing window,
  * classifies each as sale-backed / POS-backed / UNLINKED,
  * tallies shipped units for the UNLINKED ones (the potential blind spot).

Read-only: uses the same allow-listed client as the app. Run from the repo
root with the project's .env present:

    python3 scripts/audit_iii_out.py            # last 24 months
    python3 scripts/audit_iii_out.py --months 12
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # strip inline comments and surrounding whitespace/quotes
        v = v.split("#", 1)[0].strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--name", default="III/OUT",
                    help="picking name prefix to match (default III/OUT)")
    args = ap.parse_args()

    _load_dotenv()
    from app.datasources.odoo_json import OdooReadOnlyError  # noqa: F401
    from app.datasources.odoo_json import OdooJsonDataSource

    ds = OdooJsonDataSource.from_env()
    ds.authenticate()
    print(f"Connected to {ds.base_url} (db={ds.db})\n")

    since = (datetime.now(timezone.utc) - timedelta(days=args.months * 31)) \
        .strftime("%Y-%m-%d %H:%M:%S")

    # Which linkage fields does stock.picking expose on this instance?
    pfields = ds.fields_of("stock.picking")
    link_candidates = ["sale_id", "pos_order_id", "group_id", "origin",
                       "partner_id", "date_done"]
    have = [f for f in link_candidates if f in pfields]
    fields = ["name", "state"] + have
    print("stock.picking link fields available:",
          ", ".join(have) or "(none beyond name/state)", "\n")

    # The III/OUT name prefix already identifies outgoing deliveries, so we
    # don't filter on picking type (the field name varies across instances).
    domain = [["state", "=", "done"],
              ["date_done", ">=", since],
              ["name", "like", args.name]]
    pickings = ds._search_read_paged("stock.picking", domain, fields)
    print(f"{len(pickings)} done outgoing '{args.name}' deliveries since {since[:10]}\n")
    if not pickings:
        print("Nothing to audit.")
        return 0

    def classify(p: dict) -> str:
        if p.get("sale_id"):
            return "sale"
        if p.get("pos_order_id"):
            return "pos"
        origin = (p.get("origin") or "")
        up = origin.upper()
        if "POS" in up:
            return "pos"
        # sale order sequences usually look like S00001 / SO0001
        if origin[:1] in ("S",) or "SALE" in up:
            return "sale"
        return "UNLINKED"

    buckets = defaultdict(list)
    for p in pickings:
        buckets[classify(p)].append(p)

    total = len(pickings)
    for k in ("sale", "pos", "UNLINKED"):
        n = len(buckets[k])
        print(f"  {k:9} {n:6}  ({n/total*100:5.1f}%)")
    print()

    unlinked = buckets["UNLINKED"]
    if not unlinked:
        print("RESULT: every III/OUT delivery is backed by a sale or POS order.")
        print("        Counting sale.order.line + pos.order.line misses nothing.")
        return 0

    # Tally shipped units for the unlinked deliveries (the blind spot).
    ids = [p["id"] for p in unlinked]
    units = 0.0
    by_origin = defaultdict(int)
    for i in range(0, len(ids), 200):
        moves = ds._search_read_paged(
            "stock.move",
            [["picking_id", "in", ids[i:i + 200]], ["state", "=", "done"]],
            ["product_uom_qty"])
        for m in moves:
            units += m.get("product_uom_qty") or 0.0
    for p in unlinked:
        by_origin[(p.get("origin") or "(blank)")] += 1

    print(f"RESULT: {len(unlinked)} UNLINKED III/OUT deliveries "
          f"shipping ~{units:,.0f} units are NOT counted as sales.")
    print("Top origins among the unlinked (helps identify the channel):")
    for origin, n in sorted(by_origin.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {n:5}  {origin}")
    print("\nSample delivery names:")
    for p in unlinked[:10]:
        print(f"  {p['name']:18}  origin={p.get('origin') or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
