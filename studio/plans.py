"""Plan tiers, quotas, and retention policy for SaaS billing (Phase 4)."""

from __future__ import annotations

from typing import Any

# Seed tiers — owner-configurable via settings.plan_tiers override later.
# Quotas are per billing period (calendar month UTC) unless noted.
DEFAULT_PLAN_TIERS: dict[str, dict[str, Any]] = {
    "admin": {
        "label": "Admin",
        "unlimited": True,
        "price_cents": 0,
        "images_generated": None,
        "render_minutes": None,
        "tts_seconds": None,
        "narration_uploads": None,
        "youtube_uploads": None,
        "fal_spend_cents": None,
        "storage_bytes": None,
        "retention_days": 365,
    },
    "starter": {
        "label": "Starter",
        "unlimited": False,
        "price_cents": 2900,
        "images_generated": 80,
        "render_minutes": 90,
        "tts_seconds": 3600,
        "narration_uploads": 30,
        "youtube_uploads": 15,
        "fal_spend_cents": 500,  # ~$5
        "storage_bytes": 5 * 1024**3,
        "retention_days": 3,
    },
    "creator": {
        "label": "Creator",
        "unlimited": False,
        "price_cents": 7900,
        "images_generated": 300,
        "render_minutes": 360,
        "tts_seconds": 14_400,
        "narration_uploads": 100,
        "youtube_uploads": 50,
        "fal_spend_cents": 2500,
        "storage_bytes": 25 * 1024**3,
        "retention_days": 14,
    },
    "studio": {
        "label": "Studio",
        "unlimited": False,
        "price_cents": 19900,
        "images_generated": 1000,
        "render_minutes": 1200,
        "tts_seconds": 43_200,
        "narration_uploads": 300,
        "youtube_uploads": 150,
        "fal_spend_cents": 8000,
        "storage_bytes": 100 * 1024**3,
        "retention_days": 30,
    },
}

METRICS = (
    "images_generated",
    "render_minutes",
    "tts_seconds",
    "narration_uploads",
    "youtube_uploads",
    "fal_spend_cents",
    "storage_bytes",
)


def plan_tiers() -> dict[str, dict[str, Any]]:
    try:
        from studio.settings import load_settings

        custom = load_settings().get("plan_tiers")
        if isinstance(custom, dict) and custom:
            merged = {k: dict(v) for k, v in DEFAULT_PLAN_TIERS.items()}
            for key, val in custom.items():
                if isinstance(val, dict):
                    merged[str(key)] = {**(merged.get(str(key)) or {}), **val}
            return merged
    except Exception:
        pass
    return {k: dict(v) for k, v in DEFAULT_PLAN_TIERS.items()}


def normalize_plan_tier(value: str | None, *, is_admin: bool = False) -> str:
    if is_admin:
        return "admin"
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    tiers = plan_tiers()
    if raw in tiers:
        return raw
    if raw in ("pro", "professional"):
        return "creator"
    return "starter"


def plan_for_user(user: dict[str, Any] | None) -> dict[str, Any]:
    user = user or {}
    is_admin = (user.get("role") or "") == "admin"
    tier = normalize_plan_tier(user.get("plan_tier"), is_admin=is_admin)
    if is_admin:
        tier = "admin"
    cfg = dict(plan_tiers().get(tier) or plan_tiers()["starter"])
    cfg["tier"] = tier
    return cfg


def price_id_to_tier(price_id: str | None) -> str | None:
    """Map a Stripe price id to a plan tier via settings.stripe_price_tiers."""
    pid = (price_id or "").strip()
    if not pid:
        return None
    try:
        from studio.settings import load_settings

        mapping = load_settings().get("stripe_price_tiers")
        if isinstance(mapping, dict):
            for tier, val in mapping.items():
                if str(val or "").strip() == pid:
                    return normalize_plan_tier(str(tier))
            # inverted map {price_id: tier}
            if pid in mapping:
                return normalize_plan_tier(str(mapping[pid]))
        # Single-price installs: paying members land on creator by default
        default_price = str(load_settings().get("stripe_price_id") or "").strip()
        if default_price and pid == default_price:
            return normalize_plan_tier(
                load_settings().get("default_paid_plan_tier") or "creator"
            )
    except Exception:
        pass
    return None


def retention_days_for_user(user: dict[str, Any] | None) -> int:
    cfg = plan_for_user(user)
    try:
        return max(1, int(cfg.get("retention_days") or 14))
    except (TypeError, ValueError):
        return 14
