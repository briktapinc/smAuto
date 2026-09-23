"""Stripe Checkout + Customer Portal + webhooks for a single equal-access membership."""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from studio.paths import BILLING_LEDGER_PATH, ensure_dirs
from studio.settings import load_settings, save_settings

_log = logging.getLogger("bubblepod.stripe")
_lock = threading.Lock()

MEMBERSHIP_PRODUCT_NAME = "Bubble Pod Membership"
MEMBERSHIP_PRODUCT_DESC = (
    "Full Bubble Pod Studio access. Every member has the same privileges — "
    "scripts, pictures, voice, render, Topics, YouTube, and MCP."
)
DEFAULT_PRICE_CENTS = 2900
DEFAULT_CURRENCY = "usd"
DEFAULT_INTERVAL = "month"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_ledger() -> dict[str, Any]:
    return {
        "version": 1,
        "signups": [],
        "payments": [],
        "refunds": [],
        "subscription_events": [],
    }


def load_ledger() -> dict[str, Any]:
    ensure_dirs()
    if not BILLING_LEDGER_PATH.is_file():
        return _empty_ledger()
    try:
        data = json.loads(BILLING_LEDGER_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_ledger()
    if not isinstance(data, dict):
        return _empty_ledger()
    out = _empty_ledger()
    for key in out:
        if key == "version":
            continue
        rows = data.get(key)
        out[key] = rows if isinstance(rows, list) else []
    out["version"] = 1
    return out


def _save_ledger(data: dict[str, Any]) -> None:
    ensure_dirs()
    BILLING_LEDGER_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def append_ledger(kind: str, row: dict[str, Any]) -> None:
    if kind not in ("signups", "payments", "refunds", "subscription_events"):
        raise ValueError(f"Unknown ledger kind: {kind}")
    with _lock:
        data = load_ledger()
        bucket = data.setdefault(kind, [])
        row = dict(row)
        row.setdefault("id", secrets.token_hex(8))
        row.setdefault("recorded_at", _now())
        # De-dupe by stripe object id when present.
        stripe_id = row.get("stripe_id") or row.get("payment_intent") or row.get("charge_id")
        if stripe_id:
            bucket[:] = [
                r for r in bucket
                if (r.get("stripe_id") or r.get("payment_intent") or r.get("charge_id")) != stripe_id
            ]
        bucket.insert(0, row)
        data[kind] = bucket[:2000]
        _save_ledger(data)


def stripe_configured() -> bool:
    settings = load_settings()
    return bool((settings.get("stripe_secret_key") or "").strip())


def get_stripe_client():
    """Return a StripeClient instance (never set global stripe.api_key)."""
    try:
        import stripe
    except ImportError as exc:
        raise RuntimeError(
            "stripe package not installed. Run: pip install stripe"
        ) from exc
    key = (load_settings().get("stripe_secret_key") or "").strip()
    if not key:
        raise RuntimeError(
            "Stripe secret key missing. Add stripe_secret_key in Settings → Membership / Stripe."
        )
    # Prefer StripeClient when available (current SDKs).
    client_cls = getattr(stripe, "StripeClient", None)
    if client_cls is not None:
        return client_cls(key)
    stripe.api_key = key  # fallback for older SDKs
    return stripe


def _studio_base_url() -> str:
    settings = load_settings()
    host = (settings.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    if host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    port = int(settings.get("port") or 7878)
    public = (settings.get("public_base_url") or settings.get("ngrok_url") or "").strip().rstrip("/")
    if public.startswith("http"):
        return public
    return f"http://{host}:{port}"


def public_billing_config() -> dict[str, Any]:
    settings = load_settings()
    return {
        "configured": stripe_configured(),
        "publishable_key": (settings.get("stripe_publishable_key") or "").strip(),
        "price_id": (settings.get("stripe_price_id") or "").strip(),
        "product_id": (settings.get("stripe_product_id") or "").strip(),
        "membership_name": MEMBERSHIP_PRODUCT_NAME,
        "membership_equal": True,
        "amount_cents": int(settings.get("stripe_price_amount_cents") or DEFAULT_PRICE_CENTS),
        "currency": (settings.get("stripe_price_currency") or DEFAULT_CURRENCY).lower(),
        "interval": (settings.get("stripe_price_interval") or DEFAULT_INTERVAL),
        "note": "All members have equal Studio access privileges.",
    }


def ensure_membership_catalog() -> dict[str, Any]:
    """Create (or reuse) one Product + recurring Price for equal membership."""
    settings = load_settings()
    price_id = (settings.get("stripe_price_id") or "").strip()
    product_id = (settings.get("stripe_product_id") or "").strip()
    client = get_stripe_client()

    if price_id:
        try:
            price = _retrieve_price(client, price_id)
            product_id = product_id or (price.get("product") if isinstance(price, dict) else getattr(price, "product", "")) or product_id
            save_settings({
                "stripe_price_id": price_id,
                "stripe_product_id": str(product_id or ""),
                "stripe_price_amount_cents": int(
                    (price.get("unit_amount") if isinstance(price, dict) else getattr(price, "unit_amount", None))
                    or settings.get("stripe_price_amount_cents")
                    or DEFAULT_PRICE_CENTS
                ),
                "stripe_price_currency": str(
                    (price.get("currency") if isinstance(price, dict) else getattr(price, "currency", None))
                    or DEFAULT_CURRENCY
                ).lower(),
            })
            return public_billing_config()
        except Exception as exc:
            _log.warning("Stored stripe_price_id invalid (%s); recreating catalog", exc)

    amount = int(settings.get("stripe_price_amount_cents") or DEFAULT_PRICE_CENTS)
    currency = (settings.get("stripe_price_currency") or DEFAULT_CURRENCY).lower()
    interval = (settings.get("stripe_price_interval") or DEFAULT_INTERVAL).lower()

    if not product_id:
        product = _create_product(client)
        product_id = product["id"] if isinstance(product, dict) else product.id
    price = _create_price(client, product_id, amount=amount, currency=currency, interval=interval)
    price_id = price["id"] if isinstance(price, dict) else price.id
    save_settings({
        "stripe_product_id": product_id,
        "stripe_price_id": price_id,
        "stripe_price_amount_cents": amount,
        "stripe_price_currency": currency,
        "stripe_price_interval": interval,
    })
    return public_billing_config()


def _create_product(client) -> Any:
    params = {
        "name": MEMBERSHIP_PRODUCT_NAME,
        "description": MEMBERSHIP_PRODUCT_DESC,
        "metadata": {"bubblepod": "membership", "equal_access": "true"},
    }
    if hasattr(client, "v1"):
        return client.v1.products.create(params)
    import stripe

    return stripe.Product.create(**params)


def _create_price(client, product_id: str, *, amount: int, currency: str, interval: str) -> Any:
    params = {
        "product": product_id,
        "unit_amount": amount,
        "currency": currency,
        "recurring": {"interval": interval},
        "metadata": {"bubblepod": "membership"},
    }
    if hasattr(client, "v1"):
        return client.v1.prices.create(params)
    import stripe

    return stripe.Price.create(**params)


def _retrieve_price(client, price_id: str) -> Any:
    if hasattr(client, "v1"):
        return client.v1.prices.retrieve(price_id)
    import stripe

    return stripe.Price.retrieve(price_id)


def _obj_get(obj: Any, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def ensure_customer_for_user(user: dict[str, Any]) -> str:
    from studio.members import update_user

    existing = (user.get("stripe_customer_id") or "").strip()
    if existing:
        return existing
    client = get_stripe_client()
    params = {
        "email": (user.get("email") or None) or None,
        "name": user.get("username") or None,
        "metadata": {
            "bubblepod_user_id": user.get("id") or "",
            "bubblepod_username": user.get("username") or "",
        },
    }
    params = {k: v for k, v in params.items() if v}
    if hasattr(client, "v1"):
        customer = client.v1.customers.create(params)
    else:
        import stripe

        customer = stripe.Customer.create(**params)
    cid = _obj_get(customer, "id")
    update_user(user["id"], stripe_customer_id=cid)
    return str(cid)


def create_checkout_session(user: dict[str, Any]) -> dict[str, Any]:
    """Start Stripe Checkout for membership. Active/trialing members go to the portal instead."""
    from studio.members import user_has_access

    status = (user.get("subscription_status") or "none").strip().lower()
    if status in ("active", "trialing") and user_has_access(user):
        portal = create_portal_session(user)
        return {
            "ok": True,
            "already_subscribed": True,
            "reason": "You already have an active membership. Opening billing portal.",
            "portal_url": portal.get("url"),
            "url": portal.get("url"),
        }

    ensure_membership_catalog()
    settings = load_settings()
    price_id = (settings.get("stripe_price_id") or "").strip()
    if not price_id:
        raise RuntimeError("No Stripe price configured.")
    customer_id = ensure_customer_for_user(user)
    base = _studio_base_url()
    suffix = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(8))
    client = get_stripe_client()
    params = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": f"{base}/pricing?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/pricing?checkout=canceled",
        "client_reference_id": user.get("id") or "",
        "metadata": {
            "bubblepod_user_id": user.get("id") or "",
            "bubblepod_username": user.get("username") or "",
        },
        "subscription_data": {
            "metadata": {
                "bubblepod_user_id": user.get("id") or "",
                "bubblepod_username": user.get("username") or "",
                "equal_access": "true",
            }
        },
        "allow_promotion_codes": True,
        "integration_identifier": f"bubblepod_membership_{suffix}",
    }
    try:
        if hasattr(client, "v1"):
            session = client.v1.checkout.sessions.create(params)
        else:
            import stripe

            session = stripe.checkout.Session.create(**params)
    except TypeError:
        # Older API without integration_identifier
        params.pop("integration_identifier", None)
        if hasattr(client, "v1"):
            session = client.v1.checkout.sessions.create(params)
        else:
            import stripe

            session = stripe.checkout.Session.create(**params)
    return {
        "ok": True,
        "session_id": _obj_get(session, "id"),
        "url": _obj_get(session, "url"),
    }


def create_portal_session(user: dict[str, Any]) -> dict[str, Any]:
    customer_id = (user.get("stripe_customer_id") or "").strip()
    if not customer_id:
        customer_id = ensure_customer_for_user(user)
    base = _studio_base_url()
    client = get_stripe_client()
    params = {
        "customer": customer_id,
        "return_url": f"{base}/pricing",
    }
    if hasattr(client, "v1"):
        session = client.v1.billing_portal.sessions.create(params)
    else:
        import stripe

        session = stripe.billing_portal.Session.create(**params)
    return {"ok": True, "url": _obj_get(session, "url")}


def create_refund(*, payment_intent: str = "", charge_id: str = "", amount_cents: int | None = None, reason: str = "") -> dict[str, Any]:
    client = get_stripe_client()
    params: dict[str, Any] = {}
    if payment_intent:
        params["payment_intent"] = payment_intent
    elif charge_id:
        params["charge"] = charge_id
    else:
        raise ValueError("payment_intent or charge_id required")
    if amount_cents is not None and int(amount_cents) > 0:
        params["amount"] = int(amount_cents)
    if reason:
        params["reason"] = reason
    if hasattr(client, "v1"):
        refund = client.v1.refunds.create(params)
    else:
        import stripe

        refund = stripe.Refund.create(**params)
    row = {
        "stripe_id": _obj_get(refund, "id"),
        "charge_id": _obj_get(refund, "charge"),
        "payment_intent": _obj_get(refund, "payment_intent"),
        "amount": _obj_get(refund, "amount"),
        "currency": _obj_get(refund, "currency"),
        "status": _obj_get(refund, "status"),
        "reason": reason or _obj_get(refund, "reason") or "",
        "source": "admin",
    }
    append_ledger("refunds", row)
    return {"ok": True, "refund": row}


def _apply_subscription_to_user(user_id: str, sub: Any) -> None:
    from studio.members import update_user

    status = str(_obj_get(sub, "status") or "none")
    price_id = ""
    items = _obj_get(sub, "items")
    data = _obj_get(items, "data") if items is not None else None
    if isinstance(data, list) and data:
        price = _obj_get(data[0], "price")
        price_id = str(_obj_get(price, "id") or "")
    period_end = _obj_get(sub, "current_period_end")
    if period_end is not None:
        try:
            period_end = datetime.fromtimestamp(int(period_end), tz=timezone.utc).isoformat()
        except Exception:
            period_end = str(period_end)
    update_user(
        user_id,
        subscription_status=status,
        stripe_subscription_id=str(_obj_get(sub, "id") or ""),
        stripe_customer_id=str(_obj_get(sub, "customer") or "") or None,
        stripe_price_id=price_id or None,
        current_period_end=period_end,
        cancel_at_period_end=bool(_obj_get(sub, "cancel_at_period_end")),
    )


def _user_id_from_stripe(*, customer_id: str = "", metadata: dict | None = None, client_reference_id: str = "") -> str:
    from studio.members import get_user_by_id, get_user_by_stripe_customer

    meta = metadata or {}
    uid = str(meta.get("bubblepod_user_id") or client_reference_id or "").strip()
    if uid and get_user_by_id(uid):
        return uid
    if customer_id:
        user = get_user_by_stripe_customer(customer_id)
        if user:
            return user["id"]
    return ""


def handle_webhook(payload: bytes, sig_header: str) -> dict[str, Any]:
    import stripe

    secret = (load_settings().get("stripe_webhook_secret") or "").strip()
    if not secret:
        raise RuntimeError("stripe_webhook_secret not configured")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, secret)
    except Exception as exc:
        raise PermissionError(f"Webhook signature verification failed: {exc}") from exc

    etype = event["type"] if isinstance(event, dict) else event.type
    data_obj = event["data"]["object"] if isinstance(event, dict) else event.data.object
    handled = _dispatch_event(str(etype), data_obj)
    return {"ok": True, "type": etype, "handled": handled}


def _dispatch_event(etype: str, obj: Any) -> bool:
    from studio.members import get_user_by_id, update_user

    if etype == "checkout.session.completed":
        mode = str(_obj_get(obj, "mode") or "")
        uid = _user_id_from_stripe(
            customer_id=str(_obj_get(obj, "customer") or ""),
            metadata=_obj_get(obj, "metadata") or {},
            client_reference_id=str(_obj_get(obj, "client_reference_id") or ""),
        )
        if uid:
            update_user(uid, stripe_customer_id=str(_obj_get(obj, "customer") or "") or None)
            user = get_user_by_id(uid)
            append_ledger(
                "signups",
                {
                    "stripe_id": _obj_get(obj, "id"),
                    "user_id": uid,
                    "username": (user or {}).get("username") or "",
                    "email": (user or {}).get("email") or _obj_get(obj, "customer_email") or "",
                    "mode": mode,
                    "amount_total": _obj_get(obj, "amount_total"),
                    "currency": _obj_get(obj, "currency"),
                    "subscription_id": _obj_get(obj, "subscription"),
                },
            )
            try:
                from studio.email import notify_subscription_started

                if user:
                    notify_subscription_started(user)
            except Exception:
                pass
        sub_id = _obj_get(obj, "subscription")
        if uid and sub_id:
            try:
                import stripe as stripe_mod

                client = get_stripe_client()
                if hasattr(client, "v1"):
                    sub = client.v1.subscriptions.retrieve(str(sub_id))
                else:
                    sub = stripe_mod.Subscription.retrieve(str(sub_id))
                _apply_subscription_to_user(uid, sub)
            except Exception as exc:
                _log.warning("Could not load subscription %s: %s", sub_id, exc)
        return True

    if etype in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        uid = _user_id_from_stripe(
            customer_id=str(_obj_get(obj, "customer") or ""),
            metadata=_obj_get(obj, "metadata") or {},
        )
        if uid:
            if etype.endswith("deleted"):
                update_user(
                    uid,
                    subscription_status="canceled",
                    stripe_subscription_id=str(_obj_get(obj, "id") or ""),
                    cancel_at_period_end=False,
                )
            else:
                _apply_subscription_to_user(uid, obj)
            append_ledger(
                "subscription_events",
                {
                    "stripe_id": f"{etype}:{_obj_get(obj, 'id')}:{_obj_get(obj, 'status')}",
                    "event": etype,
                    "user_id": uid,
                    "subscription_id": _obj_get(obj, "id"),
                    "status": _obj_get(obj, "status"),
                    "customer_id": _obj_get(obj, "customer"),
                },
            )
            try:
                from studio.email import notify_subscription_canceled, notify_subscription_started
                from studio.members import get_user_by_id as _get

                user = _get(uid)
                if user:
                    if etype.endswith("deleted"):
                        notify_subscription_canceled(user, period_end=str(_obj_get(obj, "current_period_end") or "") or None)
                    elif str(_obj_get(obj, "status") or "") in ("active", "trialing"):
                        # Avoid duplicate welcome on every update — only on created.
                        if etype.endswith("created"):
                            notify_subscription_started(user)
            except Exception:
                pass
        return bool(uid)

    if etype in ("invoice.paid", "invoice.payment_failed"):
        uid = _user_id_from_stripe(customer_id=str(_obj_get(obj, "customer") or ""))
        append_ledger(
            "payments",
            {
                "stripe_id": _obj_get(obj, "id"),
                "event": etype,
                "user_id": uid,
                "customer_id": _obj_get(obj, "customer"),
                "subscription_id": _obj_get(obj, "subscription"),
                "payment_intent": _obj_get(obj, "payment_intent"),
                "amount_paid": _obj_get(obj, "amount_paid"),
                "amount_due": _obj_get(obj, "amount_due"),
                "currency": _obj_get(obj, "currency"),
                "status": _obj_get(obj, "status"),
                "hosted_invoice_url": _obj_get(obj, "hosted_invoice_url"),
                "ok": etype == "invoice.paid",
            },
        )
        try:
            from studio.email import notify_payment_failed, notify_payment_receipt
            from studio.members import get_user_by_id

            user = get_user_by_id(uid) if uid else None
            if user:
                if etype == "invoice.paid":
                    notify_payment_receipt(
                        user,
                        amount_cents=_obj_get(obj, "amount_paid"),
                        currency=str(_obj_get(obj, "currency") or "usd"),
                        invoice_url=str(_obj_get(obj, "hosted_invoice_url") or ""),
                    )
                else:
                    notify_payment_failed(
                        user,
                        amount_cents=_obj_get(obj, "amount_due"),
                        currency=str(_obj_get(obj, "currency") or "usd"),
                    )
        except Exception:
            pass
        return True

    if etype == "charge.refunded":
        append_ledger(
            "refunds",
            {
                "stripe_id": _obj_get(obj, "id"),
                "charge_id": _obj_get(obj, "id"),
                "payment_intent": _obj_get(obj, "payment_intent"),
                "amount": _obj_get(obj, "amount_refunded") or _obj_get(obj, "amount"),
                "currency": _obj_get(obj, "currency"),
                "customer_id": _obj_get(obj, "customer"),
                "status": "refunded",
                "source": "webhook",
            },
        )
        return True

    return False


def billing_overview() -> dict[str, Any]:
    from studio.members import list_users

    users = list_users(include_disabled=True)
    members = [u for u in users if u.get("role") == "member"]
    active = [u for u in members if u.get("has_access")]
    ledger = load_ledger()
    payments_ok = [p for p in ledger.get("payments") or [] if p.get("ok")]
    revenue = sum(int(p.get("amount_paid") or 0) for p in payments_ok)
    refund_total = sum(int(r.get("amount") or 0) for r in (ledger.get("refunds") or []))
    return {
        "membership_equal": True,
        "users_total": len(users),
        "admins": len([u for u in users if u.get("role") == "admin"]),
        "members_total": len(members),
        "members_active": len(active),
        "members_inactive": len(members) - len(active),
        "signups_count": len(ledger.get("signups") or []),
        "payments_count": len(payments_ok),
        "refunds_count": len(ledger.get("refunds") or []),
        "gross_revenue_cents": revenue,
        "refunds_cents": refund_total,
        "net_revenue_cents": revenue - refund_total,
        "currency": (load_settings().get("stripe_price_currency") or DEFAULT_CURRENCY).lower(),
        "stripe_configured": stripe_configured(),
        "catalog": public_billing_config(),
    }


def list_ledger(kind: str, limit: int = 100) -> dict[str, Any]:
    data = load_ledger()
    rows = list(data.get(kind) or [])[: max(1, min(500, int(limit)))]
    return {"kind": kind, "count": len(rows), "rows": rows}
