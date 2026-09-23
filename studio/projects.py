from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from studio.aspect import (
    ALL_ASPECTS,
    ASPECT_BOTH,
    ASPECT_9_16,
    DEFAULT_ASPECT,
    SHORTS_STEM,
    aspects_to_render,
    canvas_size,
    cover_filename,
    final_video_filename,
    frames_dirname,
    needs_shorts_assets,
    normalize_aspect,
    normalize_job_aspect,
)
from studio.backgrounds import default_background, resolve_background
from studio.paths import DELETED_IDS_PATH, PROJECTS_DIR, ensure_dirs
from studio.script_rules import ALLOWED_EMOTIONS
from studio.settings import (
    default_voice_for_provider,
    load_settings,
    normalize_character_size,
    normalize_cover_provider,
    normalize_image_provider,
    normalize_tts_provider,
    normalize_video_layout,
    normalize_youtube_auto_upload,
    normalize_youtube_privacy,
    resolve_cover_provider,
)
from studio.utils_script import (
    build_shorts_script,
    parse_tagged_script,
    raw_from_tagged,
    script_structure_warnings,
)

INPUT_STEM = "script"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:60] or "video"


def _safe_project_id(project_id: str) -> str:
    pid = (project_id or "").strip()
    if not pid or Path(pid).name != pid:
        raise FileNotFoundError(f"Unknown project: {project_id}")
    return pid


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / _safe_project_id(project_id)


def input_prefix(project_id: str) -> str:
    return str(project_dir(project_id) / INPUT_STEM)


def shorts_input_prefix(project_id: str) -> str:
    """Prefix for the 9:16 hook-only short: script_9x16.txt / .wav / .json / _billboards."""
    return str(project_dir(project_id) / SHORTS_STEM)


def billboards_dir(project_id: str, aspect: str | None = None) -> Path:
    """Line art folder. 9:16 shorts use script_9x16_billboards; 16:9 uses script_billboards."""
    if aspect is not None and normalize_aspect(aspect) == ASPECT_9_16:
        path = Path(shorts_input_prefix(project_id) + "_billboards")
    else:
        path = Path(input_prefix(project_id) + "_billboards")
    path.mkdir(parents=True, exist_ok=True)
    return path


def cover_path(project_id: str, aspect: str | None = None) -> Path:
    """Aspect-specific title-card: script_cover_16x9.png / script_cover_9x16.png."""
    if aspect is None:
        try:
            aspect = load_meta(project_id).get("aspect")
        except FileNotFoundError:
            aspect = DEFAULT_ASPECT
    return project_dir(project_id) / cover_filename(aspect)


def legacy_cover_path(project_id: str) -> Path:
    """script_cover.png — alias of the last/current aspect cover for older callers."""
    return Path(input_prefix(project_id) + "_cover.png")


def final_video_path(project_id: str, aspect: str | None = None) -> Path:
    if aspect is None:
        try:
            aspect = load_meta(project_id).get("aspect")
        except FileNotFoundError:
            aspect = DEFAULT_ASPECT
    return project_dir(project_id) / final_video_filename(aspect)


def youtube_watch_info(meta: dict | None) -> dict[str, Any]:
    """Public YouTube link fields for library/watch when the local mp4 was removed."""
    meta = meta or {}
    blob = meta.get("youtube") if isinstance(meta.get("youtube"), dict) else {}
    video_id = str(blob.get("video_id") or blob.get("id") or meta.get("youtube_video_id") or "").strip()
    url = str(blob.get("url") or "").strip()
    if not url and video_id:
        url = f"https://youtu.be/{video_id}"
    if not video_id and "youtu.be/" in url:
        video_id = url.rsplit("youtu.be/", 1)[-1].split("?")[0].split("/")[0].strip()
    elif not video_id and "watch?v=" in url:
        video_id = url.split("watch?v=", 1)[-1].split("&")[0].strip()
    return {
        "has_youtube": bool(url or video_id),
        "youtube_url": url or None,
        "youtube_video_id": video_id or None,
        "youtube_privacy": blob.get("privacy") or meta.get("youtube_privacy"),
    }


def last_video_path(project_id: str) -> Path:
    """script_final.mp4 — copy of the last rendered aspect (never the only copy)."""
    return Path(input_prefix(project_id) + "_final.mp4")


def frames_dir(project_id: str, aspect: str | None = None) -> Path:
    if aspect is None:
        try:
            aspect = load_meta(project_id).get("aspect")
        except FileNotFoundError:
            aspect = DEFAULT_ASPECT
    return project_dir(project_id) / frames_dirname(aspect)


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def cover_matches_canvas(path: Path, aspect: str | None) -> bool:
    """True only when the PNG exists and is exactly the canvas (never a stretched other aspect)."""
    if not path.is_file() or path.stat().st_size < 200:
        return False
    return png_size(path) == canvas_size(aspect)


def _copy_file(src: Path, dest: Path) -> None:
    if not src.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dest.exists() and src.resolve() == dest.resolve():
            return
    except OSError:
        pass
    tmp = dest.with_name(dest.name + ".tmpcopy")
    shutil.copy2(src, tmp)
    tmp.replace(dest)


def publish_last_video(project_id: str, source: Path) -> Path:
    alias = last_video_path(project_id)
    _copy_file(source, alias)
    return alias


def publish_cover_alias(project_id: str, source: Path, aspect: str | None = None) -> Path:
    """Refresh script_cover.png only when this cover is the job's current aspect."""
    meta = load_meta(project_id)
    current = normalize_job_aspect(meta.get("aspect") or DEFAULT_ASPECT)
    wanted = normalize_aspect(aspect or current)
    alias = legacy_cover_path(project_id)
    if current != ASPECT_BOTH and wanted != normalize_aspect(current):
        return alias
    _copy_file(source, alias)
    return alias


def migrate_legacy_aspect_files(project_id: str) -> None:
    """Copy legacy script_final.mp4 / script_cover.png into aspect-specific names. Never stretch."""
    try:
        meta = load_meta(project_id)
    except FileNotFoundError:
        return
    aspect = normalize_aspect(meta.get("aspect") or DEFAULT_ASPECT)
    legacy_video = last_video_path(project_id)
    specific_videos = [final_video_path(project_id, asp) for asp in ALL_ASPECTS]
    if (
        legacy_video.is_file()
        and legacy_video.stat().st_size >= 1000
        and not any(p.is_file() and p.stat().st_size >= 1000 for p in specific_videos)
    ):
        dest = final_video_path(project_id, aspect)
        _copy_file(legacy_video, dest)

    legacy_cover = legacy_cover_path(project_id)
    if legacy_cover.is_file() and legacy_cover.stat().st_size > 200:
        size = png_size(legacy_cover)
        for asp in ALL_ASPECTS:
            dest = cover_path(project_id, asp)
            if dest.is_file() and dest.stat().st_size > 200:
                continue
            if size == canvas_size(asp):
                _copy_file(legacy_cover, dest)


def collect_renders(project_id: str) -> dict[str, Any]:
    """Disk-backed map of 16:9 / 9:16 outputs. Other aspect files are never implied missing."""
    migrate_legacy_aspect_files(project_id)
    try:
        meta = load_meta(project_id)
    except FileNotFoundError:
        meta = {}
    stored = meta.get("renders") if isinstance(meta.get("renders"), dict) else {}
    out: dict[str, Any] = {}
    for asp in ALL_ASPECTS:
        video = final_video_path(project_id, asp)
        cover = cover_path(project_id, asp)
        ready = video.is_file() and video.stat().st_size >= 1000
        blob = dict(stored.get(asp) or {}) if isinstance(stored.get(asp), dict) else {}
        blob.update(
            {
                "file": video.name,
                "path": str(video),
                "ready": ready,
                "bytes": video.stat().st_size if ready else 0,
                "cover": cover.name,
                "cover_path": str(cover),
                "cover_ready": cover_matches_canvas(cover, asp),
                "frames": frames_dirname(asp),
            }
        )
        out[asp] = blob
    return out


def record_render(
    project_id: str,
    aspect: str,
    video_path: Path,
    *,
    job_aspect: str | None = None,
) -> dict[str, Any]:
    """Record one canvas mp4. Leaves the other aspect's meta + file untouched."""
    canvas = normalize_aspect(aspect)
    meta = load_meta(project_id)
    stored = normalize_job_aspect(job_aspect if job_aspect is not None else meta.get("aspect"))
    renders = meta.get("renders") if isinstance(meta.get("renders"), dict) else {}
    other = {key: val for key, val in renders.items() if normalize_aspect(key) != canvas}
    entry = {
        "file": video_path.name,
        "updated_at": _now(),
        "bytes": video_path.stat().st_size if video_path.is_file() else 0,
        "cover": cover_filename(canvas),
    }
    other[canvas] = entry
    meta["renders"] = other
    meta["last_render_aspect"] = canvas
    meta["aspect"] = stored
    meta["status"] = "rendered"
    save_meta(project_id, meta)
    return entry


def load_meta(project_id: str) -> dict[str, Any]:
    path = project_dir(project_id) / "meta.json"
    if not path.is_file():
        raise FileNotFoundError(f"Unknown project: {project_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def save_meta(project_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    meta["updated_at"] = _now()
    path = project_dir(project_id) / "meta.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def load_deleted_ids() -> set[str]:
    ensure_dirs()
    if not DELETED_IDS_PATH.is_file():
        return set()
    try:
        data = json.loads(DELETED_IDS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(data, list):
        return {str(x) for x in data if x}
    return set()


def save_deleted_ids(ids: set[str]) -> None:
    ensure_dirs()
    DELETED_IDS_PATH.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")


def is_listed_project(project_id: str) -> bool:
    try:
        pid = _safe_project_id(project_id)
        meta = load_meta(pid)
    except FileNotFoundError:
        return False
    if meta.get("deleted"):
        return False
    return pid not in load_deleted_ids()


def list_projects() -> list[dict[str, Any]]:
    ensure_dirs()
    hidden = load_deleted_ids()
    items = []
    for folder in sorted(PROJECTS_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not folder.is_dir():
            continue
        meta_path = folder / "meta.json"
        if not meta_path.is_file():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        pid = str(meta.get("id") or folder.name)
        if meta.get("deleted") or pid in hidden:
            continue
        items.append(meta)
    return items


def _rmtree(folder: Path) -> None:
    def _onerror(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    shutil.rmtree(folder, onerror=_onerror)
    if folder.exists():
        raise RuntimeError(f"Could not delete project folder: {folder}")


def delete_project(project_id: str, delete_files: bool = False) -> dict[str, Any]:
    pid = _safe_project_id(project_id)
    folder = project_dir(pid)
    hidden = load_deleted_ids()
    exists = folder.exists()
    if not exists and pid not in hidden:
        raise FileNotFoundError(f"Unknown project: {project_id}")

    if delete_files:
        if exists:
            _rmtree(folder)
        hidden.discard(pid)
        save_deleted_ids(hidden)
        return {"id": pid, "deleted": True, "files_deleted": True, "folder": str(folder)}

    if (folder / "meta.json").is_file():
        meta = load_meta(pid)
        meta["deleted"] = True
        save_meta(pid, meta)
    hidden.add(pid)
    save_deleted_ids(hidden)
    return {"id": pid, "deleted": True, "files_deleted": False, "folder": str(folder)}


def rename_video(
    project_id: str,
    title: str,
    *,
    topic: str | None = None,
    rename_folder: bool = False,
    update_youtube: bool = True,
) -> dict[str, Any]:
    """Rename a Studio video job's display title (and optionally folder id / YouTube title)."""
    pid = _safe_project_id(project_id)
    new_title = (title or "").strip()
    if not new_title:
        raise RuntimeError("title is required.")
    if len(new_title) > 100:
        new_title = new_title[:100].rstrip()

    try:
        from studio.pipeline import is_busy

        if is_busy(pid):
            raise RuntimeError(
                f"Project {pid} is running. Stop or wait before renaming."
            )
    except ImportError:
        pass

    meta = load_meta(pid)
    old_title = (meta.get("title") or meta.get("topic") or pid).strip()
    meta["title"] = new_title
    if topic is not None and str(topic).strip():
        meta["topic"] = str(topic).strip()
    save_meta(pid, meta)

    new_id = pid
    folder_moved = False
    if rename_folder:
        base = slugify(new_title)
        candidate = base
        if candidate != pid and project_dir(candidate).exists():
            candidate = f"{base}-{uuid.uuid4().hex[:6]}"
        if candidate != pid:
            src = project_dir(pid)
            dest = project_dir(candidate)
            if dest.exists():
                raise RuntimeError(f"Target folder already exists: {candidate}")
            shutil.move(str(src), str(dest))
            new_id = candidate
            folder_moved = True
            meta = load_meta(new_id)
            meta["id"] = new_id
            meta["title"] = new_title
            if topic is not None and str(topic).strip():
                meta["topic"] = str(topic).strip()
            save_meta(new_id, meta)
            # Keep Topics queue pointers in sync.
            from studio.topics import retarget_job_id

            retarget_job_id(pid, new_id)
            hidden = load_deleted_ids()
            if pid in hidden:
                hidden.discard(pid)
                hidden.add(new_id)
                save_deleted_ids(hidden)

    youtube_result: dict[str, Any] | None = None
    youtube_error = ""
    if update_youtube:
        try:
            from studio.youtube import update_uploaded_video_title

            youtube_result = update_uploaded_video_title(new_id, new_title)
        except Exception as exc:
            youtube_error = str(exc)

    payload = project_payload(new_id)
    out = {
        "ok": True,
        "project_id": new_id,
        "old_project_id": pid if folder_moved else new_id,
        "title": new_title,
        "old_title": old_title,
        "folder_renamed": folder_moved,
        "youtube": youtube_result,
        "youtube_error": youtube_error or None,
        "project": payload,
    }
    if youtube_error:
        out["note"] = (
            "Studio title updated, but YouTube title update failed. "
            f"{youtube_error}"
        )
    elif youtube_result and youtube_result.get("skipped"):
        out["note"] = youtube_result.get("reason") or "No YouTube video_id on this job yet."
    elif youtube_result and youtube_result.get("ok"):
        out["note"] = "Studio title and YouTube title updated."
    else:
        out["note"] = "Studio title updated."
    return out


def create_project(
    topic: str,
    duration_seconds: int,
    title: str | None = None,
    aspect: str | None = None,
    *,
    owner_id: str | None = None,
) -> dict[str, Any]:
    ensure_dirs()
    duration_seconds = max(15, int(duration_seconds))
    settings = load_settings()
    base = slugify(title or topic)
    project_id = base
    if project_dir(project_id).exists():
        project_id = f"{base}-{uuid.uuid4().hex[:6]}"
    folder = project_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{INPUT_STEM}_billboards").mkdir(exist_ok=True)
    (folder / f"{INPUT_STEM}_backgrounds").mkdir(exist_ok=True)
    tts_provider = normalize_tts_provider(
        settings.get("tts_provider") or settings.get("voice_provider")
    )
    meta = {
        "id": project_id,
        "owner_id": (owner_id or "").strip() or None,
        "title": (title or topic).strip(),
        "topic": topic.strip(),
        "duration_seconds": duration_seconds,
        "aspect": normalize_job_aspect(aspect or settings.get("default_aspect") or DEFAULT_ASPECT),
        "summary": "",
        "tts_provider": tts_provider,
        "voice_provider": tts_provider,
        "voice_id": default_voice_for_provider(tts_provider, settings),
        "image_provider": normalize_image_provider(settings.get("image_provider")),
        "cover_provider": normalize_cover_provider(settings.get("cover_provider")),
        "video_layout": normalize_video_layout(settings.get("video_layout")),
        "character_size": normalize_character_size(settings.get("character_size")),
        "art_style": "classic",
        "include_bubblehead": True,
        "background_file": (default_background() or {}).get("filename") or "",
        "youtube_auto_upload": normalize_youtube_auto_upload(settings.get("youtube_auto_upload")),
        "youtube_privacy": normalize_youtube_privacy(settings.get("youtube_privacy")),
        "youtube_description": "",
        "youtube_keywords": [],
        "youtube_hashtags": [],
        "generate_9x16": False,
        "status": "draft",
        "job": None,
        "renders": {},
        "last_render_aspect": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    save_meta(project_id, meta)
    hidden = load_deleted_ids()
    if project_id in hidden:
        hidden.discard(project_id)
        save_deleted_ids(hidden)
    return meta


def set_aspect(project_id: str, aspect: str) -> dict[str, Any]:
    meta = load_meta(project_id)
    meta["aspect"] = normalize_job_aspect(aspect)
    save_meta(project_id, meta)
    return project_payload(project_id)


def project_generate_9x16(project_id: str | dict[str, Any] | None = None) -> bool:
    """Whether to build the hook-only 9:16 short (script/images/audio). Default False."""
    if isinstance(project_id, dict):
        meta = project_id
    elif project_id:
        try:
            meta = load_meta(project_id)
        except FileNotFoundError:
            return False
    else:
        return False
    if "generate_9x16" not in meta:
        return False
    return bool(meta.get("generate_9x16"))


def set_generate_9x16(project_id: str, enabled: bool = True) -> dict[str, Any]:
    meta = load_meta(project_id)
    meta["generate_9x16"] = bool(enabled)
    save_meta(project_id, meta)
    if meta["generate_9x16"]:
        try:
            write_shorts_scripts(project_id)
        except Exception:
            pass
    return project_payload(project_id)


def project_image_provider(project_id: str) -> str:
    meta = load_meta(project_id)
    stored = (meta.get("image_provider") or "").strip()
    if stored:
        return normalize_image_provider(stored)
    return normalize_image_provider(load_settings().get("image_provider"))


def project_cover_provider(project_id: str) -> str:
    """Effective cover backend (manual|chatgpt|comfyui|flux)."""
    meta = load_meta(project_id)
    override = meta.get("cover_provider")
    if override is None or str(override).strip() == "":
        override = load_settings().get("cover_provider")
    return resolve_cover_provider(project_image_provider(project_id), override)


def project_cover_provider_override(project_id: str) -> str:
    """Stored override only ('' = inherit image_provider)."""
    meta = load_meta(project_id)
    if "cover_provider" in meta and meta.get("cover_provider") is not None:
        return normalize_cover_provider(meta.get("cover_provider"))
    return normalize_cover_provider(load_settings().get("cover_provider"))


def set_cover_provider(project_id: str, provider: str) -> dict[str, Any]:
    meta = load_meta(project_id)
    meta["cover_provider"] = normalize_cover_provider(provider)
    save_meta(project_id, meta)
    return project_payload(project_id)


def cover_provider_is_agent(project_id: str) -> bool:
    """True when covers must NOT spend fal / auto-gen — agent supplies art."""
    return project_cover_provider(project_id) in ("manual", "chatgpt")


def set_image_provider(project_id: str, provider: str) -> dict[str, Any]:
    from studio.settings import normalize_image_provider

    meta = load_meta(project_id)
    old = normalize_image_provider(meta.get("image_provider") or load_settings().get("image_provider"))
    new = normalize_image_provider(provider)
    meta["image_provider"] = new
    save_meta(project_id, meta)
    refresh_line_prompts(project_id)
    switched = None
    if new != old:
        try:
            from studio.pipeline import request_image_provider_switch

            switched = request_image_provider_switch(project_id, new)
        except Exception:
            switched = None
    payload = project_payload(project_id)
    if switched and switched.get("switching"):
        payload["provider_switch"] = switched
    return payload


def project_video_layout(project_id: str) -> str:
    meta = load_meta(project_id)
    stored = (meta.get("video_layout") or "").strip()
    if stored:
        return normalize_video_layout(stored)
    return normalize_video_layout(load_settings().get("video_layout"))


def project_art_style(project_id: str | dict[str, Any] | None = None) -> str:
    """Per-job art style id (classic = current Bubble Pod default)."""
    from studio.art_style import DEFAULT_ART_STYLE, normalize_art_style

    if isinstance(project_id, dict):
        meta = project_id
    elif project_id:
        try:
            meta = load_meta(project_id)
        except FileNotFoundError:
            return DEFAULT_ART_STYLE
    else:
        return DEFAULT_ART_STYLE
    stored = (meta.get("art_style") or "").strip()
    if not stored:
        return DEFAULT_ART_STYLE
    try:
        return normalize_art_style(stored)
    except RuntimeError:
        return DEFAULT_ART_STYLE


def refresh_line_prompts(project_id: str) -> None:
    path = project_dir(project_id) / "lines.json"
    if not path.is_file():
        return
    from studio.art_style import wrap_scene_prompt

    meta = load_meta(project_id)
    lines = json.loads(path.read_text(encoding="utf-8"))
    aspect = normalize_aspect(meta.get("aspect") or DEFAULT_ASPECT)
    layout = project_video_layout(project_id)
    style = project_art_style(meta)
    title = meta.get("title") or meta.get("topic") or ""
    summary = meta.get("summary") or ""
    provider = project_image_provider(project_id)
    for line in lines:
        scene = line.get("scene") or line.get("raw") or ""
        spoken = line.get("raw") or ""
        line["image_prompt"] = wrap_scene_prompt(
            scene,
            title,
            summary,
            spoken,
            aspect=aspect,
            layout=layout,
            provider=provider,
            art_style=style,
        )
    path.write_text(json.dumps(lines, indent=2), encoding="utf-8")


def set_video_layout(project_id: str, layout: str) -> dict[str, Any]:
    meta = load_meta(project_id)
    meta["video_layout"] = normalize_video_layout(layout)
    save_meta(project_id, meta)
    refresh_line_prompts(project_id)
    return project_payload(project_id)


def set_art_style(project_id: str, style: str) -> dict[str, Any]:
    from studio.art_style import normalize_art_style

    meta = load_meta(project_id)
    meta["art_style"] = normalize_art_style(style)
    save_meta(project_id, meta)
    refresh_line_prompts(project_id)
    return project_payload(project_id)

def project_character_size(project_id: str) -> str:
    meta = load_meta(project_id)
    stored = (meta.get("character_size") or "").strip()
    if stored:
        try:
            return normalize_character_size(stored)
        except RuntimeError:
            pass
    return normalize_character_size(load_settings().get("character_size"))


def set_character_size(project_id: str, size: str) -> dict[str, Any]:
    meta = load_meta(project_id)
    meta["character_size"] = normalize_character_size(size)
    save_meta(project_id, meta)
    return project_payload(project_id)


def project_include_bubblehead(project_id: str | dict[str, Any] | None = None) -> bool:
    """Whether to composite the yellow stick-figure narrator. Default True."""
    if isinstance(project_id, dict):
        meta = project_id
    elif project_id:
        try:
            meta = load_meta(project_id)
        except FileNotFoundError:
            return True
    else:
        return True
    if "include_bubblehead" not in meta and "show_character" in meta:
        return bool(meta.get("show_character"))
    if "include_bubblehead" not in meta:
        return True
    return bool(meta.get("include_bubblehead"))


def set_include_bubblehead(project_id: str, enabled: bool = True) -> dict[str, Any]:
    meta = load_meta(project_id)
    meta["include_bubblehead"] = bool(enabled)
    save_meta(project_id, meta)
    return project_payload(project_id)


def set_project_voice(
    project_id: str,
    provider: str | None = None,
    voice_id: str | None = None,
) -> dict[str, Any]:
    meta = load_meta(project_id)
    if provider:
        canon = normalize_tts_provider(provider)
        meta["tts_provider"] = canon
        meta["voice_provider"] = canon
        if not voice_id:
            meta["voice_id"] = default_voice_for_provider(canon, load_settings())
    if voice_id is not None and str(voice_id).strip() != "":
        meta["voice_id"] = str(voice_id).strip()
    save_meta(project_id, meta)
    return project_payload(project_id)


def project_background_file(project_id: str) -> str:
    meta = load_meta(project_id)
    stored = (meta.get("background_file") or "").strip()
    item = resolve_background(stored) if stored else None
    if item:
        return item["filename"]
    fallback = default_background()
    return (fallback or {}).get("filename") or ""


def project_youtube_auto_upload(project_id: str) -> bool:
    meta = load_meta(project_id)
    if "youtube_auto_upload" in meta and meta["youtube_auto_upload"] is not None:
        return normalize_youtube_auto_upload(meta.get("youtube_auto_upload"))
    return normalize_youtube_auto_upload(load_settings().get("youtube_auto_upload"))


def project_youtube_privacy(project_id: str) -> str:
    meta = load_meta(project_id)
    stored = (meta.get("youtube_privacy") or "").strip()
    if stored:
        return normalize_youtube_privacy(stored)
    return normalize_youtube_privacy(load_settings().get("youtube_privacy"))


def set_project_youtube(
    project_id: str,
    auto_upload: Any = None,
    privacy: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    meta = load_meta(project_id)
    if auto_upload is not None:
        meta["youtube_auto_upload"] = normalize_youtube_auto_upload(auto_upload)
    if privacy is not None and str(privacy).strip() != "":
        meta["youtube_privacy"] = normalize_youtube_privacy(privacy)
    if channel_id is not None:
        meta["youtube_channel_id"] = str(channel_id).strip()
    save_meta(project_id, meta)
    return project_payload(project_id)


_YOUTUBE_TAG_MAX = 30
_YOUTUBE_TAGS_BUDGET = 480  # YouTube allows ~500 chars total across tags


def normalize_youtube_keywords(value: Any) -> list[str]:
    """Normalize tags for YouTube Data API snippet.tags (no #, unique, budget-capped)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = [str(x) for x in value]
    else:
        text = str(value).replace("\r\n", "\n").strip()
        if not text:
            return []
        raw_items = re.split(r"[,;\n]+", text)
    out: list[str] = []
    seen: set[str] = set()
    budget = 0
    for item in raw_items:
        tag = re.sub(r"\s+", " ", item.strip().lstrip("#").strip())
        if not tag:
            continue
        tag = tag[:_YOUTUBE_TAG_MAX]
        key = tag.casefold()
        if key in seen:
            continue
        extra = len(tag) + (1 if out else 0)
        if budget + extra > _YOUTUBE_TAGS_BUDGET:
            break
        seen.add(key)
        out.append(tag)
        budget += extra
    return out


def normalize_youtube_hashtags(value: Any) -> list[str]:
    """Normalize visible hashtags (with leading #)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = [str(x) for x in value]
    else:
        text = str(value).replace("\r\n", "\n").strip()
        if not text:
            return []
        raw_items = re.findall(r"#?[A-Za-z0-9_]+", text) or re.split(r"[,;\s]+", text)
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        token = item.strip()
        if not token:
            continue
        if not token.startswith("#"):
            token = "#" + token.lstrip("#")
        token = re.sub(r"[^#A-Za-z0-9_]", "", token)
        if len(token) < 2:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
        if len(out) >= 15:
            break
    return out


def keywords_to_text(value: Any) -> str:
    return ", ".join(normalize_youtube_keywords(value))


def hashtags_to_text(value: Any) -> str:
    return " ".join(normalize_youtube_hashtags(value))


def apply_youtube_publish_meta(
    project_id: str,
    *,
    description: str | None = None,
    keywords: Any = None,
    hashtags: Any = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Persist youtube_description / youtube_keywords / youtube_hashtags on meta.json.

    When replace=True (script generation), missing/empty values clear prior fields.
    When replace=False (GUI/MCP edits), None leaves that field unchanged; empty clears.
    """
    meta = load_meta(project_id)
    if description is not None:
        meta["youtube_description"] = str(description).strip()
    elif replace:
        meta["youtube_description"] = ""
    if keywords is not None:
        meta["youtube_keywords"] = normalize_youtube_keywords(keywords)
    elif replace:
        meta["youtube_keywords"] = []
    if hashtags is not None:
        meta["youtube_hashtags"] = normalize_youtube_hashtags(hashtags)
    elif replace:
        meta["youtube_hashtags"] = []
    save_meta(project_id, meta)
    return meta


def set_project_background(project_id: str, filename: str) -> dict[str, Any]:
    item = resolve_background(filename)
    if not item:
        raise RuntimeError(f"Unknown background: {filename}")
    meta = load_meta(project_id)
    meta["background_file"] = item["filename"]
    save_meta(project_id, meta)
    return project_payload(project_id)


def invalidate_render_artifacts(project_id: str, *, alignment: bool = False) -> None:
    """Drop schedule, frames, and final mp4s so Resume cannot reuse a stale render.

    If alignment=True, also delete Gentle script.json (new script/wav makes it invalid).
    Does not touch script.txt, wav, or illustration PNGs (including aspect covers).
    """
    prefix = Path(input_prefix(project_id))
    shorts = Path(shorts_input_prefix(project_id))
    targets = [
        prefix.with_name(prefix.name + "_schedule.csv"),
        shorts.with_name(shorts.name + "_schedule.csv"),
        last_video_path(project_id),
    ]
    for asp in ALL_ASPECTS:
        targets.append(final_video_path(project_id, asp))
    if alignment:
        targets.append(prefix.with_suffix(".json"))
        targets.append(shorts.with_suffix(".json"))
    for path in targets:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
    frame_dirs = [Path(str(prefix) + "_frames"), Path(str(shorts) + "_frames")]
    for asp in ALL_ASPECTS:
        frame_dirs.append(frames_dir(project_id, asp))
    for frames in frame_dirs:
        if frames.is_dir():
            shutil.rmtree(frames, ignore_errors=True)
    try:
        meta = load_meta(project_id)
        meta["renders"] = {}
        meta["last_render_aspect"] = None
        save_meta(project_id, meta)
    except FileNotFoundError:
        pass


def write_shorts_scripts(project_id: str, tagged: str | None = None) -> dict[str, Any]:
    """Materialize script_9x16.txt / _raw.txt / _g.txt from hook + script.shorts_cta."""
    if tagged is None:
        full = Path(input_prefix(project_id)).with_suffix(".txt")
        if not full.is_file():
            raise RuntimeError("Generate a script before building the 9:16 short.")
        tagged = full.read_text(encoding="utf-8")
    tagged_out, raw_out = build_shorts_script(tagged)
    prefix = Path(shorts_input_prefix(project_id))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".txt").write_text(tagged_out, encoding="utf-8")
    prefix.with_name(prefix.name + "_raw.txt").write_text(raw_out, encoding="utf-8")
    prefix.with_name(prefix.name + "_g.txt").write_text(raw_out, encoding="utf-8")
    billboards_dir(project_id, ASPECT_9_16)
    return {
        "ok": True,
        "prefix": str(prefix),
        "script_tagged": tagged_out,
        "script_raw": raw_out,
        "word_count": len(raw_out.split()),
    }


def write_scripts(project_id: str, tagged: str, raw: str | None = None) -> dict[str, Any]:
    meta = load_meta(project_id)
    prefix = Path(input_prefix(project_id))
    tagged = tagged.replace("\r\n", "\n").strip() + "\n"
    # Always derive spoken text from tagged so illustration/art cues cannot reach TTS.
    # `raw` is accepted for call-site compat but ignored.
    _ = raw
    raw = raw_from_tagged(tagged)
    if not raw.endswith("\n"):
        raw = raw.rstrip() + "\n"
    prefix.with_suffix(".txt").write_text(tagged, encoding="utf-8")
    prefix.with_name(prefix.name + "_raw.txt").write_text(raw, encoding="utf-8")
    prefix.with_name(prefix.name + "_g.txt").write_text(raw, encoding="utf-8")
    if needs_shorts_assets(meta.get("aspect") or DEFAULT_ASPECT) or project_generate_9x16(meta):
        try:
            write_shorts_scripts(project_id, tagged)
        except Exception:
            pass
    meta["status"] = "scripted"
    meta["word_count"] = len(raw.split())
    save_meta(project_id, meta)
    return project_payload(project_id)


def save_lines(project_id: str, lines: list[dict[str, Any]], summary: str | None = None) -> None:
    meta = load_meta(project_id)
    if summary:
        meta["summary"] = summary
        save_meta(project_id, meta)
    path = project_dir(project_id) / "lines.json"
    path.write_text(json.dumps(lines, indent=2), encoding="utf-8")


def load_lines(project_id: str) -> list[dict[str, Any]]:
    path = project_dir(project_id) / "lines.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    tagged_path = Path(input_prefix(project_id)).with_suffix(".txt")
    if tagged_path.is_file():
        meta = load_meta(project_id)
        return parse_tagged_script(
            tagged_path.read_text(encoding="utf-8"),
            title=meta.get("title") or "",
            summary=meta.get("summary") or "",
            aspect=meta.get("aspect") or DEFAULT_ASPECT,
            layout=project_video_layout(project_id),
            image_provider=project_image_provider(project_id),
        )
    return []


def project_payload(project_id: str) -> dict[str, Any]:
    meta = load_meta(project_id)
    prefix = Path(input_prefix(project_id))
    tagged = prefix.with_suffix(".txt")
    raw = prefix.with_name(prefix.name + "_raw.txt")
    lines = load_lines(project_id)
    billboards = prefix.parent / f"{INPUT_STEM}_billboards"
    existing = {p.stem for p in billboards.glob("*.png")} if billboards.is_dir() else set()
    for i, line in enumerate(lines):
        short = f"b{i + 1:03d}"
        if line.get("filename") != short:
            line["filename"] = short
        # Accept legacy topic-based stems until migrate_billboard_short_names renames them.
        legacy = re.sub(r"[^A-Za-z0-9 -]+", "", (line.get("topic") or line.get("raw") or "").lower()).strip()
        line["has_image"] = short in existing or (bool(legacy) and legacy in existing)
    audio = prefix.with_suffix(".wav")
    shorts_prefix = Path(shorts_input_prefix(project_id))
    shorts_audio = shorts_prefix.with_suffix(".wav")
    migrate_legacy_aspect_files(project_id)
    job_aspect = normalize_job_aspect(meta.get("aspect") or DEFAULT_ASPECT)
    aspect = normalize_aspect(job_aspect)
    renders = collect_renders(project_id)
    video = last_video_path(project_id)
    aspect_video = final_video_path(project_id, aspect)
    if not video.is_file() and aspect_video.is_file():
        video = aspect_video
    last_aspect = meta.get("last_render_aspect")
    if last_aspect:
        last_aspect = normalize_aspect(last_aspect)
        last_specific = final_video_path(project_id, last_aspect)
        if last_specific.is_file() and last_specific.stat().st_size >= 1000:
            video = last_specific if not video.is_file() else video
    needed = aspects_to_render(job_aspect)
    has_cover = all(cover_matches_canvas(cover_path(project_id, asp), asp) for asp in needed)
    cover = cover_path(project_id, last_aspect or aspect)
    missing_lines = sum(1 for line in lines if line.get("filename") and not line.get("has_image"))
    cover_prompt = ""
    try:
        from studio.illustrations import cover_intro_prompt

        cover_prompt = cover_intro_prompt(project_id, aspect=aspect)
    except Exception:
        cover_prompt = ""
    has_video = any(item.get("ready") for item in renders.values())
    yt_info = youtube_watch_info(meta)
    payload = {
        **meta,
        "script_tagged": tagged.read_text(encoding="utf-8") if tagged.is_file() else "",
        "script_raw": raw.read_text(encoding="utf-8") if raw.is_file() else "",
        "lines": lines,
        "has_audio": audio.is_file(),
        "has_alignment": prefix.with_suffix(".json").is_file(),
        "has_shorts_script": shorts_prefix.with_suffix(".txt").is_file(),
        "has_shorts_audio": shorts_audio.is_file(),
        "has_shorts_alignment": shorts_prefix.with_suffix(".json").is_file(),
        "generate_9x16": project_generate_9x16(meta),
        "has_video": has_video,
        "has_youtube": bool(yt_info.get("has_youtube")),
        "youtube_url": yt_info.get("youtube_url"),
        "youtube_video_id": yt_info.get("youtube_video_id"),
        "library_ready": bool(has_video or yt_info.get("has_youtube")),
        "art_style": project_art_style(meta),
        "has_cover": has_cover,
        "cover_prompt": cover_prompt,
        "renders": renders,
        "covers": {
            asp: {
                "file": renders[asp]["cover"],
                "path": renders[asp]["cover_path"],
                "ready": renders[asp]["cover_ready"],
            }
            for asp in ALL_ASPECTS
        },
        "last_render_aspect": last_aspect
        if last_aspect and renders.get(last_aspect, {}).get("ready")
        else next((asp for asp in ALL_ASPECTS if renders.get(asp, {}).get("ready")), None),
        "paths": {
            "folder": str(prefix.parent),
            "tagged": str(tagged),
            "raw": str(raw),
            "audio": str(audio),
            "shorts_tagged": str(shorts_prefix.with_suffix(".txt")),
            "shorts_raw": str(shorts_prefix.with_name(shorts_prefix.name + "_raw.txt")),
            "shorts_audio": str(shorts_audio),
            "video": str(video),
            "videos": {asp: str(final_video_path(project_id, asp)) for asp in ALL_ASPECTS},
            "billboards": str(billboards),
            "billboards_9x16": str(billboards_dir(project_id, ASPECT_9_16)),
            "cover": str(cover),
            "covers": {asp: str(cover_path(project_id, asp)) for asp in ALL_ASPECTS},
        },
        "missing_illustrations": missing_lines + (0 if has_cover else 1),
        "allowed_emotions": list(ALLOWED_EMOTIONS),
        "aspect": job_aspect,
        "image_provider": project_image_provider(project_id),
        "cover_provider": project_cover_provider(project_id),
        "cover_provider_override": project_cover_provider_override(project_id),
        "video_layout": project_video_layout(project_id),
        "character_size": project_character_size(project_id),
        "include_bubblehead": project_include_bubblehead(meta),
        "background_file": project_background_file(project_id),
        "youtube_auto_upload": project_youtube_auto_upload(project_id),
        "youtube_privacy": project_youtube_privacy(project_id),
        "youtube": meta.get("youtube"),
        "youtube_error": meta.get("youtube_error"),
        "youtube_pending": bool(meta.get("youtube_pending")),
        "youtube_channel_id": (meta.get("youtube_channel_id") or "").strip()
        or (load_settings().get("youtube_channel_id") or ""),
        "youtube_description": (meta.get("youtube_description") or "").strip(),
        "youtube_keywords": normalize_youtube_keywords(meta.get("youtube_keywords")),
        "youtube_hashtags": normalize_youtube_hashtags(meta.get("youtube_hashtags")),
        "youtube_keywords_text": keywords_to_text(meta.get("youtube_keywords")),
        "youtube_hashtags_text": hashtags_to_text(meta.get("youtube_hashtags")),
    }
    tagged_text = payload["script_tagged"]
    if tagged_text:
        warnings = script_structure_warnings(tagged_text)
        if warnings:
            payload["script_warnings"] = warnings
    try:
        from studio.music import project_music_info

        payload.update(project_music_info(project_id))
    except Exception:
        payload.setdefault("music_id", meta.get("music_id"))
        payload.setdefault("music_name", meta.get("music_name") or meta.get("music_file"))
    return payload
