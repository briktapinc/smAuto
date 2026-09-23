from __future__ import annotations

import os
import subprocess
from pathlib import Path

from studio.aspect import ALL_ASPECTS, DEFAULT_ASPECT, normalize_aspect
from studio.paths import FALLBACK_COLORS
from studio.projects import (
    INPUT_STEM,
    cover_path,
    final_video_path,
    frames_dir,
    input_prefix,
    last_video_path,
    load_meta,
    project_dir,
)

THUMB_NAME = "script_thumb.jpg"
PLACEHOLDER_NAME = "script_placeholder.jpg"


def _video_path(project_id: str) -> Path:
    last = last_video_path(project_id)
    if last.is_file() and last.stat().st_size >= 1000:
        return last
    meta = load_meta(project_id)
    aspect = normalize_aspect(meta.get("last_render_aspect") or meta.get("aspect") or DEFAULT_ASPECT)
    specific = final_video_path(project_id, aspect)
    if specific.is_file():
        return specific
    for asp in ALL_ASPECTS:
        path = final_video_path(project_id, asp)
        if path.is_file() and path.stat().st_size >= 1000:
            return path
    return last


def _thumb_path(project_id: str) -> Path:
    return project_dir(project_id) / THUMB_NAME


def _placeholder_path(project_id: str) -> Path:
    return project_dir(project_id) / PLACEHOLDER_NAME


def _frames_dir(project_id: str) -> Path:
    meta = load_meta(project_id)
    aspect = normalize_aspect(meta.get("last_render_aspect") or meta.get("aspect") or DEFAULT_ASPECT)
    specific = frames_dir(project_id, aspect)
    if specific.is_dir():
        return specific
    prefix = Path(input_prefix(project_id))
    return prefix.with_name(prefix.name + "_frames")


def _mid_frame(frames: Path) -> Path | None:
    pngs = sorted(frames.glob("f*.png")) or sorted(frames.glob("*.png"))
    if not pngs:
        return None
    return pngs[len(pngs) // 2]


def _cover_still(project_id: str) -> Path:
    meta = load_meta(project_id)
    aspect = normalize_aspect(meta.get("last_render_aspect") or meta.get("aspect") or DEFAULT_ASPECT)
    path = cover_path(project_id, aspect)
    if path.is_file() and path.stat().st_size > 200:
        return path
    for asp in ALL_ASPECTS:
        alt = cover_path(project_id, asp)
        if alt.is_file() and alt.stat().st_size > 200:
            return alt
    return Path(input_prefix(project_id) + "_cover.png")


def _first_billboard(project_id: str) -> Path | None:
    folder = project_dir(project_id) / f"{INPUT_STEM}_billboards"
    if not folder.is_dir():
        return None
    pngs = sorted(p for p in folder.glob("*.png") if p.is_file())
    return pngs[0] if pngs else None


def _ffmpeg_kwargs() -> dict:
    flags: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def _ffmpeg_thumb(video: Path, dest: Path) -> bool:
    from studio.ffmpeg_bin import resolve_ffmpeg

    dest.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_ffmpeg()
    seeks = (["-ss", "0.2"], ["-ss", "1"], [])
    for extra in seeks:
        cmd = [
            ffmpeg,
            "-y",
            *extra,
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2",
            "-q:v",
            "4",
            str(dest),
        ]
        try:
            subprocess.check_call(cmd, **_ffmpeg_kwargs())
            if dest.is_file() and dest.stat().st_size > 200:
                return True
        except Exception:
            if dest.is_file():
                try:
                    dest.unlink()
                except OSError:
                    pass
    return False


def _copy_as_jpeg(src: Path, dest: Path) -> bool:
    try:
        from PIL import Image

        img = Image.open(src)
        img = img.convert("RGB")
        img.thumbnail((960, 960))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=82)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception:
        return False


def _draw_placeholder(project_id: str, dest: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    meta = load_meta(project_id)
    aspect = normalize_aspect(meta.get("aspect") or DEFAULT_ASPECT)
    size = (360, 640) if aspect == "9:16" else (640, 360)
    w, h = size
    idx = sum(ord(c) for c in project_id) % len(FALLBACK_COLORS)
    color = FALLBACK_COLORS[idx]
    img = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(img)
    lighter = tuple(min(255, c + 28) for c in color)
    darker = tuple(max(0, c - 36) for c in color)
    draw.ellipse((int(w * 0.07), int(h * 0.08), int(w * 0.38), int(h * 0.34)), fill=lighter)
    draw.ellipse((int(w * 0.62), int(h * 0.58), int(w * 0.98), int(h * 0.96)), fill=darker)
    draw.rectangle((0, h - 36, w, h), fill=darker)
    title = (meta.get("title") or meta.get("topic") or project_id).strip()
    initials = "".join(part[0] for part in title.split()[:2] if part).upper() or "LK"
    try:
        font = ImageFont.truetype("arial.ttf", 72 if aspect == "9:16" else 64)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), initials, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((w - tw) / 2, (h - th) / 2 - 8), initials, fill=(28, 23, 18), font=font)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, "JPEG", quality=80)
    return dest


def ensure_thumbnail(project_id: str) -> Path:
    """Best still for a job: generated title-card cover, then video frame, billboard, or doodle."""
    load_meta(project_id)
    thumb = _thumb_path(project_id)
    cover = _cover_still(project_id)
    if cover.is_file() and cover.stat().st_size > 200:
        if thumb.is_file() and thumb.stat().st_mtime >= cover.stat().st_mtime and thumb.stat().st_size > 200:
            return thumb
        if _copy_as_jpeg(cover, thumb):
            return thumb
        return cover

    video = _video_path(project_id)

    if video.is_file():
        if thumb.is_file() and thumb.stat().st_mtime >= video.stat().st_mtime and thumb.stat().st_size > 200:
            return thumb
        frames = _frames_dir(project_id)
        frame = _mid_frame(frames) if frames.is_dir() else None
        if frame and _copy_as_jpeg(frame, thumb):
            return thumb
        if _ffmpeg_thumb(video, thumb):
            return thumb

    billboard = _first_billboard(project_id)
    if billboard and billboard.is_file():
        preview = project_dir(project_id) / "script_billboard_thumb.jpg"
        if (
            preview.is_file()
            and preview.stat().st_mtime >= billboard.stat().st_mtime
            and preview.stat().st_size > 200
        ):
            return preview
        if _copy_as_jpeg(billboard, preview):
            return preview
        return billboard

    placeholder = _placeholder_path(project_id)
    if placeholder.is_file() and placeholder.stat().st_size > 200:
        return placeholder
    return _draw_placeholder(project_id, placeholder)
