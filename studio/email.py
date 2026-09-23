"""Transactional email (SMTP) + built-in templates for Stickman Automation Studio."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

_log = logging.getLogger("bubblepod.email")

# Template keys used by notify_* helpers and admin preview.
TEMPLATE_KEYS = (
    "signup_welcome",
    "password_reset",
    "password_changed",
    "subscription_started",
    "subscription_canceled",
    "payment_receipt",
    "payment_failed",
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
}


def _settings() -> dict[str, Any]:
    from studio.settings import load_settings

    return load_settings()


def email_enabled(settings: dict[str, Any] | None = None) -> bool:
    data = settings or _settings()
    if not _truthy(data.get("email_enabled"), False):
        return False
    host = (data.get("smtp_host") or "").strip()
    return bool(host)


def email_public_status(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    data = settings or _settings()
    return {
        "email_enabled": _truthy(data.get("email_enabled"), False),
        "smtp_host": (data.get("smtp_host") or "").strip(),
        "smtp_port": int(data.get("smtp_port") or 587),
        "smtp_user": (data.get("smtp_user") or "").strip(),
        "smtp_password_set": bool((data.get("smtp_password") or "").strip()),
        "smtp_use_tls": _truthy(data.get("smtp_use_tls"), True),
        "smtp_use_ssl": _truthy(data.get("smtp_use_ssl"), False),
        "email_from": (data.get("email_from") or "").strip(),
        "email_from_name": (data.get("email_from_name") or "Stickman Automation").strip() or "Stickman Automation",
        "email_reply_to": (data.get("email_reply_to") or "").strip(),
        "configured": email_enabled(data),
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


def send_email(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send one message via SMTP. Raises RuntimeError on misconfig / SMTP failure."""
    data = settings or _settings()
    if not email_enabled(data):
        raise RuntimeError("Email is disabled or SMTP host is not configured (Admin → Email).")
    recipient = (to or "").strip()
    if not recipient or "@" not in recipient:
        raise RuntimeError("Recipient email is missing or invalid.")
    host = (data.get("smtp_host") or "").strip()
    port = int(data.get("smtp_port") or 587)
    user = (data.get("smtp_user") or "").strip()
    password = (data.get("smtp_password") or "").strip()
    from_addr = (data.get("email_from") or user or "").strip()
    if not from_addr:
        raise RuntimeError("Set email_from (or smtp_user) in Admin → Email.")
    from_name = (data.get("email_from_name") or "Stickman Automation").strip() or "Stickman Automation"
    reply_to = (data.get("email_reply_to") or "").strip()
    use_ssl = _truthy(data.get("smtp_use_ssl"), False)
    use_tls = _truthy(data.get("smtp_use_tls"), True) and not use_ssl

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_addr}>"
    msg["To"] = recipient
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text or "")
    if html:
        msg.add_alternative(html, subtype="html")

    timeout = 30
    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=timeout, context=context) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.ehlo()
            if use_tls:
                context = ssl.create_default_context()
                smtp.starttls(context=context)
                smtp.ehlo()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    return {"ok": True, "to": recipient, "subject": subject}


def send_template(
    key: str,
    to: str,
    context: dict[str, Any] | None = None,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = settings or _settings()
    tmpl = get_template(key, data)
    ctx = {
        "app_name": (data.get("email_from_name") or "Stickman Automation").strip() or "Stickman Automation",
        "studio_url": _base_url(data) + "/",
        "login_url": _base_url(data) + "/",
        "portal_hint": "Pricing page → Manage / cancel",
        "expires_minutes": "60",
        "period_end_clause": "",
        "amount_clause": "",
        "invoice_line": "",
        "amount": "",
        "currency": "",
        "username": "",
        "reset_url": "",
        **(context or {}),
    }
    subject = _render(tmpl["subject"], ctx)
    text = _render(tmpl["text"], ctx)
    return send_email(to=to, subject=subject, text=text, settings=data)


def _safe_send(key: str, to: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Best-effort send — never raise into Stripe/auth critical paths."""
    try:
        if not (to or "").strip():
            return {"ok": False, "skipped": True, "reason": "no_email"}
        if not email_enabled():
            return {"ok": False, "skipped": True, "reason": "email_disabled"}
        return send_template(key, to, context)
    except Exception as exc:
        _log.warning("email %s to %s failed: %s", key, to, exc)
        return {"ok": False, "error": str(exc)}


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


def send_test_email(to: str) -> dict[str, Any]:
    data = _settings()
    if not email_enabled(data):
        raise RuntimeError("Enable email and set SMTP host first.")
    return send_email(
        to=to,
        subject=f"Test email from {(data.get('email_from_name') or 'Stickman Automation')}",
        text=(
            "This is a Stickman Automation Studio test message.\n"
            "If you received it, SMTP settings are working.\n"
        ),
        settings=data,
    )
