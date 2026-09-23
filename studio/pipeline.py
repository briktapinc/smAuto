from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from studio.aspect import (
    ASPECT_9_16,
    ASPECT_16_9,
    DEFAULT_ASPECT,
    aspects_to_render,
    needs_explainer_assets,
    needs_shorts_assets,
    normalize_aspect,
    normalize_job_aspect,
)
from studio.backgrounds import resolve_room_path
from studio.gentle import align_audio, align_project_audio, ensure_gentle
from studio.music import apply_project_music, assign_project_music
from studio.paths import CODE_DIR, REPO_ROOT, asset_warnings, ensure_fallback_background
from studio.projects import (
    billboards_dir,
    collect_renders,
    cover_matches_canvas,
    cover_path,
    final_video_path,
    frames_dir,
    input_prefix,
    invalidate_render_artifacts,
    last_video_path,
    list_projects,
    load_lines,
    load_meta,
    migrate_legacy_aspect_files,
    project_image_provider,
    project_payload,
    project_video_layout,
    project_character_size,
    project_include_bubblehead,
    publish_last_video,
    record_render,
    save_meta,
    shorts_input_prefix,
    write_shorts_scripts,
    project_generate_9x16,
    set_generate_9x16,
)
from studio.settings import is_studio_image_provider, load_settings, normalize_character_size, normalize_video_layout
from studio.thumbs import PLACEHOLDER_NAME, THUMB_NAME, ensure_thumbnail
from studio.scriptgen import apply_generated_script, generate_script
from studio.illustrations import (
    ensure_cover_for_aspect,
    ensure_fal_ready,
    generate_cover_image,
    generate_flux_illustrations,
    generate_illustrations,
    illustration_jobs,
    mark_all_slots_pending_regen,
    prune_stale_billboards,
    regenerate_illustration,
    resolve_illustration_slot,
    unsupervised_image_provider_gate_skip_message,
)
from studio.tts import generate_audio, generate_project_audio

_log = logging.getLogger("bubblepod.pipeline")
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_PERSIST = (
    "kind",
    "step",
    "detail",
    "progress_pct",
    "running",
    "paused",
    "error",
    "error_code",
    "youtube_error",
    "resumed_from",
    "started_at",
    "finished_at",
    "step_timings",
    "active_step",
    "waiting_on",
    "encoder",
    "encoder_preset",
)
_EPHEMERAL = ("_token", "_stop", "_alive", "_provider_switch", "_generating_file")

# Approximate pipeline weights for the global progress bar (0–100).
_STEP_PCT = {
    "script": 4,
    "cover": 10,
    "illustrations": 12,
    "audio": 48,
    "gentle": 54,
    "align": 58,
    "schedule": 62,
    "frames": 66,
    "render": 66,
    "ffmpeg": 88,
    "music": 93,
    "youtube": 96,
    "done": 100,
}
_STEP_SPAN = {
    "cover": (10, 14),
    "illustrations": (12, 47),
    "frames": (66, 86),
    "render": (66, 86),
    "ffmpeg": (88, 92),
    "youtube": (96, 99),
}


class JobHalted(Exception):
    """Worker should exit at a step boundary after pause/stop."""

    def __init__(self, mode: str = "stop"):
        self.mode = "pause" if mode == "pause" else "stop"
        super().__init__("paused" if self.mode == "pause" else "stopped")
_ARTIFACT_KEYS = (
    "script",
    "cover",
    "illustrations",
    "audio",
    "alignment",
    "frames",
    "video",
    "youtube",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_audio_source(meta: dict) -> str:
    from studio.settings import normalize_tts_provider, load_settings

    provider = normalize_tts_provider(
        meta.get("tts_provider")
        or meta.get("voice_provider")
        or load_settings().get("tts_provider")
        or "local",
        default="local",
    )
    if provider == "external":
        return "external"
    if provider == "local":
        return "chatterbox"
    return provider


def _close_step_timing(job: dict, *, end: str | None = None) -> None:
    """Finalize the open active_step entry in step_timings."""
    timings = list(job.get("step_timings") or [])
    active = (job.get("active_step") or "").strip()
    if not active or not timings:
        return
    end_ts = end or _now()
    for entry in reversed(timings):
        if entry.get("step") == active and not entry.get("ended_at"):
            entry["ended_at"] = end_ts
            try:
                from datetime import datetime as _dt

                start = _dt.fromisoformat(str(entry.get("started_at")))
                finish = _dt.fromisoformat(end_ts)
                entry["duration_ms"] = int((finish - start).total_seconds() * 1000)
            except Exception:
                entry["duration_ms"] = None
            break
    job["step_timings"] = timings


def _open_step_timing(job: dict, step: str, *, detail: str = "") -> None:
    step = (step or "").strip()
    if not step:
        return
    now = _now()
    _close_step_timing(job, end=now)
    timings = list(job.get("step_timings") or [])
    timings.append(
        {
            "step": step,
            "started_at": now,
            "ended_at": None,
            "duration_ms": None,
            "detail": (detail or "")[:240],
        }
    )
    # Keep last ~40 steps to bound meta.json size
    job["step_timings"] = timings[-40:]
    job["active_step"] = step


def estimate_progress_pct(step: str | None, detail: str | None = None, *, running: bool | None = None) -> int | None:
    """Map pipeline step (+ optional detail) to a 0–100 progress percentage."""
    import re

    step_key = (step or "").strip().lower()
    text = (detail or "").strip()
    if step_key in ("error", "paused", "stopped"):
        return None
    if step_key == "done" or (running is False and step_key in ("", "done")):
        return 100 if step_key == "done" else None

    base = _STEP_PCT.get(step_key)
    if base is None:
        if running:
            return 2
        return None

    lo, hi = _STEP_SPAN.get(step_key, (base, min(99, base + 4)))
    # Fractions like "Flux 3/56:" or "12/40"
    m = re.search(r"(?i)(?:^|\s)(\d+)\s*/\s*(\d+)", text)
    if m:
        cur, total = int(m.group(1)), int(m.group(2))
        if total > 0:
            frac = max(0.0, min(1.0, cur / total))
            return int(round(lo + (hi - lo) * frac))
    # Explicit percent (YouTube upload)
    m = re.search(r"(\d{1,3})\s*%", text)
    if m:
        pct = max(0, min(100, int(m.group(1))))
        return int(round(lo + (hi - lo) * (pct / 100.0)))
    return int(base)


def is_running(project_id: str) -> bool:
    with _lock:
        return bool((_jobs.get(project_id) or {}).get("running"))


def is_busy(project_id: str) -> bool:
    """True while a worker thread still owns this job (even after Stop)."""
    with _lock:
        return bool((_jobs.get(project_id) or {}).get("_alive"))


def any_pipeline_busy() -> bool:
    """True when no free pipeline slots remain (capacity full).

    Historically meant "any live worker". With multi-user queueing this is
    capacity-aware: up to ``max_concurrent_jobs`` workers may run at once.
    """
    try:
        from studio.job_queue import capacity_full

        return capacity_full()
    except Exception:
        with _lock:
            return any(bool(job.get("_alive")) for job in _jobs.values())


def scheduler_blocked_reason() -> str | None:
    """Why the topic due-picker must not start another pipeline.

    Blocks only when concurrent capacity is full. GPU contention is handled
    inside workers (gpu_lock); paused jobs do not occupy a live slot once the
    worker has exited, so they no longer starve other users' queues.
    """
    try:
        from studio.job_queue import capacity_full

        if capacity_full():
            return "busy"
    except Exception:
        with _lock:
            if any(bool(job.get("_alive")) for job in _jobs.values()):
                return "busy"
    return None


def running_project_ids() -> list[str]:
    with _lock:
        return [pid for pid, job in _jobs.items() if job.get("_alive")]


def pipeline_capacity() -> dict:
    """Public snapshot of concurrent pipeline slots."""
    try:
        from studio.job_queue import public_status

        return public_status()
    except Exception:
        running = running_project_ids()
        return {
            "max_concurrent": 1,
            "running_count": len(running),
            "queued_count": 0,
            "slots_free": 0 if running else 1,
            "running_project_ids": running,
            "fairness": "round_robin",
        }

def _live_job(project_id: str) -> dict:
    with _lock:
        return dict(_jobs.get(project_id) or {})


def _public_job(blob: dict) -> dict:
    job = dict(blob)
    for key in _EPHEMERAL:
        job.pop(key, None)
    return job


def job_status_lite(project_id: str) -> dict:
    """Fast job snapshot for topic sync / list endpoints.

    Skips inspect_artifacts / next_resume_step (those open images and scan
    billboards). Enough for running/busy/step/error used by topics._sync_from_jobs.
    """
    live = _live_job(project_id)
    meta = load_meta(project_id)
    persisted = dict(meta.get("job") or {})
    running = bool(live.get("running"))
    busy = bool(live.get("_alive"))
    job = _public_job({**persisted, **live, "running": running})
    if persisted.get("running") and not live:
        job["running"] = False
        if job.get("step") not in ("done", "error", "paused", "stopped"):
            job["step"] = "error"
            job["error"] = "Server restarted while this job was running."
            job["detail"] = job["error"]
            job["finished_at"] = _now()
        _set_job(
            project_id,
            running=False,
            paused=False,
            step=job.get("step"),
            detail=job.get("detail"),
            error=job.get("error"),
            finished_at=job.get("finished_at"),
        )
        running = False
        busy = False
    if job.get("progress_pct") is None:
        pct = estimate_progress_pct(job.get("step"), job.get("detail"), running=running)
        if pct is not None:
            job["progress_pct"] = pct
    return {
        "id": project_id,
        "running": running,
        "busy": busy,
        "paused": bool(job.get("paused") or job.get("step") == "paused"),
        "status": meta.get("status"),
        "job": job,
        "step": job.get("step"),
        "detail": job.get("detail"),
        "progress_pct": job.get("progress_pct"),
        "error": job.get("error"),
    }


def job_status(project_id: str) -> dict:
    live = _live_job(project_id)
    meta = load_meta(project_id)
    persisted = dict(meta.get("job") or {})
    running = bool(live.get("running"))
    busy = bool(live.get("_alive"))
    stop_mode = live.get("_stop")
    job = _public_job({**persisted, **live, "running": running})
    # Threads die with the process; clear a stale "running" flag after restart.
    if persisted.get("running") and not live:
        job["running"] = False
        if job.get("step") not in ("done", "error", "paused", "stopped"):
            job["step"] = "error"
            job["error"] = "Server restarted while this job was running."
            job["detail"] = job["error"]
            job["finished_at"] = _now()
        _set_job(
            project_id,
            running=False,
            paused=False,
            step=job.get("step"),
            detail=job.get("detail"),
            error=job.get("error"),
            finished_at=job.get("finished_at"),
        )
        running = False
        busy = False
    try:
        arts = inspect_artifacts(project_id)
        nxt = next_resume_step(project_id, arts)
    except Exception:
        arts = {}
        nxt = "done"
    halted = job.get("step") in ("paused", "stopped") or bool(job.get("paused"))
    can_resume = (not running) and (
        nxt != "done"
        or bool(job.get("error"))
        or bool(job.get("youtube_error"))
        or halted
        or bool(stop_mode)
    )
    if job.get("progress_pct") is None:
        pct = estimate_progress_pct(job.get("step"), job.get("detail"), running=running)
        if pct is not None:
            job["progress_pct"] = pct
    queue_info = None
    try:
        from studio.job_queue import queue_info_for_project

        queue_info = queue_info_for_project(project_id)
    except Exception:
        queue_info = None
    queued = bool(queue_info and queue_info.get("status") == "queued")
    return {
        "id": project_id,
        "running": running,
        "busy": busy,
        "queued": queued,
        "queue": queue_info,
        "queue_position": (queue_info or {}).get("queue_position") if queued else None,
        "paused": bool(job.get("paused") or job.get("step") == "paused"),
        "stopping": bool(stop_mode),
        "status": meta.get("status"),
        "job": job,
        "kind": job.get("kind"),
        "step": job.get("step"),
        "detail": job.get("detail"),
        "progress_pct": job.get("progress_pct"),
        "error": job.get("error"),
        "error_code": job.get("error_code"),
        "active_step": job.get("active_step") or job.get("step"),
        "waiting_on": job.get("waiting_on"),
        "step_timings": job.get("step_timings") or [],
        "encoder": job.get("encoder"),
        "encoder_preset": job.get("encoder_preset"),
        "audio_source": meta.get("audio_source") or _job_audio_source(meta),
        "resume_from": nxt,
        "resumed_from": job.get("resumed_from") or nxt,
        "can_start": not busy and not queued,
        "can_stop": bool(running or (busy and not stop_mode)),
        "can_pause": bool(running or (busy and not stop_mode)),
        "can_resume": can_resume and not queued,
        "artifacts": _slim_artifacts(arts),
        "attached": False,
    }


def attach_job(payload: dict) -> dict:
    st = job_status(payload["id"])
    payload["running"] = st["running"]
    payload["busy"] = st["busy"]
    payload["paused"] = st["paused"]
    payload["stopping"] = st["stopping"]
    payload["job"] = st["job"]
    payload["resume_from"] = st.get("resume_from")
    payload["resumed_from"] = st.get("resumed_from")
    payload["can_start"] = st.get("can_start")
    payload["can_stop"] = st.get("can_stop")
    payload["can_pause"] = st.get("can_pause")
    payload["can_resume"] = st.get("can_resume")
    payload["artifacts"] = st.get("artifacts")
    return payload


def _youtube_watch_info(meta: dict | None) -> dict[str, Any]:
    from studio.projects import youtube_watch_info

    return youtube_watch_info(meta)


def list_library_items(*, owner_id: str | None = None, is_admin: bool = False) -> list[dict]:
    """Studio library cards: jobs with thumb/cover/audio/video flags for GUI and MCP.

    Intentionally avoids per-job inspect_artifacts / next_resume_step (those open
    images and scan billboards and take ~1s each). Full resume detail is available
    from GET /api/projects/{id}/job when a card is opened.

    When owner_id is set and is_admin is False, only that member's jobs are returned.
    """
    settings = load_settings()
    host = settings.get("host") or "127.0.0.1"
    if host in ("0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    port = int(settings.get("port") or 7878)
    base = f"http://{host}:{port}"
    items = []
    for item in list_projects():
        if not is_admin and owner_id:
            if str(item.get("owner_id") or "").strip() != str(owner_id).strip():
                continue
        elif not is_admin and not owner_id:
            # Unscoped non-admin: return nothing rather than leak all jobs.
            continue
        pid = item["id"]
        live = _live_job(pid)
        persisted = dict(item.get("job") or {})
        running = bool(live.get("running"))
        busy = bool(live.get("_alive"))
        stop_mode = live.get("_stop")
        # Threads die with the process; treat orphaned running flags as idle for the list.
        if persisted.get("running") and not live:
            running = False
            busy = False
        job = _public_job({**persisted, **live, "running": running})
        if job.get("progress_pct") is None:
            pct = estimate_progress_pct(job.get("step"), job.get("detail"), running=running)
            if pct is not None:
                job["progress_pct"] = pct
        prefix = Path(input_prefix(pid))
        folder = prefix.parent
        cover = cover_path(pid)
        thumb = folder / THUMB_NAME
        placeholder = folder / PLACEHOLDER_NAME
        renders = collect_renders(pid)
        video = last_video_path(pid)
        audio = prefix.with_suffix(".wav")
        has_video = any(blob.get("ready") for blob in renders.values()) or (
            video.is_file() and video.stat().st_size > 1000
        )
        yt_info = _youtube_watch_info(item)
        has_youtube = bool(yt_info.get("has_youtube"))
        has_audio = audio.is_file() and audio.stat().st_size > 1000
        # Existence only — canvas dimension check is too slow for 40+ cards.
        has_cover = cover.is_file() and cover.stat().st_size > 200
        thumb_path = next(
            (str(path) for path in (thumb, cover, placeholder) if path.is_file() and path.stat().st_size > 200),
            None,
        )
        halted = job.get("step") in ("paused", "stopped") or bool(job.get("paused"))
        can_resume = (not running) and (
            not has_video
            or bool(job.get("error"))
            or bool(job.get("youtube_error"))
            or halted
            or bool(stop_mode)
        )
        queue_info = None
        try:
            from studio.job_queue import queue_info_for_project

            queue_info = queue_info_for_project(pid)
        except Exception:
            queue_info = None
        queued = bool(queue_info and queue_info.get("status") == "queued")
        row = dict(item)
        row.update(
            {
                "running": running,
                "busy": busy,
                "queued": queued,
                "queue": queue_info,
                "queue_position": (queue_info or {}).get("queue_position") if queued else None,
                "paused": bool(job.get("paused") or job.get("step") == "paused"),
                "stopping": bool(stop_mode),
                "job": job,
                "has_audio": has_audio,
                "has_video": has_video,
                "has_youtube": has_youtube,
                "youtube_url": yt_info.get("youtube_url"),
                "youtube_video_id": yt_info.get("youtube_video_id"),
                "library_ready": bool(has_video or has_youtube),
                "has_cover": has_cover,
                "has_thumbnail": True,
                "thumbnail_path": thumb_path,
                "thumbnail_url": f"{base}/api/projects/{pid}/thumbnail",
                "studio_url": f"{base}/",
                "renders": renders,
                "last_render_aspect": item.get("last_render_aspect"),
                "resume_from": job.get("resumed_from") or job.get("step") or ("video" if not has_video else "done"),
                "can_start": not busy and not queued,
                "can_stop": bool(running or (busy and not stop_mode)),
                "can_pause": bool(running or (busy and not stop_mode)),
                "can_resume": can_resume,
                "progress_pct": job.get("progress_pct"),
            }
        )
        items.append(row)
    return items


def _file_ok(path: Path, min_size: int = 1) -> bool:
    return path.is_file() and path.stat().st_size >= min_size


def _normalize_spoken(text: str) -> str:
    return " ".join((text or "").replace("\r", " ").replace("\n", " ").split()).strip().lower()


def alignment_is_fresh(project_id: str, *, shorts: bool = False) -> bool:
    """True only when Gentle json matches current *_g.txt and is not older than the wav."""
    prefix = Path(shorts_input_prefix(project_id) if shorts else input_prefix(project_id))
    json_path = prefix.with_suffix(".json")
    wav = prefix.with_suffix(".wav")
    g_txt = prefix.with_name(prefix.name + "_g.txt")
    if not (_file_ok(json_path, 50) and _file_ok(wav, 1000) and _file_ok(g_txt, 10)):
        return False
    json_mtime = json_path.stat().st_mtime
    if json_mtime + 0.05 < wav.stat().st_mtime:
        return False
    if json_mtime + 0.05 < g_txt.stat().st_mtime:
        return False
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    words = data.get("words")
    if not isinstance(words, list) or not words:
        return False
    transcript = _normalize_spoken(data.get("transcript") or "")
    try:
        raw = _normalize_spoken(g_txt.read_text(encoding="utf-8"))
    except OSError:
        return False
    if transcript and raw and transcript != raw:
        return False
    return True


def project_alignment_is_fresh(project_id: str) -> bool:
    meta = load_meta(project_id)
    aspect = meta.get("aspect") or DEFAULT_ASPECT
    ok = True
    if needs_explainer_assets(aspect):
        ok = ok and alignment_is_fresh(project_id, shorts=False)
    if project_generate_9x16(meta):
        ok = ok and alignment_is_fresh(project_id, shorts=True)
    if not needs_explainer_assets(aspect) and not project_generate_9x16(meta):
        ok = alignment_is_fresh(project_id, shorts=False)
    return ok


def _has_frames(frames_folder: str | Path) -> bool:
    folder = Path(frames_folder)
    if not folder.is_dir():
        return False
    return len(list(folder.glob("f*.png"))) >= 2


def _youtube_uploaded(project_id: str) -> bool:
    meta = load_meta(project_id)
    blob = meta.get("youtube")
    if isinstance(blob, dict) and (blob.get("video_id") or blob.get("id") or blob.get("url")):
        return True
    return bool(meta.get("youtube_video_id") or meta.get("youtube_id"))


def _missing_line_images(project_id: str) -> int:
    """Count missing line PNGs required for this job's aspect(s)."""
    try:
        info = illustration_jobs(project_id)
    except Exception:
        return 0
    missing = 0
    for job in info.get("jobs") or []:
        if job.get("role") == "cover":
            continue
        if not job.get("has_image"):
            missing += 1
    return missing


def inspect_artifacts(project_id: str) -> dict:
    """Detect which pipeline outputs already exist on disk / in meta.json."""
    migrate_legacy_aspect_files(project_id)
    prefix = Path(input_prefix(project_id))
    shorts = Path(shorts_input_prefix(project_id))
    tagged = prefix.with_suffix(".txt")
    meta = load_meta(project_id)
    needed = aspects_to_render(meta.get("aspect") or DEFAULT_ASPECT)
    has_script = _file_ok(tagged, 20)
    has_cover = any(cover_matches_canvas(cover_path(project_id, asp), asp) for asp in needed)
    missing_lines = _missing_line_images(project_id) if has_script else (0 if has_cover else 1)
    has_illustrations = has_cover and missing_lines == 0
    has_video = all(_file_ok(final_video_path(project_id, asp), 1000) for asp in needed)
    has_frames = all(_has_frames(frames_dir(project_id, asp)) for asp in needed)
    want_full = needs_explainer_assets(meta.get("aspect"))
    want_shorts = project_generate_9x16(meta)
    audio_ok = True
    if want_full:
        audio_ok = audio_ok and _file_ok(prefix.with_suffix(".wav"), 1000)
    if want_shorts:
        audio_ok = audio_ok and _file_ok(shorts.with_suffix(".wav"), 1000)
    if not want_full and not want_shorts:
        audio_ok = _file_ok(prefix.with_suffix(".wav"), 1000)
    return {
        "script": has_script,
        "cover": has_cover,
        "illustrations": has_illustrations,
        "missing_illustrations": missing_lines + (0 if has_cover else 1),
        "audio": audio_ok,
        "alignment": project_alignment_is_fresh(project_id),
        "frames": has_frames,
        "schedule": (
            (not want_full or _file_ok(prefix.with_name(prefix.name + "_schedule.csv"), 20))
            and (not want_shorts or _file_ok(shorts.with_name(shorts.name + "_schedule.csv"), 20))
        ),
        "video": has_video,
        "youtube": _youtube_uploaded(project_id),
    }


def _slim_artifacts(arts: dict) -> dict:
    return {key: bool(arts.get(key)) for key in _ARTIFACT_KEYS}


def next_resume_step(project_id: str, artifacts: dict | None = None) -> str:
    """First incomplete pipeline step. Resume continues from here through the rest."""
    arts = artifacts or inspect_artifacts(project_id)
    if not arts["script"]:
        return "script"
    provider = project_image_provider(project_id)
    if not arts["cover"] or (is_studio_image_provider(provider) and not arts["illustrations"]):
        return "illustrations"
    if not arts["audio"]:
        return "audio"
    if not arts["alignment"]:
        return "align"
    if not arts["video"]:
        if arts["frames"]:
            return "ffmpeg"
        return "render"
    if not arts["youtube"]:
        try:
            from studio.youtube import job_auto_upload

            if job_auto_upload(project_id):
                return "youtube"
        except Exception:
            pass
    return "done"


def _run_resume(project_id: str) -> None:
    _check_stop(project_id)
    arts = inspect_artifacts(project_id)
    if not arts["script"]:
        from studio.settings import (
            current_text_provider,
            is_native_text_provider,
            load_settings,
            normalize_script_draft_provider,
            text_provider_label,
        )

        draft = normalize_script_draft_provider(load_settings().get("script_draft_provider"))
        if is_native_text_provider() and not draft:
            label = text_provider_label()
            raise RuntimeError(
                f"text_provider is {current_text_provider()}. Write the tagged script in {label} "
                "Desktop MCP (get_chatgpt_playbook, then save_script). "
                "Or set script_draft_provider to openai/lmstudio for an auto first draft. "
                "Do not call generate_script_via_api — that spends OpenAI tokens."
            )
        from studio.textgen import script_progress_detail

        draft_provider = draft if (is_native_text_provider() and draft) else None
        detail = (
            f"Drafting script via {draft_provider} (script_draft_provider)…"
            if draft_provider
            else script_progress_detail()
        )
        _progress(project_id, step="script", detail=detail)
        meta = load_meta(project_id)
        generated = generate_script(
            meta["topic"],
            meta["duration_seconds"],
            "",
            aspect=meta.get("aspect") or DEFAULT_ASPECT,
            layout=project_video_layout(project_id),
            image_provider=project_image_provider(project_id),
            project_id=project_id,
            provider=draft_provider,
        )
        apply_generated_script(project_id, generated)
        if draft_provider:
            try:
                meta = load_meta(project_id)
                meta["script_draft"] = True
                meta["script_draft_provider"] = draft_provider
                meta["status"] = meta.get("status") or "draft"
                from studio.projects import save_meta

                save_meta(project_id, meta)
            except Exception:
                pass
            # Auto-draft only — stop before billed media / publish so the agent can edit via save_script.
            _progress(
                project_id,
                running=False,
                step="script",
                detail=(
                    f"First-draft script saved via {draft_provider}. "
                    "Review/edit with save_script, then resume_job / start_job to continue."
                ),
                error=None,
            )
            return

    _check_stop(project_id)
    arts = inspect_artifacts(project_id)
    provider = project_image_provider(project_id)
    if provider == "chatgpt":
        skip_msg = unsupervised_image_provider_gate_skip_message(project_id)
        if skip_msg:
            _log.info("skip_decision=provider_gate_skip project_id=%s detail=%s", project_id, skip_msg)
            print(skip_msg)
        else:
            from studio.projects import set_image_provider
            from studio.settings import load_settings

            if (load_settings().get("fal_key") or "").strip():
                set_image_provider(project_id, "flux")
                provider = "flux"
            else:
                raise RuntimeError(
                    "image_provider is chatgpt, which cannot generate pictures unsupervised. "
                    "Set image_provider to flux (fal_key) or comfyui."
                )
    need_images = (not arts["cover"]) or (is_studio_image_provider(provider) and not arts["illustrations"])
    need_audio = not arts["audio"]
    need_video = not arts["video"]
    gpu_section = need_images or need_audio or need_video

    def _gpu_media_steps() -> bool:
        nonlocal arts, provider
        from studio.content_cache import content_fingerprint, remember_step, should_skip_step

        fp = content_fingerprint(project_id).get("fingerprint") or ""
        if need_images:
            skip = should_skip_step(project_id, "illustrations", fingerprint=fp)
            if skip.get("skip") and arts.get("cover") and arts.get("illustrations"):
                _log.info(
                    "skip_decision=cache_reuse project_id=%s step=illustrations detail=%s",
                    project_id,
                    skip.get("detail"),
                )
                _progress(
                    project_id,
                    step="illustrations",
                    detail=skip.get("detail") or "Reusing cached illustrations.",
                    waiting_on=None,
                )
            else:
                _progress(project_id, step="illustrations", detail="Generating title-card cover and backgrounds...", waiting_on="image_provider")
                if is_studio_image_provider(provider):
                    illustration_jobs(project_id)
                    if provider == "comfyui":
                        from studio.comfyui import ensure_comfyui_ready

                        ensure_comfyui_ready()
                    else:
                        ensure_fal_ready()
                    generate_illustrations(
                        project_id,
                        progress=lambda detail: _progress(project_id, step="illustrations", detail=detail),
                        cancel_check=lambda: _check_stop(project_id),
                    )
                else:
                    for canvas in aspects_to_render(load_meta(project_id).get("aspect")):
                        generate_cover_image(
                            project_id,
                            aspect=canvas,
                            progress=lambda detail: _progress(project_id, step="cover", detail=detail),
                        )
                remember_step(project_id, "illustrations", fingerprint=fp, detail="illustrations ready")

        _check_stop(project_id)
        arts = inspect_artifacts(project_id)
        if not arts["audio"]:
            _progress(project_id, step="audio", detail="Generating narration…", waiting_on="tts")
            generate_audio_then_align(project_id)
            remember_step(project_id, "audio", fingerprint=fp, detail="audio+align ready")
        elif not arts["alignment"]:
            _ensure_fresh_alignment(project_id)

        _check_stop(project_id)
        arts = inspect_artifacts(project_id)
        if not arts["video"]:
            render_video(project_id, skip_completed=True, generate_missing_audio=True)
            remember_step(project_id, "video", fingerprint=fp, detail="video ready")
            return True
        remember_step(project_id, "video", fingerprint=fp, detail="video already present")
        return False

    if gpu_section:
        from studio.gpu_lock import holding

        with holding(f"pipeline:{project_id}", kind="pipeline", project_id=project_id):
            if _gpu_media_steps():
                return
    elif not arts["alignment"] and arts["audio"]:
        _ensure_fresh_alignment(project_id)
        arts = inspect_artifacts(project_id)

    nxt = next_resume_step(project_id, arts)
    if nxt == "youtube":
        from studio.youtube import maybe_auto_upload

        _progress(project_id, step="youtube", detail="Uploading to YouTube…", error=None)
        yt = maybe_auto_upload(
            project_id,
            progress=lambda detail: _progress(project_id, step="youtube", detail=detail, running=True),
        )
        _set_job(
            project_id,
            running=False,
            paused=False,
            step="done",
            detail=yt.get("detail") or "Resume finished.",
            error=None,
            youtube_error=yt.get("error"),
            finished_at=_now(),
        )


def _attach_live_worker(project_id: str, unpause: bool = False) -> dict | None:
    """Reuse the live thread. Resume/Start after Pause/Stop can clear the halt flag (no duplicate)."""
    live = _live_job(project_id)
    if not live.get("_alive"):
        return None
    if unpause and live.get("_stop"):
        _set_job(
            project_id,
            running=True,
            paused=False,
            _stop=None,
            error=None,
            detail="Continuing the current step...",
        )
    st = job_status(project_id)
    st["attached"] = True
    st["resumed_from"] = st.get("step") or "running"
    return st


def _launch_pipeline(project_id: str, kind: str, *, wait_gpu: bool = True) -> dict:
    load_meta(project_id)
    from studio.deps_health import refuse_job_if_deps_broken

    blocked = refuse_job_if_deps_broken()
    if blocked:
        _set_job(
            project_id,
            running=False,
            paused=False,
            step="error",
            error=blocked.get("error"),
            error_code=blocked.get("error_code"),
            detail=blocked.get("detail") or blocked.get("error"),
            finished_at=_now(),
        )
        st = job_status(project_id)
        st.update({k: blocked[k] for k in ("ok", "started", "queued", "attached", "error", "error_code", "fix_command", "dependencies") if k in blocked})
        return st

    attached = _attach_live_worker(project_id, unpause=True)
    if attached:
        attached["idempotent"] = True
        attached["noop"] = True
        attached["error_code"] = "already_running"
        attached["detail"] = attached.get("detail") or "Job already running; attached (no duplicate)."
        return attached

    artifacts = inspect_artifacts(project_id)
    nxt = next_resume_step(project_id, artifacts)
    if nxt not in ("done", "youtube"):
        from studio.gpu_lock import is_busy, wait_for_gpu

        if wait_gpu:
            wait_for_gpu(name=f"{kind}:{project_id}", kind=kind, project_id=project_id)
        elif is_busy():
            st = job_status(project_id)
            st["started"] = False
            st["gpu_lock"] = True
            st["error_code"] = "gpu_busy"
            st["detail"] = "GPU is busy; not starting a second image/TTS/render job."
            return st
    if nxt == "done":
        _set_job(
            project_id,
            running=False,
            paused=False,
            step="done",
            detail="Already complete.",
            error=None,
            resumed_from="done",
            finished_at=_now(),
        )
        st = job_status(project_id)
        st["resumed_from"] = "done"
        st["attached"] = False
        st["idempotent"] = True
        st["noop"] = True
        st["artifacts"] = _slim_artifacts(artifacts)
        return st

    verb = "Starting" if kind == "start" else "Resuming"

    def worker():
        _run_resume(project_id)

    result = start_task(
        project_id,
        kind,
        worker,
        detail=f"{verb} from {nxt}...",
        done_detail="Pipeline finished.",
        extra={"resumed_from": nxt, "step": nxt, "paused": False, "error": None},
    )
    result["resumed_from"] = nxt
    result["attached"] = False
    result["artifacts"] = _slim_artifacts(artifacts)
    result["resume_from"] = nxt
    return result


def start_project(project_id: str, *, wait_gpu: bool = True) -> dict:
    """Run the pipeline from empty / the first incomplete step. Live workers attach (no duplicate)."""
    return _launch_pipeline(project_id, "start", wait_gpu=wait_gpu)


def resume_project(project_id: str, *, wait_gpu: bool = True) -> dict:
    """Continue a job from the last successful artifact. Running jobs attach (no new thread)."""
    return _launch_pipeline(project_id, "resume", wait_gpu=wait_gpu)


def halt_project(project_id: str, mode: str = "stop") -> dict:
    """Cooperative pause/stop. Sets running=false; the worker exits at the next step boundary."""
    load_meta(project_id)
    mode = "pause" if mode == "pause" else "stop"
    live = _live_job(project_id)
    if not live.get("_alive") and not live.get("running"):
        st = job_status(project_id)
        st["detail"] = "Job is not running."
        return st
    label = "Pausing" if mode == "pause" else "Stopping"
    _set_job(
        project_id,
        running=False,
        paused=(mode == "pause"),
        _stop=mode,
        error=None,
        detail=f"{label} after the current step…",
    )
    return job_status(project_id)


def pause_project(project_id: str) -> dict:
    return halt_project(project_id, "pause")


def stop_project(project_id: str) -> dict:
    return halt_project(project_id, "stop")


def _check_stop(project_id: str) -> None:
    with _lock:
        mode = (_jobs.get(project_id) or {}).get("_stop")
    if mode:
        raise JobHalted(mode)


def provider_switch_pending(project_id: str) -> str | None:
    with _lock:
        value = (_jobs.get(project_id) or {}).get("_provider_switch")
    return str(value) if value else None


def clear_provider_switch(project_id: str) -> None:
    with _lock:
        job = _jobs.get(project_id)
        if job and "_provider_switch" in job:
            job.pop("_provider_switch", None)


def request_image_provider_switch(project_id: str, provider: str) -> dict:
    """Persist already done by caller. Cancel in-flight image gen so the worker resumes with provider."""
    from studio.illustrations import cancel_active_image_generation
    from studio.settings import image_provider_label, normalize_image_provider

    provider = normalize_image_provider(provider)
    live = _live_job(project_id)
    step = str(live.get("step") or "")
    generating = bool(live.get("_alive")) and step in ("illustrations", "cover")
    active = None
    try:
        from studio.illustrations import active_image_generation

        active = active_image_generation(project_id)
    except Exception:
        active = None
    if active:
        generating = True

    label = image_provider_label(provider)
    canceled = {"ok": True, "canceled": False}
    if generating:
        _set_job(
            project_id,
            _provider_switch=provider,
            detail=f"Switching to {label}… canceling current image",
            error=None,
        )
        canceled = cancel_active_image_generation(project_id)
        canceled["switching"] = True
        canceled["image_provider"] = provider
        canceled["detail"] = f"Switching to {label}… canceling current image"
    else:
        canceled = {
            "ok": True,
            "canceled": False,
            "switching": False,
            "image_provider": provider,
        }
    return canceled


def _progress(project_id: str, **fields) -> None:
    _check_stop(project_id)
    _set_job(project_id, **fields)


def _set_job(project_id: str, **fields) -> None:
    with _lock:
        job = _jobs.setdefault(project_id, {})
        incoming_stop = fields["_stop"] if "_stop" in fields else job.get("_stop")
        if incoming_stop and fields.get("running") is True and "_stop" not in fields:
            raise JobHalted(incoming_stop)
        new_step = fields.get("step")
        if new_step is not None and str(new_step) != str(job.get("step") or ""):
            _open_step_timing(job, str(new_step), detail=str(fields.get("detail") or job.get("detail") or ""))
        if fields.get("running") is False and job.get("active_step"):
            _close_step_timing(job)
            if "active_step" not in fields:
                fields = {**fields, "active_step": None}
        if fields.get("error") and "error_code" not in fields:
            from studio.job_errors import classify_error

            fields = {**fields, "error_code": classify_error(fields.get("error"))}
        job.update(fields)
        step = job.get("step")
        detail = job.get("detail")
        running = job.get("running")
        if "progress_pct" not in fields:
            pct = estimate_progress_pct(step, detail, running=running)
            if pct is not None:
                job["progress_pct"] = pct
            elif step in ("error", "paused", "stopped") and "progress_pct" in job:
                pass  # keep last known percent while halted
            elif not running and step != "done":
                job.pop("progress_pct", None)
        snapshot = dict(job)
    meta = load_meta(project_id)
    stored = {k: snapshot[k] for k in _PERSIST if k in snapshot}
    meta["job"] = stored
    save_meta(project_id, meta)


def _run_code(script: str, extra: list[str]) -> None:
    from studio.paths import poses_env_for_subprocess

    cmd = [sys.executable, str(CODE_DIR / script), *extra]
    env = os.environ.copy()
    env.update(poses_env_for_subprocess())
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env)
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if result.returncode != 0:
        err = (result.stderr or "").strip() or (result.stdout or "").strip() or "no output"
        raise RuntimeError(f"{script} failed (exit {result.returncode}):\n{err}")


def start_task(
    project_id: str,
    kind: str,
    worker,
    detail: str = "Starting...",
    done_detail: str = "Done.",
    extra: dict | None = None,
) -> dict:
    attached = _attach_live_worker(project_id)
    if attached:
        return attached

    token = object()

    def run():
        outcome = "done"
        detail = done_detail
        try:
            worker()
            with _lock:
                if (_jobs.get(project_id) or {}).get("_token") is not token:
                    return
                snap = dict(_jobs.get(project_id) or {})
            _set_job(
                project_id,
                running=False,
                paused=False,
                _stop=None,
                step="done",
                detail=snap.get("detail") if snap.get("step") == "done" else done_detail,
                error=snap.get("error") if snap.get("step") == "done" else None,
                youtube_error=snap.get("youtube_error"),
                resumed_from=snap.get("resumed_from"),
                finished_at=snap.get("finished_at") or _now(),
            )
            outcome = "done"
            detail = done_detail
        except JobHalted as halt:
            with _lock:
                if (_jobs.get(project_id) or {}).get("_token") is not token:
                    return
                snap = dict(_jobs.get(project_id) or {})
            paused = halt.mode == "pause"
            _set_job(
                project_id,
                running=False,
                paused=paused,
                _stop=None,
                step="paused" if paused else "stopped",
                detail="Paused after the current step." if paused else "Stopped after the current step.",
                error=None,
                resumed_from=snap.get("resumed_from"),
                finished_at=_now(),
            )
            outcome = "paused" if paused else "stopped"
            detail = outcome
        except Exception as exc:
            with _lock:
                if (_jobs.get(project_id) or {}).get("_token") is not token:
                    return
                snap = dict(_jobs.get(project_id) or {})
            _set_job(
                project_id,
                running=False,
                paused=False,
                _stop=None,
                step="error",
                error=str(exc),
                detail=str(exc),
                resumed_from=snap.get("resumed_from"),
                finished_at=_now(),
            )
            outcome = "error"
            detail = str(exc)
        finally:
            owned = False
            with _lock:
                job = _jobs.get(project_id) or {}
                if job.get("_token") is token:
                    job["_alive"] = False
                    owned = True
            if owned:
                try:
                    from studio.job_queue import on_pipeline_finished as queue_on_finished

                    queue_on_finished(project_id, outcome, detail)
                except Exception:
                    pass
                try:
                    from studio.topics import on_pipeline_finished

                    on_pipeline_finished(project_id, outcome, detail)
                except Exception:
                    pass

    fields = {
        "running": True,
        "paused": False,
        "kind": kind,
        "step": kind,
        "detail": detail,
        "error": None,
        "started_at": _now(),
        "finished_at": None,
        "_token": token,
        "_alive": True,
        "_stop": None,
    }
    if extra:
        fields.update({k: v for k, v in extra.items() if k not in ("_token", "_alive")})
    _set_job(project_id, **fields)
    threading.Thread(target=run, daemon=True, name=f"lazykh-{kind}-{project_id}").start()
    return job_status(project_id)


def _ensure_fresh_alignment(project_id: str) -> None:
    """Align when wav exists but Gentle json is missing or stale. Never skip to scheduler."""
    if project_alignment_is_fresh(project_id):
        return
    meta = load_meta(project_id)
    aspect = meta.get("aspect") or DEFAULT_ASPECT
    if needs_explainer_assets(aspect) and not _file_ok(Path(input_prefix(project_id)).with_suffix(".wav"), 1000):
        raise RuntimeError("Generate audio before aligning phonemes.")
    if project_generate_9x16(project_id) and not _file_ok(Path(shorts_input_prefix(project_id)).with_suffix(".wav"), 1000):
        raise RuntimeError("Generate 9:16 short audio before aligning phonemes.")
    _progress(project_id, step="gentle", detail="Starting Gentle (Docker if available, else local)...")
    ensure_gentle()
    _progress(project_id, step="align", detail="Aligning phonemes with Gentle...")
    align_project_audio(project_id)
    if not project_alignment_is_fresh(project_id):
        raise RuntimeError(
            "Gentle json does not match the current script and wav. "
            "Regenerate audio for this script, then align, before scheduling frames."
        )


def generate_audio_then_align(
    project_id: str,
    provider: str | None = None,
    voice_id: str | None = None,
    progress: bool = True,
) -> dict:
    """Write a new wav, drop stale json/frames/mp4, then Gentle-align before any scheduler/render."""
    from studio.settings import load_settings, normalize_tts_provider

    settings = load_settings()
    meta = load_meta(project_id)
    tts = normalize_tts_provider(
        provider
        or meta.get("tts_provider")
        or meta.get("voice_provider")
        or settings.get("tts_provider")
        or settings.get("voice_provider")
        or "local",
        default="local",
    )

    # External: reuse cached Gentle alignment when MP3 + transcript hashes match.
    if tts == "external":
        from studio.aspect import needs_explainer_assets
        from studio.tts_external import (
            EXTERNAL_AUDIO_MISSING,
            can_reuse_external_alignment,
            ensure_spoken_transcript,
            remember_external_alignment,
            require_external_mp3,
        )

        want_full = needs_explainer_assets(meta.get("aspect"))
        want_shorts = project_generate_9x16(meta)
        # Fail fast if required MP3(s) missing — never fall back to Chatterbox.
        if want_full or (not want_full and not want_shorts):
            require_external_mp3(project_id, shorts=False)
        if want_shorts:
            try:
                require_external_mp3(project_id, shorts=True)
            except RuntimeError as exc:
                # Allow shorts to reuse main MP3 only when shorts file missing? Spec: no fallback.
                raise RuntimeError(
                    f"[{EXTERNAL_AUDIO_MISSING}] tts_provider=external and generate_9x16 is on, "
                    "but narration_external_9x16.mp3 is missing. "
                    "Call upload_narration_audio(..., shorts path) or disable generate_9x16."
                ) from exc

        reuse_full = (not want_full) or can_reuse_external_alignment(project_id, shorts=False)
        reuse_shorts = (not want_shorts) or can_reuse_external_alignment(project_id, shorts=True)
        if reuse_full and reuse_shorts and project_alignment_is_fresh(project_id):
            if progress:
                _progress(
                    project_id,
                    step="align",
                    detail="Reusing cached Gentle alignment (external audio unchanged).",
                    waiting_on=None,
                )
            _log.info(
                "skip_decision=cache_reuse project_id=%s step=align source=external",
                project_id,
            )
            return {
                "ok": True,
                "reused": True,
                "audio_source": "external",
                "detail": "Cached Gentle alignment reused.",
            }

        if progress:
            _progress(
                project_id,
                step="audio",
                detail="Materializing uploaded external narration MP3…",
                waiting_on="external_audio",
            )
        ensure_spoken_transcript(project_id, shorts=False)
        if want_shorts:
            ensure_spoken_transcript(project_id, shorts=True)
        generate_project_audio(project_id, provider="external", voice_id=voice_id)
        invalidate_render_artifacts(project_id, alignment=True)
        if progress:
            _progress(project_id, step="gentle", detail="Starting Gentle (Docker if available, else local)...")
        ensure_gentle()
        if progress:
            _progress(project_id, step="align", detail="Aligning phonemes with Gentle (external audio)…", waiting_on="gentle")
        result = align_project_audio(project_id)
        if want_full or (not want_full and not want_shorts):
            remember_external_alignment(project_id, shorts=False)
        if want_shorts:
            remember_external_alignment(project_id, shorts=True)
        if not project_alignment_is_fresh(project_id):
            raise RuntimeError(
                "External audio was prepared but Gentle json still does not match the current script. "
                "Check the transcript and re-align."
            )
        result["audio_source"] = "external"
        result["reused"] = False
        if progress:
            _progress(project_id, waiting_on=None)
        return result

    if progress:
        _progress(project_id, step="audio", detail="Generating speech...")
    if tts == "local":
        from studio.gpu_lock import holding

        with holding(f"tts-local:{project_id}", kind="tts-local", project_id=project_id):
            generate_project_audio(project_id, provider=provider, voice_id=voice_id)
    else:
        generate_project_audio(project_id, provider=provider, voice_id=voice_id)
    invalidate_render_artifacts(project_id, alignment=True)
    if progress:
        _progress(project_id, step="gentle", detail="Starting Gentle (Docker if available, else local)...")
    ensure_gentle()
    if progress:
        _progress(project_id, step="align", detail="Aligning phonemes with Gentle...")
    result = align_project_audio(project_id)
    if not project_alignment_is_fresh(project_id):
        raise RuntimeError(
            "Audio was generated but Gentle json still does not match the current script. "
            "Check the transcript and re-align."
        )
    return result


def _regenerate_pictures_for_new_script(project_id: str) -> str:
    prune_stale_billboards(project_id)
    provider = project_image_provider(project_id)
    if not is_studio_image_provider(provider):
        names = mark_all_slots_pending_regen(project_id)
        _progress(
            project_id,
            step="illustrations",
            detail=f"Marked {len(names)} picture slots for ChatGPT native regen.",
        )
        return "chatgpt"
    illustration_jobs(project_id)
    if provider == "comfyui":
        from studio.comfyui import ensure_comfyui_ready

        ensure_comfyui_ready()
        _progress(project_id, step="illustrations", detail="Regenerating all cover and line images with ComfyUI...")
        generate_illustrations(
            project_id,
            force=True,
            progress=lambda detail: _progress(project_id, step="illustrations", detail=detail),
            cancel_check=lambda: _check_stop(project_id),
        )
        return "comfyui"
    ensure_fal_ready()
    _progress(project_id, step="illustrations", detail="Regenerating all cover and line images with Flux...")
    generate_illustrations(
        project_id,
        force=True,
        progress=lambda detail: _progress(project_id, step="illustrations", detail=detail),
        cancel_check=lambda: _check_stop(project_id),
    )
    return "flux"


def _after_new_script(
    project_id: str,
    regenerate_pictures: bool = True,
    regenerate_audio: bool = True,
    provider: str | None = None,
    voice_id: str | None = None,
) -> None:
    invalidate_render_artifacts(project_id, alignment=True)
    if not regenerate_pictures and not regenerate_audio:
        return
    from studio.gpu_lock import holding

    with holding(f"script-downstream:{project_id}", kind="pipeline", project_id=project_id):
        if regenerate_pictures:
            _regenerate_pictures_for_new_script(project_id)
        if regenerate_audio:
            generate_audio_then_align(project_id, provider=provider, voice_id=voice_id)


def start_script_job(
    project_id: str,
    extra: str = "",
    regenerate_pictures: bool = True,
    regenerate_audio: bool = True,
    generate_9x16: bool | None = None,
    provider: str | None = None,
    voice_id: str | None = None,
) -> dict:
    from studio.settings import (
        is_native_text_provider,
        load_settings,
        normalize_script_draft_provider,
    )
    from studio.textgen import native_script_handoff

    if generate_9x16 is not None:
        set_generate_9x16(project_id, bool(generate_9x16))

    draft = normalize_script_draft_provider(load_settings().get("script_draft_provider"))
    if is_native_text_provider() and not draft:
        return native_script_handoff(project_id, extra)

    if regenerate_pictures or regenerate_audio or project_generate_9x16(project_id):
        from studio.gpu_lock import wait_for_gpu

        wait_for_gpu(name=f"script:{project_id}", kind="pipeline", project_id=project_id)

    def worker():
        from studio.textgen import script_progress_detail

        draft_provider = draft if (is_native_text_provider() and draft) else None
        detail = (
            f"Drafting script via {draft_provider} (script_draft_provider)…"
            if draft_provider
            else script_progress_detail()
        )
        _progress(project_id, step="script", detail=detail)
        meta = load_meta(project_id)
        generated = generate_script(
            meta["topic"],
            meta["duration_seconds"],
            extra,
            aspect=meta.get("aspect") or DEFAULT_ASPECT,
            layout=project_video_layout(project_id),
            image_provider=project_image_provider(project_id),
            project_id=project_id,
            provider=draft_provider,
        )
        apply_generated_script(project_id, generated)
        if draft_provider:
            try:
                meta = load_meta(project_id)
                meta["script_draft"] = True
                meta["script_draft_provider"] = draft_provider
                from studio.projects import save_meta

                save_meta(project_id, meta)
            except Exception:
                pass
            _progress(
                project_id,
                running=False,
                step="script",
                detail=(
                    f"First-draft script saved via {draft_provider}. "
                    "Review/edit with save_script, then resume to continue."
                ),
                error=None,
            )
            return
        if project_generate_9x16(project_id):
            try:
                write_shorts_scripts(project_id)
            except Exception as exc:
                _progress(project_id, step="script", detail=f"9:16 short script note: {exc}")
        _after_new_script(
            project_id,
            regenerate_pictures=regenerate_pictures,
            regenerate_audio=regenerate_audio,
            provider=provider,
            voice_id=voice_id,
        )

    done = "Script ready."
    if regenerate_pictures and regenerate_audio:
        done = "Script, pictures, audio, and alignment ready."
    elif regenerate_pictures:
        done = "Script and pictures ready."
    elif regenerate_audio:
        done = "Script, audio, and alignment ready."
    if project_generate_9x16(project_id):
        done = done.rstrip(".") + " (9:16 hook short included)."
    return start_task(project_id, "script", worker, "Writing script...", done_detail=done)


def start_script_downstream_job(
    project_id: str,
    regenerate_pictures: bool = True,
    regenerate_audio: bool = True,
    generate_9x16: bool | None = None,
    provider: str | None = None,
    voice_id: str | None = None,
) -> dict:
    """After a script is already saved: invalidate stale lipsync, optionally regen pictures/audio+align."""
    if generate_9x16 is not None:
        set_generate_9x16(project_id, bool(generate_9x16))
    if project_generate_9x16(project_id):
        try:
            write_shorts_scripts(project_id)
        except Exception:
            pass
    invalidate_render_artifacts(project_id, alignment=True)
    if not regenerate_pictures and not regenerate_audio:
        return job_status(project_id)

    from studio.gpu_lock import holding, wait_for_gpu

    wait_for_gpu(name=f"script-downstream:{project_id}", kind="pipeline", project_id=project_id)

    def worker():
        with holding(f"script-downstream:{project_id}", kind="pipeline", project_id=project_id):
            if regenerate_pictures:
                _regenerate_pictures_for_new_script(project_id)
            if regenerate_audio:
                generate_audio_then_align(project_id, provider=provider, voice_id=voice_id)

    done = "Downstream ready."
    if regenerate_pictures and regenerate_audio:
        done = "Pictures, audio, and alignment ready."
    elif regenerate_pictures:
        done = "Pictures ready."
    elif regenerate_audio:
        done = "Audio and alignment ready."
    if project_generate_9x16(project_id):
        done = done.rstrip(".") + " (9:16 hook short included)."
    return start_task(project_id, "script", worker, "Updating pictures and audio...", done_detail=done)


def start_audio_job(project_id: str, provider: str | None = None, voice_id: str | None = None) -> dict:
    from studio.gpu_lock import holding, wait_for_gpu
    from studio.settings import load_settings, normalize_tts_provider

    settings = load_settings()
    meta = load_meta(project_id)
    tts = normalize_tts_provider(
        provider
        or meta.get("tts_provider")
        or meta.get("voice_provider")
        or settings.get("tts_provider")
        or settings.get("voice_provider")
        or "openai"
    )
    if tts == "local":
        wait_for_gpu(name=f"tts-local:{project_id}", kind="tts-local", project_id=project_id)

        def worker():
            with holding(f"tts-local:{project_id}", kind="tts-local", project_id=project_id):
                generate_audio_then_align(project_id, provider=provider, voice_id=voice_id)
    else:
        def worker():
            generate_audio_then_align(project_id, provider=provider, voice_id=voice_id)

    return start_task(project_id, "audio", worker, "Generating speech...", done_detail="Audio aligned.")


def start_illustrations_job(project_id: str) -> dict:
    from studio.illustrations import CHATGPT_COVER_HELP
    from studio.comfyui import ensure_comfyui_ready

    provider = project_image_provider(project_id)
    if provider == "chatgpt":
        raise RuntimeError(
            "This job's image_provider is chatgpt (Settings, or a per-job override). "
            "Generate the 5-second title-card cover first with ChatGPT's built-in image tool "
            "(16:9 landscape preset 1920x1080 or 9:16 portrait preset 1080x1920 matching the job; "
            "save as script_cover_16x9.png or script_cover_9x16.png, kind='cover'), then each missing line image at that job's "
            "width/height (billboard TV doodles stay 1920x1080 even on 9:16 video), "
            "and call save_illustration_image. Do not call generate_illustrations_with_flux "
            "or generate_cover — those are Flux/fal and ChatGPT jobs do not need FAL_KEY. "
            + CHATGPT_COVER_HELP
        )
    illustration_jobs(project_id)
    if provider == "comfyui":
        ensure_comfyui_ready()
        label = "Generating title-card cover and ComfyUI backgrounds..."
        lock_kind = "comfyui"
    else:
        ensure_fal_ready()
        label = "Generating title-card cover and Flux backgrounds..."
        lock_kind = "flux"

    def worker():
        from studio.gpu_lock import holding

        with holding(f"illustrations:{project_id}", kind=lock_kind, project_id=project_id):
            _progress(project_id, step="illustrations", detail=label)
            generate_illustrations(
                project_id,
                progress=lambda detail: _progress(project_id, step="illustrations", detail=detail),
                cancel_check=lambda: _check_stop(project_id),
            )

    from studio.gpu_lock import wait_for_gpu

    wait_for_gpu(name=f"illustrations:{project_id}", kind=lock_kind, project_id=project_id)
    return start_task(
        project_id,
        "illustrations",
        worker,
        label,
        done_detail="Cover and backgrounds ready.",
    )


def start_regenerate_illustration_job(
    project_id: str,
    filename: str | None = None,
    index: int | None = None,
    kind: str | None = None,
) -> dict:
    """Regenerate one picture. Flux/ComfyUI queue a GPU job; ChatGPT marks the slot for native save."""
    if is_busy(project_id):
        raise RuntimeError("This job is already running. Stop or wait before regenerating a picture.")
    provider = project_image_provider(project_id)
    from studio.illustrations import (
        COVER_POSE_NONCE_KEY,
        bump_cover_pose_nonce,
        commit_cover_to_history,
        is_cover_filename,
    )
    from studio.projects import cover_provider_is_agent, load_meta, project_cover_provider

    slot, _info = resolve_illustration_slot(project_id, filename=filename, index=index, kind=kind)
    fname = slot.get("filename") or "illustration.png"
    slot_kind = "cover" if slot.get("role") == "cover" or is_cover_filename(fname) else "line"
    if slot_kind == "cover":
        try:
            canvas = slot.get("video_aspect") or slot.get("aspect")
            commit_cover_to_history(project_id, canvas)
        except Exception:
            pass
        bump_cover_pose_nonce(project_id)
        # Refresh prompt with new pose after nonce bump.
        slot, _info = resolve_illustration_slot(project_id, filename=fname, kind="cover")

    # Cover slots: honor cover_provider (manual/chatgpt → native; never fal).
    effective_provider = provider
    if slot_kind == "cover":
        effective_provider = project_cover_provider(project_id)
        if cover_provider_is_agent(project_id) or effective_provider == "chatgpt":
            from studio.illustrations import regenerate_illustration

            return regenerate_illustration(project_id, filename=fname, index=index, kind="cover")

    if provider == "chatgpt" and slot_kind != "cover":
        from studio.illustrations import regenerate_illustration

        return regenerate_illustration(project_id, filename=fname, index=index, kind=slot_kind)

    if effective_provider == "comfyui" or (slot_kind != "cover" and provider == "comfyui"):
        from studio.comfyui import ensure_comfyui_ready

        ensure_comfyui_ready()
        verb = "ComfyUI"
        run_provider = "comfyui"
    else:
        ensure_fal_ready()
        verb = "Flux"
        run_provider = "flux"

    def worker():
        from studio.gpu_lock import holding
        from studio.illustrations import regenerate_illustration

        with holding(f"illustrations:{project_id}", kind=verb.lower(), project_id=project_id):
            _progress(project_id, step="illustrations", detail=f"Regenerating {fname} with {verb}...")
            regenerate_illustration(
                project_id,
                filename=fname,
                kind=slot_kind,
                progress=lambda detail: _progress(project_id, step="illustrations", detail=detail),
            )

    from studio.gpu_lock import wait_for_gpu

    wait_for_gpu(name=f"illustrations:{project_id}", kind=verb.lower(), project_id=project_id)
    status = start_task(
        project_id,
        "illustrations",
        worker,
        f"Regenerating {fname} with {verb}...",
        done_detail=f"Regenerated {fname}.",
        extra={"regenerate_filename": fname},
    )
    out = {
        **status,
        "ok": True,
        "queued": True,
        "native": False,
        "image_provider": provider,
        "cover_provider": project_cover_provider(project_id) if slot_kind == "cover" else None,
        "run_provider": run_provider,
        "filename": fname,
        "kind": slot_kind,
        "index": slot.get("index"),
    }
    if slot_kind == "cover":
        try:
            out["pose_nonce"] = load_meta(project_id).get(COVER_POSE_NONCE_KEY)
        except Exception:
            pass
    return out

def start_align_job(project_id: str) -> dict:
    def worker():
        _progress(project_id, step="gentle", detail="Starting Gentle (Docker if available, else local)...")
        ensure_gentle()
        _progress(project_id, step="align", detail="Aligning phonemes with Gentle...")
        align_project_audio(project_id)

    return start_task(project_id, "align", worker, "Aligning phonemes...", done_detail="Alignment ready.")


def _render_one_canvas(
    project_id: str,
    canvas: str,
    *,
    job_aspect: str,
    prefix: str,
    video_layout: str,
    character_size: str,
    include_bubblehead: bool,
    use_billboards: bool,
    keep_frames: bool,
    skip_completed: bool,
    room_path,
) -> None:
    """Draw + mux one aspect. Does not delete the other aspect's cover or mp4."""
    canvas = normalize_aspect(canvas)
    other_aspect = "9:16" if canvas == "16:9" else "16:9"
    other_video = final_video_path(project_id, other_aspect)
    other_video_size = other_video.stat().st_size if other_video.is_file() else None
    other_cover = cover_path(project_id, other_aspect)
    other_cover_size = other_cover.stat().st_size if other_cover.is_file() else None
    # 9:16 = hook-only short with its own script/audio/align and portrait line art.
    if canvas == ASPECT_9_16:
        write_shorts_scripts(project_id)
        render_prefix = shorts_input_prefix(project_id)
        # Prefer full-bleed cover layout for shorts (portrait art); TV billboard looks wrong.
        canvas_layout = "cover"
        if not alignment_is_fresh(project_id, shorts=True):
            if not _file_ok(Path(render_prefix).with_suffix(".wav"), 1000):
                generate_audio(project_id, shorts=True)
                invalidate_render_artifacts(project_id, alignment=True)
            ensure_gentle()
            align_audio(project_id, shorts=True)
        if not alignment_is_fresh(project_id, shorts=True):
            raise RuntimeError(
                "9:16 short Gentle json does not match script_9x16 wav/transcript. "
                "Regenerate audio for the hook short, then align."
            )
    else:
        render_prefix = prefix
        canvas_layout = video_layout
    provider = project_image_provider(project_id)
    if provider == "chatgpt":
        backend = "ChatGPT native"
    elif provider == "comfyui":
        backend = "ComfyUI"
    else:
        backend = "Flux"
    _progress(
        project_id,
        running=True,
        step="cover",
        detail=f"Checking {canvas} title-card cover ({backend})...",
        error=None,
    )
    ensure_cover_for_aspect(
        project_id,
        aspect=canvas,
        progress=lambda detail: _progress(project_id, step="cover", detail=detail),
    )
    cover = cover_path(project_id, canvas)
    if not cover_matches_canvas(cover, canvas):
        raise RuntimeError(
            f"Title-card cover for {canvas} is missing or the wrong size. "
            f"Need {cover.name} at the canvas — do not stretch the other aspect. "
            "Generate it on Pictures (Flux, ComfyUI, or ChatGPT native) before rendering."
        )
    # Ensure 9:16 hook line art exists (portrait) before drawing frames.
    if canvas == ASPECT_9_16:
        shorts_folder = billboards_dir(project_id, ASPECT_9_16)
        info = illustration_jobs(project_id)
        missing_shorts = [
            job
            for job in info.get("jobs") or []
            if job.get("role") == "shorts_line" and not job.get("has_image")
        ]
        if missing_shorts and is_studio_image_provider(provider):
            _progress(
                project_id,
                step="illustrations",
                detail=f"Generating {len(missing_shorts)} 9:16 short line image(s)...",
            )
            generate_illustrations(
                project_id,
                progress=lambda detail: _progress(project_id, step="illustrations", detail=detail),
                cancel_check=lambda: _check_stop(project_id),
            )
            info = illustration_jobs(project_id)
            missing_shorts = [
                job
                for job in info.get("jobs") or []
                if job.get("role") == "shorts_line" and not job.get("has_image")
            ]
        if missing_shorts:
            names = ", ".join(job.get("filename") or "?" for job in missing_shorts[:6])
            raise RuntimeError(
                f"9:16 short is missing portrait line art in {shorts_folder.name}: {names}. "
                "Generate illustrations before rendering the short."
            )
    video_path = final_video_path(project_id, canvas)
    frames_path = frames_dir(project_id, canvas)
    video_ready = skip_completed and _file_ok(video_path, 1000)
    if not video_ready:
        _check_stop(project_id)
        frames_ready = skip_completed and _has_frames(frames_path)
        if not frames_ready:
            _progress(project_id, step="schedule", detail="Building pose/phoneme schedule...")
            _run_code("scheduler.py", ["--input_file", render_prefix])
            _check_stop(project_id)
            _progress(project_id, step="frames", detail=f"Drawing {canvas} frames...")
            _run_code(
                "videoDrawer.py",
                [
                    "--input_file", render_prefix,
                    "--use_billboards", "T" if use_billboards else "F",
                    "--layout", canvas_layout,
                    "--character_size", character_size,
                    "--include_bubblehead", "T" if include_bubblehead else "F",
                    "--jiggly_transitions", "F",
                    "--aspect", canvas,
                    "--background", str(room_path),
                    "--frames_dir", str(frames_path),
                ],
            )
        else:
            _progress(project_id, step="ffmpeg", detail=f"{canvas} frames already exist; muxing video...")
        _check_stop(project_id)
        from studio.deps_health import detect_video_encoder

        enc = detect_video_encoder()
        _progress(
            project_id,
            step="ffmpeg",
            detail=f"Muxing 5s {canvas} cover still, then video and audio ({enc.get('encoder')}/{enc.get('preset')})...",
            encoder=enc.get("encoder"),
            encoder_preset=enc.get("preset"),
            waiting_on="ffmpeg",
        )
        _run_code(
            "videoFinisher.py",
            [
                "--input_file", render_prefix,
                "--keep_frames", "T" if keep_frames else "F",
                "--aspect", canvas,
                "--cover", str(cover),
                "--cover_seconds", "5",
                "--frames_dir", str(frames_path),
                "--output", str(video_path),
            ],
        )
        _set_job(
            project_id,
            encoder=enc.get("encoder"),
            encoder_preset=enc.get("preset"),
            waiting_on=None,
        )
        _check_stop(project_id)
        _progress(project_id, step="music", detail=f"Mixing looped background music under the {canvas} video...")
        apply_project_music(project_id, video_path)
        publish_last_video(project_id, video_path)
        record_render(project_id, canvas, video_path, job_aspect=job_aspect)
        if other_video_size is not None:
            if not other_video.is_file() or other_video.stat().st_size != other_video_size:
                raise RuntimeError(f"Render of {canvas} must not change {other_video.name}.")
        if other_cover_size is not None:
            if not other_cover.is_file() or other_cover.stat().st_size != other_cover_size:
                raise RuntimeError(f"Render of {canvas} must not change {other_cover.name}.")
        try:
            ensure_thumbnail(project_id)
        except Exception:
            pass
    else:
        record_render(project_id, canvas, video_path, job_aspect=job_aspect)
        publish_last_video(project_id, video_path)


def render_video(
    project_id: str,
    use_billboards: bool = True,
    keep_frames: bool = False,
    generate_missing_audio: bool = False,
    aspect: str | None = None,
    layout: str | None = None,
    character_size: str | None = None,
    include_bubblehead: bool | None = None,
    shuffle_music: bool = False,
    skip_completed: bool = False,
) -> dict:
    warnings = asset_warnings()
    ensure_fallback_background()
    prefix = input_prefix(project_id)
    tagged = Path(prefix + ".txt")
    if not tagged.is_file():
        raise RuntimeError("Generate a script before rendering.")
    meta = load_meta(project_id)
    job_aspect = normalize_job_aspect(aspect or meta.get("aspect") or DEFAULT_ASPECT)
    targets = list(aspects_to_render(job_aspect))
    video_layout = normalize_video_layout(
        layout or meta.get("video_layout") or load_settings().get("video_layout")
    )
    try:
        character_size = normalize_character_size(
            character_size or meta.get("character_size") or load_settings().get("character_size")
        )
    except RuntimeError:
        character_size = project_character_size(project_id)
    if include_bubblehead is None:
        include_bubblehead = project_include_bubblehead(meta)
    else:
        include_bubblehead = bool(include_bubblehead)
    dirty = False
    if meta.get("aspect") != job_aspect:
        meta["aspect"] = job_aspect
        dirty = True
    if meta.get("video_layout") != video_layout:
        meta["video_layout"] = video_layout
        dirty = True
    if meta.get("character_size") != character_size:
        meta["character_size"] = character_size
        dirty = True
    if meta.get("include_bubblehead") != include_bubblehead:
        meta["include_bubblehead"] = include_bubblehead
        dirty = True
    if dirty:
        save_meta(project_id, meta)
    migrate_legacy_aspect_files(project_id)
    room_path = resolve_room_path(meta.get("background_file"))
    print(f"Studio room: {room_path}")
    assign_project_music(project_id, reshuffle=shuffle_music)
    wav = Path(prefix + ".wav")
    if needs_explainer_assets(job_aspect) and not wav.is_file():
        if generate_missing_audio:
            generate_audio_then_align(project_id)
        else:
            raise RuntimeError("Generate audio before rendering.")
    elif project_generate_9x16(project_id) and not Path(shorts_input_prefix(project_id) + ".wav").is_file():
        if generate_missing_audio:
            generate_audio_then_align(project_id)
        else:
            raise RuntimeError("Generate 9:16 short audio before rendering.")
    else:
        _ensure_fresh_alignment(project_id)
    if not project_alignment_is_fresh(project_id):
        raise RuntimeError(
            "Cannot run scheduler: Gentle json does not match the current script and wav. "
            "Generate audio (which aligns phonemes) before drawing frames."
        )
    completed: list[str] = []
    errors: list[str] = []
    for canvas in targets:
        _check_stop(project_id)
        try:
            _progress(
                project_id,
                running=True,
                step="render",
                detail=f"Rendering {canvas} ({len(completed) + 1}/{len(targets)})...",
                error=None,
            )
            _render_one_canvas(
                project_id,
                canvas,
                job_aspect=job_aspect,
                prefix=prefix,
                video_layout=video_layout,
                character_size=character_size,
                include_bubblehead=include_bubblehead,
                use_billboards=use_billboards,
                keep_frames=keep_frames,
                skip_completed=skip_completed,
                room_path=room_path,
            )
            completed.append(canvas)
        except JobHalted:
            raise
        except Exception as exc:
            errors.append(f"{canvas}: {exc}")
            _progress(
                project_id,
                running=True,
                step="render",
                detail=f"{canvas} failed: {exc}",
                error=str(exc),
            )
    if not completed:
        raise RuntimeError("Render failed. " + " ".join(errors))
    if len(targets) > 1 and not errors:
        done_detail = "16:9 and 9:16 videos are ready."
    elif errors:
        done_detail = f"Rendered {', '.join(completed)}. Failed: {'; '.join(errors)}"
    else:
        done_detail = "Video is ready."
    render_error = "; ".join(errors) if errors else None
    yt_error = None
    try:
        from studio.youtube import job_auto_upload, maybe_auto_upload

        _check_stop(project_id)
        if _youtube_uploaded(project_id):
            yt = {"skipped": True, "reason": "already_uploaded"}
        else:
            if job_auto_upload(project_id):
                _progress(project_id, running=True, step="youtube", detail="Uploading to YouTube…", error=None)
            yt = maybe_auto_upload(
                project_id,
                progress=lambda detail: _progress(project_id, step="youtube", detail=detail, running=True),
            )
        if yt.get("detail") and not yt.get("skipped"):
            done_detail = yt["detail"] if not render_error else f"{yt['detail']} Render notes: {render_error}"
        yt_error = yt.get("error")
    except JobHalted:
        raise
    except Exception as exc:
        yt_error = str(exc)
        done_detail = f"{done_detail} YouTube upload failed: {exc}"
        try:
            meta = load_meta(project_id)
            meta["youtube_error"] = yt_error
            save_meta(project_id, meta)
        except Exception:
            pass
    _set_job(
        project_id,
        running=False,
        paused=False,
        _stop=None,
        step="done",
        detail=done_detail,
        error=render_error,
        youtube_error=yt_error,
        finished_at=_now(),
    )
    payload = attach_job(project_payload(project_id))
    payload["warnings"] = warnings
    return payload


def start_render_thread(project_id: str, **kwargs) -> dict:
    from studio.gpu_lock import holding, wait_for_gpu

    wait_for_gpu(name=f"render:{project_id}", kind="render", project_id=project_id)

    def worker():
        with holding(f"render:{project_id}", kind="render", project_id=project_id):
            render_video(project_id, **kwargs)

    return start_task(project_id, "render", worker, "Starting render...", done_detail="Video is ready.")
