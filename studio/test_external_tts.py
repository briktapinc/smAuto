"""Smoke tests for tts_provider=external (no Chatterbox fallback)."""

from __future__ import annotations

import base64
import subprocess
import tempfile
import unittest
from pathlib import Path


class ExternalTtsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from studio.projects import create_project, project_dir, write_scripts
        from studio.settings import normalize_tts_provider
        from studio.utils_script import raw_from_tagged

        cls.normalize = normalize_tts_provider
        assert normalize_tts_provider("external") == "external"
        assert normalize_tts_provider("local") == "local"
        meta = create_project("External TTS smoke", 60, title="External TTS smoke")
        cls.pid = meta["id"]
        tagged = "<explain> Hello world for external audio smoke test.\n"
        write_scripts(cls.pid, tagged, raw_from_tagged(tagged))
        from studio.projects import set_project_voice

        # per-project override — does not touch global settings
        set_project_voice(cls.pid, provider="external")

    def test_missing_fails_fast(self):
        from studio.tts_external import EXTERNAL_AUDIO_MISSING, require_external_mp3

        with self.assertRaises(RuntimeError) as ctx:
            require_external_mp3(self.pid)
        self.assertIn(EXTERNAL_AUDIO_MISSING, str(ctx.exception))

    def test_upload_and_materialize(self):
        from studio.ffmpeg_bin import ffmpeg_cmd
        from studio.projects import input_prefix
        from studio.tts_external import materialize_external_wav, upload_narration_audio

        tmp = Path(tempfile.mkdtemp(prefix="ext-tts-"))
        mp3 = tmp / "n.mp3"
        # 1s sine tone as mp3
        subprocess.check_call(
            ffmpeg_cmd(
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(mp3),
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        data = mp3.read_bytes()
        out = upload_narration_audio(self.pid, audio=base64.b64encode(data).decode("ascii"))
        self.assertTrue(out["ok"])
        self.assertGreater(out["bytes"], 100)
        self.assertGreater(out.get("duration_seconds") or 0, 0.2)
        dest = Path(input_prefix(self.pid)).with_suffix(".wav")
        mat = materialize_external_wav(self.pid, dest)
        self.assertTrue(dest.is_file())
        self.assertEqual(mat["audio_source"], "external")


if __name__ == "__main__":
    unittest.main()
