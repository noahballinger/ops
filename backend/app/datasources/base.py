"""
The DataSource interface.  Both the file-import path and the live Odoo
JSON-endpoint client implement it, so the rest of the app never cares which
one produced the data ("graceful degradation" -- §3).

A pull returns three lists of plain dicts:
  * products       -> catalogue rows (one per Global SKU)
  * snapshots      -> per-SKU on-hand + monthly sales time series
  * in_transit     -> per-SKU incoming quantity bucketed by arrival month

Keeping the contract as dicts (not ORM objects) keeps the sources decoupled
from persistence and trivially serialisable for caching.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Protocol


@dataclass
class PullResult:
    products: List[Dict[str, Any]] = field(default_factory=list)
    snapshots: List[Dict[str, Any]] = field(default_factory=list)
    in_transit: List[Dict[str, Any]] = field(default_factory=list)
    source: str = "unknown"          # "file_import" | "odoo_live"
    warnings: List[str] = field(default_factory=list)


class DataSource(Protocol):
    name: str

    def pull(self) -> PullResult:
        """Fetch everything needed to compute suggestions."""
        ...
