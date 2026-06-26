"""
Business-rule configuration for the Isha Life USA import ordering tool.

Every threshold here is derived from the spec-of-record workbook
(`Copy of USA INV CHK.xlsx`) and is intended to be *configurable*.  The
defaults below are the values the workbook actually uses today; a deployment
can override any of them via `config_overrides.json` (loaded at import time)
without touching code.

Provenance of each constant is noted inline so a buyer can audit it against
the workbook.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict

# --------------------------------------------------------------------------
# Lead-time horizon (months).  Goods ordered now land 4-6 months later.
# Source: §2 of the build spec; SEA sheet projects 6 months, AIR ~4.
# --------------------------------------------------------------------------
SEA_LEAD_MONTHS = 6          # sea container lands ~month 6
AIR_LEAD_MONTHS = 4          # air lands ~month 4
PROJECTION_HORIZON = 6       # we project MOH out this many months

# Near-term floor used for the AIR decision.
# Source: SEA sheet col U  ->  =IF(N2<3, 3-N2, 0)   (N2 = OH at month 4)
AIR_NEARTERM_FLOOR_MOH = 3.0

# Expiry buffer: months of shelf life that must remain for stock to be
# "useable".  Source: EXP INV col I "MIN MONS FOR SALE" = 1.5
MIN_MONTHS_FOR_SALE = 1.5

# Default case / pack size when a SKU has no explicit case size.
DEFAULT_CASE_SIZE = 1

# --------------------------------------------------------------------------
# Per-category target months-on-hand (the "refill to" target for SEA).
# Derived empirically from the SEA sheet's MTHS REQ column grouped by
# CATEGORY.  Where the workbook used more than one value for a category we
# take the modal / most common value and treat per-SKU overrides separately.
#   ACCESSORY 6 | CLOTHING 6 | BODY CARE 6-8 | everything else 8 |
#   CONX/INCENSE/TEMPLE/YOGA STORE up to 10-12 for slow movers
# Default target MOH if a category is unknown:
# --------------------------------------------------------------------------
DEFAULT_TARGET_MOH = 8.0

CATEGORY_TARGET_MOH: Dict[str, float] = {
    "ACCESSORY": 6.0,
    "CLOTHING": 6.0,
    "BODY CARE": 8.0,
    "BOOK": 8.0,
    "CONX": 8.0,
    "COPPER": 8.0,
    "CRAFT": 8.0,
    "INCENSE": 8.0,
    "INVENTORY": 8.0,
    "JEWELRY": 8.0,
    "NATURAL FOOD": 8.0,
    "PHOTO": 8.0,
    "TEMPLE": 8.0,
    "YOGA STORE": 8.0,
    "A & A": 8.0,
}

# Per-category default case sizes (overridable per SKU).
# BLOOM (Ecocert organic cosmetics) ship in cases of 24 or 32 (BLOOM sheet
# col M).  Clothing is sold in packs of 8 ("-8P").
CATEGORY_CASE_SIZE: Dict[str, int] = {
    "BLOOM": 32,
    "CLOTHING": 8,
}

# Source tags for a product (drives whether sea/air applies at all).
SOURCE_IMPORT_SEA = "IMPORT_SEA"
SOURCE_IMPORT_AIR_ELIGIBLE = "IMPORT_AIR_ELIGIBLE"
SOURCE_DOMESTIC = "DOMESTIC"

# Domestic vendors seen in the workbook (DOMESTIC sheet) -> these are not
# imported; they follow MOQ-when-low logic, no sea/air decision.
DOMESTIC_MOQ_TRIGGER_MOH = 4.0   # DOMESTIC col I: =IF(F2<4,"YES","NO")


@dataclass
class ForecastConfig:
    """Tunables for the demand forecaster (kept here so the forecaster stays
    a pure function that simply receives this object)."""
    min_months_for_seasonal: int = 24   # need ~2 yrs to trust seasonality
    min_months_for_trend: int = 12
    low_confidence_months: int = 6      # below this -> fall back to baseline
    divergence_flag_pct: float = 0.30   # forecast vs baseline gap to flag
    seasonal_period: int = 12


@dataclass
class EngineConfig:
    """Everything the pure suggestion engine needs.  No I/O lives here."""
    sea_lead_months: int = SEA_LEAD_MONTHS
    air_lead_months: int = AIR_LEAD_MONTHS
    horizon: int = PROJECTION_HORIZON
    air_nearterm_floor_moh: float = AIR_NEARTERM_FLOOR_MOH
    min_months_for_sale: float = MIN_MONTHS_FOR_SALE
    default_target_moh: float = DEFAULT_TARGET_MOH
    default_case_size: int = DEFAULT_CASE_SIZE
    domestic_moq_trigger_moh: float = DOMESTIC_MOQ_TRIGGER_MOH
    category_target_moh: Dict[str, float] = field(
        default_factory=lambda: dict(CATEGORY_TARGET_MOH))
    category_case_size: Dict[str, int] = field(
        default_factory=lambda: dict(CATEGORY_CASE_SIZE))
    forecast: ForecastConfig = field(default_factory=ForecastConfig)

    def target_moh_for(self, category: str | None,
                       override: float | None = None) -> float:
        if override is not None:
            return float(override)
        if category:
            return self.category_target_moh.get(
                category.strip().upper(), self.default_target_moh)
        return self.default_target_moh

    def case_size_for(self, category: str | None,
                      override: int | None = None) -> int:
        if override:
            return int(override)
        if category:
            return self.category_case_size.get(
                category.strip().upper(), self.default_case_size)
        return self.default_case_size


def load_config() -> EngineConfig:
    """Load defaults, then merge any deployment overrides from
    config_overrides.json sitting next to this file."""
    cfg = EngineConfig()
    path = os.path.join(os.path.dirname(__file__), "config_overrides.json")
    if os.path.exists(path):
        with open(path) as fh:
            ov = json.load(fh)
        for k, v in ov.items():
            if k == "category_target_moh":
                cfg.category_target_moh.update({k2.upper(): float(v2)
                                                for k2, v2 in v.items()})
            elif k == "category_case_size":
                cfg.category_case_size.update({k2.upper(): int(v2)
                                               for k2, v2 in v.items()})
            elif hasattr(cfg, k):
                setattr(cfg, k, v)
    return cfg
