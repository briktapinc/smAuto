"""Notices MCP can wait on when a Studio job finishes."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

from studio.paths import USER_DATA

_PATH = USER_DATA / "job_notifications.jsonl"
_MAX = 200
_lock = threading.Lock()
_cond = threading.Condition(_lock)
_events: deque[dict[str, Any]] = deque(maxlen=_MAX)
_seq = 0
_loaded = False

# Full pipeline or a final render. Step jobs (script, pictures, audio) are not a finished video.
_FINISH_KINDS = frozenset({"start", "resume", "render"})


def _load() -> None:
    global _seq, _loaded
    if _loaded:
        return
    _loaded = True
    if not _PATH.is_file():
        return
    try:
        lines = _PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines[-_MAX:]:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        _events.append(row)
        _seq = max(_seq, int(row.get("id") or 0))


def _append(event: dict[str, Any]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _is_render_problem(kind: str, detail: str, error: str, step: str) -> bool:
    if (kind or "").strip().lower() == "render" and (error or "").strip():
        return True
    blob = f"{detail}\n{error}\n{step}".lower()
    return any(
        token in blob
        for token in (
            "render failed",
            "videodrawer",
            "videofinisher",
            "ffmpeg",
            "videofinisher",
            "muxing",
            "title-card cover missing",
            "frames",
        )
    )


def publish_job_finished(
    project_id: str,
    *,
    kind: str,
    outcome: str,
    detail: str = "",
    error: str = "",
    step: str = "",
) -> dict[str, Any] | None:
    """Record a completion, a job failure, or a render error. Returns the event, or None if ignored."""
    global _seq
    kind_n = (kind or "").strip().lower()
    outcome_n = (outcome or "").strip().lower()
    if kind_n not in _FINISH_KINDS or outcome_n not in ("done", "error"):
        return None
    err = (error or "").strip()
    note = (detail or "").strip()
    title = ""
    try:
        from studio.projects import load_meta

        meta = load_meta(project_id)
        title = str(meta.get("title") or meta.get("topic") or "")
    except Exception:
        title = ""
    render_problem = bool(err) or _is_render_problem(kind_n, note, err, step)
    if kind_n == "render" and (outcome_n == "error" or err):
        render_problem = True
    if render_problem and (err or outcome_n == "error" or kind_n == "render"):
        # A stored error on a finished pipeline is the partial render failure (one aspect).
        if outcome_n == "done" and err:
            event_type = "render_error"
            status = "failed"
        elif _is_render_problem(kind_n, note, err, step) or kind_n == "render":
            event_type = "render_error"
            status = "failed"
        else:
            event_type = "job_failed"
            status = "failed"
    elif outcome_n == "error":
        event_type = "job_failed"
        status = "failed"
    else:
        event_type = "job_completed"
        status = "completed"
    message = err or note
    with _cond:
        _load()
        _seq += 1
        event = {
            "id": _seq,
            "type": event_type,
            "status": status,
            "project_id": project_id,
            "title": title,
            "kind": kind_n,
            "step": (step or "")[:80],
            "detail": message[:800],
            "error": err[:800],
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if event_type == "render_error":
            event["action"] = (
                "A render failed. Fix the cause on this project, then call resume_job "
                "or render_final_video. Do not treat this job as finished."
            )
        _events.append(event)
        _append(event)
        _cond.notify_all()
        return dict(event)


def _matching(after_id: int, project_id: str) -> list[dict[str, Any]]:
    pid = (project_id or "").strip()
    out = []
    for row in _events:
        if int(row.get("id") or 0) <= after_id:
            continue
        if pid and row.get("project_id") != pid:
            continue
        out.append(dict(row))
    return out


def listen_job_notifications(
    *,
    project_id: str = "",
    after_id: int = 0,
    timeout_sec: float = 30,
) -> dict[str, Any]:
    """Block until a newer completion notice exists, or until timeout_sec."""
    wait = max(1.0, min(50.0, float(timeout_sec or 30)))
    cursor = max(0, int(after_id or 0))
    deadline = time.time() + wait
    with _cond:
        _load()
        while True:
            found = _matching(cursor, project_id)
            if found:
                return {
                    "ok": True,
                    "notifications": found,
                    "cursor": int(found[-1]["id"]),
                    "timed_out": False,
                }
            remaining = deadline - time.time()
            if remaining <= 0:
                return {
                    "ok": True,
                    "notifications": [],
                    "cursor": cursor,
                    "timed_out": True,
                }
            _cond.wait(timeout=min(remaining, 5.0))
