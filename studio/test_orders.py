"""Client video-order pricing, storage, delivery, and MCP catalog."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from datetime import datetime, timezone
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


def _default_pricing():
    from studio.orders import normalize_order_packages

    return normalize_order_packages(None)


class OrderPricingTests(unittest.TestCase):
    def test_price_table(self):
        from studio.orders import price_cents

        with mock.patch("studio.orders.configured_pricing", return_value=_default_pricing()):
            self.assertEqual(price_cents("starter", "5"), 4900)
            self.assertEqual(price_cents("starter", "10"), 7400)
            self.assertEqual(price_cents("growth", "5"), 8900)
            self.assertEqual(price_cents("growth", "10"), 13400)
            self.assertEqual(price_cents("scale", "5"), 19700)
            self.assertEqual(price_cents("scale", "10"), 29600)

    def test_admin_catalog_rejects_a_pace_that_does_not_match_the_video_count(self):
        from studio.orders import normalize_order_packages

        with self.assertRaises(ValueError) as ctx:
            normalize_order_packages({
                "starter": {"videos": 5, "cents": 4900, "days": 5, "per_day": 2},
            })
        self.assertIn("Starter", str(ctx.exception))

    def test_admin_catalog_accepts_a_dollar_price(self):
        from studio.orders import normalize_order_packages

        saved = normalize_order_packages(
            {"growth": {"price": "89.00", "videos": 10, "days": 5, "per_day": 2}},
            multiplier="1.5",
        )
        self.assertEqual(saved["packages"]["growth"]["cents"], 8900)
        self.assertEqual(saved["packages"]["starter"]["cents"], 4900)
        self.assertEqual(saved["ten_minute_multiplier"], 1.5)


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
        self.patches.append(
            mock.patch.object(orders, "configured_pricing", return_value=_default_pricing())
        )
        self.patches[-1].start()
        self.patches.append(
            mock.patch.object(
                orders,
                "configured_art_styles",
                return_value=[
                    {"id": "classic", "name": "Classic (hand-drawn 2D)", "thumbnail": ""},
                    {"id": "pixar_3d", "name": "3D Pixar / Disney", "thumbnail": ""},
                ],
            )
        )
        self.patches[-1].start()
        self.patches.append(mock.patch("studio.members.get_user_by_email", return_value=None))
        self.patches[-1].start()
        import studio.job_notifications as notices

        self.notices = notices
        with notices._cond:
            self._notice_state = (list(notices._events), notices._seq, notices._loaded)
            notices._events.clear()
            notices._seq = 0
            notices._loaded = True
        self.patches.append(mock.patch.object(notices, "_PATH", root / "notices.jsonl"))
        self.patches[-1].start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        events, seq, loaded = self._notice_state
        with self.notices._cond:
            self.notices._events.clear()
            self.notices._events.extend(events)
            self.notices._seq = seq
            self.notices._loaded = loaded
        self.tmp.cleanup()

    def test_fulfill_is_idempotent_and_creates_video_rows(self):
        orders = self.orders
        session = _paid_session("cs_test_starter", package="starter", length="5", amount=4900, email="Buyer@Example.com")
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
        mine = orders.list_orders_for_user({"id": "usr_buyer", "email": "buyer@example.com"})
        self.assertEqual(len(mine["orders"]), 1)
        self.assertEqual(mine["user_id"], "usr_buyer")
        tied = orders.get_production_order(first["order_id"])["order"]
        self.assertEqual(tied["user_id"], "usr_buyer")
        self.assertEqual(
            orders.list_orders_for_user({"id": "usr_other", "email": "other@example.com"})["orders"],
            [],
        )

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
                    "art_style": "classic",
                }
            )
        self.assertEqual(result["amount_cents"], 7400)
        params = captured["params"]
        self.assertEqual(params["mode"], "payment")
        self.assertEqual(params["line_items"][0]["price_data"]["unit_amount"], 7400)
        self.assertEqual(params["metadata"]["type"], "video_order")
        self.assertEqual(params["metadata"]["video_length"], "10")
        self.assertEqual(params["metadata"]["format"], "both")
        self.assertEqual(params["metadata"]["art_style"], "classic")
        self.assertNotIn("subscription", params)
        self.assertIn("/order/success", params["success_url"])
        self.assertIn("/order/cancel", params["cancel_url"])

    def test_missing_stripe_key_is_an_error(self):
        with mock.patch.object(self.orders, "stripe_status", return_value={"configured": False, "mode": "missing"}):
            with self.assertRaises(RuntimeError) as ctx:
                self.orders.create_checkout(
                    {"niche": "History", "package": "starter", "video_length": "5", "format": "16:9", "art_style": "classic"}
                )
        self.assertIn("Stripe is not configured", str(ctx.exception))

    def test_art_style_is_required_persisted_and_included_in_the_notice(self):
        orders = self.orders
        with self.assertRaises(ValueError) as missing:
            orders.create_checkout(
                {"niche": "History", "package": "starter", "video_length": "5", "format": "9:16"}
            )
        self.assertIn("art style", str(missing.exception).lower())

        captured = {}

        class _Sessions:
            @staticmethod
            def create(params):
                captured["params"] = params
                return {"id": "cs_test_style", "url": "https://checkout.stripe.test/style"}

        class _Checkout:
            sessions = _Sessions()

        class _V1:
            checkout = _Checkout()

        class _Client:
            v1 = _V1()

        with mock.patch.object(orders, "stripe_status", return_value={"configured": True, "mode": "test"}), mock.patch(
            "studio.stripe_billing.get_stripe_client", return_value=_Client()
        ), mock.patch("studio.settings.resolve_public_base_url", return_value="http://127.0.0.1:7878"):
            orders.create_checkout(
                {
                    "niche": "History",
                    "package": "starter",
                    "video_length": "5",
                    "format": "9:16",
                    "art_style": "pixar",
                },
                user_id="usr_buyer",
            )
        with orders._conn() as conn:
            row = conn.execute("SELECT * FROM orders WHERE stripe_session_id = ?", ("cs_test_style",)).fetchone()
        self.assertEqual(row["art_style"], "pixar_3d")
        self.assertEqual(row["art_style_name"], "3D Pixar / Disney")
        self.assertEqual(row["video_length"], "5")
        self.assertEqual(row["format"], "9:16")
        self.assertEqual(row["user_id"], "usr_buyer")
        self.assertEqual(captured["params"]["metadata"]["art_style"], "pixar_3d")
        self.assertEqual(captured["params"]["metadata"]["video_length"], "5")
        self.assertEqual(captured["params"]["metadata"]["format"], "9:16")

        session = _paid_session("cs_test_style", package="starter", length="5", amount=4900, email="Buyer@Example.com")
        paid = orders.fulfill_checkout_session(session)
        notice = paid["notice"]
        self.assertEqual(notice["art_style"], "pixar_3d")
        self.assertEqual(notice["art_style_name"], "3D Pixar / Disney")
        self.assertEqual(notice["video_length"], "5")
        self.assertEqual(notice["duration_min"], 5)
        self.assertEqual(notice["format"], "9:16")
        self.assertIn("art style", notice["detail"].lower())
        self.assertIn("NOT started", notice["detail"])
        stored = orders.get_production_order(paid["order_id"])["order"]
        self.assertEqual(stored["art_style"], "pixar_3d")
        self.assertEqual(stored["video_length"], "5")
        self.assertEqual(stored["format"], "9:16")
        self.assertEqual(stored["user_id"], "usr_buyer")

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
        session = _paid_session("cs_test_grow", package="growth", length="10", amount=13400, email="client@example.com", name="Eli")
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
        buyer = {"id": "usr_client", "email": "client@example.com"}
        mine = orders.list_orders_for_user(buyer)["orders"][0]
        self.assertEqual(mine["status"], "delivered")
        self.assertTrue(all(video["download_url"] for video in mine["videos"]))
        self.assertNotIn("email=", mine["videos"][0]["download_url"])
        path, _name = orders.resolve_download(video_id, buyer)
        self.assertTrue(path.is_file())
        with self.assertRaises(LookupError):
            orders.resolve_download(video_id, {"id": "usr_other", "email": "someone-else@example.com"})
        with self.assertRaises(LookupError):
            orders.resolve_download(video_id)

    def test_email_failure_still_delivers_in_app(self):
        orders = self.orders
        session = _paid_session("cs_test_mail", package="starter", length="5", amount=4900, email="client@example.com")
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
        session = _paid_session("cs_test_sent", package="starter", length="5", amount=4900, email="client@example.com")
        order_id = orders.fulfill_checkout_session(session)["order_id"]
        self._make_ready(order_id)
        with mock.patch("studio.email.email_enabled", return_value=True), mock.patch(
            "studio.email.send_email", return_value={"ok": True}
        ) as send, mock.patch("studio.settings.resolve_public_base_url", return_value="http://127.0.0.1:7878"):
            delivered = orders.deliver_order(order_id)
        self.assertTrue(delivered["email_sent"])
        self.assertEqual(delivered["delivery_channel"], "email")
        text = send.call_args.kwargs["text"]
        self.assertIn("/my-orders", text)
        self.assertNotIn("?email=", text)
        self.assertIn("/api/orders/download/", text)
        again = orders.deliver_order(order_id)
        self.assertTrue(again["already_delivered"])
        self.assertEqual(send.call_count, 1)

    def test_webhook_routes_video_orders_and_leaves_membership(self):
        from studio.stripe_billing import _dispatch_event

        session = _paid_session("cs_test_hook", package="scale", length="5", amount=19700, email="scale@example.com")
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


    def _member(self, user_id: str = "usr_buyer") -> dict:
        return {"id": user_id, "email": "buyer@example.com", "role": "member", "username": "buyer"}

    def _fake_schedule(self, calls: list):
        def fake(**kwargs):
            calls.append(kwargs)
            video_id = kwargs["order_video_id"]
            return {
                "already_queued": False,
                "topic": {
                    "id": "topic-" + video_id,
                    "order_id": kwargs["order_id"],
                    "order_video_id": video_id,
                    "owner_id": kwargs["owner_id"],
                },
                "job_id": "job-" + video_id,
                "scheduled_at": kwargs["scheduled_at"],
                "kicked": {"started": False, "reason": "scheduled"},
            }

        return fake

    def test_fulfill_publishes_one_approval_notice_and_does_not_start_generation(self):
        from studio.job_notifications import _matching, listen_job_notifications

        session = _paid_session("cs_test_notice", package="starter", length="5", amount=4900, email="Buyer@Example.com")
        calls = []
        with mock.patch("studio.topics.schedule_order_topic", side_effect=self._fake_schedule(calls)), mock.patch(
            "studio.email.send_email", side_effect=AssertionError("fulfill must not email")
        ):
            first = self.orders.fulfill_checkout_session(session)
            second = self.orders.fulfill_checkout_session(session)
        self.assertTrue(first["awaiting_approval"])
        self.assertFalse(second["awaiting_approval"])
        self.assertIsNone(second["notice"])
        notice = first["notice"]
        self.assertEqual(notice["type"], "order_awaiting_approval")
        self.assertEqual(notice["order_id"], first["order_id"])
        self.assertEqual(notice["package_name"], "Starter")
        self.assertEqual(notice["video_count"], 5)
        self.assertEqual(notice["niche"], "History")
        self.assertEqual(notice["email"], "Buyer@Example.com")
        self.assertEqual(notice["name"], "Ada")
        self.assertEqual(notice["format"], "16:9")
        self.assertEqual(notice["video_length"], "5")
        self.assertEqual(notice["days"], 5)
        self.assertEqual(notice["per_day"], 1)
        self.assertFalse(notice["generation_started"])
        self.assertIn("NOT started", notice["detail"])
        self.assertIn("approve_order_generation", notice["action"])
        heard = listen_job_notifications(timeout_sec=1)
        self.assertFalse(heard["timed_out"])
        self.assertEqual(heard["notifications"][0]["type"], "order_awaiting_approval")
        self.assertEqual(heard["notifications"][0]["order_id"], first["order_id"])
        self.assertEqual(_matching(0, "some-other-project"), [])
        self.assertEqual(_matching(0, "")[0]["type"], "order_awaiting_approval")
        self.assertEqual(calls, [])
        order = self.orders.get_production_order(first["order_id"])["order"]
        self.assertEqual(order["status"], "paid")
        self.assertFalse(order["generation_approved_at"])
        self.assertTrue(all(not video["topic_id"] for video in order["videos"]))
        with self.notices._cond:
            matching = [
                row
                for row in self.notices._events
                if row.get("type") == "order_awaiting_approval" and row.get("order_id") == first["order_id"]
            ]
        self.assertEqual(len(matching), 1)

    def test_webhook_publishes_the_approval_notice_once(self):
        from studio.stripe_billing import _dispatch_event

        session = _paid_session("cs_test_hook_notice", package="scale", length="5", amount=19700, email="scale@example.com")
        with mock.patch("studio.members.update_user") as update_user, mock.patch(
            "studio.topics.schedule_order_topic"
        ) as schedule:
            self.assertTrue(_dispatch_event("checkout.session.completed", session))
            self.assertTrue(_dispatch_event("checkout.session.completed", session))
        update_user.assert_not_called()
        schedule.assert_not_called()
        with self.notices._cond:
            notices = [row for row in self.notices._events if row.get("type") == "order_awaiting_approval"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["package_name"], "Scale")
        self.assertEqual(notices[0]["video_count"], 20)
        self.assertEqual(notices[0]["per_day"], 4)
        self.assertFalse(notices[0]["generation_started"])

    def test_approve_queues_topics_on_the_member_at_the_stored_pace(self):
        orders = self.orders
        session = _paid_session("cs_test_approve", package="growth", length="10", amount=13400, email="Buyer@Example.com")
        order_id = orders.fulfill_checkout_session(session)["order_id"]
        with orders._conn() as conn:
            conn.execute(
                "UPDATE orders SET art_style = ?, art_style_name = ? WHERE id = ?",
                ("classic", "Classic (hand-drawn 2D)", order_id),
            )
        catalog = orders.configured_pricing()
        catalog["packages"]["growth"]["per_day"] = 10
        calls = []
        when = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
        with mock.patch("studio.members.get_user_by_id", return_value=None), mock.patch(
            "studio.members.get_user_by_email", return_value=self._member()
        ) as by_email, mock.patch("studio.topics.schedule_order_topic", side_effect=self._fake_schedule(calls)):
            approved = orders.approve_order_generation(order_id, now=when)
            again = orders.approve_order_generation(order_id, now=when)
        by_email.assert_called()
        self.assertTrue(approved["ok"])
        self.assertFalse(approved["already_approved"])
        self.assertEqual(approved["user_id"], "usr_buyer")
        self.assertEqual(approved["per_day"], 2)
        self.assertEqual(approved["days"], 5)
        self.assertEqual(len(calls), 10)
        dates = [call["scheduled_at"][:10] for call in calls]
        self.assertEqual(
            dates,
            [
                "2026-10-01",
                "2026-10-01",
                "2026-10-02",
                "2026-10-02",
                "2026-10-03",
                "2026-10-03",
                "2026-10-04",
                "2026-10-04",
                "2026-10-05",
                "2026-10-05",
            ],
        )
        self.assertTrue(all(call["owner_id"] == "usr_buyer" for call in calls))
        self.assertTrue(all(call["order_id"] == order_id for call in calls))
        self.assertTrue(all(call["aspect"] == "16:9" for call in calls))
        self.assertTrue(all(call["duration_min"] == 10 for call in calls))
        self.assertTrue(all(call["art_style"] == "classic" for call in calls))
        self.assertEqual(approved["art_style"], "classic")
        self.assertEqual(approved["video_length"], "10")
        self.assertEqual(approved["format"], "16:9")
        self.assertEqual(approved["duration_min"], 10)
        self.assertEqual(again["art_style"], "classic")
        self.assertEqual(again["format"], "16:9")
        self.assertIn("History", calls[0]["angle"])
        self.assertEqual(calls[0]["title"], "History video 1")
        self.assertTrue(again["already_approved"])
        self.assertEqual(len(calls), 10)
        order = orders.get_production_order(order_id)["order"]
        self.assertEqual(order["status"], "queued")
        self.assertEqual(order["user_id"], "usr_buyer")
        self.assertTrue(order["generation_approved_at"])
        self.assertEqual(
            [video["topic_id"] for video in order["videos"]],
            ["topic-" + video["id"] for video in order["videos"]],
        )
        self.assertTrue(all(video["job_id"] for video in order["videos"]))

    def test_approve_uses_a_tied_user_and_rejects_an_unknown_email(self):
        orders = self.orders
        session = _paid_session("cs_test_member", package="starter", length="5", amount=4900, email="Buyer@Example.com")
        order_id = orders.fulfill_checkout_session(session)["order_id"]
        with orders._conn() as conn:
            conn.execute("UPDATE orders SET user_id = ? WHERE id = ?", ("usr_tied", order_id))
        calls = []

        def by_id(user_id):
            if user_id == "usr_tied":
                return {"id": "usr_tied", "email": "tied@example.com", "role": "member"}
            return None

        with mock.patch("studio.members.get_user_by_id", side_effect=by_id), mock.patch(
            "studio.members.get_user_by_email", side_effect=AssertionError("email lookup is only for untied orders")
        ), mock.patch("studio.topics.schedule_order_topic", side_effect=self._fake_schedule(calls)):
            approved = orders.approve_order_generation(order_id, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertTrue(approved["ok"])
        self.assertEqual(approved["user_id"], "usr_tied")
        self.assertEqual(calls[0]["owner_id"], "usr_tied")
        self.assertEqual([call["scheduled_at"][:10] for call in calls], [
            "2026-10-01",
            "2026-10-02",
            "2026-10-03",
            "2026-10-04",
            "2026-10-05",
        ])

        missing = _paid_session("cs_test_nomember", package="starter", length="5", amount=4900, email="nobody@example.com", name="")
        missing_id = orders.fulfill_checkout_session(missing)["order_id"]
        before = len(calls)
        with mock.patch("studio.members.get_user_by_id", return_value=None), mock.patch(
            "studio.members.get_user_by_email", return_value=None
        ), mock.patch("studio.topics.schedule_order_topic", side_effect=self._fake_schedule(calls)):
            rejected = orders.approve_order_generation(missing_id)
        self.assertFalse(rejected["ok"])
        self.assertIn("not a member", rejected["error"])
        self.assertIn("no user was created", rejected["error"])
        self.assertEqual(rejected["topics"], [])
        self.assertEqual(len(calls), before)
        untouched = orders.get_production_order(missing_id)["order"]
        self.assertEqual(untouched["status"], "paid")
        self.assertFalse(untouched["user_id"])
        self.assertTrue(all(not video["topic_id"] for video in untouched["videos"]))

    def test_missing_stored_pace_falls_back_to_the_live_catalog(self):
        orders = self.orders
        session = _paid_session("cs_test_pace", package="starter", length="5", amount=4900, email="Buyer@Example.com")
        order_id = orders.fulfill_checkout_session(session)["order_id"]
        with orders._conn() as conn:
            conn.execute("UPDATE orders SET days = NULL, per_day = NULL WHERE id = ?", (order_id,))
        calls = []
        with mock.patch("studio.members.get_user_by_id", return_value=None), mock.patch(
            "studio.members.get_user_by_email", return_value=self._member()
        ), mock.patch("studio.topics.schedule_order_topic", side_effect=self._fake_schedule(calls)):
            approved = orders.approve_order_generation(order_id, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(approved["per_day"], 1)
        self.assertEqual(approved["days"], 5)
        self.assertEqual(len({call["scheduled_at"][:10] for call in calls}), 5)


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
            "approve_order_generation",
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

    def test_listener_documents_the_order_approval_event(self):
        tree = ast.parse((Path(__file__).resolve().parent / "mcp_server.py").read_text(encoding="utf-8"))
        doc = ""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "listen_job_notifications":
                doc = ast.get_docstring(node) or ""
        self.assertIn("order_awaiting_approval", doc)
        self.assertIn("approve_order_generation", doc)
        self.assertIn("NOT started", doc)


class OrderTopicLinkTests(unittest.TestCase):
    def test_schedule_order_topic_marks_the_order_and_is_idempotent(self):
        from contextlib import nullcontext

        from studio import topics

        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "topics.json"
        try:
            with mock.patch.object(topics, "TOPICS_PATH", path), mock.patch.object(topics, "ensure_dirs"), mock.patch.object(
                topics, "_prepare_unsupervised_job", return_value=[]
            ), mock.patch.object(topics, "_topic_settings", return_value=nullcontext()), mock.patch.object(
                topics, "kick_queue"
            ) as kick, mock.patch.object(topics, "_ensure_job", side_effect=self._ensure):
                first = topics.schedule_order_topic(
                    owner_id="usr_1",
                    title="History video 1",
                    angle="Niche: History.",
                    duration_min=5,
                    scheduled_at="2099-12-01T15:00:00+00:00",
                    order_id="ord_1",
                    order_video_id="vid_1",
                    aspect="9:16",
                    art_style="anime",
                )
                second = topics.schedule_order_topic(
                    owner_id="usr_1",
                    title="Other title",
                    scheduled_at="2099-12-02T15:00:00+00:00",
                    order_id="ord_1",
                    order_video_id="vid_1",
                    aspect="9:16",
                )
            self.assertFalse(first["already_queued"])
            self.assertEqual(first["topic"]["order_id"], "ord_1")
            self.assertEqual(first["topic"]["order_video_id"], "vid_1")
            self.assertEqual(first["topic"]["owner_id"], "usr_1")
            self.assertTrue(second["already_queued"])
            self.assertEqual(second["topic"]["id"], first["topic"]["id"])
            self.assertEqual(second["scheduled_at"], first["scheduled_at"])
            kick.assert_not_called()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved["topics"]), 1)
            self.assertEqual(saved["topics"][0]["order_id"], "ord_1")
            self.assertEqual(saved["topics"][0]["aspect"], "9:16")
            self.assertEqual(saved["topics"][0]["art_style"], "anime")
            self.assertEqual(saved["topics"][0]["duration_min"], 5)
        finally:
            tmp.cleanup()

    @staticmethod
    def _ensure(topic):
        topic["job_id"] = "job-" + str(topic.get("order_video_id"))
        return topic["job_id"]

    def test_job_meta_records_the_order(self):
        saved = {}

        def load_meta(job_id):
            return {"id": job_id, "aspect": "16:9"}

        def save_meta(job_id, meta):
            saved.clear()
            saved.update(meta)
            return meta

        with mock.patch("studio.projects.load_meta", load_meta), mock.patch("studio.projects.save_meta", save_meta):
            from studio.topics import _stamp_order_on_job

            _stamp_order_on_job(
                "job1",
                {"order_id": "ord_1", "order_video_id": "vid_1", "aspect": "both", "art_style": "pixar_3d"},
            )
        self.assertEqual(saved["order_id"], "ord_1")
        self.assertEqual(saved["order_video_id"], "vid_1")
        self.assertEqual(saved["aspect"], "both")
        self.assertEqual(saved["art_style"], "pixar_3d")


class ArtStyleThumbnailTests(unittest.TestCase):
    def test_thumbnail_names_cannot_escape_the_thumbs_directory(self):
        from studio.orders import _thumb_path

        for name in (
            "../settings.json",
            "..\\settings.json",
            "classic/../../settings.json",
            "/etc/passwd",
            "classic.png/../x",
            "",
        ):
            with self.assertRaises(LookupError):
                _thumb_path(name)

    def test_thumbnail_upload_uses_the_style_id_as_the_filename(self):
        from studio import orders

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        saved: dict = {}

        def fake_save(updates):
            saved.update(updates)
            return updates

        styles = [{"id": "classic", "name": "Classic (hand-drawn 2D)", "thumbnail": ""}]
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
        try:
            with mock.patch.object(orders, "ART_THUMBS_DIR", root), mock.patch.object(
                orders, "configured_art_styles", return_value=styles
            ), mock.patch("studio.settings.save_settings", side_effect=fake_save):
                orders.save_order_art_thumbnail("classic", png)
            path = root / "classic.png"
            self.assertTrue(path.is_file())
            self.assertEqual(path.resolve().parent, root.resolve())
            self.assertEqual(saved["order_art_styles"]["items"][0]["id"], "classic")
            self.assertEqual(saved["order_art_styles"]["items"][0]["thumbnail"], "classic.png")
            with self.assertRaises(ValueError):
                orders.add_order_art_style("not_a_real_style")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
