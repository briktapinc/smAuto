"""Studio-wide GPU mutex shared by MCP stdio and the 7878 API.

One holder at a time for ComfyUI, local Chatterbox TTS, Flux batches, and
pipeline image/audio/render GPU sections. Also honors VoiceSync's machine lock
at %LOCALAPPDATA%/VoiceSync/gpu_job.lock when that path is available.
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from studio.paths import GPU_LOCK_MUTEX_PATH, GPU_LOCK_PATH, REPO_ROOT, ensure_dirs

STALE_AFTER_SEC = 180
HEARTBEAT_INTERVAL_SEC = 30
DEFAULT_TIMEOUT_SEC = int(os.environ.get("BUBBLEPOD_GPU_LOCK_TIMEOUT_SEC") or 1200)
POLL_SEC = 0.25
_INSTANCE_ID = "bubblepod-" + uuid.uuid4().hex[:10]

_local = threading.RLock()
_tls = threading.local()
_owns_files = False
_holder_name = ""
_heartbeat_stop: threading.Event | None = None
_heartbeat_thread: threading.Thread | None = None
_atexit_registered = False


class GpuLockTimeout(RuntimeError):
    """Timed out waiting for the GPU lock."""

    def __init__(self, message: str, snapshot: dict[str, Any] | None = None):
        super().__init__(message)
        self.snapshot = snapshot or {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def voicesync_lock_dir() -> Path:
    override = (os.environ.get("VOICESYNC_GPU_LOCK_DIR") or os.environ.get("GPU_LOCK_DIR") or "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "VoiceSync"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "VoiceSync"
    return Path.home() / ".local" / "share" / "VoiceSync"


def voicesync_lock_path() -> Path:
    override = (os.environ.get("VOICESYNC_GPU_LOCK") or os.environ.get("GPU_LOCK_PATH") or "").strip()
    if override:
        return Path(override)
    return voicesync_lock_dir() / "gpu_job.lock"


def _voicesync_mutex_path() -> Path:
    return voicesync_lock_dir() / "gpu_job.mutex"


def studio_lock_path() -> Path:
    return GPU_LOCK_PATH


def timeout_sec(value: float | int | None = None) -> float:
    if value is None:
        return float(DEFAULT_TIMEOUT_SEC)
    return max(0.0, float(value))


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, SystemError):
        # Windows can raise SystemError (WinError 87) for invalid/foreign PIDs.
        if sys.platform == "win32":
            try:
                import ctypes

                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                    PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
                )
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
                    return True
                return False
            except Exception:
                return False
        return False
    return True


@contextmanager
def _file_mutex(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
        except OSError:
            pass
        if sys.platform == "win32":
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


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _is_stale_holder(holder: dict[str, Any] | None) -> bool:
    if not holder:
        return True
    try:
        pid = int(holder.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if not _pid_alive(pid):
        return True
    hb = _parse_iso(holder.get("heartbeat_at") or holder.get("since") or holder.get("started_at"))
    if hb is None:
        return True
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - hb).total_seconds() > STALE_AFTER_SEC


def _public_holder(holder: dict[str, Any] | None) -> dict[str, Any] | None:
    if not holder:
        return None
    name = str(holder.get("name") or holder.get("kind") or holder.get("job_id") or "gpu").strip()
    out = {
        "name": name,
        "pid": holder.get("pid"),
        "kind": holder.get("kind") or name,
        "project_id": holder.get("project_id") or holder.get("job_id"),
        "since": holder.get("since") or holder.get("started_at"),
        "app": holder.get("app") or ("voicesync" if holder.get("job_id") and not str(holder.get("app") or "").startswith("bubble") else "bubble-pod"),
    }
    if holder.get("listen_url") or holder.get("listen_port"):
        out["listen_url"] = holder.get("listen_url") or f"http://127.0.0.1:{holder.get('listen_port')}"
    topic = str(holder.get("topic") or "").strip()
    if topic:
        out["topic"] = topic
    return out


def _public_waiters(waiters: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(waiters, list):
        return out
    for item in waiters:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "name": str(item.get("name") or "gpu"),
                "pid": item.get("pid"),
                "since": item.get("since"),
                "kind": item.get("kind") or item.get("name"),
                "project_id": item.get("project_id"),
            }
        )
    return out


def _studio_state_unlocked() -> dict[str, Any]:
    data = _read_json(GPU_LOCK_PATH) or {}
    holder = data.get("holder") if isinstance(data.get("holder"), dict) else None
    if holder and _is_stale_holder(holder):
        data["holder"] = None
        waiters = data.get("waiters") if isinstance(data.get("waiters"), list) else []
        data["waiters"] = waiters
        _write_json(GPU_LOCK_PATH, {"holder": None, "waiters": waiters})
        holder = None
    waiters = data.get("waiters") if isinstance(data.get("waiters"), list) else []
    live = []
    for item in waiters:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid and _pid_alive(pid):
            live.append(item)
    if len(live) != len(waiters):
        data["waiters"] = live
        data["holder"] = holder
        _write_json(GPU_LOCK_PATH, {"holder": holder, "waiters": live})
    return {"holder": holder, "waiters": live}


def _voicesync_holder_unlocked() -> dict[str, Any] | None:
    path = voicesync_lock_path()
    holder = _read_json(path)
    if not holder:
        return None
    if _is_stale_holder(holder):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return holder


def _is_our_studio_holder(holder: dict[str, Any] | None) -> bool:
    if not holder:
        return False
    if str(holder.get("instance_id") or "") == _INSTANCE_ID:
        return True
    try:
        return int(holder.get("pid") or 0) == os.getpid()
    except (TypeError, ValueError):
        return False


def _is_our_voicesync_holder(holder: dict[str, Any] | None) -> bool:
    if not holder:
        return False
    if str(holder.get("instance_id") or "") == _INSTANCE_ID:
        return True
    try:
        if int(holder.get("pid") or 0) != os.getpid():
            return False
    except (TypeError, ValueError):
        return False
    root = str(holder.get("install_root") or "")
    return (not root) or root == str(REPO_ROOT)


def _studio_holder_payload(name: str, kind: str, project_id: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "pid": os.getpid(),
        "instance_id": _INSTANCE_ID,
        "name": name,
        "kind": kind,
        "project_id": project_id or None,
        "app": "bubble-pod",
        "install_root": str(REPO_ROOT),
        "listen_port": 7878,
        "listen_url": "http://127.0.0.1:7878",
        "since": now,
        "heartbeat_at": now,
    }


def _voicesync_payload(name: str, kind: str, project_id: str, started_at: str | None = None) -> dict[str, Any]:
    now = _now_iso()
    job_id = f"bubblepod:{project_id}" if project_id else f"bubblepod:{name}"
    return {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "instance_id": _INSTANCE_ID,
        "install_root": str(REPO_ROOT),
        "listen_port": 7878,
        "listen_url": "http://127.0.0.1:7878",
        "job_id": job_id,
        "kind": "bubblepod",
        "topic": f"{kind}:{name}"[:120],
        "started_at": started_at or now,
        "heartbeat_at": now,
        "app": "bubble-pod",
        "name": name,
        "project_id": project_id or None,
    }


def _try_claim_voicesync(name: str, kind: str, project_id: str) -> bool:
    mutex = _voicesync_mutex_path()
    lock_path = voicesync_lock_path()
    mutex.parent.mkdir(parents=True, exist_ok=True)
    with _file_mutex(mutex):
        holder = _voicesync_holder_unlocked()
        if holder and not _is_our_voicesync_holder(holder):
            return False
        started = holder.get("started_at") if holder and _is_our_voicesync_holder(holder) else None
        _write_json(lock_path, _voicesync_payload(name, kind, project_id, started_at=started))
        return True


def _release_voicesync() -> None:
    mutex = _voicesync_mutex_path()
    lock_path = voicesync_lock_path()
    try:
        with _file_mutex(mutex):
            holder = _read_json(lock_path)
            if holder and _is_our_voicesync_holder(holder):
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
            elif holder and _is_stale_holder(holder):
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        pass


def _heartbeat_files() -> None:
    now = _now_iso()
    with _file_mutex(GPU_LOCK_MUTEX_PATH):
        data = _studio_state_unlocked()
        holder = data.get("holder")
        if holder and _is_our_studio_holder(holder):
            holder["heartbeat_at"] = now
            holder["pid"] = os.getpid()
            _write_json(GPU_LOCK_PATH, {"holder": holder, "waiters": data.get("waiters") or []})
    try:
        with _file_mutex(_voicesync_mutex_path()):
            holder = _read_json(voicesync_lock_path())
            if holder and _is_our_voicesync_holder(holder):
                holder["heartbeat_at"] = now
                holder["pid"] = os.getpid()
                _write_json(voicesync_lock_path(), holder)
    except OSError:
        pass


def _ensure_heartbeat() -> None:
    global _heartbeat_stop, _heartbeat_thread, _atexit_registered
    if _heartbeat_thread and _heartbeat_thread.is_alive():
        return
    stop = threading.Event()
    _heartbeat_stop = stop

    def _loop() -> None:
        while not stop.wait(HEARTBEAT_INTERVAL_SEC):
            try:
                with _local:
                    if not _owns_files:
                        break
                _heartbeat_files()
            except Exception:
                pass

    _heartbeat_thread = threading.Thread(target=_loop, name="bubblepod-gpu-lock-hb", daemon=True)
    _heartbeat_thread.start()
    if not _atexit_registered:
        atexit.register(_atexit_release)
        _atexit_registered = True


def _stop_heartbeat() -> None:
    global _heartbeat_stop, _heartbeat_thread
    if _heartbeat_stop is not None:
        _heartbeat_stop.set()
    _heartbeat_stop = None
    _heartbeat_thread = None


def _atexit_release() -> None:
    global _owns_files, _holder_name
    try:
        _tls.depth = 0
        with _local:
            _owns_files = False
            _holder_name = ""
        _release_files()
    except Exception:
        pass


def _release_files() -> None:
    with _file_mutex(GPU_LOCK_MUTEX_PATH):
        data = _studio_state_unlocked()
        holder = data.get("holder")
        if holder and _is_our_studio_holder(holder):
            _write_json(GPU_LOCK_PATH, {"holder": None, "waiters": data.get("waiters") or []})
    _release_voicesync()
    _stop_heartbeat()


def _add_waiter(name: str, kind: str, project_id: str) -> str:
    wid = uuid.uuid4().hex[:12]
    ensure_dirs()
    with _file_mutex(GPU_LOCK_MUTEX_PATH):
        data = _studio_state_unlocked()
        waiters = list(data.get("waiters") or [])
        waiters.append(
            {
                "id": wid,
                "name": name,
                "kind": kind,
                "project_id": project_id or None,
                "pid": os.getpid(),
                "since": _now_iso(),
            }
        )
        _write_json(GPU_LOCK_PATH, {"holder": data.get("holder"), "waiters": waiters})
    return wid


def _remove_waiter(wid: str) -> None:
    if not wid:
        return
    try:
        with _file_mutex(GPU_LOCK_MUTEX_PATH):
            data = _studio_state_unlocked()
            waiters = [w for w in (data.get("waiters") or []) if not (isinstance(w, dict) and w.get("id") == wid)]
            _write_json(GPU_LOCK_PATH, {"holder": data.get("holder"), "waiters": waiters})
    except OSError:
        pass


def snapshot() -> dict[str, Any]:
    """Public GPU lock view: ``{busy, holder, waiters}`` plus paths."""
    ensure_dirs()
    with _file_mutex(GPU_LOCK_MUTEX_PATH):
        studio = _studio_state_unlocked()
    vs_holder = None
    try:
        with _file_mutex(_voicesync_mutex_path()):
            vs_holder = _voicesync_holder_unlocked()
    except OSError:
        vs_holder = _voicesync_holder_unlocked()

    studio_holder = studio.get("holder")
    waiters = _public_waiters(studio.get("waiters"))
    with _local:
        ours = bool(_owns_files) or int(getattr(_tls, "depth", 0) or 0) > 0

    holder = None
    busy = False
    if studio_holder and not _is_stale_holder(studio_holder):
        busy = True
        holder = _public_holder(studio_holder)
    elif vs_holder and not _is_our_voicesync_holder(vs_holder):
        busy = True
        vs_pub = _public_holder(vs_holder) or {}
        vs_pub["app"] = vs_pub.get("app") or "voicesync"
        vs_pub["name"] = vs_pub.get("name") or f"voicesync:{vs_holder.get('job_id') or vs_holder.get('kind') or 'job'}"
        holder = vs_pub
    elif ours:
        busy = True
        holder = {
            "name": _holder_name or "gpu",
            "pid": os.getpid(),
            "kind": "gpu",
            "project_id": None,
            "since": None,
            "app": "bubble-pod",
        }

    vs_state = "free"
    if vs_holder:
        vs_state = "held_by_self" if _is_our_voicesync_holder(vs_holder) else "held_by_other"

    return {
        "busy": bool(busy),
        "holder": holder,
        "waiters": waiters,
        "path": str(GPU_LOCK_PATH),
        "voicesync_path": str(voicesync_lock_path()),
        "voicesync_state": vs_state,
        "held_by_self": bool(ours or (studio_holder and _is_our_studio_holder(studio_holder))),
        "stale_after_sec": STALE_AFTER_SEC,
        "timeout_sec": DEFAULT_TIMEOUT_SEC,
    }


def gpu_lock_public() -> dict[str, Any]:
    snap = snapshot()
    return {
        "busy": snap["busy"],
        "holder": snap["holder"],
        "waiters": snap["waiters"],
        "path": snap["path"],
        "voicesync_path": snap.get("voicesync_path"),
        "voicesync_state": snap.get("voicesync_state"),
    }


def clear_stale() -> dict[str, Any]:
    """Drop gpu.lock holder/waiters whose pid is dead. Does not steal a live lock."""
    ensure_dirs()
    before = snapshot()
    with _file_mutex(GPU_LOCK_MUTEX_PATH):
        _studio_state_unlocked()
    try:
        with _file_mutex(_voicesync_mutex_path()):
            _voicesync_holder_unlocked()
    except OSError:
        pass
    after = snapshot()
    dropped = bool(before.get("busy") and not after.get("busy"))
    return {
        "ok": True,
        "cleared": dropped,
        "before": {"busy": before.get("busy"), "holder": before.get("holder")},
        "after": gpu_lock_public(),
    }


def release_on_studio_restart(
    *,
    killed_pid: int | None = None,
    listen_port: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Clear Stickman Automation gpu.lock after API restart/kill.

    clear_stale() alone is not enough during restart: it runs while the old API
    PID is still alive, so the holder looks valid. Call this *after* kill_listener
    (or immediately before os._exit on self-restart) with force=True / killed_pid.
    """
    ensure_dirs()
    port = int(listen_port or 7878)
    before = snapshot()
    cleared = False
    reason = ""

    with _file_mutex(GPU_LOCK_MUTEX_PATH):
        data = _read_json(GPU_LOCK_PATH) or {}
        holder = data.get("holder") if isinstance(data.get("holder"), dict) else None
        waiters = data.get("waiters") if isinstance(data.get("waiters"), list) else []
        drop = False
        if holder:
            try:
                hpid = int(holder.get("pid") or 0)
            except (TypeError, ValueError):
                hpid = 0
            try:
                hport = int(holder.get("listen_port") or 0)
            except (TypeError, ValueError):
                hport = 0
            app = str(holder.get("app") or "")
            is_ours = app.startswith("bubble") or str(holder.get("install_root") or "") == str(REPO_ROOT)
            if force and is_ours and (hport in (0, port) or not hport):
                drop = True
                reason = "force_bubblepod_restart"
            elif killed_pid and hpid == int(killed_pid):
                drop = True
                reason = f"killed_pid={killed_pid}"
            elif is_ours and hport == port and hpid and hpid != os.getpid() and not _pid_alive(hpid):
                drop = True
                reason = "dead_holder_same_port"
            elif _is_stale_holder(holder):
                drop = True
                reason = "stale"
        if drop:
            live_waiters = []
            for item in waiters:
                if not isinstance(item, dict):
                    continue
                try:
                    wpid = int(item.get("pid") or 0)
                except (TypeError, ValueError):
                    wpid = 0
                if wpid and _pid_alive(wpid) and (not killed_pid or wpid != int(killed_pid)):
                    live_waiters.append(item)
            _write_json(GPU_LOCK_PATH, {"holder": None, "waiters": live_waiters})
            cleared = True

    # Also drop our VoiceSync cross-lock file if we held it under the killed pid.
    try:
        with _file_mutex(_voicesync_mutex_path()):
            path = voicesync_lock_path()
            holder = _read_json(path)
            if holder:
                try:
                    hpid = int(holder.get("pid") or 0)
                except (TypeError, ValueError):
                    hpid = 0
                app = str(holder.get("app") or "")
                if (
                    (force and app.startswith("bubble"))
                    or (killed_pid and hpid == int(killed_pid))
                    or _is_stale_holder(holder)
                ):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
    except OSError:
        pass

    after = snapshot()
    return {
        "ok": True,
        "cleared": cleared or bool(before.get("busy") and not after.get("busy")),
        "reason": reason or ("already_free" if not before.get("busy") else "unchanged"),
        "before": {"busy": before.get("busy"), "holder": before.get("holder")},
        "after": gpu_lock_public(),
    }


def is_busy() -> bool:
    return bool(snapshot().get("busy"))


def held_by_self() -> bool:
    if int(getattr(_tls, "depth", 0) or 0) > 0:
        return True
    with _local:
        if _owns_files:
            return True
    return bool(snapshot().get("held_by_self"))


def timeout_message(snap: dict[str, Any] | None = None, waited: float | None = None) -> str:
    snap = snap or snapshot()
    holder = snap.get("holder") or {}
    name = holder.get("name") or "unknown"
    pid = holder.get("pid")
    waiters = snap.get("waiters") or []
    waited_s = f" Waited {int(waited)}s." if waited is not None and waited >= 1 else (f" Waited {waited:.1f}s." if waited else "")
    return (
        f"GPU is busy (holder={name}"
        + (f" pid={pid}" if pid else "")
        + f", waiters={len(waiters)}).{waited_s} "
        "Call get_gpu_lock. Do not start a second ComfyUI / local TTS / Flux / pipeline GPU job; "
        "wait for the lock."
    )


def wait_for_gpu(
    *,
    name: str,
    kind: str = "gpu",
    project_id: str = "",
    timeout: float | int | None = None,
) -> dict[str, Any]:
    """Block until the GPU is free (or this thread already holds it). Does not acquire."""
    if int(getattr(_tls, "depth", 0) or 0) > 0:
        return snapshot()
    limit = timeout_sec(timeout)
    deadline = time.time() + limit
    wid = _add_waiter(name, kind, project_id)
    try:
        while True:
            snap = snapshot()
            if not snap.get("busy"):
                return snap
            remaining = deadline - time.time()
            if remaining <= 0:
                raise GpuLockTimeout(timeout_message(snap, waited=limit), snap)
            time.sleep(min(POLL_SEC, max(0.05, remaining)))
    finally:
        _remove_waiter(wid)


def _try_acquire_files(name: str, kind: str, project_id: str) -> bool:
    """Claim studio + VoiceSync lock files. Same-PID other thread is treated as busy."""
    ensure_dirs()
    vs_holder = None
    try:
        with _file_mutex(_voicesync_mutex_path()):
            vs_holder = _voicesync_holder_unlocked()
    except OSError:
        vs_holder = _voicesync_holder_unlocked()
    if vs_holder and not _is_our_voicesync_holder(vs_holder):
        return False
    with _file_mutex(GPU_LOCK_MUTEX_PATH):
        data = _studio_state_unlocked()
        holder = data.get("holder")
        if holder:
            return False
        payload = _studio_holder_payload(name, kind, project_id)
        _write_json(GPU_LOCK_PATH, {"holder": payload, "waiters": data.get("waiters") or []})
    if not _try_claim_voicesync(name, kind, project_id):
        with _file_mutex(GPU_LOCK_MUTEX_PATH):
            data = _studio_state_unlocked()
            holder = data.get("holder")
            if holder and _is_our_studio_holder(holder):
                _write_json(GPU_LOCK_PATH, {"holder": None, "waiters": data.get("waiters") or []})
        return False
    return True


def acquire(
    *,
    name: str,
    kind: str = "gpu",
    project_id: str = "",
    timeout: float | int | None = None,
) -> None:
    """Acquire the GPU lock, waiting up to ``timeout`` seconds."""
    global _owns_files, _holder_name
    depth = int(getattr(_tls, "depth", 0) or 0)
    if depth > 0:
        _tls.depth = depth + 1
        return
    limit = timeout_sec(timeout)
    deadline = time.time() + limit
    wid = _add_waiter(name, kind, project_id)
    try:
        while True:
            if _try_acquire_files(name, kind, project_id):
                _tls.depth = 1
                with _local:
                    _owns_files = True
                    _holder_name = name
                _ensure_heartbeat()
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                snap = snapshot()
                raise GpuLockTimeout(timeout_message(snap, waited=limit), snap)
            time.sleep(min(POLL_SEC, max(0.05, remaining)))
    finally:
        _remove_waiter(wid)


def release() -> None:
    global _owns_files, _holder_name
    depth = int(getattr(_tls, "depth", 0) or 0)
    if depth <= 0:
        return
    if depth > 1:
        _tls.depth = depth - 1
        return
    _tls.depth = 0
    with _local:
        _owns_files = False
        _holder_name = ""
    _release_files()


@contextmanager
def holding(
    name: str,
    *,
    kind: str = "gpu",
    project_id: str = "",
    timeout: float | int | None = None,
):
    """Hold the GPU lock for the duration of the block (reentrant in-process)."""
    acquire(name=name, kind=kind, project_id=project_id or "", timeout=timeout)
    try:
        yield snapshot()
    finally:
        release()

