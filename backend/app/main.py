"""
FastAPI app: import data (3 modes), compute suggestions, review with editable
overrides, named orders, CSV/XLSX export.  Serves the React review UI at /.

Run:  uvicorn app.main:app --reload   (from the backend/ directory)
"""
import os
import secrets
import tempfile
import time
from datetime import datetime
from typing import Optional

from fastapi import (FastAPI, UploadFile, File, Form, HTTPException, Request,
                     Cookie, Depends)
from fastapi.responses import (HTMLResponse, PlainTextResponse, Response,
                               RedirectResponse)
from sqlmodel import select, delete

from .config import load_config
from .db import init_db, get_session
from .datasources.file_import import load_from_workbook, load_from_exports
from .export import rows_to_csv, rows_to_xlsx
from .models import (Order, OrderLine, Product, InventorySnapshot, InTransit,
                     ShipmentLeg, OrderLineEvent)
from .service import create_order, update_override, export_rows
from . import catalog
from . import access

# Auto-load .env (project root or backend/) so the web app has ODOO_* config
# regardless of how it's launched. Existing env vars win.
for _envp in (os.path.join(os.path.dirname(__file__), "..", "..", ".env"),
              os.path.join(os.path.dirname(__file__), "..", ".env")):
    if os.path.exists(_envp):
        for _line in open(_envp):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.split("#")[0].strip())

app = FastAPI(title="Isha Life USA Import Ordering Tool", version="1.0")
CFG = load_config()
FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "index.html")

# Idempotent: ensure tables exist at import time so the app works no matter
# how it is launched (uvicorn, TestClient, scripts).
init_db()

# --------------------------------------------------------------------------
# Server-side session store for Odoo credential login.
# A logged-in user's OdooClient (holding their password in memory ONLY) lives
# here keyed by an opaque token set as an httponly cookie. Nothing is written
# to disk; sessions expire after SESSION_TTL of inactivity.
# --------------------------------------------------------------------------
SESSIONS: dict = {}
SESSION_TTL = 8 * 3600
COOKIE = "isha_session"


def _session(token: Optional[str]) -> Optional[dict]:
    s = SESSIONS.get(token or "")
    if not s:
        return None
    if time.time() - s["last"] > SESSION_TTL:
        SESSIONS.pop(token, None)
        return None
    s["last"] = time.time()
    return s


def require_session(isha_session: Optional[str] = Cookie(default=None)) -> dict:
    s = _session(isha_session)
    if not s:
        raise HTTPException(401, "Not logged in. Sign in with your Odoo credentials.")
    return s


def require_admin(sess: dict = Depends(require_session)) -> dict:
    with get_session() as s:
        if not access.is_admin(s, sess.get("login", "")):
            raise HTTPException(403, "Admins only.")
    return sess


def require_list_access(sess: dict, list_key: str) -> None:
    """Raise 403 unless the logged-in user may order from `list_key`."""
    with get_session() as s:
        if not access.can_order(s, sess.get("login", ""), list_key):
            raise HTTPException(403, "You don't have access to this list.")


# --------------------------------------------------------------------- UI
@app.get("/", response_class=HTMLResponse)
def index():
    if os.path.exists(FRONTEND):
        with open(FRONTEND) as fh:
            return fh.read()
    return "<h1>Frontend not found</h1>"


# ------------------------------------------------------------------ auth
OAUTH_STATES: dict = {}


def _redirect_uri(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/api/auth/google/callback"


@app.get("/api/auth/google/login")
def google_login(request: Request):
    """Start 'Sign in with Google' — redirects the browser to Google."""
    from . import google_oauth
    if not os.path.exists(google_oauth.CLIENT_SECRET_FILE):
        raise HTTPException(500, "Google client secret not configured on server.")
    flow = google_oauth.login_flow(_redirect_uri(request))
    url, state = flow.authorization_url(access_type="online",
                                        include_granted_scopes="true",
                                        prompt="select_account")
    # stash the PKCE code_verifier — the token exchange needs the same one
    OAUTH_STATES[state] = {"ts": time.time(), "verifier": flow.code_verifier}
    return RedirectResponse(url)


@app.get("/api/auth/google/callback")
def google_callback(request: Request, code: str = "", state: str = ""):
    from . import google_oauth
    entry = OAUTH_STATES.pop(state, None)
    if not entry:
        raise HTTPException(400, "Invalid OAuth state.")
    flow = google_oauth.login_flow(_redirect_uri(request))
    flow.code_verifier = entry["verifier"]   # restore PKCE verifier
    try:
        flow.fetch_token(code=code)
        claims = google_oauth.verify_login_id_token(flow.credentials.id_token)
    except Exception as e:
        raise HTTPException(401, f"Google sign-in failed: {e}")
    email = claims.get("email", "")
    if not email or not claims.get("email_verified", True):
        raise HTTPException(401, "Google account has no verified email.")
    if not google_oauth.login_allowed(email):
        return HTMLResponse(f"<h3>Access denied for {email}</h3>"
                            "<p>Ask an admin to grant access.</p>", status_code=403)
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"login": email, "name": claims.get("name", ""),
                       "google": True, "last": time.time()}
    with get_session() as s:
        existed = access.user_exists(s, email)
        access.ensure_user(s, email, claims.get("name", ""))   # 1st user => admin
        # Notify coordinators the first time a brand-new user lands with no access.
        if (not existed) and access.is_pending(s, email):
            try:
                from .mailer import get_provider, compose_new_user_email
                manage = str(request.base_url).rstrip("/") + "/#/admin/users"
                subj, html = compose_new_user_email(email, claims.get("name", ""), manage)
                get_provider().send(
                    s, os.environ.get("NEW_USER_NOTIFY_TO", "noah.ballinger@ishalife.com"),
                    subj, html, kind="new_user",
                    cc=os.environ.get("NEW_USER_NOTIFY_CC", "sai.a@ishausa.org"))
            except Exception:
                pass   # never block sign-in on a notification failure
    resp = RedirectResponse("/")
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL)
    return resp


@app.post("/api/login")
def login(body: dict, response: Response):
    """Log in with Odoo credentials. Authenticates against Odoo; on success a
    server-side session holds the user's read-only client (password in memory
    only). URL/DB default to the server's env if not supplied."""
    from .datasources.odoo_json import OdooJsonDataSource
    base_url = (body.get("base_url") or os.environ.get("ODOO_BASE_URL", "")).strip()
    db = (body.get("db") or os.environ.get("ODOO_DB", "")).strip()
    login_id = (body.get("login") or "").strip()
    password = body.get("password") or ""
    if not base_url or not login_id or not password:
        raise HTTPException(400, "Odoo URL, login and password are required.")
    ds = OdooJsonDataSource(base_url, db, login_id, password,
                            warehouse=os.environ.get("ODOO_WAREHOUSE") or None,
                            sales_model=os.environ.get("ODOO_SALES_MODEL", "sale.report"))
    try:
        ds.authenticate()
    except Exception as e:
        raise HTTPException(401, f"Odoo login failed: {e}")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"client": ds, "login": login_id, "uid": ds._uid,
                       "db": ds.db, "last": time.time()}
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL)
    return {"ok": True, "login": login_id, "db": ds.db, "uid": ds._uid}


@app.post("/api/logout")
def logout(response: Response, isha_session: Optional[str] = Cookie(default=None)):
    SESSIONS.pop(isha_session or "", None)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
def me(isha_session: Optional[str] = Cookie(default=None)):
    s = _session(isha_session)
    if not s:
        return {"logged_in": False, "auth": "google",
                "odoo_url": os.environ.get("ODOO_BASE_URL", "")}
    with get_session() as db:
        access.ensure_user(db, s["login"], s.get("name", ""))
        lists = access.accessible_lists(db, s["login"])
        admin = access.is_admin(db, s["login"])
        pending = access.is_pending(db, s["login"])
    return {"logged_in": True, "login": s["login"], "name": s.get("name", ""),
            "is_admin": admin, "pending": pending, "lists": lists,
            "odoo_url": os.environ.get("ODOO_BASE_URL", "")}


# ----------------------------------------------------------------- config
@app.get("/api/config")
def get_config():
    return {
        "sea_lead_months": CFG.sea_lead_months,
        "air_lead_months": CFG.air_lead_months,
        "air_nearterm_floor_moh": CFG.air_nearterm_floor_moh,
        "default_target_moh": CFG.default_target_moh,
        "category_target_moh": CFG.category_target_moh,
        "category_case_size": CFG.category_case_size,
    }


# ------------------------------------------------------------------ orders
@app.get("/api/orders")
def list_orders(_: dict = Depends(require_session)):
    with get_session() as s:
        out = []
        for o in s.exec(select(Order).order_by(Order.created_at.desc())).all():
            n = len(s.exec(select(OrderLine).where(OrderLine.order_id == o.id)).all())
            out.append({"id": o.id, "name": o.name, "status": o.status,
                        "created_at": o.created_at.isoformat(), "lines": n,
                        "source_batch": o.snapshot_batch_id})
        return out


@app.delete("/api/orders/{order_id}")
def delete_order(order_id: int, _: dict = Depends(require_session)):
    """Delete an order and everything attached to it."""
    with get_session() as s:
        if not s.get(Order, order_id):
            raise HTTPException(404, "order not found")
        for m in (OrderLine, ShipmentLeg, OrderLineEvent):
            s.exec(delete(m).where(m.order_id == order_id))
        s.exec(delete(Order).where(Order.id == order_id))
        s.commit()
        return {"ok": True}


@app.delete("/api/orders/{order_id}/lines/{global_sku}")
def delete_line(order_id: int, global_sku: str, _: dict = Depends(require_session)):
    """Remove a single item from an order."""
    with get_session() as s:
        line = s.exec(select(OrderLine).where(
            OrderLine.order_id == order_id,
            OrderLine.global_sku == global_sku)).first()
        if not line:
            raise HTTPException(404, "line not found")
        s.delete(line)
        s.commit()
        from .service import log_event
        log_event(s, order_id, "discontinued", "Removed from order",
                  global_sku=global_sku)
        return {"ok": True}


@app.get("/api/email/status")
def email_status(_: dict = Depends(require_session)):
    from . import google_oauth
    st = google_oauth.status()
    from .mailer import get_provider
    st["provider"] = get_provider().name
    return st


@app.post("/api/orders/{order_id}/email")
def order_email(order_id: int, body: dict, _: dict = Depends(require_session)):
    """Compose + send an order email. mode = 'placement' (default) | 'changes'.
    Falls back to the stub provider (logs to MessageLog) until Gmail is authorized.
    Set body.preview=true to get the composed HTML without sending."""
    from .mailer import (get_provider, compose_order_email, compose_change_summary)
    with get_session() as s:
        order = s.get(Order, order_id)
        if not order:
            raise HTTPException(404, "order not found")
        mode = body.get("mode", "placement")
        if mode == "changes":
            evs = order_events(order_id, _)  # reuse the timeline builder
            subject, html = compose_change_summary(order.name, evs)
        else:
            subject, html = compose_order_email(order.name, export_rows(s, order_id))
        subject = body.get("subject") or subject
        if body.get("preview"):
            return {"subject": subject, "html": html}
        to = (body.get("to") or "").strip()
        if not to:
            raise HTTPException(400, "recipient 'to' is required")
        res = get_provider().send(s, to, subject, html, kind="order_"+mode)
        return {"ok": True, **res, "subject": subject}


@app.get("/api/orders/{order_id}/events")
def order_events(order_id: int, _: dict = Depends(require_session)):
    """Timeline of lifecycle events for an order (oldest→newest = left→right)."""
    with get_session() as s:
        evs = s.exec(select(OrderLineEvent).where(
            OrderLineEvent.order_id == order_id)).all()
        evs.sort(key=lambda e: (e.timestamp or datetime.min, e.id or 0))
        return [{"id": e.id, "order_id": e.order_id, "type": e.type, "note": e.note,
                 "actor": e.actor, "sku": e.source_quote,
                 "at": e.timestamp.isoformat() if e.timestamp else None}
                for e in evs]


@app.get("/api/events")
def all_events(sess: dict = Depends(require_session)):
    """Every lifecycle event across all orders (newest first) with this user's
    read/unread flag. Read state is stored PER USER on the server (EventAck)."""
    from .models import EventAck
    user = sess["login"]
    with get_session() as s:
        evs = s.exec(select(OrderLineEvent)).all()
        acked = {a.event_id for a in s.exec(
            select(EventAck).where(EventAck.user == user)).all()}
        evs.sort(key=lambda e: (e.timestamp or datetime.min, e.id or 0), reverse=True)
        items = [{"id": e.id, "order_id": e.order_id, "type": e.type,
                  "acked": e.id in acked,
                  "at": e.timestamp.isoformat() if e.timestamp else None} for e in evs]
    return {"events": items, "unacked": sum(1 for it in items if not it["acked"])}


@app.post("/api/events/ack")
def ack_events(body: dict, sess: dict = Depends(require_session)):
    """Mark events read ('clocked') for the current user. Body: {event_ids:[...]}
    or {order_id: N} to clock all of an order's events."""
    from .models import EventAck
    user = sess["login"]
    with get_session() as s:
        ids = set(body.get("event_ids") or [])
        if body.get("order_id") is not None:
            ids |= {e.id for e in s.exec(select(OrderLineEvent).where(
                OrderLineEvent.order_id == int(body["order_id"]))).all()}
        existing = {a.event_id for a in s.exec(
            select(EventAck).where(EventAck.user == user)).all()}
        for eid in ids:
            if eid not in existing:
                s.add(EventAck(user=user, event_id=int(eid)))
        s.commit()
        total = s.exec(select(OrderLineEvent)).all()
        acked = {a.event_id for a in s.exec(
            select(EventAck).where(EventAck.user == user)).all()}
        return {"ok": True, "unacked": sum(1 for e in total if e.id not in acked)}


@app.get("/api/orders/{order_id}/lines")
def order_lines(order_id: int, _: dict = Depends(require_session)):
    with get_session() as s:
        order = s.get(Order, order_id)
        if not order:
            raise HTTPException(404, "order not found")
        rows = []
        for line in s.exec(select(OrderLine).where(OrderLine.order_id == order_id)).all():
            p = s.get(Product, line.global_sku)
            sg = line.suggestion_json or {}
            rows.append({
                "global_sku": line.global_sku,
                "us_sku": p.us_sku if p else "",
                "barcode": p.barcode if p else "",
                "name": p.name if p else "",
                "category": p.category if p else "",
                "source": p.source if p else "",
                "origin": p.origin if p else "",
                "odoo_id": p.odoo_id if p else None,
                "compliance_flag": p.compliance_flag if p else "",
                # demand
                "avg_monthly_sales": sg.get("avg_monthly_sales"),
                "sell_through": sg.get("sell_through"),
                "units_sold": sg.get("units_sold"),
                "months_active": sg.get("months_active"),
                "forecast_mean": sg.get("forecast_mean"),
                "baseline_monthly_sales": sg.get("baseline_monthly_sales"),
                "forecast_monthly": sg.get("forecast_monthly"),
                "forecast_method": sg.get("forecast_method"),
                "forecast_confidence": sg.get("forecast_confidence"),
                "diverges_from_baseline": sg.get("diverges_from_baseline"),
                # stock / projection
                "on_hand": sg.get("on_hand"),
                "current_moh": sg.get("current_moh"),
                "incoming_units_by_month": sg.get("incoming_units_by_month"),
                "projected_moh_m4": sg.get("projected_moh_m4"),
                "projected_moh_m6": sg.get("projected_moh_m6"),
                "projected_moh": sg.get("projected_moh"),
                "projected_moh_with_order": sg.get("projected_moh_with_order"),
                "forecast_history_months": sg.get("forecast_history_months"),
                "target_moh": line.target_moh_used,
                "case_size": line.case_size,
                # quantities
                "suggested_sea_qty": line.suggested_sea_qty,
                "suggested_air_qty": line.suggested_air_qty,
                "baseline_sea_qty": line.baseline_sea_qty,
                "baseline_air_qty": line.baseline_air_qty,
                "final_sea_qty": line.final_sea_qty,
                "final_air_qty": line.final_air_qty,
                # economics
                "unit_cost": sg.get("unit_cost"),
                "retail_price": sg.get("retail_price"),
                "margin": sg.get("margin"),
                "air_shipping_cost": round((sg.get("unit_cost") or 0) * line.final_air_qty, 2),
                "profit_lost_by_air": round((sg.get("margin") or 0) * line.final_air_qty, 2),
                "air_split_reason": sg.get("air_split_reason"),
                "notes": sg.get("notes"),
            })
        rows.sort(key=lambda r: (-(r["final_sea_qty"] + r["final_air_qty"]),
                                 r["category"] or "", r["us_sku"] or ""))
        return {"order": {"id": order.id, "name": order.name, "status": order.status},
                "lines": rows}


@app.patch("/api/orders/{order_id}/lines/{global_sku}")
def patch_line(order_id: int, global_sku: str, body: dict,
               _: dict = Depends(require_session)):
    with get_session() as s:
        line = update_override(s, order_id, global_sku,
                               body.get("final_sea_qty"), body.get("final_air_qty"))
        if not line:
            raise HTTPException(404, "line not found")
        return {"ok": True, "final_sea_qty": line.final_sea_qty,
                "final_air_qty": line.final_air_qty}


@app.patch("/api/products/{global_sku}")
def patch_product(global_sku: str, body: dict, _: dict = Depends(require_session)):
    """Set/clear a compliance flag (never silently drop a flagged SKU)."""
    with get_session() as s:
        p = s.get(Product, global_sku)
        if not p:
            raise HTTPException(404, "product not found")
        if "compliance_flag" in body:
            p.compliance_flag = body["compliance_flag"] or ""
        s.add(p); s.commit()
        return {"ok": True, "compliance_flag": p.compliance_flag}


# ----------------------------------------------------- Phase A: catalog
@app.get("/api/vendors")
def vendors_list(_: dict = Depends(require_session)):
    with get_session() as s:
        return catalog.list_vendors(s)


@app.post("/api/vendors")
def vendors_upsert(body: dict, _: dict = Depends(require_session)):
    with get_session() as s:
        v = catalog.upsert_vendor(s, body)
        return {"id": v.id, "name": v.name}


@app.get("/api/settings")
def settings_get(_: dict = Depends(require_session)):
    with get_session() as s:
        return catalog.get_settings(s)


@app.post("/api/settings")
def settings_set(body: dict, _: dict = Depends(require_session)):
    with get_session() as s:
        return catalog.set_settings(s, body)


@app.post("/api/orders/{order_id}/place")
def place(order_id: int, _: dict = Depends(require_session)):
    from .service import place_order
    with get_session() as s:
        res = place_order(s, order_id)
        if res.get("error"):
            raise HTTPException(404, res["error"])
        return res


@app.get("/api/us-ordering")
def us_items(sess: dict = Depends(require_session)):
    require_list_access(sess, "US_VENDOR")
    from . import us_ordering
    with get_session() as s:
        return us_ordering.all_items(s, CFG)


@app.post("/api/us-ordering/place")
def us_place(body: dict, sess: dict = Depends(require_session)):
    require_list_access(sess, "US_VENDOR")
    from . import us_ordering
    with get_session() as s:
        res = us_ordering.create_and_place_all(s, body.get("lines", {}), body.get("name", ""))
        if res.get("error"):
            raise HTTPException(400, res["error"])
        return res


@app.get("/api/us-ordering/{vendor_id}")
def us_vendor_items(vendor_id: int, sess: dict = Depends(require_session)):
    require_list_access(sess, "US_VENDOR")
    from . import us_ordering
    with get_session() as s:
        res = us_ordering.vendor_items(s, vendor_id, CFG)
        if res.get("error"):
            raise HTTPException(404, res["error"])
        return res


@app.post("/api/us-ordering/{vendor_id}/place")
def us_vendor_place(vendor_id: int, body: dict, sess: dict = Depends(require_session)):
    require_list_access(sess, "US_VENDOR")
    from . import us_ordering
    with get_session() as s:
        res = us_ordering.create_and_place(s, vendor_id, body.get("lines", {}),
                                           body.get("name", ""))
        if res.get("error"):
            raise HTTPException(400, res["error"])
        return res


@app.get("/api/order-list")
def order_list(_: dict = Depends(require_session)):
    with get_session() as s:
        catalog.seed_order_list_from_json(s)
        return {"items": catalog.list_order_list(s),
                "vendors": catalog.list_vendors(s)}


@app.post("/api/order-list")
def order_list_upsert(body: dict, _: dict = Depends(require_admin)):
    with get_session() as s:
        it = catalog.upsert_order_list_item(s, body)
        return {"ok": True, "id": it.id, "global_sku": it.global_sku}


@app.delete("/api/order-list/{global_sku}")
def order_list_delete(global_sku: str, _: dict = Depends(require_admin)):
    with get_session() as s:
        if not catalog.delete_order_list_item(s, global_sku):
            raise HTTPException(404, "not on order list")
        return {"ok": True}


@app.get("/api/users")
def users_list(_: dict = Depends(require_admin)):
    with get_session() as s:
        return {"users": access.list_users(s), "lists": access.ORDERABLE_LISTS}


@app.post("/api/users")
def users_upsert(body: dict, _: dict = Depends(require_admin)):
    with get_session() as s:
        try:
            u = access.upsert_user(s, body.get("email", ""), body.get("name", ""),
                                   body.get("role", "member"),
                                   body.get("active", True))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if "access" in body and isinstance(body["access"], list):
            access.set_access(s, u.email, body["access"])
        return {"ok": True, "email": u.email}


@app.post("/api/users/access")
def users_access(body: dict, _: dict = Depends(require_admin)):
    email = body.get("email", "")
    if not email:
        raise HTTPException(400, "email required")
    with get_session() as s:
        access.set_access(s, email, body.get("list_keys", []))
        return {"ok": True}


@app.delete("/api/users/{email}")
def users_delete(email: str, sess: dict = Depends(require_admin)):
    if email.lower() == (sess.get("login", "").lower()):
        raise HTTPException(400, "You can't delete your own account.")
    with get_session() as s:
        if not access.delete_user(s, email):
            raise HTTPException(404, "user not found")
        return {"ok": True}


@app.get("/api/products")
def products_search(q: str = "", limit: int = 50, exclude_channel: str = "",
                    _: dict = Depends(require_session)):
    """Search the synced product catalogue for the master-list picker.
    `exclude_channel` drops products already on that channel's order list."""
    from .models import OrderListItem
    ql = (q or "").strip().lower()
    with get_session() as s:
        skip = set()
        if exclude_channel:
            skip = set(s.exec(select(OrderListItem.global_sku).where(
                OrderListItem.channel == exclude_channel)).all())
        rows = []
        for p in s.exec(select(Product)).all():
            if p.global_sku in skip:
                continue
            hay = " ".join([p.name or "", p.global_sku, p.us_sku or "",
                            p.category or "", p.barcode or ""]).lower()
            if ql and ql not in hay:
                continue
            rows.append({"global_sku": p.global_sku, "name": p.name,
                         "category": p.category, "us_sku": p.us_sku,
                         "origin": p.origin, "vendor": p.vendor})
            if len(rows) >= max(1, min(limit, 200)):
                break
        rows.sort(key=lambda r: (r["category"] or "", r["name"] or r["global_sku"]))
        return {"items": rows}


@app.put("/api/products/{global_sku}/tags")
def product_tags(global_sku: str, body: dict, _: dict = Depends(require_session)):
    with get_session() as s:
        return {"tags": catalog.set_tags(s, global_sku, body.get("tags", []))}


@app.get("/api/orders/{order_id}/export.csv")
def export_csv(order_id: int, _: dict = Depends(require_session)):
    with get_session() as s:
        order = s.get(Order, order_id)
        rows = export_rows(s, order_id)
    csv_text = rows_to_csv(rows)
    fn = f"{(order.name if order else 'order').replace(' ', '_')}.csv"
    return PlainTextResponse(csv_text, headers={
        "Content-Disposition": f'attachment; filename="{fn}"'})


@app.get("/api/orders/{order_id}/export.xlsx")
def export_xlsx(order_id: int, _: dict = Depends(require_session)):
    with get_session() as s:
        order = s.get(Order, order_id)
        rows = export_rows(s, order_id)
        name = order.name if order else "order"
    data = rows_to_xlsx(rows, name)
    fn = f"{name.replace(' ', '_')}.xlsx"
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


# ------------------------------------------------------------------ odoo
@app.post("/api/odoo/test")
def odoo_test(body: dict):
    """Validate a read-only connection. If a body is supplied those creds are
    used (and NOT stored); otherwise the env-configured client is used."""
    from .datasources.odoo_json import OdooJsonDataSource
    try:
        if body and body.get("base_url"):
            ds = OdooJsonDataSource(body["base_url"], body["db"], body["login"],
                                    body["password"], warehouse=body.get("warehouse"),
                                    sales_model=body.get("sales_model", "sale.report"))
        else:
            ds = OdooJsonDataSource.from_env()
        ds.authenticate()
        return {"ok": True, "uid": ds._uid, "databases": ds.list_databases(),
                "cache": ds.cache_info()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/odoo/status")
def odoo_status():
    """Server-side Odoo config + cache freshness/health (no secrets returned)."""
    from .datasources.odoo_json import OdooJsonDataSource
    from .sync import cache_status
    configured = all(os.environ.get(k) for k in
                     ("ODOO_BASE_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_PASSWORD"))
    info = {"configured": configured,
            "base_url": os.environ.get("ODOO_BASE_URL", ""),
            "db": os.environ.get("ODOO_DB", ""),
            "sales_model": os.environ.get("ODOO_SALES_MODEL", "sale.report"),
            "warehouse": os.environ.get("ODOO_WAREHOUSE", "")}
    with get_session() as s:
        info["sync"] = cache_status(s)          # skubot-style freshness/health
    if configured:
        try:
            info["read_cache"] = OdooJsonDataSource.from_env().cache_info()
        except Exception as e:
            info["error"] = str(e)
    return info


@app.post("/api/odoo/sync")
def odoo_sync(sess: dict = Depends(require_session)):
    """Run one Odoo→local-cache snapshot now using the server's Odoo service
    account (ODOO_* env). Self-healing: a failed/empty pull keeps the last good
    snapshot."""
    from .sync import run_one_sync, cache_status
    from .datasources.odoo_json import OdooJsonDataSource
    try:
        ds = sess.get("client") or OdooJsonDataSource.from_env()
    except Exception as e:
        raise HTTPException(400, f"Odoo not configured on server: {e}")
    with get_session() as s:
        run_one_sync(s, ds, CFG)
        return cache_status(s)


@app.get("/api/india/preview")
def india_preview(sess: dict = Depends(require_session)):
    """Suggested India order lines from the cached snapshot (no DB writes) —
    used to pre-populate the India view list."""
    require_list_access(sess, "INDIA_IMPORT")
    from .sync import latest_cached_pull
    from .service import compute_suggestions
    with get_session() as s:
        cached = latest_cached_pull(s)
        if not cached:
            return {"items": [], "warning": "No Odoo snapshot yet — sync first."}
        pull, _ = cached
    prod = {p["global_sku"]: p for p in pull.products}
    sugg = [x for x in compute_suggestions(pull, CFG)
            if x.suggested_sea_round or x.suggested_air_round]
    sugg.sort(key=lambda s: -(s.suggested_sea_round + s.suggested_air_round))
    items = [{"global_sku": s.global_sku, "us_sku": s.us_sku, "name": s.name,
              "category": s.category, "hsn": (prod.get(s.global_sku, {}) or {}).get("hsn_code", ""),
              "on_hand": round(s.on_hand), "moh": round(s.current_moh, 1),
              "forecast": round(s.forecast_mean, 1), "baseline": round(s.baseline_monthly_sales, 1),
              "confidence": s.forecast_confidence, "target": s.target_moh,
              "proj": [round(x, 1) for x in (s.projected_moh_with_order or s.projected_moh)],
              "moh4": s.projected_moh_m4, "moh6": s.projected_moh_m6,
              "sea": s.suggested_sea_round, "air": s.suggested_air_round} for s in sugg]
    return {"items": items, "count": len(items)}


@app.post("/api/india/draft")
def india_draft(sess: dict = Depends(require_session)):
    """Return a working India draft order (reuse the latest open one, else
    generate a fresh one from the cache). The India view edits this order's
    lines like the original review tool, then places it."""
    require_list_access(sess, "INDIA_IMPORT")
    from .sync import latest_cached_pull
    with get_session() as s:
        cached = latest_cached_pull(s)
        if not cached:
            raise HTTPException(400, "No Odoo snapshot yet — sync first.")
        pull, batch_id = cached
        drafts = [o for o in s.exec(select(Order).where(Order.status == "draft")).all()
                  if (o.config_json or {}).get("channel") != "US_VENDOR"]
        # Reuse a draft ONLY if it was built from the current good snapshot.
        # A draft from an older sync is stale: its suggestion_json froze the
        # demand fields (sell-through / velocity) at creation, so we regenerate
        # rather than hand back an order with empty/outdated numbers.
        fresh = [o for o in drafts if o.snapshot_batch_id == batch_id]
        if fresh:
            o = sorted(fresh, key=lambda x: x.created_at, reverse=True)[0]
            return {"id": o.id, "name": o.name, "reused": True}
        d = datetime.utcnow()
        name = f"Q{(d.month-1)//3+1} {d.year} · {d.date().isoformat()}"
        order = create_order(s, name, pull, CFG, batch_id=batch_id)
        order.config_json = {**(order.config_json or {}), "channel": "INDIA_IMPORT"}
        s.add(order); s.commit()
        return {"id": order.id, "name": order.name, "reused": False}


@app.post("/api/india/place")
def india_place(body: dict, sess: dict = Depends(require_session)):
    """Generate the India order from cache and place it in one action."""
    require_list_access(sess, "INDIA_IMPORT")
    from .sync import latest_cached_pull
    from .service import place_order
    with get_session() as s:
        cached = latest_cached_pull(s)
        if not cached:
            raise HTTPException(400, "No Odoo snapshot yet — sync first.")
        pull, batch_id = cached
        order = create_order(s, body.get("name") or "India order", pull, CFG, batch_id=batch_id)
        res = place_order(s, order.id)
        res["order_id"] = order.id
        res["order_name"] = order.name
        return res


@app.post("/api/orders/from-cache")
def order_from_cache(body: dict, _: dict = Depends(require_session)):
    """Create an order from the latest GOOD cached Odoo snapshot — works even
    if Odoo is offline. Use this as the normal live path; the cache is kept
    fresh by the background sync."""
    from .sync import latest_cached_pull, cache_status
    with get_session() as s:
        cached = latest_cached_pull(s)
        if not cached:
            raise HTTPException(400, "No cached Odoo snapshot yet. Run a sync "
                                "first (POST /api/odoo/sync) or start run_sync.py.")
        pull, batch_id = cached
        order = create_order(s, body.get("name", "Cached order"), pull, CFG,
                             batch_id=batch_id)
        status = cache_status(s)
        return {"id": order.id, "name": order.name, "lines": len(order.lines),
                "source": "odoo_cache", "cache_age_seconds": status["age_seconds"],
                "cache_is_stale": status["is_stale"]}


@app.post("/api/orders/from-odoo")
def order_from_odoo(body: dict):
    """Mode 3: create an order from a live, cached, read-only Odoo pull.
    Credentials come from the server environment (ODOO_* vars)."""
    from .datasources.odoo_json import OdooJsonDataSource
    try:
        ds = OdooJsonDataSource.from_env()
        if body.get("refresh"):
            ds.clear_cache()          # explicit refresh -> bypass cached reads
        pull = ds.pull(sales_months=int(body.get("sales_months", 24)))
    except Exception as e:
        raise HTTPException(400, f"Odoo pull failed: {e}")
    if not pull.products:
        raise HTTPException(400, "Odoo returned no products; "
                            + (pull.warnings[0] if pull.warnings else "check config."))
    with get_session() as s:
        order = create_order(s, body.get("name", "Odoo order"), pull, CFG)
        return {"id": order.id, "name": order.name, "lines": len(order.lines),
                "source": pull.source, "warnings": pull.warnings}


@app.post("/api/odoo/refresh")
def odoo_refresh():
    """Clear the read cache so the next pull fetches fresh data (no polling)."""
    from .datasources.odoo_json import OdooJsonDataSource
    try:
        ds = OdooJsonDataSource.from_env()
        ds.clear_cache()
        return {"ok": True, "cache": ds.cache_info()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _save_temp(upload: Optional[UploadFile]) -> Optional[str]:
    if not upload:
        return None
    suffix = os.path.splitext(upload.filename or "")[1] or ".bin"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(await upload.read())
    return path
