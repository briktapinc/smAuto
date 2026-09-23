"""Local narration TTS: Resemble Chatterbox first, Piper ONNX as a CPU fallback.

Studio itself does not import torch at boot. Weights load in an isolated worker
process on generate so a native Chatterbox/torch crash cannot kill the API.
Never fall back to OpenAI/ElevenLabs from this module.

Set BUBBLEPOD_TTS_INPROCESS=1 to force in-process generate (debug only).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path

from studio.paths import REPO_ROOT, USER_DATA, ensure_dirs

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Full scripts can take a long time on CPU Chatterbox.
_WORKER_TIMEOUT_SEC = int(os.environ.get("BUBBLEPOD_TTS_WORKER_TIMEOUT_SEC") or 3600)

CHATTERBOX_HINT = (
    "Local TTS needs Resemble Chatterbox. Install with: "
    "pip install chatterbox-tts "
    "(or pip install -r requirements-tts-local.txt), then restart Studio. "
    "Python 3.11 on Windows is supported. CUDA is optional - CPU works but is slower. "
    "Studio will not use OpenAI TTS while Local is selected."
)

PIPER_HINT = (
    "Chatterbox is not installed. For a lighter CPU local voice, install Piper: "
    "pip install piper-tts "
    "and put an English ONNX voice in user_data/piper/ "
    "(for example en_US-lessac-medium.onnx from rhasspy/piper-voices). "
    "Studio will not use OpenAI TTS while Local is selected."
)

_lock = threading.Lock()
_chatterbox_model = None
_chatterbox_loaded_device = ""


def voices_dir() -> Path:
    ensure_dirs()
    path = USER_DATA / "voices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def piper_dir() -> Path:
    ensure_dirs()
    path = USER_DATA / "piper"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def chatterbox_installed() -> bool:
    return _has_module("chatterbox.tts")


def piper_installed() -> bool:
    return _has_module("piper") or _has_module("piper.voice")


def piper_voice_files() -> list[Path]:
    folder = piper_dir()
    return sorted(p for p in folder.glob("*.onnx") if p.is_file())


def local_tts_status() -> dict:
    """Cheap probe (no torch load) for Settings / MCP."""
    cuda_ok = False
    cuda_name = ""
    try:
        import torch

        cuda_ok = bool(torch.cuda.is_available())
        if cuda_ok:
            try:
                cuda_name = str(torch.cuda.get_device_name(0) or "")
            except Exception:
                cuda_name = "cuda"
    except Exception:
        pass

    if chatterbox_installed():
        if cuda_ok:
            warn = (
                f"Prefer GPU ({cuda_name or 'CUDA'}) for Local TTS; falls back to CPU if load fails. "
                "Generate runs in an isolated worker so a crash will not take down Studio."
            )
        else:
            warn = (
                "PyTorch has no CUDA in this Python — Local TTS will use CPU (slower). "
                "Install a CUDA torch build to use VRAM. Generate runs in an isolated worker."
            )
        return {
            "ready": True,
            "engine": "chatterbox",
            "label": "Resemble Chatterbox",
            "device": "cuda" if cuda_ok else "cpu",
            "cuda_available": cuda_ok,
            "cuda_device_name": cuda_name,
            "error": "",
            "warning": warn,
            "install_hint": CHATTERBOX_HINT,
        }
    if piper_installed():
        voices = piper_voice_files()
        if voices:
            return {
                "ready": True,
                "engine": "piper",
                "label": "Piper (ONNX)",
                "device": "cpu",
                "cuda_available": cuda_ok,
                "cuda_device_name": cuda_name,
                "error": "",
                "warning": "Chatterbox was not found; using Piper as the local fallback.",
                "install_hint": PIPER_HINT,
            }
        return {
            "ready": False,
            "engine": "piper",
            "label": "Piper (ONNX)",
            "device": "cpu",
            "cuda_available": cuda_ok,
            "cuda_device_name": cuda_name,
            "error": (
                "Piper is installed but no ONNX voice is in user_data/piper/. "
                + PIPER_HINT
            ),
            "warning": "",
            "install_hint": PIPER_HINT,
        }
    return {
        "ready": False,
        "engine": "none",
        "label": "Not installed",
        "device": "",
        "cuda_available": cuda_ok,
        "cuda_device_name": cuda_name,
        "error": CHATTERBOX_HINT,
        "warning": "",
        "install_hint": CHATTERBOX_HINT,
    }


def require_local_engine() -> dict:
    status = local_tts_status()
    if not status.get("ready"):
        raise RuntimeError(status.get("error") or CHATTERBOX_HINT)
    return status


def list_local_voices() -> dict:
    status = local_tts_status()
    voices: list[dict] = []
    engine = status.get("engine")
    if engine == "chatterbox":
        voices.append(
            {
                "id": "default",
                "name": "Chatterbox English",
                "style": "built-in Resemble voice",
            }
        )
        for wav in sorted(voices_dir().glob("*.wav")):
            voices.append(
                {
                    "id": wav.stem,
                    "name": wav.stem.replace("_", " ").replace("-", " ").title(),
                    "style": "voice prompt / clone (optional)",
                }
            )
    elif engine == "piper":
        files = piper_voice_files()
        if files:
            for onnx in files:
                voices.append(
                    {
                        "id": onnx.stem,
                        "name": onnx.stem.replace("_", " "),
                        "style": "Piper ONNX",
                    }
                )
        else:
            voices.append({"id": "default", "name": "Piper", "style": "install a .onnx voice"})
    return {
        "provider": "local",
        "engine": engine,
        "ready": bool(status.get("ready")),
        "error": status.get("error") or "",
        "install_hint": status.get("install_hint") or "",
        "warning": status.get("warning") or "",
        "voices": voices,
    }


def _local_chunks(text: str, limit: int = 280) -> list[str]:
    text = " ".join((text or "").split()).strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    tokens: list[str] = []
    for para in text.split("\n"):
        piece = para.strip()
        if piece:
            tokens.extend(_split_sentences(piece))
    for sentence in tokens:
        extra = len(sentence) + (1 if buf else 0)
        if size + extra > limit and buf:
            parts.append(" ".join(buf).strip())
            buf = [sentence]
            size = len(sentence)
        else:
            buf.append(sentence)
            size += extra
    if buf:
        parts.append(" ".join(buf).strip())
    out = [p for p in parts if p]
    return out or [text[:limit]]


def _split_sentences(text: str) -> list[str]:
    import re

    bits = re.split(r"(?<=[.!?])\s+", text.strip())
    return [b.strip() for b in bits if b.strip()]


def _resolve_voice_prompt(voice_id: str | None) -> str | None:
    raw = (voice_id or "").strip()
    if not raw or raw.lower() in ("default", "chatterbox", "english", "resemble"):
        return None
    folder = voices_dir()
    for candidate in (folder / raw, folder / f"{raw}.wav", Path(raw)):
        if candidate.is_file():
            return str(candidate)
    return None


def _prefer_chatterbox_device() -> str:
    """Prefer CUDA when this Python's torch build can see a GPU; else CPU."""
    from studio.device import preferred_torch_device

    return preferred_torch_device()


def _load_chatterbox():
    global _chatterbox_model, _chatterbox_loaded_device
    with _lock:
        if _chatterbox_model is not None:
            return _chatterbox_model
        try:
            from chatterbox.tts import ChatterboxTTS
        except Exception as exc:
            raise RuntimeError(
                f"chatterbox-tts is installed but failed to import ({exc}). {CHATTERBOX_HINT}"
            ) from exc

        preferred = _prefer_chatterbox_device()
        devices = [preferred]
        if preferred == "cuda":
            devices.append("cpu")

        last_exc: Exception | None = None
        model = None
        device = preferred
        for device in devices:
            try:
                model = ChatterboxTTS.from_pretrained(device=device)
                break
            except Exception as exc:
                last_exc = exc
                model = None
                if device == "cuda":
                    # Prefer GPU, but keep Studio usable on machines without a working CUDA stack.
                    continue
                break

        if model is None:
            raise RuntimeError(
                "Chatterbox could not load the Resemble model "
                f"({last_exc}). First run downloads weights from Hugging Face "
                "(ResembleAI/chatterbox). Tried device order: "
                f"{', '.join(devices)}. Studio will not bill OpenAI TTS. "
                + CHATTERBOX_HINT
            ) from last_exc
        if getattr(model, "conds", None) is None:
            raise RuntimeError(
                "Chatterbox loaded without its built-in English voice (conds.pt missing). "
                "Re-download ResembleAI/chatterbox or pass a .wav prompt in user_data/voices/. "
                "Studio will not bill OpenAI TTS."
            )
        _chatterbox_model = model
        _chatterbox_loaded_device = device
        return model


def _write_chatterbox_wav(text: str, dest: Path, voice_id: str | None) -> dict:
    import torch
    import torchaudio

    model = _load_chatterbox()
    prompt = _resolve_voice_prompt(voice_id)
    chunks = _local_chunks(text)
    if not chunks:
        raise RuntimeError("Script text is empty; nothing to speak.")
    waves = []
    sr = int(getattr(model, "sr", 24000) or 24000)
    for i, chunk in enumerate(chunks):
        kwargs = {"text": chunk}
        if prompt:
            kwargs["audio_prompt_path"] = prompt
        try:
            wav = model.generate(**kwargs)
        except Exception as exc:
            raise RuntimeError(
                f"Chatterbox generate failed ({exc}). Studio will not fall back to OpenAI TTS. "
                + CHATTERBOX_HINT
            ) from exc
        if not torch.is_tensor(wav):
            wav = torch.as_tensor(wav)
        if wav.dim() == 1:
            wav = wav.unsqueeze(0)
        waves.append(wav.detach().cpu().float())
        if i + 1 < len(chunks):
            waves.append(torch.zeros(1, int(0.12 * sr)))
    audio = torch.cat(waves, dim=-1)
    if sr != 22050:
        audio = torchaudio.functional.resample(audio, sr, 22050)
        sr = 22050
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="lazykh-local-tts-")) / "speech.wav"
    torchaudio.save(str(tmp), audio, sr)
    dest.write_bytes(tmp.read_bytes())
    return {
        "engine": "chatterbox",
        "device": _chatterbox_loaded_device or getattr(model, "device", "cpu"),
        "voice_id": "default" if not prompt else Path(prompt).stem,
        "sample_rate": sr,
    }


def _resolve_piper_onnx(voice_id: str | None) -> Path:
    files = piper_voice_files()
    if not files:
        raise RuntimeError(PIPER_HINT)
    raw = (voice_id or "").strip()
    if raw and raw.lower() not in ("default", "piper"):
        for onnx in files:
            if onnx.stem == raw or onnx.name == raw:
                return onnx
        named = piper_dir() / raw
        if named.is_file():
            return named
        named = piper_dir() / f"{raw}.onnx"
        if named.is_file():
            return named
    return files[0]


def _write_piper_wav(text: str, dest: Path, voice_id: str | None) -> dict:
    try:
        from piper import PiperVoice
    except Exception as exc:
        raise RuntimeError(f"piper-tts import failed ({exc}). {PIPER_HINT}") from exc
    onnx = _resolve_piper_onnx(voice_id)
    try:
        voice = PiperVoice.load(str(onnx))
    except Exception as exc:
        raise RuntimeError(
            f"Could not load Piper voice {onnx.name} ({exc}). {PIPER_HINT}"
        ) from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    chunks = _local_chunks(text, limit=800) or [text]
    tmp = Path(tempfile.mkdtemp(prefix="lazykh-piper-"))
    parts = []
    for i, chunk in enumerate(chunks):
        part = tmp / f"part{i:03d}.wav"
        with wave.open(str(part), "wb") as wav_file:
            synthesized = False
            if hasattr(voice, "synthesize_wav"):
                voice.synthesize_wav(chunk, wav_file)
                synthesized = True
            elif hasattr(voice, "synthesize"):
                result = voice.synthesize(chunk, wav_file)
                if result is None:
                    synthesized = True
            if not synthesized:
                raise RuntimeError(
                    "This piper-tts build has no synthesize/synthesize_wav API. " + PIPER_HINT
                )
        parts.append(part)
    if len(parts) == 1:
        dest.write_bytes(parts[0].read_bytes())
    else:
        _concat_wavs(parts, dest)
    return {
        "engine": "piper",
        "device": "cpu",
        "voice_id": onnx.stem,
        "sample_rate": 22050,
    }


def _concat_wavs(parts: list[Path], dest: Path) -> None:
    import subprocess

    from studio.ffmpeg_bin import ffmpeg_cmd

    listing = dest.with_suffix(".concat.txt")
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    subprocess.check_call(
        ffmpeg_cmd("-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _use_inprocess_tts() -> bool:
    if (os.environ.get("BUBBLEPOD_TTS_WORKER") or "").strip() == "1":
        return True
    return (os.environ.get("BUBBLEPOD_TTS_INPROCESS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _popen_kwargs() -> dict:
    kw: dict = {}
    if os.name == "nt" and CREATE_NO_WINDOW:
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


def _write_local_wav_via_subprocess(text: str, dest: Path, voice_id: str | None = None) -> dict:
    """Run Chatterbox/Piper in a child process so Studio stays up on native crashes."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="lazykh-tts-worker-"))
    text_path = tmp / "text.txt"
    result_path = tmp / "result.json"
    text_path.write_text(text or "", encoding="utf-8")

    cmd = [
        sys.executable or "python",
        "-m",
        "studio.tts_local_worker",
        "--text-file",
        str(text_path),
        "--dest",
        str(dest),
        "--result-file",
        str(result_path),
    ]
    if voice_id:
        cmd.extend(["--voice-id", str(voice_id)])

    env = os.environ.copy()
    env["BUBBLEPOD_TTS_WORKER"] = "1"
    # Prefer this checkout on PYTHONPATH (Electron / packaged cwd may differ).
    py_path = env.get("PYTHONPATH", "")
    root = str(REPO_ROOT)
    env["PYTHONPATH"] = root if not py_path else root + os.pathsep + py_path

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=max(60, _WORKER_TIMEOUT_SEC),
            **_popen_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Local TTS worker timed out after {_WORKER_TIMEOUT_SEC}s. "
            "Studio is still running — retry, shorten the script, or switch TTS provider. "
            f"Partial stderr: {(exc.stderr or '')[-800:]}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Could not start local TTS worker ({exc}). Studio is still running. {CHATTERBOX_HINT}"
        ) from exc

    payload: dict = {}
    if result_path.is_file():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}

    if proc.returncode == 0 and payload.get("ok") and dest.is_file():
        info = {k: v for k, v in payload.items() if k != "ok"}
        info["provider"] = "local"
        info["isolated"] = True
        return info

    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    soft_err = str(payload.get("error") or "").strip()
    # Native crashes (e.g. torch access violation) often exit with a huge unsigned code
    # and never write a clean result — keep Studio alive and surface a clear error.
    crash_hint = ""
    if proc.returncode and int(proc.returncode) not in (0, 1, 2):
        crash_hint = (
            f" Worker exited abnormally (code {proc.returncode}) — likely a native "
            "PyTorch/Chatterbox crash. Studio stayed up."
        )
    detail = soft_err or stderr or stdout or f"exit code {proc.returncode}"
    if len(detail) > 1200:
        detail = detail[-1200:]
    raise RuntimeError(
        f"Local TTS worker failed: {detail}.{crash_hint} "
        "Retry the audio step, or switch TTS to OpenAI/ElevenLabs in Settings. "
        "Studio did not exit."
    )


def write_local_wav(text: str, dest: Path, voice_id: str | None = None) -> dict:
    from studio.gpu_lock import holding

    with holding("tts-local", kind="tts-local"):
        if _use_inprocess_tts():
            return _write_local_wav_unlocked(text, dest, voice_id)
        return _write_local_wav_via_subprocess(text, dest, voice_id)


def _write_local_wav_unlocked(text: str, dest: Path, voice_id: str | None = None) -> dict:
    status = require_local_engine()
    engine = status.get("engine")
    if engine == "chatterbox":
        info = _write_chatterbox_wav(text, dest, voice_id)
    elif engine == "piper":
        info = _write_piper_wav(text, dest, voice_id)
    else:
        raise RuntimeError(status.get("error") or CHATTERBOX_HINT)
    info["provider"] = "local"
    return info
