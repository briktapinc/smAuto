"""Migrate flat user_data/projects → user_data/users/<owner_id>/projects/.

Dry-run by default. Pass --execute to apply (requires explicit confirmation flag).

Usage:
  .venv/bin/python -m studio.migrate_tenancy
  .venv/bin/python -m studio.migrate_tenancy --execute --i-understand
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from studio.members import ensure_members_store, list_users
from studio.paths import PROJECTS_DIR, USERS_DIR, ensure_dirs, user_projects_dir
from studio.projects import (
    _ensure_compat_symlink,
    _load_project_index,
    _save_project_index,
    index_project,
)
from studio.tenant import migrate_orphans_to_admin


def _plan() -> list[dict[str, Any]]:
    ensure_dirs()
    ensure_members_store()
    orphan_stats = migrate_orphans_to_admin()
    moves: list[dict[str, Any]] = []
    if not PROJECTS_DIR.is_dir():
        return moves

    for folder in sorted(PROJECTS_DIR.iterdir()):
        if folder.is_symlink():
            # Already compat-linked — ensure index entry
            try:
                target = folder.resolve()
                meta_path = target / "meta.json"
                if meta_path.is_file():
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    oid = str(meta.get("owner_id") or "").strip()
                    pid = str(meta.get("id") or folder.name)
                    if oid:
                        moves.append(
                            {
                                "id": pid,
                                "action": "index_symlink",
                                "from": str(folder),
                                "to": str(target),
                                "owner_id": oid,
                                "bytes": 0,
                            }
                        )
            except OSError:
                pass
            continue
        if not folder.is_dir():
            continue
        meta_path = folder / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pid = str(meta.get("id") or folder.name)
        oid = str(meta.get("owner_id") or "").strip()
        if not oid:
            admins = [u for u in list_users(include_disabled=False) if u.get("role") == "admin"]
            oid = str(admins[0]["id"]) if admins else ""
        if not oid:
            moves.append(
                {
                    "id": pid,
                    "action": "skip_no_owner",
                    "from": str(folder),
                    "to": None,
                    "owner_id": None,
                    "bytes": 0,
                }
            )
            continue
        dest = user_projects_dir(oid) / pid
        size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
        if dest.resolve() == folder.resolve():
            moves.append(
                {
                    "id": pid,
                    "action": "already_tenant",
                    "from": str(folder),
                    "to": str(dest),
                    "owner_id": oid,
                    "bytes": size,
                }
            )
            continue
        # If dest already exists and is the real data, just link
        if dest.is_dir() and (dest / "meta.json").is_file():
            moves.append(
                {
                    "id": pid,
                    "action": "link_only",
                    "from": str(folder),
                    "to": str(dest),
                    "owner_id": oid,
                    "bytes": 0,
                }
            )
            continue
        moves.append(
            {
                "id": pid,
                "action": "move",
                "from": str(folder),
                "to": str(dest),
                "owner_id": oid,
                "bytes": size,
                "orphans_fixed": orphan_stats,
            }
        )
    return moves


def _execute(moves: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"moved": 0, "linked": 0, "indexed": 0, "skipped": 0}
    mapping = _load_project_index()
    for item in moves:
        action = item["action"]
        pid = item["id"]
        oid = item.get("owner_id") or ""
        if action == "skip_no_owner":
            counts["skipped"] += 1
            continue
        if action in ("index_symlink", "already_tenant"):
            if oid:
                mapping[pid] = oid
                counts["indexed"] += 1
            continue
        src = Path(item["from"])
        dest = Path(item["to"])
        if action == "link_only":
            if src.exists() and not src.is_symlink():
                # Replace flat dir with symlink only if empty of unique content — safer: rename aside
                bak = src.with_name(src.name + ".__pre_tenant__")
                if not bak.exists():
                    src.rename(bak)
                _ensure_compat_symlink(dest, pid)
            else:
                _ensure_compat_symlink(dest, pid)
            mapping[pid] = oid
            counts["linked"] += 1
            continue
        if action == "move":
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                counts["skipped"] += 1
                continue
            shutil.move(str(src), str(dest))
            _ensure_compat_symlink(dest, pid)
            mapping[pid] = oid
            counts["moved"] += 1
    _save_project_index(mapping)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate projects into per-user tenant folders")
    parser.add_argument("--execute", action="store_true", help="Apply moves (default is dry-run)")
    parser.add_argument(
        "--i-understand",
        action="store_true",
        help="Required with --execute to confirm destructive path moves",
    )
    args = parser.parse_args(argv)
    moves = _plan()
    total_bytes = sum(int(m.get("bytes") or 0) for m in moves if m["action"] == "move")
    print("=== Tenancy migration plan (dry-run) ===")
    print(f"USERS_DIR={USERS_DIR}")
    print(f"PROJECTS_DIR={PROJECTS_DIR}")
    print(f"entries={len(moves)} move_bytes≈{total_bytes}")
    for m in moves:
        print(
            f"  [{m['action']}] {m['id']}: {m['from']} → {m.get('to')} "
            f"(owner={m.get('owner_id')}, bytes={m.get('bytes')})"
        )
    if not args.execute:
        print("\nNo changes written. Re-run with: --execute --i-understand")
        return 0
    if not args.i_understand:
        print("Refusing --execute without --i-understand", file=sys.stderr)
        return 2
    counts = _execute(moves)
    print("\n=== Applied ===")
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
