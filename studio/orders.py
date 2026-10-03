"""Done-for-you video orders: SQLite store, Stripe Checkout, and delivery.

Admin pages and MCP tools both call these functions so the two cannot drift.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, BinaryIO
from studio.paths import USER_DATA

_log = logging.getLogger("bubblepod.orders")

ORDERS_DB = USER_DATA / "orders.db"
ORDER_FILES_DIR = USER_DATA / "order_videos"
ART_THUMBS_DIR = USER_DATA / "order_art_thumbs"
MAX_MP4_BYTES = 512 * 1024 * 1024
MAX_THUMB_BYTES = 4 * 1024 * 1024
_THUMB_NAME = re.compile(r"^[a-z0-9_]{1,40}\.(png|jpg|jpeg|webp|gif)$")

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

# Defaults until Admin → Pricing saves a catalog. 10-minute videos use the multiplier.
DEFAULT_PACKAGES: dict[str, dict[str, Any]] = {
    "starter": {"name": "Starter", "videos": 5, "cents": 4900, "days": 5, "per_day": 1},
    "growth": {"name": "Growth", "videos": 10, "cents": 8900, "days": 5, "per_day": 2},
    "scale": {"name": "Scale", "videos": 20, "cents": 19700, "days": 5, "per_day": 4},
}
DEFAULT_TEN_MINUTE_MULTIPLIER = Decimal("1.5")
PACKAGE_KEYS = ("starter", "growth", "scale")

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
    delivery_email_error TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    days INTEGER,
    per_day INTEGER,
    approval_notice_id INTEGER,
    generation_approved_at TEXT,
    art_style TEXT NOT NULL DEFAULT '',
    art_style_name TEXT NOT NULL DEFAULT ''
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
    topic_id TEXT,
    job_id TEXT,
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


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def dollars_to_cents(value: Any) -> int:
    amount = Decimal(str(value).strip())
    cents = (amount * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def _multiplier(value: Any) -> float:
    try:
        number = Decimal(str(value if value not in (None, "") else DEFAULT_TEN_MINUTE_MULTIPLIER))
    except Exception as exc:
        raise ValueError("10-minute multiplier must be a number.") from exc
    if number < Decimal("1") or number > Decimal("5"):
        raise ValueError("10-minute multiplier must be between 1 and 5.")
    return float(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalize_order_packages(raw: Any, *, multiplier: Any = None) -> dict[str, Any]:
    """Validate the admin catalog. Missing packages keep the built-in offer."""
    stored = raw if isinstance(raw, dict) else {}
    packages: dict[str, dict[str, Any]] = {}
    for key in PACKAGE_KEYS:
        base = DEFAULT_PACKAGES[key]
        row = stored.get(key) if isinstance(stored.get(key), dict) else {}
        videos = _clamp_int(row.get("videos"), int(base["videos"]), 1, 100)
        days = _clamp_int(row.get("days"), int(base["days"]), 1, 60)
        per_day = _clamp_int(row.get("per_day"), int(base["per_day"]), 1, 20)
        if "cents" in row and row.get("cents") not in (None, ""):
            cents = _clamp_int(row.get("cents"), int(base["cents"]), 50, 100_000_00)
        elif row.get("price") not in (None, ""):
            try:
                cents = dollars_to_cents(row.get("price"))
            except Exception as exc:
                raise ValueError(f"{base['name']} price must be a dollar amount.") from exc
            if cents < 50 or cents > 100_000_00:
                raise ValueError(f"{base['name']} price must be between $0.50 and $100,000.")
        else:
            cents = int(base["cents"])
        paced = per_day * days
        if videos != paced:
            raise ValueError(
                f"{base['name']} lists {videos} videos, but {per_day} per day over {days} days is {paced}."
            )
        name = " ".join(str(row.get("name") or base["name"]).split()) or str(base["name"])
        packages[key] = {
            "name": name[:40],
            "videos": videos,
            "cents": cents,
            "days": days,
            "per_day": per_day,
        }
    return {"packages": packages, "ten_minute_multiplier": _multiplier(multiplier)}


def configured_pricing() -> dict[str, Any]:
    """Admin-saved catalog, or the built-in Starter / Growth / Scale offer."""
    from studio.settings import load_settings

    data = load_settings()
    stored = data.get("order_packages")
    try:
        return normalize_order_packages(stored, multiplier=data.get("order_ten_minute_multiplier"))
    except ValueError:
        return normalize_order_packages(None)


def package_catalog() -> dict[str, dict[str, Any]]:
    return configured_pricing()["packages"]


def save_order_pricing(raw: Any, *, multiplier: Any = None) -> dict[str, Any]:
    """Persist the video-order catalog on the admin settings store."""
    from studio.settings import save_settings

    pricing = normalize_order_packages(raw, multiplier=multiplier)
    save_settings({
        "order_packages": pricing["packages"],
        "order_ten_minute_multiplier": pricing["ten_minute_multiplier"],
    })
    return public_config()


def art_thumbs_dir() -> Path:
    return Path(ART_THUMBS_DIR)


def default_order_art_styles() -> list[dict[str, str]]:
    """Pipeline styles buyers can pick until an admin saves a catalog."""
    from studio.art_style import list_art_styles

    return [
        {"id": row["id"], "name": row["label"], "thumbnail": ""}
        for row in list_art_styles()
    ]


def _safe_thumb_name(value: Any) -> str:
    name = str(value or "").strip().lower()
    if not name or not _THUMB_NAME.fullmatch(name):
        return ""
    return name


def _thumb_path(name: str) -> Path:
    """Resolve a stored thumbnail filename. Rejects anything that is not a bare file name."""
    safe = _safe_thumb_name(name)
    if not safe:
        raise LookupError("Thumbnail not found.")
    root = art_thumbs_dir().resolve()
    path = (root / safe).resolve()
    if path.parent != root:
        raise LookupError("Thumbnail not found.")
    return path


def _sniff_image(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise ValueError("Thumbnail must be a PNG, JPEG, WEBP, or GIF.")


def _thumb_media(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(path.suffix.lower(), "application/octet-stream")


def normalize_order_art_styles(raw: Any, *, thumbnails: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Validate the buyer catalog. Ids must be pipeline art styles. Thumbnails stay server-side filenames."""
    rows = raw
    if isinstance(raw, dict):
        rows = raw.get("items")
    if not isinstance(rows, list):
        raise ValueError("Art styles must be a list.")
    from studio.art_style import ART_STYLES, normalize_art_style

    kept = thumbnails or {}
    styles: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each art style needs an id and a name.")
        try:
            style_id = normalize_art_style(str(row.get("id") or ""))
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        if style_id in seen:
            raise ValueError(f"Art style {style_id} is listed more than once.")
        seen.add(style_id)
        name = " ".join(str(row.get("name") or "").split()) or ART_STYLES[style_id]["label"]
        if len(name) > 80:
            raise ValueError("Art style name is too long (80 characters max).")
        styles.append({
            "id": style_id,
            "name": name,
            "thumbnail": _safe_thumb_name(kept.get(style_id) or ""),
        })
    return styles


def _stored_art_style_items() -> list[Any] | None:
    """None when the admin has not saved a catalog yet."""
    from studio.settings import load_settings

    raw = load_settings().get("order_art_styles")
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        return raw["items"]
    if isinstance(raw, list) and raw:
        return raw
    return None


def configured_art_styles() -> list[dict[str, str]]:
    """Admin-saved styles, or every pipeline style with no thumbnail yet."""
    stored = _stored_art_style_items()
    if stored is None:
        return default_order_art_styles()
    from studio.art_style import normalize_art_style

    thumbs: dict[str, str] = {}
    for row in stored:
        if not isinstance(row, dict):
            continue
        try:
            canon = normalize_art_style(str(row.get("id") or ""))
        except RuntimeError:
            continue
        thumbs[canon] = str(row.get("thumbnail") or "")
    try:
        return normalize_order_art_styles(stored, thumbnails=thumbs)
    except ValueError:
        return default_order_art_styles()


def _art_style_row(style_id: str) -> dict[str, str]:
    for row in configured_art_styles():
        if row["id"] == style_id:
            return row
    raise ValueError("Choose an art style from the list.")


def _public_art_style(row: dict[str, str]) -> dict[str, str]:
    thumb = row.get("thumbnail") or ""
    return {
        "id": row["id"],
        "name": row["name"],
        "thumbnail_url": f"/api/orders/art-styles/{row['id']}/thumb" if thumb else "",
    }


def pipeline_art_style_choices() -> list[dict[str, str]]:
    from studio.art_style import list_art_styles

    return [{"id": row["id"], "name": row["label"]} for row in list_art_styles()]


def _persist_art_styles(items: list[dict[str, str]]) -> None:
    from studio.settings import save_settings

    save_settings({"order_art_styles": {"items": items}})


def _delete_thumb_file(name: str) -> None:
    try:
        path = _thumb_path(name)
    except LookupError:
        return
    if path.is_file():
        path.unlink()


def save_order_art_styles(raw: Any) -> dict[str, Any]:
    """Replace the buyer catalog. Existing thumbnail files stay attached by style id."""
    current = {row["id"]: row.get("thumbnail") or "" for row in configured_art_styles()}
    items = normalize_order_art_styles(raw, thumbnails=current)
    kept = {row["id"] for row in items}
    for style_id, filename in current.items():
        if style_id not in kept:
            _delete_thumb_file(filename)
    _persist_art_styles(items)
    return public_config()


def add_order_art_style(style_id: str, name: str = "") -> dict[str, Any]:
    from studio.art_style import ART_STYLES, normalize_art_style

    try:
        canon = normalize_art_style(style_id)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    items = [dict(row) for row in configured_art_styles()]
    if any(row["id"] == canon for row in items):
        raise ValueError("That art style is already listed.")
    label = " ".join((name or "").split()) or ART_STYLES[canon]["label"]
    if len(label) > 80:
        raise ValueError("Art style name is too long (80 characters max).")
    items.append({"id": canon, "name": label, "thumbnail": ""})
    _persist_art_styles(items)
    return public_config()


def remove_order_art_style(style_id: str) -> dict[str, Any]:
    from studio.art_style import normalize_art_style

    try:
        canon = normalize_art_style(style_id)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    items = [dict(row) for row in configured_art_styles()]
    kept = [row for row in items if row["id"] != canon]
    if len(kept) == len(items):
        raise LookupError("That art style is not in the catalog.")
    gone = next(row for row in items if row["id"] == canon)
    _delete_thumb_file(str(gone.get("thumbnail") or ""))
    _persist_art_styles(kept)
    return public_config()


def save_order_art_thumbnail(style_id: str, data: bytes) -> dict[str, Any]:
    """Store a buyer-facing thumbnail under user_data/order_art_thumbs/{id}.{ext}."""
    from studio.art_style import normalize_art_style

    blob = bytes(data or b"")
    if not blob:
        raise ValueError("Thumbnail file is empty.")
    if len(blob) > MAX_THUMB_BYTES:
        raise ValueError("Thumbnail must be 4 MB or smaller.")
    try:
        canon = normalize_art_style(style_id)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    ext, _media = _sniff_image(blob)
    items = [dict(row) for row in configured_art_styles()]
    row = next((item for item in items if item["id"] == canon), None)
    if row is None:
        raise ValueError("Add the art style before uploading a thumbnail.")
    filename = f"{canon}{ext}"
    dest = _thumb_path(filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    old = str(row.get("thumbnail") or "")
    dest.write_bytes(blob)
    if old and old != filename:
        _delete_thumb_file(old)
    row["thumbnail"] = filename
    _persist_art_styles(items)
    return public_config()


def art_style_thumbnail_file(style_id: str) -> tuple[Path, str]:
    """Public file for one offered style. The id is a catalog key, never a path."""
    from studio.art_style import normalize_art_style

    try:
        canon = normalize_art_style(style_id)
    except RuntimeError as exc:
        raise LookupError("Thumbnail not found.") from exc
    row = next((item for item in configured_art_styles() if item["id"] == canon), None)
    if not row or not row.get("thumbnail"):
        raise LookupError("Thumbnail not found.")
    path = _thumb_path(str(row["thumbnail"]))
    if path.stem != canon or not path.is_file():
        raise LookupError("Thumbnail not found.")
    return path, _thumb_media(path)


def delivery_line(pkg: dict[str, Any]) -> str:
    videos = int(pkg["videos"])
    days = int(pkg["days"])
    per_day = int(pkg["per_day"])
    each = "1 video per day" if per_day == 1 else f"{per_day} videos per day"
    return f"{videos} videos over {days} days, {each}"


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def order_delivery_pace(order: dict[str, Any]) -> dict[str, int]:
    """Pace for one paid order: stored days/per_day, otherwise the live catalog."""
    videos = int(order.get("video_count") or 0)
    days = _optional_int(order.get("days"))
    per_day = _optional_int(order.get("per_day"))
    if not days or not per_day:
        pkg = package_catalog().get(str(order.get("package_key") or "")) or {}
        if not days:
            days = _optional_int(pkg.get("days"))
        if not per_day:
            per_day = _optional_int(pkg.get("per_day"))
    per_day_n = per_day or 1
    days_n = days or max(1, (max(videos, 1) + per_day_n - 1) // per_day_n)
    return {"video_count": videos, "days": int(days_n), "per_day": int(per_day_n)}


def delivery_slots(video_count: int, per_day: int, *, start: datetime | None = None) -> list[str]:
    """UTC times: per_day videos share a day, then the next calendar day."""
    origin = start or datetime.now(timezone.utc)
    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=timezone.utc)
    origin = origin.astimezone(timezone.utc)
    pace = max(1, int(per_day or 1))
    slots: list[str] = []
    for index in range(max(0, int(video_count))):
        slot = origin + timedelta(days=index // pace)
        slots.append(slot.isoformat())
    return slots


def price_cents(package_key: str, video_length: str) -> int:
    """10-minute videos multiply the package price and round half-up to the nearest dollar."""
    pkg = package_catalog().get(package_key)
    if not pkg:
        raise ValueError("Choose Starter, Growth, or Scale.")
    length = _normalize_length(video_length)
    base = int(pkg["cents"])
    if length == "5":
        return base
    factor = Decimal(str(configured_pricing()["ten_minute_multiplier"]))
    dollars = (Decimal(base) / Decimal(100)) * factor
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
    catalog = package_catalog()
    if raw in catalog:
        return raw
    for key, pkg in catalog.items():
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


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add order-approval columns on databases created before this feature."""
    conn.executescript(_SCHEMA)
    order_cols = _table_columns(conn, "orders")
    additions = {
        "user_id": "TEXT NOT NULL DEFAULT ''",
        "days": "INTEGER",
        "per_day": "INTEGER",
        "approval_notice_id": "INTEGER",
        "generation_approved_at": "TEXT",
        "art_style": "TEXT NOT NULL DEFAULT ''",
        "art_style_name": "TEXT NOT NULL DEFAULT ''",
    }
    added_notice = "approval_notice_id" not in order_cols
    for name, decl in additions.items():
        if name not in order_cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {decl}")
    if added_notice:
        # Orders already paid before approval notices existed should not alert again.
        conn.execute(
            """
            UPDATE orders
            SET approval_notice_id = 0
            WHERE approval_notice_id IS NULL AND status != 'pending_payment'
            """
        )
    video_cols = _table_columns(conn, "videos")
    for name, decl in (("topic_id", "TEXT"), ("job_id", "TEXT")):
        if name not in video_cols:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {decl}")


def init_db() -> None:
    with _LOCK:
        conn = _connect()
        try:
            _migrate(conn)
            conn.commit()
        finally:
            conn.close()


@contextmanager
def _conn():
    with _LOCK:
        conn = _connect()
        try:
            _migrate(conn)
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
        "topic_id": row.get("topic_id") or "",
        "job_id": row.get("job_id") or "",
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
        "user_id": row.get("user_id") or "",
        "days": int(row["days"]) if row.get("days") else None,
        "per_day": int(row["per_day"]) if row.get("per_day") else None,
        "approval_notice_id": row.get("approval_notice_id"),
        "generation_approved_at": row.get("generation_approved_at") or "",
        "art_style": row.get("art_style") or "",
        "art_style_name": row.get("art_style_name") or "",
        "videos": vids,
    }


def _client_video(video: dict[str, Any], email: str) -> dict[str, Any]:
    ready = video["status"] == "ready" and video.get("mp4_attached")
    url = ""
    if ready:
        url = f"/api/orders/download/{video['id']}"
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
        "art_style": order.get("art_style") or "",
        "art_style_name": order.get("art_style_name") or "",
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
    pricing = configured_pricing()
    packages = []
    for key, pkg in pricing["packages"].items():
        packages.append(
            {
                "key": key,
                "name": pkg["name"],
                "videos": pkg["videos"],
                "days": pkg["days"],
                "per_day": pkg["per_day"],
                "timeline": delivery_line(pkg),
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
        "ten_minute_multiplier": pricing["ten_minute_multiplier"],
        "niches": list(NICHES),
        "formats": [{"value": "16:9", "label": "16:9"}, {"value": "9:16", "label": "9:16"}, {"value": "both", "label": "Both"}],
        "lengths": [{"value": "5", "label": "5-minute"}, {"value": "10", "label": "10-minute"}],
        "art_styles": [_public_art_style(row) for row in configured_art_styles()],
        "pipeline_art_styles": pipeline_art_style_choices(),
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
    art_style = _normalize_order_art_style(str(data.get("art_style") or ""))
    art = _art_style_row(art_style)
    pkg = package_catalog()[package_key]
    return {
        "niche": niche,
        "custom_niche": custom,
        "channel_notes": notes,
        "package_key": package_key,
        "package_name": pkg["name"],
        "video_count": int(pkg["videos"]),
        "days": int(pkg["days"]),
        "per_day": int(pkg["per_day"]),
        "video_length": length,
        "format": fmt,
        "art_style": art_style,
        "art_style_name": art["name"],
        "amount_cents": price_cents(package_key, length),
    }


def _normalize_order_art_style(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("Choose an art style.")
    from studio.art_style import normalize_art_style

    try:
        canon = normalize_art_style(raw)
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    if canon not in {row["id"] for row in configured_art_styles()}:
        raise ValueError("Choose an art style from the list.")
    return canon


def _art_style_from_meta(meta: dict[str, Any]) -> tuple[str, str]:
    """Paid webhook metadata. A missing style does not reject money already captured."""
    raw = str(meta.get("art_style") or "").strip()
    if not raw:
        return "", ""
    try:
        style_id = _normalize_order_art_style(raw)
    except ValueError:
        return "", ""
    name = " ".join(str(meta.get("art_style_name") or "").split())
    if not name:
        try:
            name = _art_style_row(style_id)["name"]
        except ValueError:
            name = style_id
    return style_id, name[:80]


def _account_user_id(value: str) -> str:
    cleaned = "".join(ch for ch in (value or "").strip() if ch.isalnum() or ch in ("-", "_"))
    return cleaned[:80]


def _member_id_for_email(email: str) -> str:
    from studio.members import get_user_by_email

    user = get_user_by_email(email) if (email or "").strip() else None
    if not user:
        return ""
    return _account_user_id(str(user.get("id") or ""))


def create_checkout(data: dict[str, Any], *, user_id: str = "") -> dict[str, Any]:
    """Start a one-time Stripe Checkout. Price is computed here, not taken from the client."""
    brief = _validate_brief(data)
    account_id = _account_user_id(user_id)
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
                amount_cents, stripe_session_id, status, reviewed, created_at,
                days, per_day, user_id, art_style, art_style_name
            ) VALUES (?, '', '', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending_payment', 0, ?, ?, ?, ?, ?, ?)
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
                brief["days"],
                brief["per_day"],
                account_id,
                brief["art_style"],
                brief["art_style_name"],
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
    style = brief.get("art_style_name") or brief.get("art_style") or ""
    name = f"{brief['package_name']} — {brief['video_count']} {length} videos ({fmt}, {style})"
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
            "art_style": brief["art_style"],
            "art_style_name": brief["art_style_name"][:200],
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
                "art_style": _obj_get(meta, "art_style") or "",
                "art_style_name": _obj_get(meta, "art_style_name") or "",
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
    notice = _publish_approval_notice(paid)
    with _conn() as conn:
        paid = _load_admin(conn, paid["id"])
    return {
        "ok": True,
        "order_id": paid["id"],
        "video_count": len(paid["videos"]),
        "order": paid,
        "awaiting_approval": notice is not None,
        "notice": notice,
    }


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
    pkg = package_catalog().get(str(row["package_key"] or "")) or {}
    existing_user = _account_user_id(str(row["user_id"] or ""))
    matched = existing_user or _member_id_for_email(snap["email"])
    conn.execute(
        """
        UPDATE orders
        SET name = ?, email = ?, stripe_session_id = ?, status = 'paid', reviewed = 0,
            days = COALESCE(days, ?), per_day = COALESCE(per_day, ?),
            user_id = CASE WHEN user_id IS NULL OR user_id = '' THEN ? ELSE user_id END
        WHERE id = ? AND status = 'pending_payment'
        """,
        (
            snap["name"],
            snap["email"],
            snap["id"],
            pkg.get("days"),
            pkg.get("per_day"),
            matched,
            row["id"],
        ),
    )


def _insert_paid_from_metadata(conn: sqlite3.Connection, snap: dict[str, Any]) -> sqlite3.Row:
    meta = snap["metadata"]
    _check_amount(None, snap)
    package_key = _normalize_package(meta.get("package_key") or "")
    length = _normalize_length(meta.get("video_length") or "5")
    fmt = _normalize_format(meta.get("format") or "16:9")
    art_style, art_name = _art_style_from_meta(meta)
    niche = (meta.get("niche") or "").strip()
    if niche not in NICHES:
        niche = "Custom niche"
    pkg = package_catalog()[package_key]
    order_id = (meta.get("order_id") or "").strip() or _new_id("ord_")
    account_id = _member_id_for_email(snap["email"])
    conn.execute(
        """
        INSERT INTO orders (
            id, name, email, niche, custom_niche, channel_notes,
            package_name, package_key, video_count, video_length, format,
            amount_cents, stripe_session_id, status, reviewed, created_at,
            days, per_day, user_id, art_style, art_style_name
        ) VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, 'paid', 0, ?, ?, ?, ?, ?, ?)
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
            int(pkg["days"]),
            int(pkg["per_day"]),
            account_id,
            art_style,
            art_name,
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


def list_orders_for_user(user: dict[str, Any]) -> dict[str, Any]:
    """Orders for the signed-in member: stored user id, or the same account email."""
    uid = _account_user_id(str((user or {}).get("id") or ""))
    email = str((user or {}).get("email") or "").strip().lower()
    if not uid:
        raise ValueError("Sign in to see your orders.")
    with _conn() as conn:
        if email:
            conn.execute(
                """
                UPDATE orders
                SET user_id = ?
                WHERE (user_id IS NULL OR user_id = '')
                  AND lower(email) = ?
                  AND status != 'pending_payment'
                """,
                (uid, email),
            )
        rows = conn.execute(
            """
            SELECT * FROM orders
            WHERE status != 'pending_payment'
              AND (
                user_id = ?
                OR (? != '' AND lower(email) = ?)
              )
            ORDER BY created_at DESC, id DESC
            """,
            (uid, email, email),
        ).fetchall()
        orders = [
            _client_order(_admin_order(row, _videos_for(conn, row["id"])))
            for row in rows
        ]
    return {"ok": True, "user_id": uid, "orders": orders}


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


def _publish_approval_notice(order: dict[str, Any]) -> dict[str, Any] | None:
    """Tell MCP a paid order is waiting. Does not queue topics. Idempotent per order."""
    if order.get("approval_notice_id") is not None:
        return None
    try:
        from studio.job_notifications import publish_order_awaiting_approval

        facts = _order_generation_facts(order)
        event = publish_order_awaiting_approval(
            order_id=str(order.get("id") or ""),
            package_name=str(order.get("package_name") or ""),
            video_count=int(order.get("video_count") or 0),
            niche=str(order.get("niche") or ""),
            email=str(order.get("email") or ""),
            name=str(order.get("name") or ""),
            format=facts["format"],
            video_length=facts["video_length"],
            format_label=facts["format_label"],
            video_length_label=facts["video_length_label"],
            duration_min=facts["duration_min"],
            art_style=facts["art_style"],
            art_style_name=facts["art_style_name"],
            days=_optional_int(order.get("days")),
            per_day=_optional_int(order.get("per_day")),
        )
    except Exception:
        _log.exception("order approval notice failed for %s", order.get("id"))
        return None
    event_id = _optional_int(event.get("id"))
    if event_id:
        with _conn() as conn:
            conn.execute(
                """
                UPDATE orders
                SET approval_notice_id = ?
                WHERE id = ? AND approval_notice_id IS NULL
                """,
                (event_id, order["id"]),
            )
    if event.get("duplicate"):
        return None
    clean = dict(event)
    clean.pop("duplicate", None)
    return clean


def _member_for_order(order: dict[str, Any]) -> dict[str, Any]:
    """Use the order's studio user, or the member with the same checkout email."""
    from studio.members import get_user_by_email, get_user_by_id

    uid = str(order.get("user_id") or "").strip()
    if uid:
        user = get_user_by_id(uid)
        if user:
            return user
    email = str(order.get("email") or "").strip()
    user = get_user_by_email(email) if email else None
    if not user:
        shown = email or "(no email on the order)"
        raise ValueError(
            f"No studio member matches this order ({order.get('id')}). "
            f"Checkout email {shown} is not a member account. "
            "Generation was not started and no user was created."
        )
    return user


def _order_topic_title(order: dict[str, Any], video: dict[str, Any]) -> str:
    existing = str(video.get("topic") or "").strip()
    if existing:
        return existing[:500]
    niche = niche_label(order) or "Video"
    position = int(video.get("position") or 0)
    return f"{niche} video {position}"[:500]


def _order_topic_angle(order: dict[str, Any]) -> str:
    parts: list[str] = []
    niche = niche_label(order)
    if niche:
        parts.append(f"Niche: {niche}.")
    notes = str(order.get("channel_notes") or "").strip()
    if notes:
        parts.append(notes)
    length = order.get("video_length_label") or length_label(str(order.get("video_length") or "5"))
    fmt = order.get("format_label") or format_label(str(order.get("format") or "16:9"))
    package = order.get("package_name") or "video"
    style = order.get("art_style_name") or order.get("art_style") or ""
    style_bit = f" Art style: {style}." if style else ""
    parts.append(f"{length} {fmt} video for a paid {package} order.{style_bit}")
    return " ".join(parts)[:2000]


def _order_generation_facts(order: dict[str, Any]) -> dict[str, Any]:
    length = str(order.get("video_length") or "")
    try:
        duration = float(length) if length else None
    except ValueError:
        duration = None
    return {
        "art_style": str(order.get("art_style") or ""),
        "art_style_name": str(order.get("art_style_name") or ""),
        "video_length": length,
        "video_length_label": str(order.get("video_length_label") or length_label(length or "5")),
        "duration_min": duration,
        "format": str(order.get("format") or ""),
        "format_label": str(order.get("format_label") or format_label(str(order.get("format") or ""))),
    }


def approve_order_generation(order_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Queue this order's videos on the member account. Explicit approval; safe to call twice."""
    from studio.topics import schedule_order_topic

    oid = (order_id or "").strip()
    order = get_production_order(oid)["order"]
    if order["status"] == "delivered":
        raise ValueError("This order is already delivered.")
    videos = list(order.get("videos") or [])
    if not videos:
        raise ValueError("This order has no videos to generate.")
    facts = _order_generation_facts(order)
    if all(str(video.get("topic_id") or "").strip() for video in videos):
        pace = order_delivery_pace(order)
        return {
            "ok": True,
            "already_approved": True,
            "order_id": oid,
            "user_id": order.get("user_id") or "",
            "per_day": pace["per_day"],
            "days": pace["days"],
            "video_count": len(videos),
            **facts,
            "topics": [
                {
                    "video_id": video["id"],
                    "topic_id": video.get("topic_id") or "",
                    "job_id": video.get("job_id") or "",
                    "position": video.get("position"),
                }
                for video in videos
            ],
        }
    try:
        user = _member_for_order(order)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "order_id": oid, "topics": []}
    user_id = str(user.get("id") or "").strip()
    if not user_id:
        return {
            "ok": False,
            "error": "Matched studio member has no id. Generation was not started.",
            "order_id": oid,
            "topics": [],
        }
    pace = order_delivery_pace(order)
    slots = delivery_slots(len(videos), pace["per_day"], start=now)
    queued: list[dict[str, Any]] = []
    for index, video in enumerate(videos):
        existing_topic = str(video.get("topic_id") or "").strip()
        if existing_topic:
            queued.append(
                {
                    "video_id": video["id"],
                    "topic_id": existing_topic,
                    "job_id": video.get("job_id") or "",
                    "position": video.get("position"),
                    "scheduled_at": slots[index],
                    "already_queued": True,
                }
            )
            continue
        title = _order_topic_title(order, video)
        result = schedule_order_topic(
            owner_id=user_id,
            title=title,
            angle=_order_topic_angle(order),
            duration_min=float(order.get("video_length") or 5),
            scheduled_at=slots[index],
            order_id=oid,
            order_video_id=str(video["id"]),
            aspect=str(order.get("format") or "16:9"),
            art_style=str(order.get("art_style") or ""),
        )
        topic = result.get("topic") or {}
        topic_id = str(topic.get("id") or "")
        job_id = str(result.get("job_id") or topic.get("job_id") or "")
        if not topic_id:
            raise RuntimeError(f"Could not queue a topic for video {video['id']}.")
        with _conn() as conn:
            conn.execute(
                """
                UPDATE videos
                SET topic = ?, topic_id = ?, job_id = ?
                WHERE id = ? AND (topic_id IS NULL OR topic_id = '')
                """,
                (title, topic_id, job_id or None, video["id"]),
            )
        queued.append(
            {
                "video_id": video["id"],
                "topic_id": topic_id,
                "job_id": job_id,
                "position": video.get("position"),
                "scheduled_at": result.get("scheduled_at") or slots[index],
                "already_queued": bool(result.get("already_queued")),
            }
        )
    approved_at = _now()
    with _conn() as conn:
        conn.execute(
            """
            UPDATE orders
            SET user_id = ?,
                generation_approved_at = COALESCE(NULLIF(generation_approved_at, ''), ?),
                status = CASE WHEN status = 'paid' THEN 'queued' ELSE status END
            WHERE id = ?
            """,
            (user_id, approved_at, oid),
        )
        updated = _load_admin(conn, oid)
    return {
        "ok": True,
        "already_approved": False,
        "order_id": oid,
        "user_id": user_id,
        "per_day": pace["per_day"],
        "days": pace["days"],
        "video_count": len(videos),
        **facts,
        "topics": queued,
        "order": updated,
    }


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
    my_orders = f"{base}/my-orders"
    style = order.get("art_style_name") or order.get("art_style") or ""
    style_bit = f" · {style}" if style else ""
    lines = [
        f"Hi {order.get('name') or 'there'},",
        "",
        f"Your {order.get('package_name')} order is ready.",
        f"Order: {order.get('id')}",
        f"Niche: {order.get('niche')}",
        f"Package: {order.get('package_name')} · {order.get('video_count')} × {order.get('video_length_label')} · {order.get('format_label')}{style_bit}",
        f"Amount paid: ${int(order.get('amount_cents') or 0) / 100:.2f}",
        "",
        f"Sign in and open My Orders to download: {my_orders}",
        "",
        "Downloads (sign in with the account on this order):",
    ]
    for video in order.get("videos") or []:
        title = (video.get("topic") or "").strip() or f"Video {video.get('position')}"
        url = f"{base}/api/orders/download/{video['id']}"
        lines.append(f"- {title}: {url}")
    lines.append("")
    text = "\n".join(lines)
    if not email_enabled():
        from studio.mailjet import log_if_unconfigured

        log_if_unconfigured()
        return (
            False,
            "in_app",
            "Delivered in the app. The client can download videos from My Orders after they sign in.",
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


def resolve_download(
    video_id: str,
    user: dict[str, Any] | None = None,
    *,
    is_admin: bool = False,
) -> tuple[Path, str]:
    vid = (video_id or "").strip()
    with _conn() as conn:
        video = _video_row(conn, vid)
        if not video:
            raise LookupError("Not found.")
        order = _order_row(conn, video["order_id"])
        if not order or order["status"] == "pending_payment":
            raise LookupError("Not found.")
        if not is_admin and not _user_owns_order(order, user):
            raise LookupError("Not found.")
        if video["status"] != "ready":
            raise LookupError("Not found." if not is_admin else "This video is not ready for download.")
        path = _video_file(video)
        if path is None:
            raise LookupError("Not found." if not is_admin else "MP4 file is missing on disk.")
        topic = (video["topic"] or "").strip()
        stem = _safe_filename(topic) if topic else f"video-{video['position']}"
    return path, f"{stem}.mp4"


def _user_owns_order(order: sqlite3.Row | dict[str, Any], user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    data = dict(order)
    uid = _account_user_id(str(user.get("id") or ""))
    email = str(user.get("email") or "").strip().lower()
    order_uid = _account_user_id(str(data.get("user_id") or ""))
    order_email = str(data.get("email") or "").strip().lower()
    if uid and order_uid and uid == order_uid:
        return True
    return bool(email and order_email and email == order_email)


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
