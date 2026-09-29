"""Client video-order pricing, storage, delivery, and MCP catalog."""

from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _mp4(path: Path) -> None:
    path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16)


def _paid_session(session_id: str, *, package: str, length: str, amount: int, email: str, name: str = "Ada") -> dict:
    return {
        "id": session_id,
        "payment_status": "paid",
        "amount_total": amount,
        "customer_email": email,
        "customer_details": {"email": email, "name": name},
        "custom_fields": [{"key": "customername", "text": {"value": name}}],
        "metadata": {
            "type": "video_order",
            "package_key": package,
            "video_length": length,
            "format": "16:9",
            "niche": "History",
            "custom_niche": "",
            "amount_cents": str(amount),
        },
    }


class OrderPricingTests(unittest.TestCase):
    def test_price_table(self):
        from studio.orders import price_cents

        self.assertEqual(price_cents("starter", "5"), 29700)
        self.assertEqual(price_cents("starter", "10"), 44600)
        self.assertEqual(price_cents("growth", "5"), 54700)
        self.assertEqual(price_cents("growth", "10"), 82100)
        self.assertEqual(price_cents("scale", "5"), 99700)
        self.assertEqual(price_cents("scale", "10"), 149600)


class OrderFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        from studio import orders

        self.orders = orders
        self.patches = [
            mock.patch.object(orders, "ORDERS_DB", root / "orders.db"),
            mock.patch.object(orders, "ORDER_FILES_DIR", root / "files"),
        ]
        for patch in self.patches:
            patch.start()
        orders.init_db()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def test_fulfill_is_idempotent_and_creates_video_rows(self):
        orders = self.orders
        session = _paid_session("cs_test_starter", package="starter", length="5", amount=29700, email="Buyer@Example.com")
        first = orders.fulfill_checkout_session(session)
        second = orders.fulfill_checkout_session(session)
        self.assertEqual(first["video_count"], 5)
        self.assertEqual(second["video_count"], 5)
        self.assertEqual(first["order_id"], second["order_id"])
        listed = orders.list_production_orders()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(len(listed["orders"][0]["videos"]), 5)
        self.assertEqual(listed["orders"][0]["email"], "Buyer@Example.com")
        self.assertEqual(listed["orders"][0]["name"], "Ada")
        self.assertEqual(listed["unreviewed_count"], 1)
        mine = orders.list_orders_for_email("buyer@example.com")
        self.assertEqual(len(mine["orders"]), 1)
        self.assertEqual(orders.list_orders_for_email("other@example.com")["orders"], [])

    def test_amount_mismatch_does_not_create_an_order(self):
        session = _paid_session("cs_test_bad", package="growth", length="10", amount=82000, email="a@b.co")
        with self.assertRaises(ValueError):
            self.orders.fulfill_checkout_session(session)
        self.assertEqual(self.orders.list_production_orders()["count"], 0)

    def test_checkout_uses_server_price_and_payment_mode(self):
        captured = {}

        class _Sessions:
            @staticmethod
            def create(params):
                captured["params"] = params
                return {"id": "cs_test_checkout", "url": "https://checkout.stripe.test/c"}

        class _Checkout:
            sessions = _Sessions()

        class _V1:
            checkout = _Checkout()

        class _Client:
            v1 = _V1()

        with mock.patch.object(self.orders, "stripe_status", return_value={"configured": True, "mode": "test"}), mock.patch(
            "studio.stripe_billing.get_stripe_client", return_value=_Client()
        ), mock.patch("studio.settings.resolve_public_base_url", return_value="http://127.0.0.1:7878"):
            result = self.orders.create_checkout(
                {
                    "niche": "Custom niche",
                    "custom_niche": "Local history",
                    "channel_notes": "Quiet tone",
                    "package": "starter",
                    "video_length": "10",
                    "format": "both",
                }
            )
        self.assertEqual(result["amount_cents"], 44600)
        params = captured["params"]
        self.assertEqual(params["mode"], "payment")
        self.assertEqual(params["line_items"][0]["price_data"]["unit_amount"], 44600)
        self.assertEqual(params["metadata"]["type"], "video_order")
        self.assertNotIn("subscription", params)
        self.assertIn("/order/success", params["success_url"])
        self.assertIn("/order/cancel", params["cancel_url"])

    def test_missing_stripe_key_is_an_error(self):
        with mock.patch.object(self.orders, "stripe_status", return_value={"configured": False, "mode": "missing"}):
            with self.assertRaises(RuntimeError) as ctx:
                self.orders.create_checkout(
                    {"niche": "History", "package": "starter", "video_length": "5", "format": "16:9"}
                )
        self.assertIn("Stripe is not configured", str(ctx.exception))

    def _make_ready(self, order_id: str):
        orders = self.orders
        order = orders.get_production_order(order_id)["order"]
        src = Path(self.tmp.name) / "clip.mp4"
        _mp4(src)
        for video in order["videos"]:
            orders.attach_video_mp4(order_id, video["id"], str(src))
            orders.mark_video_ready(order_id, video["id"])
        return orders.get_production_order(order_id)["order"]

    def test_production_progress_and_delivery(self):
        orders = self.orders
        session = _paid_session("cs_test_grow", package="growth", length="10", amount=82100, email="client@example.com", name="Eli")
        created = orders.fulfill_checkout_session(session)
        order_id = created["order_id"]
        self.assertEqual(created["video_count"], 10)
        video_id = created["order"]["videos"][0]["id"]
        moved = orders.update_order_video(order_id, video_id, topic="The vault", status="scripting")
        self.assertEqual(moved["order"]["status"], "in_production")
        self.assertEqual(moved["video"]["topic"], "The vault")
        with self.assertRaises(ValueError):
            orders.deliver_order(order_id)
        with self.assertRaises(ValueError):
            orders.attach_video_mp4(order_id, video_id, "https://example.com/video.mp4")
        order = self._make_ready(order_id)
        self.assertTrue(order["can_deliver"])
        with mock.patch("studio.email.email_enabled", return_value=False):
            delivered = orders.deliver_order(order_id)
        self.assertEqual(delivered["order"]["status"], "delivered")
        self.assertFalse(delivered["email_sent"])
        self.assertEqual(delivered["delivery_channel"], "in_app")
        self.assertIn("in the app", delivered["delivery_note"].lower())
        self.assertTrue(delivered["delivered_at"])
        mine = orders.list_orders_for_email("client@example.com")["orders"][0]
        self.assertEqual(mine["status"], "delivered")
        self.assertTrue(all(video["download_url"] for video in mine["videos"]))
        path, _name = orders.resolve_download(video_id, "client@example.com")
        self.assertTrue(path.is_file())
        with self.assertRaises(LookupError):
            orders.resolve_download(video_id, "someone-else@example.com")

    def test_email_failure_still_delivers_in_app(self):
        orders = self.orders
        session = _paid_session("cs_test_mail", package="starter", length="5", amount=29700, email="client@example.com")
        order_id = orders.fulfill_checkout_session(session)["order_id"]
        self._make_ready(order_id)
        with mock.patch("studio.email.email_enabled", return_value=True), mock.patch(
            "studio.email.send_email", side_effect=RuntimeError("smtp down")
        ):
            delivered = orders.update_order_status(order_id, "delivered")
        self.assertEqual(delivered["order"]["status"], "delivered")
        self.assertFalse(delivered["email_sent"])
        self.assertIn("smtp down", delivered["delivery_email_error"])
        self.assertIn("in the app", delivered["delivery_note"].lower())

    def test_email_success_reports_sent(self):
        orders = self.orders
        session = _paid_session("cs_test_sent", package="starter", length="5", amount=29700, email="client@example.com")
        order_id = orders.fulfill_checkout_session(session)["order_id"]
        self._make_ready(order_id)
        with mock.patch("studio.email.email_enabled", return_value=True), mock.patch(
            "studio.email.send_email", return_value={"ok": True}
        ) as send, mock.patch("studio.settings.resolve_public_base_url", return_value="http://127.0.0.1:7878"):
            delivered = orders.deliver_order(order_id)
        self.assertTrue(delivered["email_sent"])
        self.assertEqual(delivered["delivery_channel"], "email")
        text = send.call_args.kwargs["text"]
        self.assertIn("/my-orders?email=", text)
        self.assertIn("/api/orders/download/", text)
        again = orders.deliver_order(order_id)
        self.assertTrue(again["already_delivered"])
        self.assertEqual(send.call_count, 1)

    def test_webhook_routes_video_orders_and_leaves_membership(self):
        from studio.stripe_billing import _dispatch_event

        session = _paid_session("cs_test_hook", package="scale", length="5", amount=99700, email="scale@example.com")
        with mock.patch("studio.members.update_user") as update_user:
            handled = _dispatch_event("checkout.session.completed", session)
        self.assertTrue(handled)
        update_user.assert_not_called()
        self.assertEqual(self.orders.list_production_orders()["orders"][0]["video_count"], 20)
        with mock.patch("studio.orders.fulfill_checkout_session") as fulfill:
            handled = _dispatch_event(
                "checkout.session.completed",
                {"id": "cs_sub", "mode": "subscription", "metadata": {}, "customer": "", "subscription": None},
            )
        self.assertTrue(handled)
        fulfill.assert_not_called()


class McpOrderCatalogTests(unittest.TestCase):
    def test_tools_are_decorated_and_listed(self):
        from studio.mcp_server import MCP_TOOL_NAMES

        expected = {
            "list_video_orders",
            "get_video_order",
            "update_video_order_status",
            "set_order_video",
            "attach_order_video_mp4",
            "mark_order_video_ready",
            "deliver_video_order",
        }
        self.assertTrue(expected <= set(MCP_TOOL_NAMES))
        tree = ast.parse((Path(__file__).resolve().parent / "mcp_server.py").read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in expected:
                continue
            for deco in node.decorator_list:
                fn = deco.func if isinstance(deco, ast.Call) else deco
                if isinstance(fn, ast.Attribute) and fn.attr == "tool":
                    found.add(node.name)
        self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
