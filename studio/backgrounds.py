"""Shared studio-room images (clock, wall, floor behind the stick figure)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from studio.paths import FALLBACK_BG, REPO_ROOT, USER_DATA, ensure_dirs, ensure_fallback_background

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DEFAULT_NAME = "bga0.png"
REPO_BACKGROUNDS = REPO_ROOT / "backgrounds"
USER_BACKGROUNDS = USER_DATA / "backgrounds"


def ensure_background_dirs() -> None:
    ensure_dirs()
    REPO_BACKGROUNDS.mkdir(parents=True, exist_ok=True)
    USER_BACKGROUNDS.mkdir(parents=True, exist_ok=True)


def _iter_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    files.sort(key=lambda p: p.name.lower())
    return files


def list_backgrounds() -> list[dict[str, Any]]:
    ensure_background_dirs()
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for folder, source in ((REPO_BACKGROUNDS, "repo"), (USER_BACKGROUNDS, "user")):
        for path in _iter_images(folder):
            key = path.name.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "id": path.name,
                    "filename": path.name,
                    "name": path.stem,
                    "path": str(path),
                    "source": source,
                    "folder": str(folder),
                }
            )
    return items


def default_background() -> dict[str, Any] | None:
    items = list_backgrounds()
    if not items:
        return None
    for item in items:
        if item["filename"].lower() == DEFAULT_NAME.lower():
            return item
    return items[0]


def resolve_background(filename: str | None) -> dict[str, Any] | None:
    name = Path(filename or "").name.strip()
    if not name:
        return None
    lowered = name.lower()
    for item in list_backgrounds():
        if item["filename"] == name or item["id"] == name:
            return item
        if item["filename"].lower() == lowered or item["name"].lower() == Path(name).stem.lower():
            return item
    return None


def resolve_room_path(filename: str | None) -> Path:
    """Absolute path of the studio room image. Logs and falls back if the pick is missing."""
    item = resolve_background(filename)
    if item:
        return Path(item["path"])
    if filename:
        print(f"Background: selected file missing ({filename}); falling back to default.")
    fallback = default_background()
    if fallback:
        return Path(fallback["path"])
    ensure_fallback_background()
    print("Background: backgrounds folder empty; using studio fallback.")
    return FALLBACK_BG


def catalog_payload() -> dict[str, Any]:
    items = list_backgrounds()
    default = default_background()
    return {
        "backgrounds": items,
        "default": (default or {}).get("filename"),
        "folder": str(REPO_BACKGROUNDS),
        "user_folder": str(USER_BACKGROUNDS),
        "hint": (
            "Drop PNG/JPG studio-room images (clock, wall, floor) in "
            f"{REPO_BACKGROUNDS} or {USER_BACKGROUNDS}."
        ),
    }
