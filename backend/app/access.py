"""Users, groups & per-list access control (groups-only model).

Roles & flow:
  * The FIRST user to sign in becomes a MAIN ADMIN (self-bootstrapping); emails
    in ADMIN_EMAILS are always main admins (a lock-out safety hatch).
  * The main admin creates GROUPS, assigns each group one or more master lists,
    and names a GROUP ADMIN.
  * A group admin manages that group's MEMBERS and can carve a per-member
    SUBLIST (a subset of a list's SKUs) for each member.
  * A user's orderable lists = the lists of every (active) group they belong to.
    Access is enforced on the backend. A signed-in user in no group is PENDING.

Main admins implicitly have access to every list. Access no longer uses direct
per-person grants (the legacy ListAccess table is retained but unused).
"""
from __future__ import annotations

import os
from typing import List, Optional

from sqlmodel import select

from .models import AppUser, Group, GroupList, GroupMember, Sublist


# The lists a group can be granted. Extend when limited/other lists arrive.
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


# ----------------------------------------------------------------- users
def ensure_user(session, email: str, name: str = "") -> Optional[AppUser]:
    """Called on every login. Creates the user if new; the very first user
    (empty table) becomes a main admin."""
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
    else:
        if name and u.name != name:
            u.name = name
        if email in _admin_emails() and u.role != "admin":
            u.role = "admin"
        session.add(u)
        session.commit()
    return u


def ensure_appuser(session, email: str, name: str = "") -> AppUser:
    """Create a placeholder user (member) if they don't exist yet — used when an
    admin adds someone to a group before that person has ever signed in."""
    email = (email or "").lower().strip()
    u = session.get(AppUser, email)
    if not u and email:
        u = AppUser(email=email, name=name or "", role="member", active=True)
        session.add(u)
        session.commit()
    return u


def is_admin(session, email: str) -> bool:
    """Main (global) admin."""
    email = (email or "").lower()
    if email in _admin_emails():
        return True
    u = session.get(AppUser, email)
    return bool(u and u.active and u.role == "admin")


def user_exists(session, email: str) -> bool:
    return session.get(AppUser, (email or "").lower()) is not None


def is_pending(session, email: str) -> bool:
    email = (email or "").lower()
    if is_admin(session, email):
        return False
    u = session.get(AppUser, email)
    if not u or not u.active:
        return False
    return len(accessible_keys(session, email)) == 0


def list_users(session) -> List[dict]:
    memberships: dict = {}
    gname = {g.id: g.name for g in session.exec(select(Group)).all()}
    for m in session.exec(select(GroupMember)).all():
        memberships.setdefault(m.email, []).append(gname.get(m.group_id, ""))
    admin_of: dict = {}
    for g in session.exec(select(Group)).all():
        if g.admin_email:
            admin_of.setdefault(g.admin_email, []).append(g.name)
    out = []
    for u in session.exec(select(AppUser)).all():
        out.append({"email": u.email, "name": u.name, "role": u.role,
                    "active": u.active,
                    "groups": sorted(set(memberships.get(u.email, []))),
                    "admin_of": sorted(set(admin_of.get(u.email, [])))})
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


def delete_user(session, email: str) -> bool:
    email = (email or "").lower().strip()
    u = session.get(AppUser, email)
    if not u:
        return False
    for m in session.exec(select(GroupMember).where(GroupMember.email == email)).all():
        session.delete(m)
    for sl in session.exec(select(Sublist).where(Sublist.email == email)).all():
        session.delete(sl)
    session.delete(u)
    session.commit()
    return True


# ----------------------------------------------------------------- access
def _active_group_ids(session) -> set:
    return {g.id for g in session.exec(select(Group)).all() if g.active}


def groups_for_user(session, email: str) -> set:
    """Active groups the user belongs to (as member OR group admin)."""
    email = (email or "").lower()
    active = _active_group_ids(session)
    gids = set(session.exec(select(GroupMember.group_id).where(
        GroupMember.email == email)).all())
    for g in session.exec(select(Group).where(Group.admin_email == email)).all():
        gids.add(g.id)
    return gids & active


def accessible_keys(session, email: str) -> set:
    if is_admin(session, email):
        return {l["key"] for l in ORDERABLE_LISTS}
    gids = groups_for_user(session, email)
    if not gids:
        return set()
    return set(session.exec(select(GroupList.list_key).where(
        GroupList.group_id.in_(gids))).all()) & set(_LIST_BY_KEY)


def accessible_lists(session, email: str) -> List[dict]:
    keys = accessible_keys(session, email)
    return [l for l in ORDERABLE_LISTS if l["key"] in keys]


def can_order(session, email: str, list_key: str) -> bool:
    return list_key in accessible_keys(session, email)


def allowed_skus(session, email: str, list_key: str):
    """SKUs a user may order for a list: a set, or None meaning 'the whole
    list'. Main admins and members without a sublist get None."""
    email = (email or "").lower()
    if is_admin(session, email):
        return None
    gids = groups_for_user(session, email)
    if not gids:
        return set()
    rows = session.exec(select(Sublist.global_sku).where(
        Sublist.email == email, Sublist.list_key == list_key,
        Sublist.group_id.in_(gids))).all()
    return set(rows) if rows else None


# ----------------------------------------------------------------- groups
def is_group_admin(session, email: str, group_id: int) -> bool:
    g = session.get(Group, group_id)
    return bool(g and (g.admin_email == (email or "").lower())) or is_admin(session, email)


def can_manage_group(session, email: str, group_id: int) -> bool:
    return is_group_admin(session, email, group_id)


def groups_i_admin(session, email: str) -> List[dict]:
    """Groups this user administers (main admin => all)."""
    email = (email or "").lower()
    allg = list_groups(session)
    if is_admin(session, email):
        return allg
    return [g for g in allg if g["admin_email"] == email]


def list_groups(session) -> List[dict]:
    members: dict = {}
    for m in session.exec(select(GroupMember)).all():
        members.setdefault(m.group_id, []).append(m.email)
    lists: dict = {}
    for gl in session.exec(select(GroupList)).all():
        lists.setdefault(gl.group_id, []).append(gl.list_key)
    out = []
    for g in session.exec(select(Group)).all():
        out.append({"id": g.id, "name": g.name, "admin_email": g.admin_email,
                    "active": g.active, "lists": sorted(lists.get(g.id, [])),
                    "members": sorted(members.get(g.id, []))})
    out.sort(key=lambda r: r["name"].lower())
    return out


def group_detail(session, group_id: int) -> Optional[dict]:
    g = session.get(Group, group_id)
    if not g:
        return None
    keys = sorted(k for k in session.exec(select(GroupList.list_key).where(
        GroupList.group_id == group_id)).all() if k in _LIST_BY_KEY)
    member_emails = sorted(session.exec(select(GroupMember.email).where(
        GroupMember.group_id == group_id)).all())
    subs: dict = {}
    for s in session.exec(select(Sublist).where(Sublist.group_id == group_id)).all():
        subs.setdefault(s.email, {}).setdefault(s.list_key, []).append(s.global_sku)
    members = []
    for em in member_emails:
        u = session.get(AppUser, em)
        members.append({"email": em, "name": (u.name if u else ""),
                        "sublists": subs.get(em, {})})
    return {"id": g.id, "name": g.name, "admin_email": g.admin_email,
            "active": g.active,
            "lists": [_LIST_BY_KEY[k] for k in keys], "members": members}


def create_group(session, name: str, admin_email: str = "",
                 list_keys: Optional[List[str]] = None) -> Group:
    g = Group(name=(name or "").strip(), admin_email=(admin_email or "").lower())
    session.add(g)
    session.commit()
    session.refresh(g)
    if list_keys:
        set_group_lists(session, g.id, list_keys)
    if g.admin_email:
        ensure_appuser(session, g.admin_email)
    return g


def update_group(session, group_id: int, name=None, admin_email=None,
                 active=None, list_keys=None) -> Optional[Group]:
    g = session.get(Group, group_id)
    if not g:
        return None
    if name is not None:
        g.name = name.strip()
    if admin_email is not None:
        g.admin_email = (admin_email or "").lower()
        if g.admin_email:
            ensure_appuser(session, g.admin_email)
    if active is not None:
        g.active = bool(active)
    session.add(g)
    session.commit()
    if list_keys is not None:
        set_group_lists(session, group_id, list_keys)
    return g


def delete_group(session, group_id: int) -> bool:
    g = session.get(Group, group_id)
    if not g:
        return False
    for model, attr in ((GroupList, "group_id"), (GroupMember, "group_id"),
                        (Sublist, "group_id")):
        for row in session.exec(select(model).where(
                getattr(model, attr) == group_id)).all():
            session.delete(row)
    session.delete(g)
    session.commit()
    return True


def set_group_lists(session, group_id: int, list_keys: List[str]) -> None:
    want = {k for k in (list_keys or []) if k in _LIST_BY_KEY}
    have = set(session.exec(select(GroupList.list_key).where(
        GroupList.group_id == group_id)).all())
    for k in want - have:
        session.add(GroupList(group_id=group_id, list_key=k))
    for k in have - want:
        row = session.get(GroupList, (group_id, k))
        if row:
            session.delete(row)
    # dropping a list also drops sublists referencing it
    for s in session.exec(select(Sublist).where(
            Sublist.group_id == group_id)).all():
        if s.list_key not in want:
            session.delete(s)
    session.commit()


def add_member(session, group_id: int, email: str) -> None:
    email = (email or "").lower().strip()
    if not email:
        return
    ensure_appuser(session, email)
    if not session.get(GroupMember, (group_id, email)):
        session.add(GroupMember(group_id=group_id, email=email))
        session.commit()


def remove_member(session, group_id: int, email: str) -> None:
    email = (email or "").lower().strip()
    m = session.get(GroupMember, (group_id, email))
    if m:
        session.delete(m)
    for s in session.exec(select(Sublist).where(
            Sublist.group_id == group_id, Sublist.email == email)).all():
        session.delete(s)
    session.commit()


def set_member_sublist(session, group_id: int, email: str, list_key: str,
                       global_skus: List[str]) -> None:
    """Replace a member's sublist for one list. Empty list => clear (member
    then gets the whole group list for that list)."""
    email = (email or "").lower().strip()
    want = set(global_skus or [])
    for s in session.exec(select(Sublist).where(
            Sublist.group_id == group_id, Sublist.email == email,
            Sublist.list_key == list_key)).all():
        session.delete(s)
    for sku in want:
        session.add(Sublist(group_id=group_id, email=email,
                            list_key=list_key, global_sku=sku))
    session.commit()
