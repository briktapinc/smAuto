"""External narration TTS: operator-uploaded MP3 → wav + Gentle align (no Chatterbox)."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from studio.projects import (
    invalidate_render_artifacts,
    load_meta,
    project_dir,
    save_meta,
)

log = logging.getLogger("bubblepod.tts_external")

EXTERNAL_AUDIO_NAME = "narration_external.mp3"
EXTERNAL_AUDIO_SHORTS_NAME = "narration_external_9x16.mp3"
EXTERNAL_AUDIO_MISSING = "EXTERNAL_AUDIO_MISSING"
MAX_AUDIO_UPLOAD_BYTES = 80 * 1024 * 1024  # 80MB
MAX_BASE64_CHARS = 110 * 1024 * 1024


def external_mp3_path(project_id: str, *, shorts: bool = False) -> Path:
    name = EXTERNAL_AUDIO_SHORTS_NAME if shorts else EXTERNAL_AUDIO_NAME
    return project_dir(project_id) / name


def external_audio_present(project_id: str, *, shorts: bool = False) -> bool:
    path = external_mp3_path(project_id, shorts=shorts)
    try:
        return path.is_file() and path.stat().st_size > 256
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _decode_audio_bytes(audio: str, *, format_hint: str = "mp3") -> bytes:
    blob = (audio or "").strip()
    if not blob:
        raise RuntimeError("No audio data provided.")
    if blob.startswith("data:"):
        blob = blob.split(",", 1)[1]
    blob = re.sub(r"\s+", "", blob)
    if len(blob) > MAX_BASE64_CHARS:
        raise RuntimeError(
            f"Base64 audio payload is too large ({len(blob)} chars). "
            "Pass a smaller file or use multipart POST /api/projects/{{id}}/narration."
        )
    try:
        data = base64.b64decode(blob, validate=False)
    except Exception as exc:
        raise RuntimeError(f"Invalid base64 audio data: {exc}") from exc
    if len(data) > MAX_AUDIO_UPLOAD_BYTES:
        raise RuntimeError(f"Decoded audio is {len(data)} bytes (max {MAX_AUDIO_UPLOAD_BYTES}).")
    if len(data) < 256:
        raise RuntimeError("Decoded audio is empty or too small.")
    # Light magic-byte check for mp3 / mpeg
    fmt = (format_hint or "mp3").strip().lower()
    if fmt in ("mp3", "mpeg", "mpg"):
        if not (data[:3] == b"ID3" or data[:2] == b"\xff\xfb" or data[:2] == b"\xff\xf3" or data[:2] == b"\xff\xf2"):
            # Some encoders omit ID3/frame sync in first bytes — still try ffprobe later.
            log.warning("MP3 magic bytes not detected; will verify with ffprobe.")
    return data


def probe_audio_duration_seconds(path: Path) -> float | None:
    """Return duration in seconds via ffprobe, or None on failure."""
    try:
        from studio.ffmpeg_bin import resolve_ffprobe

        ffprobe = resolve_ffprobe()
    except Exception:
        try:
            from studio.ffmpeg_bin import resolve_ffmpeg

            # Some builds only ship ffmpeg; use ffmpeg -i parse as fallback below.
            resolve_ffmpeg()
            ffprobe = None
        except Exception:
            return None
    try:
        if ffprobe:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode == 0 and (proc.stdout or "").strip():
                return float((proc.stdout or "").strip())
        from studio.ffmpeg_bin import ffmpeg_cmd

        proc = subprocess.run(
            ffmpeg_cmd("-i", str(path)),
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = (proc.stderr or "") + (proc.stdout or "")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if match:
            h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
            return h * 3600 + m * 60 + s
    except Exception as exc:
        log.warning("ffprobe duration failed for %s: %s", path, exc)
    return None


def _verify_decodable(path: Path) -> float:
    duration = probe_audio_duration_seconds(path)
    if duration is None or duration <= 0.05:
        raise RuntimeError(
            f"Uploaded audio is not a decodable audio file (ffprobe failed): {path.name}"
        )
    return float(duration)


def upload_narration_audio(
    project_id: str,
    audio: str = "",
    *,
    format: str = "mp3",
    shorts: bool = False,
    data: bytes | None = None,
) -> dict[str, Any]:
    """Save operator MP3 as narration_external.mp3 (atomic). Invalidates alignment."""
    load_meta(project_id)  # raises if missing
    fmt = (format or "mp3").strip().lower() or "mp3"
    if fmt not in ("mp3", "mpeg", "mpg"):
        raise RuntimeError("Only format='mp3' is supported for external narration.")
    if data is None:
        data = _decode_audio_bytes(audio, format_hint=fmt)
    elif len(data) < 256:
        raise RuntimeError("Audio bytes are empty or too small.")
    elif len(data) > MAX_AUDIO_UPLOAD_BYTES:
        raise RuntimeError(f"Audio is {len(data)} bytes (max {MAX_AUDIO_UPLOAD_BYTES}).")

    dest = external_mp3_path(project_id, shorts=shorts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_suffix(dest.suffix + ".staging")
    try:
        staging.write_bytes(data)
        duration = _verify_decodable(staging)
        os.replace(staging, dest)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    finally:
        staging.unlink(missing_ok=True)

    digest = _sha256_file(dest)
    invalidate_render_artifacts(project_id, alignment=True)
    # Also drop derived wav so resume cannot reuse stale conversion.
    from studio.projects import input_prefix, shorts_input_prefix

    for prefix in (input_prefix(project_id), shorts_input_prefix(project_id)):
        wav = Path(prefix).with_suffix(".wav")
        try:
            if wav.is_file():
                wav.unlink()
        except OSError:
            pass

    meta = load_meta(project_id)
    key = "external_audio_shorts" if shorts else "external_audio"
    meta[key] = {
        "filename": dest.name,
        "path": str(dest),
        "bytes": dest.stat().st_size,
        "sha256": digest,
        "duration_seconds": duration,
        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    # Clear alignment reuse stamp
    meta.pop("external_align_cache", None)
    if shorts:
        meta.pop("external_align_cache_shorts", None)
    save_meta(project_id, meta)
    log.info(
        "external_audio_upload project_id=%s shorts=%s bytes=%s duration=%.2fs sha=%s",
        project_id,
        shorts,
        dest.stat().st_size,
        duration,
        digest[:16],
    )
    return {
        "ok": True,
        "path": str(dest),
        "filename": dest.name,
        "bytes": dest.stat().st_size,
        "duration_seconds": round(duration, 3),
        "sha256": digest,
        "content_hash": digest,
        "shorts": bool(shorts),
        "audio_source": "external",
        "note": (
            "Saved narration_external.mp3. Prior Gentle alignment invalidated. "
            "Set tts_provider=external on this job, then start_job / resume_job "
            "(or generate_speech) to align with Gentle."
        ),
    }


def _ffmpeg_mp3_to_wav(src: Path, dest: Path) -> None:
    from studio.ffmpeg_bin import ffmpeg_cmd

    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.with_suffix(".staging.wav")
    try:
        subprocess.check_call(
            ffmpeg_cmd(
                "-y",
                "-i",
                str(src),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "22050",
                "-ac",
                "1",
                str(staging),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.replace(staging, dest)
    finally:
        staging.unlink(missing_ok=True)


def _word_count_for_prefix(prefix: Path) -> int:
    for name in (prefix.name + "_g.txt", prefix.name + "_raw.txt"):
        path = prefix.with_name(name)
        if path.is_file():
            try:
                return len(path.read_text(encoding="utf-8").split())
            except OSError:
                pass
    return 0


def duration_sanity_warning(duration_sec: float, word_count: int) -> str | None:
    """Warn when duration is far from ~140 wpm expectation (do not block)."""
    if word_count <= 0 or duration_sec <= 0:
        return None
    expected_min = word_count / 140.0
    if expected_min <= 0:
        return None
    ratio = (duration_sec / 60.0) / expected_min if expected_min else 0
    # Compare duration minutes vs expected minutes at 140 wpm
    expected_sec = expected_min * 60.0
    if duration_sec < 0.5 * expected_sec:
        msg = (
            f"WARNING: external narration is {duration_sec:.1f}s but script is ~{word_count} words "
            f"(~{expected_sec:.0f}s at 140 wpm) — under 50% of expected length."
        )
        log.warning(msg)
        return msg
    if duration_sec > 2.0 * expected_sec:
        msg = (
            f"WARNING: external narration is {duration_sec:.1f}s but script is ~{word_count} words "
            f"(~{expected_sec:.0f}s at 140 wpm) — over 200% of expected length."
        )
        log.warning(msg)
        return msg
    return None


def require_external_mp3(project_id: str, *, shorts: bool = False) -> Path:
    path = external_mp3_path(project_id, shorts=shorts)
    if not external_audio_present(project_id, shorts=shorts):
        label = EXTERNAL_AUDIO_SHORTS_NAME if shorts else EXTERNAL_AUDIO_NAME
        raise RuntimeError(
            f"[{EXTERNAL_AUDIO_MISSING}] tts_provider=external but no narration uploaded "
            f"({label} missing). Call upload_narration_audio or switch tts_provider back to local."
        )
    return path


def materialize_external_wav(project_id: str, dest_wav: Path, *, shorts: bool = False) -> dict[str, Any]:
    """Convert uploaded MP3 → mono 22050 wav at the pipeline's expected path."""
    mp3 = require_external_mp3(project_id, shorts=shorts)
    _ffmpeg_mp3_to_wav(mp3, dest_wav)
    if not dest_wav.is_file() or dest_wav.stat().st_size < 1000:
        raise RuntimeError(f"[{EXTERNAL_AUDIO_MISSING}] Failed to convert {mp3.name} to wav.")
    duration = probe_audio_duration_seconds(dest_wav) or probe_audio_duration_seconds(mp3) or 0.0
    words = _word_count_for_prefix(dest_wav.with_suffix(""))
    # prefix is script / script_9x16 — word count from sibling _g.txt
    from studio.projects import input_prefix, shorts_input_prefix

    prefix = Path(shorts_input_prefix(project_id) if shorts else input_prefix(project_id))
    words = _word_count_for_prefix(prefix)
    warning = duration_sanity_warning(duration, words)
    digest = _sha256_file(mp3)
    return {
        "ok": True,
        "path": str(dest_wav),
        "mp3_path": str(mp3),
        "bytes": dest_wav.stat().st_size,
        "duration_seconds": round(duration, 3) if duration else None,
        "sha256": digest,
        "word_count": words,
        "warning": warning,
        "audio_source": "external",
        "shorts": bool(shorts),
    }


def align_cache_key(project_id: str, *, shorts: bool = False) -> str:
    return "external_align_cache_shorts" if shorts else "external_align_cache"


def can_reuse_external_alignment(project_id: str, *, shorts: bool = False) -> bool:
    """True when Gentle json is fresh and matches current MP3 + transcript hashes."""
    from studio.pipeline import alignment_is_fresh

    if not alignment_is_fresh(project_id, shorts=shorts):
        return False
    if not external_audio_present(project_id, shorts=shorts):
        return False
    meta = load_meta(project_id)
    cache = meta.get(align_cache_key(project_id, shorts=shorts)) or {}
    mp3 = external_mp3_path(project_id, shorts=shorts)
    try:
        digest = _sha256_file(mp3)
    except OSError:
        return False
    if cache.get("audio_sha256") != digest:
        return False
    from studio.projects import input_prefix, shorts_input_prefix

    prefix = Path(shorts_input_prefix(project_id) if shorts else input_prefix(project_id))
    g_txt = prefix.with_name(prefix.name + "_g.txt")
    try:
        g_hash = _sha256_file(g_txt) if g_txt.is_file() else ""
    except OSError:
        g_hash = ""
    if not g_hash or cache.get("transcript_sha256") != g_hash:
        return False
    log.info(
        "skip_decision=cache_reuse project_id=%s step=align source=external shorts=%s",
        project_id,
        shorts,
    )
    return True


def remember_external_alignment(project_id: str, *, shorts: bool = False, audio_sha256: str = "") -> None:
    from studio.projects import input_prefix, shorts_input_prefix

    prefix = Path(shorts_input_prefix(project_id) if shorts else input_prefix(project_id))
    g_txt = prefix.with_name(prefix.name + "_g.txt")
    mp3 = external_mp3_path(project_id, shorts=shorts)
    meta = load_meta(project_id)
    meta[align_cache_key(project_id, shorts=shorts)] = {
        "audio_sha256": audio_sha256 or (_sha256_file(mp3) if mp3.is_file() else ""),
        "transcript_sha256": _sha256_file(g_txt) if g_txt.is_file() else "",
        "aligned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "json": str(prefix.with_suffix(".json")),
    }
    save_meta(project_id, meta)


def ensure_spoken_transcript(project_id: str, *, shorts: bool = False) -> Path:
    """Ensure *_g.txt / *_raw.txt exist with emotion tags stripped (same as Chatterbox path)."""
    from studio.projects import input_prefix, shorts_input_prefix, write_shorts_scripts
    from studio.utils_script import raw_from_tagged

    if shorts:
        write_shorts_scripts(project_id)
        prefix = Path(shorts_input_prefix(project_id))
    else:
        prefix = Path(input_prefix(project_id))
        tagged = prefix.with_suffix(".txt")
        if not tagged.is_file():
            raise RuntimeError("Generate a script before external audio alignment.")
        raw = raw_from_tagged(tagged.read_text(encoding="utf-8"))
        prefix.with_name(prefix.name + "_raw.txt").write_text(raw, encoding="utf-8")
        prefix.with_name(prefix.name + "_g.txt").write_text(raw, encoding="utf-8")
    g_txt = prefix.with_name(prefix.name + "_g.txt")
    if not g_txt.is_file() or g_txt.stat().st_size < 10:
        raise RuntimeError("Spoken transcript (*_g.txt) is missing after tag strip.")
    return g_txt


def project_audio_source(project_id: str | dict | None = None) -> str:
    """Return audio_source label for status payloads."""
    from studio.settings import load_settings, normalize_tts_provider

    if isinstance(project_id, dict):
        meta = project_id
        provider = normalize_tts_provider(
            meta.get("tts_provider") or meta.get("voice_provider") or load_settings().get("tts_provider") or "local",
            default="local",
        )
    elif project_id:
        meta = load_meta(project_id)
        provider = normalize_tts_provider(
            meta.get("tts_provider") or meta.get("voice_provider") or load_settings().get("tts_provider") or "local",
            default="local",
        )
    else:
        provider = normalize_tts_provider(load_settings().get("tts_provider") or "local", default="local")
    if provider == "external":
        return "external"
    if provider == "local":
        return "chatterbox"
    return provider
