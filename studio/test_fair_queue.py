"""Phase 3: fair queue, per-user caps, owner priority."""

from __future__ import annotations

import unittest
from unittest import mock


class FairQueueTests(unittest.TestCase):
    def test_owner_priority_before_members(self):
        from studio import job_queue as jq

        jobs = [
            {"id": "1", "project_id": "m1", "owner_id": "memberA", "status": "queued", "enqueued_at": "2026-01-01T00:00:00+00:00"},
            {"id": "2", "project_id": "a1", "owner_id": "adminX", "status": "queued", "enqueued_at": "2026-01-01T00:01:00+00:00"},
            {"id": "3", "project_id": "m2", "owner_id": "memberB", "status": "queued", "enqueued_at": "2026-01-01T00:02:00+00:00"},
        ]

        def _prio(oid):
            return oid == "adminX"

        with mock.patch.object(jq, "_is_priority_owner", side_effect=_prio):
            with mock.patch.object(jq, "owner_priority_enabled", return_value=True):
                ordered = jq._queue_order(jobs)
        self.assertEqual(ordered[0]["project_id"], "a1")
        # Members still round-robin after
        self.assertEqual({ordered[1]["owner_id"], ordered[2]["owner_id"]}, {"memberA", "memberB"})

    def test_per_user_cap_blocks_second_job(self):
        from studio import job_queue as jq

        jobs = [
            {"id": "1", "project_id": "p1", "owner_id": "u1", "status": "running"},
            {"id": "2", "project_id": "p2", "owner_id": "u1", "status": "queued", "enqueued_at": "2026-01-01T00:00:00+00:00"},
            {"id": "3", "project_id": "p3", "owner_id": "u2", "status": "queued", "enqueued_at": "2026-01-01T00:01:00+00:00"},
        ]
        with mock.patch.object(jq, "_live_running_ids", return_value={"p1"}):
            with mock.patch.object(jq, "per_user_concurrency", return_value=1):
                with mock.patch.object(jq, "admin_concurrency", return_value=1):
                    with mock.patch.object(jq, "_is_priority_owner", return_value=False):
                        with mock.patch.object(jq, "user_quota_blocks_start", return_value=None):
                            selected = jq.select_runnable(jobs, limit=2)
        pids = [j["project_id"] for j in selected]
        self.assertEqual(pids, ["p3"])  # u1 already at cap; u2 runs

    def test_render_workers_env_alias(self):
        from studio import job_queue as jq
        import os

        old = os.environ.get("BUBBLEPOD_RENDER_WORKERS")
        try:
            os.environ["BUBBLEPOD_RENDER_WORKERS"] = "2"
            self.assertEqual(jq._env_max_concurrent(), 2)
        finally:
            if old is None:
                os.environ.pop("BUBBLEPOD_RENDER_WORKERS", None)
            else:
                os.environ["BUBBLEPOD_RENDER_WORKERS"] = old


if __name__ == "__main__":
    unittest.main()
