"""Recolor stick-figure head fills in poses/*.png.

Stock Bubblehead uses two flat yellows:
  light #FAE02E = RGB(250, 224, 46)
  dark  #F8AF05 = RGB(248, 175, 5)

Dark is derived from light in HSL: H - 10.4 deg, L - 8.4 (S unchanged).
"""

from __future__ import annotations

import colorsys
import re
import shutil
import threading
from pathlib import Path
from typing import Any

from studio.paths import REPO_ROOT, USER_DATA, ensure_dirs, ensure_runtime_poses, poses_dir

DEFAULT_LIGHT = (250, 224, 46)
DEFAULT_DARK = (248, 175, 5)
DEFAULT_HEX = "#FAE02E"

# Empirically measured from stock poses (HSL degrees / percent).
_HSL_H_DELTA = -10.38
_HSL_L_DELTA = -8.43

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_lock = threading.Lock()
_NEAR_THRESH2 = 38 * 38  # remap anti-aliased yellow edge pixels


def stock_backup_dir() -> Path:
    ensure_dirs()
    return USER_DATA / "poses_stock"


def normalize_hex_color(value: Any, default: str = DEFAULT_HEX) -> str:
    raw = str(value or "").strip()
    if not raw:
        return default
    if not raw.startswith("#"):
        raw = "#" + raw
    if not _HEX_RE.match(raw):
        raise RuntimeError(f"Invalid color {value!r}. Use #RRGGBB.")
    return raw.upper()


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    h = normalize_hex_color(value)
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def _rgb_to_hsl(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (c / 255.0 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360.0, s * 100.0, l * 100.0


def _hsl_to_rgb(h: float, s: float, l: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((h % 360.0) / 360.0, max(0.0, min(1.0, l / 100.0)), max(0.0, min(1.0, s / 100.0)))
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def dark_from_light(light: tuple[int, int, int]) -> tuple[int, int, int]:
    """Generate the head shadow color from the light fill (stock formula)."""
    h, s, l = _rgb_to_hsl(light)
    return _hsl_to_rgb(h + _HSL_H_DELTA, s, l + _HSL_L_DELTA)


def list_pose_files() -> list[Path]:
    folder = ensure_runtime_poses()
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("pose*.png") if p.is_file())


def _dist2(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _is_outline(rgb: tuple[int, int, int]) -> bool:
    return sum(rgb) < 90


def ensure_stock_backup(*, force: bool = False) -> dict[str, Any]:
    """Copy pristine bundled poses into user_data/poses_stock once (for Reset)."""
    from studio.paths import bundled_poses_dir

    dest = stock_backup_dir()
    dest.mkdir(parents=True, exist_ok=True)
    if dest.is_dir() and any(dest.glob("pose*.png")) and not force:
        return {"ok": True, "skipped": True, "path": str(dest), "count": len(list(dest.glob("pose*.png")))}
    # Prefer yellow bundled poses; fall back to current runtime poses.
    src = bundled_poses_dir()
    files = sorted(p for p in src.glob("pose*.png") if p.is_file()) if src.is_dir() else []
    if not files:
        files = list_pose_files()
        src = poses_dir()
    if not files:
        return {"ok": False, "error": f"No pose*.png files in {src}", "path": str(dest)}
    for path in files:
        shutil.copy2(path, dest / path.name)
    return {"ok": True, "skipped": False, "path": str(dest), "count": len(files)}


def restore_stock_poses() -> dict[str, Any]:
    backup = stock_backup_dir()
    files = sorted(p for p in backup.glob("pose*.png") if p.is_file()) if backup.is_dir() else []
    if not files:
        # No backup yet — recolor whatever is on disk to stock yellow.
        return apply_head_color(DEFAULT_HEX, from_color=None, save_setting=True)
    dest = ensure_runtime_poses()
    dest.mkdir(parents=True, exist_ok=True)
    for path in files:
        shutil.copy2(path, dest / path.name)
    from studio.settings import save_settings

    save_settings({"stickman_head_color": DEFAULT_HEX})
    out = {
        "ok": True,
        "restored": True,
        "color": DEFAULT_HEX,
        "dark": rgb_to_hex(DEFAULT_DARK),
        "files": len(files),
        "path": str(dest),
        "backup": str(backup),
        "poses_dir": str(dest),
    }
    out["renders_invalidated"] = _invalidate_all_project_renders()
    out["note"] = (
        "Restored stock yellow into user_data/poses. Cached frames/mp4s cleared — re-render to update videos. "
        "Title-card covers with a drawn stickman still need regenerate_cover."
    )
    return out


def _invalidate_all_project_renders() -> dict[str, Any]:
    """Drop frames/mp4 so the next render composites the new pose PNGs."""
    try:
        from studio.projects import invalidate_render_artifacts, list_projects
    except Exception as exc:
        return {"ok": False, "error": str(exc), "cleared": 0}
    cleared = 0
    errors: list[str] = []
    for item in list_projects():
        pid = (item.get("id") or "").strip()
        if not pid:
            continue
        try:
            invalidate_render_artifacts(pid, alignment=False)
            cleared += 1
        except Exception as exc:
            errors.append(f"{pid}: {exc}")
    return {"ok": True, "cleared": cleared, "errors": errors[:8]}


def _recolor_image(
    path: Path,
    old_light: tuple[int, int, int],
    old_dark: tuple[int, int, int],
    new_light: tuple[int, int, int],
    new_dark: tuple[int, int, int],
) -> int:
    import numpy as np
    from PIL import Image

    im = Image.open(path).convert("RGBA")
    arr = np.asarray(im).copy()
    rgb = arr[:, :, :3].astype(np.int16)
    alpha = arr[:, :, 3]
    outline = rgb.sum(axis=2) < 90
    opaque = alpha >= 8

    dl = (rgb[:, :, 0] - old_light[0]) ** 2 + (rgb[:, :, 1] - old_light[1]) ** 2 + (rgb[:, :, 2] - old_light[2]) ** 2
    dd = (rgb[:, :, 0] - old_dark[0]) ** 2 + (rgb[:, :, 1] - old_dark[1]) ** 2 + (rgb[:, :, 2] - old_dark[2]) ** 2
    exact_light = (
        (arr[:, :, 0] == old_light[0])
        & (arr[:, :, 1] == old_light[1])
        & (arr[:, :, 2] == old_light[2])
    )
    exact_dark = (
        (arr[:, :, 0] == old_dark[0])
        & (arr[:, :, 1] == old_dark[1])
        & (arr[:, :, 2] == old_dark[2])
    )
    light_mask = opaque & ~outline & (exact_light | ((dl <= _NEAR_THRESH2) & (dl <= dd)))
    dark_mask = opaque & ~outline & ~light_mask & (exact_dark | (dd <= _NEAR_THRESH2))

    changed = int(light_mask.sum() + dark_mask.sum())
    if not changed:
        return 0
    arr[light_mask, 0] = new_light[0]
    arr[light_mask, 1] = new_light[1]
    arr[light_mask, 2] = new_light[2]
    arr[dark_mask, 0] = new_dark[0]
    arr[dark_mask, 1] = new_dark[1]
    arr[dark_mask, 2] = new_dark[2]
    Image.fromarray(arr, mode="RGBA").save(path, format="PNG", optimize=True)
    return changed


def apply_head_color(
    color: str,
    *,
    from_color: str | None = None,
    save_setting: bool = True,
) -> dict[str, Any]:
    """Replace head light/dark across every pose*.png under repo poses/ (not poses_stock)."""
    new_hex = normalize_hex_color(color)
    new_light = hex_to_rgb(new_hex)
    new_dark = dark_from_light(new_light)

    from studio.settings import load_settings, save_settings

    settings = load_settings()
    old_hex = normalize_hex_color(from_color or settings.get("stickman_head_color") or DEFAULT_HEX)
    old_light = hex_to_rgb(old_hex)
    # Prefer exact stock shadow when coming from the shipped yellow so every pixel matches.
    old_dark = DEFAULT_DARK if old_light == DEFAULT_LIGHT else dark_from_light(old_light)

    files = list_pose_files()
    if not files:
        raise RuntimeError(f"No pose images found in {ensure_runtime_poses()}")

    with _lock:
        # Preserve stock yellow once before the first non-default recolor.
        if old_hex.upper() == DEFAULT_HEX or (
            old_light == DEFAULT_LIGHT and not (stock_backup_dir() / "pose0001.png").is_file()
        ):
            ensure_stock_backup()

        total = 0
        touched = 0
        for path in files:
            n = _recolor_image(path, old_light, old_dark, new_light, new_dark)
            # Also sweep stock defaults if settings drifted but files still yellow.
            if (old_light, old_dark) != (DEFAULT_LIGHT, DEFAULT_DARK):
                n += _recolor_image(path, DEFAULT_LIGHT, DEFAULT_DARK, new_light, new_dark)
            total += n
            if n:
                touched += 1

        if save_setting:
            save_settings({"stickman_head_color": new_hex})

    invalidated = _invalidate_all_project_renders()
    return {
        "ok": True,
        "color": new_hex,
        "dark": rgb_to_hex(new_dark),
        "from_color": old_hex,
        "from_dark": rgb_to_hex(old_dark),
        "files": len(files),
        "files_changed": touched,
        "pixels_changed": total,
        "poses_dir": str(poses_dir()),
        "poses_stock_note": (
            "Runtime poses are user_data/poses (recolor + videoDrawer). "
            "user_data/poses_stock is yellow Reset only; bundled repo poses/ is the seed."
        ),
        "renders_invalidated": invalidated,
        "note": (
            "Pose PNGs updated under user_data/poses and cached frames/mp4s cleared. "
            "Call render_final_video to rebuild. "
            "5s title-card covers that drew the stickman into the artwork still need "
            "regenerate_cover / refresh_covers — those are not composited from poses/."
        ),
        "formula": {
            "hsl_h_delta": _HSL_H_DELTA,
            "hsl_l_delta": _HSL_L_DELTA,
            "note": "dark = HSL(H-10.4deg, S, L-8.4) from the light fill",
        },
    }


def head_color_status() -> dict[str, Any]:
    from studio.settings import load_settings

    settings = load_settings()
    color = normalize_hex_color(settings.get("stickman_head_color") or DEFAULT_HEX)
    light = hex_to_rgb(color)
    dark = DEFAULT_DARK if light == DEFAULT_LIGHT else dark_from_light(light)
    files = list_pose_files()
    preview = next((p for p in files if p.name.lower() == "pose0001.png"), files[0] if files else None)
    backup = stock_backup_dir()
    return {
        "color": color,
        "dark": rgb_to_hex(dark),
        "default_color": DEFAULT_HEX,
        "default_dark": rgb_to_hex(DEFAULT_DARK),
        "files": len(files),
        "poses_dir": str(poses_dir()),
        "preview": preview.name if preview else "",
        "stock_backup": (backup / "pose0001.png").is_file(),
        "stock_backup_path": str(backup),
        "formula": "dark = HSL(H - 10.4deg, S, L - 8.4) from the light fill",
    }
