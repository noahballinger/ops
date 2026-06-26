"""
The single SKU resolver.  Global SKU is the spine; everything maps through it.

    Global SKU  <->  US SKU  <->  Odoo Internal Reference

Build it once from the product catalogue and reuse everywhere.  All lookups
are case-insensitive and whitespace-trimmed because the source data is messy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


def _norm(s: Optional[str]) -> str:
    return (s or "").strip()


@dataclass
class SkuResolver:
    by_global: Dict[str, str] = field(default_factory=dict)
    _us_to_global: Dict[str, str] = field(default_factory=dict)
    _ref_to_global: Dict[str, str] = field(default_factory=dict)
    _global_to_us: Dict[str, str] = field(default_factory=dict)

    def register(self, global_sku: str, us_sku: str = "", odoo_ref: str = "") -> None:
        g = _norm(global_sku)
        if not g:
            return
        self.by_global[g] = g
        if us_sku:
            self._us_to_global[_norm(us_sku).upper()] = g
            self._global_to_us[g] = _norm(us_sku)
        if odoo_ref:
            self._ref_to_global[_norm(odoo_ref).upper()] = g

    def to_global(self, any_key: str) -> Optional[str]:
        k = _norm(any_key)
        if not k:
            return None
        if k in self.by_global:
            return k
        ku = k.upper()
        return (self._us_to_global.get(ku)
                or self._ref_to_global.get(ku)
                or (k if k in self.by_global else None))

    def to_us(self, global_sku: str) -> str:
        return self._global_to_us.get(_norm(global_sku), "")
