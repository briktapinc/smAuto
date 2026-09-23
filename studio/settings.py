from __future__ import annotations

import json
import os
from typing import Any

from studio.paths import SETTINGS_PATH, YOUTUBE_TOKEN_PATH, ensure_dirs

# Secrets never returned raw from public_settings / MCP get_studio_settings.
SECRET_KEYS = frozenset({
    "openai_api_key",
    "elevenlabs_api_key",
    "fal_key",
    "youtube_client_secret",
    "ngrok_basic_auth_password",
    "ngrok_tunnel_gate",
    "mcp_pin_hash",
    "jwt_secret",
    "stripe_secret_key",
    "stripe_webhook_secret",
    "smtp_password",
})
REDACTION_PLACEHOLDER = "********"
REDACTION_TOKENS = frozenset({
    REDACTION_PLACEHOLDER,
    "****",
    "*",
    "<redacted>",
    "[redacted]",
    "null",
    "none",
    "undefined",
})


def is_placeholder_secret(value: Any) -> bool:
    """True for empty / null / UI redaction placeholders that must not overwrite secrets."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if not raw:
        return True
    if raw.lower() in REDACTION_TOKENS:
        return True
    if len(raw) >= 4 and set(raw) <= {"*"}:
        return True
    return False


def scrub_secret_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """Drop omitted/empty/placeholder secret fields so existing keys stay intact."""
    out: dict[str, Any] = {}
    for key, value in updates.items():
        if key in SECRET_KEYS or key.endswith("_api_key") or key.endswith("_secret") or key.endswith("_password"):
            if is_placeholder_secret(value):
                continue
        out[key] = value
    return out

IMAGE_PROVIDERS = ("flux", "chatgpt", "comfyui")
IMAGE_PROVIDER_LABELS = {
    "flux": "Flux (fal-ai/flux-2)",
    "chatgpt": "ChatGPT (native in-chat)",
    "comfyui": "ComfyUI (local API)",
}
COVER_PROVIDERS = ("", "flux", "chatgpt", "comfyui", "manual")
COVER_PROVIDER_LABELS = {
    "": "Same as image_provider",
    "flux": "Flux (fal)",
    "chatgpt": "ChatGPT (native / agent upload)",
    "comfyui": "ComfyUI (local)",
    "manual": "Manual / agent upload only",
}
COMFYUI_DEFAULT_URL = "http://127.0.0.1:8188"
STUDIO_IMAGE_PROVIDERS = frozenset({"flux", "comfyui"})
TEXT_PROVIDERS = ("openai", "chatgpt", "claude", "lmstudio")
TEXT_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "lmstudio": "LM Studio",
}
LMSTUDIO_DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
VIDEO_LAYOUTS = ("cover", "billboard")
CHARACTER_SIZES = ("large", "medium", "small")
CHARACTER_SIZE_SCALES = {
    "large": 1.0,
    "medium": 0.5,
    "small": 1.0 / 3.0,
}
YOUTUBE_PRIVACY = ("private", "unlisted", "public")
TTS_PROVIDERS = ("openai", "elevenlabs", "local")
# Official fal FLUX.2 [dev] endpoint (not Pro). Verified via fal catalog / OpenAPI about.
FLUX_MODEL = "fal-ai/flux-2"
BILLBOARD_SIZE = (1920, 1080)

DEFAULTS = {
    "openai_api_key": "",
    "openai_model": "gpt-4o",
    "openai_tts_model": "gpt-4o-mini-tts",
    "elevenlabs_api_key": "",
    "elevenlabs_model": "eleven_multilingual_v2",
    "fal_key": "",
    "gentle_url": "http://127.0.0.1:8766",
    "tts_provider": "openai",
    "voice_provider": "openai",
    "openai_voice": "coral",
    "elevenlabs_voice_id": "",
    "local_voice": "default",
    "text_provider": "openai",
    "lmstudio_base_url": LMSTUDIO_DEFAULT_BASE_URL,
    "lmstudio_model": "",
    "image_provider": "flux",
    "cover_provider": "",
    "script_draft_provider": "",
    "comfyui_url": COMFYUI_DEFAULT_URL,
    "comfyui_workflow_filename": "",
    "video_layout": "cover",
    "character_size": "large",
    "stickman_head_color": "#FAE02E",
    "music_volume_pct": 15,
    "host": "127.0.0.1",
    "port": 7878,
    "default_aspect": "16:9",
    "youtube_client_id": "",
    "youtube_client_secret": "",
    "youtube_channel_id": "",
    "youtube_channel_title": "",
    "youtube_auto_upload": False,
    "youtube_privacy": "unlisted",
    "auto_scheduler": True,
    "hands_off": False,
    "hands_off_interval_hours": 0.0,
    "hands_off_min_queue": 5,
    "ngrok_url": "",
    "ngrok_local_port": 7878,
    "ngrok_autostart": False,
    "ngrok_basic_auth_user": "bubblepod",
    "ngrok_basic_auth_password": "",
    "ngrok_tunnel_gate": "",
    "mcp_pin_hash": "",
    # Security / ops
    "rate_limit_enabled": True,
    "rate_limit_login": 8,
    "rate_limit_login_window_sec": 900,
    "rate_limit_api": 180,
    "rate_limit_api_window_sec": 60,
    "rate_limit_static": 600,
    "rate_limit_static_window_sec": 60,
    "require_spend_confirm": True,
    "max_openai_calls_per_day": 0,
    "max_flux_images_per_day": 0,
    # Stripe membership (single equal-access plan)
    "stripe_secret_key": "",
    "stripe_publishable_key": "",
    "stripe_webhook_secret": "",
    "stripe_product_id": "",
    "stripe_price_id": "",
    "stripe_price_amount_cents": 2900,
    "stripe_price_currency": "usd",
    "stripe_price_interval": "month",
    "public_base_url": "",
    "membership_required": True,
    # Email / SMTP (Admin → Email)
    "email_enabled": False,
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_use_tls": True,
    "smtp_use_ssl": False,
    "email_from": "",
    "email_from_name": "Stickman Automation",
    "email_reply_to": "",
    "email_templates": {},
}


def normalize_tts_provider(value: str | None, default: str = "openai") -> str:
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "")
    if raw in ("chatgpt", "openai", "openai_tts", "gpt", "gpt4o", "gpt-4o-mini-tts"):
        return "openai"
    if raw in ("elevenlabs", "eleven", "11labs", "xi"):
        return "elevenlabs"
    if raw in ("local", "resemble", "chatterbox", "piper", "resemble_chatterbox", "resembleai"):
        return "local"
    if not raw:
        return default if default in TTS_PROVIDERS else "openai"
    raise RuntimeError(
        f"Unknown TTS provider: {value}. Use openai, elevenlabs, or local "
        "(aliases: resemble, chatterbox)."
    )


def default_voice_for_provider(provider: str, settings: dict | None = None) -> str:
    data = settings or {}
    if provider == "elevenlabs":
        return (data.get("elevenlabs_voice_id") or "").strip()
    if provider == "local":
        return (data.get("local_voice") or "default").strip() or "default"
    return (data.get("openai_voice") or "coral").strip() or "coral"


def normalize_text_provider(value: str | None, default: str = "openai") -> str:
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "")
    aliases = {
        "openai": "openai",
        "api": "openai",
        "openaiapi": "openai",
        "billed": "openai",
        "gptapi": "openai",
        "chatgpt": "chatgpt",
        "chatgptnative": "chatgpt",
        "chatgptdesktop": "chatgpt",
        "codex": "chatgpt",
        "gpt": "chatgpt",
        "claude": "claude",
        "claudedesktop": "claude",
        "claudecode": "claude",
        "anthropic": "claude",
        "clause": "claude",
        "lmstudio": "lmstudio",
        "lm_studio": "lmstudio",
        "lms": "lmstudio",
        "localllm": "lmstudio",
        "localstudio": "lmstudio",
        "openaicompatible": "lmstudio",
    }
    if raw in aliases:
        return aliases[raw]
    if not raw:
        return default if default in TEXT_PROVIDERS else "openai"
    raise RuntimeError(
        f"Unknown text provider: {value}. Use openai (billed API), chatgpt, claude (Desktop MCP), or lmstudio (local)."
    )


def current_text_provider() -> str:
    try:
        return normalize_text_provider(load_settings().get("text_provider"))
    except RuntimeError:
        return "openai"


def is_native_text_provider(value: str | None = None) -> bool:
    try:
        provider = normalize_text_provider(value) if value not in (None, "") else current_text_provider()
    except RuntimeError:
        provider = current_text_provider()
    return provider in ("chatgpt", "claude")


def is_api_text_provider(value: str | None = None) -> bool:
    try:
        provider = normalize_text_provider(value) if value not in (None, "") else current_text_provider()
    except RuntimeError:
        provider = current_text_provider()
    return provider in ("openai", "lmstudio")


def normalize_lmstudio_base_url(value: str | None, default: str = LMSTUDIO_DEFAULT_BASE_URL) -> str:
    raw = (value or "").strip() or default
    raw = raw.rstrip("/")
    if raw.endswith("/v1"):
        return raw
    return raw + "/v1"


def text_provider_label(value: str | None = None) -> str:
    try:
        provider = normalize_text_provider(value) if value not in (None, "") else current_text_provider()
    except RuntimeError:
        provider = current_text_provider()
    return TEXT_PROVIDER_LABELS.get(provider, provider)


def normalize_image_provider(value: str | None, default: str = "flux") -> str:
    raw = (value or "").strip().lower()
    compact = raw.replace("-", "_").replace(" ", "").replace("/", "")
    if raw in ("chatgpt", "openai", "native", "chat") or compact in ("chatgpt", "openai", "native", "chat"):
        return "chatgpt"
    if raw in (
        "flux",
        "fal",
        "flux-2",
        "flux-2-dev",
        "flux-2-pro",
        "fal-ai/flux-2",
        "fal-ai/flux-2-pro",
    ) or compact in (
        "flux",
        "fal",
        "flux2",
        "flux2dev",
        "flux_2",
        "flux_2_dev",
        "flux2pro",
        "flux_2_pro",
        "falaiflux2",
        "falaiflux2pro",
    ):
        return "flux"
    if raw in ("comfyui", "comfy", "comfy ui") or compact in ("comfyui", "comfy", "comfy_ui", "localcomfy"):
        return "comfyui"
    if not raw:
        return default if default in IMAGE_PROVIDERS else "flux"
    raise RuntimeError(f"Unknown image provider: {value}. Use 'flux', 'chatgpt', or 'comfyui'.")


def normalize_cover_provider(value: str | None, default: str = "") -> str:
    """Per-job/global cover backend. Empty string = follow image_provider."""
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "")
    if raw in ("", "inherit", "default", "same", "auto"):
        return ""
    if raw in ("manual", "agent", "upload", "none"):
        return "manual"
    if raw in ("chatgpt", "openai", "native", "chat"):
        return "chatgpt"
    if raw in ("comfyui", "comfy"):
        return "comfyui"
    if raw in ("flux", "fal", "flux2", "flux_2"):
        return "flux"
    raise RuntimeError(
        f"Unknown cover_provider: {value}. Use '', 'manual', 'chatgpt', 'comfyui', or 'flux'."
    )


def resolve_cover_provider(image_provider: str | None = None, cover_provider: str | None = None) -> str:
    """Effective cover backend after applying override."""
    override = normalize_cover_provider(cover_provider)
    if override:
        return override
    return normalize_image_provider(image_provider)


def normalize_script_draft_provider(value: str | None, default: str = "") -> str:
    """Optional auto-draft backend when text_provider is chatgpt/claude. Empty = off."""
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "")
    if raw in ("", "off", "none", "disabled", "manual"):
        return ""
    if raw in ("openai", "api", "gpt"):
        return "openai"
    if raw in ("lmstudio", "local", "lm_studio"):
        return "lmstudio"
    raise RuntimeError(
        f"Unknown script_draft_provider: {value}. Use '', 'openai', or 'lmstudio'."
    )


def normalize_comfyui_url(value: str | None, default: str = COMFYUI_DEFAULT_URL) -> str:
    raw = (value or "").strip() or default
    return raw.rstrip("/")


def is_studio_image_provider(value: str | None = None) -> bool:
    """True when Studio generates PNGs itself (Flux or ComfyUI), not ChatGPT native."""
    try:
        provider = normalize_image_provider(value) if value not in (None, "") else normalize_image_provider(
            load_settings().get("image_provider")
        )
    except RuntimeError:
        return False
    return provider in STUDIO_IMAGE_PROVIDERS


def uses_short_image_prompt(value: str | None = None) -> bool:
    """Flux and ComfyUI get the short subject prompt — never the ChatGPT playbook."""
    return is_studio_image_provider(value)


def image_provider_label(value: str | None = None) -> str:
    try:
        provider = normalize_image_provider(value) if value not in (None, "") else normalize_image_provider(
            load_settings().get("image_provider")
        )
    except RuntimeError:
        provider = "flux"
    return IMAGE_PROVIDER_LABELS.get(provider, provider)


def normalize_music_volume_pct(value: Any, default: int = 15) -> int:
    if value is None or value == "":
        return default
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, n))


def normalize_default_aspect(value: str | None, default: str = "16:9") -> str:
    raw = (value or "").strip().lower().replace(" ", "")
    compact = raw.replace("x", ":")
    allowed = ("16:9", "9:16", "both")
    if raw in {"both", "all"} or compact in {"both", "16:9+9:16", "16:9,9:16"}:
        return "both"
    if compact in {"9:16", "portrait", "vertical", "shorts", "tiktok", "reel"}:
        return "9:16"
    if compact in {"16:9", "landscape", "wide"}:
        return "16:9"
    if not raw:
        return default if default in allowed else "16:9"
    return default if default in allowed else "16:9"


def normalize_youtube_privacy(value: str | None, default: str = "unlisted") -> str:
    raw = (value or "").strip().lower()
    if raw in YOUTUBE_PRIVACY:
        return raw
    if not raw:
        return default if default in YOUTUBE_PRIVACY else "unlisted"
    raise RuntimeError(f"Unknown YouTube privacy: {value}. Use private, unlisted, or public.")


def normalize_youtube_auto_upload(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def normalize_auto_scheduler(value: Any, default: bool = True) -> bool:
    return normalize_youtube_auto_upload(value, default=default)


def normalize_hands_off(value: Any, default: bool = False) -> bool:
    return normalize_youtube_auto_upload(value, default=default)


def normalize_hands_off_interval_hours(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return float(default)
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return float(default)
    if hours < 0:
        return 0.0
    return min(24 * 14, hours)


def normalize_hands_off_min_queue(value: Any, default: int = 5) -> int:
    if value is None or value == "":
        return int(default)
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)
    return max(1, min(20, n))


def normalize_bool(value: Any, default: bool = False) -> bool:
    return normalize_youtube_auto_upload(value, default=default)


def normalize_rate_limit_int(value: Any, default: int, *, minimum: int = 1, maximum: int = 100_000) -> int:
    if value is None or value == "":
        return int(default)
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)
    return max(minimum, min(maximum, n))


def normalize_daily_cap(value: Any, default: int = 0) -> int:
    """0 = unlimited."""
    if value is None or value == "":
        return int(default)
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)
    return max(0, min(100_000, n))


def hands_off_enabled() -> bool:
    try:
        return normalize_hands_off(load_settings().get("hands_off"))
    except Exception:
        return False


def normalize_video_layout(value: str | None, default: str = "cover") -> str:
    raw = (value or "").strip().lower().replace("-", "").replace("_", " ")
    raw = " ".join(raw.split())
    if raw in ("billboard", "billboards", "framed", "inset", "panel", "original"):
        return "billboard"
    if raw in ("cover", "fullbleed", "full bleed", "full", "bleed"):
        return "cover"
    if not raw:
        return default if default in VIDEO_LAYOUTS else "cover"
    raise RuntimeError(f"Unknown video layout: {value}. Use 'cover' or 'billboard'.")


def normalize_character_size(value: str | None, default: str = "large") -> str:
    raw = (value or "").strip().lower().replace("-", " ").replace("_", " ")
    raw = " ".join(raw.split())
    if raw in ("large", "lg", "full", "big", "current"):
        return "large"
    if raw in ("medium", "med", "mid", "half", "m"):
        return "medium"
    if raw in ("small", "sm", "tiny", "third", "s"):
        return "small"
    if not raw:
        return default if default in CHARACTER_SIZES else "large"
    raise RuntimeError(f"Unknown character size: {value}. Use 'large', 'medium', or 'small'.")


def character_size_scale(value: str | None, default: str = "large") -> float:
    return CHARACTER_SIZE_SCALES[normalize_character_size(value, default=default)]


def _env_overrides() -> dict[str, Any]:
    mapping = {
        "openai_api_key": "OPENAI_API_KEY",
        "elevenlabs_api_key": "ELEVENLABS_API_KEY",
        "openai_model": "OPENAI_MODEL",
        "gentle_url": "GENTLE_URL",
        "youtube_client_id": "YOUTUBE_CLIENT_ID",
        "youtube_client_secret": "YOUTUBE_CLIENT_SECRET",
        "stripe_secret_key": "STRIPE_SECRET_KEY",
        "stripe_publishable_key": "STRIPE_PUBLISHABLE_KEY",
        "stripe_webhook_secret": "STRIPE_WEBHOOK_SECRET",
        "stripe_price_id": "STRIPE_PRICE_ID",
        "public_base_url": "BUBBLEPOD_PUBLIC_BASE_URL",
        "smtp_host": "SMTP_HOST",
        "smtp_user": "SMTP_USER",
        "smtp_password": "SMTP_PASSWORD",
        "email_from": "EMAIL_FROM",
        "email_from_name": "EMAIL_FROM_NAME",
        "email_reply_to": "EMAIL_REPLY_TO",
    }
    out = {}
    for key, env in mapping.items():
        val = os.environ.get(env, "").strip()
        if val:
            out[key] = val
    fal = os.environ.get("FAL_KEY", "").strip() or os.environ.get("FAL_API_KEY", "").strip()
    if fal:
        out["fal_key"] = fal
    if os.environ.get("EMAIL_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        out["email_enabled"] = True
    port = os.environ.get("SMTP_PORT", "").strip()
    if port.isdigit():
        out["smtp_port"] = int(port)
    return out


def load_settings() -> dict[str, Any]:
    ensure_dirs()
    data = dict(DEFAULTS)
    if SETTINGS_PATH.is_file():
        try:
            data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    data.update(_env_overrides())
    data["image_provider"] = normalize_image_provider(data.get("image_provider"))
    try:
        data["cover_provider"] = normalize_cover_provider(data.get("cover_provider"))
    except RuntimeError:
        data["cover_provider"] = ""
    try:
        data["script_draft_provider"] = normalize_script_draft_provider(
            data.get("script_draft_provider")
        )
    except RuntimeError:
        data["script_draft_provider"] = ""
    data["comfyui_url"] = normalize_comfyui_url(data.get("comfyui_url"))
    try:
        data["text_provider"] = normalize_text_provider(
            data.get("text_provider") or data.get("script_provider")
        )
    except RuntimeError:
        data["text_provider"] = "openai"
    data.pop("script_provider", None)
    data["lmstudio_base_url"] = normalize_lmstudio_base_url(data.get("lmstudio_base_url"))
    data["lmstudio_model"] = str(data.get("lmstudio_model") or "").strip()
    try:
        tts = normalize_tts_provider(data.get("tts_provider") or data.get("voice_provider"))
    except RuntimeError:
        tts = "openai"
    data["tts_provider"] = tts
    data["voice_provider"] = tts
    data["video_layout"] = normalize_video_layout(data.get("video_layout"))
    try:
        data["character_size"] = normalize_character_size(data.get("character_size"))
    except RuntimeError:
        data["character_size"] = "large"
    try:
        from studio.pose_colors import normalize_hex_color

        data["stickman_head_color"] = normalize_hex_color(data.get("stickman_head_color"))
    except Exception:
        data["stickman_head_color"] = "#FAE02E"
    data["music_volume_pct"] = normalize_music_volume_pct(data.get("music_volume_pct"))
    data["default_aspect"] = normalize_default_aspect(data.get("default_aspect"))
    data["youtube_auto_upload"] = normalize_youtube_auto_upload(data.get("youtube_auto_upload"))
    data["auto_scheduler"] = normalize_auto_scheduler(data.get("auto_scheduler", True))
    data["hands_off"] = normalize_hands_off(data.get("hands_off"))
    data["hands_off_interval_hours"] = normalize_hands_off_interval_hours(data.get("hands_off_interval_hours"))
    data["hands_off_min_queue"] = normalize_hands_off_min_queue(data.get("hands_off_min_queue"))
    try:
        data["youtube_privacy"] = normalize_youtube_privacy(data.get("youtube_privacy"))
    except RuntimeError:
        data["youtube_privacy"] = "unlisted"
    from studio.ngrok_tunnel import (
        normalize_autostart,
        normalize_local_port,
        normalize_public_url,
    )

    data["port"] = normalize_local_port(data.get("port"), 7878)
    data["host"] = str(data.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    data["ngrok_url"] = normalize_public_url(data.get("ngrok_url"))
    data["ngrok_local_port"] = normalize_local_port(
        data.get("ngrok_local_port"),
        default=normalize_local_port(data.get("port"), 7878),
    )
    data["ngrok_autostart"] = normalize_autostart(data.get("ngrok_autostart"))
    data["ngrok_basic_auth_user"] = str(data.get("ngrok_basic_auth_user") or "bubblepod").strip() or "bubblepod"
    data["ngrok_basic_auth_password"] = str(data.get("ngrok_basic_auth_password") or "")
    data["mcp_pin_hash"] = str(data.get("mcp_pin_hash") or "")
    data["rate_limit_enabled"] = normalize_bool(data.get("rate_limit_enabled"), True)
    data["rate_limit_login"] = normalize_rate_limit_int(data.get("rate_limit_login"), 8, minimum=1, maximum=100)
    data["rate_limit_login_window_sec"] = normalize_rate_limit_int(
        data.get("rate_limit_login_window_sec"), 900, minimum=60, maximum=86400
    )
    data["rate_limit_api"] = normalize_rate_limit_int(data.get("rate_limit_api"), 180, minimum=30, maximum=10000)
    data["rate_limit_api_window_sec"] = normalize_rate_limit_int(
        data.get("rate_limit_api_window_sec"), 60, minimum=10, maximum=3600
    )
    data["rate_limit_static"] = normalize_rate_limit_int(data.get("rate_limit_static"), 600, minimum=60, maximum=50000)
    data["rate_limit_static_window_sec"] = normalize_rate_limit_int(
        data.get("rate_limit_static_window_sec"), 60, minimum=10, maximum=3600
    )
    data["require_spend_confirm"] = normalize_bool(data.get("require_spend_confirm"), True)
    data["max_openai_calls_per_day"] = normalize_daily_cap(data.get("max_openai_calls_per_day"), 0)
    data["max_flux_images_per_day"] = normalize_daily_cap(data.get("max_flux_images_per_day"), 0)
    return data


def save_settings(updates: dict[str, Any]) -> dict[str, Any]:
    data = load_settings()
    incoming = scrub_secret_updates(dict(updates))
    if "text_provider" not in incoming and "script_provider" in incoming:
        incoming["text_provider"] = incoming["script_provider"]
    # mcp_pin plaintext → hash only (never store raw PIN)
    if "mcp_pin" in updates and not is_placeholder_secret(updates.get("mcp_pin")):
        from studio.auth import hash_password

        pin = str(updates.get("mcp_pin") or "").strip()
        if len(pin) < 6:
            raise RuntimeError("MCP PIN must be at least 6 characters.")
        incoming["mcp_pin_hash"] = hash_password(pin)
    incoming.pop("mcp_pin", None)
    for key, value in incoming.items():
        if key == "script_provider":
            continue
        if key in DEFAULTS:
            if key in SECRET_KEYS and is_placeholder_secret(value):
                continue
            if key == "image_provider":
                data[key] = normalize_image_provider(value)
            elif key == "cover_provider":
                data[key] = normalize_cover_provider(value)
            elif key == "script_draft_provider":
                data[key] = normalize_script_draft_provider(value)
            elif key == "comfyui_url":
                data[key] = normalize_comfyui_url(value)
            elif key == "text_provider":
                data[key] = normalize_text_provider(value)
            elif key == "lmstudio_base_url":
                data[key] = normalize_lmstudio_base_url(value)
            elif key == "lmstudio_model":
                data[key] = str(value or "").strip()
            elif key in ("tts_provider", "voice_provider"):
                canon = normalize_tts_provider(value)
                data["tts_provider"] = canon
                data["voice_provider"] = canon
            elif key == "video_layout":
                data[key] = normalize_video_layout(value)
            elif key == "character_size":
                data[key] = normalize_character_size(value)
            elif key == "stickman_head_color":
                from studio.pose_colors import normalize_hex_color

                data[key] = normalize_hex_color(value)
            elif key == "music_volume_pct":
                data[key] = normalize_music_volume_pct(value)
            elif key == "default_aspect":
                data[key] = normalize_default_aspect(value)
            elif key == "youtube_privacy":
                data[key] = normalize_youtube_privacy(value)
            elif key == "youtube_auto_upload":
                data[key] = normalize_youtube_auto_upload(value)
            elif key == "auto_scheduler":
                data[key] = normalize_auto_scheduler(value)
            elif key == "hands_off":
                data[key] = normalize_hands_off(value)
            elif key == "hands_off_interval_hours":
                data[key] = normalize_hands_off_interval_hours(value)
            elif key == "hands_off_min_queue":
                data[key] = normalize_hands_off_min_queue(value)
            elif key == "ngrok_url":
                from studio.ngrok_tunnel import normalize_public_url

                data[key] = normalize_public_url(value)
            elif key == "port":
                from studio.ngrok_tunnel import normalize_local_port

                data[key] = normalize_local_port(value, 7878)
            elif key == "ngrok_local_port":
                from studio.ngrok_tunnel import normalize_local_port

                data[key] = normalize_local_port(value)
            elif key == "ngrok_autostart":
                from studio.ngrok_tunnel import normalize_autostart

                data[key] = normalize_autostart(value)
            elif key == "ngrok_basic_auth_user":
                data[key] = str(value or "").strip() or "bubblepod"
            elif key == "mcp_pin_hash":
                raw = str(value or "").strip()
                if raw:
                    data[key] = raw
            elif key == "rate_limit_enabled":
                data[key] = normalize_bool(value, True)
            elif key == "rate_limit_login":
                data[key] = normalize_rate_limit_int(value, 8, minimum=1, maximum=100)
            elif key == "rate_limit_login_window_sec":
                data[key] = normalize_rate_limit_int(value, 900, minimum=60, maximum=86400)
            elif key == "rate_limit_api":
                data[key] = normalize_rate_limit_int(value, 180, minimum=30, maximum=10000)
            elif key == "rate_limit_api_window_sec":
                data[key] = normalize_rate_limit_int(value, 60, minimum=10, maximum=3600)
            elif key == "rate_limit_static":
                data[key] = normalize_rate_limit_int(value, 600, minimum=60, maximum=50000)
            elif key == "rate_limit_static_window_sec":
                data[key] = normalize_rate_limit_int(value, 60, minimum=10, maximum=3600)
            elif key == "require_spend_confirm":
                data[key] = normalize_bool(value, True)
            elif key == "max_openai_calls_per_day":
                data[key] = normalize_daily_cap(value, 0)
            elif key == "max_flux_images_per_day":
                data[key] = normalize_daily_cap(value, 0)
            else:
                data[key] = value
    # Do not persist env-only secrets over a blank form field unless provided.
    SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return load_settings()


def public_settings() -> dict[str, Any]:
    data = load_settings()
    data["openai_api_key_set"] = bool(data.get("openai_api_key"))
    data["elevenlabs_api_key_set"] = bool(data.get("elevenlabs_api_key"))
    data["fal_key_set"] = bool(data.get("fal_key"))
    data["stripe_secret_key_set"] = bool(data.get("stripe_secret_key"))
    data["stripe_webhook_secret_set"] = bool(data.get("stripe_webhook_secret"))
    data["openai_api_key"] = REDACTION_PLACEHOLDER if data.get("openai_api_key") else ""
    data["elevenlabs_api_key"] = REDACTION_PLACEHOLDER if data.get("elevenlabs_api_key") else ""
    data["fal_key"] = REDACTION_PLACEHOLDER if data.get("fal_key") else ""
    data["stripe_secret_key"] = REDACTION_PLACEHOLDER if data.get("stripe_secret_key") else ""
    data["stripe_webhook_secret"] = REDACTION_PLACEHOLDER if data.get("stripe_webhook_secret") else ""
    data["stripe_publishable_key"] = str(data.get("stripe_publishable_key") or "")
    data["stripe_price_id"] = str(data.get("stripe_price_id") or "")
    data["stripe_product_id"] = str(data.get("stripe_product_id") or "")
    data["stripe_price_amount_cents"] = int(data.get("stripe_price_amount_cents") or 2900)
    data["stripe_price_currency"] = str(data.get("stripe_price_currency") or "usd")
    data["stripe_price_interval"] = str(data.get("stripe_price_interval") or "month")
    data["public_base_url"] = str(data.get("public_base_url") or "")
    data["membership_required"] = normalize_bool(data.get("membership_required"), True)
    data["membership_equal"] = True
    data["email_enabled"] = normalize_bool(data.get("email_enabled"), False)
    data["smtp_host"] = str(data.get("smtp_host") or "")
    data["smtp_port"] = int(data.get("smtp_port") or 587)
    data["smtp_user"] = str(data.get("smtp_user") or "")
    data["smtp_password_set"] = bool(data.get("smtp_password"))
    data["smtp_password"] = REDACTION_PLACEHOLDER if data.get("smtp_password") else ""
    data["smtp_use_tls"] = normalize_bool(data.get("smtp_use_tls"), True)
    data["smtp_use_ssl"] = normalize_bool(data.get("smtp_use_ssl"), False)
    data["email_from"] = str(data.get("email_from") or "")
    data["email_from_name"] = str(data.get("email_from_name") or "Stickman Automation")
    data["email_reply_to"] = str(data.get("email_reply_to") or "")
    data["email_configured"] = bool(data["email_enabled"] and data["smtp_host"])
    try:
        from studio.email import list_templates

        data["email_templates"] = {t["key"]: {"subject": t["subject"], "text": t["text"]} for t in list_templates(data)}
    except Exception:
        data["email_templates"] = data.get("email_templates") if isinstance(data.get("email_templates"), dict) else {}
    data["youtube_client_secret_set"] = bool(data.get("youtube_client_secret"))
    data["youtube_client_secret"] = REDACTION_PLACEHOLDER if data.get("youtube_client_secret") else ""
    data["ngrok_basic_auth_user"] = (data.get("ngrok_basic_auth_user") or "bubblepod").strip() or "bubblepod"
    data["ngrok_basic_auth_password_set"] = bool(data.get("ngrok_basic_auth_password"))
    data["ngrok_basic_auth_password"] = (
        REDACTION_PLACEHOLDER if data.get("ngrok_basic_auth_password") else ""
    )
    data["mcp_pin_set"] = bool(data.get("mcp_pin_hash"))
    data.pop("mcp_pin_hash", None)
    data["mcp_pin"] = ""
    data["youtube_connected"] = YOUTUBE_TOKEN_PATH.is_file()
    try:
        from studio.auth import get_auth_config

        data["auth_username"] = get_auth_config().get("username") or "admin"
    except Exception:
        data["auth_username"] = "admin"
    host = data.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    port = int(data.get("port") or 7878)
    data["port"] = port
    data["admin_url"] = f"http://{host}:{port}/admin"
    data["youtube_connect_url"] = f"http://{host}:{port}/api/youtube/connect"
    data["youtube_oauth_callback"] = f"http://{host}:{port}/api/youtube/oauth/callback"
    data["youtube_privacy_options"] = list(YOUTUBE_PRIVACY)
    data["auto_scheduler"] = normalize_auto_scheduler(data.get("auto_scheduler", True))
    data["hands_off"] = normalize_hands_off(data.get("hands_off"))
    data["hands_off_interval_hours"] = normalize_hands_off_interval_hours(data.get("hands_off_interval_hours"))
    data["hands_off_min_queue"] = normalize_hands_off_min_queue(data.get("hands_off_min_queue"))
    data["scheduler_interval_sec"] = 30
    data["flux_model"] = FLUX_MODEL
    data["image_providers"] = list(IMAGE_PROVIDERS)
    data["image_provider_labels"] = dict(IMAGE_PROVIDER_LABELS)
    data["cover_providers"] = list(COVER_PROVIDERS)
    data["cover_provider_labels"] = dict(COVER_PROVIDER_LABELS)
    data["cover_provider"] = normalize_cover_provider(data.get("cover_provider"))
    data["script_draft_providers"] = ["", "openai", "lmstudio"]
    data["script_draft_provider_labels"] = {
        "": "Off (native text_provider writes scripts)",
        "openai": "OpenAI API draft when text_provider is chatgpt/claude",
        "lmstudio": "LM Studio draft when text_provider is chatgpt/claude",
    }
    data["script_draft_provider"] = normalize_script_draft_provider(
        data.get("script_draft_provider")
    )
    from studio.comfyui import workflow_public_status

    data["comfyui_url"] = normalize_comfyui_url(data.get("comfyui_url"))
    data["comfyui_workflow"] = workflow_public_status()
    data["comfyui_workflow_loaded"] = bool(data["comfyui_workflow"].get("loaded"))
    if not data.get("comfyui_workflow_filename"):
        data["comfyui_workflow_filename"] = data["comfyui_workflow"].get("filename") or ""
    data["script_provider"] = data.get("text_provider") or "openai"
    data["text_providers"] = list(TEXT_PROVIDERS)
    data["text_provider_note"] = (
        "openai bills the OpenAI chat API for scripts and topic batches. "
        "lmstudio calls the local OpenAI-compatible server (default http://127.0.0.1:1234/v1) — no OpenAI cloud. "
        "chatgpt and claude write natively through Desktop MCP (save_script / create_topic) "
        "and never call OpenAI chat. When text_provider is lmstudio, ChatGPT/Claude MCP should call "
        "generate_script_via_api / generate_topics instead of writing the body themselves."
    )
    data["video_layouts"] = list(VIDEO_LAYOUTS)
    data["character_sizes"] = list(CHARACTER_SIZES)
    data["character_size_scales"] = dict(CHARACTER_SIZE_SCALES)
    try:
        from studio.pose_colors import head_color_status

        head = head_color_status()
        data["stickman_head_color"] = head["color"]
        data["stickman_head_dark"] = head["dark"]
        data["stickman_head"] = head
    except Exception:
        data["stickman_head_color"] = data.get("stickman_head_color") or "#FAE02E"
        data["stickman_head_dark"] = "#F8AF05"
    data["aspects"] = ["16:9", "9:16", "both"]
    data["tts_providers"] = list(TTS_PROVIDERS)
    from studio.tts_local import local_tts_status

    data["local_tts"] = local_tts_status()
    data["fal_key_note"] = (
        "FAL_KEY / fal_key is only required when image_provider is flux. "
        "ChatGPT native images never call fal. ComfyUI is local (comfyui_url, default "
        f"{COMFYUI_DEFAULT_URL}) and needs an uploaded API JSON — it never calls Fal."
    )
    data["comfyui_note"] = (
        "Upload ComfyUI File → Save (API Format) JSON in Settings. "
        "Stored at user_data/comfyui_workflow.json. Size is injected from the job aspect: "
        "16:9 → 1920x1080, 9:16 → 1080x1920 (never square). Billboard line art stays "
        "1920x1080 even on 9:16 video. Cover always matches that cover's aspect. "
        "If no workflow is uploaded, Studio errors and does not call Fal."
    )
    from studio.gpu_lock import gpu_lock_public

    data["gpu_lock"] = gpu_lock_public()
    try:
        from studio.ngrok_tunnel import ngrok_status

        data["ngrok"] = ngrok_status()
        data["ngrok_command"] = data["ngrok"].get("command")
    except Exception:
        data["ngrok"] = {
            "ok": False,
            "running": False,
            "ngrok_found": False,
            "detail": "ngrok status unavailable",
            "command": f'ngrok http {int(data.get("ngrok_local_port") or 7878)} --url {data.get("ngrok_url")}',
        }
        data["ngrok_command"] = data["ngrok"]["command"]
    try:
        from studio.spend_guard import public_spend_status

        data["spend"] = public_spend_status()
    except Exception:
        data["spend"] = {
            "require_spend_confirm": bool(data.get("require_spend_confirm", True)),
            "max_openai_calls_per_day": int(data.get("max_openai_calls_per_day") or 0),
            "max_flux_images_per_day": int(data.get("max_flux_images_per_day") or 0),
        }
    data["rate_limit_note"] = (
        "Login is capped per IP (default 8 / 15 min). General /api and /mcp share a per-IP "
        "sliding window (default 180 / min). Static assets use a looser bucket. "
        "Client IP uses the rightmost X-Forwarded-For hop when present (ngrok)."
    )
    data["spend_note"] = (
        "OpenAI cloud (scripts, topics, TTS) and Flux/fal require confirm_spend or a "
        "spend_confirm_id from request_spend_confirm when require_spend_confirm is on. "
        "LM Studio, ChatGPT/Claude native, ComfyUI, and local TTS are free paths and skip this gate. "
        "Hands-off scheduler spends without per-call confirm while hands_off is enabled. "
        "Daily caps of 0 mean unlimited."
    )
    return data


# Fields safe for regular members (no secrets, server control, or shared infra knobs).
_MEMBER_SETTINGS_KEYS = frozenset({
    "tts_provider",
    "voice_provider",
    "openai_voice",
    "elevenlabs_voice_id",
    "local_voice",
    "tts_providers",
    "image_provider",
    "image_providers",
    "image_provider_labels",
    "cover_provider",
    "cover_providers",
    "cover_provider_labels",
    "text_provider",
    "script_provider",
    "text_providers",
    "video_layout",
    "video_layouts",
    "character_size",
    "character_sizes",
    "character_size_scales",
    "default_aspect",
    "aspects",
    "include_bubblehead",
    "generate_9x16",
    "music_volume_pct",
    "youtube_connected",
    "youtube_privacy",
    "youtube_privacy_options",
    "youtube_channel_id",
    "youtube_auto_upload",
    "local_tts",
    "membership_required",
    "membership_equal",
    "stripe_publishable_key",
    "stripe_price_id",
    "stripe_product_id",
    "stripe_price_amount_cents",
    "stripe_price_currency",
    "stripe_price_interval",
    "public_base_url",
    "flux_model",
})


def member_safe_settings(username: str = "") -> dict[str, Any]:
    """Subset of public_settings for non-admin members (no secrets / server control)."""
    full = public_settings()
    out = {k: full[k] for k in _MEMBER_SETTINGS_KEYS if k in full}
    out["viewer_role"] = "member"
    out["signed_in_as"] = (username or "").strip()
    out["is_admin"] = False
    # Explicitly omit admin-facing blobs.
    out.pop("ngrok", None)
    out.pop("spend", None)
    out.pop("auth_username", None)
    out.pop("admin_url", None)
    out.pop("mcp_pin", None)
    out.pop("mcp_pin_set", None)
    return out


def settings_for_user(user: dict[str, Any] | None) -> dict[str, Any]:
    role = (user or {}).get("role") or ""
    if role == "admin":
        data = public_settings()
        data["viewer_role"] = "admin"
        data["signed_in_as"] = (user or {}).get("username") or data.get("auth_username") or ""
        data["is_admin"] = True
        return data
    return member_safe_settings((user or {}).get("username") or "")
