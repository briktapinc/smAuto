"""Loop background music under a voice track (original lazykh mixer).

The original script added `music * musicMulti` onto the voice wav, looping the
music until the voice ended, then wrote a mixed wav. Studio calls
`loop_mix_wav` / `mix_into_video` so the same add-and-loop sits under speech
for the finished video duration (5s title-card cover + explainer).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

# Original default was 0.044. Studio settings use music_volume_pct / 100.
musicMulti = 0.15


def _read_wav(path: str | Path) -> tuple[int, np.ndarray]:
    try:
        import scipy.io.wavfile as wavfile

        rate, data = wavfile.read(str(path))
        return int(rate), np.asarray(data)
    except ImportError:
        pass
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sw == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128
        data *= 256
    elif sw == 2:
        data = np.frombuffer(raw, dtype=np.int16)
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32)
    else:
        raise RuntimeError(f"Unsupported WAV sample width {sw} in {path}")
    if nch > 1:
        data = data.reshape(-1, nch)
    return int(rate), data


def _write_wav(path: str | Path, rate: int, data: np.ndarray) -> None:
    samples = np.asarray(data, dtype=np.int16)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        import scipy.io.wavfile as wavfile

        wavfile.write(str(dest), int(rate), samples)
        return
    except ImportError:
        pass
    with wave.open(str(dest), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(samples.tobytes())


def _to_mono_float(data: np.ndarray) -> np.ndarray:
    arr = np.array(data, dtype=np.float64)
    if arr.ndim == 2:
        arr = arr[:, 0]
    return arr


def loop_mix_wav(
    voice_path: str | Path,
    music_path: str | Path,
    output_path: str | Path,
    volume: float | None = None,
) -> Path:
    """Loop music under voice at `volume` (0–1), matching original musicAdder.py."""
    mix = musicMulti if volume is None else float(volume)
    mix = max(0.0, mix)
    rate, voice_raw = _read_wav(voice_path)
    rate2, music_raw = _read_wav(music_path)
    if int(rate2) != int(rate):
        raise RuntimeError(f"Sample rate mismatch: voice {rate} vs music {rate2}")
    voice_data = _to_mono_float(voice_raw)
    music_data = _to_mono_float(music_raw)
    if music_data.size == 0:
        raise RuntimeError(f"Music file is empty: {music_path}")
    voice_len = int(voice_data.shape[0])
    music_len = int(music_data.shape[0])
    added = np.zeros(voice_len, dtype=np.float64)
    for ind in range(0, voice_len, music_len):
        if ind + music_len >= voice_len:
            span = voice_len - ind
            added[ind:voice_len] = voice_data[ind:voice_len] + music_data[0:span] * mix
        else:
            added[ind : ind + music_len] = voice_data[ind : ind + music_len] + music_data * mix
    peak = float(np.amax(np.abs(added))) if added.size else 0.0
    if peak > 32767.0:
        added = added * (32767.0 / peak)
    finished = np.asarray(np.clip(added, -32768, 32767), dtype=np.int16)
    dest = Path(output_path)
    _write_wav(dest, rate, finished)
    return dest


def _ffmpeg_kwargs() -> dict:
    flags: dict = {}
    if os.name == "nt":
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return flags


def _ffmpeg_bin() -> str:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from studio.ffmpeg_bin import resolve_ffmpeg

    return resolve_ffmpeg()


def _ffprobe_bin() -> str:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from studio.ffmpeg_bin import resolve_ffprobe

    return resolve_ffprobe()


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, **_ffmpeg_kwargs())
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        tail = err[-800:] if err else f"exit {proc.returncode}"
        raise RuntimeError(f"ffmpeg failed: {tail}")


def probe_duration(path: str | Path) -> float:
    proc = subprocess.run(
        [
            _ffprobe_bin(),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        **_ffmpeg_kwargs(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {(proc.stderr or '').strip()[-400:]}")
    try:
        return float((proc.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"Could not read duration of {path}") from exc


def _has_audio(path: str | Path) -> bool:
    proc = subprocess.run(
        [
            _ffprobe_bin(),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        **_ffmpeg_kwargs(),
    )
    return "audio" in (proc.stdout or "").lower()


def _to_wav(src: str | Path, dest: Path, sample_rate: int, duration: float | None = None) -> None:
    cmd = [_ffmpeg_bin(), "-y", "-i", str(src)]
    if duration and duration > 0:
        cmd += ["-t", f"{duration:.4f}"]
    cmd += [
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        str(dest),
    ]
    _run_ffmpeg(cmd)


def _silence_wav(dest: Path, sample_rate: int, duration: float) -> None:
    dur = max(0.05, float(duration))
    _run_ffmpeg(
        [
            _ffmpeg_bin(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=mono",
            "-t",
            f"{dur:.4f}",
            "-acodec",
            "pcm_s16le",
            str(dest),
        ]
    )


def mix_into_video(
    video_path: str | Path,
    music_path: str | Path,
    output_path: str | Path,
    volume: float | None = None,
    sample_rate: int = 22050,
) -> Path:
    """Loop music for the full video duration and mix it under the existing speech track."""
    mix = musicMulti if volume is None else float(volume)
    video = Path(video_path)
    music = Path(music_path)
    dest = Path(output_path)
    if not video.is_file():
        raise RuntimeError(f"Video not found: {video}")
    if not music.is_file():
        raise RuntimeError(f"Music not found: {music}")
    if mix <= 0:
        if dest.resolve() != video.resolve():
            dest.write_bytes(video.read_bytes())
        return dest

    duration = probe_duration(video)
    if duration <= 0:
        raise RuntimeError(f"Video has no duration: {video}")

    with tempfile.TemporaryDirectory(prefix="lazykh-music-") as tmp:
        tmp_dir = Path(tmp)
        voice_wav = tmp_dir / "voice.wav"
        music_wav = tmp_dir / "music.wav"
        mixed_wav = tmp_dir / "mixed.wav"
        if _has_audio(video):
            _to_wav(video, voice_wav, sample_rate, duration=duration)
        else:
            print("Background music: video has no audio track; mixing over silence.")
            _silence_wav(voice_wav, sample_rate, duration)
        _to_wav(music, music_wav, sample_rate)
        loop_mix_wav(voice_wav, music_wav, mixed_wav, volume=mix)
        tmp_out = dest if dest.resolve() != video.resolve() else tmp_dir / "out.mp4"
        _run_ffmpeg(
            [
                _ffmpeg_bin(),
                "-y",
                "-i",
                str(video),
                "-i",
                str(mixed_wav),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-t",
                f"{duration:.4f}",
                str(tmp_out),
            ]
        )
        if tmp_out.resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(tmp_out.read_bytes())
    return dest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Loop music under a voice wav (original lazykh mixer).")
    parser.add_argument("--voice", default="", help="voice wav")
    parser.add_argument("--music", default="", help="music file (wav preferred)")
    parser.add_argument("--output", default="", help="mixed wav output")
    parser.add_argument("--volume", type=float, default=musicMulti, help="music loudness 0-1")
    args = parser.parse_args()
    voice = args.voice or os.environ.get("AUDIO_FILE_LOCATION", "")
    music = args.music or os.environ.get("MUSIC_FILE_LOCATION", "")
    output = args.output or os.environ.get("OUTPUT_FILE_LOCATION", "")
    if not (voice and music and output):
        print("Need --voice, --music, and --output (or the original LOCATION env vars).", file=sys.stderr)
        sys.exit(1)
    loop_mix_wav(voice, music, output, volume=args.volume)
    print(f"Wrote mixed wav: {output}")
