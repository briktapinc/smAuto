from __future__ import annotations

import json
import os
import random
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from studio.paths import CODE_DIR, MUSIC_DIR, ensure_dirs
from studio.projects import input_prefix, load_meta, save_meta
from studio.settings import load_settings, normalize_music_volume_pct

MUSIC_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
MAX_MUSIC_BYTES = 80 * 1024 * 1024
LIBRARY_NAME = "library.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _library_path() -> Path:
    ensure_dirs()
    return MUSIC_DIR / LIBRARY_NAME


def _load_library() -> list[dict[str, Any]]:
    path = _library_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    tracks = data.get("tracks") if isinstance(data, dict) else data
    if not isinstance(tracks, list):
        return []
    out = []
    for item in tracks:
        if isinstance(item, dict) and item.get("id"):
            out.append(item)
    return out


def _save_library(tracks: list[dict[str, Any]]) -> None:
    _library_path().write_text(json.dumps({"tracks": tracks}, indent=2), encoding="utf-8")


def _safe_name(name: str) -> str:
    base = Path(name or "track").name
    base = re.sub(r"[^\w.\- ]+", "_", base).strip("._ ") or "track"
    return base[:80]


def _ext_for(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in MUSIC_EXTS:
        raise RuntimeError("Upload mp3, wav, m4a, ogg, or flac.")
    return ext


def _public_track(item: dict[str, Any]) -> dict[str, Any]:
    stored = item.get("filename") or ""
    path = MUSIC_DIR / Path(stored).name
    return {
        "id": item.get("id"),
        "name": item.get("name") or stored,
        "filename": stored,
        "path": str(path),
        "exists": path.is_file(),
        "size": path.stat().st_size if path.is_file() else 0,
        "uploaded_at": item.get("uploaded_at") or "",
    }


def library_payload() -> dict[str, Any]:
    return {
        "tracks": list_tracks(),
        "music_volume_pct": normalize_music_volume_pct(load_settings().get("music_volume_pct")),
        "folder": str(MUSIC_DIR),
    }


def list_tracks() -> list[dict[str, Any]]:
    ensure_dirs()
    known = {str(item.get("id")): item for item in _load_library() if item.get("id")}
    for path in sorted(MUSIC_DIR.iterdir()) if MUSIC_DIR.is_dir() else []:
        if not path.is_file() or path.suffix.lower() not in MUSIC_EXTS:
            continue
        stem = path.stem
        if stem in known:
            known[stem]["filename"] = path.name
            continue
        if any(item.get("filename") == path.name for item in known.values()):
            continue
        known[stem] = {
            "id": stem,
            "name": path.name,
            "filename": path.name,
            "uploaded_at": _now(),
        }
    tracks = [_public_track(item) for item in known.values()]
    tracks = [t for t in tracks if t.get("exists")]
    tracks.sort(key=lambda t: t.get("uploaded_at") or t.get("name") or "")
    _save_library(
        [
            {
                "id": t["id"],
                "name": t["name"],
                "filename": t["filename"],
                "uploaded_at": t.get("uploaded_at") or "",
            }
            for t in tracks
        ]
    )
    return tracks


def get_track(track_id: str | None) -> dict[str, Any] | None:
    tid = (track_id or "").strip()
    if not tid:
        return None
    for item in list_tracks():
        if item["id"] == tid or item["filename"] == tid:
            return item
    return None


def save_upload(filename: str, data: bytes) -> dict[str, Any]:
    if not data:
        raise RuntimeError("Empty music file.")
    if len(data) > MAX_MUSIC_BYTES:
        raise RuntimeError("Music file is too large (max 80 MB).")
    ext = _ext_for(filename)
    ensure_dirs()
    track_id = uuid.uuid4().hex[:12]
    stored = f"{track_id}{ext}"
    dest = MUSIC_DIR / stored
    dest.write_bytes(data)
    tracks = _load_library()
    tracks.append(
        {
            "id": track_id,
            "name": _safe_name(filename),
            "filename": stored,
            "uploaded_at": _now(),
        }
    )
    _save_library(tracks)
    track = get_track(track_id)
    if not track:
        raise RuntimeError("Saved music file but could not read it back.")
    return track


def delete_track(track_id: str) -> dict[str, Any]:
    track = get_track(track_id)
    if not track:
        raise FileNotFoundError(f"Unknown music track: {track_id}")
    path = Path(track["path"])
    if path.is_file() and path.resolve().parent.resolve() == MUSIC_DIR.resolve():
        path.unlink()
    remaining = [item for item in _load_library() if item.get("id") != track["id"]]
    _save_library(remaining)
    return {"id": track["id"], "deleted": True}


def project_music_info(project_id: str) -> dict[str, Any]:
    meta = load_meta(project_id)
    tid = meta.get("music_id")
    track = get_track(tid) if tid else None
    mode = (meta.get("music_mode") or "").strip().lower()
    if mode not in {"fixed", "shuffle"}:
        mode = "fixed" if track or tid else "shuffle"
    return {
        "music_id": (track or {}).get("id") or tid or None,
        "music_name": (track or {}).get("name") or meta.get("music_name") or meta.get("music_file"),
        "music_file": (track or {}).get("filename") or meta.get("music_file"),
        "music_missing": bool(tid) and track is None,
        "music_mode": mode,
    }


def assign_project_music(project_id: str, reshuffle: bool = False) -> dict[str, Any] | None:
    meta = load_meta(project_id)
    tracks = list_tracks()
    if not tracks:
        print("Background music: skipped (library empty).")
        if meta.get("music_id") or meta.get("music_file"):
            meta["music_id"] = None
            meta["music_file"] = None
            meta["music_name"] = None
            save_meta(project_id, meta)
        return None
    if not reshuffle:
        existing = get_track(meta.get("music_id"))
        if existing:
            return existing
        if meta.get("music_id"):
            print(f"Background music: {meta.get('music_id')} missing; picking another.")
    choices = tracks
    current_id = meta.get("music_id")
    if reshuffle and current_id and len(tracks) > 1:
        choices = [t for t in tracks if t["id"] != current_id] or tracks
    track = random.choice(choices)
    meta["music_id"] = track["id"]
    meta["music_file"] = track["filename"]
    meta["music_name"] = track["name"]
    if reshuffle or not meta.get("music_mode"):
        meta["music_mode"] = "shuffle"
    save_meta(project_id, meta)
    print(f"Background music: selected {track['name']} ({track['id']})")
    return track


def shuffle_project_music(project_id: str) -> dict[str, Any]:
    from studio.projects import project_payload

    assign_project_music(project_id, reshuffle=True)
    meta = load_meta(project_id)
    meta["music_mode"] = "shuffle"
    save_meta(project_id, meta)
    return project_payload(project_id)


def set_project_music(project_id: str, track_id: str) -> dict[str, Any]:
    """Lock this job to a specific library track (by id)."""
    from studio.projects import project_payload

    track = get_track(track_id)
    if not track:
        raise FileNotFoundError(f"Unknown music track: {track_id}")
    meta = load_meta(project_id)
    meta["music_id"] = track["id"]
    meta["music_file"] = track["filename"]
    meta["music_name"] = track["name"]
    meta["music_mode"] = "fixed"
    save_meta(project_id, meta)
    return project_payload(project_id)


def clear_project_music(project_id: str) -> dict[str, Any]:
    """Clear the locked track so the next render auto-picks (shuffle)."""
    from studio.projects import project_payload

    meta = load_meta(project_id)
    meta["music_id"] = None
    meta["music_file"] = None
    meta["music_name"] = None
    meta["music_mode"] = "shuffle"
    save_meta(project_id, meta)
    return project_payload(project_id)


def _import_music_adder():
    code = str(CODE_DIR)
    if code not in sys.path:
        sys.path.insert(0, code)
    import musicAdder

    return musicAdder


def apply_project_music(project_id: str, video_path: Path | None = None) -> dict[str, Any]:
    """Loop the job's chosen track under the finished video (cover intro + explainer)."""
    settings = load_settings()
    pct = normalize_music_volume_pct(settings.get("music_volume_pct"))
    prefix = Path(input_prefix(project_id))
    video = Path(video_path) if video_path else prefix.with_name(prefix.name + "_final.mp4")
    result = {
        "applied": False,
        "skipped": True,
        "music_id": None,
        "music_name": None,
        "music_volume_pct": pct,
        "detail": "no music",
    }
    if not video.is_file():
        print("Background music: skipped (video file missing).")
        result["detail"] = "video missing"
        return result
    if pct <= 0:
        print("Background music: skipped (loudness 0%).")
        result["detail"] = "loudness 0%"
        return result
    track = assign_project_music(project_id, reshuffle=False)
    if not track:
        result["detail"] = "library empty"
        return result
    path = Path(track["path"])
    if not path.is_file():
        print(f"Background music: file missing ({path}); picking another.")
        track = assign_project_music(project_id, reshuffle=True)
        if not track:
            result["detail"] = "chosen file missing"
            return result
        path = Path(track["path"])
        if not path.is_file():
            print("Background music: skipped (chosen file missing).")
            result["detail"] = "chosen file missing"
            return result
    result.update(
        {
            "music_id": track["id"],
            "music_name": track["name"],
            "detail": f"looping {track['name']} at {pct}%",
        }
    )
    print(f"Background music: looping {track['name']} at {pct}% over the full video.")
    adder = _import_music_adder()
    tmp = video.with_name(video.stem + "_with_music.mp4")
    try:
        adder.mix_into_video(video, path, tmp, volume=pct / 100.0)
        os.replace(tmp, video)
        result["applied"] = True
        result["skipped"] = False
    except Exception as exc:
        print(f"Background music: mix failed ({exc}); leaving speech-only video.")
        result["detail"] = f"mix failed: {exc}"
        if tmp.is_file():
            try:
                tmp.unlink()
            except OSError:
                pass
    return result
