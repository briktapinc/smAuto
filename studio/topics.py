"""Topic generator + FIFO pipeline queue. Persisted in user_data/topics.json."""

from __future__ import annotations

import json
import logging
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from studio.paths import TOPICS_PATH, ensure_dirs
from studio.prompts import get_prompt

_lock = threading.Lock()
_log = logging.getLogger("bubblepod.topics")
STATUSES = ("draft", "queued", "running", "done")
DEFAULT_COUNT = 8
DEFAULT_DURATION_MIN = 5
SCHEDULER_INTERVAL_SEC = 30
# Auto-schedule batch gap: at least 1h (hands_off_interval_hours if larger).
MIN_SCHEDULE_STAGGER = timedelta(hours=1)
# When a due topic cannot start because GPU/pipeline is busy.
GPU_BUSY_RESCHEDULE_MIN_MINUTES = 60
GPU_BUSY_RESCHEDULE_MAX_MINUTES = 120


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_tzinfo():
    return datetime.now().astimezone().tzinfo


def local_timezone_name() -> str:
    now = datetime.now().astimezone()
    key = getattr(now.tzinfo, "key", None)
    if key:
        return str(key)
    name = now.tzname() or ""
    stamp = now.strftime("%z") or ""
    if stamp:
        pretty = f"UTC{stamp[:3]}:{stamp[3:]}" if len(stamp) >= 5 else f"UTC{stamp}"
        return f"{name} ({pretty})".strip() if name else pretty
    return name or "local"


def parse_scheduled_at(value: str | None, *, default_now: bool = False) -> datetime | None:
    """Parse ISO / datetime-local into UTC. Naive values are this machine's local time."""
    raw = str(value or "").strip()
    if not raw:
        if default_now:
            return datetime.now(timezone.utc)
        return None
    cleaned = raw.replace("Z", "+00:00")
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        raise RuntimeError(
            f"Invalid scheduled_at {value!r}. Use an ISO datetime "
            "(e.g. 2026-09-16T18:30 or 2026-09-16T18:30:00-04:00)."
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tzinfo())
    return dt.astimezone(timezone.utc)


def store_scheduled_at(value: str | datetime | None, *, default_now: bool = False) -> str | None:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=_local_tzinfo())
        return dt.astimezone(timezone.utc).isoformat()
    dt = parse_scheduled_at(value if isinstance(value, str) else None, default_now=default_now)
    return dt.isoformat() if dt else None


def _as_utc(value: str | None) -> datetime | None:
    try:
        return parse_scheduled_at(value)
    except RuntimeError:
        return None


def auto_scheduler_enabled() -> bool:
    from studio.settings import load_settings, normalize_auto_scheduler

    return normalize_auto_scheduler(load_settings().get("auto_scheduler", True))


def hands_off_enabled(owner_id: str | None = None) -> bool:
    """Per-member hands-off. With no owner_id, true if any member has it on."""
    from studio.members import list_hands_off_owner_ids, user_hands_off_enabled

    uid = (owner_id or "").strip()
    if uid:
        return user_hands_off_enabled(uid)
    return bool(list_hands_off_owner_ids())


def _hands_off_interval(owner_id: str | None = None) -> timedelta:
    from studio.members import hands_off_prefs
    from studio.settings import normalize_hands_off_interval_hours

    hours = normalize_hands_off_interval_hours(
        hands_off_prefs(owner_id).get("hands_off_interval_hours")
    )
    return timedelta(hours=hours)


def _hands_off_stagger(owner_id: str | None = None) -> timedelta:
    """Gap between auto-scheduled drafts. Interval 0 still means first can be due-now, but
    batch mates and follow-ups are always at least 1 hour apart."""
    interval = _hands_off_interval(owner_id)
    if interval < MIN_SCHEDULE_STAGGER:
        return MIN_SCHEDULE_STAGGER
    return interval


def _gpu_busy_reschedule_delta() -> timedelta:
    minutes = random.randint(GPU_BUSY_RESCHEDULE_MIN_MINUTES, GPU_BUSY_RESCHEDULE_MAX_MINUTES)
    return timedelta(minutes=minutes)


def _hands_off_min_queue(owner_id: str | None = None) -> int:
    from studio.members import hands_off_prefs
    from studio.settings import normalize_hands_off_min_queue

    return normalize_hands_off_min_queue(hands_off_prefs(owner_id).get("hands_off_min_queue"))


def _empty() -> dict[str, Any]:
    return {"topics": [], "queue": []}


def _load() -> dict[str, Any]:
    ensure_dirs()
    if not TOPICS_PATH.is_file():
        return _empty()
    try:
        data = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    topics = data.get("topics")
    queue = data.get("queue")
    if not isinstance(topics, list):
        topics = []
    if not isinstance(queue, list):
        queue = []
    cleaned = []
    for item in topics:
        if isinstance(item, dict) and item.get("id"):
            cleaned.append(item)
    ids = {str(t["id"]) for t in cleaned}
    return {
        "topics": cleaned,
        "queue": [str(tid) for tid in queue if str(tid) in ids],
    }


def _save(data: dict[str, Any]) -> None:
    ensure_dirs()
    TOPICS_PATH.write_text(
        json.dumps({"topics": data.get("topics") or [], "queue": data.get("queue") or []}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def _find(data: dict[str, Any], topic_id: str) -> dict[str, Any] | None:
    tid = str(topic_id or "").strip()
    for item in data.get("topics") or []:
        if str(item.get("id")) == tid:
            return item
    return None


def _duration_seconds(duration_min: float | int | None) -> int:
    minutes = float(duration_min if duration_min not in (None, "") else DEFAULT_DURATION_MIN)
    seconds = int(round(minutes * 60))
    return max(15, min(1800, seconds))


def _duration_min(value: float | int | None) -> float:
    try:
        minutes = float(value if value not in (None, "") else DEFAULT_DURATION_MIN)
    except (TypeError, ValueError):
        minutes = float(DEFAULT_DURATION_MIN)
    return max(0.25, min(30.0, minutes))


def _public(item: dict[str, Any]) -> dict[str, Any]:
    minutes = _duration_min(item.get("duration_min"))
    status = item.get("status") if item.get("status") in STATUSES else "draft"
    due_dt = _as_utc(item.get("scheduled_at"))
    now = datetime.now(timezone.utc)
    due = bool(status == "queued" and due_dt and due_dt <= now)
    local = due_dt.astimezone(_local_tzinfo()) if due_dt else None
    return {
        "id": item.get("id"),
        "owner_id": item.get("owner_id") or None,
        "title": item.get("title") or "",
        "angle": item.get("angle") or "",
        "duration_min": minutes,
        "duration_seconds": _duration_seconds(minutes),
        "status": status,
        "job_id": item.get("job_id") or None,
        "created_at": item.get("created_at"),
        "scheduled_at": item.get("scheduled_at"),
        "scheduled_at_local": local.isoformat() if local else None,
        "due": due,
        "due_in_seconds": int((due_dt - now).total_seconds()) if due_dt and status == "queued" else None,
        "error": item.get("error") or None,
    }


def _pipeline_busy() -> bool:
    from studio.pipeline import any_pipeline_busy

    return bool(any_pipeline_busy())


def _queue_hold_reason(data: dict[str, Any] | None = None) -> str | None:
    from studio.pipeline import scheduler_blocked_reason

    blocked = scheduler_blocked_reason()
    if blocked:
        return blocked
    return None


def _script_ready(job_id: str) -> bool:
    """True when the tagged script file exists — avoid full inspect_artifacts."""
    try:
        from pathlib import Path

        from studio.projects import input_prefix

        tagged = Path(input_prefix(job_id)).with_suffix(".txt")
        return tagged.is_file() and tagged.stat().st_size >= 20
    except Exception:
        return False


def _prepare_unsupervised_job(job_id: str) -> list[str]:
    """Fail chatgpt pictures (or fall back to flux). Native text needs save_script first."""
    from studio.comfyui import workflow_public_status
    from studio.projects import project_image_provider, set_image_provider
    from studio.settings import is_native_text_provider, load_settings, text_provider_label

    notes: list[str] = []
    settings = load_settings()
    provider = project_image_provider(job_id)
    fal_key = bool((settings.get("fal_key") or "").strip())
    if provider == "chatgpt":
        if fal_key:
            set_image_provider(job_id, "flux")
            notes.append(
                "image_provider chatgpt cannot run unsupervised; this job fell back to flux."
            )
        else:
            raise RuntimeError(
                "image_provider is chatgpt, which cannot generate pictures unsupervised "
                "(ChatGPT must click Pictures). Set image_provider to flux (needs fal_key) "
                "or comfyui, then schedule again."
            )
    elif provider == "flux" and not fal_key:
        raise RuntimeError(
            "image_provider is flux but fal_key is not set. Add a fal key in Settings "
            "or switch to comfyui for a walk-away run."
        )
    elif provider == "comfyui":
        wf = workflow_public_status()
        if not wf.get("loaded"):
            raise RuntimeError(
                "image_provider is comfyui but no API workflow is uploaded. "
                "Upload a ComfyUI Save (API Format) JSON in Settings first."
            )
    if is_native_text_provider() and not _script_ready(job_id):
        notes.append(
            f"text_provider is {text_provider_label()}. Studio will not auto-write the script. "
            "Call save_script on this job_id in the same turn, then the scheduler can run "
            "pictures → audio → Gentle → render → optional YouTube."
        )
    if hands_off_enabled():
        notes.extend(_apply_hands_off_youtube(job_id))
    return notes


def _apply_hands_off_youtube(job_id: str) -> list[str]:
    """Per-job private auto-upload while hands-off. Does not change Settings defaults."""
    from studio.projects import load_meta, save_meta, set_project_youtube
    from studio.youtube import is_connected

    notes: list[str] = []
    set_project_youtube(job_id, auto_upload=True, privacy="private")
    notes.append("hands-off: this job will auto-upload as private (settings default unchanged).")
    if not is_connected():
        meta = load_meta(job_id)
        meta["youtube_pending"] = True
        meta["youtube_error"] = None
        save_meta(job_id, meta)
        notes.append("YouTube is not connected; upload marked pending after render.")
    return notes


def _next_hands_off_slot(data: dict[str, Any]) -> datetime:
    """Next free auto-schedule slot: due-now if nothing queued, else max(now, last) + stagger."""
    now = datetime.now(timezone.utc)
    stagger = _hands_off_stagger()
    latest: datetime | None = None
    for topic in data.get("topics") or []:
        if topic.get("status") not in ("queued", "running"):
            continue
        due = _as_utc(topic.get("scheduled_at"))
        if due and (latest is None or due > latest):
            latest = due
    if latest is None:
        return now
    return max(now, latest) + stagger


def _bump_topic_scheduled_at(
    topic: dict[str, Any],
    *,
    reason: str,
    delay: timedelta | None = None,
) -> dict[str, Any]:
    """Push a queued topic's scheduled_at into the future; keep status queued."""
    delta = delay or _gpu_busy_reschedule_delta()
    when = datetime.now(timezone.utc) + delta
    topic["scheduled_at"] = when.isoformat()
    topic["error"] = None
    detail = (
        f"Rescheduled +{int(delta.total_seconds() // 60)}m ({reason}); "
        f"new due {when.isoformat()}"
    )
    _log.info("topic %s: %s", topic.get("id"), detail)
    return {
        "scheduled_at": topic["scheduled_at"],
        "delay_minutes": int(delta.total_seconds() // 60),
        "detail": detail,
        "reason": reason,
    }


def _due_candidates(data: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    moment = now or datetime.now(timezone.utc)
    queue_index = {str(tid): i for i, tid in enumerate(data.get("queue") or [])}
    rows: list[tuple[datetime, int, dict[str, Any]]] = []
    for topic in data.get("topics") or []:
        if topic.get("status") != "queued":
            continue
        tid = str(topic.get("id") or "")
        due_dt = _as_utc(topic.get("scheduled_at")) or datetime.min.replace(tzinfo=timezone.utc)
        if due_dt > moment:
            continue
        rows.append((due_dt, queue_index.get(tid, 10_000), topic))
    rows.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in rows]


def _next_waiting(data: dict[str, Any], *, future_only: bool = True) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    waiting: list[tuple[datetime, dict[str, Any]]] = []
    for topic in data.get("topics") or []:
        if topic.get("status") != "queued":
            continue
        due_dt = _as_utc(topic.get("scheduled_at"))
        if not due_dt:
            continue
        if future_only and due_dt <= now:
            continue
        waiting.append((due_dt, topic))
    if not waiting:
        return None
    waiting.sort(key=lambda row: row[0])
    return waiting[0][1]


def _catalog_extras(data: dict[str, Any], topics: list[dict[str, Any]]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    due = [t for t in topics if t.get("due")]
    waiting = _next_waiting(data, future_only=False)
    waiting_pub = _public(waiting) if waiting else None
    from studio.settings import load_settings, normalize_hands_off_interval_hours, normalize_hands_off_min_queue

    settings = load_settings()
    return {
        "auto_scheduler": auto_scheduler_enabled(),
        "hands_off": hands_off_enabled(),
        "hands_off_interval_hours": normalize_hands_off_interval_hours(settings.get("hands_off_interval_hours")),
        "hands_off_min_queue": normalize_hands_off_min_queue(settings.get("hands_off_min_queue")),
        "scheduler_interval_sec": SCHEDULER_INTERVAL_SEC,
        "timezone": local_timezone_name(),
        "due_count": len(due),
        "next_due_at": waiting_pub.get("scheduled_at") if waiting_pub else None,
        "next_due_at_local": waiting_pub.get("scheduled_at_local") if waiting_pub else None,
        "next_due_topic": waiting_pub,
        "now": now.isoformat(),
    }


def _sync_from_jobs(data: dict[str, Any]) -> None:
    from studio.pipeline import job_status_lite

    queue = [str(x) for x in data.get("queue") or []]
    for topic in data.get("topics") or []:
        job_id = (topic.get("job_id") or "").strip()
        if not job_id:
            if topic.get("status") == "running":
                topic["status"] = "queued" if topic.get("id") in queue else "draft"
            continue
        try:
            st = job_status_lite(job_id)
        except FileNotFoundError:
            if topic.get("status") in ("running", "queued"):
                topic["status"] = "draft"
            continue
        except Exception:
            continue
        step = (st.get("job") or {}).get("step") or st.get("step")
        if st.get("running") or st.get("busy"):
            topic["status"] = "running"
            topic["error"] = None
        elif topic.get("status") == "running":
            if step == "done":
                topic["status"] = "done"
                topic["error"] = None
                if topic.get("id") in queue:
                    queue = [x for x in queue if x != topic.get("id")]
            elif step == "paused":
                # Pause holds the queue: this topic stays current, others wait.
                topic["status"] = "running"
            else:
                topic["status"] = "draft"
                if step == "error":
                    topic["error"] = st.get("error") or (st.get("job") or {}).get("error")
        elif topic.get("status") == "queued" and step == "done":
            topic["status"] = "done"
            queue = [x for x in queue if x != topic.get("id")]
        elif topic.get("status") != "done" and step == "done" and topic.get("job_id"):
            topic["status"] = "done"
            queue = [x for x in queue if x != topic.get("id")]
    data["queue"] = queue


def _job_text(title: str, angle: str) -> str:
    title = (title or "").strip()
    angle = (angle or "").strip()
    if angle:
        return f"{title}\n\nAngle: {angle}"
    return title


def _make_topic(
    title: str,
    angle: str = "",
    duration_min: float | int | None = None,
    status: str = "draft",
    *,
    owner_id: str | None = None,
) -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise RuntimeError("Topic title is required.")
    minutes = _duration_min(duration_min)
    return {
        "id": uuid.uuid4().hex[:12],
        "owner_id": (owner_id or "").strip() or None,
        "title": title,
        "angle": (angle or "").strip(),
        "duration_min": minutes,
        "status": status if status in STATUSES else "draft",
        "job_id": None,
        "created_at": _now(),
        "scheduled_at": None,
        "error": None,
    }


def _invent_topics(seed: str, count: int, duration_min: float) -> list[dict[str, str]]:
    from studio.textgen import chat_json

    user = get_prompt(
        "topics.generate",
        seed=(seed or "").strip() or "(none — invent a varied mix)",
        count=count,
        duration_min=duration_min,
    )
    data, _model, _provider = chat_json(
        temperature=0.9,
        messages=[
            {
                "role": "system",
                "content": (
                    "You invent original YouTube explainer topics. "
                    "Return ONLY a JSON object with key topics: an array of "
                    "{title, angle} objects. Titles are short and specific. "
                    "Angle is one sentence: the unique take, not a restatement of the title. "
                    "No markdown fences, no trailing commas, no commentary."
                ),
            },
            {"role": "user", "content": user},
        ],
    )
    rows = data.get("topics") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise RuntimeError("Topic generator did not return a topics array.")
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or row.get("topic") or "").strip()
        angle = str(row.get("angle") or row.get("hook") or row.get("summary") or "").strip()
        if title:
            out.append({"title": title, "angle": angle})
        if len(out) >= count:
            break
    if not out:
        raise RuntimeError("Topic generator returned no titles.")
    return out


def generate_topics(
    seed: str = "",
    count: int | None = None,
    duration_min: float | int | None = None,
    *,
    owner_id: str | None = None,
) -> dict[str, Any]:
    n = int(count if count not in (None, "") else DEFAULT_COUNT)
    n = max(1, min(20, n))
    minutes = _duration_min(duration_min)
    from studio.settings import is_native_text_provider
    from studio.textgen import native_topics_handoff

    if is_native_text_provider():
        payload = list_topics(owner_id=owner_id, is_admin=not owner_id)
        payload.update(native_topics_handoff((seed or "").strip(), n, minutes))
        payload["created"] = []
        payload["count"] = 0
        return payload
    invented = _invent_topics((seed or "").strip(), n, minutes)
    created: list[dict[str, Any]] = []
    with _lock:
        data = _load()
        for row in invented:
            topic = _make_topic(row["title"], row.get("angle") or "", minutes, owner_id=owner_id)
            data["topics"].insert(0, topic)
            created.append(topic)
        _save(data)
    payload = list_topics(owner_id=owner_id, is_admin=not bool(owner_id))
    payload["created"] = [_public(t) for t in created]
    payload["seed"] = (seed or "").strip()
    payload["count"] = len(created)
    payload["duration_min"] = minutes
    return payload


def create_topic(
    title: str,
    angle: str = "",
    duration_min: float | int | None = None,
    *,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """Save one draft topic without calling OpenAI. Used by ChatGPT/Claude MCP."""
    topic = _make_topic(title, angle, duration_min, owner_id=owner_id)
    with _lock:
        data = _load()
        data["topics"].insert(0, topic)
        _save(data)
        saved = _public(topic)
    payload = list_topics(owner_id=owner_id, is_admin=not bool(owner_id))
    payload["topic"] = saved
    payload["created"] = [saved]
    payload["count"] = 1
    return payload


def get_topic_raw(topic_id: str) -> dict[str, Any] | None:
    with _lock:
        data = _load()
        topic = _find(data, topic_id)
        return dict(topic) if topic else None


def assign_orphan_owners(admin_id: str) -> int:
    """Set owner_id on topics missing one. Returns count updated."""
    aid = (admin_id or "").strip()
    if not aid:
        return 0
    n = 0
    with _lock:
        data = _load()
        for topic in data.get("topics") or []:
            if str(topic.get("owner_id") or "").strip():
                continue
            topic["owner_id"] = aid
            n += 1
        if n:
            _save(data)
    return n


def list_topics(
    status: str | None = None,
    kick: bool = False,
    *,
    owner_id: str | None = None,
    is_admin: bool = False,
) -> dict[str, Any]:
    """List topics for UI / MCP.

    kick defaults False so GET /api/topics stays fast. Pass kick=True from
    mutation paths that should nudge the due-picker (hands-off still ticks ~30s).
    """
    wanted = (status or "").strip().lower()
    if wanted and wanted not in STATUSES:
        raise RuntimeError(f"Unknown status {status!r}. Use draft, queued, running, or done.")
    should_kick = bool(kick)
    with _lock:
        data = _load()
        _sync_from_jobs(data)
        _save(data)
        topics = [_public(t) for t in data["topics"]]
        queue = list(data["queue"])
        has_running = any(t.get("status") == "running" for t in data["topics"])
        hold = _queue_hold_reason(data)
        if (
            not queue
            or has_running
            or hold
            or not hands_off_enabled()
            or not auto_scheduler_enabled()
        ):
            should_kick = False
    if should_kick:
        kick_queue()
        with _lock:
            data = _load()
            _sync_from_jobs(data)
            _save(data)
            topics = [_public(t) for t in data["topics"]]
            queue = list(data["queue"])
    if not is_admin:
        uid = (owner_id or "").strip()
        topics = [t for t in topics if str(t.get("owner_id") or "").strip() == uid]
        owned_ids = {str(t.get("id")) for t in topics}
        queue = [q for q in queue if str(q) in owned_ids]
    if wanted:
        topics = [t for t in topics if t.get("status") == wanted]
    from studio.pipeline import running_project_ids

    extras = _catalog_extras(data, topics if not is_admin else [_public(t) for t in data["topics"]])
    return {
        "topics": topics,
        "queue": queue,
        "busy": _pipeline_busy(),
        "running_job_ids": running_project_ids(),
        "path": str(TOPICS_PATH),
        **extras,
    }


def update_topic(
    topic_id: str,
    title: str | None = None,
    angle: str | None = None,
    duration_min: float | int | None = None,
    scheduled_at: str | None = None,
) -> dict[str, Any]:
    with _lock:
        data = _load()
        topic = _find(data, topic_id)
        if not topic:
            raise FileNotFoundError(f"Unknown topic: {topic_id}")
        if title is not None:
            cleaned = title.strip()
            if not cleaned:
                raise RuntimeError("Topic title cannot be empty.")
            topic["title"] = cleaned
        if angle is not None:
            topic["angle"] = angle.strip()
        if duration_min is not None:
            topic["duration_min"] = _duration_min(duration_min)
        if scheduled_at is not None:
            topic["scheduled_at"] = store_scheduled_at(scheduled_at) if str(scheduled_at).strip() else None
        _save(data)
        saved = _public(topic)
    return {"topic": saved, **list_topics()}


def delete_topic(topic_id: str) -> dict[str, Any]:
    with _lock:
        data = _load()
        topic = _find(data, topic_id)
        if not topic:
            raise FileNotFoundError(f"Unknown topic: {topic_id}")
        data["topics"] = [t for t in data["topics"] if str(t.get("id")) != str(topic_id)]
        data["queue"] = [x for x in data["queue"] if x != str(topic_id)]
        _save(data)
    payload = list_topics()
    payload["deleted"] = str(topic_id)
    return payload


def unschedule_topic(topic_id: str) -> dict[str, Any]:
    """Clear scheduled_at and return a queued topic to draft. Keeps title/angle/job_id."""
    with _lock:
        data = _load()
        topic = _find(data, topic_id)
        if not topic:
            raise FileNotFoundError(f"Unknown topic: {topic_id}")
        status = topic.get("status") or "draft"
        if status == "running":
            raise RuntimeError("Cannot cancel schedule while this topic is running.")
        if status == "done":
            raise RuntimeError("This topic is already done; nothing to unschedule.")
        tid = str(topic_id)
        topic["scheduled_at"] = None
        topic["status"] = "draft"
        data["queue"] = [x for x in data["queue"] if x != tid]
        _save(data)
        saved = _public(topic)
    payload = list_topics()
    payload["topic"] = next((t for t in payload["topics"] if t["id"] == tid), saved)
    payload["unscheduled"] = tid
    return payload


def _ensure_job(topic: dict[str, Any]) -> str:
    from studio.projects import create_project, load_meta, save_meta

    minutes = _duration_min(topic.get("duration_min"))
    seconds = _duration_seconds(minutes)
    existing = (topic.get("job_id") or "").strip()
    if existing:
        try:
            meta = load_meta(existing)
            if int(meta.get("duration_seconds") or 0) != seconds:
                meta["duration_seconds"] = seconds
                save_meta(existing, meta)
            return existing
        except FileNotFoundError:
            topic["job_id"] = None
    project = create_project(
        _job_text(topic.get("title") or "", topic.get("angle") or ""),
        seconds,
        title=topic.get("title") or None,
        owner_id=topic.get("owner_id"),
    )
    job_id = project.get("id")
    if not job_id:
        raise RuntimeError("Failed to create a Studio job from this topic.")
    topic["job_id"] = job_id
    return job_id


def _requeue(topic_id: str, front: bool) -> None:
    with _lock:
        data = _load()
        tid = str(topic_id)
        topic = _find(data, tid)
        if not topic:
            return
        data["queue"] = [x for x in data["queue"] if x != tid]
        if front:
            data["queue"].insert(0, tid)
        else:
            data["queue"].append(tid)
        if topic.get("status") not in ("running", "done"):
            topic["status"] = "queued"
        _save(data)


def kick_queue(*, force: bool = False) -> dict[str, Any]:
    """Start due queued topics into free pipeline slots (up to max concurrent).

    Automatic kicks (force=False) require Hands-off. Run now passes force=True.
    When capacity is full, due topics stay queued (no schedule bump) until a slot frees.
    """
    from studio.job_queue import max_concurrent_jobs, public_status, request_run, slots_free
    from studio.pipeline import scheduler_blocked_reason
    from studio.settings import is_native_text_provider, text_provider_label

    if not force and not hands_off_enabled():
        return {"started": False, "reason": "hands_off_off"}
    if not force and not auto_scheduler_enabled():
        return {"started": False, "reason": "auto_scheduler_off"}

    started_list: list[dict[str, Any]] = []
    last_result: dict[str, Any] | None = None

    while slots_free() > 0:
        hold = scheduler_blocked_reason()
        with _lock:
            data = _load()
            _sync_from_jobs(data)
            topic_hold = _queue_hold_reason(data)
            due = _due_candidates(data)
            topic = due[0] if due else None
            if not topic:
                _save(data)
                if started_list:
                    break
                if topic_hold or hold:
                    return {"started": False, "reason": topic_hold or hold, **public_status()}
                waiting = _next_waiting(data)
                if waiting:
                    return {
                        "started": False,
                        "reason": "waiting",
                        "next_due_at": waiting.get("scheduled_at"),
                        "topic_id": waiting.get("id"),
                        **public_status(),
                    }
                return {"started": False, "reason": "empty", **public_status()}

            block = hold or topic_hold
            if block == "busy":
                _save(data)
                # Stay queued — multi-user capacity will free a slot later.
                return last_result or {
                    "started": False,
                    "reason": "waiting_for_slot",
                    "topic_id": str(topic.get("id")),
                    "job_id": topic.get("job_id"),
                    **public_status(),
                }
            if block:
                _save(data)
                return last_result or {"started": False, "reason": block, **public_status()}

            tid = str(topic["id"])
            data["queue"] = [x for x in data["queue"] if x != tid]
            try:
                job_id = _ensure_job(topic)
                notes = _prepare_unsupervised_job(job_id)
            except Exception as exc:
                topic["status"] = "draft"
                topic["error"] = str(exc)
                _save(data)
                continue
            if is_native_text_provider() and not _script_ready(job_id):
                topic["status"] = "queued"
                topic["error"] = (
                    f"Waiting for save_script ({text_provider_label()} cannot auto-generate). "
                    "Write the tagged script then save_script, or switch text_provider to openai/lmstudio."
                )
                if tid not in data["queue"]:
                    data["queue"].insert(0, tid)
                _save(data)
                return last_result or {
                    "started": False,
                    "reason": "needs_script",
                    "topic_id": tid,
                    "job_id": job_id,
                    "notes": notes,
                    **public_status(),
                }
            topic["status"] = "running"
            topic["error"] = None
            topic_id = tid
            owner_id = str(topic.get("owner_id") or "") or None
            _save(data)

        if scheduler_blocked_reason() == "busy":
            _requeue(topic_id, front=True)
            with _lock:
                data = _load()
                item = _find(data, topic_id)
                if item and item.get("status") == "running":
                    item["status"] = "queued"
                    _save(data)
            return last_result or {
                "started": False,
                "reason": "waiting_for_slot",
                "topic_id": topic_id,
                "job_id": job_id,
                **public_status(),
            }

        try:
            st = request_run(
                job_id,
                kind="start",
                owner_id=owner_id,
                wait_gpu=False,
                topic_id=topic_id,
            )
        except Exception as exc:
            with _lock:
                data = _load()
                item = _find(data, topic_id)
                if item:
                    item["status"] = "draft"
                    item["error"] = str(exc)
                    _save(data)
            continue

        if st.get("queued") and not (st.get("running") or st.get("busy")):
            with _lock:
                data = _load()
                item = _find(data, topic_id)
                if item:
                    item["status"] = "queued"
                    if topic_id not in data["queue"]:
                        data["queue"].insert(0, topic_id)
                    _save(data)
            last_result = {
                "started": False,
                "reason": "waiting_for_slot",
                "topic_id": topic_id,
                "job_id": job_id,
                "queue_position": st.get("queue_position"),
                **public_status(),
            }
            break

        if st.get("gpu_lock") and not (st.get("running") or st.get("busy")):
            # Leave due — retry next tick / when GPU frees; do not bump schedule.
            _requeue(topic_id, front=True)
            with _lock:
                data = _load()
                item = _find(data, topic_id)
                if item:
                    item["status"] = "queued"
                    _save(data)
            last_result = {
                "started": False,
                "reason": "gpu",
                "topic_id": topic_id,
                "job_id": job_id,
                "detail": st.get("detail"),
                **public_status(),
            }
            break

        live = bool(st.get("running") or st.get("busy") or st.get("started"))
        step = (st.get("job") or {}).get("step") or st.get("step")
        if live:
            entry = {
                "started": True,
                "reason": "started",
                "topic_id": topic_id,
                "job_id": job_id,
                "job": st,
            }
            started_list.append(entry)
            last_result = entry
            # Fill remaining slots when max concurrent > 1.
            if slots_free() <= 0:
                break
            continue

        with _lock:
            data = _load()
            item = _find(data, topic_id)
            if item:
                if step == "done":
                    item["status"] = "done"
                    item["error"] = None
                else:
                    item["status"] = "draft"
                    if step == "error":
                        item["error"] = st.get("error") or (st.get("job") or {}).get("error")
                _save(data)
        last_result = {
            "started": step == "done",
            "reason": "already_done" if step == "done" else "finished",
            "topic_id": topic_id,
            "job_id": job_id,
            "job": st,
        }
        if step == "done":
            continue
        continue

    if started_list:
        first = started_list[0]
        out = dict(first)
        out["started"] = True
        out["started_count"] = len(started_list)
        out["started_jobs"] = started_list
        out["max_concurrent"] = max_concurrent_jobs()
        out.update({k: v for k, v in public_status().items() if k not in out})
        return out
    return last_result or {"started": False, "reason": "empty", **public_status()}


def _pool_count(data: dict[str, Any]) -> int:
    return sum(1 for t in (data.get("topics") or []) if t.get("status") in ("draft", "queued"))


def hands_off_tick() -> dict[str, Any]:
    """Replenish drafts, auto-schedule them, leave the due-picker to start jobs."""
    from studio.settings import is_api_text_provider, is_native_text_provider

    if not hands_off_enabled():
        return {"skipped": True, "reason": "hands_off_off"}
    min_queue = _hands_off_min_queue()
    generated: dict[str, Any] | None = None
    with _lock:
        data = _load()
        pool = _pool_count(data)
    if pool < min_queue and is_api_text_provider():
        need = max(1, min(8, min_queue - pool))
        try:
            from studio.spend_guard import require_spend

            require_spend(
                "openai_topics",
                confirm_spend=True,
                source="hands_off",
                units=1,
                detail=f"hands-off auto generate {need} topics",
            )
            generated = generate_topics(count=need)
        except Exception as exc:
            generated = {"ok": False, "error": str(exc), "count": 0}
    elif pool < min_queue and is_native_text_provider():
        generated = {
            "ok": False,
            "skipped": True,
            "reason": "native_text",
            "count": 0,
            "message": "hands-off will not auto-generate topics while text_provider is chatgpt/claude.",
        }
    scheduled: list[str] = []
    scheduled_at_list: list[str] = []
    errors: list[str] = []
    stagger = _hands_off_stagger()
    interval = _hands_off_interval()
    with _lock:
        data = _load()
        _sync_from_jobs(data)
        drafts = [t for t in (data.get("topics") or []) if t.get("status") == "draft"]
        for topic in drafts:
            tid = str(topic.get("id") or "")
            try:
                job_id = _ensure_job(topic)
                _prepare_unsupervised_job(job_id)
            except Exception as exc:
                topic["error"] = str(exc)
                errors.append(f"{tid}: {exc}")
                continue
            # Recompute each time so we respect just-queued siblings (≥1h apart).
            slot = _next_hands_off_slot(data)
            topic["scheduled_at"] = slot.isoformat()
            topic["error"] = None
            if topic.get("status") != "running":
                topic["status"] = "queued"
            data["queue"] = [x for x in (data.get("queue") or []) if x != tid]
            data["queue"].append(tid)
            scheduled.append(tid)
            scheduled_at_list.append(topic["scheduled_at"])
        _save(data)
    return {
        "skipped": False,
        "min_queue": min_queue,
        "generated": (generated or {}).get("count") or 0,
        "generate_error": (generated or {}).get("error"),
        "generate_skipped": (generated or {}).get("skipped"),
        "scheduled": scheduled,
        "scheduled_at": scheduled_at_list,
        "scheduled_count": len(scheduled),
        "errors": errors,
        "interval_hours": interval.total_seconds() / 3600.0,
        "stagger_hours": stagger.total_seconds() / 3600.0,
    }


def scheduler_tick() -> dict[str, Any]:
    """Background due-picker (~30s). Starts due topics only while Hands-off is on."""
    if not hands_off_enabled():
        return {"started": False, "reason": "hands_off_off"}
    extra: dict[str, Any] = {}
    try:
        extra = hands_off_tick()
    except Exception as exc:
        extra = {"skipped": False, "error": str(exc)}
    if not auto_scheduler_enabled():
        return {"started": False, "reason": "auto_scheduler_off", "hands_off": extra}
    kicked = kick_queue(force=True)
    kicked["hands_off"] = extra
    return kicked


def schedule_topic(
    topic_id: str = "",
    title: str = "",
    duration_min: float | int | None = None,
    angle: str = "",
    run: str = "queue",
    scheduled_at: str = "",
    run_now: bool = False,
) -> dict[str, Any]:
    """Create a Studio job from a topic (or title) and enqueue it for scheduled_at (local or ISO)."""
    mode = (run or "queue").strip().lower()
    if run_now or mode in ("now", "run", "run_now"):
        mode = "now"
    else:
        mode = "queue"
    tid = (topic_id or "").strip()
    due_iso = (
        store_scheduled_at(None, default_now=True)
        if mode == "now"
        else store_scheduled_at(scheduled_at, default_now=not str(scheduled_at or "").strip())
    )
    notes: list[str] = []
    with _lock:
        data = _load()
        if tid:
            topic = _find(data, tid)
            if not topic:
                raise FileNotFoundError(f"Unknown topic: {tid}")
            if duration_min not in (None, "", 0):
                topic["duration_min"] = _duration_min(duration_min)
            if title.strip():
                topic["title"] = title.strip()
            if angle.strip():
                topic["angle"] = angle.strip()
        else:
            if not (title or "").strip():
                raise RuntimeError("Provide topic_id or a title to schedule.")
            topic = _make_topic(title, angle, duration_min)
            data["topics"].insert(0, topic)
            tid = str(topic["id"])
        job_id = _ensure_job(topic)
        try:
            notes = _prepare_unsupervised_job(job_id)
        except Exception as exc:
            topic["error"] = str(exc)
            _save(data)
            raise
        topic["scheduled_at"] = due_iso
        topic["error"] = None
        if topic.get("status") != "running":
            topic["status"] = "queued"
        data["queue"] = [x for x in data["queue"] if x != tid]
        if mode == "now":
            data["queue"].insert(0, tid)
        else:
            data["queue"].append(tid)
        _save(data)
        saved = _public(topic)
    due_now = bool(saved.get("due") or mode == "now")
    kicked: dict[str, Any]
    if mode == "now" or due_now:
        try:
            from studio.gpu_lock import wait_for_gpu

            wait_for_gpu(name=f"topic:{tid}", kind="pipeline", project_id=str(job_id or ""))
            kicked = kick_queue(force=mode == "now")
        except Exception as exc:
            from studio.gpu_lock import GpuLockTimeout

            if isinstance(exc, GpuLockTimeout):
                kicked = {"started": False, "reason": "gpu", "error": str(exc)}
            else:
                raise
    else:
        kicked = {"started": False, "reason": "scheduled"}
    payload = list_topics(kick=False)
    payload["topic"] = next((t for t in payload["topics"] if t["id"] == tid), saved)
    payload["job_id"] = job_id
    payload["run"] = mode
    payload["run_now"] = mode == "now"
    payload["kicked"] = kicked
    payload["notes"] = notes
    payload["scheduled_at"] = due_iso
    if kicked.get("started"):
        payload["started"] = True
        payload["message"] = "Pipeline started for this topic."
    elif kicked.get("reason") == "needs_script":
        payload["started"] = False
        payload["needs_script"] = True
        payload["message"] = (
            "Job created. Call save_script on this job_id (chatgpt/claude cannot auto-generate), "
            "then the due picker will run pictures → audio → render."
        )
    elif payload.get("busy") or kicked.get("reason") in ("busy", "paused", "gpu", "waiting_for_slot"):
        payload["started"] = False
        when = payload["topic"].get("scheduled_at_local") or due_iso
        if kicked.get("reason") == "gpu":
            payload["error"] = kicked.get("error")
            payload["gpu_lock"] = True
            payload["message"] = (
                kicked.get("error")
                or "GPU is busy. This topic is queued and will start when the lock is free"
                + (f" (due {when})." if when else ".")
            )
        else:
            pos = kicked.get("queue_position")
            pos_bit = f" (queue position {pos})" if pos else ""
            payload["queue_position"] = pos
            payload["message"] = (
                f"Pipeline slots are full{pos_bit}. This topic is queued and will start when a slot frees"
                + (f" (due {when})." if when else ".")
            )
    elif kicked.get("reason") == "hands_off_off":
        payload["started"] = False
        when = payload["topic"].get("scheduled_at_local") or due_iso
        payload["message"] = (
            f"Queued for {when}. Hands-off is off — turn it on in the top bar to auto-start due topics, or use Run now."
        )
    elif kicked.get("reason") == "auto_scheduler_off":
        payload["started"] = False
        when = payload["topic"].get("scheduled_at_local") or due_iso
        payload["message"] = (
            f"Queued for {when}. Auto-run due topics is off — turn it on under Hands-off scheduling, or use Run now."
        )
    elif kicked.get("reason") == "waiting" or not due_now:
        when = payload["topic"].get("scheduled_at_local") or due_iso
        payload["started"] = False
        payload["message"] = (
            f"Queued for {when}. With Hands-off on, Studio will start it when that time arrives "
            f"(checks about every {SCHEDULER_INTERVAL_SEC}s)."
        )
    else:
        payload["started"] = False
        payload["message"] = kicked.get("reason") or "Queued."
    return payload


def start_topic_pipeline(
    topic_id: str = "",
    title: str = "",
    duration_min: float | int | None = None,
    angle: str = "",
    scheduled_at: str = "",
    run_now: bool = True,
) -> dict[str, Any]:
    """MCP alias: schedule and optionally start immediately."""
    return schedule_topic(
        topic_id=topic_id,
        title=title,
        duration_min=duration_min,
        angle=angle,
        scheduled_at=scheduled_at,
        run_now=run_now,
        run="now" if run_now else "queue",
    )


def hands_off_status() -> dict[str, Any]:
    """Readiness + one-shot unsupervised recipe for MCP / Settings."""
    from studio.comfyui import workflow_public_status
    from studio.settings import (
        current_text_provider,
        is_api_text_provider,
        load_settings,
        normalize_hands_off,
        normalize_hands_off_interval_hours,
        normalize_hands_off_min_queue,
        normalize_image_provider,
        normalize_tts_provider,
        public_settings,
        text_provider_label,
    )
    from studio.youtube import status as youtube_status

    settings = load_settings()
    text = current_text_provider()
    image = normalize_image_provider(settings.get("image_provider"))
    tts = normalize_tts_provider(settings.get("tts_provider") or settings.get("voice_provider"))
    fal_key = bool((settings.get("fal_key") or "").strip())
    wf = workflow_public_status()
    yt = {}
    try:
        yt = youtube_status()
    except Exception:
        yt = {"connected": bool(public_settings().get("youtube_connected"))}
    connected = bool(yt.get("connected") or public_settings().get("youtube_connected"))
    text_ok = is_api_text_provider()
    if image == "flux":
        image_ok = fal_key
        image_note = "flux" if fal_key else "flux needs fal_key"
    elif image == "comfyui":
        image_ok = bool(wf.get("loaded"))
        image_note = "comfyui" if image_ok else "comfyui needs an uploaded API workflow"
    else:
        image_ok = fal_key
        image_note = (
            "chatgpt cannot generate pictures unsupervised; schedule will fall back to flux "
            if fal_key
            else "chatgpt cannot run unsupervised (no fal_key for flux fallback). Use flux or comfyui."
        )
    tts_ok = tts in ("openai", "elevenlabs", "local")
    walk_away = bool(text_ok and image_ok and tts_ok)
    catalog = list_topics(kick=False)
    from studio.gpu_lock import gpu_lock_public

    gpu = gpu_lock_public()
    on = normalize_hands_off(settings.get("hands_off"))
    interval_hours = normalize_hands_off_interval_hours(settings.get("hands_off_interval_hours"))
    min_queue = normalize_hands_off_min_queue(settings.get("hands_off_min_queue"))
    warnings: list[str] = []
    if image == "chatgpt":
        warnings.append(
            "ChatGPT images cannot run unsupervised. Switch to Flux or ComfyUI for walk-away."
        )
    if not text_ok:
        warnings.append(
            "text_provider is chatgpt/claude — auto topic generation is skipped; existing drafts still schedule."
        )
    if not connected:
        warnings.append(
            "YouTube is not connected. Videos still render; uploads are marked pending (private when connected)."
        )
    recipe = (
        "HANDS-OFF WALK-AWAY: "
        "1) text_provider openai or lmstudio; image_provider flux (fal_key) or comfyui (workflow uploaded). "
        "2) set_hands_off(true) or update_studio_settings(hands_off=true) — also a toggle on Settings and Topics. "
        "Optional interval: hands_off_interval_hours (0 = due now FIFO; e.g. 2 = every 2 hours). "
        "3) Studio's ~30s loop keeps drafts+queued at hands_off_min_queue (default 5) via generate_topics, "
        "auto-schedules new drafts, and the due-picker runs the full pipeline unsupervised. "
        "4) Each hands-off job sets youtube_auto_upload=true and youtube_privacy=private on THAT job only "
        "(Settings defaults are not changed). If YouTube is disconnected, render still happens and upload is pending. "
        "5) chatgpt/claude text: will not auto-generate topics (existing drafts still schedule). "
        "chatgpt pictures cannot run headless. "
        "GPU: never fire illustrations + local TTS + another job in parallel — wait for get_gpu_lock."
    )
    return {
        "walk_away": walk_away,
        "hands_off": on,
        "hands_off_interval_hours": interval_hours,
        "hands_off_min_queue": min_queue,
        "warnings": warnings,
        "auto_scheduler": auto_scheduler_enabled() or on,
        "scheduler_interval_sec": SCHEDULER_INTERVAL_SEC,
        "timezone": local_timezone_name(),
        "text_provider": text,
        "text_provider_label": text_provider_label(),
        "text_unsupervised": text_ok,
        "image_provider": image,
        "image_unsupervised": image_ok,
        "image_note": image_note,
        "tts_provider": tts,
        "tts_unsupervised": tts_ok,
        "youtube_connected": connected,
        "youtube_auto_upload": bool(settings.get("youtube_auto_upload")),
        "youtube_privacy": settings.get("youtube_privacy") or "unlisted",
        "next_due_at": catalog.get("next_due_at"),
        "next_due_at_local": catalog.get("next_due_at_local"),
        "next_due_topic": catalog.get("next_due_topic"),
        "due_count": catalog.get("due_count"),
        "busy": catalog.get("busy") or bool(gpu.get("busy")),
        "gpu_lock": gpu,
        "recipe": recipe,
        "native_script_note": (
            None
            if text_ok
            else (
                "text_provider is chatgpt/claude. For unsupervised: write the tagged script, "
                "save_script(job_id) in the same turn, then schedule_topic / start_topic_pipeline."
            )
        ),
    }


def retarget_job_id(old_job_id: str, new_job_id: str) -> dict[str, Any]:
    """Point Topics queue entries at a new project folder id after rename_folder."""
    old_id = (old_job_id or "").strip()
    new_id = (new_job_id or "").strip()
    if not old_id or not new_id or old_id == new_id:
        return {"ok": True, "updated": 0, "old_job_id": old_id, "new_job_id": new_id}
    updated = 0
    with _lock:
        data = _load()
        for topic in data.get("topics") or []:
            if topic.get("job_id") == old_id:
                topic["job_id"] = new_id
                updated += 1
        if updated:
            _save(data)
    return {
        "ok": True,
        "updated": updated,
        "old_job_id": old_id,
        "new_job_id": new_id,
    }


def on_pipeline_finished(project_id: str, outcome: str, detail: str = "") -> None:
    """Called from the pipeline worker thread after a job releases the GPU."""
    outcome = (outcome or "done").strip().lower()
    with _lock:
        data = _load()
        matched = None
        for topic in data.get("topics") or []:
            if topic.get("job_id") == project_id:
                matched = topic
                break
        if matched:
            tid = str(matched["id"])
            if outcome == "done":
                matched["status"] = "done"
                matched["error"] = None
                data["queue"] = [x for x in data["queue"] if x != tid]
            elif outcome == "paused":
                matched["status"] = "running"
            else:
                matched["status"] = "draft"
                if outcome == "error":
                    matched["error"] = detail or "Pipeline failed."
                data["queue"] = [x for x in data["queue"] if x != tid]
            _save(data)
    if outcome != "paused":
        kick_queue()
        try:
            from studio.job_queue import pump

            pump()
        except Exception:
            pass

