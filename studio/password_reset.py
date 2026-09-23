"""Password reset tokens (forgot-password flow)."""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from studio.paths import USER_DATA, ensure_dirs

RESET_PATH = USER_DATA / "password_resets.json"
_lock = threading.Lock()
DEFAULT_TTL_MINUTES = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict[str, Any]:
    ensure_dirs()
    if not RESET_PATH.is_file():
        return {"tokens": {}}
    try:
        data = json.loads(RESET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"tokens": {}}
    tokens = data.get("tokens") if isinstance(data, dict) else {}
    if not isinstance(tokens, dict):
        tokens = {}
    return {"tokens": tokens}


def _save(data: dict[str, Any]) -> None:
    ensure_dirs()
    RESET_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _prune(data: dict[str, Any]) -> None:
    now = _now()
    keep = {}
    for token, row in (data.get("tokens") or {}).items():
        if not isinstance(row, dict):
            continue
        try:
            exp = datetime.fromisoformat(str(row.get("expires_at") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp > now and not row.get("used"):
            keep[token] = row
    data["tokens"] = keep


def create_reset_token(user_id: str, *, ttl_minutes: int = DEFAULT_TTL_MINUTES) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(minutes=max(5, int(ttl_minutes)))
    with _lock:
        data = _load()
        _prune(data)
        # One active token per user
        data["tokens"] = {
            k: v
            for k, v in data["tokens"].items()
            if str(v.get("user_id") or "") != str(user_id)
        }
        data["tokens"][token] = {
            "user_id": user_id,
            "expires_at": expires.isoformat(),
            "created_at": _now().isoformat(),
            "used": False,
        }
        _save(data)
    return token, expires


def consume_reset_token(token: str) -> str | None:
    """Validate and mark used. Returns user_id or None."""
    raw = (token or "").strip()
    if not raw:
        return None
    with _lock:
        data = _load()
        _prune(data)
        row = data["tokens"].get(raw)
        if not isinstance(row, dict) or row.get("used"):
            return None
        try:
            exp = datetime.fromisoformat(str(row.get("expires_at") or "").replace("Z", "+00:00"))
        except Exception:
            return None
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= _now():
            data["tokens"].pop(raw, None)
            _save(data)
            return None
        row["used"] = True
        data["tokens"][raw] = row
        _save(data)
        return str(row.get("user_id") or "") or None
