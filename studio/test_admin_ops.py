"""Phase 5: admin ops, backup, API-key rate limits, narration caps."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class BackupTests(unittest.TestCase):
    def test_run_backup_copies_and_scrubs(self):
        from studio import backup

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "members.json").write_text('{"users":[]}\n', encoding="utf-8")
            (root / "settings.json").write_text(
                '{"fal_key":"secret","public_base_url":"https://x"}\n',
                encoding="utf-8",
            )
            with mock.patch.object(backup, "USER_DATA", root), mock.patch.object(
                backup, "BACKUP_DIR", root / "backups"
            ), mock.patch.object(backup, "ensure_dirs", lambda: None):
                result = backup.run_backup(keep=3)
            self.assertTrue(result["ok"])
            dest = Path(result["path"])
            self.assertTrue((dest / "members.json").is_file())
            scrubbed = (dest / "settings.json").read_text(encoding="utf-8")
            self.assertIn("***", scrubbed)
            self.assertNotIn("secret", scrubbed)


class NarrationCapTests(unittest.TestCase):
    def test_upload_too_large_message(self):
        from studio import tts_external

        with mock.patch.object(tts_external, "MAX_AUDIO_UPLOAD_BYTES", 100), mock.patch.object(
            tts_external, "load_meta", return_value={}
        ):
            with self.assertRaises(RuntimeError) as ctx:
                tts_external.upload_narration_audio(
                    "proj",
                    data=b"x" * 400,
                    format="mp3",
                )
            self.assertIn("upload_too_large", str(ctx.exception))
            self.assertIn("413", str(ctx.exception))


class RateLimitApiKeyTests(unittest.TestCase):
    def test_api_key_bucket_isolated(self):
        from studio import rate_limit

        rate_limit.reset_for_tests()

        class _URL:
            path = "/api/projects"

        class _Req:
            method = "GET"
            url = _URL()
            headers = {"Authorization": "Bearer bp_live_testkey"}
            state = type("S", (), {})()

            @staticmethod
            def client():
                return type("C", (), {"host": "1.2.3.4"})()

        fake_keys = {
            "keys": [
                {
                    "id": "k1",
                    "key_hash": "deadbeef",
                    "rate_limit": 2,
                    "revoked_at": None,
                }
            ]
        }

        with mock.patch("studio.api_keys.looks_like_api_key", return_value=True), mock.patch(
            "studio.api_keys.hash_api_key", return_value="deadbeef"
        ), mock.patch("studio.api_keys._load", return_value=fake_keys), mock.patch.object(
            rate_limit, "client_ip", return_value="9.9.9.9"
        ), mock.patch.object(
            rate_limit,
            "_cfg",
            return_value={
                "enabled": True,
                "login_limit": 8,
                "login_window": 900,
                "signup_limit": 5,
                "signup_window": 3600,
                "api_limit": 1000,
                "api_window": 60,
                "mcp_limit": 600,
                "mcp_window": 60,
                "static_limit": 600,
                "static_window": 60,
            },
        ):
            self.assertIsNone(rate_limit.enforce(_Req()))
            self.assertIsNone(rate_limit.enforce(_Req()))
            blocked = rate_limit.enforce(_Req())
            self.assertIsNotNone(blocked)
            self.assertEqual(blocked.status_code, 429)
            body = blocked.body.decode("utf-8") if hasattr(blocked, "body") else ""
            # Starlette JSONResponse stores content
            import json

            payload = json.loads(blocked.body.decode("utf-8"))
            self.assertEqual(payload.get("error_code"), "rate_limited")
            self.assertIn("API key", payload.get("detail", ""))


class FailedJobRetryTests(unittest.TestCase):
    def test_retry_requeues(self):
        from studio import admin_ops
        from studio import job_queue

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job_queue.json"
            store = {
                "jobs": [
                    {
                        "id": "qj1",
                        "project_id": "p1",
                        "status": job_queue.STATUS_FAILED,
                        "attempts": 5,
                        "max_attempts": 5,
                        "error": "boom",
                        "kind": "run",
                    }
                ]
            }
            path.write_text(__import__("json").dumps(store), encoding="utf-8")
            with mock.patch.object(job_queue, "QUEUE_PATH", path), mock.patch.object(
                job_queue, "QUEUE_MUTEX_PATH", Path(tmp) / "job_queue.lock"
            ), mock.patch.object(job_queue, "pump", lambda: None), mock.patch(
                "studio.audit.write_entry", return_value={}
            ):
                out = admin_ops.retry_failed_job("qj1", admin_username="owner")
            self.assertTrue(out["ok"])
            self.assertEqual(out["job"]["status"], job_queue.STATUS_QUEUED)
            self.assertEqual(out["job"]["kind"], "resume")


if __name__ == "__main__":
    unittest.main()
