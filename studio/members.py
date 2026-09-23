"""Multi-user membership store. All paying members share equal Studio access; admins manage billing."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from studio.auth import hash_password, verify_password
from studio.paths import MEMBERS_PATH, ensure_dirs

_lock = threading.Lock()

ROLES = ("admin", "member")
# Equal entitlements for every paying member (no tiers).
ACTIVE_STATUSES = frozenset({"active", "trialing"})
MEMBERSHIP_STATUSES = (
    "none",
    "incomplete",
    "incomplete_expired",
    "trialing",
    "active",
    "past_due",
    "canceled",
    "unpaid",
    "paused",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty() -> dict[str, Any]:
    return {"version": 1, "users": []}


def _load() -> dict[str, Any]:
    ensure_dirs()
    if not MEMBERS_PATH.is_file():
        return _empty()
    try:
        data = json.loads(MEMBERS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    users = data.get("users")
    if not isinstance(users, list):
        users = []
    cleaned = [u for u in users if isinstance(u, dict) and u.get("id") and u.get("username")]
    return {"version": 1, "users": cleaned}


def _save(data: dict[str, Any]) -> None:
    ensure_dirs()
    MEMBERS_PATH.write_text(
        json.dumps({"version": 1, "users": data.get("users") or []}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    from studio.settings import (
        normalize_hands_off,
        normalize_hands_off_interval_hours,
        normalize_hands_off_min_queue,
    )

    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "email": user.get("email") or "",
        "role": user.get("role") or "member",
        "subscription_status": user.get("subscription_status") or "none",
        "plan_tier": user.get("plan_tier") or ("admin" if (user.get("role") or "") == "admin" else "starter"),
        "stripe_customer_id": user.get("stripe_customer_id") or "",
        "stripe_subscription_id": user.get("stripe_subscription_id") or "",
        "stripe_price_id": user.get("stripe_price_id") or "",
        "current_period_end": user.get("current_period_end") or None,
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
        "has_access": user_has_access(user),
        "token_version": int(user.get("token_version") or 0),
        "must_change_password": bool(user.get("must_change_password")),
        "hands_off": normalize_hands_off(user.get("hands_off")),
        "hands_off_interval_hours": normalize_hands_off_interval_hours(
            user.get("hands_off_interval_hours")
        ),
        "hands_off_min_queue": normalize_hands_off_min_queue(user.get("hands_off_min_queue")),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "last_login_at": user.get("last_login_at"),
        "disabled": bool(user.get("disabled")),
    }


def user_has_access(user: dict[str, Any] | None) -> bool:
    """Admins always; every member with the same active/trialing membership."""
    if not user or user.get("disabled"):
        return False
    if (user.get("role") or "") == "admin":
        return True
    return (user.get("subscription_status") or "none") in ACTIVE_STATUSES


def ensure_members_store() -> dict[str, Any]:
    """Create members.json and migrate the legacy single auth.json admin once."""
    with _lock:
        data = _load()
        if data["users"]:
            return data
        try:
            from studio.auth import get_auth_config

            cfg = get_auth_config()
            username = (cfg.get("username") or "admin").strip()
            admin = {
                "id": uuid.uuid4().hex,
                "username": username,
                "email": "",
                "password_hash": cfg.get("password_hash") or hash_password("bubblepod"),
                "role": "admin",
                "subscription_status": "active",
                "stripe_customer_id": "",
                "stripe_subscription_id": "",
                "stripe_price_id": "",
                "current_period_end": None,
                "cancel_at_period_end": False,
                "disabled": False,
                "token_version": 0,
                "must_change_password": True,
                "created_at": cfg.get("created_at") or _now(),
                "updated_at": _now(),
                "last_login_at": None,
                "migrated_from": "auth.json",
            }
            data["users"] = [admin]
            _save(data)
        except Exception:
            admin = {
                "id": uuid.uuid4().hex,
                "username": "admin",
                "email": "",
                "password_hash": hash_password("bubblepod"),
                "role": "admin",
                "subscription_status": "active",
                "stripe_customer_id": "",
                "stripe_subscription_id": "",
                "stripe_price_id": "",
                "current_period_end": None,
                "cancel_at_period_end": False,
                "disabled": False,
                "token_version": 0,
                "must_change_password": True,
                "created_at": _now(),
                "updated_at": _now(),
                "last_login_at": None,
            }
            data["users"] = [admin]
            _save(data)
        return data


def list_users(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    ensure_members_store()
    with _lock:
        users = _load()["users"]
    if not include_disabled:
        users = [u for u in users if not u.get("disabled")]
    return [_public_user(u) for u in users]


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    ensure_members_store()
    uid = (user_id or "").strip()
    with _lock:
        for user in _load()["users"]:
            if user.get("id") == uid:
                return dict(user)
    return None


def get_user_by_username(username: str) -> dict[str, Any] | None:
    ensure_members_store()
    name = (username or "").strip().lower()
    with _lock:
        for user in _load()["users"]:
            if str(user.get("username") or "").strip().lower() == name:
                return dict(user)
    return None


def get_user_by_email(email: str) -> dict[str, Any] | None:
    ensure_members_store()
    mail = (email or "").strip().lower()
    if not mail:
        return None
    with _lock:
        for user in _load()["users"]:
            if str(user.get("email") or "").strip().lower() == mail:
                return dict(user)
    return None


def get_user_by_stripe_customer(customer_id: str) -> dict[str, Any] | None:
    ensure_members_store()
    cid = (customer_id or "").strip()
    if not cid:
        return None
    with _lock:
        for user in _load()["users"]:
            if user.get("stripe_customer_id") == cid:
                return dict(user)
    return None


def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    ident = (username or "").strip()
    user = get_user_by_username(ident)
    if not user and "@" in ident:
        user = get_user_by_email(ident)
    if not user:
        # Legacy single-user auth.json fallback before migration completed.
        try:
            from studio.auth import authenticate as legacy_auth, get_auth_config

            if legacy_auth(ident, password):
                ensure_members_store()
                return get_user_by_username(get_auth_config().get("username") or ident)
        except Exception:
            return None
        return None
    if user.get("disabled"):
        return None
    if not verify_password(password or "", user.get("password_hash") or ""):
        return None
    # Flag well-known default password so UI can force rotation.
    try:
        from studio.auth import DEFAULT_PASSWORD

        if (password or "") == DEFAULT_PASSWORD and not user.get("must_change_password"):
            update_user(user["id"], must_change_password=True)
    except Exception:
        pass
    touch_login(user["id"])
    return get_user_by_id(user["id"])


def touch_login(user_id: str) -> None:
    with _lock:
        data = _load()
        for user in data["users"]:
            if user.get("id") == user_id:
                user["last_login_at"] = _now()
                user["updated_at"] = _now()
                _save(data)
                return


def create_user(
    *,
    username: str,
    password: str,
    email: str = "",
    role: str = "member",
    subscription_status: str = "none",
) -> dict[str, Any]:
    ensure_members_store()
    name = (username or "").strip()
    mail = (email or "").strip().lower()
    pw = (password or "").strip()
    role_n = (role or "member").strip().lower()
    if role_n not in ROLES:
        raise ValueError("role must be admin or member")
    if len(name) < 2:
        raise ValueError("username must be at least 2 characters")
    if len(pw) < 6:
        raise ValueError("password must be at least 6 characters")
    if get_user_by_username(name):
        raise ValueError("username already exists")
    if mail and get_user_by_email(mail):
        raise ValueError("email already exists")
    user = {
        "id": uuid.uuid4().hex,
        "username": name,
        "email": mail,
        "password_hash": hash_password(pw),
        "role": role_n,
        "subscription_status": subscription_status if subscription_status in MEMBERSHIP_STATUSES else "none",
        "plan_tier": "admin" if role_n == "admin" else "starter",
        "stripe_customer_id": "",
        "stripe_subscription_id": "",
        "stripe_price_id": "",
        "current_period_end": None,
        "cancel_at_period_end": False,
        "disabled": False,
        "token_version": 0,
        "must_change_password": False,
        "hands_off": False,
        "hands_off_interval_hours": 0.0,
        "hands_off_min_queue": 5,
        "created_at": _now(),
        "updated_at": _now(),
        "last_login_at": None,
    }
    # Admins do not need a Stripe subscription for equal member access rules.
    if role_n == "admin" and user["subscription_status"] == "none":
        user["subscription_status"] = "active"
    with _lock:
        data = _load()
        data["users"].append(user)
        _save(data)
    return _public_user(user)


def update_user(user_id: str, **fields: Any) -> dict[str, Any]:
    ensure_members_store()
    with _lock:
        data = _load()
        target = None
        for user in data["users"]:
            if user.get("id") == user_id:
                target = user
                break
        if target is None:
            raise FileNotFoundError(f"Unknown user: {user_id}")
        if "email" in fields and fields["email"] is not None:
            mail = str(fields["email"] or "").strip().lower()
            if mail:
                for other in data["users"]:
                    if other.get("id") != user_id and str(other.get("email") or "").lower() == mail:
                        raise ValueError("email already exists")
            target["email"] = mail
        if "username" in fields and fields["username"] is not None:
            name = str(fields["username"] or "").strip()
            if len(name) < 2:
                raise ValueError("username must be at least 2 characters")
            for other in data["users"]:
                if other.get("id") != user_id and str(other.get("username") or "").strip().lower() == name.lower():
                    raise ValueError("username already exists")
            target["username"] = name
            target["token_version"] = int(target.get("token_version") or 0) + 1
        if "role" in fields and fields["role"] is not None:
            role_n = str(fields["role"] or "").strip().lower()
            if role_n not in ROLES:
                raise ValueError("role must be admin or member")
            # Keep at least one admin.
            if target.get("role") == "admin" and role_n != "admin":
                admins = [u for u in data["users"] if u.get("role") == "admin" and not u.get("disabled")]
                if len(admins) <= 1:
                    raise ValueError("cannot demote the last admin")
            target["role"] = role_n
        if "disabled" in fields and fields["disabled"] is not None:
            disabled = bool(fields["disabled"])
            if disabled and target.get("role") == "admin":
                admins = [u for u in data["users"] if u.get("role") == "admin" and not u.get("disabled")]
                if len(admins) <= 1:
                    raise ValueError("cannot disable the last admin")
            target["disabled"] = disabled
        if "password" in fields and fields["password"]:
            pw = str(fields["password"]).strip()
            if len(pw) < 6:
                raise ValueError("password must be at least 6 characters")
            target["password_hash"] = hash_password(pw)
            target["token_version"] = int(target.get("token_version") or 0) + 1
            target["must_change_password"] = False
        for key in (
            "subscription_status",
            "plan_tier",
            "stripe_customer_id",
            "stripe_subscription_id",
            "stripe_price_id",
            "current_period_end",
            "cancel_at_period_end",
            "must_change_password",
            "token_version",
            "hands_off",
            "hands_off_interval_hours",
            "hands_off_min_queue",
        ):
            if key in fields and fields[key] is not None:
                if key == "hands_off":
                    from studio.settings import normalize_hands_off

                    target[key] = normalize_hands_off(fields[key])
                elif key == "hands_off_interval_hours":
                    from studio.settings import normalize_hands_off_interval_hours

                    target[key] = normalize_hands_off_interval_hours(fields[key])
                elif key == "hands_off_min_queue":
                    from studio.settings import normalize_hands_off_min_queue

                    target[key] = normalize_hands_off_min_queue(fields[key])
                elif key == "plan_tier":
                    from studio.plans import normalize_plan_tier

                    target[key] = normalize_plan_tier(
                        str(fields[key]),
                        is_admin=(target.get("role") or "") == "admin",
                    )
                else:
                    target[key] = fields[key]
        target["updated_at"] = _now()
        _save(data)
        return _public_user(target)


def delete_user(user_id: str) -> dict[str, Any]:
    ensure_members_store()
    with _lock:
        data = _load()
        target = None
        keep = []
        for user in data["users"]:
            if user.get("id") == user_id:
                target = user
            else:
                keep.append(user)
        if target is None:
            raise FileNotFoundError(f"Unknown user: {user_id}")
        if target.get("role") == "admin":
            admins = [u for u in keep if u.get("role") == "admin" and not u.get("disabled")]
            if not admins:
                raise ValueError("cannot delete the last admin")
        data["users"] = keep
        _save(data)
        return {"ok": True, "id": user_id, "deleted": True}


def set_password_for_username(username: str, current_password: str, new_password: str) -> dict[str, Any]:
    user = get_user_by_username(username)
    if not user:
        raise FileNotFoundError("Unknown user")
    if not verify_password(current_password or "", user.get("password_hash") or ""):
        raise PermissionError("Current password is incorrect")
    return update_user(user["id"], password=new_password)


def public_session(user: dict[str, Any]) -> dict[str, Any]:
    pub = _public_user(user)
    return {
        "username": pub["username"],
        "user_id": pub["id"],
        "role": pub["role"],
        "email": pub["email"],
        "has_access": pub["has_access"],
        "subscription_status": pub["subscription_status"],
        "is_admin": pub["role"] == "admin",
        "must_change_password": bool(pub.get("must_change_password")),
        "hands_off": pub["hands_off"],
        "hands_off_interval_hours": pub["hands_off_interval_hours"],
        "hands_off_min_queue": pub["hands_off_min_queue"],
        "membership_equal": True,
        "membership_note": "All members share the same Studio privileges within their own jobs.",
    }


def hands_off_prefs(owner_id: str | None) -> dict[str, Any]:
    """Per-member hands-off prefs (falls back to defaults; never global settings)."""
    from studio.settings import (
        normalize_hands_off,
        normalize_hands_off_interval_hours,
        normalize_hands_off_min_queue,
    )

    defaults = {
        "hands_off": False,
        "hands_off_interval_hours": 0.0,
        "hands_off_min_queue": 5,
    }
    uid = (owner_id or "").strip()
    if not uid:
        return defaults
    user = get_user_by_id(uid)
    if not user:
        return defaults
    return {
        "hands_off": normalize_hands_off(user.get("hands_off")),
        "hands_off_interval_hours": normalize_hands_off_interval_hours(
            user.get("hands_off_interval_hours")
        ),
        "hands_off_min_queue": normalize_hands_off_min_queue(user.get("hands_off_min_queue")),
    }


def user_hands_off_enabled(owner_id: str | None) -> bool:
    return bool(hands_off_prefs(owner_id).get("hands_off"))


def list_hands_off_owner_ids() -> list[str]:
    """Owner ids with hands-off enabled (and not disabled)."""
    ensure_members_store()
    out: list[str] = []
    with _lock:
        for user in _load()["users"]:
            if user.get("disabled"):
                continue
            if not user.get("hands_off"):
                continue
            uid = str(user.get("id") or "").strip()
            if uid:
                out.append(uid)
    return out


def set_hands_off_prefs(
    owner_id: str,
    *,
    enabled: bool | None = None,
    interval_hours: float | None = None,
    min_queue: int | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if enabled is not None:
        fields["hands_off"] = enabled
    if interval_hours is not None:
        fields["hands_off_interval_hours"] = interval_hours
    if min_queue is not None:
        fields["hands_off_min_queue"] = min_queue
    if not fields:
        user = get_user_by_id(owner_id)
        if not user:
            raise FileNotFoundError("Unknown user")
        return _public_user(user)
    return update_user(owner_id, **fields)
