"""Subprocess entry for local TTS (Chatterbox / Piper).

Spawned by studio.tts_local.write_local_wav so a native torch crash cannot
take down the Studio API process. GPU lock is held by the parent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    # Prevent nested subprocess if worker code ever calls write_local_wav.
    os.environ["BUBBLEPOD_TTS_WORKER"] = "1"

    parser = argparse.ArgumentParser(description="Stickman Automation local TTS worker (isolated process).")
    parser.add_argument("--text-file", required=True, help="UTF-8 script text path")
    parser.add_argument("--dest", required=True, help="Output WAV path")
    parser.add_argument("--result-file", required=True, help="JSON result path written on success or soft failure")
    parser.add_argument("--voice-id", default="", help="Optional local voice id / prompt stem")
    args = parser.parse_args(argv)

    text_path = Path(args.text_file)
    dest = Path(args.dest)
    result_path = Path(args.result_file)
    voice_id = (args.voice_id or "").strip() or None

    def _write_result(payload: dict) -> None:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    try:
        text = text_path.read_text(encoding="utf-8")
    except Exception as exc:
        _write_result({"ok": False, "error": f"Could not read text file: {exc}"})
        print(f"tts_local_worker: could not read text file: {exc}", file=sys.stderr)
        return 2

    try:
        # Import after env flag so write_local_wav stays in-process here.
        from studio.tts_local import _write_local_wav_unlocked

        info = _write_local_wav_unlocked(text, dest, voice_id)
        payload = {"ok": True, **info}
        _write_result(payload)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        err = str(exc) or repr(exc)
        _write_result({"ok": False, "error": err})
        print(f"tts_local_worker failed: {err}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
