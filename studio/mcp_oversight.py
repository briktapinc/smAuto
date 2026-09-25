"""MCP helpers that are not already covered by get_file, get_project, or list_illustration_jobs."""

from __future__ import annotations

import base64
import subprocess
import wave
from pathlib import Path

from studio.aspect import normalize_aspect
from studio.ffmpeg_bin import resolve_ffmpeg
from studio.pipeline import alignment_is_fresh, job_status
from studio.projects import final_video_path, frames_dir, input_prefix, load_meta, project_dir
from studio.render_trace import kill_render_processes, read_render_log
from studio.tts_external import probe_audio_duration_seconds


def get_render_log(project_id: str, tail_chars: int = 12000) -> dict:
    log = read_render_log(project_id, tail_chars=tail_chars)
    status = job_status(project_id)
    return {
        "ok": True,
        "project_id": project_id,
        "step": status.get("step"),
        "running": status.get("running"),
        "error": status.get("error"),
        "detail": status.get("detail"),
        "youtube_error": status.get("youtube_error"),
        **log,
    }


def cancel_render(project_id: str) -> dict:
    from studio.pipeline import stop_project

    killed = kill_render_processes(project_id)
    status = stop_project(project_id)
    status["killed_pids"] = killed
    status["killed"] = bool(killed)
    status["detail"] = (
        "Killed the in-process render (scheduler, frame draw, or ffmpeg) and stopped the job."
        if killed
        else (status.get("detail") or "Stop requested. No live render subprocess was registered.")
    )
    return status


def extract_frame(project_id: str, timestamp: str = "1", aspect: str = "") -> dict:
    meta = load_meta(project_id)
    chosen = normalize_aspect(aspect or meta.get("last_render_aspect") or meta.get("aspect") or "16:9")
    video = final_video_path(project_id, chosen)
    if not video.is_file():
        raise RuntimeError(f"No {chosen} video yet at {video.name}.")
    stamp = (timestamp or "1").strip() or "1"
    safe = stamp.replace(":", "-").replace(".", "p")
    dest = project_dir(project_id) / "_audit_frames" / f"{chosen.replace(':', 'x')}_{safe}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        resolve_ffmpeg(),
        "-y",
        "-ss",
        stamp,
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(dest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dest.is_file():
        err = (proc.stderr or proc.stdout or "ffmpeg failed").strip()
        raise RuntimeError(err[-800:])
    data = dest.read_bytes()
    return {
        "ok": True,
        "project_id": project_id,
        "aspect": chosen,
        "timestamp": stamp,
        "path": str(dest),
        "bytes": len(data),
        "media_type": "image/jpeg",
        "base64": base64.b64encode(data).decode("ascii"),
    }


def _wav_seconds(path: Path) -> float | None:
    probed = probe_audio_duration_seconds(path)
    if probed:
        return round(probed, 3)
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate() or 1
            return round(handle.getnframes() / float(rate), 3)
    except Exception:
        return None


def get_narration_info(project_id: str) -> dict:
    prefix = Path(input_prefix(project_id))
    raw = prefix.with_name(prefix.name + "_raw.txt")
    tagged = prefix.with_suffix(".txt")
    wav = prefix.with_suffix(".wav")
    spoken = raw.read_text(encoding="utf-8") if raw.is_file() else ""
    words = len(spoken.split())
    wav_mtime = wav.stat().st_mtime if wav.is_file() else 0
    raw_mtime = raw.stat().st_mtime if raw.is_file() else 0
    newer = bool(wav.is_file() and raw.is_file() and wav_mtime + 0.05 >= raw_mtime)
    aligned = alignment_is_fresh(project_id, shorts=False)
    return {
        "ok": True,
        "project_id": project_id,
        "has_script_raw": raw.is_file(),
        "has_script_tagged": tagged.is_file(),
        "has_wav": wav.is_file(),
        "script_raw_words": words,
        "script_raw_mtime": raw_mtime or None,
        "wav_mtime": wav_mtime or None,
        "wav_seconds": _wav_seconds(wav) if wav.is_file() else None,
        "wav_newer_than_script_raw": newer,
        "alignment_matches_current_script": aligned,
        "audio_matches_current_script": bool(newer and aligned),
        "note": (
            "audio_matches_current_script is true only when the wav is newer than script_raw.txt "
            "and Gentle json matches that same spoken text."
        ),
    }


def estimate_render_time(project_id: str, aspect: str = "") -> dict:
    """Rough wall-clock guess. Drawer is about 8 frames per wall second on this host."""
    info = get_narration_info(project_id)
    seconds = float(info.get("wav_seconds") or 0)
    if seconds <= 0:
        try:
            seconds = float(load_meta(project_id).get("duration_seconds") or 0)
        except Exception:
            seconds = 0
    total_frames = int(round(max(0.0, seconds) * 30))
    folder = frames_dir(project_id, aspect or None)
    have = 0
    if folder.is_dir():
        have = sum(1 for p in folder.glob("f*.png") if p.is_file())
    remaining = max(0, total_frames - have)
    draw_sec = remaining / 8.0
    mux_sec = 25.0 if total_frames else 0.0
    eta = int(round(draw_sec + mux_sec))
    return {
        "ok": True,
        "project_id": project_id,
        "audio_seconds": round(seconds, 1),
        "frames_expected": total_frames,
        "frames_done": have,
        "frames_remaining": remaining,
        "eta_seconds": eta,
        "eta_note": "Estimate only: about 8 drawn frames per second, plus ~25s to mux. Not a measurement.",
    }
