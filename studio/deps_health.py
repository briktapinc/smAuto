"""Startup / get_health dependency probes (TTS, image providers, models, disk, GPU)."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any

log = logging.getLogger("bubblepod.deps_health")

# Soft floor for free space under user_data (jobs need room for frames/wav/mp4).
MIN_FREE_DISK_BYTES = int(os.environ.get("BUBBLEPOD_MIN_FREE_DISK_GB") or 5) * (1024**3)


def _hf_cache_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            roots.append(Path(raw).expanduser())
    home = Path.home()
    roots.append(home / ".cache" / "huggingface" / "hub")
    roots.append(home / ".cache" / "huggingface")
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for p in roots:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def model_cache_status() -> dict[str, Any]:
    """Report whether Resemble Chatterbox (and related) weights look present."""
    roots = _hf_cache_roots()
    chatterbox_hits: list[str] = []
    total_bytes = 0
    for root in roots:
        if not root.exists():
            continue
        try:
            for path in root.rglob("*"):
                name = path.name.lower()
                if "chatterbox" in name or "resembleai" in name:
                    chatterbox_hits.append(str(path))
                if path.is_file():
                    try:
                        total_bytes += path.stat().st_size
                    except OSError:
                        pass
        except OSError:
            continue
        # Prefer hub/models--ResembleAI--chatterbox style
        for cand in (
            root / "models--ResembleAI--chatterbox",
            root / "hub" / "models--ResembleAI--chatterbox",
        ):
            if cand.is_dir() and str(cand) not in chatterbox_hits:
                chatterbox_hits.append(str(cand))

    present = bool(chatterbox_hits)
    progress = "ready" if present else "missing"
    return {
        "ok": present,
        "progress": progress,
        "chatterbox_cached": present,
        "cache_roots": [str(r) for r in roots if r.exists()],
        "hits": chatterbox_hits[:8],
        "approx_cache_bytes": total_bytes if total_bytes else None,
        "note": (
            "Chatterbox weights found in Hugging Face cache."
            if present
            else (
                "Chatterbox weights not found yet. First Local TTS generate downloads "
                "ResembleAI/chatterbox from Hugging Face, or call warm_local_tts_models()."
            )
        ),
        "warm_command": (
            "cd /root/stickmanautomation && .venv/bin/python -c "
            "\"from studio.deps_health import warm_local_tts_models; print(warm_local_tts_models())\""
        ),
    }


def disk_status(path: Path | None = None) -> dict[str, Any]:
    from studio.paths import USER_DATA

    target = path or USER_DATA
    try:
        target.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(str(target))
    except OSError as exc:
        return {
            "ok": False,
            "path": str(target),
            "error": str(exc),
            "fix_command": f"Ensure {target} is writable and has free space.",
        }
    free = int(usage.free)
    total = int(usage.total)
    ok = free >= MIN_FREE_DISK_BYTES
    return {
        "ok": ok,
        "path": str(target),
        "free_bytes": free,
        "total_bytes": total,
        "free_gb": round(free / (1024**3), 2),
        "total_gb": round(total / (1024**3), 2),
        "min_free_gb": MIN_FREE_DISK_BYTES // (1024**3),
        "error": "" if ok else f"Less than {MIN_FREE_DISK_BYTES // (1024**3)} GiB free under {target}.",
        "fix_command": "" if ok else f"Free disk space under {target} (need ≥{MIN_FREE_DISK_BYTES // (1024**3)} GiB).",
    }


def gpu_status() -> dict[str, Any]:
    cuda_ok = False
    cuda_name = ""
    torch_error = ""
    try:
        import torch

        cuda_ok = bool(torch.cuda.is_available())
        if cuda_ok:
            try:
                cuda_name = str(torch.cuda.get_device_name(0) or "")
            except Exception:
                cuda_name = "cuda"
    except Exception as exc:
        torch_error = str(exc)

    encoder = detect_video_encoder()
    return {
        "cuda_available": cuda_ok,
        "cuda_device_name": cuda_name,
        "torch_error": torch_error,
        "video_encoder": encoder.get("encoder"),
        "video_encoder_preset": encoder.get("preset"),
        "nvenc_available": bool(encoder.get("nvenc")),
        "ok": True,  # GPU optional; Local TTS can use CPU
    }


def detect_video_encoder() -> dict[str, Any]:
    """Prefer h264_nvenc when ffmpeg reports it and a probe encode succeeds (or env forces)."""
    force = (os.environ.get("BUBBLEPOD_VIDEO_ENCODER") or "").strip().lower()
    if force in ("libx264", "x264", "cpu"):
        return {"encoder": "libx264", "preset": "veryfast", "nvenc": False, "source": "env"}
    if force in ("h264_nvenc", "nvenc"):
        return {"encoder": "h264_nvenc", "preset": "p4", "nvenc": True, "source": "env"}

    nvenc = False
    try:
        from studio.ffmpeg_bin import resolve_ffmpeg

        import subprocess

        ffmpeg = resolve_ffmpeg()
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        text = (probe.stdout or "") + (probe.stderr or "")
        nvenc = "h264_nvenc" in text
    except Exception:
        nvenc = False

    # Without a working NVIDIA driver, NVENC usually fails at encode time — prefer CPU then.
    driver_ok = False
    if nvenc:
        try:
            import subprocess

            smi = subprocess.run(
                ["nvidia-smi", "-L"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            driver_ok = smi.returncode == 0 and bool((smi.stdout or "").strip())
        except Exception:
            driver_ok = False

    if nvenc and driver_ok:
        return {"encoder": "h264_nvenc", "preset": "p4", "nvenc": True, "source": "auto"}
    return {
        "encoder": "libx264",
        "preset": "veryfast",
        "nvenc": False,
        "nvenc_listed": nvenc,
        "source": "auto",
    }


def image_provider_status(settings: dict | None = None) -> dict[str, Any]:
    from studio.settings import (
        load_settings,
        normalize_image_provider,
        is_placeholder_secret,
    )

    settings = settings if settings is not None else load_settings()
    provider = normalize_image_provider(settings.get("image_provider"))
    fal_key = (settings.get("fal_key") or "").strip()
    fal_ok = bool(fal_key) and not is_placeholder_secret(fal_key)
    comfy_loaded = False
    comfy_url = (settings.get("comfyui_url") or "").strip()
    try:
        from studio.comfyui import workflow_is_loaded

        comfy_loaded = bool(workflow_is_loaded())
    except Exception:
        try:
            from studio.paths import USER_DATA

            comfy_loaded = (USER_DATA / "comfyui_workflow.json").is_file()
        except Exception:
            comfy_loaded = False

    ready = True
    error = ""
    fix = ""
    if provider == "flux":
        ready = fal_ok
        if not ready:
            error = "image_provider is flux but fal_key is missing or a placeholder."
            fix = "Set fal_key in Settings (or FAL_KEY env), then restart if needed."
    elif provider == "comfyui":
        ready = comfy_loaded and bool(comfy_url)
        if not comfy_loaded:
            error = "image_provider is comfyui but no API workflow is uploaded."
            fix = "Upload a ComfyUI API workflow JSON in Settings."
        elif not comfy_url:
            error = "image_provider is comfyui but comfyui_url is empty."
            fix = "Set comfyui_url in Settings to your ComfyUI server."
    elif provider == "chatgpt":
        ready = True
        error = ""
        fix = ""

    return {
        "ok": ready,
        "provider": provider,
        "fal_key_set": fal_ok,
        "comfyui_workflow_loaded": comfy_loaded,
        "comfyui_url": comfy_url,
        "error": error,
        "fix_command": fix,
    }


def tts_engine_status(settings: dict | None = None) -> dict[str, Any]:
    from studio.settings import load_settings, normalize_tts_provider
    from studio.tts_local import CHATTERBOX_HINT, local_tts_status

    settings = settings if settings is not None else load_settings()
    provider = normalize_tts_provider(settings.get("tts_provider") or "local", default="local")
    local = local_tts_status()
    out: dict[str, Any] = {
        "provider": provider,
        "local": local,
        "ok": True,
        "error": "",
        "fix_command": "",
        "blocks_job_start": False,
        "warning": "",
    }
    if provider == "external":
        out["ok"] = True
        out["blocks_job_start"] = False
        out["warning"] = (
            "Studio default tts_provider is external — each job needs "
            "upload_narration_audio(project_id, audio) before the audio step. "
            "Jobs without narration_external.mp3 fail with EXTERNAL_AUDIO_MISSING "
            "(no Chatterbox fallback)."
        )
        out["external"] = {
            "ready": True,
            "note": "Per-project MP3 upload required; startup does not fail.",
        }
        return out
    if provider == "local":
        if not local.get("ready"):
            out["ok"] = False
            out["blocks_job_start"] = True
            out["error"] = local.get("error") or CHATTERBOX_HINT
            out["fix_command"] = (
                local.get("install_hint")
                or (
                    "pip install -r requirements-tts-local.txt && "
                    "systemctl restart stickman-studio"
                )
            )
            out["error_code"] = "tts_model_load_failed"
        else:
            cache = model_cache_status()
            out["model_cache"] = cache
            if not cache.get("chatterbox_cached") and local.get("engine") == "chatterbox":
                out["warning"] = (
                    "Local TTS engine is installed but weights are not cached yet; "
                    "first generate will download from Hugging Face."
                )
    return out


def check_dependencies(*, settings: dict | None = None) -> dict[str, Any]:
    """Full self-check for get_health / fail-fast before job start."""
    from studio.settings import load_settings

    settings = settings if settings is not None else load_settings()
    tts = tts_engine_status(settings)
    images = image_provider_status(settings)
    disk = disk_status()
    gpu = gpu_status()
    cache = model_cache_status()
    gentle_ok = True
    gentle: dict[str, Any] = {}
    try:
        from studio.gentle import gentle_status

        gentle = gentle_status()
        gentle_ok = bool(gentle.get("ok") or gentle.get("running") or gentle.get("ready"))
    except Exception as exc:
        gentle = {"error": str(exc)}
        gentle_ok = False

    blockers: list[dict[str, str]] = []
    if tts.get("blocks_job_start"):
        blockers.append(
            {
                "code": str(tts.get("error_code") or "tts_model_load_failed"),
                "message": str(tts.get("error") or ""),
                "fix_command": str(tts.get("fix_command") or ""),
            }
        )
    if not disk.get("ok"):
        blockers.append(
            {
                "code": "disk_space_low",
                "message": str(disk.get("error") or ""),
                "fix_command": str(disk.get("fix_command") or ""),
            }
        )

    ok = not blockers
    return {
        "ok": ok,
        "tts": tts,
        "image_provider": images,
        "model_cache": cache,
        "disk": disk,
        "gpu": gpu,
        "gentle": gentle,
        "gentle_ok": gentle_ok,
        "blockers": blockers,
        "can_start_jobs": ok,
    }


def refuse_job_if_deps_broken(*, settings: dict | None = None) -> dict[str, Any] | None:
    """Return an error payload if Local TTS (or hard deps) would fail the job; else None."""
    deps = check_dependencies(settings=settings)
    if deps.get("can_start_jobs"):
        return None
    blockers = deps.get("blockers") or []
    first = blockers[0] if blockers else {}
    msg = first.get("message") or "Dependencies not ready."
    fix = first.get("fix_command") or ""
    code = first.get("code") or "dependency_failed"
    full = msg if not fix else f"{msg} Fix: {fix}"
    return {
        "ok": False,
        "started": False,
        "queued": False,
        "attached": False,
        "error": full,
        "error_code": code,
        "fix_command": fix,
        "dependencies": deps,
        "detail": full,
    }


def warm_local_tts_models(*, timeout_sec: int = 600) -> dict[str, Any]:
    """Pre-download / load Chatterbox weights so first generate is fast."""
    from studio.tts_local import chatterbox_installed, local_tts_status

    status = local_tts_status()
    if not chatterbox_installed():
        return {
            "ok": False,
            "error": status.get("error") or "Chatterbox not installed.",
            "fix_command": status.get("install_hint") or "",
            "model_cache": model_cache_status(),
        }
    cache_before = model_cache_status()
    if cache_before.get("chatterbox_cached"):
        return {
            "ok": True,
            "already_cached": True,
            "model_cache": cache_before,
            "progress": "ready",
        }

    # Trigger from_pretrained in a short worker so Studio process stays clean.
    import subprocess
    import sys

    from studio.paths import REPO_ROOT

    code = (
        "from chatterbox.tts import ChatterboxTTS\n"
        "import torch\n"
        "device='cuda' if torch.cuda.is_available() else 'cpu'\n"
        "m=ChatterboxTTS.from_pretrained(device=device)\n"
        "print('warmed', device, type(m).__name__)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=max(60, int(timeout_sec)),
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"Model warm timed out after {timeout_sec}s.",
            "model_cache": model_cache_status(),
            "progress": "timeout",
        }
    cache_after = model_cache_status()
    ok = proc.returncode == 0 and cache_after.get("chatterbox_cached")
    return {
        "ok": ok,
        "already_cached": False,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-2000:],
        "stderr": (proc.stderr or "")[-2000:],
        "model_cache": cache_after,
        "progress": "ready" if ok else "failed",
    }


def invalidate_cached_errors_for_settings(
    *,
    changed_keys: list[str] | None = None,
    image_provider: str | None = None,
) -> dict[str, Any]:
    """Clear stale topic/project errors that depended on settings that just changed."""
    from studio.projects import list_projects, load_meta, save_meta
    from studio.topics import _load as load_topics_store, _save as save_topics_store

    keys = {str(k) for k in (changed_keys or [])}
    if image_provider is not None:
        keys.add("image_provider")
    relevant = keys & {
        "image_provider",
        "fal_key",
        "tts_provider",
        "comfyui_url",
        "cover_provider",
        "openai_api_key",
        "text_provider",
    }
    if not relevant and image_provider is None:
        return {"cleared_topics": 0, "cleared_jobs": 0, "skipped": True}

    cleared_topics = 0
    cleared_jobs = 0
    provider_gate_markers = (
        "image_provider",
        "fal_key",
        "fal",
        "flux",
        "comfyui",
        "chatgpt",
        "tts",
        "chatterbox",
        "local tts",
        "provider_gate",
    )

    try:
        data = load_topics_store()
        topics = data.get("topics") or []
        dirty = False
        for topic in topics:
            err = (topic.get("error") or "").strip()
            if not err:
                continue
            low = err.lower()
            if any(m in low for m in provider_gate_markers):
                topic["error"] = None
                cleared_topics += 1
                dirty = True
                log.info(
                    "skip_decision=stale_error_cleared scope=topic id=%s reason=settings_changed keys=%s",
                    topic.get("id"),
                    sorted(relevant),
                )
        if dirty:
            save_topics_store(data)
    except Exception as exc:
        log.warning("Could not clear topic errors: %s", exc)

    try:
        for item in list_projects():
            pid = str(item.get("id") or "")
            if not pid:
                continue
            try:
                meta = load_meta(pid)
            except Exception:
                continue
            job = dict(meta.get("job") or {})
            err = (job.get("error") or "").strip()
            if not err:
                continue
            low = err.lower()
            if job.get("step") == "error" and any(m in low for m in provider_gate_markers):
                job["error"] = None
                job["detail"] = "Cleared stale error after settings change; resume when ready."
                job["step"] = "stopped"
                job["running"] = False
                meta["job"] = job
                save_meta(pid, meta)
                cleared_jobs += 1
                log.info(
                    "skip_decision=stale_error_cleared scope=job project_id=%s reason=settings_changed keys=%s",
                    pid,
                    sorted(relevant),
                )
    except Exception as exc:
        log.warning("Could not clear job errors: %s", exc)

    return {
        "cleared_topics": cleared_topics,
        "cleared_jobs": cleared_jobs,
        "keys": sorted(relevant),
    }
