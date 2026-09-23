"""Structured pipeline / upload error codes + human messages."""

from __future__ import annotations

from typing import Any


EXTERNAL_AUDIO_MISSING = "EXTERNAL_AUDIO_MISSING"
EXTERNAL_IMAGES_MISSING = "EXTERNAL_IMAGES_MISSING"
TTS_MODEL_LOAD_FAILED = "tts_model_load_failed"
PROVIDER_GATE = "provider_gate"
UPLOAD_RESET = "upload_reset"
UPLOAD_TOO_LARGE = "upload_too_large"
DEPENDENCY_FAILED = "dependency_failed"
DISK_SPACE_LOW = "disk_space_low"
GPU_BUSY = "gpu_busy"
ALREADY_RUNNING = "already_running"
ALREADY_QUEUED = "already_queued"
CACHE_REUSE = "cache_reuse"


def classify_error(exc: BaseException | str | None) -> str:
    text = str(exc or "")
    low = text.lower()
    if not text:
        return "unknown"
    if "EXTERNAL_AUDIO_MISSING" in text or "narration_external" in low:
        return EXTERNAL_AUDIO_MISSING
    if (
        "EXTERNAL_IMAGES_MISSING" in text
        or "external images" in low
        or ("image_provider=external" in low and "missing" in low)
    ):
        return EXTERNAL_IMAGES_MISSING
    if "upload" in low and ("reset" in low or "broken pipe" in low or "connection reset" in low):
        return UPLOAD_RESET
    if "too large" in low or ("payload" in low and "limit" in low):
        return UPLOAD_TOO_LARGE
    if "chatterbox" in low or "local tts" in low or "piper" in low:
        return TTS_MODEL_LOAD_FAILED
    if "image_provider" in low or "fal_key" in low or "comfyui" in low or ("chatgpt" in low and "unsupervised" in low):
        return PROVIDER_GATE
    if "disk" in low and ("space" in low or "no space" in low):
        return DISK_SPACE_LOW
    if "gpu" in low and "busy" in low:
        return GPU_BUSY
    return "pipeline_error"


def error_payload(
    code: str,
    message: str,
    *,
    fix_command: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "error": message,
        "error_code": code,
        "message": message,
    }
    if fix_command:
        out["fix_command"] = fix_command
        out["error"] = f"{message} Fix: {fix_command}"
    if extra:
        out.update(extra)
    return out
