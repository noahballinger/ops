"""
Live Odoo 19 DataSource over the standard web JSON endpoints -- NO External
API (no API key, no XML-RPC).  We authenticate as a normal user and exchange
JSON over the same endpoints the Odoo web client uses.

STRICTLY READ-ONLY.  Only `search_read` / `read_group` / `read` / `fields_get`
are ever called.  There is no code path here that can create/write/unlink.
Ordering output leaves the system as files only.

Caching: every read is cached on disk (keyed by model+method+args) for a TTL,
and "refresh" is explicit -- we never poll.  Large reads are paged.

Credentials: passed in at runtime, held only in memory for the session,
never logged, never written to disk, never placed in URLs/query strings.

This client implements the same `PullResult` contract as the file importer,
so the rest of the app is identical whether data came from a file or Odoo.
Field/model names are confirmed against the live instance via `fields_get`
rather than hard-coded, because Odoo deployments customise them (§5).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, List, Optional

import requests

from .base import PullResult
from ..config import SOURCE_IMPORT_SEA


class OdooReadOnlyError(RuntimeError):
    """Raised if a write-style method is ever requested."""


_ALLOWED_METHODS = {"search_read", "read_group", "read", "fields_get",
                    "search", "search_count"}


class OdooJsonDataSource:
    name = "odoo_live"

    @classmethod
    def from_env(cls) -> "OdooJsonDataSource":
        """Build a client from environment variables (credentials are read
        from the process env, never written to disk or the repo).

        Connection:   ODOO_BASE_URL, ODOO_DB, ODOO_LOGIN, ODOO_PASSWORD
        Optional:     ODOO_WAREHOUSE, ODOO_SALES_MODEL (default sale.report)
        Cache:        ODOO_CACHE_DIR (default backend/data/odoo_cache),
                      ODOO_CACHE_TTL_SECONDS (default 3600),
                      ODOO_PAGE_SIZE (default 2000),
                      ODOO_THROTTLE_SECONDS (default 0.2)
        """
        missing = [k for k in ("ODOO_BASE_URL", "ODOO_DB", "ODOO_LOGIN",
                               "ODOO_PASSWORD") if not os.environ.get(k)]
        if missing:
            raise RuntimeError("Missing Odoo env vars: " + ", ".join(missing))
        return cls(
            base_url=os.environ["ODOO_BASE_URL"], db=os.environ["ODOO_DB"],
            login=os.environ["ODOO_LOGIN"], password=os.environ["ODOO_PASSWORD"],
            warehouse=os.environ.get("ODOO_WAREHOUSE") or None,
            sales_model=os.environ.get("ODOO_SALES_MODEL", "sale.report"),
            cache_dir=os.environ.get("ODOO_CACHE_DIR") or None,
            cache_ttl_seconds=int(os.environ.get("ODOO_CACHE_TTL_SECONDS", "3600")),
            page_size=int(os.environ.get("ODOO_PAGE_SIZE", "2000")),
            throttle_seconds=float(os.environ.get("ODOO_THROTTLE_SECONDS", "0.2")),
            disk_cache=os.environ.get("ODOO_DISK_CACHE", "0").lower()
            in ("1", "true", "yes"))

    def cache_info(self) -> dict:
        """Cache configuration + current state (for the /api/odoo/status route)."""
        files = [f for f in os.listdir(self.cache_dir) if f.endswith(".json")] \
            if (self.disk_cache and os.path.isdir(self.cache_dir)) else []
        return {"disk_cache": self.disk_cache,
                "cache_dir": os.path.abspath(self.cache_dir),
                "cache_ttl_seconds": self.cache_ttl,
                "page_size": self.page_size, "throttle_seconds": self.throttle,
                "cached_reads": len(files)}

    def __init__(self, base_url: str, db: str, login: str, password: str,
                 warehouse: Optional[str] = None,
                 sales_model: str = "sale.report",
                 cache_dir: Optional[str] = None,
                 cache_ttl_seconds: int = 3600,
                 page_size: int = 2000,
                 throttle_seconds: float = 0.2,
                 disk_cache: bool = False):
        self.base_url = base_url.rstrip("/")
        self.db = db
        self._login = login
        self._password = password           # held in memory only
        self.warehouse = warehouse
        self.sales_model = sales_model
        # Per-read disk cache is OFF by default. The single cache layer is the
        # DB snapshot batch written by the sync; the disk cache was redundant
        # and grew without bound. Opt in with ODOO_DISK_CACHE=1 if ever needed.
        self.disk_cache = disk_cache
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "odoo_cache")
        self.cache_ttl = cache_ttl_seconds
        self.page_size = page_size
        self.throttle = throttle_seconds
        self._sess = requests.Session()
        self._uid: Optional[int] = None
        if self.disk_cache:
            os.makedirs(self.cache_dir, exist_ok=True)

    # --------------------------------------------------------------- auth
    def authenticate(self) -> None:
        # If no db is configured, try to auto-detect a single-db instance
        # (skubot pattern). DB listing is often disabled/proxied, so this is
        # best-effort -- an explicit ODOO_DB is the reliable path.
        db = self.db or self._detect_single_db()
        if not db:
            raise RuntimeError(
                "No Odoo database set and it couldn't be auto-detected. "
                "Set ODOO_DB to the exact database name (the value in the "
                "login screen's database selector).")
        self.db = db
        url = f"{self.base_url}/web/session/authenticate"
        payload = {"jsonrpc": "2.0", "method": "call", "params": {
            "db": db, "login": self._login, "password": self._password}}
        r = self._sess.post(url, json=payload, timeout=30)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(
                f"Non-JSON response from Odoo (HTTP {r.status_code}). A proxy/"
                f"WAF (e.g. Cloudflare) may be blocking the request.")
        if data.get("error"):
            raise RuntimeError("Odoo auth failed: " + _err_msg(data["error"]))
        self._uid = (data.get("result") or {}).get("uid")
        if not self._uid:
            raise RuntimeError("Odoo auth returned no uid. Check ODOO_DB / login / "
                               "password, and ensure 2FA is DISABLED on this login.")

    def _detect_single_db(self) -> Optional[str]:
        dbs = self.list_databases()
        return dbs[0] if isinstance(dbs, list) and len(dbs) == 1 else None

    def list_databases(self) -> List[str]:
        try:
            r = self._sess.post(f"{self.base_url}/web/database/list",
                                json={"jsonrpc": "2.0", "method": "call",
                                      "params": {}}, timeout=30)
            return r.json().get("result", []) or []
        except Exception:
            return []

    # --------------------------------------------------------------- read
    def _call_kw(self, model: str, method: str, args: list,
                 kwargs: Optional[dict] = None, use_cache: bool = True) -> Any:
        if method not in _ALLOWED_METHODS:
            raise OdooReadOnlyError(
                f"Refusing non-read method '{method}'. This client is read-only.")
        kwargs = kwargs or {}
        ck = self._cache_key(model, method, args, kwargs)
        if use_cache:
            cached = self._cache_get(ck)
            if cached is not None:
                return cached
        if self._uid is None:
            self.authenticate()
        url = f"{self.base_url}/web/dataset/call_kw/{model}/{method}"
        payload = {"jsonrpc": "2.0", "method": "call", "params": {
            "model": model, "method": method, "args": args, "kwargs": kwargs}}
        time.sleep(self.throttle)
        r = self._sess.post(url, json=payload, timeout=120)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Odoo {model}.{method} error: "
                               + _err_msg(data["error"]))
        result = data.get("result")
        if use_cache:
            self._cache_put(ck, result)
        return result

    def _search_read_paged(self, model: str, domain: list, fields: list,
                           order: Optional[str] = None) -> List[dict]:
        out: List[dict] = []
        offset = 0
        while True:
            kwargs = {"fields": fields, "limit": self.page_size, "offset": offset}
            if order:
                kwargs["order"] = order
            chunk = self._call_kw(model, "search_read", [domain], kwargs)
            if not chunk:
                break
            out.extend(chunk)
            if len(chunk) < self.page_size:
                break
            offset += self.page_size
        return out

    def fields_of(self, model: str) -> Dict[str, dict]:
        return self._call_kw(model, "fields_get", [], {"attributes": ["type", "string"]})

    def _model_exists(self, model: str) -> bool:
        try:
            self._call_kw(model, "fields_get", [], {"attributes": ["string"]})
            return True
        except Exception:
            return False

    def _sales_series(self, pid_to_code: Dict[int, str], months: int,
                      res: PullResult):
        """Build a per-product monthly sales series from POS + sale order lines
        over the trailing `months`. Sales velocity drives the order engine, so
        this is the number that decides what gets ordered.

        Returns ({code: [ {year, month, units, is_stockout} ... ]}, {code: total}).
        """
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(days=months * 31)) \
            .strftime("%Y-%m-%d %H:%M:%S")
        # (line model, qty field, parent order model, accepted parent states)
        # Only COUNT confirmed sales: skip draft quotations and cancelled
        # orders. POS register sales are paid/done/invoiced; sale orders are
        # confirmed once state is 'sale' (or 'done' for locked orders).
        sources = [("pos.order.line", "qty", "pos.order",
                    ["paid", "done", "invoiced"]),
                   ("sale.order.line", "product_uom_qty", "sale.order",
                    ["sale", "done"])]
        # bucket[code][(y,m)] = units
        bucket: Dict[str, Dict[tuple, float]] = {}
        total: Dict[str, float] = {}
        any_source = False
        for line_model, qty_field, order_model, states in sources:
            if not self._model_exists(line_model):
                continue
            any_source = True
            try:
                lines = self._search_read_paged(
                    line_model,
                    [["order_id.date_order", ">=", since],
                     ["order_id.state", "in", states]],
                    ["product_id", "order_id", qty_field])
            except Exception as e:
                res.warnings.append(f"{line_model} read failed: {e}")
                continue
            if not lines:
                continue
            # fetch parent-order dates in chunks
            oids = sorted({l["order_id"][0] for l in lines if l.get("order_id")})
            dates: Dict[int, str] = {}
            for i in range(0, len(oids), 500):
                for o in self._search_read_paged(
                        order_model, [["id", "in", oids[i:i + 500]]],
                        ["id", "date_order"]):
                    dates[o["id"]] = o.get("date_order") or ""
            for l in lines:
                pidf = l.get("product_id")
                if not pidf:
                    continue
                pid = pidf[0]
                code = pid_to_code.get(pid) or _parse_code(
                    pidf[1] if isinstance(pidf, list) else "")
                if not code:
                    continue
                oid = l["order_id"][0] if l.get("order_id") else None
                y, m = _parse_date_ym(dates.get(oid, ""))
                if not y:
                    continue
                qty = l.get(qty_field) or 0.0
                bucket.setdefault(code, {})
                bucket[code][(y, m)] = bucket[code].get((y, m), 0.0) + qty
                total[code] = total.get(code, 0.0) + qty
        if not any_source:
            res.warnings.append("Neither pos.order.line nor sale.order.line is "
                                "available; no sales history could be read.")
        series: Dict[str, List[dict]] = {}
        for code, months_map in bucket.items():
            series[code] = [
                {"year": y, "month": m, "units": u, "is_stockout": False}
                for (y, m), u in sorted(months_map.items())]
        return series, total

    # --------------------------------------------------------------- pull
    def pull(self, sales_months: int = 24) -> PullResult:
        res = PullResult(source="odoo_live")
        try:
            self.authenticate()
        except Exception as e:  # graceful: caller can fall back to file import
            res.warnings.append(f"Odoo auth failed ({e}); use file import instead.")
            return res

        prod_fields = self._safe_fields(
            "product.product",
            ["default_code", "name", "categ_id", "qty_available", "free_qty",
             "standard_price", "list_price", "barcode",
             # country-of-origin field name varies by Odoo version / install:
             "country_of_origin", "origin_country_id", "x_country_of_origin"])
        products = self._search_read_paged(
            "product.product", [["sale_ok", "=", True]], prod_fields)

        # Monthly sales from POS + online order lines (the skubot method on
        # this instance). The POS lines are the "<wh>/POS/<n>" sales; the
        # aggregate read_group on sale.report returns nothing here.
        pid_to_code = {p["id"]: _parse_code(p.get("default_code") or "")
                       for p in products if p.get("id")}
        sales_series, sales_total = self._sales_series(pid_to_code, sales_months, res)

        # in-transit from incoming stock moves (best-effort)
        in_transit = self._incoming(res)

        allow = _order_list_allowlist()
        seen_codes = set()
        for p in products:
            code = _parse_code(p.get("default_code") or "")
            if allow and code not in allow:
                continue   # restrict to the workbook ORDER LIST SKUs
            # skip blank / placeholder codes and duplicates (Odoo variants can
            # share a default_code); the catalogue is keyed by global SKU.
            if not code or code.lower() in ("---", "false", "none"):
                continue
            if code in seen_codes:
                continue
            seen_codes.add(code)
            categ = p.get("categ_id")
            cat = categ[1] if isinstance(categ, list) else ""
            # Country of origin (many2one -> [id, name], or a plain string).
            origin = _origin_name(p)
            # Exclude anything whose origin is KNOWN and not India.
            if origin and "india" not in origin.lower():
                continue
            # Snacks are domestically sourced — never part of the India import.
            src = SOURCE_DOMESTIC if "snack" in (cat or "").lower() \
                else classify_source(p.get("default_code") or code)
            res.products.append({
                "odoo_id": p.get("id"),
                "origin": origin,
                "global_sku": code,
                "us_sku": code,
                "barcode": p.get("barcode") or "",
                "odoo_internal_ref": p.get("default_code") or code,
                "name": p.get("name", ""),
                "category": cat,
                "case_size": 1,
                "hsn_code": "",
                "cost": p.get("standard_price") or 0.0,
                "retail_price": p.get("list_price") or 0.0,
                "compliance_flag": "",
                "source": src,
                "expiry_tracked": False,
                "moq": None, "target_moh_override": None, "vendor": "",
            })
            series = sorted(sales_series.get(code, []),
                            key=lambda d: (d["year"], d["month"]))
            # Sell-through velocity: total sold / months actually in stock
            # (i.e. months that recorded sales), so stock-outs don't deflate it.
            sold_total = sum(d["units"] for d in series)
            months_active = len(series)
            avg = (sold_total / months_active) if months_active \
                else (sales_total.get(code, 0.0) / 12.0)
            on_hand = p.get("free_qty")
            if on_hand is None:
                on_hand = p.get("qty_available") or 0.0
            res.snapshots.append({
                "global_sku": code, "on_hand": on_hand,
                "useable_on_hand": on_hand, "avg_monthly_sales": avg,
                "units_sold": sold_total, "months_active": months_active,
                "monthly_sales_series": series, "source": "odoo_live"})

        res.in_transit = in_transit
        return res

    def _incoming(self, res: PullResult) -> List[dict]:
        try:
            moves = self._search_read_paged(
                "stock.move",
                [["state", "in", ["assigned", "confirmed", "waiting", "partially_available"]],
                 ["picking_code", "=", "incoming"]],
                ["product_id", "product_qty", "date"])
        except Exception as e:
            res.warnings.append(f"incoming stock.move read failed ({e}); "
                                f"supply in-transit via file import.")
            return []
        from datetime import date as _d
        today = _d.today()
        out = []
        for mv in moves:
            pid = mv.get("product_id")
            code = _parse_code(pid[1] if isinstance(pid, list) else "")
            dt = (mv.get("date") or "")[:10]
            month_idx = 1
            try:
                y, m, _ = dt.split("-")
                month_idx = max(1, min(6, (int(y) * 12 + int(m)) -
                                       (today.year * 12 + today.month) + 1))
            except Exception:
                pass
            out.append({"global_sku": code, "quantity": mv.get("product_qty") or 0.0,
                        "expected_arrival_month": month_idx, "shipment_label": ""})
        return out

    # ------------------------------------------------------------- helpers
    def _safe_fields(self, model: str, wanted: List[str]) -> List[str]:
        """Keep only fields the live model actually exposes (Odoo customises)."""
        try:
            have = set(self.fields_of(model).keys())
            keep = [f for f in wanted if f in have]
            return keep or ["name"]
        except Exception:
            return wanted

    def _cache_key(self, *parts) -> str:
        raw = json.dumps(parts, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _cache_get(self, key: str):
        if not self.disk_cache:
            return None
        p = os.path.join(self.cache_dir, key + ".json")
        if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < self.cache_ttl:
            with open(p) as fh:
                return json.load(fh)
        return None

    def _cache_put(self, key: str, value) -> None:
        if not self.disk_cache:
            return
        with open(os.path.join(self.cache_dir, key + ".json"), "w") as fh:
            json.dump(value, fh)

    def clear_cache(self) -> None:
        if not os.path.isdir(self.cache_dir):
            return
        for f in os.listdir(self.cache_dir):
            if f.endswith(".json"):
                os.remove(os.path.join(self.cache_dir, f))


import re as _re
# India-import internal references look like 2 letters + 10 digits, e.g.
# "CA0023000009". Anything else is treated as domestic (US-sourced) for now.
_INDIA_REF = _re.compile(r"^[A-Za-z]{2}\d{10}$")


def classify_source(internal_ref: str) -> str:
    from ..config import SOURCE_IMPORT_SEA, SOURCE_DOMESTIC
    ref = (internal_ref or "").strip()
    return SOURCE_IMPORT_SEA if _INDIA_REF.match(ref) else SOURCE_DOMESTIC


_ALLOWLIST_CACHE = None
def _order_list_allowlist():
    """Set of Global SKUs from the workbook 'ORDER LIST' sheet, if present."""
    global _ALLOWLIST_CACHE
    if _ALLOWLIST_CACHE is None:
        p = os.path.join(os.path.dirname(__file__), "..", "..", "data",
                         "order_list_skus.json")
        try:
            with open(p) as fh:
                _ALLOWLIST_CACHE = set(json.load(fh))
        except Exception:
            _ALLOWLIST_CACHE = set()
    return _ALLOWLIST_CACHE


def _origin_name(p: dict) -> str:
    """Extract country-of-origin name from whichever field the install uses."""
    for f in ("country_of_origin", "origin_country_id", "x_country_of_origin"):
        v = p.get(f)
        if isinstance(v, list) and len(v) == 2:
            return str(v[1]).strip()
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _parse_date_ym(s: str):
    """'2025-06-14 10:22:01' -> (2025, 6). Returns (None, None) if unparseable."""
    s = (s or "").strip()
    if len(s) >= 7 and s[:4].isdigit() and s[4] == "-":
        try:
            return int(s[:4]), int(s[5:7])
        except ValueError:
            return None, None
    return None, None


def _err_msg(err: dict) -> str:
    """Surface Odoo's real error. The top-level message is often the generic
    'Odoo Server Error'; the useful detail (e.g. wrong-db, AccessDenied) is in
    error.data."""
    data = err.get("data") or {}
    detail = data.get("message") or ""
    name = data.get("name") or ""
    top = err.get("message") or ""
    parts = [p for p in (top, name, detail) if p]
    # de-dup while preserving order
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p); out.append(p)
    return " | ".join(out) or str(err)


def _parse_code(display: str) -> str:
    s = (display or "").strip()
    if s.startswith("[") and "]" in s:
        return s[1:s.index("]")].strip()
    return s


def _parse_month_label(lbl: str):
    """Odoo date:month labels look like 'June 2025' or '2025-06'."""
    lbl = (lbl or "").strip()
    months = {m: i + 1 for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"])}
    if "-" in lbl and lbl[:4].isdigit():
        try:
            y, m = lbl[:7].split("-")
            return int(y), int(m)
        except Exception:
            return None, None
    parts = lbl.split()
    if len(parts) == 2 and parts[0].lower() in months:
        return int(parts[1]), months[parts[0].lower()]
    return None, None
