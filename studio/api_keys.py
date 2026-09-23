"""Per-user API keys (bp_live_...) — hashed at rest, scoped to that user's projects."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from studio.paths import API_KEYS_PATH, ensure_dirs

_lock = threading.Lock()
KEY_PREFIX = "bp_live_"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty() -> dict[str, Any]:
    return {"version": 1, "keys": []}


def _load() -> dict[str, Any]:
    ensure_dirs()
    if not API_KEYS_PATH.is_file():
        return _empty()
    try:
        data = json.loads(API_KEYS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    keys = data.get("keys")
    if not isinstance(keys, list):
        keys = []
    cleaned = [k for k in keys if isinstance(k, dict) and k.get("id") and k.get("key_hash")]
    return {"version": 1, "keys": cleaned}


def _save(data: dict[str, Any]) -> None:
    ensure_dirs()
    API_KEYS_PATH.write_text(
        json.dumps({"version": 1, "keys": data.get("keys") or []}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def hash_api_key(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name") or "default",
        "prefix": row.get("prefix") or KEY_PREFIX,
        "user_id": row.get("user_id"),
        "rate_limit": int(row.get("rate_limit") or 60),
        "created_at": row.get("created_at"),
        "revoked_at": row.get("revoked_at"),
        "last_used_at": row.get("last_used_at"),
    }


def list_keys_for_user(user_id: str, *, include_revoked: bool = False) -> list[dict[str, Any]]:
    uid = (user_id or "").strip()
    with _lock:
        rows = _load()["keys"]
    out = []
    for row in rows:
        if str(row.get("user_id") or "") != uid:
            continue
        if row.get("revoked_at") and not include_revoked:
            continue
        out.append(_public_row(row))
    return out


def create_api_key(user_id: str, *, name: str = "default", rate_limit: int = 60) -> dict[str, Any]:
    """Mint a new key. Returns public fields + one-time `key` plaintext."""
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    kid = secrets.token_hex(8)
    row = {
        "id": kid,
        "user_id": uid,
        "name": (name or "default").strip()[:80] or "default",
        "prefix": KEY_PREFIX,
        "key_hash": hash_api_key(raw),
        "rate_limit": max(1, min(10_000, int(rate_limit or 60))),
        "created_at": _now(),
        "revoked_at": None,
        "last_used_at": None,
    }
    with _lock:
        data = _load()
        data["keys"].append(row)
        _save(data)
    public = _public_row(row)
    public["key"] = raw
    public["note"] = "Store this key now — it will not be shown again."
    return public


def revoke_api_key(user_id: str, key_id: str) -> dict[str, Any]:
    uid = (user_id or "").strip()
    kid = (key_id or "").strip()
    with _lock:
        data = _load()
        found = None
        for row in data["keys"]:
            if str(row.get("id") or "") != kid:
                continue
            if str(row.get("user_id") or "") != uid:
                raise PermissionError("Not your API key")
            row["revoked_at"] = _now()
            found = row
            break
        if found is None:
            raise KeyError(f"Unknown API key: {key_id}")
        _save(data)
    return _public_row(found)


def resolve_api_key(raw: str) -> dict[str, Any] | None:
    """Return members-store user dict if the Bearer token is a valid bp_live_ key."""
    token = (raw or "").strip()
    if not token.startswith(KEY_PREFIX):
        return None
    digest = hash_api_key(token)
    with _lock:
        data = _load()
        match = None
        for row in data["keys"]:
            if row.get("revoked_at"):
                continue
            if row.get("key_hash") == digest:
                match = row
                row["last_used_at"] = _now()
                break
        if match is None:
            return None
        _save(data)
    from studio.members import get_user_by_id

    user = get_user_by_id(str(match.get("user_id") or ""))
    if user is None or user.get("disabled"):
        return None
    return user


def looks_like_api_key(token: str) -> bool:
    return (token or "").strip().startswith(KEY_PREFIX)
