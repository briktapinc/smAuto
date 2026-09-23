"""Focused checks for unsupervised chatgpt image-provider gate skip.

Run from repo root:
  .venv/bin/python -m studio.test_unsupervised_image_gate
"""
from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class UnsupervisedImageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        from studio.paths import ensure_dirs
        from studio.projects import create_project, project_dir, set_image_provider

        ensure_dirs()
        self._created: list[str] = []
        self._create_project = create_project
        self._project_dir = project_dir
        self._set_image_provider = set_image_provider

    def tearDown(self) -> None:
        for pid in self._created:
            shutil.rmtree(self._project_dir(pid), ignore_errors=True)

    def _new_project(self) -> str:
        project = self._create_project("unsupervised-gate-test", 60, title="Gate Test")
        pid = str(project["id"])
        self._created.append(pid)
        self._set_image_provider(pid, "chatgpt")
        return pid

    def _write_cover(self, project_id: str) -> Path:
        from studio.aspect import ASPECT_16_9, canvas_size
        from studio.projects import cover_path

        dest = cover_path(project_id, ASPECT_16_9)
        dest.parent.mkdir(parents=True, exist_ok=True)
        w, h = canvas_size(ASPECT_16_9)
        Image.new("RGB", (w, h), color=(40, 80, 120)).save(dest)
        return dest

    def test_skip_message_when_all_slots_filled(self) -> None:
        from studio.illustrations import (
            all_illustration_slots_filled,
            illustration_slot_inventory,
            unsupervised_image_provider_gate_skip_message,
        )

        pid = self._new_project()
        self.assertIsNone(unsupervised_image_provider_gate_skip_message(pid))
        self.assertFalse(all_illustration_slots_filled(pid))

        self._write_cover(pid)
        inv = illustration_slot_inventory(pid)
        self.assertEqual(inv["missing"], 0)
        self.assertGreater(inv["total"], 0)
        self.assertTrue(all_illustration_slots_filled(pid))
        msg = unsupervised_image_provider_gate_skip_message(pid)
        self.assertEqual(
            msg,
            f"image provider gate skipped: all {inv['total']} illustration slots already filled",
        )

    def test_prepare_unsupervised_skips_when_filled(self) -> None:
        from studio.topics import _prepare_unsupervised_job

        pid = self._new_project()
        self._write_cover(pid)
        with (
            mock.patch("studio.settings.load_settings", return_value={"fal_key": ""}),
            mock.patch("studio.topics.hands_off_enabled", return_value=False),
            mock.patch("studio.topics._script_ready", return_value=True),
            mock.patch("studio.settings.is_native_text_provider", return_value=False),
        ):
            notes = _prepare_unsupervised_job(pid)
        self.assertTrue(
            any(n.startswith("image provider gate skipped:") for n in notes),
            notes,
        )
        from studio.projects import project_image_provider

        # Must not mutate chatgpt → flux when slots are already filled.
        self.assertEqual(project_image_provider(pid), "chatgpt")

    def test_prepare_unsupervised_notes_when_chatgpt_missing(self) -> None:
        from studio.topics import _prepare_unsupervised_job

        pid = self._new_project()
        with (
            mock.patch("studio.settings.load_settings", return_value={"fal_key": ""}),
            mock.patch("studio.topics.hands_off_enabled", return_value=False),
            mock.patch("studio.topics._script_ready", return_value=True),
            mock.patch("studio.settings.is_native_text_provider", return_value=True),
            mock.patch("studio.settings.is_api_text_provider", return_value=False),
        ):
            notes = _prepare_unsupervised_job(pid)
        self.assertTrue(
            any("save_illustration_image" in n and "chatgpt" in n for n in notes),
            notes,
        )
        from studio.projects import project_image_provider

        self.assertEqual(project_image_provider(pid), "chatgpt")

    def test_prepare_external_notes_when_missing(self) -> None:
        from studio.topics import _prepare_unsupervised_job

        pid = self._new_project()
        self._set_image_provider(pid, "external")
        with (
            mock.patch("studio.settings.load_settings", return_value={"fal_key": ""}),
            mock.patch("studio.topics.hands_off_enabled", return_value=False),
            mock.patch("studio.topics._script_ready", return_value=True),
            mock.patch("studio.settings.is_native_text_provider", return_value=True),
        ):
            notes = _prepare_unsupervised_job(pid)
        self.assertTrue(any("image_provider is external" in n for n in notes), notes)

    def test_repro_project_inventory_matches_list_jobs(self) -> None:
        """Live inventory sanity for the reported filled project (skip if absent)."""
        from studio.illustrations import illustration_jobs, illustration_slot_inventory
        from studio.projects import project_dir

        pid = "why-do-your-fingers-get-wrinkly-in-water-e8143e"
        if not project_dir(pid).is_dir():
            self.skipTest("repro project not on disk")
        info = illustration_jobs(pid)
        inv = illustration_slot_inventory(pid)
        self.assertEqual(inv["total"], len(info.get("jobs") or []))
        self.assertEqual(inv["missing"], int(info.get("missing") or 0))
        if inv["missing"] == 0 and inv["total"] > 0:
            from studio.illustrations import unsupervised_image_provider_gate_skip_message

            self.assertIsNotNone(unsupervised_image_provider_gate_skip_message(pid))


def main() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(UnsupervisedImageGateTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
