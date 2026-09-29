"""Done-for-you video orders: SQLite store, Stripe Checkout, and delivery.

Admin pages and MCP tools both call these functions so the two cannot drift.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

from studio.paths import USER_DATA

ORDERS_DB = USER_DATA / "orders.db"
ORDER_FILES_DIR = USER_DATA / "order_videos"
MAX_MP4_BYTES = 512 * 1024 * 1024

NICHES = (
    "True Crime",
    "Curiosity Explainers",
    "Bible Stories",
    "History",
    "Finance",
    "Health",
    "Sports",
    "Motivation",
    "Custom niche",
)

PACKAGES: dict[str, dict[str, Any]] = {
    "starter": {"name": "Starter", "videos": 5, "cents": 29700},
    "growth": {"name": "Growth", "videos": 10, "cents": 54700},
    "scale": {"name": "Scale", "videos": 20, "cents": 99700},
}

FORMATS = ("16:9", "9:16", "both")
LENGTHS = ("5", "10")
ORDER_STATUSES = ("paid", "queued", "in_production", "delivered")
VIDEO_STATUSES = ("queued", "scripting", "art", "narration", "rendering", "ready")
PRODUCTION_VIDEO_STATUSES = ("scripting", "art", "narration", "rendering")

_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    niche TEXT NOT NULL DEFAULT '',
    custom_niche TEXT NOT NULL DEFAULT '',
    channel_notes TEXT NOT NULL DEFAULT '',
    package_name TEXT NOT NULL DEFAULT '',
    package_key TEXT NOT NULL DEFAULT '',
    video_count INTEGER NOT NULL,
    video_length TEXT NOT NULL,
    format TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    stripe_session_id TEXT UNIQUE,
    status TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    delivery_note TEXT NOT NULL DEFAULT '',
    delivery_channel TEXT NOT NULL DEFAULT '',
    delivery_email_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    topic TEXT,
    status TEXT NOT NULL,
    mp4_path TEXT,
    mp4_url TEXT,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);
CREATE INDEX IF NOT EXISTS idx_orders_email ON orders(email);
CREATE INDEX IF NOT EXISTS idx_videos_order ON videos(order_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex[:12]


def orders_db_path() -> Path:
    return Path(ORDERS_DB)


def order_files_dir() -> Path:
    return Path(ORDER_FILES_DIR)


def price_cents(package_key: str, video_length: str) -> int:
    """1.5x the package dollars for 10-minute videos, rounded half-up to the nearest dollar."""
    pkg = PACKAGES.get(package_key)
    if not pkg:
        raise ValueError("Choose Starter, Growth, or Scale.")
    length = _normalize_length(video_length)
    base = int(pkg["cents"])
    if length == "5":
        return base
    dollars = (Decimal(base) / Decimal(100)) * Decimal("1.5")
    rounded = int(dollars.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return rounded * 100


def _normalize_length(value: str) -> str:
    raw = (value or "").strip().lower().replace(" ", "").replace("_", "-")
    if raw in ("5", "5min", "5-minute", "5-minutes", "5minute"):
        return "5"
    if raw in ("10", "10min", "10-minute", "10-minutes", "10minute"):
        return "10"
    raise ValueError("Video length must be 5-minute or 10-minute.")


def _normalize_format(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in ("16:9", "16x9", "landscape"):
        return "16:9"
    if raw in ("9:16", "9x16", "portrait", "vertical"):
        return "9:16"
    if raw in ("both", "both formats", "16:9 and 9:16"):
        return "both"
    raise ValueError("Format must be 16:9, 9:16, or Both.")


def _normalize_package(value: str) -> str:
    raw = (value or "").strip().lower()
    if raw in PACKAGES:
        return raw
    for key, pkg in PACKAGES.items():
        if raw == str(pkg["name"]).lower():
            return key
    raise ValueError("Choose a package: Starter, Growth, or Scale.")


def _normalize_order_status(value: str) -> str:
    raw = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "inproduction": "in_production",
        "in_production": "in_production",
        "production": "in_production",
    }
    raw = aliases.get(raw, raw)
    if raw not in ORDER_STATUSES:
        raise ValueError("Order status must be paid, queued, in_production, or delivered.")
    return raw


def _normalize_video_status(value: str) -> str:
    raw = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if raw not in VIDEO_STATUSES:
        raise ValueError(
            "Video status must be queued, scripting, art, narration, rendering, or ready."
        )
    return raw


def format_label(value: str) -> str:
    return "Both" if value == "both" else value


def length_label(value: str) -> str:
    return f"{value}-minute"


def niche_label(order: dict[str, Any]) -> str:
    niche = (order.get("niche") or "").strip()
    custom = (order.get("custom_niche") or "").strip()
    if niche == "Custom niche" and custom:
        return custom
    return niche


def _connect() -> sqlite3.Connection:
    path = orders_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


@contextmanager
def _conn():
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _order_row(conn: sqlite3.Connection, order_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def _videos_for(conn: sqlite3.Connection, order_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM videos WHERE order_id = ? ORDER BY position ASC, id ASC",
            (order_id,),
        )
    )


def _video_row(conn: sqlite3.Connection, video_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()


def _video_file(video: dict[str, Any] | sqlite3.Row) -> Path | None:
    rel = str(video["mp4_path"] or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    root = order_files_dir().resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() else None


def _mp4_attached(video: dict[str, Any] | sqlite3.Row) -> bool:
    return _video_file(video) is not None


def _progress(videos: list[dict[str, Any]], order_status: str) -> int:
    if order_status == "delivered":
        return 100
    if not videos:
        return 0
    weights = {name: i + 1 for i, name in enumerate(VIDEO_STATUSES)}
    total = 0
    for video in videos:
        total += weights.get(video.get("status") or "", 0)
    return int(round(100 * total / (len(videos) * len(VIDEO_STATUSES))))


def _admin_video(video: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    row = dict(video)
    attached = _mp4_attached(row)
    return {
        "id": row["id"],
        "order_id": row["order_id"],
        "topic": row.get("topic") or "",
        "status": row["status"],
        "position": row["position"],
        "mp4_attached": attached,
        "mp4_url": f"/api/orders/download/{row['id']}" if attached and row["status"] == "ready" else "",
    }


def _admin_order(order: sqlite3.Row | dict[str, Any], videos: list[sqlite3.Row] | list[dict]) -> dict[str, Any]:
    row = dict(order)
    vids = [_admin_video(v) for v in videos]
    channel = row.get("delivery_channel") or ""
    email_error = row.get("delivery_email_error") or ""
    return {
        "id": row["id"],
        "name": row.get("name") or "",
        "email": row.get("email") or "",
        "niche": niche_label(row),
        "niche_preset": row.get("niche") or "",
        "custom_niche": row.get("custom_niche") or "",
        "channel_notes": row.get("channel_notes") or "",
        "package_name": row.get("package_name") or "",
        "package_key": row.get("package_key") or "",
        "video_count": int(row["video_count"]),
        "video_length": row.get("video_length") or "",
        "video_length_label": length_label(str(row.get("video_length") or "")),
        "format": row.get("format") or "",
        "format_label": format_label(str(row.get("format") or "")),
        "amount_cents": int(row["amount_cents"]),
        "status": row["status"],
        "stripe_session_id": row.get("stripe_session_id") or "",
        "created_at": row.get("created_at") or "",
        "reviewed": bool(row.get("reviewed")),
        "delivered_at": row.get("delivered_at") or "",
        "delivery_note": row.get("delivery_note") or "",
        "delivery_channel": channel,
        "delivery_email_error": email_error,
        "email_sent": channel == "email" and not email_error,
        "can_deliver": bool(vids) and all(v["status"] == "ready" and v["mp4_attached"] for v in vids),
        "progress_pct": _progress(vids, row["status"]),
        "videos": vids,
    }


def _client_video(video: dict[str, Any], email: str) -> dict[str, Any]:
    ready = video["status"] == "ready" and video.get("mp4_attached")
    url = ""
    if ready:
        url = f"/api/orders/download/{video['id']}?email={quote(email)}"
    return {
        "id": video["id"],
        "topic": video.get("topic") or "",
        "status": video["status"],
        "position": video["position"],
        "ready": ready,
        "download_url": url,
    }


def _client_order(order: dict[str, Any]) -> dict[str, Any]:
    email = order.get("email") or ""
    videos = [_client_video(v, email) for v in order.get("videos") or []]
    delivered = order.get("status") == "delivered"
    if delivered and order.get("email_sent"):
        message = "Your videos are ready. We also emailed download links to you."
    elif delivered:
        message = "Your videos are ready. Download them below."
    else:
        message = ""
    return {
        "id": order["id"],
        "name": order.get("name") or "",
        "niche": order.get("niche") or "",
        "package_name": order.get("package_name") or "",
        "video_count": order.get("video_count"),
        "video_length": order.get("video_length") or "",
        "video_length_label": order.get("video_length_label") or "",
        "format": order.get("format") or "",
        "format_label": order.get("format_label") or "",
        "amount_cents": order.get("amount_cents"),
        "status": order.get("status"),
        "created_at": order.get("created_at") or "",
        "delivered_at": order.get("delivered_at") or "",
        "progress_pct": order.get("progress_pct") or 0,
        "delivery_message": message,
        "videos": videos,
    }


def _load_admin(conn: sqlite3.Connection, order_id: str) -> dict[str, Any]:
    row = _order_row(conn, order_id)
    if not row or row["status"] == "pending_payment":
        raise LookupError("Order not found.")
    return _admin_order(row, _videos_for(conn, order_id))


def stripe_status() -> dict[str, Any]:
    from studio.settings import is_placeholder_secret, load_settings

    key = (load_settings().get("stripe_secret_key") or "").strip()
    configured = bool(key) and not is_placeholder_secret(key)
    if not configured:
        mode = "missing"
    elif key.startswith("sk_test_"):
        mode = "test"
    elif key.startswith("sk_live_"):
        mode = "live"
    else:
        mode = "other"
    return {"configured": configured, "mode": mode}


def public_config() -> dict[str, Any]:
    status = stripe_status()
    packages = []
    for key, pkg in PACKAGES.items():
        packages.append(
            {
                "key": key,
                "name": pkg["name"],
                "videos": pkg["videos"],
                "prices": {
                    "5": price_cents(key, "5"),
                    "10": price_cents(key, "10"),
                },
            }
        )
    return {
        "ok": True,
        "stripe_configured": status["configured"],
        "stripe_mode": status["mode"],
        "niches": list(NICHES),
        "formats": [{"value": "16:9", "label": "16:9"}, {"value": "9:16", "label": "9:16"}, {"value": "both", "label": "Both"}],
        "lengths": [{"value": "5", "label": "5-minute"}, {"value": "10", "label": "10-minute"}],
        "packages": packages,
        "stripe_error": ""
        if status["configured"]
        else "Stripe is not configured. Add a secret key in Settings before checkout.",
    }


def _validate_brief(data: dict[str, Any]) -> dict[str, Any]:
    niche = (data.get("niche") or "").strip()
    if niche not in NICHES:
        raise ValueError("Choose a niche.")
    custom = (data.get("custom_niche") or "").strip()
    if niche == "Custom niche":
        if not custom:
            raise ValueError("Enter a custom niche.")
        if len(custom) > 200:
            raise ValueError("Custom niche is too long.")
    else:
        custom = ""
    notes = (data.get("channel_notes") or "").strip()
    if len(notes) > 4000:
        raise ValueError("Channel notes are too long (4000 characters max).")
    package_key = _normalize_package(str(data.get("package") or ""))
    length = _normalize_length(str(data.get("video_length") or "5"))
    fmt = _normalize_format(str(data.get("format") or "16:9"))
    pkg = PACKAGES[package_key]
    return {
        "niche": niche,
        "custom_niche": custom,
        "channel_notes": notes,
        "package_key": package_key,
        "package_name": pkg["name"],
        "video_count": int(pkg["videos"]),
        "video_length": length,
        "format": fmt,
        "amount_cents": price_cents(package_key, length),
    }


def create_checkout(data: dict[str, Any]) -> dict[str, Any]:
    """Start a one-time Stripe Checkout. Price is computed here, not taken from the client."""
    brief = _validate_brief(data)
    status = stripe_status()
    if not status["configured"]:
        raise RuntimeError(
            "Stripe is not configured. Add a secret key in Settings before checkout."
        )
    created = _now()
    order_id = _new_id("ord_")
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO orders (
                id, name, email, niche, custom_niche, channel_notes,
                package_name, package_key, video_count, video_length, format,
                amount_cents, stripe_session_id, status, reviewed, created_at
            ) VALUES (?, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending_payment', 0, ?)
            """,
            (
                order_id,
                brief["niche"],
                brief["custom_niche"],
                brief["channel_notes"],
                brief["package_name"],
                brief["package_key"],
                brief["video_count"],
                brief["video_length"],
                brief["format"],
                brief["amount_cents"],
                created,
            ),
        )
    try:
        session = _create_stripe_session(order_id, brief)
    except Exception:
        with _conn() as conn:
            conn.execute("DELETE FROM orders WHERE id = ? AND status = 'pending_payment'", (order_id,))
        raise
    session_id = str(_obj_get(session, "id") or "")
    url = str(_obj_get(session, "url") or "")
    if not session_id or not url:
        with _conn() as conn:
            conn.execute("DELETE FROM orders WHERE id = ? AND status = 'pending_payment'", (order_id,))
        raise RuntimeError("Stripe did not return a checkout URL.")
    with _conn() as conn:
        conn.execute(
            "UPDATE orders SET stripe_session_id = ? WHERE id = ?",
            (session_id, order_id),
        )
    return {
        "ok": True,
        "url": url,
        "session_id": session_id,
        "amount_cents": brief["amount_cents"],
        "stripe_mode": status["mode"],
    }


def _create_stripe_session(order_id: str, brief: dict[str, Any]) -> Any:
    from studio.settings import resolve_public_base_url
    from studio.stripe_billing import get_stripe_client

    base = resolve_public_base_url().rstrip("/")
    length = length_label(brief["video_length"])
    fmt = format_label(brief["format"])
    name = f"{brief['package_name']} — {brief['video_count']} {length} videos ({fmt})"
    params = {
        "mode": "payment",
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(brief["amount_cents"]),
                    "product_data": {
                        "name": name[:120],
                        "description": "Done-for-you Bubble Pod videos",
                    },
                },
            }
        ],
        "custom_fields": [
            {
                "key": "customername",
                "label": {"type": "custom", "custom": "Your name"},
                "type": "text",
            }
        ],
        "success_url": f"{base}/order/success?session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/order/cancel",
        "metadata": {
            "type": "video_order",
            "order_id": order_id,
            "package_key": brief["package_key"],
            "video_length": brief["video_length"],
            "format": brief["format"],
            "niche": brief["niche"][:200],
            "custom_niche": brief["custom_niche"][:200],
            "video_count": str(brief["video_count"]),
            "amount_cents": str(brief["amount_cents"]),
        },
        "payment_intent_data": {
            "metadata": {"type": "video_order", "order_id": order_id},
        },
    }
    client = get_stripe_client()
    if hasattr(client, "v1"):
        return client.v1.checkout.sessions.create(params)
    import stripe

    return stripe.checkout.Session.create(**params)


def _obj_get(obj: Any, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _session_snapshot(obj: Any) -> dict[str, Any]:
    meta = _obj_get(obj, "metadata") or {}
    if not isinstance(meta, dict):
        if hasattr(meta, "to_dict"):
            meta = meta.to_dict()
        else:
            meta = {
                "type": _obj_get(meta, "type") or "",
                "order_id": _obj_get(meta, "order_id") or "",
                "package_key": _obj_get(meta, "package_key") or "",
                "video_length": _obj_get(meta, "video_length") or "",
                "format": _obj_get(meta, "format") or "",
                "niche": _obj_get(meta, "niche") or "",
                "custom_niche": _obj_get(meta, "custom_niche") or "",
                "video_count": _obj_get(meta, "video_count") or "",
                "amount_cents": _obj_get(meta, "amount_cents") or "",
            }
    details = _obj_get(obj, "customer_details") or {}
    detail_email = _obj_get(details, "email") or ""
    detail_name = _obj_get(details, "name") or ""
    custom_name = ""
    for field in _obj_get(obj, "custom_fields") or []:
        key = str(_obj_get(field, "key") or "")
        text = _obj_get(field, "text") or {}
        value = _obj_get(text, "value") or ""
        if key in ("customername", "customer_name") and value:
            custom_name = str(value).strip()
    email = str(_obj_get(obj, "customer_email") or detail_email or "").strip()
    name = (custom_name or str(detail_name or "")).strip()
    return {
        "id": str(_obj_get(obj, "id") or ""),
        "payment_status": str(_obj_get(obj, "payment_status") or ""),
        "amount_total": _obj_get(obj, "amount_total"),
        "email": email,
        "name": name,
        "metadata": {str(k): "" if v is None else str(v) for k, v in dict(meta).items()},
    }


def fulfill_checkout_session(session: Any) -> dict[str, Any]:
    """Create the paid order and queued video rows. Idempotent on stripe_session_id."""
    snap = _session_snapshot(session)
    meta = snap["metadata"]
    if meta.get("type") != "video_order":
        return {"ok": False, "skipped": True}
    if snap["payment_status"] not in ("paid", "no_payment_required"):
        return {"ok": False, "skipped": True, "reason": "unpaid"}
    session_id = snap["id"]
    if not session_id:
        raise ValueError("Checkout session id missing.")
    order_id = (meta.get("order_id") or "").strip()
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE stripe_session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None and order_id:
            row = _order_row(conn, order_id)
        if row is None:
            row = _insert_paid_from_metadata(conn, snap)
        elif row["status"] == "pending_payment":
            _promote_pending(conn, row, snap)
            row = _order_row(conn, row["id"])
        elif row["stripe_session_id"] and row["stripe_session_id"] != session_id:
            raise ValueError("This order is already tied to a different checkout session.")
        assert row is not None
        _ensure_video_rows(conn, row)
        paid = _load_admin(conn, row["id"])
    return {"ok": True, "order_id": paid["id"], "video_count": len(paid["videos"]), "order": paid}


def _expected_amount(row: sqlite3.Row | None, meta: dict[str, str]) -> int:
    if row is not None:
        return int(row["amount_cents"])
    package_key = _normalize_package(meta.get("package_key") or "")
    length = _normalize_length(meta.get("video_length") or "5")
    return price_cents(package_key, length)


def _check_amount(row: sqlite3.Row | None, snap: dict[str, Any]) -> None:
    expected = _expected_amount(row, snap["metadata"])
    paid = snap.get("amount_total")
    if paid is None:
        raise ValueError("Checkout session has no amount_total.")
    if int(paid) != int(expected):
        raise ValueError("Paid amount does not match the server-side order price.")


def _promote_pending(conn: sqlite3.Connection, row: sqlite3.Row, snap: dict[str, Any]) -> None:
    _check_amount(row, snap)
    conn.execute(
        """
        UPDATE orders
        SET name = ?, email = ?, stripe_session_id = ?, status = 'paid', reviewed = 0
        WHERE id = ? AND status = 'pending_payment'
        """,
        (snap["name"], snap["email"], snap["id"], row["id"]),
    )


def _insert_paid_from_metadata(conn: sqlite3.Connection, snap: dict[str, Any]) -> sqlite3.Row:
    meta = snap["metadata"]
    _check_amount(None, snap)
    package_key = _normalize_package(meta.get("package_key") or "")
    length = _normalize_length(meta.get("video_length") or "5")
    fmt = _normalize_format(meta.get("format") or "16:9")
    niche = (meta.get("niche") or "").strip()
    if niche not in NICHES:
        niche = "Custom niche"
    pkg = PACKAGES[package_key]
    order_id = (meta.get("order_id") or "").strip() or _new_id("ord_")
    conn.execute(
        """
        INSERT INTO orders (
            id, name, email, niche, custom_niche, channel_notes,
            package_name, package_key, video_count, video_length, format,
            amount_cents, stripe_session_id, status, reviewed, created_at
        ) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 'paid', 0, ?)
        """,
        (
            order_id,
            snap["name"],
            snap["email"],
            niche,
            (meta.get("custom_niche") or "").strip(),
            pkg["name"],
            package_key,
            int(pkg["videos"]),
            length,
            fmt,
            price_cents(package_key, length),
            snap["id"],
            _now(),
        ),
    )
    row = _order_row(conn, order_id)
    if row is None:
        raise RuntimeError("Could not store the paid order.")
    return row


def _ensure_video_rows(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    have = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE order_id = ?",
        (row["id"],),
    ).fetchone()["n"]
    target = int(row["video_count"])
    created = _now()
    for position in range(int(have) + 1, target + 1):
        conn.execute(
            """
            INSERT INTO videos (id, order_id, topic, status, mp4_path, mp4_url, position, created_at)
            VALUES (?, ?, NULL, 'queued', NULL, NULL, ?, ?)
            """,
            (_new_id("vid_"), row["id"], position, created),
        )


def order_for_session(session_id: str) -> dict[str, Any]:
    sid = (session_id or "").strip()
    if not sid.startswith("cs_"):
        raise ValueError("Unknown checkout session.")
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE stripe_session_id = ?",
            (sid,),
        ).fetchone()
        if row is not None and row["status"] != "pending_payment":
            order = _client_order(_load_admin(conn, row["id"]))
            return {"ok": True, "pending": False, "order": order}
    if stripe_status()["configured"]:
        try:
            session = _retrieve_stripe_session(sid)
        except Exception as exc:
            return {"ok": True, "pending": True, "order": None, "detail": str(exc)}
        fulfill_checkout_session(session)
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE stripe_session_id = ?",
                (sid,),
            ).fetchone()
            if row is not None and row["status"] != "pending_payment":
                order = _client_order(_load_admin(conn, row["id"]))
                return {"ok": True, "pending": False, "order": order}
    return {"ok": True, "pending": True, "order": None}


def _retrieve_stripe_session(session_id: str) -> Any:
    from studio.stripe_billing import get_stripe_client

    client = get_stripe_client()
    if hasattr(client, "v1"):
        return client.v1.checkout.sessions.retrieve(session_id)
    import stripe

    return stripe.checkout.Session.retrieve(session_id)


def list_orders_for_email(email: str) -> dict[str, Any]:
    target = (email or "").strip().lower()
    if not target or "@" not in target:
        raise ValueError("Enter the email you used at checkout.")
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM orders
            WHERE lower(email) = ? AND status != 'pending_payment'
            ORDER BY created_at DESC, id DESC
            """,
            (target,),
        ).fetchall()
        orders = [
            _client_order(_admin_order(row, _videos_for(conn, row["id"])))
            for row in rows
        ]
    return {"ok": True, "email": target, "orders": orders}


def list_production_orders() -> dict[str, Any]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM orders
            WHERE status != 'pending_payment'
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        orders = [_admin_order(row, _videos_for(conn, row["id"])) for row in rows]
        unreviewed = conn.execute(
            """
            SELECT COUNT(*) AS n FROM orders
            WHERE status != 'pending_payment' AND reviewed = 0
            """
        ).fetchone()["n"]
    return {"ok": True, "count": len(orders), "unreviewed_count": int(unreviewed), "orders": orders}


def get_production_order(order_id: str) -> dict[str, Any]:
    with _conn() as conn:
        order = _load_admin(conn, (order_id or "").strip())
    return {"ok": True, "order": order}


def mark_order_reviewed(order_id: str) -> dict[str, Any]:
    oid = (order_id or "").strip()
    with _conn() as conn:
        _load_admin(conn, oid)
        conn.execute("UPDATE orders SET reviewed = 1 WHERE id = ?", (oid,))
        order = _load_admin(conn, oid)
        unreviewed = conn.execute(
            """
            SELECT COUNT(*) AS n FROM orders
            WHERE status != 'pending_payment' AND reviewed = 0
            """
        ).fetchone()["n"]
    return {"ok": True, "unreviewed_count": int(unreviewed), "order": order}


def update_order_status(order_id: str, status: str) -> dict[str, Any]:
    """Change order status. `delivered` always runs the approval/delivery path."""
    new_status = _normalize_order_status(status)
    oid = (order_id or "").strip()
    if new_status == "delivered":
        return deliver_order(oid)
    with _conn() as conn:
        order = _load_admin(conn, oid)
        if order["status"] == "delivered":
            raise ValueError("This order is already delivered.")
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, oid))
        updated = _load_admin(conn, oid)
    return {"ok": True, "order": updated}


def update_order_video(
    order_id: str,
    video_id: str,
    *,
    topic: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    oid = (order_id or "").strip()
    vid = (video_id or "").strip()
    if topic is None and status is None:
        raise ValueError("Provide a topic and/or a video status.")
    new_status = _normalize_video_status(status) if status is not None else None
    with _conn() as conn:
        order = _load_admin(conn, oid)
        video = _video_row(conn, vid)
        if not video or video["order_id"] != oid:
            raise LookupError("Video not found on this order.")
        if new_status == "ready" and not _mp4_attached(video):
            raise ValueError("Upload an MP4 before marking this video ready.")
        if topic is not None:
            text = topic.strip()
            if len(text) > 500:
                raise ValueError("Topic is too long (500 characters max).")
            conn.execute(
                "UPDATE videos SET topic = ? WHERE id = ?",
                (text or None, vid),
            )
        if new_status is not None:
            conn.execute("UPDATE videos SET status = ? WHERE id = ?", (new_status, vid))
            if new_status in PRODUCTION_VIDEO_STATUSES and order["status"] in ("paid", "queued"):
                conn.execute(
                    "UPDATE orders SET status = 'in_production' WHERE id = ?",
                    (oid,),
                )
        updated = _load_admin(conn, oid)
    return {"ok": True, "order": updated, "video": next(v for v in updated["videos"] if v["id"] == vid)}


def mark_video_ready(order_id: str, video_id: str) -> dict[str, Any]:
    return update_order_video(order_id, video_id, status="ready")


def _assert_local_mp4(file_path: str) -> Path:
    raw = (file_path or "").strip().strip('"')
    if not raw:
        raise ValueError("Provide a local MP4 path.")
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://", "ftp://")) or lowered.startswith("file://"):
        raise ValueError("Remote URLs are not accepted. Provide a local MP4 path.")
    path = Path(raw)
    if not path.is_file():
        raise ValueError("MP4 file was not found on this computer.")
    if path.suffix.lower() != ".mp4":
        raise ValueError("File must be an .mp4.")
    _assert_mp4_header(path)
    return path


def _assert_mp4_header(path: Path) -> None:
    with path.open("rb") as handle:
        head = handle.read(32)
    if b"ftyp" not in head:
        raise ValueError("File is not a valid MP4.")


def _install_mp4_file(order_id: str, video_id: str, source: Path) -> dict[str, Any]:
    dest_dir = order_files_dir() / order_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{video_id}.mp4"
    staging = dest_dir / f"{video_id}.staging"
    shutil.copy2(source, staging)
    try:
        with _conn() as conn:
            video = _video_row(conn, video_id)
            if not video or video["order_id"] != order_id:
                raise LookupError("Video not found on this order.")
            _load_admin(conn, order_id)
            if dest.exists():
                dest.unlink()
            staging.replace(dest)
            conn.execute(
                "UPDATE videos SET mp4_path = ?, mp4_url = ? WHERE id = ?",
                (f"{order_id}/{video_id}.mp4", f"/api/orders/download/{video_id}", video_id),
            )
            updated = _load_admin(conn, order_id)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    video_out = next(v for v in updated["videos"] if v["id"] == video_id)
    return {"ok": True, "order": updated, "video": video_out}


def attach_video_mp4(order_id: str, video_id: str, file_path: str) -> dict[str, Any]:
    """Copy a local MP4 onto the video so the client can download it later."""
    source = _assert_local_mp4(file_path)
    oid = (order_id or "").strip()
    vid = (video_id or "").strip()
    return _install_mp4_file(oid, vid, source)


def save_uploaded_mp4(order_id: str, video_id: str, source: BinaryIO) -> dict[str, Any]:
    oid = (order_id or "").strip()
    vid = (video_id or "").strip()
    with _conn() as conn:
        video = _video_row(conn, vid)
        if not video or video["order_id"] != oid:
            raise LookupError("Video not found on this order.")
        _load_admin(conn, oid)
    dest_dir = order_files_dir() / oid
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f"{vid}.upload"
    total = 0
    head = b""
    try:
        with tmp.open("wb") as handle:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:32]
                total += len(chunk)
                if total > MAX_MP4_BYTES:
                    raise ValueError("MP4 is larger than 512 MB.")
                handle.write(chunk)
        if total < 8 or b"ftyp" not in head:
            raise ValueError("Upload must be an MP4 file.")
        return _install_mp4_file(oid, vid, tmp)
    finally:
        tmp.unlink(missing_ok=True)


def deliver_order(order_id: str) -> dict[str, Any]:
    """Approve the order: mark it delivered and email or record in-app delivery."""
    oid = (order_id or "").strip()
    with _conn() as conn:
        order = _load_admin(conn, oid)
        if not order["can_deliver"]:
            raise ValueError(
                "Mark delivered is available only when every video is ready and has an MP4."
            )
        already = (
            order["status"] == "delivered"
            and order["delivery_channel"] == "email"
            and not order["delivery_email_error"]
        )
        if not already and order["status"] != "delivered":
            conn.execute(
                "UPDATE orders SET status = 'delivered', delivered_at = ? WHERE id = ?",
                (order["delivered_at"] or _now(), oid),
            )
        snapshot = _load_admin(conn, oid) if not already else order
    if already:
        return _delivery_result(order, already=True, email_sent=True)
    email_sent, channel, note, error = _send_delivery_email(snapshot)
    with _conn() as conn:
        conn.execute(
            """
            UPDATE orders
            SET status = 'delivered',
                delivered_at = COALESCE(delivered_at, ?),
                delivery_channel = ?,
                delivery_note = ?,
                delivery_email_error = ?
            WHERE id = ?
            """,
            (_now(), channel, note, error, oid),
        )
        updated = _load_admin(conn, oid)
    return _delivery_result(updated, already=False, email_sent=email_sent)


def _delivery_result(order: dict[str, Any], *, already: bool, email_sent: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "already_delivered": already,
        "email_sent": bool(email_sent),
        "delivery_channel": order.get("delivery_channel") or "",
        "delivery_note": order.get("delivery_note") or "",
        "delivery_email_error": order.get("delivery_email_error") or "",
        "delivered_at": order.get("delivered_at") or "",
        "order": order,
    }


def _send_delivery_email(order: dict[str, Any]) -> tuple[bool, str, str, str]:
    from studio.email import email_enabled, send_email
    from studio.settings import resolve_public_base_url

    base = resolve_public_base_url().rstrip("/")
    email = (order.get("email") or "").strip()
    my_orders = f"{base}/my-orders?email={quote(email)}"
    lines = [
        f"Hi {order.get('name') or 'there'},",
        "",
        f"Your {order.get('package_name')} order is ready.",
        f"Order: {order.get('id')}",
        f"Niche: {order.get('niche')}",
        f"Package: {order.get('package_name')} · {order.get('video_count')} × {order.get('video_length_label')} · {order.get('format_label')}",
        f"Amount paid: ${int(order.get('amount_cents') or 0) / 100:.2f}",
        "",
        f"My Orders: {my_orders}",
        "",
        "Downloads:",
    ]
    for video in order.get("videos") or []:
        title = (video.get("topic") or "").strip() or f"Video {video.get('position')}"
        url = f"{base}/api/orders/download/{video['id']}?email={quote(email)}"
        lines.append(f"- {title}: {url}")
    lines.append("")
    text = "\n".join(lines)
    if not email_enabled():
        return (
            False,
            "in_app",
            "Delivered in the app. Email is not configured, so no message was sent. "
            "The client can download videos from My Orders with their checkout email.",
            "",
        )
    try:
        send_email(
            to=email,
            subject=f"Your videos are ready — {order.get('package_name') or 'Bubble Pod'}",
            text=text,
        )
    except Exception as exc:
        return (
            False,
            "in_app",
            "Delivered in the app. The client can download from My Orders. The delivery email could not be sent.",
            str(exc),
        )
    return (
        True,
        "email",
        f"Emailed {email} with the order summary, a My Orders link, and download links.",
        "",
    )


def resolve_download(video_id: str, email: str = "", *, is_admin: bool = False) -> tuple[Path, str]:
    vid = (video_id or "").strip()
    with _conn() as conn:
        video = _video_row(conn, vid)
        if not video:
            raise LookupError("Not found.")
        order = _order_row(conn, video["order_id"])
        if not order or order["status"] == "pending_payment":
            raise LookupError("Not found.")
        owner = (order["email"] or "").strip().lower()
        given = (email or "").strip().lower()
        if not is_admin and given != owner:
            raise LookupError("Not found.")
        if video["status"] != "ready":
            raise LookupError("Not found." if not is_admin else "This video is not ready for download.")
        path = _video_file(video)
        if path is None:
            raise LookupError("Not found." if not is_admin else "MP4 file is missing on disk.")
        topic = (video["topic"] or "").strip()
        stem = _safe_filename(topic) if topic else f"video-{video['position']}"
    return path, f"{stem}.mp4"


def _safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value.strip())
    cleaned = cleaned.strip("-")[:80]
    return cleaned or "video"


def unreviewed_count() -> int:
    with _conn() as conn:
        return int(
            conn.execute(
                """
                SELECT COUNT(*) AS n FROM orders
                WHERE status != 'pending_payment' AND reviewed = 0
                """
            ).fetchone()["n"]
        )
