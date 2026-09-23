"""Admin ops: overview, dead-letter jobs, system health (Phase 5)."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def system_load() -> dict[str, Any]:
    load1 = load5 = load15 = None
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        pass
    mem = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            info = {}
            for line in fh:
                if ":" in line:
                    k, v = line.split(":", 1)
                    info[k.strip()] = v.strip()
        total = int(info.get("MemTotal", "0").split()[0]) * 1024
        avail = int(info.get("MemAvailable", "0").split()[0]) * 1024
        mem = {
            "total_bytes": total,
            "available_bytes": avail,
            "used_pct": round(100.0 * (1 - avail / total), 1) if total else None,
        }
    except Exception:
        mem = {}
    disk = {}
    try:
        from studio.paths import USER_DATA

        usage = shutil.disk_usage(str(USER_DATA))
        disk = {
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_pct": round(100.0 * (usage.used / usage.total), 1) if usage.total else None,
        }
    except Exception:
        pass
    return {
        "loadavg": {"1": load1, "5": load5, "15": load15},
        "memory": mem,
        "disk": disk,
        "observed_at": _now(),
    }


def fal_spend_today() -> dict[str, Any]:
    from studio.costs import estimate_flux_usd
    from studio.spend_guard import load_counters

    c = load_counters()
    n = int(c.get("flux_images") or 0)
    return {
        "day": c.get("day"),
        "flux_images": n,
        "usd_estimate": estimate_flux_usd(n),
    }


def worker_health() -> dict[str, Any]:
    from studio.job_queue import (
        STATUS_RUNNING,
        _live_running_ids,
        _parse_iso,
        _read_store,
        _utcnow,
        lease_seconds,
        list_queue,
        public_status,
    )

    snap = public_status()
    live = _live_running_ids()
    store = _read_store()
    now = _utcnow()
    leases = []
    for job in store.get("jobs") or []:
        if str(job.get("status") or "") != STATUS_RUNNING:
            continue
        pid = str(job.get("project_id") or "")
        lease_at = _parse_iso(str(job.get("lease_expires_at") or ""))
        leases.append(
            {
                "project_id": pid,
                "owner_id": job.get("owner_id"),
                "step": job.get("step"),
                "progress_pct": job.get("progress_pct"),
                "attempts": job.get("attempts"),
                "lease_expires_at": job.get("lease_expires_at"),
                "lease_alive": bool(lease_at and lease_at > now),
                "thread_alive": pid in live,
                "detail": job.get("detail"),
                "error": job.get("error"),
            }
        )
    return {
        "queue": snap,
        "lease_seconds": lease_seconds(),
        "workers": leases,
        "orphans": [w for w in leases if not w["thread_alive"]],
    }


def users_usage_rows(*, limit: int = 100) -> list[dict[str, Any]]:
    from studio.members import list_users
    from studio.usage import usage_snapshot

    rows = []
    for user in list_users(include_disabled=True)[: max(1, min(500, limit))]:
        snap = usage_snapshot(user)
        rows.append(
            {
                "id": user.get("id"),
                "username": user.get("username"),
                "role": user.get("role"),
                "plan_tier": snap.get("plan_tier"),
                "subscription_status": user.get("subscription_status"),
                "has_access": user.get("has_access"),
                "disabled": user.get("disabled"),
                "used": snap.get("used"),
                "allowed": snap.get("allowed"),
                "unlimited": snap.get("unlimited"),
            }
        )
    return rows


def count_failed_jobs() -> int:
    from studio.job_queue import STATUS_FAILED, _read_store

    store = _read_store()
    return sum(
        1 for j in (store.get("jobs") or []) if str(j.get("status") or "") == STATUS_FAILED
    )


def list_failed_jobs(*, limit: int = 50) -> list[dict[str, Any]]:
    from studio.job_queue import STATUS_FAILED, _public_job, _read_store

    store = _read_store()
    failed = [
        j for j in (store.get("jobs") or []) if str(j.get("status") or "") == STATUS_FAILED
    ]
    failed.sort(
        key=lambda j: str(j.get("finished_at") or j.get("updated_at") or ""),
        reverse=True,
    )
    return [_public_job(j) for j in failed[: max(1, min(200, int(limit)))]]


def retry_failed_job(queue_job_id: str, *, admin_username: str = "") -> dict[str, Any]:
    """Re-queue a failed job for resume (respects attempts / clears terminal state)."""
    from studio.audit import write_entry
    from studio.job_queue import (
        STATUS_FAILED,
        STATUS_QUEUED,
        _file_mutex,
        _lock,
        _now,
        _public_job,
        _read_store,
        _write_store,
        QUEUE_MUTEX_PATH,
        max_job_attempts,
        pump,
    )

    qid = (queue_job_id or "").strip()
    if not qid:
        raise ValueError("job id required")
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _read_store()
            job = None
            for row in data.get("jobs") or []:
                if str(row.get("id") or "") == qid:
                    job = row
                    break
            if job is None:
                raise FileNotFoundError(f"Unknown job: {qid}")
            if str(job.get("status") or "") != STATUS_FAILED:
                raise ValueError(f"Job is not failed (status={job.get('status')})")
            attempts = int(job.get("attempts") or 0)
            max_a = int(job.get("max_attempts") or max_job_attempts())
            if attempts >= max_a:
                # Admin retry resets attempt budget once
                job["attempts"] = max(0, max_a - 1)
            job["status"] = STATUS_QUEUED
            job["kind"] = "resume"
            job["finished_at"] = None
            job["started_at"] = None
            job["claimed_at"] = None
            job["lease_expires_at"] = None
            job["next_retry_at"] = None
            job["block_reason"] = None
            job["error"] = None
            job["error_code"] = None
            job["detail"] = "Re-queued by admin (dead-letter retry)."
            job["updated_at"] = _now()
            _write_store(data)
            public = _public_job(job)
    try:
        write_entry(
            source="admin",
            action="job_retry",
            username=admin_username or "admin",
            args={"queue_job_id": qid, "project_id": public.get("project_id")},
            success=True,
        )
    except Exception:
        pass
    try:
        pump()
    except Exception:
        pass
    return {"ok": True, "job": public}


def admin_overview_payload() -> dict[str, Any]:
    from studio.stripe_billing import billing_overview

    base = billing_overview()
    base["system"] = system_load()
    base["fal_today"] = fal_spend_today()
    base["workers"] = worker_health()
    base["users_usage"] = users_usage_rows(limit=50)
    base["failed_jobs"] = list_failed_jobs(limit=25)
    base["failed_jobs_count"] = count_failed_jobs()
    return base
