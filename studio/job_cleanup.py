"""Auto-cleanup for completed (rendered + YouTube-uploaded) Studio jobs."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from studio.audit import write_entry
from studio.settings import load_settings

_HEX_SUFFIX = re.compile(r"^(?P<base>.+)-(?P<hex>[a-f0-9]{6})$")
_DEFAULT_RETENTION_DAYS = 7
_lock = threading.Lock()


def story_slug(project_id: str) -> str:
    """Base story id without the random -abcdef retry suffix."""
    pid = (project_id or "").strip().lower()
    match = _HEX_SUFFIX.match(pid)
    if match:
        return match.group("base")
    return pid


def _parse_ts(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _job_created_at(item: dict[str, Any]) -> datetime | None:
    return _parse_ts(item.get("created_at")) or _parse_ts(item.get("updated_at"))


def _completion_sort_key(item: dict[str, Any]) -> str:
    yt = item.get("youtube") if isinstance(item.get("youtube"), dict) else {}
    return str(
        yt.get("uploaded_at")
        or item.get("updated_at")
        or item.get("created_at")
        or ""
    )


def is_completed_job(item: dict[str, Any]) -> bool:
    """Rendered and successfully uploaded (confirmed YouTube id/URL stored)."""
    if not item:
        return False
    yt = item.get("youtube") if isinstance(item.get("youtube"), dict) else {}
    video_id = str(
        item.get("youtube_video_id")
        or yt.get("video_id")
        or yt.get("id")
        or ""
    ).strip()
    url = str(item.get("youtube_url") or yt.get("url") or "").strip()
    has_youtube = bool(item.get("has_youtube") or video_id or url)
    if not has_youtube:
        return False
    has_video = bool(item.get("has_video"))
    status = str(item.get("status") or "").strip().lower()
    renders = item.get("renders") if isinstance(item.get("renders"), dict) else {}
    rendered = has_video or status == "rendered" or bool(item.get("last_render_aspect")) or any(
        isinstance(blob, dict) and blob.get("ready") for blob in renders.values()
    )
    # A confirmed upload implies the job was rendered even if local mp4 was deleted.
    return rendered or bool(video_id or url)


def is_protected_job(item: dict[str, Any]) -> bool:
    """Never auto-delete these: live pipeline, pending upload, or failed/retryable."""
    if not item:
        return True
    if item.get("running") or item.get("busy") or item.get("queued") or item.get("stopping"):
        return True
    if item.get("youtube_pending"):
        return True
    job = item.get("job") if isinstance(item.get("job"), dict) else {}
    err = str(job.get("error") or item.get("youtube_error") or job.get("youtube_error") or "").strip()
    if err and not is_completed_job(item):
        return True
    return False


def retention_cutoff(retention_days: int, *, now: datetime | None = None) -> datetime:
    days = max(1, int(retention_days or _DEFAULT_RETENTION_DAYS))
    stamp = now or datetime.now(timezone.utc)
    return stamp - timedelta(days=days)


def list_eligible_completed_jobs(
    *,
    owner_id: str | None = None,
    is_admin: bool = False,
    retention_days: int | None = None,
    now: datetime | None = None,
    items: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Completed jobs eligible for cleanup (aged and/or older story duplicates)."""
    settings = load_settings()
    days = int(retention_days if retention_days is not None else settings.get("completed_job_retention_days") or _DEFAULT_RETENTION_DAYS)
    cutoff = retention_cutoff(days, now=now)
    stamp = now or datetime.now(timezone.utc)

    if items is None:
        from studio.pipeline import list_library_items

        items = list_library_items(owner_id=owner_id, is_admin=is_admin, archived_only=False)
    completed = [item for item in items if is_completed_job(item) and not is_protected_job(item)]

    # Per story slug, keep only the latest completed job.
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for item in completed:
        by_slug.setdefault(story_slug(str(item.get("id") or "")), []).append(item)

    keep_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    for group in by_slug.values():
        ordered = sorted(group, key=_completion_sort_key, reverse=True)
        if not ordered:
            continue
        keep_ids.add(str(ordered[0].get("id") or ""))
        for older in ordered[1:]:
            duplicate_ids.add(str(older.get("id") or ""))

    eligible: list[dict[str, Any]] = []
    for item in completed:
        pid = str(item.get("id") or "")
        if not pid:
            continue
        if pid in duplicate_ids:
            reason = "older_completed_duplicate"
        elif pid in keep_ids:
            created = _job_created_at(item)
            if created is None or created > cutoff:
                continue
            reason = "completed_past_retention"
        else:
            continue
        eligible.append(
            {
                "id": pid,
                "title": item.get("title") or item.get("topic") or pid,
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "youtube_url": item.get("youtube_url"),
                "story_slug": story_slug(pid),
                "reason": reason,
                "retention_days": days,
                "as_of": stamp.isoformat().replace("+00:00", "Z"),
            }
        )
    eligible.sort(key=lambda row: str(row.get("created_at") or ""))
    return eligible


def cleanup_completed_jobs(
    *,
    owner_id: str | None = None,
    is_admin: bool = True,
    dry_run: bool = False,
    source: str = "auto",
    username: str = "",
    retention_days: int | None = None,
) -> dict[str, Any]:
    """Delete eligible completed jobs. Logs each deletion to the audit log."""
    from studio.projects import delete_project

    with _lock:
        eligible = list_eligible_completed_jobs(
            owner_id=owner_id,
            is_admin=is_admin,
            retention_days=retention_days,
        )
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "count": len(eligible),
                "jobs": eligible,
                "retention_days": int(
                    retention_days
                    if retention_days is not None
                    else load_settings().get("completed_job_retention_days")
                    or _DEFAULT_RETENTION_DAYS
                ),
            }

        deleted: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for row in eligible:
            pid = str(row.get("id") or "")
            title = str(row.get("title") or pid)
            reason = str(row.get("reason") or "completed_cleanup")
            try:
                delete_project(pid, delete_files=True)
                entry = {
                    "id": pid,
                    "title": title,
                    "reason": reason,
                    "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                deleted.append(entry)
                write_entry(
                    action="job_cleanup_delete",
                    source=source,
                    username=username or ("system" if source == "auto" else ""),
                    success=True,
                    args={
                        "project_id": pid,
                        "title": title,
                        "reason": reason,
                        "story_slug": row.get("story_slug"),
                        "retention_days": row.get("retention_days"),
                    },
                    extra={"cleanup": entry},
                )
            except Exception as exc:
                errors.append({"id": pid, "error": str(exc)})
                write_entry(
                    action="job_cleanup_delete",
                    source=source,
                    username=username or ("system" if source == "auto" else ""),
                    success=False,
                    error=str(exc),
                    args={"project_id": pid, "title": title, "reason": reason},
                )
        return {
            "ok": not errors,
            "dry_run": False,
            "count": len(deleted),
            "deleted": deleted,
            "errors": errors,
            "retention_days": int(
                retention_days
                if retention_days is not None
                else load_settings().get("completed_job_retention_days")
                or _DEFAULT_RETENTION_DAYS
            ),
        }


def run_scheduled_cleanup() -> dict[str, Any]:
    """System-wide cleanup used by the Studio background loop."""
    return cleanup_completed_jobs(owner_id=None, is_admin=True, dry_run=False, source="auto")
