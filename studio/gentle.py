"""Start and talk to Gentle: Docker when healthy, else a local HTTP fallback.

On macOS, prefer an already-running Gentle app (or Settings gentle_url);
Docker is optional and is not started automatically.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from studio.paths import DOCKER_COMPOSE, REPO_ROOT, USER_DATA
from studio.projects import (
    input_prefix,
    load_meta,
    project_generate_9x16,
    save_meta,
    shorts_input_prefix,
    write_shorts_scripts,
)
from studio.settings import load_settings, save_settings
from studio.aspect import needs_explainer_assets, DEFAULT_ASPECT

_gentle_status_cache: dict = {"at": 0.0, "value": None}
_GENTLE_STATUS_TTL = 5.0

GENTLE_IMAGE = "lowerquality/gentle"
GENTLE_NAME = "lazykh-gentle"
DEFAULT_URL = "http://127.0.0.1:8766"
# Native Gentle.app commonly listens on 8765; Docker compose maps host 8766 → 8765.
MAC_GENTLE_PORTS = (8765, 8766, 8767, 8768, 8770)
IS_DARWIN = sys.platform == "darwin"

_lock = threading.Lock()
_local_proc: subprocess.Popen | None = None
_docker_ok_cache: tuple[float, bool] | None = None
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def gentle_url() -> str:
    return (load_settings().get("gentle_url") or DEFAULT_URL).rstrip("/")


def _port_from_url(url: str) -> int:
    parsed = urlparse(url)
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def _popen_kwargs() -> dict:
    kw: dict = {}
    if os.name == "nt" and CREATE_NO_WINDOW:
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


def docker_available() -> bool:
    global _docker_ok_cache
    now = time.time()
    if _docker_ok_cache and now - _docker_ok_cache[0] < 8:
        return _docker_ok_cache[1]
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            **_popen_kwargs(),
        )
        ok = result.returncode == 0
    except Exception:
        ok = False
    _docker_ok_cache = (now, ok)
    return ok


def docker_container_running() -> bool:
    if not docker_available():
        return False
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", GENTLE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
            **_popen_kwargs(),
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except Exception:
        return False


def _identify_backend(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("backend") == "local":
            return "local"
        if isinstance(data, dict) and data.get("name") == "lazykh-gentle-local":
            return "local"
    except Exception:
        pass
    if docker_container_running():
        return "docker"
    if IS_DARWIN:
        return "app"
    return "remote"


def _probe_url(url: str, timeout: float = 1.5) -> httpx.Response | None:
    try:
        response = httpx.get(url.rstrip("/"), timeout=timeout)
        if response.status_code < 500:
            return response
    except Exception:
        return None
    return None


def _candidate_ports(preferred: int | None = None) -> list[int]:
    ports: list[int] = []
    if preferred and preferred > 0:
        ports.append(preferred)
    extras = MAC_GENTLE_PORTS if IS_DARWIN else (8766, 8767, 8768, 8770, 8765)
    for extra in extras:
        if extra not in ports:
            ports.append(extra)
    return ports


def _detail_for_backend(backend: str, url: str) -> str:
    if backend == "local":
        return f"Local Gentle-compatible aligner at {url}"
    if backend == "docker":
        return f"Gentle Docker container at {url}"
    if backend == "app":
        return f"Gentle app at {url}"
    return f"Aligner reachable at {url}"


def _set_url(url: str) -> None:
    current = (load_settings().get("gentle_url") or "").rstrip("/")
    if current != url.rstrip("/"):
        save_settings({"gentle_url": url.rstrip("/")})


def _local_running() -> bool:
    return _local_proc is not None and _local_proc.poll() is None


def detect_listening_gentle(host: str | None = None) -> dict | None:
    """Find a reachable Gentle HTTP server (Settings URL first, then common ports)."""
    configured = gentle_url()
    parsed = urlparse(configured)
    host = host or parsed.hostname or "127.0.0.1"
    preferred = _port_from_url(configured) if configured else None

    probe = _probe_url(configured)
    if probe is not None:
        backend = _identify_backend(probe)
        return {
            "ok": True,
            "url": configured.rstrip("/"),
            "port": preferred or _port_from_url(configured),
            "backend": backend,
            "detail": _detail_for_backend(backend, configured.rstrip("/")),
            "docker_available": docker_available(),
            "docker_container": docker_container_running(),
            "owned_local": _local_running(),
            "platform": sys.platform,
            "prefer_app": IS_DARWIN,
        }

    for port in _candidate_ports(preferred):
        url = f"http://{host}:{port}"
        if url.rstrip("/") == configured.rstrip("/"):
            continue
        probe = _probe_url(url)
        if probe is None:
            continue
        backend = _identify_backend(probe)
        _set_url(url)
        return {
            "ok": True,
            "url": url,
            "port": port,
            "backend": backend,
            "detail": _detail_for_backend(backend, url),
            "docker_available": docker_available(),
            "docker_container": docker_container_running(),
            "owned_local": _local_running(),
            "platform": sys.platform,
            "prefer_app": IS_DARWIN,
        }
    return None


def gentle_status() -> dict:
    """Probe Gentle; cached briefly so /api/health polls stay cheap."""
    now = time.monotonic()
    cached = _gentle_status_cache.get("value")
    at = float(_gentle_status_cache.get("at") or 0)
    if cached is not None and (now - at) < _GENTLE_STATUS_TTL:
        return dict(cached)

    url = gentle_url()
    port = _port_from_url(url)
    docker = docker_available()
    base = {
        "url": url,
        "port": port,
        "docker_available": docker,
        "docker_container": docker_container_running(),
        "owned_local": _local_running(),
        "platform": sys.platform,
        "prefer_app": IS_DARWIN,
    }
    try:
        response = httpx.get(url, timeout=1.5)
        ok = response.status_code < 500
        backend = _identify_backend(response) if ok else "none"
        if ok:
            detail = _detail_for_backend(backend, url)
        else:
            detail = f"HTTP {response.status_code}"
        result = {**base, "ok": ok, "backend": backend, "detail": detail}
    except Exception as exc:
        result = {
            **base,
            "ok": False,
            "backend": "none",
            "detail": str(exc) or exc.__class__.__name__,
        }
        if IS_DARWIN:
            found = detect_listening_gentle()
            if found and found.get("ok"):
                result = {**base, **found}
    _gentle_status_cache["at"] = now
    _gentle_status_cache["value"] = result
    return dict(result)


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _wait_ready(seconds: float) -> dict:
    deadline = time.time() + seconds
    status = gentle_status()
    while time.time() < deadline:
        if status["ok"]:
            return status
        time.sleep(0.5)
        status = gentle_status()
    return status


def _start_docker() -> None:
    if DOCKER_COMPOSE.is_file():
        subprocess.check_call(
            ["docker", "compose", "-f", str(DOCKER_COMPOSE), "up", "-d"],
            cwd=str(REPO_ROOT),
            **_popen_kwargs(),
        )
        return
    subprocess.check_call(
        [
            "docker", "run", "-d", "--restart", "unless-stopped",
            "--name", GENTLE_NAME,
            "-p", "8766:8765",
            GENTLE_IMAGE,
        ],
        **_popen_kwargs(),
    )


def _log_path() -> Path:
    USER_DATA.mkdir(parents=True, exist_ok=True)
    return USER_DATA / "gentle_local.log"


def _start_local(host: str, port: int) -> None:
    global _local_proc
    if _local_running():
        return
    log = _log_path().open("ab")
    env = os.environ.copy()
    env.setdefault("CUDA_VISIBLE_DEVICES", "")
    _local_proc = subprocess.Popen(
        [sys.executable, "-m", "studio.gentle_local", "--host", host, "--port", str(port)],
        cwd=str(REPO_ROOT),
        stdout=log,
        stderr=log,
        env=env,
        **_popen_kwargs(),
    )


def stop_owned_local_gentle() -> None:
    global _local_proc
    proc = _local_proc
    _local_proc = None
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=4)
    except Exception:
        proc.kill()


def _stop_docker() -> bool:
    """Stop the lazykh-gentle container. Returns True if it was running."""
    if not docker_available():
        return False
    was_running = docker_container_running()
    if DOCKER_COMPOSE.is_file():
        subprocess.run(
            ["docker", "compose", "-f", str(DOCKER_COMPOSE), "stop"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=40,
            check=False,
            **_popen_kwargs(),
        )
    if docker_container_running():
        subprocess.run(
            ["docker", "stop", GENTLE_NAME],
            capture_output=True,
            timeout=40,
            check=False,
            **_popen_kwargs(),
        )
    return was_running


def stop_gentle() -> dict:
    """Stop Docker Gentle and/or the local aligner process Studio owns.

    Does not touch a remote Gentle URL / Gentle.app Studio did not start.
    """
    with _lock:
        stopped: list[str] = []
        errors: list[str] = []
        if _local_running():
            stop_owned_local_gentle()
            stopped.append("local")
        try:
            if _stop_docker():
                stopped.append("docker")
        except Exception as exc:
            errors.append(f"Docker stop failed: {exc}")

        status = gentle_status()
        if stopped:
            detail = f"Stopped Gentle ({', '.join(stopped)})."
            if status.get("ok"):
                detail += " Aligner still reachable (likely Gentle app, remote, or unmanaged)."
            status["detail"] = detail
        elif errors:
            status["detail"] = "; ".join(errors)
        else:
            status["detail"] = (
                "Nothing to stop - no Studio-owned local process and Docker Gentle is not running."
                + (" Gentle.app (if open) is left running." if IS_DARWIN else "")
            )
        status["stopped"] = stopped
        if errors:
            status["errors"] = errors
        return status


def _start_local_fallback(host: str, ports: list[int], last_error: str) -> tuple[dict, str]:
    """Try to start studio.gentle_local on the first free candidate port."""
    for cand in ports:
        url = f"http://{host}:{cand}"
        try:
            probe = httpx.get(url, timeout=1.5)
            if probe.status_code < 500:
                _set_url(url)
                st = gentle_status()
                if st["ok"]:
                    return st, last_error
        except Exception:
            pass
        if _port_open(host, cand) and not _local_running():
            continue
        try:
            _start_local(host, cand)
        except Exception as exc:
            last_error = str(exc)
            continue
        _set_url(url)
        status = _wait_ready(20.0)
        if status["ok"]:
            status["backend"] = "local"
            status["detail"] = f"Local Gentle-compatible aligner is up at {url}."
            return status, last_error
        last_error = status.get("detail") or last_error
        if _local_running():
            break
    return gentle_status(), last_error


def ensure_gentle(required: bool = True, wait_docker: float | None = None) -> dict:
    """Reach a Gentle HTTP aligner.

    macOS: prefer Settings gentle_url / Gentle.app on common ports; do not start
    Docker. Falls back to the local Python aligner if nothing is listening.

    Windows/Linux: Docker Gentle when healthy; otherwise local fallback.
    """
    with _lock:
        status = gentle_status()
        if status["ok"]:
            return status

        if IS_DARWIN:
            found = detect_listening_gentle()
            if found and found.get("ok"):
                return found

            parsed = urlparse(gentle_url())
            host = parsed.hostname or "127.0.0.1"
            ports = _candidate_ports(_port_from_url(gentle_url()))
            last_error = status.get("detail") or "Aligner not reachable."
            status, last_error = _start_local_fallback(host, ports, last_error)
            if status.get("ok"):
                return status

            status = gentle_status()
            status["ok"] = False
            status["detail"] = (
                "Could not reach Gentle on this Mac. Open the Gentle app "
                "(often http://127.0.0.1:8765), set Gentle URL in Settings, "
                f"or allow the local fallback. {last_error}"
            )
            status["prefer_app"] = True
            status["platform"] = "darwin"
            if required:
                raise RuntimeError(status["detail"])
            return status

        docker_wait = 20.0 if wait_docker is None else wait_docker
        if required and wait_docker is None:
            docker_wait = 50.0

        if docker_available():
            try:
                _start_docker()
                status = _wait_ready(docker_wait)
                if status["ok"]:
                    status["detail"] = "Gentle Docker container is up."
                    status["backend"] = "docker"
                    return status
            except Exception as exc:
                status = gentle_status()
                status["detail"] = f"Docker Gentle failed ({exc}); trying local fallback."

        parsed = urlparse(gentle_url())
        host = parsed.hostname or "127.0.0.1"
        ports = _candidate_ports(_port_from_url(gentle_url()))
        last_error = status.get("detail") or "Aligner not reachable."
        status, last_error = _start_local_fallback(host, ports, last_error)
        if status.get("ok"):
            return status

        status = gentle_status()
        status["ok"] = False
        status["detail"] = (
            "Could not start Gentle. Docker is unavailable or unhealthy, and the "
            f"local fallback did not come up. {last_error}"
        )
        if required:
            raise RuntimeError(status["detail"])
        return status


def align_audio(project_id: str, *, shorts: bool = False) -> dict:
    status = ensure_gentle(required=True)
    if shorts:
        write_shorts_scripts(project_id)
        prefix = Path(shorts_input_prefix(project_id))
    else:
        prefix = Path(input_prefix(project_id))
    audio = prefix.with_suffix(".wav")
    transcript = prefix.with_name(prefix.name + "_g.txt")
    if not audio.is_file():
        raise RuntimeError(
            "Generate 9:16 short audio before aligning with Gentle."
            if shorts
            else "Generate audio before aligning with Gentle."
        )
    if not transcript.is_file():
        raise RuntimeError("Generate a script before aligning with Gentle.")
    url = gentle_url() + "/transcriptions"
    with audio.open("rb") as audio_f, transcript.open("rb") as text_f:
        response = httpx.post(
            url,
            params={"async": "false"},
            files={
                "audio": (audio.name, audio_f, "audio/wav"),
                "transcript": (transcript.name, text_f, "text/plain"),
            },
            timeout=600,
        )
    response.raise_for_status()
    dest = prefix.with_suffix(".json")
    dest.write_text(response.text, encoding="utf-8")
    from studio.projects import invalidate_render_artifacts

    invalidate_render_artifacts(project_id, alignment=False)
    meta = load_meta(project_id)
    meta["status"] = "aligned"
    meta["gentle_backend"] = status.get("backend")
    save_meta(project_id, meta)
    return {
        "ok": True,
        "path": str(dest),
        "url": url,
        "backend": status.get("backend"),
        "shorts": bool(shorts),
    }


def align_project_audio(project_id: str) -> dict:
    """Align full explainer and/or 9:16 short based on job aspect + generate_9x16."""
    meta = load_meta(project_id)
    aspect = meta.get("aspect") or DEFAULT_ASPECT
    parts = []
    if needs_explainer_assets(aspect):
        parts.append(align_audio(project_id, shorts=False))
    if project_generate_9x16(meta):
        parts.append(align_audio(project_id, shorts=True))
    if not parts:
        parts.append(align_audio(project_id, shorts=False))
    return {"ok": True, "parts": parts, "path": parts[0].get("path"), "backend": parts[0].get("backend")}
