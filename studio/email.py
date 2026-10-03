"""Transactional email via Mailjet Send API v3.1."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

_log = logging.getLogger("bubblepod.email")

# Template keys used by notify_* helpers and admin preview.
TEMPLATE_KEYS = (
    "signup_welcome",
    "password_reset",
    "password_changed",
    "subscription_started",
    "subscription_canceled",
    "subscription_expiring",
    "payment_receipt",
    "payment_failed",
    "job_finished",
)

DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    "signup_welcome": {
        "subject": "Welcome to {{app_name}}, {{username}}",
        "text": (
            "Hi {{username}},\n\n"
            "Your {{app_name}} account is ready.\n"
            "Sign in: {{login_url}}\n\n"
            "If you did not create this account, you can ignore this message.\n"
        ),
    },
    "password_reset": {
        "subject": "Reset your {{app_name}} password",
        "text": (
            "Hi {{username}},\n\n"
            "We received a request to reset your password.\n"
            "Open this link within {{expires_minutes}} minutes:\n"
            "{{reset_url}}\n\n"
            "If you did not request this, you can ignore this email.\n"
        ),
    },
    "password_changed": {
        "subject": "Your {{app_name}} password was changed",
        "text": (
            "Hi {{username}},\n\n"
            "Your password was changed successfully.\n"
            "If this was not you, reset it immediately: {{login_url}}\n"
        ),
    },
    "subscription_started": {
        "subject": "Membership active — {{app_name}}",
        "text": (
            "Hi {{username}},\n\n"
            "Your {{app_name}} membership is active. Full Studio access is unlocked.\n"
            "Open Studio: {{studio_url}}\n"
            "Manage billing: {{portal_hint}}\n"
        ),
    },
    "subscription_canceled": {
        "subject": "Membership canceled — {{app_name}}",
        "text": (
            "Hi {{username}},\n\n"
            "Your {{app_name}} membership was canceled"
            "{{period_end_clause}}.\n"
            "You can still browse and delete jobs; subscribe again anytime: {{studio_url}}\n"
        ),
    },
    "subscription_expiring": {
        "subject": "Your {{app_name}} membership ends {{period_end}}",
        "text": (
            "Hi {{username}},\n\n"
            "Your {{app_name}} membership ends on {{period_end}} and will not renew.\n"
            "Renew to keep Studio access: {{renew_url}}\n"
        ),
    },
    "payment_receipt": {
        "subject": "Payment received — {{app_name}}",
        "text": (
            "Hi {{username}},\n\n"
            "We received your payment of {{amount}} {{currency}}.\n"
            "{{invoice_line}}"
            "Thank you for supporting {{app_name}}.\n"
        ),
    },
    "payment_failed": {
        "subject": "Payment failed — {{app_name}}",
        "text": (
            "Hi {{username}},\n\n"
            "We could not process your latest membership payment"
            "{{amount_clause}}.\n"
            "Update your payment method in the billing portal from Studio, "
            "or reply if you need help.\n"
            "Studio: {{studio_url}}\n"
        ),
    },
    "job_finished": {
        "subject": "Video job update — {{title}}",
        "text": (
            "Hi {{username}},\n\n"
            "Your {{title}} job is {{status_label}}.\n"
            "{{detail_line}}"
            "Open Studio: {{studio_url}}\n"
        ),
    },
}


def _settings() -> dict[str, Any]:
    """Admin settings only, so a member overlay cannot blank Mailjet credentials."""
    from studio.settings import load_settings, settings_owner

    with settings_owner(""):
        return load_settings()


def email_enabled(settings: dict[str, Any] | None = None) -> bool:
    from studio.mailjet import credentials_present

    data = settings or _settings()
    if not _truthy(data.get("email_enabled"), False):
        return False
    return credentials_present(data)


def email_public_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    from studio.mailjet import credentials_present, from_name, resolved_from_email, sender_domain

    data = settings or _settings()
    domain = sender_domain(data.get("mailjet_sender_domain"))
    return {
        "email_enabled": _truthy(data.get("email_enabled"), False),
        "mailjet_sender_domain": domain,
        "mailjet_from_email": resolved_from_email(
            data.get("mailjet_from_email") or data.get("email_from"),
            domain,
        ),
        "mailjet_from_name": from_name(data.get("mailjet_from_name") or data.get("email_from_name")),
        "mailjet_api_key_set": bool(str(data.get("mailjet_api_key") or "").strip()),
        "mailjet_secret_key_set": bool(str(data.get("mailjet_secret_key") or "").strip()),
        "email_reply_to": (data.get("email_reply_to") or "").strip(),
        "configured": bool(_truthy(data.get("email_enabled"), False) and credentials_present(data)),
    }


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _base_url(settings: dict[str, Any] | None = None) -> str:
    from studio.settings import resolve_public_base_url

    return resolve_public_base_url(settings)


def _render(template: str, context: dict[str, Any]) -> str:
    out = template or ""
    for key, value in context.items():
        out = out.replace("{{" + key + "}}", str(value if value is not None else ""))
    return out


def get_template(key: str, settings: dict[str, Any] | None = None) -> dict[str, str]:
    data = settings or _settings()
    overrides = data.get("email_templates") if isinstance(data.get("email_templates"), dict) else {}
    base = dict(DEFAULT_TEMPLATES.get(key) or {"subject": key, "text": ""})
    custom = overrides.get(key) if isinstance(overrides.get(key), dict) else {}
    if custom.get("subject"):
        base["subject"] = str(custom["subject"])
    if custom.get("text"):
        base["text"] = str(custom["text"])
    return base


def list_templates(settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = settings or _settings()
    out = []
    for key in TEMPLATE_KEYS:
        tmpl = get_template(key, data)
        out.append({"key": key, "subject": tmpl["subject"], "text": tmpl["text"]})
    return out


def _html_from_text(text: str) -> str:
    import html as html_lib

    return "<div>" + html_lib.escape(text or "").replace("\n", "<br>\n") + "</div>"


def send_email(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    to_name: str = "",
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one message through Mailjet. Raises RuntimeError when it cannot send."""
    from studio.mailjet import credentials_present, log_if_unconfigured, send_message

    data = settings or _settings()
    if not credentials_present(data):
        log_if_unconfigured(data)
        raise RuntimeError("Mailjet is not configured")
    if not _truthy(data.get("email_enabled"), False):
        raise RuntimeError("Outbound email is turned off in Admin → Email.")
    return send_message(
        to=to,
        subject=subject,
        text=text,
        html=html if html is not None else _html_from_text(text),
        to_name=to_name,
        settings=data,
    )


def send_template(
    key: str,
    to: str,
    context: dict[str, Any] | None = None,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from studio.mailjet import from_name

    data = settings or _settings()
    tmpl = get_template(key, data)
    ctx = {
        "app_name": from_name(data.get("mailjet_from_name") or data.get("email_from_name")),
        "studio_url": _base_url(data) + "/",
        "login_url": _base_url(data) + "/",
        "portal_hint": "Pricing page → Manage / cancel",
        "expires_minutes": "60",
        "period_end_clause": "",
        "period_end": "",
        "renew_url": "",
        "amount_clause": "",
        "invoice_line": "",
        "amount": "",
        "currency": "",
        "username": "",
        "reset_url": "",
        "title": "",
        "status_label": "",
        "detail_line": "",
        **(context or {}),
    }
    subject = _render(tmpl["subject"], ctx)
    text = _render(tmpl["text"], ctx)
    return send_email(
        to=to,
        subject=subject,
        text=text,
        html=_html_from_text(text),
        to_name=str((context or {}).get("username") or ""),
        settings=data,
    )


def _safe_send(key: str, to: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Best-effort send — never raise into Stripe, auth, or job paths."""
    try:
        if not (to or "").strip():
            return {"ok": False, "skipped": True, "reason": "no_email"}
        data = _settings()
        from studio.mailjet import credentials_present, log_if_unconfigured

        if not credentials_present(data):
            log_if_unconfigured(data)
            return {"ok": False, "skipped": True, "reason": "mailjet_not_configured"}
        if not _truthy(data.get("email_enabled"), False):
            return {"ok": False, "skipped": True, "reason": "email_disabled"}
        return send_template(key, to, context, settings=data)
    except Exception:
        _log.error("email %s failed", key)
        return {"ok": False, "error": "send_failed"}


def notify_signup(user: dict[str, Any]) -> dict[str, Any]:
    return _safe_send(
        "signup_welcome",
        user.get("email") or "",
        {"username": user.get("username") or ""},
    )


def notify_password_reset(user: dict[str, Any], reset_url: str, *, expires_minutes: int = 60) -> dict[str, Any]:
    return _safe_send(
        "password_reset",
        user.get("email") or "",
        {
            "username": user.get("username") or "",
            "reset_url": reset_url,
            "expires_minutes": str(expires_minutes),
        },
    )


def notify_password_changed(user: dict[str, Any]) -> dict[str, Any]:
    return _safe_send(
        "password_changed",
        user.get("email") or "",
        {"username": user.get("username") or ""},
    )


def notify_subscription_started(user: dict[str, Any]) -> dict[str, Any]:
    return _safe_send(
        "subscription_started",
        user.get("email") or "",
        {"username": user.get("username") or ""},
    )


def notify_subscription_canceled(user: dict[str, Any], *, period_end: str | None = None) -> dict[str, Any]:
    clause = f" (access until {period_end})" if period_end else ""
    return _safe_send(
        "subscription_canceled",
        user.get("email") or "",
        {"username": user.get("username") or "", "period_end_clause": clause},
    )


# One reminder when a membership is set to end and the period closes within this window.
RENEWAL_REMINDER_DAYS = 3


def parse_period_end(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def renewal_reminder_due(user: dict[str, Any] | None, *, now: datetime | None = None) -> str | None:
    """Period-end key when this member should get one renew reminder, else None.

    Only memberships that will not renew (cancel at period end) and still have
    access. Auto-renewing subscriptions are billed by Stripe and are not emailed.
    """
    if not user or user.get("disabled"):
        return None
    if (user.get("role") or "") == "admin":
        return None
    if not str(user.get("email") or "").strip():
        return None
    if not user.get("cancel_at_period_end"):
        return None
    status = str(user.get("subscription_status") or "").strip().lower()
    if status not in ("active", "trialing"):
        return None
    end = parse_period_end(user.get("current_period_end"))
    if end is None:
        return None
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    delta = end - moment.astimezone(timezone.utc)
    if delta > timedelta(days=RENEWAL_REMINDER_DAYS) or delta < timedelta(days=-1):
        return None
    key = end.isoformat()
    if str(user.get("renewal_reminder_for") or "") == key:
        return None
    return key


def _renew_url() -> str:
    return _base_url() + "/?step=subscription"


def notify_subscription_expiring(user: dict[str, Any], *, period_end: str, renew_url: str = "") -> dict[str, Any]:
    return _safe_send(
        "subscription_expiring",
        user.get("email") or "",
        {
            "username": user.get("username") or "",
            "period_end": period_end,
            "renew_url": renew_url or _renew_url(),
        },
    )


def send_due_renewal_reminders(*, now: datetime | None = None) -> dict[str, Any]:
    """Email members whose membership ends within the reminder window. One email per period."""
    from studio.members import list_user_records, update_user

    if not email_enabled():
        return {"ok": True, "skipped": True, "reason": "email_disabled", "sent": 0}
    sent = 0
    due = 0
    for user in list_user_records():
        key = renewal_reminder_due(user, now=now)
        if not key:
            continue
        due += 1
        end = parse_period_end(user.get("current_period_end"))
        label = end.strftime("%B %d, %Y").replace(" 0", " ") if end else key[:10]
        result = notify_subscription_expiring(user, period_end=label)
        if not result.get("ok"):
            continue
        try:
            update_user(str(user.get("id") or ""), renewal_reminder_for=key)
        except Exception:
            _log.error("could not record renewal reminder")
            continue
        sent += 1
    return {"ok": True, "sent": sent, "due": due}


def notify_payment_receipt(
    user: dict[str, Any],
    *,
    amount_cents: int | None = None,
    currency: str = "usd",
    invoice_url: str = "",
) -> dict[str, Any]:
    amount = ""
    if amount_cents is not None:
        try:
            amount = f"{int(amount_cents) / 100:.2f}"
        except (TypeError, ValueError):
            amount = str(amount_cents)
    invoice_line = f"Invoice: {invoice_url}\n" if invoice_url else ""
    return _safe_send(
        "payment_receipt",
        user.get("email") or "",
        {
            "username": user.get("username") or "",
            "amount": amount,
            "currency": (currency or "usd").upper(),
            "invoice_line": invoice_line,
        },
    )


def notify_payment_failed(
    user: dict[str, Any],
    *,
    amount_cents: int | None = None,
    currency: str = "usd",
) -> dict[str, Any]:
    clause = ""
    if amount_cents is not None:
        try:
            clause = f" ({int(amount_cents) / 100:.2f} {(currency or 'usd').upper()})"
        except (TypeError, ValueError):
            clause = ""
    return _safe_send(
        "payment_failed",
        user.get("email") or "",
        {"username": user.get("username") or "", "amount_clause": clause},
    )


def notify_job_finished(event: dict[str, Any]) -> dict[str, Any]:
    """Email the project owner when a pipeline or final render finishes. Never raises."""
    try:
        from studio.members import get_user_by_id
        from studio.projects import owner_id_for_project

        project_id = str(event.get("project_id") or "").strip()
        owner_id = owner_id_for_project(project_id) if project_id else None
        user = get_user_by_id(owner_id) if owner_id else None
        if not user:
            return {"ok": False, "skipped": True, "reason": "no_owner"}
        status = str(event.get("status") or "").strip().lower()
        label = "complete" if status == "completed" else "ready for another look"
        detail = str(event.get("detail") or "").strip()
        detail_line = f"{detail}\n" if detail else ""
        title = str(event.get("title") or project_id or "your video").strip() or "your video"
        return _safe_send(
            "job_finished",
            user.get("email") or "",
            {
                "username": user.get("username") or "",
                "title": title,
                "status_label": label,
                "detail_line": detail_line,
            },
        )
    except Exception:
        _log.error("job notification email failed")
        return {"ok": False, "error": "send_failed"}


def send_test_email(to: str) -> dict[str, Any]:
    from studio.mailjet import credentials_present, from_name, log_if_unconfigured

    data = _settings()
    if not credentials_present(data):
        log_if_unconfigured(data)
        raise RuntimeError("Mailjet is not configured")
    if not _truthy(data.get("email_enabled"), False):
        raise RuntimeError("Turn on outbound email in Admin → Email.")
    name = from_name(data.get("mailjet_from_name") or data.get("email_from_name"))
    return send_email(
        to=to,
        subject=f"Test email from {name}",
        text=(
            "This is a Stickman Automation Studio test message.\n"
            "Mailjet accepted the send.\n"
        ),
        settings=data,
    )
