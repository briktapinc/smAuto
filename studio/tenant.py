"""Multi-tenant ownership helpers for projects and topics."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request


def is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user) and (user.get("role") or "") == "admin"


def owner_id_of(user: dict[str, Any] | None) -> str:
    return str((user or {}).get("id") or "").strip()


def user_owns_meta(user: dict[str, Any] | None, meta: dict[str, Any] | None) -> bool:
    if not user or not meta:
        return False
    if is_admin(user):
        return True
    owner = str(meta.get("owner_id") or "").strip()
    uid = owner_id_of(user)
    return bool(owner and uid and owner == uid)


def user_owns_topic(user: dict[str, Any] | None, topic: dict[str, Any] | None) -> bool:
    return user_owns_meta(user, topic)


def filter_owned(items: list[dict[str, Any]], user: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Admins see everything; members see only their owner_id rows (orphans hidden)."""
    if is_admin(user):
        return items
    uid = owner_id_of(user)
    if not uid:
        return []
    return [item for item in items if str(item.get("owner_id") or "").strip() == uid]


def require_project_access(request: Request, project_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load project meta and ensure the session user owns it (or is admin)."""
    from studio.auth import require_session
    from studio.projects import load_meta

    user = require_session(request)
    try:
        meta = load_meta(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not user_owns_meta(user, meta):
        raise HTTPException(status_code=403, detail="Not your job")
    return user, meta


def require_topic_access(request: Request, topic_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from studio.auth import require_session
    from studio.topics import get_topic_raw

    user = require_session(request)
    topic = get_topic_raw(topic_id)
    if not topic:
        raise HTTPException(status_code=404, detail=f"Unknown topic: {topic_id}")
    if not user_owns_topic(user, topic):
        raise HTTPException(status_code=403, detail="Not your topic")
    return user, topic


def migrate_orphans_to_admin() -> dict[str, int]:
    """Assign owner_id on legacy projects/topics that have none → first admin."""
    from studio.members import ensure_members_store, list_users
    from studio.projects import list_projects, save_meta
    from studio.topics import assign_orphan_owners

    ensure_members_store()
    admins = [u for u in list_users(include_disabled=False) if u.get("role") == "admin"]
    if not admins:
        return {"projects": 0, "topics": 0}
    admin_id = str(admins[0]["id"])
    projects_n = 0
    for meta in list_projects():
        if str(meta.get("owner_id") or "").strip():
            continue
        meta["owner_id"] = admin_id
        try:
            save_meta(str(meta["id"]), meta)
            projects_n += 1
        except Exception:
            continue
    topics_n = assign_orphan_owners(admin_id)
    return {"projects": projects_n, "topics": topics_n}
