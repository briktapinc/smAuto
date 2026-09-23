"""Spending guards for billed OpenAI (cloud) and fal/Flux calls.

LM Studio, ChatGPT/Claude native, ComfyUI, and local TTS do not require spend confirm.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any

from studio.paths import USER_DATA, ensure_dirs

COUNTERS_PATH = USER_DATA / "spend_counters.json"
_lock = threading.Lock()
# spend_confirm_id -> {action, created, expires, used}
_pending: dict[str, dict[str, Any]] = {}
CONFIRM_TTL_SEC = 5 * 60


class SpendBlocked(RuntimeError):
    """Raised when a billed action is refused (missing confirm or over cap)."""

    def __init__(self, message: str, *, payload: dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _nonneg_int(value: Any, default: int = 0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, n)


def load_counters() -> dict[str, Any]:
    ensure_dirs()
    data = {"day": _today(), "openai_calls": 0, "flux_images": 0}
    if COUNTERS_PATH.is_file():
        try:
            raw = json.loads(COUNTERS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update(raw)
        except (json.JSONDecodeError, OSError):
            pass
    if data.get("day") != _today():
        data = {"day": _today(), "openai_calls": 0, "flux_images": 0}
        _write_counters(data)
    return data


def _write_counters(data: dict[str, Any]) -> None:
    ensure_dirs()
    COUNTERS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def public_spend_status() -> dict[str, Any]:
    from studio.costs import FLUX_USD_PER_IMAGE, estimate_flux_usd
    from studio.settings import FLUX_MODEL, load_settings

    s = load_settings()
    c = load_counters()
    flux_n = int(c.get("flux_images") or 0)
    return {
        "require_spend_confirm": _truthy(s.get("require_spend_confirm"), True),
        "max_openai_calls_per_day": _nonneg_int(s.get("max_openai_calls_per_day"), 0),
        "max_flux_images_per_day": _nonneg_int(s.get("max_flux_images_per_day"), 0),
        "day": c.get("day"),
        "openai_calls_today": int(c.get("openai_calls") or 0),
        "flux_images_today": flux_n,
        "flux_usd_today_estimate": estimate_flux_usd(flux_n),
        "flux_usd_per_image": FLUX_USD_PER_IMAGE,
        "flux_model": FLUX_MODEL,
        "confirm_ttl_sec": CONFIRM_TTL_SEC,
    }


def _settings_flags() -> dict[str, Any]:
    from studio.settings import load_settings

    s = load_settings()
    return {
        "require_confirm": _truthy(s.get("require_spend_confirm"), True),
        "max_openai": _nonneg_int(s.get("max_openai_calls_per_day"), 0),
        "max_flux": _nonneg_int(s.get("max_flux_images_per_day"), 0),
        "hands_off": _truthy(s.get("hands_off"), False),
    }


def _image_for(ctx: dict[str, Any]) -> str:
    from studio.settings import load_settings, normalize_image_provider

    project_id = (ctx.get("project_id") or "").strip()
    if project_id:
        try:
            from studio.projects import project_image_provider

            return project_image_provider(project_id)
        except Exception:
            pass
    return normalize_image_provider(ctx.get("image_provider") or load_settings().get("image_provider"))


def _text_for(ctx: dict[str, Any]) -> str:
    from studio.settings import load_settings, normalize_text_provider

    return normalize_text_provider(ctx.get("text_provider") or load_settings().get("text_provider"))


def _tts_for(ctx: dict[str, Any]) -> str:
    from studio.settings import load_settings, normalize_tts_provider

    return normalize_tts_provider(ctx.get("tts_provider") or load_settings().get("tts_provider") or "openai")


def is_billed(action: str, *, context: dict[str, Any] | None = None) -> bool:
    ctx = context or {}
    text = _text_for(ctx)
    tts = _tts_for(ctx)
    image = _image_for(ctx)
    if action in ("openai_script", "openai_topics", "openai_chat"):
        return text == "openai"
    if action == "openai_tts":
        return tts == "openai"
    if action in ("flux_images", "flux_cover", "flux_regen"):
        return image == "flux"
    if action == "pipeline_billed":
        return text == "openai" or image == "flux" or tts == "openai"
    return False


def _purge_pending(now: float | None = None) -> None:
    t = now if now is not None else time.time()
    dead = [k for k, v in _pending.items() if v.get("used") or float(v.get("expires") or 0) < t]
    for k in dead:
        _pending.pop(k, None)


def request_spend_confirm(
    action: str,
    *,
    detail: str = "",
    units: int = 1,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue a one-time spend_confirm_id for a billed MCP/API call."""
    ctx = context or {}
    if not is_billed(action, context=ctx):
        return {
            "ok": True,
            "billed": False,
            "action": action,
            "message": "This action does not bill OpenAI cloud or fal; no confirmation required.",
            "spend_confirm_id": None,
        }
    _purge_pending()
    cid = secrets.token_urlsafe(18)
    now = time.time()
    with _lock:
        _pending[cid] = {
            "action": action,
            "detail": (detail or "")[:400],
            "units": max(1, int(units or 1)),
            "created": now,
            "expires": now + CONFIRM_TTL_SEC,
            "used": False,
            "context": {k: ctx.get(k) for k in ("project_id", "text_provider", "image_provider", "tts_provider") if ctx.get(k)},
        }
    return {
        "ok": True,
        "billed": True,
        "action": action,
        "detail": detail or action,
        "units": max(1, int(units or 1)),
        "spend_confirm_id": cid,
        "expires_in_sec": CONFIRM_TTL_SEC,
        "message": (
            f"Confirmation required to spend on {action}. "
            f"Pass spend_confirm_id={cid!r} or confirm_spend=true on the billed tool within {CONFIRM_TTL_SEC}s."
        ),
        "status": public_spend_status(),
    }


def _consume_confirm_id(spend_confirm_id: str | None, action: str) -> bool:
    if not spend_confirm_id:
        return False
    raw = str(spend_confirm_id).strip()
    if not raw:
        return False
    now = time.time()
    with _lock:
        _purge_pending(now)
        row = _pending.get(raw)
        if not row or row.get("used"):
            return False
        if float(row.get("expires") or 0) < now:
            _pending.pop(raw, None)
            return False
        # Allow exact action match, or pipeline_billed covering sub-actions.
        wanted = row.get("action") or ""
        if wanted not in (action, "pipeline_billed") and action not in (wanted, "pipeline_billed"):
            # Still accept if the pending action is a prefix family match for openai/flux.
            if not (
                (wanted.startswith("openai") and action.startswith("openai"))
                or (wanted.startswith("flux") and action.startswith("flux"))
            ):
                return False
        row["used"] = True
        _pending.pop(raw, None)
        return True


def _check_caps(action: str, units: int, *, user_id: str | None = None) -> None:
    flags = _settings_flags()
    c = load_counters()
    openai_n = int(c.get("openai_calls") or 0)
    flux_n = int(c.get("flux_images") or 0)
    if action.startswith("openai") and flags["max_openai"] > 0:
        if openai_n + units > flags["max_openai"]:
            raise SpendBlocked(
                f"Daily OpenAI call cap reached ({flags['max_openai']}/day). "
                f"Used {openai_n} today (UTC).",
                payload={"code": "daily_cap", "kind": "openai", "error_code": "quota_exhausted", **public_spend_status()},
            )
    if action.startswith("flux") and flags["max_flux"] > 0:
        if flux_n + units > flags["max_flux"]:
            raise SpendBlocked(
                f"Daily Flux image cap reached ({flags['max_flux']}/day). "
                f"Used {flux_n} today (UTC).",
                payload={"code": "daily_cap", "kind": "flux", "error_code": "quota_exhausted", **public_spend_status()},
            )
    # Per-user FAL spend / image quotas (plan tier)
    uid = (user_id or "").strip()
    if uid and action.startswith("flux"):
        try:
            from studio.costs import FLUX_USD_PER_IMAGE
            from studio.members import get_user_by_id
            from studio.plans import plan_for_user
            from studio.usage import sum_usage

            user = get_user_by_id(uid)
            if user and (user.get("role") or "") != "admin":
                plan = plan_for_user(user)
                img_cap = plan.get("images_generated")
                if img_cap is not None and sum_usage(uid, "images_generated") + units > float(img_cap):
                    raise SpendBlocked(
                        f"Plan image quota reached ({img_cap}/period).",
                        payload={"code": "quota_exhausted", "kind": "images_generated", "error_code": "quota_exhausted"},
                    )
                fal_cap = plan.get("fal_spend_cents")
                if fal_cap is not None:
                    add_cents = int(round(units * FLUX_USD_PER_IMAGE * 100))
                    if sum_usage(uid, "fal_spend_cents") + add_cents > float(fal_cap):
                        raise SpendBlocked(
                            f"Plan FAL spend cap reached (${float(fal_cap)/100:.2f}/period).",
                            payload={"code": "quota_exhausted", "kind": "fal_spend_cents", "error_code": "quota_exhausted"},
                        )
        except SpendBlocked:
            raise
        except Exception:
            pass


def record_spend(action: str, units: int = 1, *, user_id: str | None = None, project_id: str | None = None) -> dict[str, Any]:
    """Increment daily counters after a billed call succeeds (or is started)."""
    units = max(1, int(units or 1))
    with _lock:
        c = load_counters()
        if action.startswith("openai"):
            c["openai_calls"] = int(c.get("openai_calls") or 0) + units
        elif action.startswith("flux"):
            c["flux_images"] = int(c.get("flux_images") or 0) + units
        _write_counters(c)
    uid = (user_id or "").strip()
    if uid and action.startswith("flux"):
        try:
            from studio.costs import FLUX_USD_PER_IMAGE
            from studio.usage import record_usage

            cents = int(round(units * FLUX_USD_PER_IMAGE * 100))
            record_usage(
                uid,
                "images_generated",
                units,
                cost_cents=0,
                project_id=project_id,
            )
            record_usage(
                uid,
                "fal_spend_cents",
                cents,
                cost_cents=cents,
                project_id=project_id,
            )
        except Exception:
            pass
    return public_spend_status()


def require_spend(
    action: str,
    *,
    confirm_spend: bool | str | None = False,
    spend_confirm_id: str | None = None,
    units: int = 1,
    source: str = "api",
    detail: str = "",
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Gate a billed action. Returns status dict when allowed; raises SpendBlocked otherwise.

    Free paths (lmstudio / native / comfyui / local TTS) return immediately with billed=False.
    Hands-off scheduler (source='hands_off' or 'scheduler') may spend without per-call confirm
    when hands_off is enabled — enabling hands-off is the confirmation.
    """
    ctx = context or {}
    units = max(1, int(units or 1))
    if not is_billed(action, context=ctx):
        return {"ok": True, "billed": False, "action": action, "status": public_spend_status()}

    flags = _settings_flags()
    user_id = str(ctx.get("user_id") or ctx.get("owner_id") or "").strip() or None
    _check_caps(action, units, user_id=user_id)

    confirmed = _truthy(confirm_spend, False) or _consume_confirm_id(spend_confirm_id, action)
    if source in ("hands_off", "scheduler") and flags["hands_off"]:
        confirmed = True
    if not flags["require_confirm"]:
        confirmed = True

    if not confirmed:
        issued = request_spend_confirm(action, detail=detail or action, units=units, context=ctx)
        raise SpendBlocked(
            issued.get("message")
            or (
                f"Spend confirmation required for {action} (OpenAI cloud / fal). "
                "Pass confirm_spend=true or spend_confirm_id from request_spend_confirm."
            ),
            payload={
                "code": "spend_confirm_required",
                "action": action,
                "billed": True,
                "spend_confirm_id": issued.get("spend_confirm_id"),
                "expires_in_sec": CONFIRM_TTL_SEC,
                "status": public_spend_status(),
                "detail": detail or action,
            },
        )

    # Reserve counters now so concurrent calls cannot overrun the daily cap.
    record_spend(
        action,
        units,
        user_id=user_id,
        project_id=str(ctx.get("project_id") or "") or None,
    )
    return {
        "ok": True,
        "billed": True,
        "action": action,
        "units": units,
        "confirmed": True,
        "status": public_spend_status(),
    }


def spend_error_http(exc: SpendBlocked):
    """Build FastAPI HTTPException-friendly detail dict."""
    payload = dict(exc.payload or {})
    payload["detail"] = str(exc)
    code = payload.get("code") or "spend_blocked"
    status = 402 if code == "daily_cap" else 402
    return status, payload


def reset_for_tests() -> None:
    with _lock:
        _pending.clear()
        if COUNTERS_PATH.is_file():
            try:
                COUNTERS_PATH.unlink()
            except OSError:
                pass
