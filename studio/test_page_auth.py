"""Anonymous visitors must not receive private HTML pages."""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ["BUBBLEPOD_DESKTOP"] = "0"
os.environ.pop("LAZYKH_DESKTOP", None)
os.environ["BUBBLEPOD_ROOT_PATH"] = "/app"

_HTML = {"Accept": "text/html"}
_JSON = {"Accept": "application/json"}


class PrivateHtmlGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patches = [
            mock.patch(
                "studio.auth.ensure_mcp_pin",
                return_value={"mcp_pin_set": True, "generated": False, "pin": None},
            ),
            mock.patch(
                "studio.tenant.migrate_orphans_to_admin",
                return_value={"projects": 0, "topics": 0},
            ),
            mock.patch(
                "studio.mcp_install.ensure_claude_mcp_config",
                return_value={"ok": True},
            ),
            mock.patch("studio.settings.save_settings", side_effect=AssertionError("settings must not be written")),
        ]
        for patch in cls._patches:
            patch.start()
        from starlette.testclient import TestClient

        from studio.web import create_app

        cls.client = TestClient(create_app(), follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        for patch in cls._patches:
            patch.stop()

    def test_anonymous_my_orders_is_blocked_and_session_is_allowed(self):
        for path in ("/my-orders", "/app/my-orders"):
            api = self.client.get(path, headers=_JSON)
            self.assertEqual(api.status_code, 401, path)
            self.assertEqual(api.json().get("detail"), "Not authenticated")
            self.assertNotIn(b"My orders", api.content)

            page = self.client.get(path, headers=_HTML)
            self.assertEqual(page.status_code, 303, path)
            self.assertIn("next=/my-orders", page.headers.get("location", ""))
            self.assertNotIn(b"data-page=\"my-orders\"", page.content)
            self.assertNotIn(b"My orders", page.content)

        buyer = {"id": "buyer-test", "username": "buyer-test", "role": "buyer", "disabled": False}
        with mock.patch("studio.auth.require_session", return_value=buyer):
            for path in ("/my-orders", "/app/my-orders"):
                allowed = self.client.get(path, headers=_HTML)
                self.assertEqual(allowed.status_code, 200, path)
                self.assertIn('data-page="my-orders"', allowed.text)

    def test_other_private_pages_redirect_anonymous_browsers(self):
        cases = (
            ("/admin", "next=/admin", "Stickman Automation Admin"),
            ("/pricing", "next=/pricing", "Stickman Automation — Pricing"),
            ("/production", "next=/production", "Production queue"),
            ("/app/admin", "next=/admin", "Stickman Automation Admin"),
            ("/app/pricing", "next=/pricing", "Stickman Automation — Pricing"),
            ("/app/production", "next=/production", "Production queue"),
        )
        for path, location_bit, leaked in cases:
            page = self.client.get(path, headers=_HTML)
            self.assertEqual(page.status_code, 303, path)
            self.assertIn(location_bit, page.headers.get("location", ""))
            self.assertNotIn(leaked, page.text)
            api = self.client.get(path, headers=_JSON)
            self.assertEqual(api.status_code, 401, path)

    def test_public_order_pages_and_login_stay_open(self):
        order = self.client.get("/order", headers=_HTML)
        self.assertEqual(order.status_code, 200)
        self.assertIn('data-page="order"', order.text)

        prefixed = self.client.get("/app/order", headers=_HTML)
        self.assertEqual(prefixed.status_code, 200)
        self.assertIn('data-page="order"', prefixed.text)

        success = self.client.get("/order/success", headers=_HTML)
        self.assertEqual(success.status_code, 200)
        self.assertIn("Order confirmed", success.text)

        cancel = self.client.get("/order/cancel", headers=_HTML)
        self.assertEqual(cancel.status_code, 200)

        home = self.client.get("/", headers=_HTML)
        self.assertEqual(home.status_code, 200)
        self.assertIn("Sign in — Stickman Automation", home.text)
        self.assertNotIn("<title>Stickman Automation</title>", home.text)

        app_home = self.client.get("/app", headers=_HTML)
        self.assertEqual(app_home.status_code, 200)
        self.assertIn("Sign in — Stickman Automation", app_home.text)

    def test_signed_in_buyer_still_reaches_my_orders_not_admin(self):
        buyer = {"id": "buyer-test", "username": "buyer-test", "role": "buyer", "disabled": False}
        with mock.patch("studio.auth.require_session", return_value=buyer):
            orders = self.client.get("/my-orders", headers=_HTML)
            self.assertEqual(orders.status_code, 200)
            admin = self.client.get("/admin", headers=_HTML)
            self.assertEqual(admin.status_code, 303)
            self.assertIn("/order", admin.headers.get("location", ""))
            self.assertNotIn("Stickman Automation Admin", admin.text)


if __name__ == "__main__":
    unittest.main()
