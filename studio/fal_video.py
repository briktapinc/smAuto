"""fal.ai image-to-video clips from existing picture slots.

Each ready PNG can become a sibling .mp4. The final render uses that clip
in place of the still (title card and line art).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen

from studio.illustrations import (
    illustration_jobs,
    is_cover_filename,
    resolve_illustration_slot,
)
from studio.settings import load_settings, normalize_fal_video_model

log = logging.getLogger("studio.fal_video")

# Curated image-to-video endpoints. image_key is the fal argument that takes the still.
FAL_VIDEO_MODELS: tuple[dict[str, str], ...] = (
    {
        "id": "fal-ai/kling-video/v3/standard/image-to-video",
        "label": "Kling 3.0 Standard",
        "image_key": "start_image_url",
        "duration": "5",
        "audio": "off",
    },
    {
        "id": "fal-ai/kling-video/v2.6/pro/image-to-video",
        "label": "Kling 2.6 Pro",
        "image_key": "start_image_url",
        "duration": "5",
        "audio": "off",
    },
    {
        "id": "fal-ai/ltx-2/image-to-video/fast",
        "label": "LTX-2 Fast",
        "image_key": "image_url",
        "duration": "6",
        "audio": "",
    },
    {
        "id": "fal-ai/minimax/video-01/image-to-video",
        "label": "MiniMax Video-01",
        "image_key": "image_url",
        "duration": "",
        "audio": "",
    },
    {
        "id": "fal-ai/veo3.1/fast/image-to-video",
        "label": "Veo 3.1 Fast",
        "image_key": "image_url",
        "duration": "4s",
        "audio": "",
    },
)

DEFAULT_FAL_VIDEO_MODEL = FAL_VIDEO_MODELS[0]["id"]
# Rough planning number for spend caps — actual fal invoices vary by model.
FAL_VIDEO_USD_ESTIMATE = 0.40


def fal_video_catalog() -> list[dict[str, str]]:
    return [dict(row) for row in FAL_VIDEO_MODELS]


def model_spec(model_id: str | None = None) -> dict[str, str]:
    chosen = normalize_fal_video_model(model_id or load_settings().get("fal_video_model"))
    for row in FAL_VIDEO_MODELS:
        if row["id"] == chosen:
            return dict(row)
    return dict(FAL_VIDEO_MODELS[0])


def clip_path_for_still(still: Path) -> Path:
    return still.with_suffix(".mp4")


def clip_is_ready(path: Path | None) -> bool:
    try:
        return bool(path and path.is_file() and path.stat().st_size > 1000)
    except OSError:
        return False


def _motion_prompt(slot: dict) -> str:
    scene = (slot.get("topic") or slot.get("line") or slot.get("prompt") or "").strip()
    scene = " ".join(scene.split())
    if len(scene) > 280:
        scene = scene[:280].rstrip() + "…"
    base = (
        "Subtle natural motion of this existing illustration. "
        "Keep the same subjects, framing, colors, and style. "
        "No new text, no logos, no cut, no extra people."
    )
    if slot.get("role") == "cover" or is_cover_filename(slot.get("filename") or ""):
        base = (
            "Gentle motion on this title card. Keep any lettering readable "
            "and the character in place. No extra text, no cut."
        )
    return f"{base} Scene: {scene}" if scene else base


def _video_url(result: Any) -> str:
    data = result if isinstance(result, dict) else None
    if data is None and hasattr(result, "keys"):
        try:
            data = dict(result)
        except Exception:
            data = None
    if not data:
        raise RuntimeError(f"fal video returned no payload: {result!r}")
    video = data.get("video")
    if isinstance(video, dict) and video.get("url"):
        return str(video["url"])
    if isinstance(video, str) and video.startswith("http"):
        return video
    videos = data.get("videos") or []
    if videos:
        first = videos[0]
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"])
    raise RuntimeError(f"fal video result had no video URL: {list(data)[:12]}")


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urlopen(url, timeout=180) as resp:  # nosec — fal result URL
        data = resp.read()
    if len(data) < 1000:
        raise RuntimeError(f"Downloaded clip is too small ({len(data)} bytes).")
    tmp.write_bytes(data)
    tmp.replace(dest)


def convert_slot_to_clip(
    project_id: str,
    slot: dict,
    *,
    model_id: str | None = None,
    note: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Turn one ready picture PNG into a sibling mp4 via the selected fal model."""
    from studio.illustrations import _configure_fal

    still = Path(slot.get("save_path") or "")
    if not still.is_file():
        raise RuntimeError(f"Picture is missing: {slot.get('filename') or still.name}")
    client = _configure_fal()
    spec = model_spec(model_id)
    image_url = client.upload_file(str(still))
    arguments: dict[str, Any] = {
        "prompt": _motion_prompt(slot),
        spec["image_key"]: image_url,
    }
    if spec.get("duration"):
        arguments["duration"] = spec["duration"]
    if spec.get("audio") == "off":
        arguments["generate_audio"] = False
    label = slot.get("filename") or still.name
    if note:
        note(f"Converting {label} with {spec['label']}…")
    log.info("fal video %s model=%s still=%s", label, spec["id"], still)
    result = client.subscribe(spec["id"], arguments=arguments, with_logs=False)
    video_url = _video_url(result)
    dest = clip_path_for_still(still)
    _download(video_url, dest)
    if note:
        note(f"Saved clip {dest.name}")
    return {
        "ok": True,
        "filename": label,
        "clip": dest.name,
        "clip_path": str(dest),
        "bytes": dest.stat().st_size,
        "model": spec["id"],
        "model_label": spec["label"],
    }


def slots_for_motion(
    project_id: str,
    *,
    filename: str | None = None,
    index: int | None = None,
    kind: str | None = None,
    all_slots: bool = False,
    redo: bool = False,
) -> list[dict]:
    if not all_slots:
        slot, _info = resolve_illustration_slot(
            project_id, filename=filename, index=index, kind=kind
        )
        if not slot.get("ready"):
            raise RuntimeError(f"{slot.get('filename') or 'Picture'} has no image yet.")
        clip = clip_path_for_still(Path(slot.get("save_path") or ""))
        if clip_is_ready(clip) and not redo:
            return []
        return [slot]
    info = illustration_jobs(project_id)
    out: list[dict] = []
    for job in info.get("jobs") or []:
        if not job.get("ready"):
            continue
        clip = clip_path_for_still(Path(job.get("save_path") or ""))
        if clip_is_ready(clip) and not redo:
            continue
        out.append(job)
    return out


def convert_slots(
    project_id: str,
    slots: list[dict],
    *,
    model_id: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    done: list[dict] = []
    errors: list[dict[str, str]] = []
    total = len(slots)
    for i, slot in enumerate(slots, start=1):
        name = slot.get("filename") or f"slot-{i}"

        def _note(msg: str, *, _i=i, _n=name) -> None:
            if progress:
                progress(f"({_i}/{total}) {msg}")

        try:
            done.append(convert_slot_to_clip(project_id, slot, model_id=model_id, note=_note))
        except Exception as exc:
            log.warning("clip failed %s: %s", name, exc)
            errors.append({"filename": name, "error": str(exc)})
            if progress:
                progress(f"({i}/{total}) {name} failed: {exc}")
    if not done and errors:
        raise RuntimeError(errors[0]["error"])
    return {
        "ok": True,
        "converted": done,
        "count": len(done),
        "errors": errors,
        "model": model_spec(model_id)["id"],
    }
