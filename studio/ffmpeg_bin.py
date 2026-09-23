"""Resolve ffmpeg / ffprobe: bundled Electron resources, then PATH."""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WIN = os.name == "nt"


def _exe(name: str) -> str:
    return f"{name}.exe" if _WIN else name


def _bundled_candidates(name: str) -> list[Path]:
    """Locations next to the app / in the repo (Electron extraResources)."""
    binary = _exe(name)
    out: list[Path] = []
    # Packaged: resources/ffmpeg/ next to resources/lazykh/
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass) / "ffmpeg" / binary)
    # When Studio runs from resources/lazykh, sibling is ../ffmpeg/
    out.append(_REPO_ROOT.parent / "ffmpeg" / binary)
    out.append(_REPO_ROOT / "resources" / "ffmpeg" / binary)
    # Dev / pre-bundle: desktop/bin/ffmpeg/
    out.append(_REPO_ROOT / "desktop" / "bin" / "ffmpeg" / binary)
    # Same folder as this process if Electron put it on cwd
    out.append(Path.cwd() / "ffmpeg" / binary)
    return out


def _env_binary(env_key: str) -> Path | None:
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_file():
        return path
    return None


@lru_cache(maxsize=4)
def resolve_ffmpeg() -> str:
    """Return absolute path to ffmpeg, or raise with a clear message."""
    env = _env_binary("FFMPEG_BINARY")
    if env is not None:
        return str(env.resolve())
    for cand in _bundled_candidates("ffmpeg"):
        if cand.is_file():
            return str(cand.resolve())
    which = shutil.which("ffmpeg")
    if which:
        return which
    raise FileNotFoundError(
        "ffmpeg was not found. Install it, add it to PATH, or use the Bubble Pod "
        "desktop build that ships ffmpeg under resources/ffmpeg/. "
        "You can also set FFMPEG_BINARY to the full path of ffmpeg.exe."
    )


@lru_cache(maxsize=4)
def resolve_ffprobe() -> str:
    """Return absolute path to ffprobe, or raise with a clear message."""
    env = _env_binary("FFPROBE_BINARY")
    if env is not None:
        return str(env.resolve())
    # Same directory as a resolved ffmpeg (common for static builds)
    try:
        ffmpeg = Path(resolve_ffmpeg())
        sibling = ffmpeg.with_name(_exe("ffprobe"))
        if sibling.is_file():
            return str(sibling.resolve())
    except FileNotFoundError:
        pass
    for cand in _bundled_candidates("ffprobe"):
        if cand.is_file():
            return str(cand.resolve())
    which = shutil.which("ffprobe")
    if which:
        return which
    raise FileNotFoundError(
        "ffprobe was not found. It usually ships next to ffmpeg. "
        "Set FFPROBE_BINARY or install ffmpeg (with ffprobe) on PATH."
    )


def ffmpeg_cmd(*args: str) -> list[str]:
    return [resolve_ffmpeg(), *args]


def ffprobe_cmd(*args: str) -> list[str]:
    return [resolve_ffprobe(), *args]


def ensure_ffmpeg_on_path() -> str | None:
    """Put bundled ffmpeg ahead of PATH for child processes. Returns path or None."""
    try:
        ffmpeg = resolve_ffmpeg()
    except FileNotFoundError:
        return None
    bin_dir = str(Path(ffmpeg).parent)
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if bin_dir not in parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("FFMPEG_BINARY", ffmpeg)
    try:
        probe = resolve_ffprobe()
        os.environ.setdefault("FFPROBE_BINARY", probe)
    except FileNotFoundError:
        pass
    return ffmpeg

