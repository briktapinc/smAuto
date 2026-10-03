"""Renewal reminders and the Stripe renew action. No network, no member store writes."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock


def _member(**extra):
    end = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    base = {
        "id": "member-1",
        "role": "member",
        "email": "member@example.com",
        "disabled": False,
        "subscription_status": "active",
        "cancel_at_period_end": True,
        "current_period_end": end.isoformat(),
        "renewal_reminder_for": "",
    }
    base.update(extra)
    return base


class RenewalReminderDueTests(unittest.TestCase):
    def test_due_inside_three_day_window(self):
        from studio.email import renewal_reminder_due

        user = _member()
        now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(renewal_reminder_due(user, now=now), user["current_period_end"])

    def test_not_due_when_auto_renewing_or_too_early(self):
        from studio.email import renewal_reminder_due

        now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(renewal_reminder_due(_member(cancel_at_period_end=False), now=now))
        early = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(renewal_reminder_due(_member(), now=early))

    def test_one_reminder_per_period_end(self):
        from studio.email import renewal_reminder_due

        user = _member()
        now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        key = renewal_reminder_due(user, now=now)
        self.assertIsNone(renewal_reminder_due(_member(renewal_reminder_for=key), now=now))

    def test_skips_admin_and_missing_email(self):
        from studio.email import renewal_reminder_due

        now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(renewal_reminder_due(_member(role="admin"), now=now))
        self.assertIsNone(renewal_reminder_due(_member(email=""), now=now))

    def test_send_records_the_period_once(self):
        from studio.email import send_due_renewal_reminders

        user = _member()
        now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        with mock.patch("studio.email.email_enabled", return_value=True), mock.patch(
            "studio.members.list_user_records", return_value=[user]
        ), mock.patch("studio.members.update_user") as update, mock.patch(
            "studio.email.notify_subscription_expiring", return_value={"ok": True}
        ) as notify:
            result = send_due_renewal_reminders(now=now)
        self.assertEqual(result["sent"], 1)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["period_end"], "October 4, 2026")
        update.assert_called_once_with("member-1", renewal_reminder_for=user["current_period_end"])

    def test_skips_when_email_disabled(self):
        from studio.email import send_due_renewal_reminders

        with mock.patch("studio.email.email_enabled", return_value=False), mock.patch(
            "studio.members.list_user_records"
        ) as listed:
            result = send_due_renewal_reminders(now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertEqual(result["sent"], 0)
        listed.assert_not_called()


class RenewSubscriptionTests(unittest.TestCase):
    def test_pending_cancel_resumes_the_stripe_subscription(self):
        from studio.stripe_billing import renew_subscription

        user = _member()
        with mock.patch(
            "studio.stripe_billing.reactivate_subscription",
            return_value={"ok": True, "message": "resumed"},
        ) as resume:
            result = renew_subscription(user)
        resume.assert_called_once_with(user)
        self.assertEqual(result["action"], "resumed")
        self.assertTrue(result["message"])

    def test_canceled_opens_checkout(self):
        from studio.stripe_billing import renew_subscription

        user = _member(subscription_status="canceled", cancel_at_period_end=False)
        with mock.patch(
            "studio.stripe_billing.create_checkout_session",
            return_value={"ok": True, "url": "https://checkout.stripe.test/session"},
        ) as checkout:
            result = renew_subscription(user)
        checkout.assert_called_once_with(user)
        self.assertEqual(result["action"], "checkout")
        self.assertIn("stripe.test", result["url"])

    def test_past_due_opens_portal(self):
        from studio.stripe_billing import renew_subscription

        user = _member(subscription_status="past_due", cancel_at_period_end=False)
        with mock.patch(
            "studio.stripe_billing.create_portal_session",
            return_value={"ok": True, "url": "https://billing.stripe.test/portal"},
        ) as portal:
            result = renew_subscription(user)
        portal.assert_called_once_with(user)
        self.assertEqual(result["action"], "portal")


if __name__ == "__main__":
    unittest.main()
