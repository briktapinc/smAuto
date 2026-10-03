"""Mailjet Send API v3.1. Credentials stay on the admin settings store."""

from __future__ import annotations

import base64
import logging
import re
from typing import Any, Callable

_log = logging.getLogger("bubblepod.mailjet")

SEND_URL = "https://api.mailjet.com/v3.1/send"
DEFAULT_FROM_NAME = "Stickman Automation"
DEFAULT_SENDER_DOMAIN = "stickmanautomation.com"
DEFAULT_LOCAL_PART = "noreply"

_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_LOCAL_RE = re.compile(r"^[a-z0-9._+-]+$", re.I)

Transport = Callable[[str, dict[str, Any], dict[str, str], float], tuple[int, dict[str, Any]]]


def sender_domain(value: Any) -> str:
    """Hostname used as the sending domain. Blank or invalid values use the public site domain."""
    raw = str(value or "").strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0].strip().strip(".")
    if raw.startswith("@"):
        raw = raw[1:]
    if raw.startswith("www.") and raw.count(".") >= 2:
        raw = raw[4:]
    if not raw or not _DOMAIN_RE.match(raw):
        return DEFAULT_SENDER_DOMAIN
    return raw


def from_name(value: Any) -> str:
    name = " ".join(str(value or "").split())
    return name or DEFAULT_FROM_NAME


def resolved_from_email(from_value: Any, domain_value: Any) -> str:
    """From.Email is always local-part@sender-domain.

    A bare local part (noreply) becomes noreply@<domain>. A full address on
    another domain keeps its local part and uses the configured sender domain.
    """
    domain = sender_domain(domain_value)
    raw = str(from_value or "").strip()
    local = raw.split("@", 1)[0].strip() if raw else ""
    local = local.lower()
    if not _LOCAL_RE.match(local):
        local = DEFAULT_LOCAL_PART
    return f"{local}@{domain}"


def credentials_present(settings: dict[str, Any] | None) -> bool:
    data = settings or {}
    return bool(str(data.get("mailjet_api_key") or "").strip() and str(data.get("mailjet_secret_key") or "").strip())


def log_if_unconfigured(settings: dict[str, Any] | None = None) -> None:
    data = settings if settings is not None else _admin_settings()
    if credentials_present(data):
        return
    _log.error("Mailjet is not configured")


def basic_auth_header(api_key: str, secret: str) -> str:
    token = base64.b64encode(f"{api_key}:{secret}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def build_message(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None,
    to_name: str = "",
    settings: dict[str, Any],
) -> dict[str, Any]:
    """v3.1 Messages[0] body. From.Email is always on the configured sender domain.

    Send API v3.1 identifies the sender with From. It does not take a separate
    Sender property, and a Sender header is rejected as a protected header, so
    the domain travels in From.Email.
    """
    domain = sender_domain(settings.get("mailjet_sender_domain"))
    raw_from = (settings.get("mailjet_from_email") or settings.get("email_from") or "")
    email = resolved_from_email(raw_from, domain)
    name = from_name(settings.get("mailjet_from_name") or settings.get("email_from_name"))
    recipient: dict[str, str] = {"Email": to}
    display = " ".join(str(to_name or "").split())
    if display:
        recipient["Name"] = display
    body_text = text or ""
    body_html = html if html else _text_to_html(body_text)
    message: dict[str, Any] = {
        "From": {"Email": email, "Name": name},
        "To": [recipient],
        "Subject": subject or "",
        "TextPart": body_text,
        "HTMLPart": body_html,
    }
    reply = str(settings.get("email_reply_to") or "").strip()
    if reply and "@" in reply:
        message["ReplyTo"] = {"Email": reply}
    return message


def send_message(
    *,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    to_name: str = "",
    settings: dict[str, Any] | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Send one message. Raises RuntimeError when Mailjet cannot send. Never logs secrets."""
    data = settings if settings is not None else _admin_settings()
    recipient = (to or "").strip()
    if not recipient or "@" not in recipient or any(ch in recipient for ch in ("\n", "\r", " ")):
        raise RuntimeError("Recipient email is missing or invalid.")
    api_key = str(data.get("mailjet_api_key") or "").strip()
    secret = str(data.get("mailjet_secret_key") or "").strip()
    if not api_key or not secret:
        _log.error("Mailjet is not configured")
        raise RuntimeError("Mailjet is not configured")
    payload = {"Messages": [build_message(
        to=recipient,
        subject=subject,
        text=text,
        html=html,
        to_name=to_name,
        settings=data,
    )]}
    headers = {
        "Authorization": basic_auth_header(api_key, secret),
        "Content-Type": "application/json",
    }
    post = transport or _httpx_transport
    try:
        status, body = post(SEND_URL, payload, headers, 30.0)
    except RuntimeError:
        raise
    except Exception:
        _log.error("Mailjet send failed")
        raise RuntimeError("Mailjet send failed")
    messages = body.get("Messages") if isinstance(body, dict) else None
    first = messages[0] if isinstance(messages, list) and messages and isinstance(messages[0], dict) else {}
    if status == 200 and str(first.get("Status") or "").lower() == "success":
        _log.info("Mailjet send ok")
        return {"ok": True, "to": recipient, "subject": subject}
    _log.error("Mailjet send failed (HTTP %s)", status)
    raise RuntimeError(f"Mailjet send failed (HTTP {status})")


def _text_to_html(text: str) -> str:
    import html as html_lib

    escaped = html_lib.escape(text or "")
    return "<div>" + escaped.replace("\n", "<br>\n") + "</div>"


def _admin_settings() -> dict[str, Any]:
    """Admin store, including env seed for blank keys. Ignores a member overlay."""
    from studio.settings import load_settings, settings_owner

    with settings_owner(""):
        return load_settings()


def _httpx_transport(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    import httpx

    response = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    try:
        body = response.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return response.status_code, body
