"""Restart Studio's uvicorn without killing Gentle, VoiceSync, or Electron.

Electron owns the happy path (stop the child it spawned, or kill only the
listener on the configured Studio port, then spawn `python run_studio.py` and
wait for /api/health).
Browser-only POST and stdio MCP spawn a replacement that waits for the port,
then the old process exits (or MCP waits for health 200).

Listen port defaults to 7878 and comes from settings.json (`port`).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from studio.paths import REPO_ROOT, USER_DATA, ensure_dirs

PROTECTED_PORTS = frozenset({8765, 8766})  # VoiceSync, Gentle
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

_keep_gentle = False
_restarting = False


def keep_gentle_on_shutdown() -> bool:
    return bool(_keep_gentle or _restarting)


def studio_bind() -> tuple[str, int]:
    """Host/port Studio should listen on (from settings, default 127.0.0.1:7878)."""
    try:
        from studio.settings import load_settings

        data = load_settings()
        host = str(data.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        if host in ("0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        port = int(data.get("port") or 7878)
        if port < 1 or port > 65535:
            port = 7878
        return host, port
    except Exception:
        return "127.0.0.1", 7878


def health_url(host: str | None = None, port: int | None = None) -> str:
    h, p = studio_bind()
    if host is None:
        host = h
    if port is None:
        port = p
    return f"http://{host}:{int(port)}/api/health"


# Back-compat names used by older call sites
HOST = "127.0.0.1"
PORT = 7878
HEALTH_URL = f"http://{HOST}:{PORT}/api/health"


def python_exe() -> str:
    return sys.executable or "python"


def _log_path() -> Path:
    ensure_dirs()
    return USER_DATA / "studio_restart.log"


def clear_stale_gpu_lock() -> dict[str, Any]:
    """Drop gpu.lock if the holder pid is dead. Does not steal a live lock."""
    try:
        from studio.gpu_lock import clear_stale

        return clear_stale()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def release_gpu_lock_for_restart(
    *,
    killed_pid: int | None = None,
    listen_port: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Clear Bubble Pod holders after kill / before os._exit (see gpu_lock.release_on_studio_restart)."""
    try:
        from studio.gpu_lock import release_on_studio_restart

        return release_on_studio_restart(
            killed_pid=killed_pid,
            listen_port=listen_port,
            force=force,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def pid_listening(port: int) -> int | None:
    """Return the PID listening on TCP `port`, or None."""
    port = int(port)
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception:
            return None
        needle = f":{port}"
        for raw in (result.stdout or "").splitlines():
            line = raw.upper()
            if "LISTENING" not in line or needle not in raw:
                continue
            parts = raw.split()
            if len(parts) < 2:
                continue
            local = parts[1] if len(parts) > 1 else ""
            if not local.endswith(needle) and f"]:{port}" not in local:
                continue
            try:
                return int(parts[-1])
            except ValueError:
                continue
        return None
    for cmd in (
        ["lsof", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
        ["fuser", "%d/tcp" % port],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except Exception:
            continue
        text = (result.stdout or "").strip().split()
        for token in text:
            token = token.strip()
            if token.isdigit():
                return int(token)
    return None


def kill_pid(pid: int, *, tree: bool = False) -> bool:
    """Kill one process. Never use a process-tree kill on restart (Gentle/VoiceSync)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return False
    if sys.platform == "win32":
        args = ["taskkill", "/PID", str(pid), "/F"]
        if tree:
            args.append("/T")
        try:
            subprocess.run(
                args,
                capture_output=True,
                timeout=8,
                creationflags=CREATE_NO_WINDOW,
            )
            return True
        except Exception:
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def kill_listener(port: int) -> dict[str, Any]:
    """Kill only the process bound to `port`. Refuses Gentle 8766 and VoiceSync 8765."""
    port = int(port)
    if port in PROTECTED_PORTS:
        return {"killed": False, "reason": "protected_port", "port": port}
    pid = pid_listening(port)
    if not pid:
        return {"killed": False, "reason": "not_listening", "port": port, "pid": None}
    if pid == os.getpid():
        return {"killed": False, "reason": "self", "port": port, "pid": pid}
    ok = kill_pid(pid, tree=False)
    return {"killed": bool(ok), "port": port, "pid": pid}


def port_free(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return False
    except OSError:
        return True


def wait_for_port_free(host: str, port: int, timeout: float = 45) -> bool:
    deadline = time.time() + max(1.0, float(timeout))
    while time.time() < deadline:
        if port_free(host, port):
            return True
        time.sleep(0.2)
    return port_free(host, port)


def health_ok(timeout: float = 2.0, *, host: str | None = None, port: int | None = None) -> bool:
    url = health_url(host, port)
    try:
        with urlopen(url, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200) or 200) < 300
    except (URLError, OSError, TimeoutError, ValueError):
        return False


def wait_for_health(timeout: float = 50, *, host: str | None = None, port: int | None = None) -> dict[str, Any]:
    h, p = studio_bind()
    if host is None:
        host = h
    if port is None:
        port = p
    url = health_url(host, port)
    deadline = time.time() + max(2.0, float(timeout))
    last_ok = False
    while time.time() < deadline:
        last_ok = health_ok(host=host, port=port)
        if last_ok:
            return {"ok": True, "health": 200, "url": url, "port": int(port)}
        time.sleep(0.35)
    return {"ok": False, "health": None, "url": url, "port": int(port), "error": "Timed out waiting for /api/health"}


def spawn_studio(*, wait_for_port: bool = True) -> dict[str, Any]:
    script = REPO_ROOT / "run_studio.py"
    if not script.is_file():
        raise RuntimeError(f"Could not find run_studio.py in {REPO_ROOT}")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if wait_for_port:
        env["BUBBLEPOD_WAIT_FOR_PORT"] = "1"
    log = _log_path().open("ab")
    kw: dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": log,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kw["creationflags"] = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW
        kw["close_fds"] = False
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen([python_exe(), str(script)], **kw)
    host, port = studio_bind()
    return {
        "pid": proc.pid,
        "python": python_exe(),
        "script": str(script),
        "host": host,
        "port": port,
    }


def _exit_soon(delay: float = 0.45) -> None:
    def _go() -> None:
        time.sleep(max(0.05, delay))
        os._exit(0)

    threading.Thread(target=_go, name="studio-restart-exit", daemon=True).start()


def begin_self_restart() -> dict[str, Any]:
    """Spawn a replacement, keep Gentle, then os._exit after the HTTP response flushes."""
    global _keep_gentle, _restarting
    _keep_gentle = True
    _restarting = True
    host, port = studio_bind()
    # os._exit skips atexit — release our GPU hold before dying.
    gpu = release_gpu_lock_for_restart(killed_pid=os.getpid(), listen_port=port, force=True)
    spawned = spawn_studio(wait_for_port=True)
    _exit_soon()
    return {
        "ok": True,
        "restarting": True,
        "spawned": spawned,
        "gpu_lock": gpu,
        "host": host,
        "port": port,
        "url": health_url(host, port).rsplit("/api/health", 1)[0] + "/",
        "keep": {"gentle": 8766, "voicesync": 8765, "electron": True},
        "message": f"Spawned a new Studio process on {host}:{port}. This API will exit; wait for /api/health.",
    }


def restart_studio(*, wait: bool = True) -> dict[str, Any]:
    """Kill the Studio listen port (not Gentle/VoiceSync), spawn run_studio.py, optionally wait for health.

    If this process *is* the listener (uvicorn / HTTP MCP), spawn then exit.
    stdio MCP is a different PID, so it can wait for health 200.
    """
    global _keep_gentle, _restarting
    host, port = studio_bind()
    listener = pid_listening(port)
    self_is_api = bool(listener and listener == os.getpid())
    keep = {"gentle": 8766, "voicesync": 8765, "electron": True}
    url = health_url(host, port)

    if self_is_api:
        _keep_gentle = True
        _restarting = True
        # os._exit skips atexit — drop the lock while we still own it.
        gpu = release_gpu_lock_for_restart(killed_pid=os.getpid(), listen_port=port, force=True)
        spawned = spawn_studio(wait_for_port=True)
        _exit_soon()
        return {
            "ok": True,
            "restarting": True,
            "self": True,
            "spawned": spawned,
            "gpu_lock": gpu,
            "host": host,
            "port": port,
            "url": url,
            "keep": keep,
            "message": "This process is the API. Spawned a replacement and exiting.",
        }

    spawned = spawn_studio(wait_for_port=True)
    killed = None
    killed_pid = listener
    if listener:
        killed = kill_listener(port)
        if isinstance(killed, dict) and killed.get("pid"):
            try:
                killed_pid = int(killed["pid"])
            except (TypeError, ValueError):
                killed_pid = listener
    # AFTER kill — clear_stale before kill cannot drop a still-alive holder.
    gpu = release_gpu_lock_for_restart(
        killed_pid=killed_pid,
        listen_port=port,
        force=True,
    )
    health: dict[str, Any] = {"ok": False}
    if wait:
        health = wait_for_health(host=host, port=port)
    return {
        "ok": bool(health.get("ok") or not wait),
        "restarting": False,
        "self": False,
        "killed": killed,
        "spawned": spawned,
        "gpu_lock": gpu,
        "keep": keep,
        "health": health.get("health"),
        "host": host,
        "port": port,
        "url": url,
        "error": health.get("error"),
        "message": "Studio API restarted." if health.get("ok") else (health.get("error") or "Spawned Studio."),
    }
