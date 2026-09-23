"""Content-hash caches for illustration slots and pipeline step skip-on-retry."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("bubblepod.content_cache")

# Inbound base64 / raw image hard cap (before decode). ~18MB binary ≈ 24MB base64.
MAX_IMAGE_UPLOAD_BYTES = 18 * 1024 * 1024
MAX_BASE64_CHARS = 28 * 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, hex_len: int | None = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()
    if hex_len:
        return digest[:hex_len]
    return digest


def short_hash(data: bytes | str, *, n: int = 16) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return sha256_bytes(data)[:n]


def pipeline_cache_path(project_id: str) -> Path:
    from studio.projects import project_dir

    return project_dir(project_id) / "pipeline_cache.json"


def load_pipeline_cache(project_id: str) -> dict[str, Any]:
    path = pipeline_cache_path(project_id)
    if not path.is_file():
        return {"steps": {}, "updated_at": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("steps", {})
            return data
    except Exception:
        pass
    return {"steps": {}, "updated_at": None}


def save_pipeline_cache(project_id: str, data: dict[str, Any]) -> None:
    path = pipeline_cache_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["updated_at"] = int(time.time())
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def content_fingerprint(project_id: str) -> dict[str, str]:
    """Fingerprint script + illustration digests + key settings for skip-on-retry."""
    from studio.illustrations import illustration_jobs
    from studio.projects import load_meta, project_dir
    from studio.settings import load_settings

    meta = load_meta(project_id)
    settings = load_settings()
    parts: list[str] = []
    root = project_dir(project_id)
    for name in ("script_tagged.txt", "script.txt", "lines.json"):
        path = root / name
        if path.is_file():
            parts.append(f"{name}:{sha256_file(path)}")
    try:
        jobs = illustration_jobs(project_id).get("jobs") or []
        for job in jobs:
            digest = job.get("content_hash") or ""
            parts.append(f"{job.get('filename')}:{digest}:{int(bool(job.get('has_image')))}")
    except Exception:
        pass
    settings_keys = (
        "tts_provider",
        "image_provider",
        "video_layout",
        "character_size",
        "music_volume_pct",
        "include_bubblehead",
    )
    for key in settings_keys:
        parts.append(f"s:{key}:{settings.get(key)}")
        parts.append(f"m:{key}:{meta.get(key)}")
    blob = "|".join(parts)
    return {
        "fingerprint": sha256_bytes(blob.encode("utf-8")),
        "short": sha256_bytes(blob.encode("utf-8"))[:16],
    }


def remember_step(
    project_id: str,
    step: str,
    *,
    fingerprint: str,
    artifact_hash: str = "",
    detail: str = "",
) -> None:
    cache = load_pipeline_cache(project_id)
    steps = dict(cache.get("steps") or {})
    steps[step] = {
        "fingerprint": fingerprint,
        "artifact_hash": artifact_hash,
        "detail": detail,
        "ts": int(time.time()),
    }
    cache["steps"] = steps
    cache["fingerprint"] = fingerprint
    save_pipeline_cache(project_id, cache)
    log.info(
        "skip_decision=cache_store project_id=%s step=%s fingerprint=%s",
        project_id,
        step,
        fingerprint[:16],
    )


def should_skip_step(project_id: str, step: str, *, fingerprint: str) -> dict[str, Any]:
    cache = load_pipeline_cache(project_id)
    entry = (cache.get("steps") or {}).get(step) or {}
    if entry.get("fingerprint") == fingerprint:
        log.info(
            "skip_decision=cache_reuse project_id=%s step=%s fingerprint=%s",
            project_id,
            step,
            fingerprint[:16],
        )
        return {
            "skip": True,
            "error_code": "cache_reuse",
            "detail": f"Reusing cached {step} (content unchanged).",
            "entry": entry,
        }
    return {"skip": False}
