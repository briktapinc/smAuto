"""Multi-user pipeline job queue with configurable concurrency.

Persists to ``user_data/job_queue.json`` so restarts keep queued work.
Ordering defaults to round-robin across ``owner_id`` (fair-ish); FIFO within
each user. Capacity is ``BUBBLEPOD_MAX_CONCURRENT_JOBS`` /
``STICKMAN_MAX_CONCURRENT_JOBS`` (default 1), optionally overridden by the
admin setting ``max_concurrent_jobs`` in settings.json.

Desktop / single-user mode keeps working: with one user and a free slot,
start still runs immediately; when at capacity, the same queue waits.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from studio.paths import USER_DATA, ensure_dirs

QUEUE_PATH = USER_DATA / "job_queue.json"
QUEUE_MUTEX_PATH = USER_DATA / "job_queue.mutex"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_ACTIVE = frozenset({STATUS_QUEUED, STATUS_RUNNING})
_TERMINAL = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED})

_lock = threading.RLock()
_pump_lock = threading.Lock()
_last_served_owner: str = ""
_HISTORY_LIMIT = 80


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


def _reconcile(data: dict[str, Any]) -> dict[str, Any]:
    """Align persisted running rows with live worker threads after restart."""
    live = _live_running_ids()
    changed = False
    for job in data.get("jobs") or []:
        status = str(job.get("status") or "")
        pid = str(job.get("project_id") or "")
        if status == STATUS_RUNNING and pid and pid not in live:
            job["status"] = STATUS_QUEUED
            job["detail"] = "Re-queued after Studio restart."
            job["started_at"] = None
            job["updated_at"] = _now()
            changed = True
        elif status == STATUS_QUEUED and pid and pid in live:
            job["status"] = STATUS_RUNNING
            job["started_at"] = job.get("started_at") or _now()
            job["updated_at"] = _now()
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


def _queue_order(jobs: list[dict[str, Any]], *, last_owner: str = "") -> list[dict[str, Any]]:
    """Round-robin across owners; FIFO (enqueued_at) within each owner."""
    queued = [j for j in jobs if j.get("status") == STATUS_QUEUED]
    if not queued:
        return []
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for job in queued:
        key = _owner_key(job.get("owner_id"))
        by_owner.setdefault(key, []).append(job)
    for group in by_owner.values():
        group.sort(key=lambda j: (str(j.get("enqueued_at") or ""), str(j.get("id") or "")))
    owners = sorted(by_owner.keys())
    if last_owner and last_owner in owners:
        i = owners.index(last_owner)
        owners = owners[i + 1 :] + owners[: i + 1]
    ordered: list[dict[str, Any]] = []
    while any(by_owner[o] for o in owners):
        for owner in owners:
            bucket = by_owner[owner]
            if bucket:
                ordered.append(bucket.pop(0))
    return ordered


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
        "updated_at": job.get("updated_at"),
    }
    if position is not None and job.get("status") == STATUS_QUEUED:
        out["queue_position"] = position
    elif job.get("status") == STATUS_RUNNING:
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
            job["updated_at"] = _now()
            job["detail"] = "Pipeline running."
            _write_store(data)
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
            job["status"] = status
            job["finished_at"] = _now()
            if outcome_n == "paused":
                job["detail"] = detail or "Paused — slot freed."
                job["error"] = None
            elif outcome_n == "stopped":
                job["detail"] = detail or "Stopped — slot freed."
                job["error"] = None
            else:
                job["detail"] = detail or outcome_n
                job["error"] = error or (detail if status == STATUS_FAILED else None)
            job["updated_at"] = _now()
            _write_store(data)
            return _public_job(job)


def peek_next(n: int | None = None) -> list[dict[str, Any]]:
    limit = n if n is not None else max(1, slots_free())
    with _lock:
        with _file_mutex(QUEUE_MUTEX_PATH):
            data = _reconcile(_read_store())
            ordered = _queue_order(data["jobs"], last_owner=_last_served_owner)
            return [_public_job(j, position=i + 1) for i, j in enumerate(ordered[: max(0, limit)])]


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
        "running_count": len(live),
        "queued_count": len(queued),
        "slots_free": max(0, max_n - len(live)),
        "running_project_ids": live,
        "jobs": active + (history[:40] if include_history else []),
        "queued": queued,
        "running": running,
        "path": str(QUEUE_PATH),
        "fairness": "round_robin",
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
        "running_count": snap["running_count"],
        "queued_count": snap["queued_count"],
        "slots_free": snap["slots_free"],
        "running_project_ids": snap["running_project_ids"],
        "fairness": "round_robin",
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
        info = queue_info_for_project(pid)
        if info:
            st["queue"] = info
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
    """Start up to ``slots_free()`` (or ``limit``) queued jobs. Safe to call often."""
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
            if pid in _live_running_ids():
                mark_running(pid, queue_id=str(item.get("id") or ""))
                continue
            kind = str(item.get("kind") or "start")
            try:
                result = request_run(
                    pid,
                    kind=kind,
                    owner_id=item.get("owner_id"),
                    wait_gpu=False,
                    topic_id=str(item.get("topic_id") or ""),
                )
            except Exception as exc:
                mark_finished(pid, outcome="error", detail=str(exc), error=str(exc))
                skipped.append({"project_id": pid, "error": str(exc)})
                continue
            if result.get("started") or result.get("running") or result.get("busy") or result.get("attached"):
                started.append({"project_id": pid, "job": result})
            elif result.get("queued"):
                skipped.append({"project_id": pid, "reason": "still_queued"})
            else:
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


def ensure_queue_boot() -> None:
    """Reconcile persisted queue on Studio startup and try to fill free slots."""
    try:
        with _lock:
            with _file_mutex(QUEUE_MUTEX_PATH):
                _reconcile(_read_store())
    except Exception:
        pass
    try:
        pump()
    except Exception:
        pass
