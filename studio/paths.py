from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = REPO_ROOT / "code"
STUDIO_DIR = REPO_ROOT / "studio"


def _resolve_user_data() -> Path:
    """Writable data root. Packaged Electron sets BUBBLEPOD_USER_DATA (AppData)."""
    for key in ("BUBBLEPOD_USER_DATA", "LAZYKH_USER_DATA"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return Path(raw)
    return REPO_ROOT / "user_data"


USER_DATA = _resolve_user_data()
PROJECTS_DIR = USER_DATA / "projects"
MUSIC_DIR = USER_DATA / "music"
VOICES_DIR = USER_DATA / "voices"
PIPER_DIR = USER_DATA / "piper"
POSES_DIR = USER_DATA / "poses"
POSES_STOCK_DIR = USER_DATA / "poses_stock"
BUNDLED_POSES_DIR = REPO_ROOT / "poses"
BACKGROUNDS_DIR = REPO_ROOT / "backgrounds"
USER_BACKGROUNDS_DIR = USER_DATA / "backgrounds"
BUNDLED_MUSIC_DIR = REPO_ROOT / "music"
SETTINGS_PATH = USER_DATA / "settings.json"
PROMPTS_PATH = USER_DATA / "prompts.json"
PROMPTS_USERS_DIR = USER_DATA / "prompts_users"
COMFYUI_WORKFLOW_PATH = USER_DATA / "comfyui_workflow.json"
GPU_LOCK_PATH = USER_DATA / "gpu.lock"
GPU_LOCK_MUTEX_PATH = USER_DATA / "gpu.mutex"
TOPICS_PATH = USER_DATA / "topics.json"
DELETED_IDS_PATH = USER_DATA / "deleted_ids.json"
YOUTUBE_TOKEN_PATH = USER_DATA / "youtube_token.json"
MEMBERS_PATH = USER_DATA / "members.json"
BILLING_LEDGER_PATH = USER_DATA / "billing_ledger.json"
ASSETS_DIR = STUDIO_DIR / "assets"
FALLBACK_BG = ASSETS_DIR / "fallback_bg.png"
FALLBACK_COLORS = [
    (126, 196, 214),  # sky
    (168, 214, 176),  # mint
    (244, 230, 196),  # cream
    (232, 148, 148),  # coral
    (196, 176, 224),  # lavender
    (244, 212, 96),   # yellow
    (64, 72, 88),     # charcoal
    (88, 148, 112),   # forest
]
DOCKER_COMPOSE = REPO_ROOT / "docker-compose.yml"
_MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
_POSES_ENV = ("BUBBLEPOD_POSES_DIR", "LAZYKH_POSES_DIR")


def _env_poses_dir() -> Path | None:
    for key in _POSES_ENV:
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return Path(raw)
    return None


def poses_dir() -> Path:
    """Writable runtime poses (recolor + videoDrawer). Prefer env, else user_data/poses."""
    override = _env_poses_dir()
    if override is not None:
        return override
    return POSES_DIR


def bundled_poses_dir() -> Path:
    return BUNDLED_POSES_DIR


def ensure_runtime_poses(*, force_seed: bool = False) -> Path:
    """Ensure user_data/poses exists (seed from bundled REPO poses on first use).

    Recolor and videoDrawer both use this folder so packaged Electron (read-only
    resources/lazykh/poses) and MCP restart (git checkout) stay in sync.
    """
    USER_DATA.mkdir(parents=True, exist_ok=True)
    dest = poses_dir()
    dest.mkdir(parents=True, exist_ok=True)
    have = any(dest.glob("pose*.png"))
    if have and not force_seed:
        return dest
    src = bundled_poses_dir()
    if not src.is_dir():
        return dest
    for path in sorted(src.glob("pose*.png")):
        target = dest / path.name
        if force_seed or not target.is_file():
            shutil.copy2(path, target)
    return dest


def poses_env_for_subprocess() -> dict[str, str]:
    """Env vars so videoDrawer.py opens the same poses folder Studio recolors."""
    root = str(ensure_runtime_poses())
    return {
        "BUBBLEPOD_POSES_DIR": root,
        "LAZYKH_POSES_DIR": root,
        "BUBBLEPOD_USER_DATA": str(USER_DATA),
        "LAZYKH_USER_DATA": str(USER_DATA),
    }


def ensure_dirs() -> None:
    USER_DATA.mkdir(parents=True, exist_ok=True)
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    POSES_DIR.mkdir(parents=True, exist_ok=True)
    POSES_STOCK_DIR.mkdir(parents=True, exist_ok=True)
    BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    USER_BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    seed_bundled_music()
    try:
        ensure_runtime_poses()
    except Exception:
        pass


def seed_bundled_music() -> int:
    """Copy bundled music/ tracks into writable user_data/music (skip existing files)."""
    if not BUNDLED_MUSIC_DIR.is_dir():
        return 0
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    seed_meta: dict[str, dict] = {}
    seed_lib = BUNDLED_MUSIC_DIR / "library.json"
    if seed_lib.is_file():
        try:
            data = json.loads(seed_lib.read_text(encoding="utf-8"))
            tracks = data.get("tracks") if isinstance(data, dict) else data
            if isinstance(tracks, list):
                for item in tracks:
                    if isinstance(item, dict) and item.get("filename"):
                        seed_meta[str(item["filename"])] = item
        except (json.JSONDecodeError, OSError):
            pass

    for src in sorted(BUNDLED_MUSIC_DIR.iterdir()):
        if not src.is_file() or src.suffix.lower() not in _MUSIC_EXTS:
            continue
        dest = MUSIC_DIR / src.name
        if dest.is_file():
            continue
        try:
            shutil.copy2(src, dest)
            copied += 1
        except OSError:
            continue

    # Merge friendly names for newly seeded files without clobbering user renames.
    lib_path = MUSIC_DIR / "library.json"
    existing: list[dict] = []
    if lib_path.is_file():
        try:
            data = json.loads(lib_path.read_text(encoding="utf-8"))
            tracks = data.get("tracks") if isinstance(data, dict) else data
            if isinstance(tracks, list):
                existing = [t for t in tracks if isinstance(t, dict) and t.get("id")]
        except (json.JSONDecodeError, OSError):
            existing = []
    by_id = {str(t.get("id")): dict(t) for t in existing}
    by_file = {str(t.get("filename")): str(t.get("id")) for t in existing if t.get("filename")}
    for path in sorted(MUSIC_DIR.iterdir()) if MUSIC_DIR.is_dir() else []:
        if not path.is_file() or path.suffix.lower() not in _MUSIC_EXTS:
            continue
        stem = path.stem
        meta = seed_meta.get(path.name) or {}
        if stem in by_id:
            cur = by_id[stem]
            if not cur.get("name") or cur.get("name") == path.name:
                if meta.get("name"):
                    cur["name"] = meta["name"]
            cur["filename"] = path.name
            continue
        if path.name in by_file:
            continue
        by_id[stem] = {
            "id": stem,
            "name": meta.get("name") or path.name,
            "filename": path.name,
            "uploaded_at": meta.get("uploaded_at") or "",
        }
    try:
        lib_path.write_text(
            json.dumps({"tracks": list(by_id.values())}, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass
    return copied


def ensure_fallback_background() -> Path:
    ensure_dirs()
    from PIL import Image, ImageDraw

    def paint(size: tuple[int, int], color: tuple[int, int, int]) -> Image.Image:
        w, h = size
        img = Image.new("RGB", size, color)
        draw = ImageDraw.Draw(img)
        lighter = tuple(min(255, c + 28) for c in color)
        darker = tuple(max(0, c - 36) for c in color)
        draw.ellipse((int(w * 0.07), int(h * 0.08), int(w * 0.32), int(h * 0.32)), fill=lighter)
        draw.ellipse((int(w * 0.68), int(h * 0.58), int(w * 0.97), int(h * 0.96)), fill=darker)
        draw.rectangle((0, h - 40, w, h), fill=darker)
        return img

    for i, color in enumerate(FALLBACK_COLORS):
        landscape = ASSETS_DIR / f"fallback_bg{i}.png"
        portrait = ASSETS_DIR / f"fallback_bg{i}_9x16.png"
        if not landscape.is_file():
            img = paint((1920, 1080), color)
            img.save(landscape)
            if i == 0:
                img.save(FALLBACK_BG)
        if not portrait.is_file():
            paint((1080, 1920), color).save(portrait)
    if not FALLBACK_BG.is_file():
        Image.new("RGB", (1920, 1080), FALLBACK_COLORS[0]).save(FALLBACK_BG)
    return FALLBACK_BG


def asset_warnings() -> list[str]:
    warnings = []
    try:
        runtime_poses = ensure_runtime_poses()
    except Exception:
        runtime_poses = poses_dir()
    if not runtime_poses.is_dir() or not any(runtime_poses.glob("pose*.png")):
        warnings.append(
            f"Missing stick-figure poses in {runtime_poses}. "
            f"Seed from bundled {BUNDLED_POSES_DIR} (copy pose*.png) before rendering."
        )
    mouths = REPO_ROOT / "mouths"
    if not mouths.is_dir() or not any(mouths.glob("*.png")):
        warnings.append(
            f"Missing mouth sprites in {mouths}. Copy the original lazykh `mouths/` folder here before rendering."
        )
    bg_files = []
    for folder in (BACKGROUNDS_DIR, USER_BACKGROUNDS_DIR):
        if folder.is_dir():
            bg_files.extend(
                p for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
            )
    if not bg_files:
        warnings.append(
            f"No studio room images. Drop PNG/JPG files in {BACKGROUNDS_DIR} "
            f"(original lazykh folder) or {USER_BACKGROUNDS_DIR}."
        )
    return warnings
