"""Delete job: stop running/queued work and remove assets."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class DeleteJobTests(unittest.TestCase):
    def test_delete_stops_and_removes_folder(self):
        from studio import projects

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects_dir = root / "projects"
            pid = "del-job-1"
            folder = projects_dir / pid
            folder.mkdir(parents=True)
            (folder / "meta.json").write_text(
                json.dumps({"id": pid, "title": "T"}),
                encoding="utf-8",
            )
            (folder / "script.wav").write_bytes(b"x" * 32)
            index = root / "project_index.json"
            index.write_text(json.dumps({"version": 1, "projects": {}}), encoding="utf-8")
            deleted = root / "deleted_projects.json"

            stop = mock.Mock(return_value={"running": False})
            with mock.patch.object(projects, "PROJECTS_DIR", projects_dir), mock.patch.object(
                projects, "USERS_DIR", root / "users"
            ), mock.patch.object(projects, "PROJECT_INDEX_PATH", index), mock.patch.object(
                projects, "DELETED_IDS_PATH", deleted
            ), mock.patch.object(projects, "ensure_dirs", lambda: None), mock.patch(
                "studio.pipeline.is_busy", side_effect=[True, False, False]
            ), mock.patch("studio.pipeline.is_running", return_value=True), mock.patch(
                "studio.pipeline.stop_project", stop
            ), mock.patch("studio.pipeline.forget_live_job") as forget, mock.patch(
                "studio.job_queue.purge_project",
                return_value={"ok": True, "removed": 1, "cancelled": 1},
            ), mock.patch("studio.job_queue.pump"), mock.patch(
                "studio.topics.unlink_job_from_topics",
                return_value={"ok": True, "updated": 1},
            ):
                out = projects.delete_project(pid, delete_files=True)

            self.assertTrue(out["deleted"])
            self.assertTrue(out["files_deleted"])
            self.assertTrue(out["halt"]["was_busy"])
            self.assertTrue(out["halt"]["stopped"])
            stop.assert_called_once()
            forget.assert_called()
            self.assertFalse(folder.exists())


if __name__ == "__main__":
    unittest.main()
