"""Phase 4 usage ledger + plan quotas."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class UsageLedgerTests(unittest.TestCase):
    def test_record_and_sum_idempotent(self):
        from studio import usage

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage_ledger.json"
            with mock.patch.object(usage, "USAGE_LEDGER_PATH", path):
                usage.record_usage("u1", "render_minutes", 10, idempotency_key="k1")
                usage.record_usage("u1", "render_minutes", 10, idempotency_key="k1")
                self.assertEqual(usage.sum_usage("u1", "render_minutes"), 10.0)
                usage.record_usage("u1", "render_minutes", 5, idempotency_key="k2")
                self.assertEqual(usage.sum_usage("u1", "render_minutes"), 15.0)


class QuotaTests(unittest.TestCase):
    def test_admin_never_blocked(self):
        from studio.usage import quota_block_reason

        self.assertIsNone(quota_block_reason({"id": "a", "role": "admin", "subscription_status": "active"}))

    def test_exhausted_render_minutes(self):
        from studio import usage
        from studio.plans import DEFAULT_PLAN_TIERS

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage_ledger.json"
            with mock.patch.object(usage, "USAGE_LEDGER_PATH", path):
                user = {
                    "id": "m1",
                    "role": "member",
                    "plan_tier": "starter",
                    "subscription_status": "active",
                }
                cap = float(DEFAULT_PLAN_TIERS["starter"]["render_minutes"])
                usage.record_usage("m1", "render_minutes", cap)
                self.assertEqual(usage.quota_block_reason(user), "quota_exhausted")


class PlanMapTests(unittest.TestCase):
    def test_normalize(self):
        from studio.plans import normalize_plan_tier

        self.assertEqual(normalize_plan_tier(None, is_admin=True), "admin")
        self.assertEqual(normalize_plan_tier("creator"), "creator")
        self.assertEqual(normalize_plan_tier("pro"), "creator")


if __name__ == "__main__":
    unittest.main()
