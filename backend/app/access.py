"""Users & per-list access control.

Model (set by the product decisions):
  * The FIRST user to sign in becomes an admin (self-bootstrapping). Admins can
    add users and grant access; they implicitly have access to every list.
  * Access is enforced on the backend: ordering from a list a user isn't
    granted is rejected (see main.py `require_list_access`).
  * Emails in the ADMIN_EMAILS env var are always treated as admins (a safety
    hatch so you can never lock yourself out).

A "list" today is a channel (INDIA_IMPORT, US_VENDOR). When limited lists are
added later they become additional list_keys and slot in here unchanged.
"""
from __future__ import annotations

import os
from typing import List

from sqlmodel import select

from .models import AppUser, ListAccess


# The lists a user can be granted access to ORDER FROM. Extend when limited
# lists arrive (append {key, slug, label, order_path}).
ORDERABLE_LISTS = [
    {"key": "INDIA_IMPORT", "slug": "india", "label": "India Reorder List",
     "order_path": "/order/india"},
    {"key": "US_VENDOR", "slug": "usa", "label": "USA Reorder List",
     "order_path": "/order/usa"},
]
_LIST_BY_KEY = {l["key"]: l for l in ORDERABLE_LISTS}


def _admin_emails() -> set:
    return {e.strip().lower() for e in
            os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def ensure_user(session, email: str, name: str = "") -> AppUser:
    """Called on every successful login. Creates the user if new; the very
    first user (empty table) is made an admin with access to all lists."""
    email = (email or "").lower()
    if not email:
        return None
    u = session.get(AppUser, email)
    first_user = session.exec(select(AppUser).limit(1)).first() is None
    if not u:
        role = "admin" if (first_user or email in _admin_emails()) else "member"
        u = AppUser(email=email, name=name or "", role=role, active=True)
        session.add(u)
        session.commit()
        if role == "admin":
            _grant_all(session, email)
    else:
        # keep name fresh; never silently demote, but honour ADMIN_EMAILS
        if name and u.name != name:
            u.name = name
        if email in _admin_emails() and u.role != "admin":
            u.role = "admin"
        session.add(u)
        session.commit()
    return u


def is_admin(session, email: str) -> bool:
    email = (email or "").lower()
    if email in _admin_emails():
        return True
    u = session.get(AppUser, email)
    return bool(u and u.active and u.role == "admin")


def user_exists(session, email: str) -> bool:
    return session.get(AppUser, (email or "").lower()) is not None


def is_pending(session, email: str) -> bool:
    """A logged-in user with no list access yet (and not an admin) — they see
    the waiting screen until a coordinator grants a list."""
    email = (email or "").lower()
    if is_admin(session, email):
        return False
    u = session.get(AppUser, email)
    if not u or not u.active:
        return False
    return len(accessible_keys(session, email)) == 0


def _grant_all(session, email: str) -> None:
    for l in ORDERABLE_LISTS:
        set_access_one(session, email, l["key"], True)


def accessible_keys(session, email: str) -> set:
    """Set of list_keys this user may order from (admins => all)."""
    email = (email or "").lower()
    if is_admin(session, email):
        return {l["key"] for l in ORDERABLE_LISTS}
    return set(session.exec(select(ListAccess.list_key).where(
        ListAccess.email == email)).all())


def accessible_lists(session, email: str) -> List[dict]:
    keys = accessible_keys(session, email)
    return [l for l in ORDERABLE_LISTS if l["key"] in keys]


def can_order(session, email: str, list_key: str) -> bool:
    return list_key in accessible_keys(session, email)


# ----------------------------------------------------------------- admin ops
def list_users(session) -> List[dict]:
    access: dict = {}
    for a in session.exec(select(ListAccess)).all():
        access.setdefault(a.email, []).append(a.list_key)
    out = []
    for u in session.exec(select(AppUser)).all():
        out.append({"email": u.email, "name": u.name, "role": u.role,
                    "active": u.active,
                    "access": sorted(access.get(u.email, []))})
    out.sort(key=lambda r: (r["role"] != "admin", r["email"]))
    return out


def upsert_user(session, email: str, name: str = "", role: str = "member",
                active: bool = True) -> AppUser:
    email = (email or "").lower().strip()
    if not email:
        raise ValueError("email required")
    u = session.get(AppUser, email)
    if not u:
        u = AppUser(email=email)
    u.name = name or u.name or ""
    u.role = "admin" if role == "admin" else "member"
    u.active = bool(active)
    session.add(u)
    session.commit()
    return u


def set_access(session, email: str, list_keys: List[str]) -> None:
    """Replace a user's access grants with exactly `list_keys`."""
    email = (email or "").lower().strip()
    want = {k for k in (list_keys or []) if k in _LIST_BY_KEY}
    have = set(session.exec(select(ListAccess.list_key).where(
        ListAccess.email == email)).all())
    for k in want - have:
        session.add(ListAccess(email=email, list_key=k))
    for k in have - want:
        row = session.get(ListAccess, (email, k))
        if row:
            session.delete(row)
    session.commit()


def set_access_one(session, email: str, list_key: str, granted: bool) -> None:
    email = (email or "").lower().strip()
    row = session.get(ListAccess, (email, list_key))
    if granted and not row:
        session.add(ListAccess(email=email, list_key=list_key))
        session.commit()
    elif row and not granted:
        session.delete(row)
        session.commit()


def delete_user(session, email: str) -> bool:
    email = (email or "").lower().strip()
    u = session.get(AppUser, email)
    if not u:
        return False
    for a in session.exec(select(ListAccess).where(ListAccess.email == email)).all():
        session.delete(a)
    session.delete(u)
    session.commit()
    return True
