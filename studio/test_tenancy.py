"""Phase 1 tenancy: path isolation, API keys, registration mode."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TenancyPathTests(unittest.TestCase):
    def test_safe_project_id_rejects_traversal(self):
        from studio.projects import _safe_project_id

        with self.assertRaises(FileNotFoundError):
            _safe_project_id("../etc/passwd")
        with self.assertRaises(FileNotFoundError):
            _safe_project_id("foo/bar")
        self.assertEqual(_safe_project_id("ok-project"), "ok-project")

    def test_create_project_lands_under_user_namespace(self):
        from studio import paths, projects

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(paths, "USER_DATA", root):
                with mock.patch.object(paths, "PROJECTS_DIR", root / "projects"):
                    with mock.patch.object(paths, "USERS_DIR", root / "users"):
                        with mock.patch.object(paths, "PROJECT_INDEX_PATH", root / "project_index.json"):
                            with mock.patch.object(projects, "PROJECTS_DIR", root / "projects"):
                                with mock.patch.object(projects, "USERS_DIR", root / "users"):
                                    with mock.patch.object(
                                        projects, "PROJECT_INDEX_PATH", root / "project_index.json"
                                    ):
                                        (root / "projects").mkdir()
                                        (root / "users").mkdir()
                                        meta = projects.create_project(
                                            "Hello World",
                                            60,
                                            title="Hello World",
                                            owner_id="userabc123",
                                        )
                                        pid = meta["id"]
                                        tenant = root / "users" / "userabc123" / "projects" / pid
                                        self.assertTrue((tenant / "meta.json").is_file())
                                        self.assertEqual(
                                            projects.project_dir(pid).resolve(), tenant.resolve()
                                        )
                                        # Compat symlink at legacy path
                                        link = root / "projects" / pid
                                        self.assertTrue(link.exists())


class ApiKeyTests(unittest.TestCase):
    def test_create_resolve_revoke(self):
        from studio import api_keys, paths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keys_path = root / "api_keys.json"
            with mock.patch.object(paths, "API_KEYS_PATH", keys_path):
                with mock.patch.object(api_keys, "API_KEYS_PATH", keys_path):
                    with mock.patch("studio.api_keys.get_user_by_id", create=True) as _:
                        pass
                    # Patch resolve's get_user_by_id
                    fake_user = {
                        "id": "uid1",
                        "username": "member1",
                        "role": "member",
                        "disabled": False,
                    }

                    def _get(uid):
                        return fake_user if uid == "uid1" else None

                    with mock.patch("studio.members.get_user_by_id", _get):
                        created = api_keys.create_api_key("uid1", name="test")
                        raw = created["key"]
                        self.assertTrue(raw.startswith("bp_live_"))
                        user = api_keys.resolve_api_key(raw)
                        self.assertEqual(user["id"], "uid1")
                        api_keys.revoke_api_key("uid1", created["id"])
                        self.assertIsNone(api_keys.resolve_api_key(raw))


class RegistrationModeTests(unittest.TestCase):
    def test_normalize(self):
        from studio.settings import normalize_registration_mode

        self.assertEqual(normalize_registration_mode("open"), "open")
        self.assertEqual(normalize_registration_mode("invite"), "invite_only")
        self.assertEqual(normalize_registration_mode("closed"), "disabled")


class DeviceTests(unittest.TestCase):
    def test_preferred_device_honors_env(self):
        from studio.device import preferred_torch_device
        import os

        old = os.environ.get("BUBBLEPOD_TTS_DEVICE")
        try:
            os.environ["BUBBLEPOD_TTS_DEVICE"] = "cpu"
            self.assertEqual(preferred_torch_device(), "cpu")
            os.environ["BUBBLEPOD_TTS_DEVICE"] = "cuda"
            self.assertEqual(preferred_torch_device(), "cuda")
        finally:
            if old is None:
                os.environ.pop("BUBBLEPOD_TTS_DEVICE", None)
            else:
                os.environ["BUBBLEPOD_TTS_DEVICE"] = old


if __name__ == "__main__":
    unittest.main()
