"""Buyer accounts land on the order page and cannot call Studio APIs."""

from __future__ import annotations

import unittest
from unittest import mock

from studio.auth import buyer_api_denial, buyer_shell_redirect
from studio.members import (
    ROLES,
    SIGNUP_ROLE,
    is_buyer,
    login_landing_path,
    role_label,
    user_has_access,
)


class BuyerIdentityTests(unittest.TestCase):
    def test_buyer_role_is_recognized_and_labeled(self):
        buyer = {"id": "b1", "role": "buyer", "username": "buyer"}
        member = {"id": "m1", "role": "member", "username": "member"}
        self.assertIn("buyer", ROLES)
        self.assertTrue(is_buyer(buyer))
        self.assertFalse(is_buyer(member))
        self.assertFalse(is_buyer({"role": "admin"}))
        self.assertEqual(role_label("buyer"), "Buyer")

    def test_public_signup_stays_a_member(self):
        self.assertEqual(SIGNUP_ROLE, "member")
        self.assertNotEqual(SIGNUP_ROLE, "buyer")

    def test_buyer_has_no_studio_access_even_if_marked_active(self):
        buyer = {"role": "buyer", "subscription_status": "active", "disabled": False}
        member = {"role": "member", "subscription_status": "active", "disabled": False}
        self.assertFalse(user_has_access(buyer))
        self.assertTrue(user_has_access(member))


class BuyerRedirectTests(unittest.TestCase):
    def test_login_and_home_send_buyer_to_order_page(self):
        buyer = {"role": "buyer"}
        self.assertEqual(login_landing_path(buyer), "/order")
        self.assertEqual(buyer_shell_redirect(buyer, "/"), "/order")
        self.assertEqual(buyer_shell_redirect(buyer, "/app"), "/order")
        self.assertEqual(buyer_shell_redirect(buyer, "/app/"), "/order")

    def test_member_and_admin_are_not_redirected(self):
        member = {"role": "member"}
        admin = {"role": "admin"}
        self.assertEqual(login_landing_path(member), "/")
        self.assertEqual(login_landing_path(admin), "/")
        self.assertIsNone(buyer_shell_redirect(member, "/"))
        self.assertIsNone(buyer_shell_redirect(member, "/app"))
        self.assertIsNone(buyer_shell_redirect(admin, "/"))
        self.assertIsNone(buyer_shell_redirect(admin, "/app"))

    def test_order_pages_stay_put(self):
        buyer = {"role": "buyer"}
        self.assertIsNone(buyer_shell_redirect(buyer, "/order"))
        self.assertIsNone(buyer_shell_redirect(buyer, "/my-orders"))

    def test_desktop_mode_does_not_redirect(self):
        buyer = {"role": "buyer"}
        with mock.patch("studio.auth.is_desktop_mode", return_value=True):
            self.assertIsNone(buyer_shell_redirect(buyer, "/"))
            self.assertIsNone(buyer_shell_redirect(buyer, "/app"))


class BuyerApiGateTests(unittest.TestCase):
    def test_buyer_studio_job_topic_and_render_are_denied(self):
        buyer = {"role": "buyer"}
        for path in (
            "/api/projects",
            "/api/projects/job1/render",
            "/api/jobs/job1/start",
            "/api/topics",
            "/api/topics/generate",
            "/api/settings",
            "/api/production/orders",
            "/mcp",
        ):
            self.assertEqual(buyer_api_denial(buyer, path), "Buyers cannot use Studio")

    def test_buyer_can_identify_themselves_and_use_orders(self):
        buyer = {"role": "buyer"}
        for path in (
            "/api/auth/me",
            "/api/auth/logout",
            "/api/orders/checkout",
            "/api/orders/config",
            "/api/my-orders",
            "/api/orders/download/vid_1",
            "/api/orders/by-session/cs_test",
        ):
            self.assertIsNone(buyer_api_denial(buyer, path))

    def test_member_is_not_denied_studio_apis(self):
        member = {"role": "member"}
        self.assertIsNone(buyer_api_denial(member, "/api/projects"))
        self.assertIsNone(buyer_api_denial(member, "/api/topics"))
        self.assertIsNone(buyer_api_denial(member, "/api/projects/job1/render"))

    def test_desktop_mode_does_not_deny_api(self):
        buyer = {"role": "buyer"}
        with mock.patch("studio.auth.is_desktop_mode", return_value=True):
            self.assertIsNone(buyer_api_denial(buyer, "/api/projects"))


if __name__ == "__main__":
    unittest.main()
