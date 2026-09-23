"""Append-only usage ledger + quota checks (Phase 4)."""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from studio.paths import USER_DATA, ensure_dirs
from studio.plans import METRICS, plan_for_user

USAGE_LEDGER_PATH = USER_DATA / "usage_ledger.json"
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_period() -> str:
    """UTC calendar month key YYYY-MM."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _empty() -> dict[str, Any]:
    return {"version": 1, "entries": []}


def _load() -> dict[str, Any]:
    ensure_dirs()
    if not USAGE_LEDGER_PATH.is_file():
        return _empty()
    try:
        data = json.loads(USAGE_LEDGER_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    return {"version": 1, "entries": [e for e in entries if isinstance(e, dict)]}


def _save(data: dict[str, Any]) -> None:
    ensure_dirs()
    entries = data.get("entries") or []
    # Cap growth — keep last 50k events
    if len(entries) > 50_000:
        entries = entries[-50_000:]
    USAGE_LEDGER_PATH.write_text(
        json.dumps({"version": 1, "entries": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def record_usage(
    user_id: str,
    metric: str,
    quantity: float | int,
    *,
    cost_cents: int = 0,
    job_id: str | None = None,
    project_id: str | None = None,
    idempotency_key: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a usage row. Duplicate idempotency_key is a no-op (returns existing)."""
    uid = (user_id or "").strip()
    metric_n = (metric or "").strip()
    if not uid or metric_n not in METRICS:
        raise ValueError(f"user_id and metric ({', '.join(METRICS)}) required")
    qty = float(quantity or 0)
    if qty == 0 and not cost_cents:
        return {"ok": True, "skipped": True, "reason": "zero"}
    key = (idempotency_key or "").strip() or None
    with _lock:
        data = _load()
        if key:
            for row in data["entries"]:
                if row.get("idempotency_key") == key:
                    return {"ok": True, "duplicate": True, "entry": row}
        entry = {
            "id": secrets.token_hex(8),
            "user_id": uid,
            "metric": metric_n,
            "quantity": qty,
            "cost_cents": int(cost_cents or 0),
            "job_id": (job_id or "").strip() or None,
            "project_id": (project_id or "").strip() or None,
            "period": current_period(),
            "idempotency_key": key,
            "meta": meta or {},
            "recorded_at": _now(),
        }
        data["entries"].append(entry)
        _save(data)
    return {"ok": True, "entry": entry}


def sum_usage(
    user_id: str,
    metric: str,
    *,
    period: str | None = None,
) -> float:
    uid = (user_id or "").strip()
    metric_n = (metric or "").strip()
    period_n = period or current_period()
    total = 0.0
    with _lock:
        entries = _load()["entries"]
    for row in entries:
        if row.get("user_id") != uid:
            continue
        if row.get("metric") != metric_n:
            continue
        if row.get("period") != period_n:
            continue
        try:
            total += float(row.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
    return total


def sum_cost_cents(user_id: str, *, period: str | None = None, metric: str | None = None) -> int:
    uid = (user_id or "").strip()
    period_n = period or current_period()
    total = 0
    with _lock:
        entries = _load()["entries"]
    for row in entries:
        if row.get("user_id") != uid:
            continue
        if period_n and row.get("period") != period_n:
            continue
        if metric and row.get("metric") != metric:
            continue
        try:
            total += int(row.get("cost_cents") or 0)
        except (TypeError, ValueError):
            continue
    return total


def usage_snapshot(user: dict[str, Any] | None) -> dict[str, Any]:
    """Used vs allowed for the current period."""
    from studio.members import user_has_access

    user = user or {}
    uid = str(user.get("id") or "")
    plan = plan_for_user(user)
    period = current_period()
    used: dict[str, float] = {}
    allowed: dict[str, Any] = {}
    for metric in METRICS:
        used[metric] = sum_usage(uid, metric, period=period) if uid else 0.0
        allowed[metric] = plan.get(metric)
    return {
        "user_id": uid or None,
        "plan_tier": plan.get("tier"),
        "plan_label": plan.get("label"),
        "unlimited": bool(plan.get("unlimited")),
        "period": period,
        "has_access": user_has_access(user),
        "subscription_status": user.get("subscription_status") or "none",
        "used": used,
        "allowed": allowed,
        "retention_days": plan.get("retention_days"),
    }


def quota_block_reason(user: dict[str, Any] | None, *, metric: str = "render_minutes") -> str | None:
    """Return machine-readable code if this user cannot start more work."""
    from studio.members import user_has_access

    user = user or {}
    if (user.get("role") or "") == "admin":
        return None
    status = (user.get("subscription_status") or "none").strip().lower()
    if status in ("past_due", "unpaid", "canceled", "incomplete_expired", "paused"):
        return "subscription_inactive"
    if not user_has_access(user):
        # Membership gate may already block; surface quota_exhausted for consistency when required
        try:
            from studio.auth import membership_required_enabled

            if membership_required_enabled():
                return "subscription_inactive"
        except Exception:
            pass
    plan = plan_for_user(user)
    if plan.get("unlimited"):
        return None
    uid = str(user.get("id") or "")
    if not uid:
        return "unauthorized"
    allowed = plan.get(metric)
    if allowed is None:
        return None
    try:
        limit = float(allowed)
    except (TypeError, ValueError):
        return None
    used = sum_usage(uid, metric)
    if used >= limit:
        return "quota_exhausted"
    # FAL spend cap (separate metric)
    fal_cap = plan.get("fal_spend_cents")
    if fal_cap is not None:
        try:
            if sum_usage(uid, "fal_spend_cents") >= float(fal_cap):
                return "quota_exhausted"
        except (TypeError, ValueError):
            pass
    return None
