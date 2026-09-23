from __future__ import annotations

import base64
import logging
import os
import re
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from studio.art_style import wrap_scene_prompt
from studio.aspect import (
    ALL_ASPECTS,
    ASPECT_16_9,
    ASPECT_9_16,
    ASPECT_BOTH,
    DEFAULT_ASPECT,
    aspect_from_cover_filename,
    aspect_slug,
    aspects_to_render,
    canvas_phrase,
    canvas_size,
    cover_filename,
    generate_at_line,
    illustration_size,
    is_portrait,
    job_image_fields,
    needs_explainer_assets,
    needs_shorts_assets,
    normalize_aspect,
    normalize_job_aspect,
    pixel_size,
)
from studio.projects import (
    billboards_dir,
    cover_matches_canvas,
    cover_path,
    input_prefix,
    load_lines,
    load_meta,
    migrate_legacy_aspect_files,
    project_art_style,
    project_image_provider,
    project_payload,
    project_video_layout,
    publish_cover_alias,
    save_lines,
    save_meta,
    shorts_input_prefix,
    write_shorts_scripts,
)
from studio.prompts import get_prompt
from studio.settings import FLUX_MODEL, is_studio_image_provider, load_settings, normalize_video_layout, uses_short_image_prompt
from studio.utils_script import build_shorts_script, parse_tagged_script

log = logging.getLogger("studio.flux")

FLUX_MIN_SIDE = 256
FLUX_MAX_SIDE = 2560
FLUX_MULTIPLE = 16
FLUX_MAX_AREA = 4_194_304
COVER_FILENAME = "script_cover.png"
COVER_DURATION_SECONDS = 5
MIN_IMAGE_BYTES = 200
PENDING_REGEN_KEY = "pending_illustration_regen"
# Keep illustration paths well under Windows MAX_PATH (~260). Base project paths are ~100–140 chars.
WIN_PATH_SOFT_MAX = 200
LINE_BILLBOARD_STEM_RE = re.compile(r"^b(\d{3})$", re.I)
BILLBOARD_NAMING_RULE = (
    "Line PNGs use short stems b001.png, b002.png, … (1-based script line order). "
    "Covers stay script_cover_16x9.png / script_cover_9x16.png. "
    "Never save ChatGPT's long image title as the disk filename — use jobs[].filename / "
    "jobs[].save_path from list_illustration_jobs (or kind+line_index)."
)
CHATGPT_COVER_HELP = (
    "This job uses ChatGPT native images. Generate the 5-second title-card in "
    "ChatGPT's built-in image tool at the canvas for that aspect — never stretch "
    "16:9 into 9:16. 16:9 landscape preset at 1920x1080 → script_cover_16x9.png; "
    "9:16 portrait preset at 1080x1920 → script_cover_9x16.png. Never square. "
    "Paste the cover job prompt from list_illustration_jobs verbatim (live stickman_head_color). "
    "Then save it with save_illustration_image("
    "filename='script_cover_16x9.png' or 'script_cover_9x16.png', kind='cover') "
    "or upload that slot on Pictures. FAL_KEY is not required for ChatGPT covers. "
    + BILLBOARD_NAMING_RULE
)

# Injected into every cover prompt (Flux/Comfy/ChatGPT) so title lettering is never dropped
# by a stale images.cover_intro override. Line billboards do not get this clause.
COVER_TITLE_LETTERING_MARKER = "exact video title"
COVER_COMPOSITION_MARKER = "bubblehead"
COVER_POSE_NONCE_KEY = "cover_pose_nonce"
COVERS_HISTORY_DIRNAME = "covers_history"

COVER_BUBBLEHEAD_POSES = (
    "thinking pose — one hand raised to the chin, looking at the TV",
    "pointing at the TV screen with one arm extended",
    "both arms raised in excited surprise while facing the screen",
    "presenting with an open palm toward the billboard",
    "leaning slightly forward with a curious expression at the screen",
    "hands on hips, confident stance facing the TV",
    "one hand waving hello toward the viewer, body angled to the TV",
    "scratching the side of the head, puzzled look at the screen",
    "arms crossed thoughtfully while studying the billboard",
    "one knee bent, dynamic storytelling pose gesturing at the TV",
    "jumping slightly with delight, eyes on the glowing screen",
    "holding an imaginary microphone, presenting the billboard topic",
)


def cover_title_on_image_clause(title: str) -> str:
    """Require the video title as large billboard headline lettering on the 5s title-card."""
    label = (title or "").strip()
    if not label:
        return (
            "The exact video title (project title/topic) MUST appear as large, bold, "
            "readable headline lettering ON the TV/billboard screen. Keep the full title "
            "on-frame — not cut off, not tiny, not a watermark. Topic illustration also on "
            "the screen. Bubblehead stays outside the TV — never inside the screen art."
        )
    return (
        f'The exact video title "{label}" MUST appear as large, bold, readable headline '
        "lettering ON the TV/billboard screen (title-card energy). Keep the full title "
        "on-frame — not cut off, not tiny, not a watermark. Topic illustration/diagram also "
        "on the screen. Bubblehead stays outside the TV — never draw the presenter inside "
        "the screen art."
    )


def ensure_cover_title_on_image(prompt: str, title: str) -> str:
    """Append title-on-billboard requirement unless the prompt already states it."""
    text = (prompt or "").strip()
    if COVER_TITLE_LETTERING_MARKER in text.lower():
        return text
    clause = cover_title_on_image_clause(title)
    return f"{text}\n\n{clause}".strip() if text else clause


def cover_background_room_clause(project_id: str) -> str:
    """Describe matching the project's selected studio-room background."""
    from studio.projects import project_background_file

    name = (project_background_file(project_id) or "").strip() or "the selected studio background"
    return (
        f"Studio room MUST match this project's selected background image '{name}' "
        "(same wall color, wood floor, furniture, and room vibe as that still — "
        "clock / shelf / plant / acoustic panels / ON AIR energy). "
        "Do not invent a different room style."
    )


def cover_head_colors() -> dict[str, str]:
    """Live Bubblehead fill + shadow from Settings (same as set_stickman_head_color)."""
    from studio.pose_colors import head_color_status

    st = head_color_status()
    return {
        "head_color": str(st.get("color") or "#FAE02E"),
        "head_dark": str(st.get("dark") or "#F8AF05"),
    }


def cover_bubblehead_look_clause() -> str:
    """Describe Bubblehead using the live stickman_head_color (not hardcoded yellow)."""
    colors = cover_head_colors()
    fill = colors["head_color"]
    dark = colors["head_dark"]
    return (
        f"smiley Bubblehead stick figure (thin brown stick limbs, large round head filled "
        f"{fill} with slightly darker {dark} shading, simple black pill eyes and mouth)"
    )


def cover_head_color_lock_clause() -> str:
    """Hard lock so stale prompt overrides cannot bake stock yellow."""
    colors = cover_head_colors()
    fill = colors["head_color"]
    dark = colors["head_dark"]
    return (
        f"Bubblehead head fill MUST be exactly {fill} (head shading/shadow {dark}) — "
        "match Settings stickman_head_color / get_stickman_head_color. "
        "Do not use stock yellow #FAE02E / #F8AF05 unless that is the current setting."
    )


def cover_layout_clause(aspect: str | None) -> str:
    look = cover_bubblehead_look_clause()
    if is_portrait(aspect):
        return (
            "9:16 portrait composition: large TV/billboard in the UPPER portion of the frame; "
            f"{look} in the LOWER area looking up at the screen. "
            "Same idea as the landscape cover — character outside, topic text + art on the TV."
        )
    return (
        f"16:9 landscape composition: {look} on the LEFT; "
        "large TV/billboard occupying the RIGHT side of the frame. "
        "Character outside the TV; topic title text + illustration on the screen."
    )


def cover_bubblehead_pose_clause(project_id: str, aspect: str | None = None) -> str:
    """Stable unique pose until regenerate bumps cover_pose_nonce."""
    try:
        meta = load_meta(project_id)
    except FileNotFoundError:
        meta = {}
    nonce = int(meta.get(COVER_POSE_NONCE_KEY) or 0)
    slug = aspect_slug(aspect or meta.get("aspect") or DEFAULT_ASPECT)
    # Mix aspect so 16:9 and 9:16 covers can differ without an extra bump.
    idx = (nonce + (0 if slug == "16x9" else 7)) % len(COVER_BUBBLEHEAD_POSES)
    return COVER_BUBBLEHEAD_POSES[idx]


def bump_cover_pose_nonce(project_id: str) -> int:
    meta = load_meta(project_id)
    next_nonce = int(meta.get(COVER_POSE_NONCE_KEY) or 0) + 1
    meta[COVER_POSE_NONCE_KEY] = next_nonce
    save_meta(project_id, meta)
    return next_nonce


def ensure_cover_composition(prompt: str, project_id: str, aspect: str | None, title: str) -> str:
    """Append studio-composition clauses when a stale images.cover_intro omits them."""
    text = ensure_cover_title_on_image(prompt, title)
    lower = text.lower()
    extras: list[str] = []
    look = cover_bubblehead_look_clause()
    if COVER_COMPOSITION_MARKER not in lower and "stick figure" not in lower:
        extras.append(
            f"Include the {look} OUTSIDE the TV in a unique expressive pose. "
            "Do not put the presenter inside the TV screen art."
        )
    if "tv" not in lower and "billboard" not in lower and "monitor" not in lower:
        extras.append(
            "Include a large TV/billboard with the topic title as bold on-screen headline text "
            "plus a topic illustration on the screen."
        )
    if "selected background" not in lower and "studio room" not in lower:
        extras.append(cover_background_room_clause(project_id))
    if "16:9 landscape composition" not in lower and "9:16 portrait composition" not in lower:
        extras.append(cover_layout_clause(aspect))
    # Always lock live head color when missing (stale overrides that still say "yellow head").
    colors = cover_head_colors()
    lock = cover_head_color_lock_clause()
    if colors["head_color"].lower() not in lower:
        extras.append(lock)
    if extras:
        text = f"{text}\n\n" + "\n".join(extras)
    return text.strip()


def chatgpt_cover_help() -> str:
    """Operator help for ChatGPT native covers — uses live head color."""
    look = cover_bubblehead_look_clause()
    colors = cover_head_colors()
    return (
        "This job uses ChatGPT native images. Generate the 5-second title-card in "
        "ChatGPT's built-in image tool at the canvas for that aspect — never stretch "
        "16:9 into 9:16. 16:9 landscape preset at 1920x1080 → script_cover_16x9.png; "
        "9:16 portrait preset at 1080x1920 → script_cover_9x16.png. Never square. "
        "Paste the cover job prompt from list_illustration_jobs verbatim — studio room "
        f"matching the project's selected background, {look} OUTSIDE the TV "
        f"(head fill {colors['head_color']}, shading {colors['head_dark']}) "
        "in a unique pose, and the exact video title as large bold headline text ON the "
        "TV/billboard with topic art on the screen (presenter never inside the TV). "
        "Then save it with save_illustration_image("
        "filename='script_cover_16x9.png' or 'script_cover_9x16.png', kind='cover') "
        "or upload that slot on Pictures. FAL_KEY is not required for ChatGPT covers. "
        + BILLBOARD_NAMING_RULE
    )


def covers_history_dir(project_id: str, aspect: str | None = None) -> Path:
    from studio.projects import project_dir

    return project_dir(project_id) / COVERS_HISTORY_DIRNAME / aspect_slug(aspect)


def _cover_file_digest(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:16]


def _next_cover_history_index(folder: Path) -> int:
    highest = 0
    if folder.is_dir():
        for path in folder.iterdir():
            if not path.is_file() or path.suffix.lower() != ".png":
                continue
            match = re.match(r"^v(\d+)", path.stem, re.I)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def commit_cover_to_history(project_id: str, aspect: str | None, source: Path | None = None) -> dict | None:
    """Append cover bytes to covers_history/{16x9|9x16}/vNNN.png when new."""
    aspect = normalize_aspect(aspect)
    src = Path(source) if source else cover_path(project_id, aspect)
    if not src.is_file() or src.stat().st_size < MIN_IMAGE_BYTES:
        return None
    folder = covers_history_dir(project_id, aspect)
    folder.mkdir(parents=True, exist_ok=True)
    digest = _cover_file_digest(src)
    versions = list_cover_versions(project_id, aspect)
    if versions and versions[-1].get("digest") == digest:
        return versions[-1]
    idx = _next_cover_history_index(folder)
    version_id = f"v{idx:03d}"
    dest = folder / f"{version_id}.png"
    shutil.copy2(src, dest)
    entry = {
        "version_id": version_id,
        "filename": dest.name,
        "path": str(dest),
        "aspect": aspect,
        "aspect_slug": aspect_slug(aspect),
        "digest": digest,
        "mtime": int(dest.stat().st_mtime * 1000),
        "bytes": dest.stat().st_size,
        "url": (
            f"/api/projects/{project_id}/covers/history/{aspect_slug(aspect)}/"
            f"{version_id}.png?t={int(dest.stat().st_mtime * 1000)}"
        ),
        "active": False,
    }
    return entry


def list_cover_versions(project_id: str, aspect: str | None = None) -> list[dict]:
    """List archived cover generations for one canvas aspect (oldest → newest)."""
    aspect = normalize_aspect(aspect)
    folder = covers_history_dir(project_id, aspect)
    active = cover_path(project_id, aspect)
    active_digest = _cover_file_digest(active) if active.is_file() and active.stat().st_size > MIN_IMAGE_BYTES else ""
    items: list[dict] = []
    if not folder.is_dir():
        return items
    paths = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".png"],
        key=lambda p: p.name.lower(),
    )
    for path in paths:
        match = re.match(r"^(v\d+)", path.stem, re.I)
        version_id = match.group(1).lower() if match else path.stem
        try:
            digest = _cover_file_digest(path)
            mtime = int(path.stat().st_mtime * 1000)
            size = path.stat().st_size
        except OSError:
            continue
        if size < MIN_IMAGE_BYTES:
            continue
        items.append(
            {
                "version_id": version_id,
                "filename": path.name,
                "path": str(path),
                "aspect": aspect,
                "aspect_slug": aspect_slug(aspect),
                "digest": digest,
                "mtime": mtime,
                "bytes": size,
                "url": (
                    f"/api/projects/{project_id}/covers/history/{aspect_slug(aspect)}/"
                    f"{path.name}?t={mtime}"
                ),
                "active": bool(active_digest and digest == active_digest),
            }
        )
    return items


def resolve_cover_history_path(project_id: str, aspect: str | None, version_id: str) -> Path:
    aspect = normalize_aspect(aspect)
    raw = (version_id or "").strip()
    name = Path(raw).name
    if not name:
        raise RuntimeError("version_id is required (e.g. v001).")
    if not name.lower().endswith(".png"):
        name = f"{name}.png"
    if not re.match(r"^v\d+", name, re.I):
        raise RuntimeError(f"Invalid cover version id: {version_id!r}")
    path = covers_history_dir(project_id, aspect) / name
    if not path.is_file():
        # Allow bare stem match
        stem = Path(name).stem.lower()
        for candidate in covers_history_dir(project_id, aspect).glob("v*.png"):
            if candidate.stem.lower() == stem or candidate.stem.lower().startswith(stem):
                path = candidate
                break
    if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
        raise RuntimeError(f"Cover version not found: {version_id}")
    return path


def set_active_cover(project_id: str, aspect: str | None, version_id: str) -> dict:
    """Promote a history version to the canonical script_cover_*.png used by render."""
    aspect = normalize_aspect(aspect)
    src = resolve_cover_history_path(project_id, aspect, version_id)
    dest = cover_path(project_id, aspect)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    publish_cover_alias(project_id, dest, aspect)
    try:
        from studio.thumbs import ensure_thumbnail

        ensure_thumbnail(project_id)
    except Exception:
        pass
    versions = list_cover_versions(project_id, aspect)
    active = next((v for v in versions if v.get("active")), None)
    return {
        "ok": True,
        "aspect": aspect,
        "version_id": (active or {}).get("version_id") or Path(src).stem.lower(),
        "path": str(dest),
        "filename": dest.name,
        "url": f"/api/projects/{project_id}/cover?aspect={aspect}&t={int(dest.stat().st_mtime * 1000)}",
        "versions": versions,
    }


def cover_history_payload(project_id: str, aspect: str | None = None) -> dict:
    """History for one aspect, or every canvas the job needs when aspect is omitted/both."""
    meta = load_meta(project_id)
    raw = (aspect or "").strip().lower()
    if raw in ("", "all"):
        canvases = list(aspects_to_render(meta.get("aspect") or DEFAULT_ASPECT))
    elif raw == "both":
        canvases = list(ALL_ASPECTS)
    else:
        canvases = [normalize_aspect(aspect)]
    by_aspect: dict[str, list[dict]] = {}
    for canvas in canvases:
        by_aspect[canvas] = list_cover_versions(project_id, canvas)
    return {
        "ok": True,
        "project_id": project_id,
        "aspects": canvases,
        "history": by_aspect,
        "active": {canvas: str(cover_path(project_id, canvas)) for canvas in canvases},
    }

def line_billboard_stem(index: int) -> str:
    """Short stable billboard stem: b001, b002, … (1-based)."""
    try:
        n = int(index)
    except (TypeError, ValueError):
        n = 1
    return f"b{max(1, n):03d}"


def is_short_billboard_stem(stem: str | None) -> bool:
    return bool(LINE_BILLBOARD_STEM_RE.match((stem or "").strip()))


def _compact_name(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _legacy_line_stem(line: dict | None) -> str:
    """Topic-based stem from older Studio builds (can exceed Windows MAX_PATH)."""
    if not line:
        return ""
    tagged = (line.get("tagged") or line.get("raw") or line.get("topic") or "").strip()
    if not tagged:
        return ""
    try:
        import sys

        from studio.paths import CODE_DIR

        if str(CODE_DIR) not in sys.path:
            sys.path.insert(0, str(CODE_DIR))
        from utils import getFilenameOfLine  # type: ignore

        return (getFilenameOfLine(tagged) or "").strip()
    except Exception:
        topic = (line.get("topic") or line.get("raw") or "").lower()
        return re.sub(r"[^a-z0-9 -]+", "", topic).strip()


def _win_long_path(path: Path) -> Path:
    """Optional \\\\?\\ prefix backup when a path is still near Windows MAX_PATH."""
    if os.name != "nt":
        return path
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    if resolved.startswith("\\\\?\\"):
        return Path(resolved)
    if len(resolved) < WIN_PATH_SOFT_MAX:
        return path
    if resolved.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + resolved[2:])
    return Path("\\\\?\\" + resolved)


def _write_bytes_path_aware(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(data)
    except OSError:
        _win_long_path(path).write_bytes(data)


def _replace_path_aware(src: Path, dest: Path) -> None:
    try:
        os.replace(src, dest)
    except OSError:
        os.replace(_win_long_path(src), _win_long_path(dest))


def migrate_billboard_short_names(project_id: str) -> dict:
    """Rename legacy topic-text billboard PNGs to b001.png… and rewrite lines.json.

    Keeps video render + MCP list/save under Windows MAX_PATH. Safe to call repeatedly.
    """
    renamed: list[dict] = []
    updated_lines = False

    def _migrate_folder(folder: Path, lines: list[dict]) -> None:
        nonlocal renamed
        if not folder.is_dir() or not lines:
            return
        for i, line in enumerate(lines):
            short_stem = line_billboard_stem(i + 1)
            short_path = folder / f"{short_stem}.png"
            legacy_stem = _legacy_line_stem(line)
            old_stem = (line.get("filename") or "").strip()
            candidates: list[Path] = []
            for stem in (old_stem, legacy_stem):
                if not stem or stem == short_stem:
                    continue
                cand = folder / f"{stem}.png"
                if cand.is_file() and cand not in candidates:
                    candidates.append(cand)
            if short_path.is_file():
                # Drop duplicate legacy copies once the short slot exists.
                for cand in candidates:
                    try:
                        if cand.resolve() != short_path.resolve():
                            cand.unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            if not candidates:
                continue
            src = candidates[0]
            try:
                src.replace(short_path)
                renamed.append({"from": src.name, "to": short_path.name, "folder": str(folder)})
                for extra in candidates[1:]:
                    try:
                        extra.unlink(missing_ok=True)
                    except OSError:
                        pass
            except OSError as exc:
                log.warning("Could not rename %s → %s: %s", src, short_path, exc)

    try:
        lines = load_lines(project_id)
    except Exception:
        lines = []
    if lines:
        new_lines = []
        for i, line in enumerate(lines):
            item = dict(line)
            short = line_billboard_stem(i + 1)
            if item.get("filename") != short:
                item["filename"] = short
                updated_lines = True
            new_lines.append(item)
        _migrate_folder(billboards_dir(project_id, ASPECT_16_9), new_lines)
        if updated_lines:
            try:
                save_lines(project_id, new_lines)
            except Exception as exc:
                log.warning("Could not rewrite lines.json short names: %s", exc)

    # 9:16 hook lines live in a separate folder / script.
    try:
        sp = Path(shorts_input_prefix(project_id)).with_suffix(".txt")
        if sp.is_file():
            shorts_tagged = sp.read_text(encoding="utf-8")
            if shorts_tagged.strip():
                meta = load_meta(project_id)
                shorts_lines = parse_tagged_script(
                    shorts_tagged,
                    title=meta.get("title") or "",
                    summary=meta.get("summary") or "",
                    aspect=ASPECT_9_16,
                    layout="cover",
                    image_provider=project_image_provider(project_id),
                )
                _migrate_folder(billboards_dir(project_id, ASPECT_9_16), shorts_lines)
    except Exception as exc:
        log.warning("Shorts billboard migrate skipped: %s", exc)

    return {"renamed": renamed, "lines_updated": updated_lines}

# MCP operator note only — never concatenated into the fal / ComfyUI prompt.
FLUX_MCP_NOTE = (
    "This job uses Flux (fal-ai/flux-2). Call generate_illustrations_with_flux "
    "so Studio generates the 5-second title-card cover and missing line backgrounds "
    "with the fal client using live app prompts (images.flux_instructions + art.* only). "
    "Do not invent an art style. Do not generate images in chat. Do not send the ChatGPT "
    "playbook, script.rules, or these MCP instructions to fal."
)
COMFYUI_MCP_NOTE = (
    "This job uses ComfyUI. Call generate_illustrations_with_flux so Studio POSTs the "
    "uploaded API-format workflow to ComfyUI /prompt (not fal, not ChatGPT images) with "
    "live app prompts (images.flux_instructions + art.*). Do not invent an art style. "
    "Upload a Save (API Format) JSON in Settings first. Do not generate images in chat. "
    "Do not send the ChatGPT playbook, script.rules, or these MCP instructions to ComfyUI."
)

_active_lock = threading.Lock()
# project_id -> {filename, provider, fal_app, fal_request_id, comfy_prompt_id, cancel}
_active_gens: dict[str, dict] = {}


class ImageProviderSwitched(Exception):
    """In-flight image batch should abandon the current slot and resume with a new provider."""

    def __init__(
        self,
        provider: str,
        remaining_filenames: list[str] | None = None,
        slot: str | None = None,
    ):
        self.provider = provider
        self.remaining_filenames = [str(name) for name in (remaining_filenames or []) if name]
        self.slot = slot
        super().__init__(f"image_provider switched to {provider}")


def active_image_generation(project_id: str) -> dict | None:
    with _active_lock:
        blob = _active_gens.get(project_id)
        if not blob:
            return None
        out = {k: v for k, v in blob.items() if k not in ("cancel", "fal_handle")}
        return out


def _begin_active_generation(project_id: str, filename: str, provider: str) -> threading.Event:
    cancel = threading.Event()
    with _active_lock:
        prior = _active_gens.get(project_id)
        if prior and prior.get("cancel"):
            try:
                prior["cancel"].set()
            except Exception:
                pass
        _active_gens[project_id] = {
            "filename": filename,
            "provider": provider,
            "fal_app": None,
            "fal_request_id": None,
            "fal_handle": None,
            "comfy_prompt_id": None,
            "cancel": cancel,
        }
    return cancel


def _update_active_generation(project_id: str, **fields) -> None:
    with _active_lock:
        blob = _active_gens.get(project_id)
        if not blob:
            return
        blob.update(fields)


def _end_active_generation(project_id: str, filename: str | None = None) -> None:
    with _active_lock:
        blob = _active_gens.get(project_id)
        if not blob:
            return
        if filename and blob.get("filename") and blob.get("filename") != filename:
            return
        _active_gens.pop(project_id, None)


def cancel_active_image_generation(project_id: str) -> dict:
    """Cancel the in-flight Flux/Comfy/ChatGPT wait for this job (provider switch)."""
    with _active_lock:
        blob = dict(_active_gens.get(project_id) or {})
        cancel = blob.get("cancel")
        fal_handle = blob.get("fal_handle")
        if cancel:
            try:
                cancel.set()
            except Exception:
                pass
    if not blob:
        return {"ok": True, "canceled": False, "reason": "idle"}

    provider = blob.get("provider") or ""
    canceled = {"ok": True, "canceled": True, "provider": provider, "filename": blob.get("filename")}

    if provider == "flux":
        request_id = blob.get("fal_request_id")
        app = blob.get("fal_app") or FLUX_MODEL
        try:
            if fal_handle is not None and hasattr(fal_handle, "cancel"):
                fal_handle.cancel()
                canceled["fal_canceled"] = True
            elif request_id:
                client = _configure_fal()
                if hasattr(client, "cancel"):
                    client.cancel(app, request_id)
                else:
                    import fal_client

                    fal_client.cancel(app, request_id)
                canceled["fal_canceled"] = True
            else:
                canceled["fal_canceled"] = False
                canceled["fal_error"] = "no request id yet"
        except Exception as exc:
            log.info("fal cancel failed for %s: %s", request_id, exc)
            canceled["fal_canceled"] = False
            canceled["fal_error"] = str(exc)
    elif provider == "comfyui":
        try:
            from studio.comfyui import interrupt_comfyui

            canceled["comfy_interrupted"] = interrupt_comfyui()
        except Exception as exc:
            log.info("ComfyUI interrupt failed: %s", exc)
            canceled["comfy_interrupted"] = False
            canceled["comfy_error"] = str(exc)
    elif provider == "chatgpt":
        canceled["chatgpt_cleared"] = True

    return canceled


def _raise_if_canceled(
    project_id: str,
    cancel: threading.Event | None = None,
    cancel_check: Callable[[], None] | None = None,
    remaining_filenames: list[str] | None = None,
    slot: str | None = None,
) -> None:
    if cancel_check:
        cancel_check()
    if cancel and cancel.is_set():
        provider = project_image_provider(project_id)
        raise ImageProviderSwitched(provider, remaining_filenames=remaining_filenames, slot=slot)
    try:
        from studio.pipeline import provider_switch_pending

        pending = provider_switch_pending(project_id)
    except Exception:
        pending = None
    if pending:
        raise ImageProviderSwitched(
            pending, remaining_filenames=remaining_filenames, slot=slot
        )


def prompt_for_provider(provider: str | None) -> str:
    if provider == "chatgpt":
        return "chatgpt-native"
    if provider == "comfyui":
        return "comfyui"
    return FLUX_MODEL


def _backend_label(provider: str) -> str:
    if provider == "comfyui":
        return "ComfyUI"
    if provider == "chatgpt":
        return "ChatGPT native"
    return "Flux"


def is_cover_filename(filename: str | None, kind: str = "") -> bool:
    if (kind or "").strip().lower() == "cover":
        return True
    stem = Path(filename or "").stem.lower().replace("-", "_")
    return stem in {"script_cover", "cover"} or stem.startswith("script_cover_")


def _file_stem(name: str | None) -> str:
    return Path(name or "").stem.lower().replace("-", "_")


def normalize_slot_kind(kind: str | None) -> str:
    value = (kind or "").strip().lower()
    if value in {"cover"}:
        return "cover"
    if value in {"line", "billboard", "background", "illustration"}:
        return "line"
    return ""


def pending_regen_filenames(project_id: str) -> list[str]:
    try:
        meta = load_meta(project_id)
    except FileNotFoundError:
        return []
    raw = meta.get(PENDING_REGEN_KEY) or []
    if not isinstance(raw, list):
        return []
    return [str(name) for name in raw if name]


def mark_pending_regen(project_id: str, filename: str) -> None:
    name = Path(filename).name
    meta = load_meta(project_id)
    names = [str(item) for item in (meta.get(PENDING_REGEN_KEY) or []) if item]
    if name not in names:
        names.append(name)
        meta[PENDING_REGEN_KEY] = names
        save_meta(project_id, meta)


def mark_all_slots_pending_regen(project_id: str) -> list[str]:
    """Mark cover + every line slot as needs_regen (ChatGPT native wait)."""
    info = illustration_jobs(project_id)
    names: list[str] = []
    for job in info.get("jobs") or []:
        fname = job.get("filename")
        if not fname:
            continue
        mark_pending_regen(project_id, fname)
        names.append(fname)
    return names


def prune_stale_billboards(project_id: str) -> list[str]:
    """Delete line PNGs whose stems are no longer in the current script / short."""
    info = illustration_jobs(project_id)
    keep_by_folder: dict[str, set[str]] = {}
    for job in info.get("jobs") or []:
        if job.get("role") == "cover" or is_cover_filename(job.get("filename")):
            continue
        stem = _file_stem(job.get("filename"))
        if not stem:
            continue
        folder = str(Path(job.get("save_path") or "").parent)
        keep_by_folder.setdefault(folder, set()).add(stem)
    removed: list[str] = []
    folders = [
        billboards_dir(project_id, ASPECT_16_9),
        billboards_dir(project_id, ASPECT_9_16),
    ]
    for folder in folders:
        if not folder.is_dir():
            continue
        keep = keep_by_folder.get(str(folder), set())
        for path in folder.glob("*.png"):
            if path.stem in keep:
                continue
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


def clear_pending_regen(project_id: str, filename: str) -> None:
    stem = _file_stem(filename)
    if not stem:
        return
    try:
        meta = load_meta(project_id)
    except FileNotFoundError:
        return
    names = [str(item) for item in (meta.get(PENDING_REGEN_KEY) or []) if item]
    kept = [item for item in names if _file_stem(item) != stem]
    if kept != names:
        meta[PENDING_REGEN_KEY] = kept
        save_meta(project_id, meta)


def cover_intro_prompt(project_id: str, aspect: str | None = None) -> str:
    from studio.art_style import art_style_texts

    meta = load_meta(project_id)
    aspect = normalize_aspect(aspect or meta.get("aspect") or DEFAULT_ASPECT)
    width, height = canvas_size(aspect)
    topic = (meta.get("topic") or meta.get("title") or "").strip()
    title = (meta.get("title") or topic).strip()
    summary = (meta.get("summary") or "").strip()
    background_room = cover_background_room_clause(project_id)
    bubblehead_pose = cover_bubblehead_pose_clause(project_id, aspect)
    layout = cover_layout_clause(aspect)
    look = cover_bubblehead_look_clause()
    colors = cover_head_colors()
    style = art_style_texts(project_art_style(meta))
    prompt = get_prompt(
        "images.cover_intro",
        topic=topic or title,
        title=title,
        summary=summary,
        aspect=aspect,
        width=width,
        height=height,
        size_line=generate_at_line(aspect, "cover", provider=project_image_provider(project_id)),
        phrase=canvas_phrase(aspect, "cover"),
        background_room=background_room,
        bubblehead_pose=bubblehead_pose,
        bubblehead_look=look,
        cover_layout=layout,
        head_color=colors["head_color"],
        head_dark=colors["head_dark"],
        art_style_short=style["short"],
        art_style_full=style["full"],
        art_style=style["id"],
        art_style_label=style["label"],
        # Covers intentionally include Bubblehead + TV; do not inject art.no_character.
        no_character="",
    )
    return ensure_cover_composition(prompt, project_id, aspect, title or topic)


def _stat_image(path: Path, min_bytes: int = MIN_IMAGE_BYTES) -> tuple[bool, int]:
    try:
        st = path.stat()
    except OSError:
        return False, 0
    if not path.is_file() or st.st_size <= min_bytes:
        return False, 0
    return True, int(st.st_mtime * 1000)


def _slot_content_hash(path: Path) -> str:
    """Full SHA-256 hex of slot bytes (empty string if missing)."""
    try:
        if not path.is_file() or path.stat().st_size < MIN_IMAGE_BYTES:
            return ""
        from studio.content_cache import sha256_file

        return sha256_file(path)
    except OSError:
        return ""


def attach_illustration_file_meta(project_id: str, job: dict) -> dict:
    dest = Path(job.get("save_path") or "")
    filename = job.get("filename") or dest.name
    is_cover = job.get("role") == "cover" or is_cover_filename(filename)
    ready, mtime = _stat_image(dest)
    content_hash = _slot_content_hash(dest) if ready else ""
    if is_cover:
        cover_aspect = normalize_aspect(
            job.get("video_aspect") or job.get("aspect") or load_meta(project_id).get("aspect")
        )
        if ready and not cover_matches_canvas(dest, cover_aspect):
            ready = False
            job["needs_size_regen"] = True
            content_hash = ""
        rel = f"/api/projects/{project_id}/cover?aspect={cover_aspect}"
        url = f"{rel}&t={mtime}" if ready else rel
    else:
        aspect_q = ""
        if job.get("role") == "shorts_line" or normalize_aspect(job.get("video_aspect") or "") == ASPECT_9_16:
            aspect_q = "?aspect=9:16"
        rel = f"/api/projects/{project_id}/billboards/{filename}{aspect_q}"
        if ready:
            sep = "&" if aspect_q else "?"
            url = f"{rel}{sep}t={mtime}"
        else:
            url = rel
    job["ready"] = ready
    job["has_image"] = ready
    job["mtime"] = mtime
    job["url"] = url
    job["content_hash"] = content_hash
    job["sha256"] = content_hash
    return job


def cover_job(project_id: str, aspect: str | None = None) -> dict:
    migrate_legacy_aspect_files(project_id)
    meta = load_meta(project_id)
    current = normalize_aspect(meta.get("aspect") or DEFAULT_ASPECT)
    aspect = normalize_aspect(aspect or current)
    dest = cover_path(project_id, aspect)
    prompt = cover_intro_prompt(project_id, aspect=aspect)
    provider = project_image_provider(project_id)
    fields = job_image_fields(aspect, "cover", provider=provider)
    return attach_illustration_file_meta(
        project_id,
        {
            "index": -1 if aspect == ASPECT_16_9 else -2,
            "role": "cover",
            "filename": cover_filename(aspect),
            "topic": meta.get("topic") or meta.get("title") or "",
            "line": "",
            "prompt": prompt,
            "prompt_chars": len(prompt or ""),
            "prompt_for": prompt_for_provider(provider),
            "video_layout": "cover",
            "video_aspect": aspect,
            **fields,
            "save_path": str(dest),
            "duration_seconds": COVER_DURATION_SECONDS,
        },
    )


def resolve_project_image_aspect(project_id: str | dict | None = None) -> dict:
    """Studio project aspect the GUI / MCP must obey before generating images.

    Reads meta.aspect (16:9 | 9:16 | both) plus generate_9x16 the same way Pictures does.
    Never assume 16:9 — call this (or list_illustration_jobs) first.
    """
    from studio.projects import load_meta, project_generate_9x16, project_payload
    from studio.settings import load_settings

    if isinstance(project_id, dict):
        payload = project_id
        pid = str(payload.get("id") or payload.get("project_id") or "")
    else:
        pid = str(project_id or "")
        payload = project_payload(pid) if pid else {}
    settings = load_settings()
    job_aspect = normalize_job_aspect(
        payload.get("aspect") or settings.get("default_aspect") or DEFAULT_ASPECT
    )
    want_shorts = bool(project_generate_9x16(payload if payload else pid))
    render = list(aspects_to_render(job_aspect))
    cover_aspects = list(dict.fromkeys([*render, *([ASPECT_9_16] if want_shorts else [])]))
    canvas = {}
    for asp in ALL_ASPECTS:
        w, h = canvas_size(asp)
        fields = job_image_fields(asp, "cover")
        canvas[asp] = {
            "width": w,
            "height": h,
            "image_size": f"{w}x{h}",
            "chatgpt_preset": fields.get("chatgpt_preset") or asp,
            "cover_filename": cover_filename(asp),
            "required": asp in cover_aspects,
        }
    return {
        "project_id": pid,
        "aspect": job_aspect,
        "project_aspect": job_aspect,
        "default_aspect": normalize_job_aspect(settings.get("default_aspect") or DEFAULT_ASPECT),
        "generate_9x16": want_shorts,
        "aspects_to_render": render,
        "cover_aspects": cover_aspects,
        "primary_canvas": normalize_aspect(job_aspect),
        "canvas": canvas,
        "sizes": {
            ASPECT_16_9: "1920x1080",
            ASPECT_9_16: "1080x1920",
        },
        "rule": (
            "Read aspect/project_aspect from the app (this payload or list_illustration_jobs) "
            "before generating. Do not assume 16:9. "
            "16:9 → 1920x1080; 9:16 → 1080x1920; both → both cover slots + matching line art. "
            "Obey each job's width/height/aspect/image_size. Never stretch 16:9 into 9:16."
        ),
    }


def cover_jobs(project_id: str, aspects: Iterable[str] | None = None) -> list[dict]:
    wanted = tuple(normalize_aspect(a) for a in aspects) if aspects is not None else ALL_ASPECTS
    # Preserve order: 16:9 then 9:16 when both requested.
    ordered = [asp for asp in ALL_ASPECTS if asp in wanted]
    return [cover_job(project_id, asp) for asp in ordered]


def billboard_dir(project_id: str, aspect: str | None = None) -> Path:
    return billboards_dir(project_id, aspect)


def background_dir(project_id: str) -> Path:
    path = Path(input_prefix(project_id) + "_backgrounds")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_line_jobs(
    jobs: list[dict],
    *,
    project_id: str,
    lines: list[dict],
    title: str,
    summary: str,
    aspect: str,
    layout: str,
    provider: str,
    role: str,
    index_offset: int = 0,
) -> None:
    fields = job_image_fields(aspect, layout, provider=provider)
    style = project_art_style(project_id)
    seen: set[str] = set()
    for i, line in enumerate(lines):
        # Always use short b00N stems (ignore legacy topic filenames / ChatGPT titles).
        filename = line_billboard_stem(i + 1)
        if filename in seen:
            continue
        seen.add(filename)
        dest = billboard_dir(
            project_id, ASPECT_9_16 if role == "shorts_line" else ASPECT_16_9
        ) / f"{filename}.png"
        prompt = wrap_scene_prompt(
            line.get("scene") or line.get("topic") or line.get("raw") or "",
            title,
            summary,
            line.get("raw") or "",
            aspect=aspect,
            layout=layout,
            provider=provider,
            art_style=style,
        )
        jobs.append(
            attach_illustration_file_meta(
                project_id,
                {
                    "index": index_offset + i,
                    "role": role,
                    "filename": f"{filename}.png",
                    "topic": line.get("topic"),
                    "line": line.get("raw"),
                    "prompt": prompt,
                    "prompt_chars": len(prompt),
                    "prompt_for": prompt_for_provider(provider),
                    "video_layout": layout,
                    "video_aspect": aspect,
                    "art_style": style,
                    **fields,
                    "save_path": str(dest),
                },
            )
        )


def illustration_jobs(project_id: str) -> dict:
    migrate_legacy_aspect_files(project_id)
    try:
        migrate_billboard_short_names(project_id)
    except Exception as exc:
        log.warning("billboard short-name migrate failed: %s", exc)
    payload = project_payload(project_id)
    aspect_info = resolve_project_image_aspect(payload)
    job_aspect = normalize_job_aspect(aspect_info.get("aspect") or DEFAULT_ASPECT)
    aspect = normalize_aspect(job_aspect)
    layout = normalize_video_layout(payload.get("video_layout") or project_video_layout(project_id))
    provider = payload.get("image_provider") or project_image_provider(project_id)
    want_full = needs_explainer_assets(job_aspect)
    want_shorts = bool(aspect_info.get("generate_9x16"))
    # Full explainer line art is for the 16:9 canvas when both/16:9.
    # 9:16-only jobs skip full body lines and only generate hook short portraits.
    # generate_9x16 also forces hook-line portrait slots even on 16:9 jobs.
    line_aspect = ASPECT_16_9 if want_full else aspect
    line_layout = layout if want_full else "cover"
    line_fields = job_image_fields(line_aspect, line_layout, provider=provider)
    cover_fields = job_image_fields(aspect, "cover", provider=provider)
    video_w, video_h = canvas_size(aspect)
    size_line = generate_at_line(line_aspect, line_layout, provider=provider)
    phrase = canvas_phrase(line_aspect, line_layout)
    # Only list cover slots this project actually needs (16:9 / 9:16 / both + generate_9x16).
    jobs = cover_jobs(project_id, aspect_info.get("cover_aspects") or aspects_to_render(job_aspect))
    title = payload.get("title") or ""
    summary = payload.get("summary") or ""
    if want_full:
        _append_line_jobs(
            jobs,
            project_id=project_id,
            lines=payload.get("lines") or [],
            title=title,
            summary=summary,
            aspect=line_aspect,
            layout=layout,
            provider=provider,
            role="line",
            index_offset=0,
        )
    if want_shorts:
        shorts_tagged = ""
        try:
            shorts_info = write_shorts_scripts(project_id)
            shorts_tagged = shorts_info.get("script_tagged") or ""
        except Exception:
            try:
                sp = Path(shorts_input_prefix(project_id)).with_suffix(".txt")
                if sp.is_file():
                    shorts_tagged = sp.read_text(encoding="utf-8")
                else:
                    tagged = (payload.get("script_tagged") or "").strip()
                    if tagged:
                        shorts_tagged, _ = build_shorts_script(tagged)
            except Exception:
                shorts_tagged = ""
        shorts_lines = (
            parse_tagged_script(
                shorts_tagged,
                title=title,
                summary=summary,
                aspect=ASPECT_9_16,
                layout="cover",
                image_provider=provider,
            )
            if shorts_tagged
            else []
        )
        _append_line_jobs(
            jobs,
            project_id=project_id,
            lines=shorts_lines,
            title=title,
            summary=summary,
            aspect=ASPECT_9_16,
            layout="cover",
            provider=provider,
            role="shorts_line",
            index_offset=10_000,
        )
    if layout == "billboard" and want_full:
        shape = get_prompt(
            "images.shape_billboard",
            aspect=aspect,
            video_w=video_w,
            video_h=video_h,
            size_line=size_line,
            phrase=phrase,
        )
    else:
        shape = get_prompt(
            "images.shape_cover",
            phrase=phrase,
            size_line=size_line,
            aspect=aspect,
            video_w=video_w,
            video_h=video_h,
        )
    shorts_note = (
        " 9:16 short uses ONLY the hook lines plus script.shorts_cta; "
        "generate separate 1080x1920 portraits into script_9x16_billboards "
        "(never stretch 16:9 art). "
        if want_shorts
        else ""
    )
    if provider == "flux":
        instructions = FLUX_MCP_NOTE + shorts_note
    elif provider == "comfyui":
        instructions = COMFYUI_MCP_NOTE + shorts_note
    else:
        extra = (
            "Billboard line art for the 16:9 explainer stays the 16:9 landscape preset at 1920x1080. "
            "9:16 short hook lines are separate 1080x1920 portraits in script_9x16_billboards. "
            "Do not draw a TV, monitor, bezel, stand, or screen-in-screen in the PNG. "
            if layout == "billboard"
            else "Do not generate square, 1:1, or 4:5 images. "
        ) + shorts_note
        instructions = get_prompt("images.chatgpt_instructions", shape=shape, extra=extra)
    pending_stems = {_file_stem(name) for name in pending_regen_filenames(project_id)}
    cover_needed = set(aspect_info.get("cover_aspects") or aspects_to_render(job_aspect))
    for job in jobs:
        stem = _file_stem(job.get("filename"))
        pending_key = f"9x16:{stem}" if job.get("role") == "shorts_line" else stem
        job["needs_regen"] = stem in pending_stems or pending_key in pending_stems
        job["waiting_for"] = "chatgpt" if job["needs_regen"] and provider == "chatgpt" else ""
        if job.get("role") == "cover" or is_cover_filename(job.get("filename")):
            job_asp = normalize_aspect(job.get("video_aspect") or job.get("aspect") or aspect)
            job["required"] = job_asp in cover_needed
        else:
            job["required"] = True
        job["project_aspect"] = job_aspect
    pending = [job["filename"] for job in jobs if job.get("needs_regen")]
    shorts_size_bit = (
        " 9:16 short hook lines: 1080x1920 in script_9x16_billboards."
        if want_shorts
        else ""
    )
    if uses_short_image_prompt(provider):
        size_note = (
            f"Cover: {cover_fields['image_size']}. Line art: {line_fields['image_size']}"
            + (
                " (16:9 explainer billboard line art stays 1920x1080)."
                if layout == "billboard" and want_full
                else "."
            )
            + shorts_size_bit
            + (
                " ComfyUI injects those pixels into the uploaded API workflow and a SHORT "
                "scene prompt (images.flux_instructions + art.no_character; no ChatGPT playbook)."
                if provider == "comfyui"
                else " Flux sends images.flux_instructions + art.* only (no ChatGPT playbook)."
            )
        )
    else:
        size_note = (
            f"Covers: script_cover_16x9.png 1920x1080 and script_cover_9x16.png 1080x1920 "
            f"(do not stretch one into the other). Line art: short names b001.png, b002.png, … "
            f"at {line_fields['image_size']}"
            + (
                " (billboard line art stays 16:9 1920x1080 for the explainer)."
                if layout == "billboard" and want_full
                else "."
            )
            + shorts_size_bit
            + " "
            + BILLBOARD_NAMING_RULE
        )
    return {
        "project_id": project_id,
        "aspect": job_aspect,
        "project_aspect": job_aspect,
        "aspects_to_render": aspect_info.get("aspects_to_render") or list(aspects_to_render(job_aspect)),
        "cover_aspects": aspect_info.get("cover_aspects") or list(aspects_to_render(job_aspect)),
        "primary_canvas": aspect_info.get("primary_canvas") or aspect,
        "canvas": aspect_info.get("canvas") or {},
        "sizes": aspect_info.get("sizes")
        or {ASPECT_16_9: "1920x1080", ASPECT_9_16: "1080x1920"},
        "aspect_rule": aspect_info.get("rule")
        or (
            "Read aspect from this payload before generating. Do not assume 16:9. "
            "Obey each job's width/height/aspect/image_size."
        ),
        "video_layout": layout,
        "width": line_fields["width"],
        "height": line_fields["height"],
        "image_size": line_fields["image_size"],
        "pixel_size": line_fields["pixel_size"],
        "chatgpt_preset": line_fields["chatgpt_preset"],
        "cover_width": cover_fields["width"],
        "cover_height": cover_fields["height"],
        "cover_aspect": cover_fields["aspect"],
        "cover_image_size": cover_fields["image_size"],
        "size_note": size_note,
        "generate_at": size_line,
        "image_provider": provider,
        "flux_model": FLUX_MODEL,
        "instructions": instructions,
        "jobs": jobs,
        "missing": sum(1 for job in jobs if not job["has_image"]),
        "pending_regen": pending,
        "regenerate_one": (
            "Call regenerate_illustration(project_id, filename=...) or POST "
            f"/api/projects/{project_id}/illustrations/regenerate with filename or index "
            "and kind cover|line. Flux regenerates that one PNG via fal using app prompts "
            "(images.flux_instructions + art.*). ComfyUI regenerates via the uploaded local "
            "workflow with the same short app prompt. ChatGPT marks the slot (needs_regen) and "
            "returns the app prompt plus save_illustration_image args — paste prompt verbatim; "
            "do not invent an art style."
        ),
        "cover_duration_seconds": COVER_DURATION_SECONDS,
        "shorts_hook_only": want_shorts,
        "generate_9x16": want_shorts,
        "naming_rule": BILLBOARD_NAMING_RULE,
        "filename_scheme": {
            "cover_16x9": cover_filename(ASPECT_16_9),
            "cover_9x16": cover_filename(ASPECT_9_16),
            "line": "b001.png, b002.png, … (1-based script order)",
            "note": "Always use jobs[].filename / jobs[].save_path; ignore ChatGPT image titles.",
        },
    }


def illustration_slot_inventory(project_id: str) -> dict[str, Any]:
    """total/missing/filled from the same jobs list as list_illustration_jobs."""
    try:
        info = illustration_jobs(project_id)
    except Exception:
        return {"total": 0, "missing": 0, "filled": 0, "missing_names": []}
    jobs = info.get("jobs") or []
    total = len(jobs)
    missing_names = [
        str(job.get("filename") or job.get("name") or job.get("id") or "?")
        for job in jobs
        if not job.get("has_image")
    ]
    missing = len(missing_names)
    return {
        "total": total,
        "missing": int(missing),
        "filled": max(0, total - int(missing)),
        "missing_names": missing_names,
    }


def all_illustration_slots_filled(project_id: str) -> bool:
    """True when the project has at least one slot and none are missing images."""
    inv = illustration_slot_inventory(project_id)
    return inv["total"] > 0 and inv["missing"] == 0


def unsupervised_image_provider_gate_skip_message(project_id: str) -> str | None:
    """If every illustration slot is filled, return the skip log line; else None.

    Unsupervised entry points (start_job / resume_job / schedule / hands-off) use this
    before requiring flux+fal or comfyui when image_provider is chatgpt/external.
    """
    inv = illustration_slot_inventory(project_id)
    if inv["total"] > 0 and inv["missing"] == 0:
        return (
            f"image provider gate skipped: all {inv['total']} illustration slots already filled"
        )
    return None


def require_external_images(project_id: str, *, provider: str = "external") -> dict[str, Any]:
    """Ensure MCP/agent-uploaded cover + line art exist for chatgpt/external providers."""
    from studio.job_errors import EXTERNAL_IMAGES_MISSING

    label = (provider or "external").strip() or "external"
    inv = illustration_slot_inventory(project_id)
    if inv["total"] <= 0:
        raise RuntimeError(
            f"[{EXTERNAL_IMAGES_MISSING}] image_provider={label} but this job has no "
            "illustration slots yet (save a tagged script first, then "
            "save_illustration_image / save_illustration_images for cover + each line)."
        )
    if inv["missing"] > 0:
        missing = ", ".join(str(x) for x in (inv.get("missing_names") or [])[:8])
        extra = f" Missing: {missing}." if missing else ""
        raise RuntimeError(
            f"[{EXTERNAL_IMAGES_MISSING}] image_provider={label} but {inv['missing']} of "
            f"{inv['total']} picture slots are empty.{extra} "
            "Generate in MCP/ChatGPT then call save_illustration_image (or bulk "
            "save_illustration_images). Studio will not call Flux/ComfyUI."
        )
    return {
        "ok": True,
        "total": inv["total"],
        "missing": 0,
        "provider": label,
    }


def _decode_image_bytes(image: str, image_url: str | None = None) -> bytes:
    from studio.content_cache import MAX_BASE64_CHARS, MAX_IMAGE_UPLOAD_BYTES

    if image_url:
        response = httpx.get(image_url, timeout=120, follow_redirects=True)
        response.raise_for_status()
        data = response.content
        if len(data) > MAX_IMAGE_UPLOAD_BYTES:
            raise RuntimeError(
                f"Downloaded image is {len(data)} bytes (max {MAX_IMAGE_UPLOAD_BYTES})."
            )
        return data
    blob = (image or "").strip()
    if not blob:
        raise RuntimeError("No image data provided.")
    if blob.startswith("http://") or blob.startswith("https://"):
        response = httpx.get(blob, timeout=120, follow_redirects=True)
        response.raise_for_status()
        data = response.content
        if len(data) > MAX_IMAGE_UPLOAD_BYTES:
            raise RuntimeError(
                f"Downloaded image is {len(data)} bytes (max {MAX_IMAGE_UPLOAD_BYTES})."
            )
        return data
    if blob.startswith("data:"):
        blob = blob.split(",", 1)[1]
    blob = re.sub(r"\s+", "", blob)
    if len(blob) > MAX_BASE64_CHARS:
        raise RuntimeError(
            f"Base64 image payload is too large ({len(blob)} chars; max {MAX_BASE64_CHARS}). "
            "Use save_illustration_images (bulk), binary multipart POST "
            "/api/projects/{id}/illustrations, or image_url instead of giant JSON base64."
        )
    try:
        data = base64.b64decode(blob, validate=False)
    except Exception as exc:
        raise RuntimeError(f"Invalid base64 image data: {exc}") from exc
    if len(data) > MAX_IMAGE_UPLOAD_BYTES:
        raise RuntimeError(
            f"Decoded image is {len(data)} bytes (max {MAX_IMAGE_UPLOAD_BYTES}). "
            "Use bulk or binary upload for large files."
        )
    if len(data) < MIN_IMAGE_BYTES:
        raise RuntimeError("Decoded image is empty or too small.")
    return data


def save_illustration(
    project_id: str,
    filename: str | None = None,
    line_index: int | None = None,
    image: str = "",
    image_url: str | None = None,
    kind: str = "billboard",
    aspect: str | None = None,
    save_path: str | None = None,
    prompt_used: str | None = None,
) -> dict:
    """Save a PNG into the correct project slot so Pictures / list_illustration_jobs see it.

    Resolves cover vs line vs 9:16 shorts folders from illustration_jobs when possible.
    Long client filenames (ChatGPT image titles) are ignored — bytes always land on the
    short canonical slot path (b001.png / script_cover_*.png). Official MCP/API path
    stamps prompt_source=app (Studio catalog prompts).
    """
    try:
        migrate_billboard_short_names(project_id)
    except Exception:
        pass
    slot: dict | None = None
    expected_prompt = ""
    client_filename = (filename or "").strip() or None
    resolve_error = ""
    # Prefer explicit line_index / kind over a long ChatGPT title as the disk name.
    if line_index is not None and not is_cover_filename(client_filename, kind):
        try:
            slot, _info = resolve_illustration_slot(
                project_id, index=int(line_index), kind=kind or "line"
            )
        except Exception as exc:
            slot = None
            resolve_error = str(exc)
    if slot is None and (client_filename or normalize_slot_kind(kind) == "cover"):
        try:
            slot, _info = resolve_illustration_slot(
                project_id,
                filename=client_filename,
                kind=kind or None,
            )
        except Exception as exc:
            slot = None
            resolve_error = str(exc)

    if slot:
        filename = slot.get("filename") or client_filename
        save_path = slot.get("save_path") or save_path
        expected_prompt = (slot.get("prompt") or "").strip()
        if slot.get("role") == "cover" or is_cover_filename(filename, kind):
            kind = "cover"
        elif slot.get("role") == "shorts_line":
            kind = "shorts_line"
            aspect = ASPECT_9_16
        elif kind in ("", "billboard", "line"):
            kind = "billboard"
    elif line_index is not None and not client_filename:
        lines = load_lines(project_id)
        if line_index < 0 or line_index >= len(lines):
            raise RuntimeError("line_index is out of range.")
        filename = f"{line_billboard_stem(line_index + 1)}.png"
    elif client_filename:
        filename = client_filename
    else:
        expected = []
        try:
            expected = [
                str(j.get("filename"))
                for j in (illustration_jobs(project_id).get("jobs") or [])[:8]
                if j.get("filename")
            ]
        except Exception:
            pass
        hint = f" Expected filenames include: {', '.join(expected)}." if expected else ""
        raise RuntimeError(
            "Provide filename or line_index (and optional kind: cover|line). "
            "Use the exact short jobs[].filename from list_illustration_jobs "
            f"(b001.png / script_cover_16x9.png) — never a long ChatGPT image title.{hint}"
        )

    if not filename:
        raise RuntimeError("Provide filename or line_index.")

    # Never write a long/messy client name to disk — force short canonical paths.
    stem = Path(filename).stem
    if save_path:
        dest = Path(save_path)
        if is_cover_filename(filename, kind):
            kind = "cover"
    elif is_cover_filename(filename, kind) or normalize_slot_kind(kind) == "cover":
        meta = load_meta(project_id)
        named = aspect_from_cover_filename(filename)
        aspect = named or normalize_aspect(aspect or meta.get("aspect") or DEFAULT_ASPECT)
        dest = cover_path(project_id, aspect)
        kind = "cover"
        filename = dest.name
    elif kind == "background":
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem)[:48] or "bg"
        dest = background_dir(project_id) / f"{safe_stem}.png"
    else:
        if not is_short_billboard_stem(stem):
            expected = []
            try:
                expected = [
                    str(j.get("filename"))
                    for j in (illustration_jobs(project_id).get("jobs") or [])
                    if j.get("role") != "cover" and j.get("filename")
                ][:12]
            except Exception:
                pass
            hint = f" Expected: {', '.join(expected)}." if expected else ""
            extra = f" ({resolve_error})" if resolve_error else ""
            raise RuntimeError(
                f"Unknown or non-canonical illustration filename {client_filename!r}{extra}. "
                "Pass jobs[].filename from list_illustration_jobs (b001.png, b002.png, …) "
                f"or kind='cover' / line_index=N. Do not use ChatGPT's long image title as the disk name.{hint}"
            )
        line_aspect = normalize_aspect(aspect) if aspect else None
        if kind in ("shorts", "shorts_line") or line_aspect == ASPECT_9_16:
            dest = billboard_dir(project_id, ASPECT_9_16) / f"{stem}.png"
            kind = "shorts_line"
        else:
            dest = billboard_dir(project_id, ASPECT_16_9) / f"{stem}.png"
        filename = dest.name

    if len(str(dest)) >= 240:
        raise RuntimeError(
            f"Refusing to write path of length {len(str(dest))} (Windows MAX_PATH risk): {dest}. "
            "Use short b001.png / script_cover_*.png slot names."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = load_meta(project_id)
    job_aspect = normalize_aspect(meta.get("aspect") or DEFAULT_ASPECT)
    data = _decode_image_bytes(image, image_url)
    from studio.content_cache import sha256_bytes

    inbound_hash = sha256_bytes(data)
    # Idempotent: same bytes already on slot → no rewrite (avoids upload_reset churn).
    if dest.is_file() and dest.stat().st_size >= MIN_IMAGE_BYTES:
        existing = _slot_content_hash(dest)
        if existing and existing == inbound_hash:
            job_meta = {
                "filename": dest.name,
                "role": "cover" if kind == "cover" else ("shorts_line" if kind == "shorts_line" else "line"),
                "save_path": str(dest),
                "aspect": aspect or (ASPECT_9_16 if kind == "shorts_line" else job_aspect),
                "video_aspect": aspect or (ASPECT_9_16 if kind == "shorts_line" else job_aspect),
            }
            if slot:
                for key in ("index", "width", "height", "image_size", "chatgpt_preset", "video_aspect", "aspect"):
                    if slot.get(key) is not None:
                        job_meta[key] = slot.get(key)
            attach_illustration_file_meta(project_id, job_meta)
            return {
                "ok": True,
                "path": str(dest),
                "filename": dest.name,
                "kind": kind,
                "project_id": project_id,
                "content_hash": existing,
                "sha256": existing,
                "unchanged": True,
                "ready": bool(job_meta.get("ready")),
                "has_image": bool(job_meta.get("has_image")),
                "url": job_meta.get("url") or "",
                "mtime": job_meta.get("mtime") or 0,
                "index": job_meta.get("index"),
                "gui_visible": True,
                "note": "Slot already had identical content; skipped rewrite.",
            }
    # Stage + validate/fit BEFORE touching the canonical slot so a wrong-size
    # reject never overwrites good art (or leaves a bad PNG on disk).
    staging = dest.with_suffix(".staging.png")
    named_cover = None
    # Archive the current active cover before replace (legacy projects / first history seed).
    if (is_cover_filename(filename, kind) or normalize_slot_kind(kind) == "cover") and dest.is_file():
        try:
            cover_asp = aspect_from_cover_filename(dest.name) or normalize_aspect(
                aspect or job_aspect
            )
            commit_cover_to_history(project_id, cover_asp, dest)
        except Exception:
            pass
    try:
        _write_bytes_path_aware(staging, data)
        with Image.open(staging) as im:
            im.convert("RGBA").save(staging)
        if kind == "cover":
            named_cover = aspect_from_cover_filename(dest.name) or normalize_aspect(
                aspect or job_aspect
            )
            fit_illustration_to_canvas(staging, named_cover, layout="cover")
        elif kind == "shorts_line":
            fit_illustration_to_canvas(staging, ASPECT_9_16, layout="cover")
        elif kind != "background" and project_video_layout(project_id) != "billboard":
            # Cover-layout line art must match the job canvas (ChatGPT often defaults to square).
            fit_illustration_to_canvas(staging, job_aspect, layout="cover")
        _replace_path_aware(staging, dest)
    except Exception as exc:
        staging.unlink(missing_ok=True)
        # Map connection-ish failures for structured clients.
        msg = str(exc)
        if "reset" in msg.lower() or "broken pipe" in msg.lower():
            from studio.job_errors import UPLOAD_RESET

            raise RuntimeError(f"[{UPLOAD_RESET}] {msg}") from exc
        raise
    finally:
        staging.unlink(missing_ok=True)

    if kind == "cover":
        publish_cover_alias(project_id, dest, named_cover or job_aspect)
        try:
            commit_cover_to_history(project_id, named_cover or job_aspect, dest)
        except Exception:
            pass
        try:
            from studio.thumbs import ensure_thumbnail

            ensure_thumbnail(project_id)
        except Exception:
            pass
    clear_pending_regen(project_id, dest.name)

    prompt_match: bool | None = None
    used = (prompt_used or "").strip()
    if used and expected_prompt:
        # Soft check: official path still saves; flag drift for audit/GUI.
        prompt_match = used == expected_prompt or expected_prompt in used or used in expected_prompt

    # Stamp last official save so Pictures knows the slot was filled via Studio path.
    try:
        meta = load_meta(project_id)
        stamps = dict(meta.get("illustration_saves") or {})
        stamps[dest.name] = {
            "ts": int(time.time()),
            "kind": kind,
            "prompt_source": "app",
            "prompt_match": prompt_match,
            "path": str(dest),
            "client_filename": client_filename,
            "content_hash": _slot_content_hash(dest),
        }
        meta["illustration_saves"] = stamps
        meta["last_illustration_save"] = {
            "filename": dest.name,
            "kind": kind,
            "prompt_source": "app",
            "ts": stamps[dest.name]["ts"],
        }
        save_meta(project_id, meta)
    except Exception:
        pass

    job_meta = {
        "filename": dest.name,
        "role": "cover" if kind == "cover" else ("shorts_line" if kind == "shorts_line" else "line"),
        "save_path": str(dest),
        "aspect": aspect or (ASPECT_9_16 if kind == "shorts_line" else job_aspect),
        "video_aspect": aspect or (ASPECT_9_16 if kind == "shorts_line" else job_aspect),
    }
    if slot:
        for key in ("index", "width", "height", "image_size", "chatgpt_preset", "video_aspect", "aspect"):
            if slot.get(key) is not None:
                job_meta[key] = slot.get(key)
    attach_illustration_file_meta(project_id, job_meta)

    out = {
        "ok": True,
        "path": str(dest),
        "filename": dest.name,
        "kind": kind,
        "project_id": project_id,
        "prompt_source": "app",
        "ready": bool(job_meta.get("ready")),
        "has_image": bool(job_meta.get("has_image")),
        "content_hash": job_meta.get("content_hash") or _slot_content_hash(dest),
        "sha256": job_meta.get("content_hash") or _slot_content_hash(dest),
        "inbound_hash": inbound_hash,
        "url": job_meta.get("url") or "",
        "mtime": job_meta.get("mtime") or 0,
        "index": job_meta.get("index"),
        "gui_visible": True,
        "path_len": len(str(dest)),
        "note": (
            "Saved under this project using the short canonical slot name. "
            "Pictures tab / list_illustration_jobs will show it (refresh if the GUI was already open). "
            + BILLBOARD_NAMING_RULE
        ),
    }
    if client_filename and Path(client_filename).name.lower() != dest.name.lower():
        out["client_filename_ignored"] = Path(client_filename).name
        out["canonical_filename"] = dest.name
    if prompt_match is not None:
        out["prompt_match"] = prompt_match
        if not prompt_match:
            out["prompt_warning"] = (
                "prompt_used did not match the Studio slot prompt from list_illustration_jobs. "
                "Always paste jobs[].prompt verbatim (app art style)."
            )
    try:
        from studio.audit import write_entry

        write_entry(
            action="save_illustration",
            source="studio",
            success=True,
            args={
                "project_id": project_id,
                "filename": dest.name,
                "kind": kind,
                "prompt_source": "app",
                "prompt_match": prompt_match,
                "client_filename": client_filename,
            },
            extra={"path": str(dest), "path_len": len(str(dest))},
        )
    except Exception:
        pass
    return out



def save_upload(
    project_id: str,
    filename: str,
    data: bytes,
    kind: str = "billboard",
    aspect: str | None = None,
) -> dict:
    from studio.content_cache import MAX_IMAGE_UPLOAD_BYTES

    if len(data) > MAX_IMAGE_UPLOAD_BYTES:
        raise RuntimeError(
            f"Upload is {len(data)} bytes (max {MAX_IMAGE_UPLOAD_BYTES})."
        )
    encoded = base64.b64encode(data).decode("ascii")
    return save_illustration(
        project_id, filename=filename, image=encoded, kind=kind, aspect=aspect
    )


def save_illustration_images(
    project_id: str,
    images: list[dict] | str | None = None,
) -> dict:
    """Bulk-save many illustration slots in one call (target: 33 slots under 3 minutes).

    Each item: {filename|line_index, image|image_url, kind?, prompt_used?}.
    ``images`` may also be a JSON string. Saves are sequential with atomic writes;
    failures on one slot do not roll back prior successes.
    """
    import json as _json
    import time as _time

    if isinstance(images, str):
        images = _json.loads(images)
    items = list(images or [])
    if not items:
        raise RuntimeError("Pass images=[{filename, image|image_url, kind?}, ...].")
    started = _time.monotonic()
    results: list[dict] = []
    errors: list[dict] = []
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            errors.append({"index": i, "error": "Each image entry must be an object.", "error_code": "upload_reset"})
            continue
        try:
            out = save_illustration(
                project_id,
                filename=raw.get("filename") or None,
                line_index=raw.get("line_index") if raw.get("line_index") is not None else raw.get("index"),
                image=raw.get("image") or "",
                image_url=raw.get("image_url") or None,
                kind=raw.get("kind") or "billboard",
                aspect=raw.get("aspect") or None,
                save_path=raw.get("save_path") or None,
                prompt_used=raw.get("prompt_used") or None,
            )
            results.append(out)
        except Exception as exc:
            from studio.job_errors import classify_error

            errors.append(
                {
                    "index": i,
                    "filename": raw.get("filename"),
                    "error": str(exc),
                    "error_code": classify_error(exc),
                }
            )
    elapsed_ms = int((_time.monotonic() - started) * 1000)
    return {
        "ok": not errors,
        "project_id": project_id,
        "saved": len(results),
        "failed": len(errors),
        "total": len(items),
        "elapsed_ms": elapsed_ms,
        "results": results,
        "errors": errors,
        "slot_hashes": {
            r.get("filename"): r.get("content_hash") or r.get("sha256")
            for r in results
            if r.get("filename")
        },
    }


def _snap16(value: int) -> int:
    snapped = int(round(value / FLUX_MULTIPLE) * FLUX_MULTIPLE)
    return max(FLUX_MIN_SIDE, min(FLUX_MAX_SIDE, snapped))


def _flux_size_ok(width: int, height: int) -> bool:
    return (
        FLUX_MIN_SIDE <= width <= FLUX_MAX_SIDE
        and FLUX_MIN_SIDE <= height <= FLUX_MAX_SIDE
        and width % FLUX_MULTIPLE == 0
        and height % FLUX_MULTIPLE == 0
        and width * height <= FLUX_MAX_AREA
    )


def flux_request_size(width: int, height: int) -> tuple[int, int]:
    """Prefer exact canvas pixels when fal accepts them; otherwise snap to multiples of 16."""
    if _flux_size_ok(width, height):
        return width, height
    return _snap16(width), _snap16(height)


def fit_illustration_to_canvas(path: Path, aspect: str, layout: str = "cover") -> None:
    """Normalize a slot image to the canvas.

    Same-orientation wrong sizes are Lanczos cover-cropped (never rejected).
    Opposite orientation is still rejected (do not stretch 16:9 into 9:16).
    """
    from studio.image_normalize import fit_path_to_canvas, maybe_reencode_slot_png
    from studio.settings import normalize_video_layout

    layout = normalize_video_layout(layout)
    if layout == "billboard":
        # Keep TV-screen art as generated; videoDrawer composites it.
        return
    fit_path_to_canvas(path, aspect, layout="cover")
    try:
        maybe_reencode_slot_png(path)
    except Exception:
        pass


def _configure_fal():
    try:
        import fal_client
    except ImportError as exc:
        raise RuntimeError("fal-client is not installed. pip install fal-client") from exc
    key = (load_settings().get("fal_key") or "").strip()
    if not key:
        raise RuntimeError(
            "fal API key is not set. Add it in Settings, or set FAL_KEY / FAL_API_KEY."
        )
    os.environ["FAL_KEY"] = key
    return fal_client


def ensure_fal_ready() -> None:
    _configure_fal()


def ensure_image_backend_ready(provider: str | None = None) -> str:
    """Ping Flux or ComfyUI before queueing a Studio image job. Refuses agent/MCP upload providers."""
    from studio.settings import normalize_image_provider

    provider = normalize_image_provider(provider or load_settings().get("image_provider"))
    if provider in ("chatgpt", "external"):
        raise RuntimeError(
            f"This job's image_provider is {provider}. Generate natively / in MCP, then call "
            "save_illustration_image (or save_illustration_images). Do not call "
            "generate_illustrations_with_flux or fal."
        )
    if provider == "comfyui":
        from studio.comfyui import ensure_comfyui_ready

        ensure_comfyui_ready()
        return provider
    ensure_fal_ready()
    return provider


def _image_url_from_fal(result) -> str:
    if result is None:
        raise RuntimeError("Flux returned an empty result.")
    data = result if isinstance(result, dict) else None
    if data is None and hasattr(result, "keys"):
        data = dict(result)
    images = (data or {}).get("images") if data else None
    if not images:
        images = getattr(result, "images", None)
    if not images:
        raise RuntimeError(f"Flux returned no images: {result!r}")
    first = images[0]
    url = first.get("url") if isinstance(first, dict) else getattr(first, "url", None)
    if not url:
        raise RuntimeError("Flux image had no URL.")
    return str(url)


def _flux_prompt_log(label: str, prompt: str) -> None:
    text = (prompt or "").strip()
    n = len(text)
    preview = text if n <= 240 else text[:240] + "…"
    log.info("fal %s (%s chars): %s", label, n, preview)
    lowered = text.lower()
    leaked = any(
        marker in lowered
        for marker in (
            "chatgpt",
            "get_chatgpt_playbook",
            "save_illustration_image",
            "script.rules",
            "mcp handshake",
        )
    )
    if leaked or n > 2000:
        log.warning(
            "fal prompt looks too large or contains ChatGPT/MCP catalog text (%s chars)",
            n,
        )


def _run_flux(
    client,
    prompt: str,
    width: int,
    height: int,
    *,
    project_id: str | None = None,
    cancel: threading.Event | None = None,
    cancel_check: Callable[[], None] | None = None,
):
    arguments = {
        "prompt": prompt,
        "image_size": {"width": width, "height": height},
        "output_format": "png",
    }

    def _poll_handle(handle):
        request_id = getattr(handle, "request_id", None) or getattr(handle, "id", None)
        if project_id:
            _update_active_generation(
                project_id,
                fal_app=FLUX_MODEL,
                fal_request_id=str(request_id) if request_id else None,
                fal_handle=handle,
            )
        deadline = time.time() + 300
        while time.time() < deadline:
            _raise_if_canceled(project_id or "", cancel=cancel, cancel_check=cancel_check)
            try:
                status = handle.status() if hasattr(handle, "status") else None
                status_name = type(status).__name__ if status is not None else ""
                if status_name == "Completed" or getattr(status, "completed", None) is True:
                    return handle.get() if hasattr(handle, "get") else status
                # Still queued / in progress.
            except ImageProviderSwitched:
                try:
                    if hasattr(handle, "cancel"):
                        handle.cancel()
                    elif request_id and hasattr(client, "cancel"):
                        client.cancel(FLUX_MODEL, str(request_id))
                except Exception:
                    pass
                raise
            except Exception as exc:
                if cancel and cancel.is_set():
                    raise ImageProviderSwitched(project_image_provider(project_id or "")) from exc
                raise
            time.sleep(0.5)
        raise RuntimeError("Flux timed out waiting for fal result.")

    if hasattr(client, "submit"):
        handle = client.submit(FLUX_MODEL, arguments=arguments)
        return _poll_handle(handle)

    request_id_box: dict[str, str] = {}

    def _on_enqueue(request_id: str) -> None:
        request_id_box["id"] = str(request_id)
        if project_id:
            _update_active_generation(
                project_id, fal_app=FLUX_MODEL, fal_request_id=str(request_id)
            )

    if hasattr(client, "subscribe"):
        # Fall back when submit is missing; still register request_id for cancel.
        try:
            return client.subscribe(
                FLUX_MODEL,
                arguments=arguments,
                client_timeout=300,
                on_enqueue=_on_enqueue,
            )
        except Exception:
            if cancel and cancel.is_set():
                raise ImageProviderSwitched(project_image_provider(project_id or ""))
            raise
    return client.run(FLUX_MODEL, arguments=arguments)


def _flux_generate_to_path(
    client,
    prompt: str,
    dest_kind: str,
    filename: str,
    project_id: str,
    aspect: str,
    layout: str,
    width: int,
    height: int,
    note: Callable[[str], None],
    label: str,
    save_path: str | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    gen_w, gen_h = flux_request_size(width, height)
    _flux_prompt_log(label, prompt)
    note(f"{label} at {width}x{height} ({len(prompt or '')} chars)…")
    cancel = _begin_active_generation(project_id, filename, "flux")
    try:
        _raise_if_canceled(project_id, cancel=cancel, cancel_check=cancel_check, slot=filename)
        try:
            result = _run_flux(
                client,
                prompt,
                width,
                height,
                project_id=project_id,
                cancel=cancel,
                cancel_check=cancel_check,
            )
        except ImageProviderSwitched:
            raise
        except Exception as exc:
            if cancel.is_set():
                raise ImageProviderSwitched(project_image_provider(project_id), slot=filename) from exc
            if (gen_w, gen_h) == (width, height):
                raise RuntimeError(f"{label} failed: {exc}") from exc
            note(f"Flux size {width}x{height} failed ({exc}); retrying {gen_w}x{gen_h}…")
            try:
                result = _run_flux(
                    client,
                    prompt,
                    gen_w,
                    gen_h,
                    project_id=project_id,
                    cancel=cancel,
                    cancel_check=cancel_check,
                )
            except ImageProviderSwitched:
                raise
            except Exception as retry_exc:
                if cancel.is_set():
                    raise ImageProviderSwitched(
                        project_image_provider(project_id), slot=filename
                    ) from retry_exc
                raise RuntimeError(f"{label} failed: {retry_exc}") from retry_exc
        _raise_if_canceled(project_id, cancel=cancel, cancel_check=cancel_check, slot=filename)
        url = _image_url_from_fal(result)
        saved = save_illustration(
            project_id,
            filename=filename,
            image_url=url,
            kind=dest_kind,
            aspect=aspect,
            save_path=save_path,
        )
        fit_illustration_to_canvas(Path(saved["path"]), aspect, layout=layout)
        return saved["filename"]
    finally:
        _end_active_generation(project_id, filename)


def _generate_flux_slot(
    client,
    project_id: str,
    job: dict,
    aspect: str,
    layout: str,
    note: Callable[[str], None],
    label: str,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    prompt = (job.get("prompt") or "").strip()
    is_cover = job.get("role") == "cover" or is_cover_filename(job.get("filename"), "")
    is_shorts = job.get("role") == "shorts_line"
    if not prompt:
        if is_cover:
            raise RuntimeError("Cover prompt is empty. Check images.cover_intro on the Prompts page.")
        raise RuntimeError(f"No image prompt for {job['filename']}. Generate a script first.")
    job_layout = "cover" if (is_cover or is_shorts) else layout
    slot_aspect = normalize_aspect(job.get("video_aspect") or job.get("aspect") or aspect)
    if is_cover or is_shorts:
        aspect = slot_aspect
        job_layout = "cover"
    if job.get("width") and job.get("height"):
        canvas_w, canvas_h = int(job["width"]), int(job["height"])
    else:
        canvas_w, canvas_h = canvas_size(aspect) if (is_cover or is_shorts) else illustration_size(aspect, layout)
    if is_cover:
        kind = "cover"
    elif is_shorts:
        kind = "shorts_line"
    else:
        kind = "billboard"
    return _flux_generate_to_path(
        client,
        prompt,
        kind,
        job["filename"],
        project_id,
        aspect,
        job_layout,
        canvas_w,
        canvas_h,
        note,
        label,
        save_path=job.get("save_path"),
        cancel_check=cancel_check,
    )


def _comfyui_generate_to_path(
    prompt: str,
    dest_kind: str,
    filename: str,
    project_id: str,
    aspect: str,
    layout: str,
    width: int,
    height: int,
    note: Callable[[str], None],
    label: str,
    save_path: str | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    from studio.comfyui import generate_image_bytes

    text = (prompt or "").strip()
    _flux_prompt_log(label, text)
    note(f"{label} via ComfyUI at {width}x{height} ({len(text)} chars)…")
    cancel = _begin_active_generation(project_id, filename, "comfyui")

    def _combined_check() -> None:
        _raise_if_canceled(project_id, cancel=cancel, cancel_check=cancel_check, slot=filename)

    def _on_submitted(prompt_id: str) -> None:
        _update_active_generation(project_id, comfy_prompt_id=str(prompt_id))

    try:
        _combined_check()
        data = generate_image_bytes(
            text,
            int(width),
            int(height),
            cancel_check=_combined_check,
            on_submitted=_on_submitted,
        )
        _combined_check()
        encoded = base64.b64encode(data).decode("ascii")
        saved = save_illustration(
            project_id,
            filename=filename,
            image=encoded,
            kind=dest_kind,
            aspect=aspect,
            save_path=save_path,
        )
        fit_illustration_to_canvas(Path(saved["path"]), aspect, layout=layout)
        return saved["filename"]
    except ImageProviderSwitched:
        try:
            from studio.comfyui import interrupt_comfyui

            interrupt_comfyui()
        except Exception:
            pass
        raise
    finally:
        _end_active_generation(project_id, filename)


def _generate_comfyui_slot(
    project_id: str,
    job: dict,
    aspect: str,
    layout: str,
    note: Callable[[str], None],
    label: str,
    cancel_check: Callable[[], None] | None = None,
) -> str:
    prompt = (job.get("prompt") or "").strip()
    is_cover = job.get("role") == "cover" or is_cover_filename(job.get("filename"), "")
    is_shorts = job.get("role") == "shorts_line"
    if not prompt:
        if is_cover:
            raise RuntimeError("Cover prompt is empty. Check images.cover_intro on the Prompts page.")
        raise RuntimeError(f"No image prompt for {job['filename']}. Generate a script first.")
    job_layout = "cover" if (is_cover or is_shorts) else layout
    slot_aspect = normalize_aspect(job.get("video_aspect") or job.get("aspect") or aspect)
    if is_cover or is_shorts:
        aspect = slot_aspect
        job_layout = "cover"
    if job.get("width") and job.get("height"):
        canvas_w, canvas_h = int(job["width"]), int(job["height"])
    else:
        canvas_w, canvas_h = canvas_size(aspect) if (is_cover or is_shorts) else illustration_size(aspect, layout)
    if is_cover:
        kind = "cover"
    elif is_shorts:
        kind = "shorts_line"
    else:
        kind = "billboard"
    return _comfyui_generate_to_path(
        prompt,
        kind,
        job["filename"],
        project_id,
        aspect,
        job_layout,
        canvas_w,
        canvas_h,
        note,
        label,
        save_path=job.get("save_path"),
        cancel_check=cancel_check,
    )


def generate_cover_image(
    project_id: str,
    progress: Callable[[str], None] | None = None,
    force: bool = False,
    aspect: str | None = None,
) -> dict:
    def note(msg: str) -> None:
        if progress:
            progress(msg)

    migrate_legacy_aspect_files(project_id)
    meta = load_meta(project_id)
    aspect = normalize_aspect(aspect or meta.get("aspect") or DEFAULT_ASPECT)
    dest = cover_path(project_id, aspect)
    from studio.projects import cover_provider_is_agent, project_cover_provider

    cover_backend = project_cover_provider(project_id)
    provider = project_image_provider(project_id)
    ready = cover_matches_canvas(dest, aspect)
    if ready and not force:
        note(f"Title-card cover already exists for {aspect} ({dest.name}).")
        publish_cover_alias(project_id, dest, aspect)
        return {
            "ok": True,
            "path": str(dest),
            "filename": dest.name,
            "generated": False,
            "skipped": True,
            "image_provider": provider,
            "cover_provider": cover_backend,
            "aspect": aspect,
            "reason": "already_correct_size",
        }

    # manual / chatgpt cover override: never spend fal — mark needs_regen + return prompt.
    if cover_provider_is_agent(project_id) or cover_backend in ("manual", "chatgpt"):
        size = pixel_size(aspect)
        prompt = (cover_intro_prompt(project_id, aspect=aspect) or "").strip()
        mark_pending_regen(project_id, dest.name)
        note(
            f"cover_provider={cover_backend}: not auto-generating {dest.name}. "
            "Supply art via save_illustration_image / refresh_covers."
        )
        return {
            "ok": True,
            "path": str(dest),
            "filename": dest.name,
            "generated": False,
            "skipped": False,
            "native": True,
            "waiting": True,
            "waiting_for": cover_backend if cover_backend != "manual" else "manual",
            "needs_regen": True,
            "image_provider": provider,
            "cover_provider": cover_backend,
            "aspect": aspect,
            "prompt": prompt,
            "canvas_size": size,
            "instructions": (
                f"cover_provider is {cover_backend} — do not call fal for covers. "
                f"Generate {dest.name} at {size} with the returned prompt, then "
                f"save_illustration_image(filename={dest.name!r}, kind='cover') "
                "or refresh_covers(...). "
                + chatgpt_cover_help()
            ),
            "save": {
                "tool": "save_illustration_image",
                "filename": dest.name,
                "kind": "cover",
            },
            "reason": "cover_provider_agent",
        }

    if cover_backend == "comfyui" or (cover_backend == provider and provider == "comfyui"):
        from studio.comfyui import ensure_comfyui_ready

        ensure_comfyui_ready()
        prompt = (cover_intro_prompt(project_id, aspect=aspect) or "").strip()
        if not prompt:
            raise RuntimeError("Cover prompt is empty. Check images.cover_intro on the Prompts page.")
        width, height = canvas_size(aspect)
        other = cover_path(project_id, ASPECT_9_16 if aspect == ASPECT_16_9 else ASPECT_16_9)
        other_mtime = other.stat().st_mtime if other.is_file() else None
        reason = "missing" if not dest.is_file() else "wrong_size"
        note(f"Generating {dest.name} at {width}x{height} for {aspect} via ComfyUI…")
        filename = _comfyui_generate_to_path(
            prompt,
            "cover",
            cover_filename(aspect),
            project_id,
            aspect,
            "cover",
            width,
            height,
            note,
            f"ComfyUI title-card cover {aspect}",
        )
        dest = cover_path(project_id, aspect)
        if not cover_matches_canvas(dest, aspect):
            raise RuntimeError(
                f"Cover image for {aspect} was not {width}x{height} after generation."
            )
        if other_mtime is not None:
            still = other.stat().st_mtime if other.is_file() else None
            if still != other_mtime:
                raise RuntimeError("Refusing to clobber the other aspect's cover.")
        publish_cover_alias(project_id, dest, aspect)
        note(f"Saved 5s title-card cover {dest.name} ({reason} → regenerated).")
        return {
            "ok": True,
            "path": str(dest),
            "filename": filename,
            "generated": True,
            "skipped": False,
            "image_provider": "comfyui",
            "cover_provider": cover_backend,
            "aspect": aspect,
            "reason": reason,
            "duration_seconds": COVER_DURATION_SECONDS,
            "canvas_size": f"{width}x{height}",
        }

    # Default / flux cover backend
    if provider == "chatgpt" and cover_backend == "chatgpt":
        # unreachable via agent branch above, kept for safety
        size = pixel_size(aspect)
        raise RuntimeError(
            f"Title-card cover for {aspect} is missing ({dest.name} at {size}). "
            + chatgpt_cover_help()
        )
    prompt = (cover_intro_prompt(project_id, aspect=aspect) or "").strip()
    if not prompt:
        raise RuntimeError("Cover prompt is empty. Check images.cover_intro on the Prompts page.")
    width, height = canvas_size(aspect)
    other = cover_path(project_id, ASPECT_9_16 if aspect == ASPECT_16_9 else ASPECT_16_9)
    other_mtime = other.stat().st_mtime if other.is_file() else None
    client = _configure_fal()
    reason = "missing" if not dest.is_file() else "wrong_size"
    note(f"Generating {dest.name} at {width}x{height} for {aspect}…")
    filename = _flux_generate_to_path(
        client,
        prompt,
        "cover",
        cover_filename(aspect),
        project_id,
        aspect,
        "cover",
        width,
        height,
        note,
        f"Flux title-card cover {aspect}",
    )
    dest = cover_path(project_id, aspect)
    if not cover_matches_canvas(dest, aspect):
        raise RuntimeError(
            f"Cover image for {aspect} was not {width}x{height} after generation."
        )
    if other_mtime is not None:
        still = other.stat().st_mtime if other.is_file() else None
        if still != other_mtime:
            raise RuntimeError("Refusing to clobber the other aspect's cover.")
    publish_cover_alias(project_id, dest, aspect)
    note(f"Saved 5s title-card cover {dest.name} ({reason} → regenerated).")
    return {
        "ok": True,
        "path": str(dest),
        "filename": filename,
        "generated": True,
        "skipped": False,
        "image_provider": "flux",
        "cover_provider": cover_backend,
        "aspect": aspect,
        "reason": reason,
        "duration_seconds": COVER_DURATION_SECONDS,
        "canvas_size": f"{width}x{height}",
    }


def ensure_cover_for_aspect(
    project_id: str,
    aspect: str | None = None,
    progress: Callable[[str], None] | None = None,
    force: bool = False,
) -> dict:
    """Render-time cover: regen this aspect if missing or wrong size. Never stretch the other."""
    return generate_cover_image(project_id, progress=progress, force=force, aspect=aspect)


def _illustration_targets(
    project_id: str,
    force: bool = False,
    only_filenames: Iterable[str] | None = None,
) -> tuple[dict, list[dict], list[str]]:
    jobs_info = illustration_jobs(project_id)
    stored = normalize_job_aspect(jobs_info.get("aspect") or DEFAULT_ASPECT)
    from studio.projects import project_generate_9x16 as _pg9

    want_9x16 = _pg9(project_id)
    only = {Path(name).name for name in (only_filenames or []) if name} or None

    def _include_job(job: dict) -> bool:
        fname = Path(job.get("filename") or "").name
        if only is not None and fname not in only:
            return False
        is_cover = job.get("role") == "cover" or is_cover_filename(job.get("filename"))
        if is_cover:
            from studio.projects import cover_provider_is_agent

            # Lines may use flux while covers are manual/chatgpt — never auto-spend fal on covers.
            if cover_provider_is_agent(project_id):
                return False
        if not is_cover:
            return True
        if stored == ASPECT_BOTH or want_9x16:
            # generate_9x16 also needs script_cover_9x16.png
            job_aspect = normalize_aspect(job.get("video_aspect") or job.get("aspect") or stored)
            if want_9x16 and job_aspect == ASPECT_9_16:
                return True
            if stored == ASPECT_BOTH:
                return True
        job_aspect = normalize_aspect(job.get("video_aspect") or job.get("aspect") or stored)
        if job_aspect == normalize_aspect(stored):
            return True
        # Other-aspect cover: regenerate with the script (force), else leave for render-time.
        return force

    if force or only is not None:
        # only_filenames = overwrite those slots (provider switch mid-run / targeted resume).
        targets = [job for job in jobs_info["jobs"] if _include_job(job)]
        skipped: list[str] = [
            job["filename"] for job in jobs_info["jobs"] if not _include_job(job)
        ]
    else:
        targets = [
            job for job in jobs_info["jobs"] if _include_job(job) and not job["has_image"]
        ]
        skipped = [
            job["filename"]
            for job in jobs_info["jobs"]
            if job["has_image"] or not _include_job(job)
        ]
    return jobs_info, targets, skipped


def generate_flux_illustrations(
    project_id: str,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    force: bool = False,
    only_filenames: Iterable[str] | None = None,
) -> dict:
    from studio.gpu_lock import holding

    with holding(f"flux:{project_id}", kind="flux", project_id=project_id):
        return _generate_flux_illustrations_unlocked(
            project_id,
            progress=progress,
            cancel_check=cancel_check,
            force=force,
            only_filenames=only_filenames,
        )


def _generate_flux_illustrations_unlocked(
    project_id: str,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    force: bool = False,
    only_filenames: Iterable[str] | None = None,
) -> dict:
    def note(msg: str) -> None:
        if progress:
            progress(msg)

    jobs_info, targets, skipped = _illustration_targets(
        project_id, force=force, only_filenames=only_filenames
    )
    if not targets:
        note("Title-card cover and line backgrounds already exist.")
        return {
            "ok": True,
            "model": FLUX_MODEL,
            "image_provider": "flux",
            "generated": [],
            "skipped": skipped,
            "missing": 0,
        }

    client = _configure_fal()
    aspect = jobs_info["aspect"]
    layout = normalize_video_layout(jobs_info.get("video_layout") or "cover")
    generated = []
    total = len(targets)
    for i, job in enumerate(targets, start=1):
        remaining = [j["filename"] for j in targets[i - 1 :]]

        def _check(remaining=remaining, slot=job["filename"]) -> None:
            _raise_if_canceled(
                project_id,
                cancel_check=cancel_check,
                remaining_filenames=remaining,
                slot=slot,
            )

        _check()
        try:
            generated.append(
                _generate_flux_slot(
                    client,
                    project_id,
                    job,
                    aspect,
                    layout,
                    note,
                    f"Flux {i}/{total}: {job['filename']}",
                    cancel_check=_check,
                )
            )
        except ImageProviderSwitched as exc:
            if not exc.remaining_filenames:
                exc.remaining_filenames = remaining
            if not exc.slot:
                exc.slot = job["filename"]
            raise
    note(f"Saved {len(generated)} Flux image{'s' if len(generated) != 1 else ''} (cover + backgrounds).")
    return {
        "ok": True,
        "model": FLUX_MODEL,
        "image_provider": "flux",
        "generated": generated,
        "skipped": skipped,
        "missing": 0,
        "forced": force,
    }


def generate_comfyui_illustrations(
    project_id: str,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    force: bool = False,
    only_filenames: Iterable[str] | None = None,
) -> dict:
    from studio.gpu_lock import holding

    with holding(f"comfyui:{project_id}", kind="comfyui", project_id=project_id):
        return _generate_comfyui_illustrations_unlocked(
            project_id,
            progress=progress,
            cancel_check=cancel_check,
            force=force,
            only_filenames=only_filenames,
        )


def _generate_comfyui_illustrations_unlocked(
    project_id: str,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    force: bool = False,
    only_filenames: Iterable[str] | None = None,
) -> dict:
    from studio.comfyui import ensure_comfyui_ready

    def note(msg: str) -> None:
        if progress:
            progress(msg)

    jobs_info, targets, skipped = _illustration_targets(
        project_id, force=force, only_filenames=only_filenames
    )
    if not targets:
        note("Title-card cover and line backgrounds already exist.")
        return {
            "ok": True,
            "model": "comfyui",
            "image_provider": "comfyui",
            "generated": [],
            "skipped": skipped,
            "missing": 0,
        }

    ensure_comfyui_ready()
    aspect = jobs_info["aspect"]
    layout = normalize_video_layout(jobs_info.get("video_layout") or "cover")
    generated = []
    total = len(targets)
    for i, job in enumerate(targets, start=1):
        remaining = [j["filename"] for j in targets[i - 1 :]]

        def _check(remaining=remaining, slot=job["filename"]) -> None:
            _raise_if_canceled(
                project_id,
                cancel_check=cancel_check,
                remaining_filenames=remaining,
                slot=slot,
            )

        _check()
        try:
            generated.append(
                _generate_comfyui_slot(
                    project_id,
                    job,
                    aspect,
                    layout,
                    note,
                    f"ComfyUI {i}/{total}: {job['filename']}",
                    cancel_check=_check,
                )
            )
        except ImageProviderSwitched as exc:
            if not exc.remaining_filenames:
                exc.remaining_filenames = remaining
            if not exc.slot:
                exc.slot = job["filename"]
            raise
    note(
        f"Saved {len(generated)} ComfyUI image{'s' if len(generated) != 1 else ''} "
        "(cover + backgrounds, sequential)."
    )
    return {
        "ok": True,
        "model": "comfyui",
        "image_provider": "comfyui",
        "generated": generated,
        "skipped": skipped,
        "missing": 0,
        "forced": force,
    }


def generate_illustrations(
    project_id: str,
    progress: Callable[[str], None] | None = None,
    cancel_check: Callable[[], None] | None = None,
    force: bool = False,
    only_filenames: Iterable[str] | None = None,
) -> dict:
    """Dispatch cover + line generation by this job's image_provider. Refuses ChatGPT native.

    If the provider changes mid-run, cancels the in-flight slot, keeps completed PNGs,
    and resumes current + remaining unfinished slots with the new generator.
    """
    from studio.settings import image_provider_label, normalize_image_provider

    filenames = [Path(name).name for name in (only_filenames or []) if name] or None
    all_generated: list = []
    last_skipped: list[str] = []
    switches = 0

    while True:
        try:
            from studio.pipeline import clear_provider_switch

            clear_provider_switch(project_id)
        except Exception:
            pass

        provider = project_image_provider(project_id)
        if provider == "chatgpt":
            if not switches:
                raise RuntimeError(
                    "This job's image_provider is chatgpt. Generate natively in ChatGPT's image tool "
                    "and call save_illustration_image. Do not call generate_illustrations or Fal."
                )
            names = filenames
            if not names:
                info = illustration_jobs(project_id)
                names = [
                    job["filename"]
                    for job in (info.get("jobs") or [])
                    if not job.get("has_image")
                ]
            for name in names or []:
                mark_pending_regen(project_id, name)
            if progress:
                progress(
                    f"Switched to ChatGPT native — marked {len(names or [])} slot(s) for in-chat regen."
                )
            return {
                "ok": True,
                "image_provider": "chatgpt",
                "generated": all_generated,
                "skipped": last_skipped,
                "missing": len(names or []),
                "waiting_for": "chatgpt",
                "remaining": names or [],
                "provider_switches": switches,
            }

        try:
            if provider == "comfyui":
                result = generate_comfyui_illustrations(
                    project_id,
                    progress=progress,
                    cancel_check=cancel_check,
                    force=force,
                    only_filenames=filenames,
                )
            else:
                result = generate_flux_illustrations(
                    project_id,
                    progress=progress,
                    cancel_check=cancel_check,
                    force=force,
                    only_filenames=filenames,
                )
            all_generated.extend(result.get("generated") or [])
            last_skipped = list(result.get("skipped") or [])
            result = dict(result)
            result["generated"] = all_generated
            result["skipped"] = last_skipped
            result["provider_switches"] = switches
            return result
        except ImageProviderSwitched as exc:
            switches += 1
            new_provider = normalize_image_provider(
                exc.provider or project_image_provider(project_id)
            )
            remaining = [
                Path(name).name
                for name in (exc.remaining_filenames or filenames or [])
                if name
            ]
            if not remaining and exc.slot:
                remaining = [Path(exc.slot).name]
            filenames = remaining or None
            force = False  # completed PNGs from this batch stay; only remaining overwrite
            label = image_provider_label(new_provider)
            if progress:
                progress(f"Switching to {label}… restarting remaining images")
            try:
                from studio.pipeline import clear_provider_switch

                clear_provider_switch(project_id)
            except Exception:
                pass
            if new_provider == "chatgpt":
                for name in remaining or []:
                    mark_pending_regen(project_id, name)
                if progress:
                    progress(
                        f"Switched to ChatGPT native — marked {len(remaining or [])} "
                        "remaining slot(s) for in-chat regen."
                    )
                return {
                    "ok": True,
                    "image_provider": "chatgpt",
                    "generated": all_generated,
                    "skipped": last_skipped,
                    "missing": len(remaining or []),
                    "waiting_for": "chatgpt",
                    "remaining": remaining or [],
                    "provider_switches": switches,
                }
            # Refresh prompts for the new provider before continuing.
            try:
                from studio.projects import refresh_line_prompts

                refresh_line_prompts(project_id)
            except Exception:
                pass
            continue


def resolve_illustration_slot(
    project_id: str,
    filename: str | None = None,
    index: int | None = None,
    kind: str | None = None,
) -> tuple[dict, dict]:
    info = illustration_jobs(project_id)
    jobs = info.get("jobs") or []
    if not jobs:
        raise RuntimeError("No illustration slots. Generate a script first.")
    kind_n = normalize_slot_kind(kind)
    current_aspect = normalize_aspect(info.get("aspect") or DEFAULT_ASPECT)
    cover_slots = [
        job for job in jobs if job.get("role") == "cover" or is_cover_filename(job.get("filename"))
    ]

    def _cover_for(aspect: str | None = None, filename: str | None = None) -> dict:
        named = aspect_from_cover_filename(filename) if filename else None
        wanted = normalize_aspect(named or aspect or current_aspect)
        for job in cover_slots:
            job_aspect = normalize_aspect(job.get("video_aspect") or job.get("aspect") or current_aspect)
            if filename and _file_stem(job.get("filename")) == _file_stem(filename):
                return job
            if job_aspect == wanted:
                return job
        return cover_slots[0] if cover_slots else jobs[0]

    if index is not None:
        try:
            index = int(index)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("index must be an integer.") from exc
        if kind_n == "cover" or index < 0:
            if index == -2:
                return _cover_for(ASPECT_9_16), info
            if index == -1:
                return _cover_for(current_aspect), info
            return _cover_for(current_aspect), info
        for job in jobs:
            if job.get("index") == index:
                if kind_n == "line" and (job.get("role") == "cover" or is_cover_filename(job.get("filename"))):
                    continue
                return job, info
        if 0 <= index < len(jobs):
            job = jobs[index]
            if kind_n == "line" and (job.get("role") == "cover" or is_cover_filename(job.get("filename"))):
                raise RuntimeError("That index is the cover. Pass kind='cover' or a line index.")
            return job, info
        raise RuntimeError(f"No illustration at index {index}.")

    if filename:
        raw = str(filename).strip()
        # Strip accidental folders / double extensions from ChatGPT-ish names.
        raw_name = Path(raw.replace("\\", "/").split("/")[-1]).name
        if kind_n == "cover" or is_cover_filename(raw_name, kind_n) or is_cover_filename(raw, kind_n):
            return _cover_for(filename=raw_name), info
        stem = _file_stem(raw_name)
        m_short = LINE_BILLBOARD_STEM_RE.match(stem)
        matches = [
            job
            for job in jobs
            if _file_stem(job.get("filename")) == stem
            or str(job.get("filename") or "").lower() == raw_name.lower()
        ]
        if not matches and m_short:
            # b001 / b001.png even if client added noise around the stem.
            want = line_billboard_stem(int(m_short.group(1)))
            matches = [job for job in jobs if _file_stem(job.get("filename")) == want]
        if not matches:
            # Fuzzy: long ChatGPT titles / legacy topic stems → match line/topic text.
            compact = _compact_name(stem)
            if compact:
                fuzzy = []
                for job in jobs:
                    if job.get("role") == "cover" or is_cover_filename(job.get("filename")):
                        continue
                    for candidate in (
                        job.get("line"),
                        job.get("topic"),
                        job.get("filename"),
                        Path(str(job.get("save_path") or "")).stem,
                    ):
                        c = _compact_name(str(candidate or ""))
                        if not c:
                            continue
                        if c == compact or (len(compact) >= 12 and (c in compact or compact in c)):
                            fuzzy.append(job)
                            break
                matches = fuzzy
        if kind_n == "line":
            matches = [
                job
                for job in matches
                if not (job.get("role") == "cover" or is_cover_filename(job.get("filename")))
            ]
        if len(matches) > 1:
            shorts = [job for job in matches if job.get("role") == "shorts_line"]
            lines = [job for job in matches if job.get("role") != "shorts_line"]
            # Default to explainer line unless the caller asked for a shorts index.
            if shorts and not lines:
                return shorts[0], info
            if lines:
                return lines[0], info
        if matches:
            return matches[0], info
        expected = [str(j.get("filename")) for j in jobs if j.get("filename")][:12]
        hint = f" Expected: {', '.join(expected)}." if expected else ""
        raise RuntimeError(
            f"Unknown illustration filename: {filename!r}.{hint} "
            + BILLBOARD_NAMING_RULE
        )

    if kind_n == "cover":
        return _cover_for(current_aspect), info
    raise RuntimeError("Provide filename or index (and optional kind: cover|line).")


def chatgpt_slot_instructions(job: dict) -> str:
    filename = job.get("filename") or COVER_FILENAME
    is_cover = job.get("role") == "cover" or is_cover_filename(filename)
    save_kind = "cover" if is_cover else "billboard"
    preset = job.get("chatgpt_preset") or ""
    size = job.get("image_size") or f"{job.get('width')}x{job.get('height')}"
    preset_bit = f"pick the {preset} preset at {size}" if preset else f"generate at {size}"
    return (
        f"This slot needs a ChatGPT native image. In ChatGPT's image UI {preset_bit} "
        "(never square, never 1:1, never 4:5). Paste the returned prompt VERBATIM — it is "
        "the live Studio art style (art.* / images.cover_intro or art.scene_wrapper). "
        "Do not invent a new art style. Then call "
        f"save_illustration_image(filename={filename!r}, kind={save_kind!r}) "
        "using that exact short filename from list_illustration_jobs (never ChatGPT's long "
        "image title). Pictures will show the file under this project. "
        "Do not call fal or generate_illustrations_with_flux. "
        + BILLBOARD_NAMING_RULE
    )


def regenerate_illustration(
    project_id: str,
    filename: str | None = None,
    index: int | None = None,
    kind: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Regenerate one cover or line image using the job's current image_provider."""

    def note(msg: str) -> None:
        if progress:
            progress(msg)

    slot, info = resolve_illustration_slot(project_id, filename=filename, index=index, kind=kind)
    provider = info.get("image_provider") or project_image_provider(project_id)
    is_cover = slot.get("role") == "cover" or is_cover_filename(slot.get("filename"))
    kind_out = "cover" if is_cover else "line"
    save_kind = "cover" if is_cover else "billboard"
    fname = slot.get("filename") or COVER_FILENAME

    # Cover slots honor cover_provider independently of line image_provider.
    if is_cover:
        from studio.projects import cover_provider_is_agent, project_cover_provider

        cover_backend = project_cover_provider(project_id)
        if cover_provider_is_agent(project_id):
            mark_pending_regen(project_id, fname)
            slot["needs_regen"] = True
            slot["waiting_for"] = cover_backend
            note(f"Waiting for agent cover upload of {fname} (cover_provider={cover_backend}).")
            return {
                "ok": True,
                "queued": True,
                "native": True,
                "waiting": True,
                "waiting_for": cover_backend,
                "image_provider": provider,
                "cover_provider": cover_backend,
                "filename": fname,
                "kind": kind_out,
                "index": slot.get("index"),
                "prompt": slot.get("prompt") or "",
                "width": slot.get("width"),
                "height": slot.get("height"),
                "aspect": slot.get("video_aspect") or slot.get("aspect") or info.get("aspect"),
                "video_aspect": slot.get("video_aspect") or slot.get("aspect"),
                "project_aspect": info.get("project_aspect") or info.get("aspect"),
                "chatgpt_preset": slot.get("chatgpt_preset"),
                "image_size": slot.get("image_size"),
                "instructions": chatgpt_slot_instructions(slot),
                "save": {
                    "tool": "save_illustration_image",
                    "filename": fname,
                    "kind": save_kind,
                    "line_index": None,
                },
                "job": slot,
                "aspect_rule": info.get("aspect_rule") or info.get("rule"),
            }
        provider = cover_backend

    if provider == "chatgpt":
        mark_pending_regen(project_id, fname)
        slot["needs_regen"] = True
        slot["waiting_for"] = "chatgpt"
        note(f"Waiting for ChatGPT native regen of {fname}.")
        return {
            "ok": True,
            "queued": True,
            "native": True,
            "waiting": True,
            "waiting_for": "chatgpt",
            "image_provider": "chatgpt",
            "filename": fname,
            "kind": kind_out,
            "index": slot.get("index"),
            "prompt": slot.get("prompt") or "",
            "width": slot.get("width"),
            "height": slot.get("height"),
            "aspect": slot.get("video_aspect") or slot.get("aspect") or info.get("aspect"),
            "video_aspect": slot.get("video_aspect") or slot.get("aspect"),
            "project_aspect": info.get("project_aspect") or info.get("aspect"),
            "chatgpt_preset": slot.get("chatgpt_preset"),
            "image_size": slot.get("image_size"),
            "instructions": chatgpt_slot_instructions(slot),
            "save": {
                "tool": "save_illustration_image",
                "filename": fname,
                "kind": save_kind,
                "line_index": None if is_cover else slot.get("index"),
            },
            "job": slot,
            "aspect_rule": info.get("aspect_rule") or info.get("rule"),
        }

    if provider == "comfyui":
        from studio.comfyui import ensure_comfyui_ready

        ensure_comfyui_ready()
        aspect = info.get("aspect") or DEFAULT_ASPECT
        layout = normalize_video_layout(info.get("video_layout") or "cover")
        note(f"Regenerating {fname} with ComfyUI...")
        generated_name = _generate_comfyui_slot(
            project_id,
            slot,
            aspect,
            layout,
            note,
            f"ComfyUI regenerate: {fname}",
        )
        refreshed, _ = resolve_illustration_slot(project_id, filename=generated_name, kind=kind_out)
        note(f"Saved {generated_name}.")
        return {
            "ok": True,
            "queued": False,
            "native": False,
            "generated": True,
            "image_provider": "comfyui",
            "model": "comfyui",
            "filename": generated_name,
            "kind": kind_out,
            "index": refreshed.get("index"),
            "url": refreshed.get("url"),
            "mtime": refreshed.get("mtime"),
            "job": refreshed,
        }

    client = _configure_fal()
    aspect = info.get("aspect") or DEFAULT_ASPECT
    layout = normalize_video_layout(info.get("video_layout") or "cover")
    note(f"Regenerating {fname} with Flux...")
    generated_name = _generate_flux_slot(
        client,
        project_id,
        slot,
        aspect,
        layout,
        note,
        f"Flux regenerate: {fname}",
    )
    refreshed, _ = resolve_illustration_slot(project_id, filename=generated_name, kind=kind_out)
    note(f"Saved {generated_name}.")
    return {
        "ok": True,
        "queued": False,
        "native": False,
        "generated": True,
        "image_provider": "flux",
        "model": FLUX_MODEL,
        "filename": generated_name,
        "kind": kind_out,
        "index": refreshed.get("index"),
        "url": refreshed.get("url"),
        "mtime": refreshed.get("mtime"),
        "job": refreshed,
    }


def regenerate_cover(
    project_id: str,
    aspect: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Regenerate title-card cover(s) via the job's image_provider; keep history.

    Uses flux / chatgpt / comfyui the same way Pictures regenerate does — never hardcodes Flux.
    Pass aspect 16:9, 9:16, or both. Pose bump + history archive happen inside the regen job.
    """
    from studio.pipeline import start_regenerate_illustration_job

    meta = load_meta(project_id)
    wanted = normalize_job_aspect(aspect or meta.get("aspect") or DEFAULT_ASPECT)
    canvases = list(aspects_to_render(wanted))
    from studio.projects import cover_provider_is_agent, project_cover_provider

    provider = project_image_provider(project_id)
    cover_backend = project_cover_provider(project_id)
    results = []
    for canvas in canvases:
        fname = cover_filename(canvas)
        out = start_regenerate_illustration_job(
            project_id,
            filename=fname,
            kind="cover",
        )
        if isinstance(out, dict):
            out["aspect"] = canvas
            out["cover_provider"] = cover_backend
            try:
                out["cover_prompt"] = cover_intro_prompt(project_id, aspect=canvas)
                out["history"] = list_cover_versions(project_id, canvas)
            except Exception:
                pass
        results.append(out)
        # Only one Flux/ComfyUI GPU regen job can run at a time.
        if (
            len(canvases) > 1
            and not cover_provider_is_agent(project_id)
            and cover_backend != "chatgpt"
            and provider != "chatgpt"
            and isinstance(out, dict)
            and out.get("queued")
            and not out.get("native")
        ):
            out["note"] = (
                f"Queued {fname}. Call regenerate_cover again for the other aspect "
                "after this job finishes."
            )
            break
    if len(results) == 1:
        return results[0]
    return {
        "ok": True,
        "aspect": wanted,
        "covers": results,
        "image_provider": provider,
        "cover_provider": cover_backend,
        "pose_nonce": load_meta(project_id).get(COVER_POSE_NONCE_KEY),
    }


def refresh_covers(
    project_id: str,
    covers: list[dict] | None = None,
    *,
    render: bool = True,
    wait: bool = False,
    layout: str = "",
    character_size: str = "",
    include_bubblehead: bool | None = None,
) -> dict:
    """One-shot: normalize + archive + write covers, then queue render (auto-upload path).

    covers: [{aspect: "16:9"|"9:16", image_b64|image|image_url}]
    Wrong-size same-orientation inputs are Lanczos-normalized (never rejected).
    """
    from studio.aspect import aspects_to_render, normalize_job_aspect
    from studio.gpu_lock import holding, wait_for_gpu
    from studio.pipeline import job_status, render_video, start_render_thread

    if not covers:
        raise RuntimeError(
            "Provide covers=[{aspect:'16:9'| '9:16', image_b64|image_url}, ...]."
        )

    meta = load_meta(project_id)
    job_aspect = normalize_job_aspect(meta.get("aspect") or DEFAULT_ASPECT)
    saved: list[dict] = []
    affected: list[str] = []

    for entry in covers:
        if not isinstance(entry, dict):
            raise RuntimeError("Each covers[] entry must be an object.")
        aspect = normalize_aspect(entry.get("aspect") or "")
        image = (
            entry.get("image_b64")
            or entry.get("image")
            or entry.get("b64")
            or ""
        )
        image_url = entry.get("image_url") or entry.get("url") or None
        if not str(image).strip() and not image_url:
            raise RuntimeError(f"Cover for {aspect} needs image_b64 or image_url.")
        out = save_illustration(
            project_id,
            filename=cover_filename(aspect),
            kind="cover",
            aspect=aspect,
            image=str(image or ""),
            image_url=str(image_url) if image_url else None,
        )
        saved.append(out)
        if aspect not in affected:
            affected.append(aspect)

    # Render only the aspects we refreshed (or job "both" → those canvases).
    render_aspect = affected[0] if len(affected) == 1 else "both"
    if not any(a in aspects_to_render(job_aspect) for a in affected):
        # Still allow refresh even if job aspect differs; render the uploaded canvases.
        pass

    payload: dict = {
        "ok": True,
        "project_id": project_id,
        "covers": saved,
        "aspects": affected,
        "render": None,
    }
    if not render:
        return payload

    kwargs = {
        "aspect": render_aspect if len(affected) > 1 else affected[0],
    }
    if layout:
        kwargs["layout"] = layout
    if character_size:
        kwargs["character_size"] = character_size
    if include_bubblehead is not None:
        kwargs["include_bubblehead"] = include_bubblehead

    # start_render_thread waits for the GPU lock itself — do not hold it here.
    if wait:
        wait_for_gpu(name=f"refresh-covers:{project_id}", kind="render", project_id=project_id)
        with holding(f"refresh-covers:{project_id}", kind="render", project_id=project_id):
            rendered = render_video(project_id, **kwargs)
        payload["render"] = {
            "started": True,
            "waited": True,
            "result": rendered,
            "status": job_status(project_id),
        }
    else:
        started = start_render_thread(project_id, **kwargs)
        payload["render"] = {
            "started": True,
            "waited": False,
            "queued": started,
            "status": job_status(project_id),
            "poll": "get_render_status / GET /api/projects/{id}/job",
        }
    return payload

