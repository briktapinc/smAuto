"""Multi-user pipeline job queue with configurable concurrency.

Persists to ``user_data/job_queue.json`` so restarts keep queued work.

Scheduling (Phase 3):
  - Global worker slots: ``max_concurrent_jobs`` / env ``BUBBLEPOD_MAX_CONCURRENT_JOBS``
    / ``BUBBLEPOD_RENDER_WORKERS`` / ``RENDER_WORKERS`` (default **1** — correct for a
    2 vCPU / 8 GB box; frame drawing oversubscribes, so do not set N = core count).
  - Fairness: round-robin across ``owner_id``; FIFO within each user.
  - Per-user concurrency cap (default 1); admins use ``admin_concurrency`` (default
    same as global max, so the owner priority lane can use every free slot).
  - Owner/admin priority lane: admin jobs are scheduled before member jobs each round.
  - Quota gate hook (Phase 4 fills it): jobs may stay queued with ``quota_exhausted``.

Desktop / single-user mode keeps working: with one user and a free slot, start still
runs immediately; when at capacity, the same queue waits.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from studio.paths import USER_DATA, ensure_dirs

QUEUE_PATH = USER_DATA / "job_queue.json"
QUEUE_MUTEX_PATH = USER_DATA / "job_queue.mutex"

STATUS_QUEUED = "queued"
STATUS_CLAIMED = "claimed"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_ACTIVE = frozenset({STATUS_QUEUED, STATUS_CLAIMED, STATUS_RUNNING})
_TERMINAL = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED})

_lock = threading.RLock()
_pump_lock = threading.Lock()
_last_served_owner: str = ""
_HISTORY_LIMIT = 80
_lease_thread: threading.Thread | None = None
_lease_stop = threading.Event()

# Restart-safe engine defaults (overridable via env).
DEFAULT_LEASE_SEC = 60
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_HEARTBEAT_SEC = 20


def lease_seconds() -> int:
    raw = (os.environ.get("BUBBLEPOD_JOB_LEASE_SEC") or "").strip()
    try:
        return max(15, min(600, int(raw))) if raw else DEFAULT_LEASE_SEC
    except (TypeError, ValueError):
        return DEFAULT_LEASE_SEC


def max_job_attempts() -> int:
    raw = (os.environ.get("BUBBLEPOD_JOB_MAX_ATTEMPTS") or "").strip()
    try:
        return max(1, min(10, int(raw))) if raw else DEFAULT_MAX_ATTEMPTS
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTEMPTS


def _parse_iso(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_expiry_iso(*, from_dt: datetime | None = None) -> str:
    base = from_dt or _utcnow()
    return (base + timedelta(seconds=lease_seconds())).replace(microsecond=0).isoformat()


def is_transient_failure(error: str | None) -> bool:
    text = (error or "").lower()
    needles = (
        "exit -15",
        "sigterm",
        "killed",
        "timeout",
        "timed out",
        "connection reset",
        "connection aborted",
        "broken pipe",
        "temporarily unavailable",
        "chatterbox",
        "nonetype",
        "model-load",
        "model load",
        "gentle",
        "ffmpeg",
        "errno 11",
        "resource temporarily",
        "gpu busy",
    )
    return any(n in text for n in needles)


def _retry_delay_sec(attempts: int) -> int:
    # 5, 10, 20, ... capped at 300
    return min(300, 5 * (2 ** max(0, attempts - 1)))


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _file_mutex(path: Path):
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                if os.fstat(fd).st_size < 1:
                    os.write(fd, b"\0")
            except OSError:
                pass
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    return _cm()


def _env_max_concurrent() -> int | None:
    for key in (
        "BUBBLEPOD_RENDER_WORKERS",
        "RENDER_WORKERS",
        "STICKMAN_MAX_CONCURRENT_JOBS",
        "BUBBLEPOD_MAX_CONCURRENT_JOBS",
        "LAZYKH_MAX_CONCURRENT_JOBS",
    ):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        try:
            return max(1, min(32, int(raw)))
        except (TypeError, ValueError):
            continue
    return None


def normalize_max_concurrent(value: Any, default: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(32, n))


def max_concurrent_jobs() -> int:
    """How many pipeline workers may run at once (env, then settings, else 1)."""
    env_n = _env_max_concurrent()
    if env_n is not None:
        return env_n
    try:
        from studio.settings import load_settings

        settings = load_settings()
        if "max_concurrent_jobs" in settings and settings.get("max_concurrent_jobs") is not None:
            return normalize_max_concurrent(settings.get("max_concurrent_jobs"), 1)
    except Exception:
        pass
    return 1


def per_user_concurrency() -> int:
    """Max simultaneous running jobs per non-admin user (default 1)."""
    for key in ("BUBBLEPOD_PER_USER_CONCURRENCY", "STICKMAN_PER_USER_CONCURRENCY"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            try:
                return max(1, min(32, int(raw)))
            except (TypeError, ValueError):
                pass
    try:
        from studio.settings import load_settings, normalize_max_concurrent_jobs

        return normalize_max_concurrent_jobs(load_settings().get("per_user_concurrency"), 1)
    except Exception:
        return 1


def admin_concurrency() -> int:
    """Max simultaneous jobs for admin/owner accounts (default = global max)."""
    for key in ("BUBBLEPOD_ADMIN_CONCURRENCY", "STICKMAN_ADMIN_CONCURRENCY"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            try:
                return max(1, min(32, int(raw)))
            except (TypeError, ValueError):
                pass
    try:
        from studio.settings import load_settings, normalize_max_concurrent_jobs

        settings = load_settings()
        if settings.get("admin_concurrency") is not None:
            return normalize_max_concurrent_jobs(settings.get("admin_concurrency"), max_concurrent_jobs())
    except Exception:
        pass
    return max_concurrent_jobs()


def owner_priority_enabled() -> bool:
    for key in ("BUBBLEPOD_OWNER_PRIORITY", "STICKMAN_OWNER_PRIORITY"):
        raw = (os.environ.get(key) or "").strip().lower()
        if raw in ("0", "false", "no", "off"):
            return False
        if raw in ("1", "true", "yes", "on"):
            return True
    try:
        from studio.settings import load_settings, normalize_bool

        return normalize_bool(load_settings().get("owner_priority"), True)
    except Exception:
        return True


def _is_priority_owner(owner_id: str | None) -> bool:
    if not owner_priority_enabled():
        return False
    oid = (owner_id or "").strip()
    if not oid:
        return False
    try:
        from studio.members import get_user_by_id

        user = get_user_by_id(oid)
        return bool(user) and (user.get("role") or "") == "admin"
    except Exception:
        return False


def _owner_cap(owner_id: str | None) -> int:
    if _is_priority_owner(owner_id):
        return admin_concurrency()
    return per_user_concurrency()


def user_quota_blocks_start(owner_id: str | None) -> str | None:
    """Return a machine-readable reason if this user cannot start a job yet.

    Phase 4 fills metering; until then always allows starts.
    """
    _ = owner_id
    return None


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "jobs": [], "updated_at": _now()}


def _read_store() -> dict[str, Any]:
    ensure_dirs()
    if not QUEUE_PATH.is_file():
        return _empty_store()
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        jobs = []
    data["jobs"] = [j for j in jobs if isinstance(j, dict)]
    data.setdefault("version", 1)
    return data


def _write_store(data: dict[str, Any]) -> None:
    ensure_dirs()
    data = dict(data)
    data["updated_at"] = _now()
    jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict)]
    # Trim old terminal entries so the file stays small.
    active = [j for j in jobs if j.get("status") in _ACTIVE]
    terminal = [j for j in jobs if j.get("status") in _TERMINAL]
    terminal.sort(key=lambda j: str(j.get("finished_at") or j.get("updated_at") or ""), reverse=True)
    data["jobs"] = active + terminal[:_HISTORY_LIMIT]
    tmp = QUEUE_PATH.with_suffix(QUEUE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(QUEUE_PATH))


def _live_running_ids() -> set[str]:
    try:
        from studio.pipeline import running_project_ids

        return {str(x) for x in running_project_ids()}
    except Exception:
        return set()


def _reconcile(data: dict[str, Any], *, boot: bool = False) -> dict[str, Any]:
    """Align persisted rows with live workers; expire dead leases back to queued."""
    live = _live_running_ids()
    now = _utcnow()
    changed = False
    for job in data.get("jobs") or []:
        status = str(job.get("status") or "")
        pid = str(job.get("project_id") or "")
        # Normalize legacy/partial rows
        if "attempts" not in job:
            job["attempts"] = int(job.get("attempts") or 0)
            changed = True
        if "max_attempts" not in job:
            job["max_attempts"] = max_job_attempts()
            changed = True
        if status in (STATUS_RUNNING, STATUS_CLAIMED) and pid and pid not in live:
            lease_at = _parse_iso(str(job.get("lease_expires_at") or ""))
            expired = boot or lease_at is None or lease_at <= now
            if expired:
                attempts = int(job.get("attempts") or 0) + (0 if boot else 1)
                max_a = int(job.get("max_attempts") or max_job_attempts())
                job["attempts"] = attempts
                job["max_attempts"] = max_a
                if not boot and attempts >= max_a:
                    job["status"] = STATUS_FAILED
                    job["finished_at"] = _now()
                    job["error"] = job.get("error") or "Worker lease expired repeatedly."
                    job["detail"] = (
                        f"Failed after {attempts}/{max_a} attempts (lease expired)."
                    )
                    job["lease_expires_at"] = None
                    job["updated_at"] = _now()
                    changed = True
                    continue
                job["status"] = STATUS_QUEUED
                job["kind"] = "resume"
                job["detail"] = (
                    "Re-queued after Studio restart (resume from checkpoint)."
                    if boot
                    else "Lease expired — re-queued for resume from checkpoint."
                )
                job["started_at"] = None
                job["claimed_at"] = None
                job["lease_expires_at"] = None
                job["next_retry_at"] = None
                job["updated_at"] = _now()
                changed = True
        elif status == STATUS_QUEUED and pid and pid in live:
            job["status"] = STATUS_RUNNING
            job["started_at"] = job.get("started_at") or _now()
            job["lease_expires_at"] = _lease_expiry_iso()
            job["updated_at"] = _now()
            changed = True
        elif status == STATUS_RUNNING and pid and pid in live:
            # Refresh missing lease while live
            if not job.get("lease_expires_at"):
                job["lease_expires_at"] = _lease_expiry_iso()
                changed = True
    if changed:
        _write_store(data)
    return data


def running_count() -> int:
    return len(_live_running_ids())


def slots_free() -> int:
    return max(0, max_concurrent_jobs() - running_count())


def capacity_full() -> bool:
    return slots_free() <= 0


def _owner_key(owner_id: str | None) -> str:
    return str(owner_id or "").strip() or "_local"


def _round_robin_owners(
    by_owner: dict[str, list[dict[str, Any]]],
    owners: list[str],
    *,
    last_owner: str = "",
) -> list[dict[str, Any]]:
    owners = list(owners)
    if last_owner and last_owner in owners:
        i = owners.index(last_owner)
        owners = owners[i + 1 :] + owners[: i + 1]
    buckets = {o: list(by_owner.get(o) or []) for o in owners}
    ordered: list[dict[str, Any]] = []
    while any(buckets[o] for o in owners):
        for owner in owners:
            bucket = buckets[owner]
            if bucket:
                ordered.append(bucket.pop(0))
    return ordered


def _queue_order(jobs: list[dict[str, Any]], *, last_owner: str = "") -> list[dict[str, Any]]:
    """Fair schedule: admin priority lane, then round-robin members. Honor next_retry_at."""
    now = _utcnow()
    queued = []
    for j in jobs:
        if j.get("status") != STATUS_QUEUED:
            continue
        retry_at = _parse_iso(str(j.get("next_retry_at") or ""))
        if retry_at and retry_at > now:
            continue
        if str(j.get("block_reason") or "") == "quota_exhausted":
            continue
        queued.append(j)
    if not queued:
        return []
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for job in queued:
        key = _owner_key(job.get("owner_id"))
        by_owner.setdefault(key, []).append(job)
    for group in by_owner.values():
        group.sort(key=lambda j: (str(j.get("enqueued_at") or ""), str(j.get("id") or "")))

    priority_owners = sorted(
        o for o in by_owner if o != "_local" and _is_priority_owner(o)
    )
    # Treat unknown/_local as member lane
    member_owners = sorted(o for o in by_owner if o not in priority_owners)

    ordered: list[dict[str, Any]] = []
    if priority_owners:
        ordered.extend(_round_robin_owners(by_owner, priority_owners, last_owner=last_owner))
    if member_owners:
        member_last = last_owner if last_owner in member_owners else ""
        ordered.extend(_round_robin_owners(by_owner, member_owners, last_owner=member_last))
    return ordered


def _running_counts(jobs: list[dict[str, Any]], live: set[str] | None = None) -> dict[str, int]:
    live = live if live is not None else _live_running_ids()
    counts: dict[str, int] = {}
    seen_pids: set[str] = set()
    for job in jobs:
        pid = str(job.get("project_id") or "")
        status = str(job.get("status") or "")
        active = status in (STATUS_RUNNING, STATUS_CLAIMED) or (pid and pid in live)
        if not active or not pid or pid in seen_pids:
            continue
        seen_pids.add(pid)
        key = _owner_key(job.get("owner_id"))
        counts[key] = counts.get(key, 0) + 1
    for pid in live:
        if pid in seen_pids:
            continue
        key = "_local"
        try:
            from studio.projects import load_meta

            oid = str((load_meta(pid) or {}).get("owner_id") or "").strip()
            if oid:
                key = _owner_key(oid)
        except Exception:
            pass
        counts[key] = counts.get(key, 0) + 1
    return counts


def select_runnable(
    jobs: list[dict[str, Any]],
    *,
    limit: int,
    last_owner: str = "",
) -> list[dict[str, Any]]:
    """Pick up to ``limit`` queued jobs respecting global slots and per-user caps."""
    if limit <= 0:
        return []
    ordered = _queue_order(jobs, last_owner=last_owner)
    counts = _running_counts(jobs)
    selected: list[dict[str, Any]] = []
    for job in ordered:
        oid = _owner_key(job.get("owner_id"))
        cap = _owner_cap(job.get("owner_id"))
        if counts.get(oid, 0) >= cap:
            continue
        if user_quota_blocks_start(job.get("owner_id")):
            continue
        selected.append(job)
        counts[oid] = counts.get(oid, 0) + 1
        if len(selected) >= limit:
            break
    return selected


def _position_map(jobs: list[dict[str, Any]], *, last_owner: str = "") -> dict[str, int]:
    ordered = _queue_order(jobs, last_owner=last_owner)
    return {str(j.get("id")): i + 1 for i, j in enumerate(ordered)}


def _public_job(job: dict[str, Any], *, position: int | None = None) -> dict[str, Any]:
    out = {
        "id": job.get("id"),
        "project_id": job.get("project_id"),
        "owner_id": job.get("owner_id") or None,
        "owner_label": job.get("owner_label") or None,
        "kind": job.get("kind") or "start",
        "status": job.get("status"),
        "title": job.get("title") or None,
        "topic_id": job.get("topic_id") or None,
        "enqueued_at": job.get("enqueued_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "detail": job.get("detail"),
        "error": job.get("error"),
        "error_code": job.get("error_code"),
        "updated_at": job.get("updated_at"),
        "attempts": int(job.get("attempts") or 0),
        "max_attempts": int(job.get("max_attempts") or max_job_attempts()),
        "lease_expires_at": job.get("lease_expires_at"),
        "next_retry_at": job.get("next_retry_at"),
        "checkpoint": job.get("checkpoint") if isinstance(job.get("checkpoint"), dict) else None,
        "progress_pct": job.get("progress_pct"),
        "step": job.get("step"),
    }
    if position is not None and job.get("status") == STATUS_QUEUED:
        out["queue_position"] = position
    elif job.get("status") in (STATUS_RUNNING, STATUS_CLAIMED):
        out["queue_position"] = 0
    return out


def _find_active(data: dict[str, Any], project_id: str) -> dict[str, Any] | None:
    pid = str(project_id or "").strip()
    for job in data.get("jobs") or []:
        if str(job.get("project_id") or "") == pid and job.get("status") in _ACTIVE:
            return job
    return None


def enqueue(
    project_id: str,
    *,
    owner_id: str | None = None,
    kind: str = "start",
    title: str = "",
    topic_id: str = "",
    owner_label: str = "",
    detail: str = "",
) -> dict[str, Any]:
    """Add or refresh a queued entry for this project. Idempotent while active."""
    pid = str(project_id or "").strip()
    if not pid:
        raise ValueError("project_id required")
    kind_n = (kind or "start").strip().lower()
    if kind_n not in ("start", "resume"):
        kind_n = "start"
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _reconcile(_read_store())
            existing = _find_active(data, pid)
            if existing:
                existing["kind"] = kind_n
                if title:
                    existing["title"] = title
                if topic_id:
                    existing["topic_id"] = topic_id
                if owner_id and not existing.get("owner_id"):
                    existing["owner_id"] = owner_id
                if owner_label:
                    existing["owner_label"] = owner_label
                if detail:
                    existing["detail"] = detail
                existing["updated_at"] = _now()
                _write_store(data)
                positions = _position_map(data["jobs"], last_owner=_last_served_owner)
                return _public_job(existing, position=positions.get(str(existing.get("id"))))
            row = {
                "id": uuid.uuid4().hex[:16],
                "project_id": pid,
                "owner_id": (owner_id or "").strip() or None,
                "owner_label": (owner_label or "").strip() or None,
                "kind": kind_n,
                "status": STATUS_QUEUED,
                "title": (title or "").strip() or None,
                "topic_id": (topic_id or "").strip() or None,
                "enqueued_at": _now(),
                "started_at": None,
                "finished_at": None,
                "detail": detail or "Waiting for a free pipeline slot.",
                "error": None,
                "error_code": None,
                "attempts": 0,
                "max_attempts": max_job_attempts(),
                "lease_expires_at": None,
                "claimed_at": None,
                "next_retry_at": None,
                "checkpoint": None,
                "progress_pct": None,
                "step": None,
                "updated_at": _now(),
            }
            data["jobs"].append(row)
            _write_store(data)
            positions = _position_map(data["jobs"], last_owner=_last_served_owner)
            return _public_job(row, position=positions.get(str(row["id"])))


def cancel(project_id: str, *, owner_id: str | None = None, is_admin: bool = False) -> dict[str, Any]:
    pid = str(project_id or "").strip()
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _reconcile(_read_store())
            job = None
            for item in data.get("jobs") or []:
                if str(item.get("project_id") or "") == pid and item.get("status") == STATUS_QUEUED:
                    job = item
                    break
            if not job:
                raise FileNotFoundError(f"No queued job for project: {pid}")
            if not is_admin and owner_id:
                if str(job.get("owner_id") or "") != str(owner_id):
                    raise PermissionError("Not your queued job")
            job["status"] = STATUS_CANCELLED
            job["finished_at"] = _now()
            job["updated_at"] = _now()
            job["detail"] = "Cancelled while queued."
            _write_store(data)
            return _public_job(job)


def mark_running(project_id: str, *, queue_id: str | None = None) -> dict[str, Any] | None:
    pid = str(project_id or "").strip()
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _reconcile(_read_store())
            job = None
            for item in data.get("jobs") or []:
                if queue_id and str(item.get("id")) == str(queue_id):
                    job = item
                    break
                if not queue_id and str(item.get("project_id") or "") == pid and item.get("status") in _ACTIVE:
                    job = item
                    break
            if not job:
                return None
            global _last_served_owner
            _last_served_owner = _owner_key(job.get("owner_id"))
            job["status"] = STATUS_RUNNING
            job["started_at"] = job.get("started_at") or _now()
            job["claimed_at"] = job.get("claimed_at") or _now()
            job["lease_expires_at"] = _lease_expiry_iso()
            job["next_retry_at"] = None
            job["updated_at"] = _now()
            job["detail"] = "Pipeline running."
            _write_store(data)
            _ensure_lease_thread()
            return _public_job(job, position=0)


def mark_finished(
    project_id: str,
    *,
    outcome: str = "done",
    detail: str = "",
    error: str = "",
) -> dict[str, Any] | None:
    pid = str(project_id or "").strip()
    outcome_n = (outcome or "done").strip().lower()
    if outcome_n in ("paused", "stopped"):
        status = STATUS_CANCELLED
    elif outcome_n == "error":
        status = STATUS_FAILED
    else:
        status = STATUS_DONE
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _reconcile(_read_store())
            job = None
            for item in data.get("jobs") or []:
                if str(item.get("project_id") or "") == pid and item.get("status") == STATUS_RUNNING:
                    job = item
                    break
            if not job:
                for item in data.get("jobs") or []:
                    if str(item.get("project_id") or "") == pid and item.get("status") == STATUS_QUEUED:
                        job = item
                        break
            if not job:
                return None
            err_text = error or (detail if status == STATUS_FAILED else "") or ""
            if status == STATUS_FAILED:
                attempts = int(job.get("attempts") or 0) + 1
                job["attempts"] = attempts
                max_a = int(job.get("max_attempts") or max_job_attempts())
                job["max_attempts"] = max_a
                from studio.job_errors import classify_error

                job["error_code"] = classify_error(err_text)
                job["error"] = err_text or None
                if attempts < max_a and is_transient_failure(err_text):
                    delay = _retry_delay_sec(attempts)
                    job["status"] = STATUS_QUEUED
                    job["kind"] = "resume"
                    job["finished_at"] = None
                    job["started_at"] = None
                    job["claimed_at"] = None
                    job["lease_expires_at"] = None
                    job["next_retry_at"] = (_utcnow() + timedelta(seconds=delay)).replace(
                        microsecond=0
                    ).isoformat()
                    job["detail"] = (
                        f"Transient failure — retry {attempts}/{max_a} in {delay}s. {err_text}"
                    )[:500]
                    job["updated_at"] = _now()
                    _write_store(data)
                    return _public_job(job)
                job["status"] = STATUS_FAILED
                job["finished_at"] = _now()
                job["detail"] = detail or err_text or "failed"
                job["lease_expires_at"] = None
                job["updated_at"] = _now()
                _write_store(data)
                return _public_job(job)

            job["status"] = status
            job["finished_at"] = _now()
            job["lease_expires_at"] = None
            job["next_retry_at"] = None
            if outcome_n == "paused":
                job["detail"] = detail or "Paused — slot freed."
                job["error"] = None
                job["kind"] = "resume"
            elif outcome_n == "stopped":
                job["detail"] = detail or "Stopped — slot freed."
                job["error"] = None
            else:
                job["detail"] = detail or outcome_n
                job["error"] = None
                job["error_code"] = None
            job["updated_at"] = _now()
            _write_store(data)
            return _public_job(job)


def peek_next(n: int | None = None) -> list[dict[str, Any]]:
    limit = n if n is not None else max(1, slots_free())
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _reconcile(_read_store())
            selected = select_runnable(
                data["jobs"], limit=max(0, limit), last_owner=_last_served_owner
            )
            positions = _position_map(data["jobs"], last_owner=_last_served_owner)
            return [
                _public_job(j, position=positions.get(str(j.get("id"))))
                for j in selected
            ]


def list_queue(
    *,
    owner_id: str | None = None,
    is_admin: bool = False,
    include_history: bool = False,
) -> dict[str, Any]:
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _reconcile(_read_store())
            jobs = list(data.get("jobs") or [])
            positions = _position_map(jobs, last_owner=_last_served_owner)
            public = [_public_job(j, position=positions.get(str(j.get("id")))) for j in jobs]
    if not is_admin and owner_id:
        uid = str(owner_id).strip()
        public = [j for j in public if str(j.get("owner_id") or "") == uid]
    elif not is_admin and not owner_id:
        public = []
    active = [j for j in public if j.get("status") in _ACTIVE]
    history = [j for j in public if j.get("status") in _TERMINAL] if include_history else []
    queued = [j for j in active if j.get("status") == STATUS_QUEUED]
    running = [j for j in active if j.get("status") == STATUS_RUNNING]
    # Prefer live running ids so UI matches pipeline threads even if JSON lags.
    live = sorted(_live_running_ids())
    max_n = max_concurrent_jobs()
    return {
        "ok": True,
        "max_concurrent": max_n,
        "render_workers": max_n,
        "per_user_concurrency": per_user_concurrency(),
        "admin_concurrency": admin_concurrency(),
        "owner_priority": owner_priority_enabled(),
        "running_count": len(live),
        "queued_count": len(queued),
        "slots_free": max(0, max_n - len(live)),
        "running_project_ids": live,
        "jobs": active + (history[:40] if include_history else []),
        "queued": queued,
        "running": running,
        "path": str(QUEUE_PATH),
        "fairness": "owner_priority_round_robin" if owner_priority_enabled() else "round_robin",
    }


def queue_info_for_project(project_id: str) -> dict[str, Any] | None:
    pid = str(project_id or "").strip()
    snap = list_queue(is_admin=True, include_history=True)
    for job in snap.get("jobs") or []:
        if str(job.get("project_id") or "") == pid and job.get("status") in _ACTIVE:
            return job
    return None


def public_status() -> dict[str, Any]:
    snap = list_queue(is_admin=True)
    return {
        "max_concurrent": snap["max_concurrent"],
        "render_workers": snap.get("render_workers", snap["max_concurrent"]),
        "per_user_concurrency": snap.get("per_user_concurrency"),
        "admin_concurrency": snap.get("admin_concurrency"),
        "owner_priority": snap.get("owner_priority"),
        "running_count": snap["running_count"],
        "queued_count": snap["queued_count"],
        "slots_free": snap["slots_free"],
        "running_project_ids": snap["running_project_ids"],
        "fairness": snap.get("fairness") or "round_robin",
    }


def _resolve_owner(project_id: str, owner_id: str | None) -> tuple[str | None, str]:
    title = ""
    label = ""
    oid = (owner_id or "").strip() or None
    try:
        from studio.projects import load_meta

        meta = load_meta(project_id)
        title = str(meta.get("title") or meta.get("topic") or "").strip()
        if not oid:
            oid = str(meta.get("owner_id") or "").strip() or None
    except Exception:
        pass
    if oid:
        try:
            from studio.members import get_user_by_id

            user = get_user_by_id(oid)
            if user:
                label = str(user.get("username") or user.get("email") or oid)
        except Exception:
            label = oid
    return oid, title or label


def request_run(
    project_id: str,
    *,
    kind: str = "start",
    owner_id: str | None = None,
    wait_gpu: bool = True,
    topic_id: str = "",
) -> dict[str, Any]:
    """Start now if a slot is free; otherwise enqueue and return queue status.

    Never rejects solely because another user's job is running.
    """
    from studio.pipeline import is_busy, job_status, resume_project, start_project

    pid = str(project_id or "").strip()
    if is_busy(pid):
        st = job_status(pid)
        st["attached"] = True
        st["queued"] = False
        st["idempotent"] = True
        st["noop"] = True
        st["error_code"] = "already_running"
        st["detail"] = st.get("detail") or "Job already running; attached (no duplicate)."
        info = queue_info_for_project(pid)
        if info:
            st["queue"] = info
        return st

    # Already queued (not running) — refresh row, do not double-enqueue.
    existing_q = queue_info_for_project(pid)
    if existing_q and existing_q.get("status") == "queued":
        st = job_status(pid)
        st["started"] = False
        st["queued"] = True
        st["idempotent"] = True
        st["noop"] = True
        st["error_code"] = "already_queued"
        st["queue"] = existing_q
        st["queue_position"] = existing_q.get("queue_position")
        st["detail"] = (
            f"Already queued (position {existing_q.get('queue_position')}); "
            "not creating a duplicate queue entry."
        )
        return st

    oid, title = _resolve_owner(pid, owner_id)
    kind_n = (kind or "start").strip().lower()
    if kind_n not in ("start", "resume"):
        kind_n = "start"

    if capacity_full():
        q = enqueue(
            pid,
            owner_id=oid,
            kind=kind_n,
            title=title,
            topic_id=topic_id,
            owner_label=oid or "",
            detail="Waiting for a free pipeline slot.",
        )
        snap = public_status()
        st = job_status(pid)
        st["started"] = False
        st["queued"] = True
        st["queue"] = q
        st["queue_position"] = q.get("queue_position")
        st["detail"] = (
            f"Queued (position {q.get('queue_position')}). "
            f"{snap['running_count']} running / {snap['queued_count']} waiting "
            f"(max {snap['max_concurrent']})."
        )
        st["max_concurrent"] = snap["max_concurrent"]
        return st

    # Reserve a queue row as running, then launch.
    q = enqueue(
        pid,
        owner_id=oid,
        kind=kind_n,
        title=title,
        topic_id=topic_id,
        detail="Starting…",
    )
    mark_running(pid, queue_id=str(q.get("id") or ""))
    try:
        if kind_n == "resume":
            result = resume_project(pid, wait_gpu=wait_gpu)
        else:
            result = start_project(pid, wait_gpu=wait_gpu)
    except Exception as exc:
        mark_finished(pid, outcome="error", detail=str(exc), error=str(exc))
        raise
    if result.get("gpu_lock") and not (result.get("running") or result.get("busy")):
        # Could not start — keep queued for pump retry.
        with _lock:
            with _file_mutex(QUEUE_MUTEX_PATH):
                data = _reconcile(_read_store())
                for item in data.get("jobs") or []:
                    if str(item.get("project_id") or "") == pid and item.get("status") == STATUS_RUNNING:
                        item["status"] = STATUS_QUEUED
                        item["started_at"] = None
                        item["detail"] = result.get("detail") or "GPU busy — waiting for slot."
                        item["updated_at"] = _now()
                _write_store(data)
        result["queued"] = True
        result["started"] = False
        info = queue_info_for_project(pid)
        if info:
            result["queue"] = info
            result["queue_position"] = info.get("queue_position")
        return result
    if result.get("attached") or result.get("running") or result.get("busy"):
        result["queued"] = False
        result["started"] = True
        result["queue"] = queue_info_for_project(pid)
        return result
    # Already done / no-op start
    mark_finished(pid, outcome="done", detail=result.get("detail") or "Already complete.")
    result["queued"] = False
    return result


def pump(*, limit: int | None = None) -> dict[str, Any]:
    """Start up to ``slots_free()`` (or ``limit``) queued jobs. Safe to call often.

    Launches directly via start/resume_project — must not call request_run(), which
    no-ops on already-queued rows and would leave reboot re-queues stuck forever.
    """
    from studio.pipeline import resume_project, start_project

    started: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not _pump_lock.acquire(blocking=False):
        return {"started": [], "skipped": [], "reason": "pump_busy", **public_status()}
    try:
        free = slots_free()
        if limit is not None:
            free = min(free, max(0, int(limit)))
        if free <= 0:
            return {"started": [], "skipped": [], "reason": "full", **public_status()}
        candidates = peek_next(free)
        for item in candidates:
            if slots_free() <= 0:
                break
            pid = str(item.get("project_id") or "")
            if not pid:
                continue
            qid = str(item.get("id") or "")
            if pid in _live_running_ids():
                mark_running(pid, queue_id=qid)
                continue
            kind = str(item.get("kind") or "start").strip().lower()
            if kind not in ("start", "resume"):
                kind = "start"
            mark_running(pid, queue_id=qid)
            try:
                if kind == "resume":
                    result = resume_project(pid, wait_gpu=False)
                else:
                    result = start_project(pid, wait_gpu=False)
            except Exception as exc:
                mark_finished(pid, outcome="error", detail=str(exc), error=str(exc))
                skipped.append({"project_id": pid, "error": str(exc)})
                continue
            if result.get("gpu_lock") and not (result.get("running") or result.get("busy") or result.get("attached")):
                # GPU busy — put back on the queue for a later pump.
                with _lock:
                    with _file_mutex(QUEUE_MUTEX_PATH):
                        data = _reconcile(_read_store())
                        for row in data.get("jobs") or []:
                            if str(row.get("project_id") or "") == pid and row.get("status") == STATUS_RUNNING:
                                row["status"] = STATUS_QUEUED
                                row["started_at"] = None
                                row["detail"] = result.get("detail") or "GPU busy — waiting for slot."
                                row["updated_at"] = _now()
                        _write_store(data)
                skipped.append({"project_id": pid, "reason": "gpu_busy"})
                continue
            if result.get("started") or result.get("running") or result.get("busy") or result.get("attached"):
                started.append({"project_id": pid, "job": result})
            else:
                mark_finished(pid, outcome="done", detail=result.get("detail") or "Already complete.")
                skipped.append({"project_id": pid, "reason": result.get("detail") or "not_started"})
        return {"started": started, "skipped": skipped, "reason": "ok", **public_status()}
    finally:
        _pump_lock.release()


def on_pipeline_finished(project_id: str, outcome: str, detail: str = "") -> None:
    """Hook from pipeline worker finally-block: free the slot and start the next."""
    try:
        mark_finished(project_id, outcome=outcome, detail=detail, error=detail if outcome == "error" else "")
    except Exception:
        pass
    try:
        pump()
    except Exception:
        pass


def heartbeat(project_id: str, *, checkpoint: dict[str, Any] | None = None) -> None:
    """Extend the lease for a live worker; optionally persist a checkpoint blob."""
    pid = str(project_id or "").strip()
    if not pid:
        return
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _read_store()
            for job in data.get("jobs") or []:
                if str(job.get("project_id") or "") != pid:
                    continue
                if job.get("status") not in (STATUS_RUNNING, STATUS_CLAIMED):
                    continue
                job["lease_expires_at"] = _lease_expiry_iso()
                job["updated_at"] = _now()
                if checkpoint and isinstance(checkpoint, dict):
                    job["checkpoint"] = dict(checkpoint)
                    if checkpoint.get("step"):
                        job["step"] = checkpoint.get("step")
                    if checkpoint.get("progress_pct") is not None:
                        job["progress_pct"] = checkpoint.get("progress_pct")
                    if checkpoint.get("detail"):
                        job["detail"] = str(checkpoint.get("detail"))[:400]
                _write_store(data)
                return


def sync_job_progress(
    project_id: str,
    *,
    step: str | None = None,
    detail: str | None = None,
    progress_pct: Any = None,
    checkpoint: dict[str, Any] | None = None,
) -> None:
    """Mirror live pipeline progress into the durable queue row + refresh lease."""
    blob: dict[str, Any] = {}
    if step is not None:
        blob["step"] = step
    if detail is not None:
        blob["detail"] = detail
    if progress_pct is not None:
        blob["progress_pct"] = progress_pct
    if checkpoint:
        blob.update(checkpoint)
        blob["step"] = checkpoint.get("step") or step
    heartbeat(project_id, checkpoint=blob or None)


def _lease_loop() -> None:
    while not _lease_stop.wait(DEFAULT_HEARTBEAT_SEC):
        try:
            live = _live_running_ids()
            with _lock:
                with _file_mutex(QUEUE_MUTEX_PATH):
                    data = _reconcile(_read_store(), boot=False)
                    changed = False
                    for job in data.get("jobs") or []:
                        pid = str(job.get("project_id") or "")
                        if job.get("status") == STATUS_RUNNING and pid in live:
                            job["lease_expires_at"] = _lease_expiry_iso()
                            job["updated_at"] = _now()
                            changed = True
                    if changed:
                        _write_store(data)
            # Re-pump if anything became eligible after retry backoff / lease expiry
            pump()
        except Exception:
            continue


def _ensure_lease_thread() -> None:
    global _lease_thread
    if _lease_thread and _lease_thread.is_alive():
        return
    _lease_stop.clear()
    _lease_thread = threading.Thread(target=_lease_loop, name="bubblepod-job-lease", daemon=True)
    _lease_thread.start()


def ensure_queue_boot() -> None:
    """Reconcile persisted queue on Studio startup and try to fill free slots."""
    try:
        with _lock:
            with _file_mutex(QUEUE_MUTEX_PATH):
                _reconcile(_read_store(), boot=True)
    except Exception:
        pass
    _ensure_lease_thread()
    try:
        pump()
    except Exception:
        pass
