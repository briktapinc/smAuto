from __future__ import annotations

import io
import math
import tempfile
from pathlib import Path

from studio.projects import (
    input_prefix,
    load_meta,
    project_generate_9x16,
    save_meta,
    shorts_input_prefix,
    write_shorts_scripts,
)
from studio.settings import default_voice_for_provider, load_settings, normalize_tts_provider
from studio.tts_local import list_local_voices, write_local_wav
from studio.aspect import needs_explainer_assets, DEFAULT_ASPECT

OPENAI_VOICES = [
    {"id": "alloy", "name": "Alloy", "style": "neutral"},
    {"id": "ash", "name": "Ash", "style": "clear"},
    {"id": "ballad", "name": "Ballad", "style": "warm narrative"},
    {"id": "coral", "name": "Coral", "style": "friendly explainer"},
    {"id": "echo", "name": "Echo", "style": "steady"},
    {"id": "fable", "name": "Fable", "style": "storyteller"},
    {"id": "onyx", "name": "Onyx", "style": "deep"},
    {"id": "nova", "name": "Nova", "style": "bright"},
    {"id": "sage", "name": "Sage", "style": "calm teacher"},
    {"id": "shimmer", "name": "Shimmer", "style": "light"},
    {"id": "verse", "name": "Verse", "style": "expressive"},
]


def list_voices(provider: str | None = None) -> dict:
    settings = load_settings()
    provider = normalize_tts_provider(
        provider or settings.get("tts_provider") or settings.get("voice_provider") or "openai"
    )
    if provider == "openai":
        return {"provider": "openai", "voices": OPENAI_VOICES}
    if provider == "elevenlabs":
        return {"provider": "elevenlabs", "voices": _eleven_voices(settings)}
    if provider == "local":
        return list_local_voices()
    raise RuntimeError(f"Unknown voice provider: {provider}")


def _eleven_voices(settings: dict) -> list[dict]:
    import httpx

    key = settings.get("elevenlabs_api_key") or ""
    if not key:
        raise RuntimeError("ElevenLabs API key is not set. Add it in Settings.")
    response = httpx.get(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": key},
        timeout=30,
    )
    response.raise_for_status()
    voices = []
    for item in response.json().get("voices", []):
        voices.append(
            {
                "id": item.get("voice_id"),
                "name": item.get("name"),
                "style": (item.get("labels") or {}).get("description") or item.get("category") or "",
            }
        )
    return voices


def _chunks(text: str, limit: int = 3500) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts = []
    buf = []
    size = 0
    for paragraph in text.split("\n"):
        piece = paragraph + "\n"
        if size + len(piece) > limit and buf:
            parts.append("".join(buf).strip())
            buf = [piece]
            size = len(piece)
        else:
            buf.append(piece)
            size += len(piece)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _write_openai_wav(text: str, dest: Path, settings: dict, voice: str) -> None:
    from openai import OpenAI

    key = settings.get("openai_api_key") or ""
    if not key:
        raise RuntimeError("OpenAI API key is not set. Add it in Settings.")
    client = OpenAI(api_key=key)
    model = settings.get("openai_tts_model") or "gpt-4o-mini-tts"
    chunks = _chunks(text)
    wavs = []
    tmp = Path(tempfile.mkdtemp(prefix="lazykh-tts-"))
    for i, chunk in enumerate(chunks):
        kwargs = {
            "model": model,
            "voice": voice,
            "input": chunk,
            "response_format": "wav",
        }
        if "gpt-4o" in model or "mini-tts" in model:
            from studio.prompts import get_prompt

            kwargs["instructions"] = get_prompt("tts.openai_instructions")
        try:
            audio = client.audio.speech.create(**kwargs)
        except Exception:
            kwargs.pop("instructions", None)
            kwargs.pop("response_format", None)
            audio = client.audio.speech.create(**kwargs)
        part = tmp / f"part{i:03d}.wav"
        audio.write_to_file(str(part))
        wavs.append(part)
    _concat_audio(wavs, dest)


def _write_eleven_wav(text: str, dest: Path, settings: dict, voice_id: str) -> None:
    import httpx

    key = settings.get("elevenlabs_api_key") or ""
    if not key:
        raise RuntimeError("ElevenLabs API key is not set. Add it in Settings.")
    if not voice_id:
        raise RuntimeError("Pick an ElevenLabs voice first.")
    model = settings.get("elevenlabs_model") or "eleven_multilingual_v2"
    chunks = _chunks(text, limit=4000)
    tmp = Path(tempfile.mkdtemp(prefix="lazykh-el-"))
    wavs = []
    for i, chunk in enumerate(chunks):
        response = httpx.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": key, "Accept": "audio/mpeg"},
            json={"text": chunk, "model_id": model, "voice_settings": {"stability": 0.4, "similarity_boost": 0.75}},
            timeout=120,
        )
        response.raise_for_status()
        mp3 = tmp / f"part{i:03d}.mp3"
        mp3.write_bytes(response.content)
        wav = tmp / f"part{i:03d}.wav"
        _ffmpeg_convert(mp3, wav)
        wavs.append(wav)
    _concat_audio(wavs, dest)


def _ffmpeg_convert(src: Path, dest: Path) -> None:
    import subprocess

    from studio.ffmpeg_bin import ffmpeg_cmd

    subprocess.check_call(
        ffmpeg_cmd("-y", "-i", str(src), "-acodec", "pcm_s16le", "-ar", "22050", "-ac", "1", str(dest)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _concat_audio(parts: list[Path], dest: Path) -> None:
    import subprocess

    from studio.ffmpeg_bin import ffmpeg_cmd

    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        src = parts[0]
        if src.suffix.lower() == ".wav":
            dest.write_bytes(src.read_bytes())
            return
        _ffmpeg_convert(src, dest)
        return
    listing = dest.with_suffix(".concat.txt")
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    subprocess.check_call(
        ffmpeg_cmd("-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def generate_audio(
    project_id: str,
    provider: str | None = None,
    voice_id: str | None = None,
    *,
    shorts: bool = False,
) -> dict:
    settings = load_settings()
    meta = load_meta(project_id)
    provider = normalize_tts_provider(
        provider
        or meta.get("tts_provider")
        or meta.get("voice_provider")
        or settings.get("tts_provider")
        or settings.get("voice_provider")
        or "openai"
    )
    if shorts:
        write_shorts_scripts(project_id)
        prefix = Path(shorts_input_prefix(project_id))
    else:
        prefix = Path(input_prefix(project_id))
    raw_path = prefix.with_name(prefix.name + "_raw.txt")
    if not raw_path.is_file():
        raise RuntimeError("Generate a script before audio.")
    text = raw_path.read_text(encoding="utf-8")
    dest = prefix.with_suffix(".wav")
    extra = {}
    if provider == "openai":
        voice = voice_id or meta.get("voice_id") or default_voice_for_provider("openai", settings)
        _write_openai_wav(text, dest, settings, voice)
    elif provider == "elevenlabs":
        voice = voice_id or meta.get("voice_id") or default_voice_for_provider("elevenlabs", settings)
        _write_eleven_wav(text, dest, settings, voice)
    elif provider == "local":
        voice = voice_id or meta.get("voice_id") or default_voice_for_provider("local", settings)
        extra = write_local_wav(text, dest, voice)
        voice = extra.get("voice_id") or voice or "default"
    else:
        raise RuntimeError("Voice provider must be openai, elevenlabs, or local (Resemble Chatterbox).")
    meta["tts_provider"] = provider
    meta["voice_provider"] = provider
    meta["voice_id"] = voice
    meta["status"] = "audio"
    if extra:
        meta["tts_engine"] = extra.get("engine")
        meta["tts_device"] = extra.get("device")
    save_meta(project_id, meta)
    payload = {
        "ok": True,
        "provider": provider,
        "voice_id": voice,
        "path": str(dest),
        "shorts": bool(shorts),
    }
    if extra:
        payload["engine"] = extra.get("engine")
        payload["device"] = extra.get("device")
    return payload


def generate_project_audio(
    project_id: str,
    provider: str | None = None,
    voice_id: str | None = None,
) -> dict:
    """Write full explainer wav and/or 9:16 hook+CTA wav based on job aspect + generate_9x16."""
    meta = load_meta(project_id)
    aspect = meta.get("aspect") or DEFAULT_ASPECT
    results: dict = {"ok": True, "parts": []}
    if needs_explainer_assets(aspect):
        results["parts"].append(generate_audio(project_id, provider=provider, voice_id=voice_id, shorts=False))
        results["path"] = results["parts"][-1]["path"]
    if project_generate_9x16(meta):
        results["parts"].append(generate_audio(project_id, provider=provider, voice_id=voice_id, shorts=True))
        results["shorts_path"] = results["parts"][-1]["path"]
    if not results["parts"]:
        # Fallback: always at least the full script audio.
        results["parts"].append(generate_audio(project_id, provider=provider, voice_id=voice_id, shorts=False))
        results["path"] = results["parts"][-1]["path"]
    results["provider"] = results["parts"][-1].get("provider")
    results["voice_id"] = results["parts"][-1].get("voice_id")
    return results
