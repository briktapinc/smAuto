"""Gentle-compatible local aligner (no Docker / no Kaldi).

HTTP API matches lowerquality/gentle:
  GET  /                 -> JSON identifying this as the local backend
  POST /transcriptions?async=false  multipart audio + transcript

JSON words/phones are the shape scheduler.py already consumes.
"""

from __future__ import annotations

import os
import re
import tempfile
import traceback
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from studio.g2p_lite import phone_weight, syllable_weight, word_to_phones

# Do not steal a GPU from other local tools (VoiceSync, etc.).
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

_WHISPER_MODEL = None
_WHISPER_ERROR = None


def tokenize_transcript(text: str) -> list[str]:
    """Whitespace tokens from the spoken transcript (may still include punctuation)."""
    text = (text or "").replace("\r\n", "\n").strip()
    return [tok for tok in text.split() if tok]


_WORD_CORE = re.compile(r"[A-Za-z0-9']+")


def orthographic_word(token: str) -> str:
    """Core spelling for scheduler matching (Docker Gentle-style: no attached punctuation).

    Tagged scripts use [emphasis] brackets, e.g. ``[buffet],`` — scheduler searches for the
    Gentle ``word`` field inside that text, so ``buffet,`` must not keep the comma.
    """
    raw = (token or "").strip()
    if not raw:
        return ""
    match = _WORD_CORE.search(raw)
    return match.group(0) if match else raw


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    try:
        import soundfile as sf

        y, sr = sf.read(str(path), always_2d=False)
        y = np.asarray(y, dtype=np.float32)
        if y.ndim > 1:
            y = y.mean(axis=1)
        return y, int(sr)
    except Exception:
        pass
    import wave

    with wave.open(str(path), "rb") as wav:
        sr = wav.getframerate()
        nch = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width == 2:
        y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 1:
        y = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        y = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError(f"Unsupported WAV sample width: {width}")
    if nch > 1:
        y = y.reshape(-1, nch).mean(axis=1)
    return y, int(sr)


def _voiced_spans(y: np.ndarray, sr: int, frame_ms: float = 20.0) -> tuple[list[tuple[float, float]], float]:
    duration = max(len(y) / float(sr), 0.05)
    hop = max(1, int(sr * frame_ms / 1000.0))
    n = max(1, len(y) // hop)
    rms = np.array(
        [float(np.sqrt(np.mean(y[i * hop : (i + 1) * hop] ** 2) + 1e-12)) for i in range(n)],
        dtype=np.float32,
    )
    thr = max(float(np.median(rms)) * 0.55, float(np.percentile(rms, 25)) * 0.9, 0.008)
    voiced = rms > thr
    spans: list[tuple[float, float]] = []
    i = 0
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        j = i
        while j < n and voiced[j]:
            j += 1
        start = i * hop / sr
        end = min(duration, j * hop / sr)
        if end - start >= 0.04:
            spans.append((start, end))
        i = j
    if not spans:
        pad = min(0.08, duration * 0.05)
        spans = [(pad, max(pad + 0.05, duration - pad))]
    return spans, duration


def _map_words_to_spans(words: list[str], spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    weights = [float(syllable_weight(w)) for w in words]
    total_w = sum(weights) or float(len(words) or 1)
    voiced = sum(b - a for a, b in spans) or 0.05
    out: list[tuple[float, float]] = []
    cursor_span = 0
    cursor_t = spans[0][0]
    remain = weights[:]
    idx = 0
    while idx < len(words):
        need = voiced * (remain[idx] / total_w)
        start = cursor_t
        taken = 0.0
        while taken < need - 1e-6 and cursor_span < len(spans):
            a, b = spans[cursor_span]
            cursor_t = max(cursor_t, a)
            room = b - cursor_t
            if room <= 1e-4:
                cursor_span += 1
                if cursor_span < len(spans):
                    cursor_t = spans[cursor_span][0]
                continue
            take = min(room, need - taken)
            cursor_t += take
            taken += take
            if abs(cursor_t - b) < 1e-4:
                cursor_span += 1
                if cursor_span < len(spans):
                    cursor_t = spans[cursor_span][0]
        if taken < 1e-4:
            cursor_t = start + max(0.04, need)
        end = max(start + 0.03, cursor_t)
        out.append((start, end))
        idx += 1
    if out:
        last_end = spans[-1][1]
        s, e = out[-1]
        out[-1] = (s, max(e, last_end))
    return out


def _norm_token(word: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", word.lower())


def _whisper_words(path: Path) -> list[dict] | None:
    global _WHISPER_MODEL, _WHISPER_ERROR
    engine = (os.environ.get("LAZYKH_GENTLE_ENGINE") or "auto").strip().lower()
    if engine in {"energy", "rms", "off"}:
        return None
    try:
        import whisper
    except Exception as exc:
        _WHISPER_ERROR = f"whisper not installed ({exc})"
        return None
    try:
        if _WHISPER_MODEL is None:
            name = os.environ.get("LAZYKH_WHISPER_MODEL") or "tiny"
            _WHISPER_MODEL = whisper.load_model(name, device="cpu")
        result = _WHISPER_MODEL.transcribe(
            str(path),
            word_timestamps=True,
            language="en",
            fp16=False,
            condition_on_previous_text=False,
        )
    except Exception as exc:
        _WHISPER_ERROR = str(exc)
        return None
    words = []
    for seg in result.get("segments") or []:
        for item in seg.get("words") or []:
            token = str(item.get("word") or "").strip()
            if not token:
                continue
            try:
                start = float(item.get("start", 0.0))
                end = float(item.get("end", start))
            except (TypeError, ValueError):
                continue
            if end <= start:
                end = start + 0.05
            words.append({"word": token, "start": start, "end": end})
    return words or None


def _align_with_whisper(words: list[str], whispered: list[dict], duration: float) -> list[tuple[float, float]] | None:
    if not whispered:
        return None
    from difflib import SequenceMatcher

    a = [_norm_token(w) for w in words]
    b = [_norm_token(w["word"]) for w in whispered]
    if not any(a) or not any(b):
        return None
    matcher = SequenceMatcher(a=a, b=b, autojunk=False)
    if matcher.ratio() < 0.35:
        return None
    assigned: list[tuple[float, float] | None] = [None] * len(words)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag not in {"equal", "replace"}:
            continue
        span_a = i2 - i1
        span_b = j2 - j1
        if span_a <= 0 or span_b <= 0:
            continue
        for k in range(span_a):
            src = j1 + min(span_b - 1, int(k * span_b / span_a))
            hit = whispered[src]
            assigned[i1 + k] = (float(hit["start"]), float(hit["end"]))
    # Fill holes by interpolating neighbors.
    last_end = 0.04
    for i, slot in enumerate(assigned):
        if slot is None:
            continue
        assigned[i] = (max(0.0, slot[0]), min(duration, max(slot[0] + 0.03, slot[1])))
        last_end = assigned[i][1]
    for i, slot in enumerate(assigned):
        if slot is not None:
            continue
        prev_end = 0.0
        for j in range(i - 1, -1, -1):
            if assigned[j] is not None:
                prev_end = assigned[j][1]
                break
        next_start = duration
        for j in range(i + 1, len(assigned)):
            if assigned[j] is not None:
                next_start = assigned[j][0]
                break
        gap = max(0.04, next_start - prev_end)
        start = prev_end
        end = min(next_start, start + gap / 2.0 if next_start < duration else start + 0.12)
        assigned[i] = (start, max(start + 0.04, end))
    # Enforce monotonic times.
    t = 0.0
    out: list[tuple[float, float]] = []
    for slot in assigned:
        start, end = slot if slot is not None else (t, t + 0.08)
        start = max(t, start)
        end = max(start + 0.03, end)
        out.append((start, min(duration, end)))
        t = out[-1][1]
    if out:
        s, _e = out[-1]
        out[-1] = (s, max(out[-1][1], min(duration, last_end)))
    return out


def _phones_for_word(word: str, start: float, end: float) -> list[dict]:
    phones = word_to_phones(word)
    if not phones:
        return [{"duration": max(0.03, end - start), "phone": "sil"}]
    weights = [phone_weight(p) for p in phones]
    total = sum(weights) or float(len(phones))
    dur = max(0.04, end - start)
    tagged: list[dict] = []
    n = len(phones)
    for i, phone in enumerate(phones):
        length = dur * (weights[i] / total)
        if n == 1:
            suffix = "S"
        elif i == 0:
            suffix = "B"
        elif i == n - 1:
            suffix = "E"
        else:
            suffix = "I"
        tagged.append({"duration": round(length, 3), "phone": f"{phone}_{suffix}"})
    # Fix rounding so durations sum to the word length.
    drift = dur - sum(p["duration"] for p in tagged)
    tagged[-1]["duration"] = round(max(0.01, tagged[-1]["duration"] + drift), 3)
    return tagged


def align_files(audio_path: Path, transcript: str) -> dict:
    words = tokenize_transcript(transcript)
    if not words:
        raise RuntimeError("Transcript is empty.")
    y, sr = load_wav(audio_path)
    spans, duration = _voiced_spans(y, sr)
    engine = "energy"
    times = None
    whispered = _whisper_words(audio_path)
    if whispered:
        times = _align_with_whisper(words, whispered, duration)
        if times:
            engine = "whisper"
    if not times:
        times = _map_words_to_spans(words, spans)
        engine = "energy"
    offset = 0
    gentle_words = []
    for token, (start, end) in zip(words, times):
        core = orthographic_word(token) or token
        # Prefer locating the full whitespace token (with punct) for offsets; fall back to core.
        start_off = transcript.find(token, offset)
        if start_off < 0:
            start_off = transcript.find(core, offset)
        if start_off < 0:
            start_off = offset
        end_off = start_off + (len(token) if transcript.startswith(token, start_off) else len(core))
        offset = max(offset, end_off)
        gentle_words.append(
            {
                "alignedWord": core,
                "case": "success",
                "start": round(float(start), 3),
                "end": round(float(end), 3),
                "startOffset": start_off,
                "endOffset": end_off,
                "word": core,
                "phones": _phones_for_word(core, start, end),
            }
        )
    return {
        "transcript": transcript,
        "words": gentle_words,
        "backend": "local",
        "engine": engine,
    }


async def _read_upload(item) -> bytes:
    if item is None:
        return b""
    read = getattr(item, "read", None)
    if read is None:
        return bytes(item)
    data = read()
    if hasattr(data, "__await__"):
        data = await data
    return data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")


def create_app():
    app = FastAPI(title="Bubble Pod local Gentle")

    @app.get("/")
    def root():
        return {
            "ok": True,
            "backend": "local",
            "name": "lazykh-gentle-local",
            "engine": (os.environ.get("LAZYKH_GENTLE_ENGINE") or "auto"),
            "whisper_error": _WHISPER_ERROR,
            "detail": "Local Gentle-compatible aligner (no Docker).",
        }

    @app.post("/transcriptions")
    async def transcriptions(request: Request):
        form = await request.form()
        audio = form.get("audio")
        transcript = form.get("transcript")
        if audio is None or transcript is None:
            return JSONResponse(
                {"ok": False, "detail": "multipart fields 'audio' and 'transcript' are required."},
                status_code=400,
            )
        filename = getattr(audio, "filename", None) or "audio.wav"
        suffix = Path(str(filename)).suffix or ".wav"
        text = (await _read_upload(transcript)).decode("utf-8", errors="replace")
        data = await _read_upload(audio)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            tmp.write(data)
            tmp.close()
            payload = align_files(Path(tmp.name), text)
            return JSONResponse(payload)
        except Exception as exc:
            traceback.print_exc()
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Local Gentle-compatible aligner")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
