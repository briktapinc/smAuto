"""Phase 2 job engine: leases, retries, frame resume helpers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TransientTests(unittest.TestCase):
    def test_transient_detection(self):
        from studio.job_queue import is_transient_failure

        self.assertTrue(is_transient_failure("videoDrawer.py failed (exit -15):"))
        self.assertTrue(is_transient_failure("Chatterbox could not load"))
        self.assertTrue(is_transient_failure("Gentle timed out"))
        self.assertFalse(is_transient_failure("Unknown project: foo"))


class FrameResumeTests(unittest.TestCase):
    def test_frames_resume_at(self):
        from studio.pipeline import _frames_resume_at

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.assertEqual(_frames_resume_at(folder), 0)
            (folder / "f000000.png").write_bytes(b"x")
            (folder / "f000001.png").write_bytes(b"x")
            (folder / "f000003.png").write_bytes(b"x")  # gap
            self.assertEqual(_frames_resume_at(folder), 2)


class LeaseReconcileTests(unittest.TestCase):
    def test_boot_requeues_orphaned_running(self):
        from studio import job_queue

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qpath = root / "job_queue.json"
            mpath = root / "job_queue.mutex"
            with mock.patch.object(job_queue, "QUEUE_PATH", qpath):
                with mock.patch.object(job_queue, "QUEUE_MUTEX_PATH", mpath):
                    with mock.patch.object(job_queue, "_live_running_ids", return_value=set()):
                        store = {
                            "version": 1,
                            "jobs": [
                                {
                                    "id": "abc",
                                    "project_id": "proj1",
                                    "status": job_queue.STATUS_RUNNING,
                                    "kind": "start",
                                    "attempts": 0,
                                    "max_attempts": 3,
                                    "lease_expires_at": None,
                                }
                            ],
                        }
                        out = job_queue._reconcile(store, boot=True)
                        job = out["jobs"][0]
                        self.assertEqual(job["status"], job_queue.STATUS_QUEUED)
                        self.assertEqual(job["kind"], "resume")


class RetryBackoffTests(unittest.TestCase):
    def test_retry_delay_grows(self):
        from studio.job_queue import _retry_delay_sec

        self.assertEqual(_retry_delay_sec(1), 5)
        self.assertEqual(_retry_delay_sec(2), 10)
        self.assertEqual(_retry_delay_sec(3), 20)
        self.assertEqual(_retry_delay_sec(10), 300)


if __name__ == "__main__":
    unittest.main()
