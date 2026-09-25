"""Render subprocess log and a way to kill a stuck drawer/ffmpeg child."""

from __future__ import annotations

import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from studio.projects import project_dir

_lock = threading.Lock()
_procs: dict[str, set[int]] = {}


def project_id_from_args(extra: list[str]) -> str:
    try:
        idx = extra.index("--input_file")
        return Path(extra[idx + 1]).parent.name
    except (ValueError, IndexError):
        return ""


def render_log_path(project_id: str) -> Path:
    return project_dir(project_id) / "render.log"


def register_render_pid(project_id: str, pid: int) -> None:
    if not project_id or pid <= 0:
        return
    with _lock:
        _procs.setdefault(project_id, set()).add(pid)


def unregister_render_pid(project_id: str, pid: int) -> None:
    with _lock:
        row = _procs.get(project_id)
        if not row:
            return
        row.discard(pid)
        if not row:
            _procs.pop(project_id, None)


def append_render_log(
    project_id: str,
    *,
    script: str,
    exit_code: int,
    stdout: str,
    stderr: str,
) -> None:
    if not project_id:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    block = (
        f"\n=== {stamp} {script} exit={exit_code} ===\n"
        f"{(stdout or '').rstrip()}\n"
        f"--- stderr ---\n"
        f"{(stderr or '').rstrip()}\n"
    )
    try:
        path = render_log_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(block)
        # Keep the log from growing without bound.
        if path.stat().st_size > 1_500_000:
            text = path.read_text(encoding="utf-8", errors="replace")
            path.write_text(text[-800_000:], encoding="utf-8")
    except OSError:
        pass


def kill_render_processes(project_id: str) -> list[int]:
    with _lock:
        pids = list(_procs.get(project_id) or [])
    killed: list[int] = []
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGKILL)
            killed.append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        unregister_render_pid(project_id, pid)
    return killed


def read_render_log(project_id: str, *, tail_chars: int = 12000) -> dict:
    path = render_log_path(project_id)
    text = ""
    if path.is_file():
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = raw[-max(1000, int(tail_chars)) :]
    return {
        "path": str(path),
        "exists": path.is_file(),
        "bytes": path.stat().st_size if path.is_file() else 0,
        "log": text,
    }
