"""Daily JSON-store backup helpers (Phase 5).

Hostinger weekly snapshots remain the primary restore path. This writes a
timestamped dump of critical Studio JSON files under user_data/backups/.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from studio.paths import USER_DATA, ensure_dirs

BACKUP_DIR = USER_DATA / "backups"
BACKUP_TARGETS = (
    "members.json",
    "auth.json",
    "job_queue.json",
    "usage_ledger.json",
    "billing_ledger.json",
    "api_keys.json",
    "project_index.json",
    "topics.json",
    "settings.json",
)


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_backup(*, keep: int = 14) -> dict[str, Any]:
    ensure_dirs()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    dest = BACKUP_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    missing: list[str] = []
    for name in BACKUP_TARGETS:
        src = USER_DATA / name
        if not src.is_file():
            missing.append(name)
            continue
        shutil.copy2(src, dest / name)
        copied.append(name)
    # Scrub secrets in the settings copy
    settings_copy = dest / "settings.json"
    if settings_copy.is_file():
        try:
            data = json.loads(settings_copy.read_text(encoding="utf-8"))
            for key in list(data.keys()):
                low = key.lower()
                if any(s in low for s in ("secret", "password", "api_key", "token", "pin_hash", "fal_key")):
                    if data.get(key):
                        data[key] = "***"
            settings_copy.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
    # Prune old backups
    dirs = sorted([p for p in BACKUP_DIR.iterdir() if p.is_dir()], reverse=True)
    removed = []
    for old in dirs[max(1, int(keep)) :]:
        try:
            shutil.rmtree(old)
            removed.append(old.name)
        except OSError:
            pass
    manifest = {
        "ok": True,
        "stamp": stamp,
        "path": str(dest),
        "copied": copied,
        "missing": missing,
        "removed": removed,
        "note": (
            "Local JSON dump only. Keep Hostinger weekly snapshots enabled; "
            "optionally sync user_data/backups/ to object storage."
        ),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    print(json.dumps(run_backup(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
