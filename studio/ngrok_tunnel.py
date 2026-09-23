"""Managed ngrok tunnel for Stickman Automation Studio (ChatGPT MCP / remote access).

Ngrok agent v3+ applies HTTP Basic Auth via a Traffic Policy file
(--traffic-policy-file). Older agents that still accept --basic-auth are
supported as a fallback.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from studio.paths import REPO_ROOT, USER_DATA, ensure_dirs, load_repo_dotenv
from studio.settings import load_settings, save_settings

DEFAULT_PUBLIC_URL = ""
DEFAULT_LOCAL_PORT = 7878
DEFAULT_BASIC_AUTH_USER = "bubblepod"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_reader: threading.Thread | None = None
_output: deque[str] = deque(maxlen=80)
_last_error: str = ""
_started_at: float | None = None
_last_revealed_password: str | None = None
_authtoken_applied: bool = False

COMMON_WIN_PATHS = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "ngrok" / "ngrok.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ngrok" / "ngrok.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "ngrok" / "ngrok.exe",
    Path.home() / "ngrok" / "ngrok.exe",
    Path(r"C:\ngrok\ngrok.exe"),
)

COMMON_UNIX_PATHS = (
    Path("/usr/local/bin/ngrok"),
    Path("/usr/bin/ngrok"),
    Path("/snap/bin/ngrok"),
    Path.home() / ".local" / "bin" / "ngrok",
    Path.home() / "ngrok" / "ngrok",
    REPO_ROOT / "desktop" / "bin" / "ngrok",
    REPO_ROOT / "bin" / "ngrok",
)


def _popen_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if os.name == "nt" and CREATE_NO_WINDOW:
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


def _pid_path() -> Path:
    ensure_dirs()
    return USER_DATA / "ngrok.pid"


def _log_path() -> Path:
    ensure_dirs()
    return USER_DATA / "ngrok.log"


def _policy_path() -> Path:
    ensure_dirs()
    return USER_DATA / "ngrok_traffic_policy.json"


def normalize_public_url(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.netloc or parsed.path or "").strip().rstrip("/")
    if not host:
        return ""
    scheme = (parsed.scheme or "https").lower()
    if scheme not in ("http", "https"):
        scheme = "https"
    return f"{scheme}://{host}"


def normalize_local_port(value: Any, default: int = DEFAULT_LOCAL_PORT) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return int(default)
    if port < 1 or port > 65535:
        return int(default)
    return port


def normalize_autostart(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    raw = str(value).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off", ""):
        return False
    return False


def generate_basic_auth_password() -> str:
    # ngrok basic-auth passwords must be 8–128 chars
    return secrets.token_urlsafe(18)


def ensure_basic_auth_credentials(
    *,
    user: str | None = None,
    password: str | None = None,
    rotate_password: bool = False,
) -> dict[str, Any]:
    """Ensure username/password exist in settings. Returns plaintext password when generated/rotated."""
    settings = load_settings()
    username = (user if user is not None else settings.get("ngrok_basic_auth_user") or DEFAULT_BASIC_AUTH_USER)
    username = str(username or DEFAULT_BASIC_AUTH_USER).strip() or DEFAULT_BASIC_AUTH_USER
    current = str(settings.get("ngrok_basic_auth_password") or "")
    generated = False
    if password is not None and str(password).strip() and str(password).strip() != "********":
        current = str(password).strip()
        generated = True
    elif rotate_password or not current.strip():
        current = generate_basic_auth_password()
        generated = True
    updates: dict[str, Any] = {
        "ngrok_basic_auth_user": username,
        "ngrok_basic_auth_password": current,
    }
    save_settings(updates)
    global _last_revealed_password
    if generated:
        _last_revealed_password = current
    return {
        "username": username,
        "password": current,
        "generated": generated,
        "password_set": True,
    }


def write_basic_auth_policy(username: str, password: str) -> Path:
    """Write ngrok traffic policy: basic-auth then stamp a tunnel header.

    ngrok strips Authorization after a successful basic-auth check, so Studio
    would otherwise see an unauthenticated request. We add X-BubblePod-Tunnel-Auth
    for the upstream to accept the already-authenticated tunnel hop.
    """
    from studio.settings import load_settings, save_settings
    import secrets as _secrets

    settings = load_settings()
    gate = str(settings.get("ngrok_tunnel_gate") or "").strip()
    if len(gate) < 16:
        gate = _secrets.token_urlsafe(24)
        save_settings({"ngrok_tunnel_gate": gate})

    path = _policy_path()
    policy = {
        "on_http_request": [
            {
                "name": "Stickman Automation Studio basic auth",
                "actions": [
                    {
                        "type": "basic-auth",
                        "config": {
                            "realm": "Stickman Automation Studio",
                            "credentials": [f"{username}:{password}"],
                            "enforce": True,
                        },
                    },
                    {
                        "type": "add-headers",
                        "config": {
                            "headers": {
                                "X-BubblePod-Tunnel-Auth": gate,
                            }
                        },
                    },
                ],
            }
        ]
    }
    path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    return path


def config_from_settings(data: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = data if data is not None else load_settings()
    public_url = normalize_public_url(settings.get("ngrok_url"))
    local_port = normalize_local_port(
        settings.get("ngrok_local_port"),
        default=normalize_local_port(settings.get("port"), DEFAULT_LOCAL_PORT),
    )
    user = str(settings.get("ngrok_basic_auth_user") or DEFAULT_BASIC_AUTH_USER).strip() or DEFAULT_BASIC_AUTH_USER
    password = str(settings.get("ngrok_basic_auth_password") or "")
    return {
        "ngrok_url": public_url,
        "ngrok_local_port": local_port,
        "ngrok_autostart": normalize_autostart(settings.get("ngrok_autostart")),
        "ngrok_basic_auth_user": user,
        "ngrok_basic_auth_password_set": bool(password),
        "public_url": public_url,
        "mcp_url": f"{public_url.rstrip('/')}/mcp" if public_url else "",
        "local_port": local_port,
        "command": build_command_line(local_port, public_url or "<your-ngrok-url>", basic_auth=True),
    }


def build_command_line(local_port: int, public_url: str, *, basic_auth: bool = True) -> str:
    base = f"ngrok http {int(local_port)} --url {public_url}"
    if basic_auth:
        return f"{base} --traffic-policy-file {_policy_path()}"
    return base


def find_ngrok() -> str | None:
    """Locate the ngrok agent binary (PATH, NGROK_PATH/NGROK_BIN, common install dirs)."""
    load_repo_dotenv()
    for key in ("NGROK_PATH", "NGROK_BIN"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if path.is_file():
                return str(path.resolve())
    which = shutil.which("ngrok")
    if which:
        path = Path(which)
        if path.is_file():
            return str(path)
    candidates = COMMON_WIN_PATHS if os.name == "nt" else COMMON_UNIX_PATHS
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue
    return None


def ngrok_authtoken() -> str:
    """Agent authtoken from env (never invent one). Empty means user must configure ngrok."""
    load_repo_dotenv()
    return (os.environ.get("NGROK_AUTHTOKEN") or os.environ.get("NGROK_TOKEN") or "").strip()


def ensure_ngrok_authtoken(exe: str | None = None) -> dict[str, Any]:
    """If NGROK_AUTHTOKEN is set, write it into the local ngrok agent config once.

    Returns ok=True when a token is configured (env applied or already present in config),
    or ok=False with a clear message when missing — does not invent a token.
    """
    global _authtoken_applied
    token = ngrok_authtoken()
    binary = exe or find_ngrok()
    if not binary:
        return {
            "ok": False,
            "configured": False,
            "detail": "ngrok binary not found.",
        }
    if not token:
        return {
            "ok": False,
            "configured": False,
            "detail": (
                "ngrok authtoken not set. Add NGROK_AUTHTOKEN to .env "
                "(from https://dashboard.ngrok.com/get-started/your-authtoken) "
                "or run: ngrok config add-authtoken YOUR_TOKEN"
            ),
        }
    if _authtoken_applied:
        return {"ok": True, "configured": True, "detail": "NGROK_AUTHTOKEN already applied."}
    try:
        result = subprocess.run(
            [binary, "config", "add-authtoken", token],
            capture_output=True,
            text=True,
            timeout=30,
            **_popen_kwargs(),
        )
    except Exception as exc:
        return {
            "ok": False,
            "configured": False,
            "detail": f"Failed to apply NGROK_AUTHTOKEN: {exc}",
        }
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        return {
            "ok": False,
            "configured": False,
            "detail": f"ngrok config add-authtoken failed: {err}",
        }
    _authtoken_applied = True
    return {"ok": True, "configured": True, "detail": "NGROK_AUTHTOKEN applied to ngrok agent config."}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                **_popen_kwargs(),
            )
        except Exception:
            return False
        text = (result.stdout or "").lower()
        return str(pid) in text and "ngrok" in text
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid_file() -> int | None:
    path = _pid_path()
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        pid = int(raw)
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def _write_pid_file(pid: int) -> None:
    try:
        _pid_path().write_text(str(int(pid)), encoding="utf-8")
    except OSError:
        pass


def _clear_pid_file() -> None:
    try:
        path = _pid_path()
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def _owned_running() -> bool:
    global _proc
    if _proc is not None and _proc.poll() is None:
        return True
    pid = _read_pid_file()
    if pid and _pid_alive(pid):
        return True
    if _proc is not None and _proc.poll() is not None:
        _proc = None
    return False


def _owned_pid() -> int | None:
    if _proc is not None and _proc.poll() is None:
        return int(_proc.pid)
    pid = _read_pid_file()
    if pid and _pid_alive(pid):
        return pid
    return None


def _append_output(line: str) -> None:
    global _last_error
    text = (line or "").rstrip()
    if not text:
        return
    _output.append(text)
    low = text.lower()
    if any(
        needle in low
        for needle in (
            "err",
            "error",
            "authtoken",
            "auth token",
            "unauthorized",
            "failed",
            "not found",
            "already online",
            "endpoint is already online",
        )
    ):
        _last_error = text


def _drain_reader(proc: subprocess.Popen, log_handle: Any) -> None:
    try:
        stream = proc.stdout
        if stream is None:
            return
        while True:
            line = stream.readline()
            if not line:
                break
            if isinstance(line, bytes):
                text = line.decode("utf-8", errors="replace")
            else:
                text = line
            _append_output(text)
            try:
                log_handle.write(text if text.endswith("\n") else text + "\n")
                log_handle.flush()
            except Exception:
                pass
    finally:
        try:
            log_handle.close()
        except Exception:
            pass


def _kill_pid(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return False
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=8,
                **_popen_kwargs(),
            )
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 15)
        return True
    except OSError:
        try:
            os.kill(pid, 9)
            return True
        except OSError:
            return False


def stop_owned_ngrok() -> dict[str, Any]:
    """Stop only the ngrok process Studio started (Popen or pid file)."""
    global _proc, _started_at
    with _lock:
        stopped = False
        pid = None
        if _proc is not None and _proc.poll() is None:
            pid = int(_proc.pid)
            try:
                _proc.terminate()
                _proc.wait(timeout=4)
                stopped = True
            except Exception:
                try:
                    _proc.kill()
                    stopped = True
                except Exception:
                    stopped = _kill_pid(pid)
            _proc = None
        else:
            _proc = None
            pid = _read_pid_file()
            if pid and _pid_alive(pid):
                stopped = _kill_pid(pid)
        _clear_pid_file()
        _started_at = None
        status = ngrok_status()
        if stopped:
            status["detail"] = f"Stopped ngrok (pid {pid})." if pid else "Stopped ngrok."
            status["stopped"] = True
        else:
            status["detail"] = "Nothing to stop — Studio has no owned ngrok process."
            status["stopped"] = False
        return status


def start_ngrok(*, wait: float = 2.5) -> dict[str, Any]:
    """Spawn managed ngrok with the reserved URL → Studio local port + basic auth."""
    global _proc, _reader, _last_error, _started_at
    with _lock:
        creds = ensure_basic_auth_credentials()
        username = creds["username"]
        password = creds["password"]
        policy = write_basic_auth_policy(username, password)

        cfg = config_from_settings()
        public_url = cfg["public_url"]
        local_port = int(cfg["local_port"])
        exe = find_ngrok()
        if not public_url:
            status = ngrok_status()
            status["ok"] = False
            status["error"] = (
                "Set your reserved ngrok public URL in Settings → Ngrok before starting the tunnel."
            )
            status["detail"] = status["error"]
            return status
        if not exe:
            status = ngrok_status()
            status["ok"] = False
            status["error"] = (
                "ngrok was not found on PATH or in common install folders. "
                "Install from https://ngrok.com/download and ensure `ngrok` works in a terminal, "
                "or set NGROK_PATH to the binary."
            )
            status["detail"] = status["error"]
            return status

        token_state = ensure_ngrok_authtoken(exe)
        if not token_state.get("configured"):
            status = ngrok_status()
            status["ok"] = False
            status["error"] = token_state.get("detail") or "ngrok authtoken missing."
            status["detail"] = status["error"]
            status["authtoken_configured"] = False
            return status

        if _owned_running():
            status = ngrok_status(reveal_password=True)
            status["detail"] = "ngrok is already running (Studio-owned)."
            return status

        _last_error = ""
        _output.clear()
        cmd_line = build_command_line(local_port, public_url, basic_auth=True)
        log = _log_path().open("a", encoding="utf-8")
        try:
            log.write(
                f"\n--- start {time.strftime('%Y-%m-%d %H:%M:%S')} :: "
                f"{cmd_line} ---\n"
            )
            log.flush()
        except Exception:
            pass

        args = [
            exe,
            "http",
            str(local_port),
            "--url",
            public_url,
            "--traffic-policy-file",
            str(policy),
        ]
        child_env = os.environ.copy()
        token = ngrok_authtoken()
        if token:
            child_env["NGROK_AUTHTOKEN"] = token
        try:
            _proc = subprocess.Popen(
                args,
                cwd=str(USER_DATA),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=child_env,
                **_popen_kwargs(),
            )
        except Exception as exc:
            try:
                log.close()
            except Exception:
                pass
            status = ngrok_status()
            status["ok"] = False
            status["error"] = f"Failed to launch ngrok: {exc}"
            status["detail"] = status["error"]
            return status

        _write_pid_file(int(_proc.pid))
        _started_at = time.time()
        _reader = threading.Thread(
            target=_drain_reader,
            args=(_proc, log),
            name="ngrok-stdout",
            daemon=True,
        )
        _reader.start()

        deadline = time.time() + max(0.5, float(wait))
        while time.time() < deadline:
            if _proc.poll() is not None:
                break
            time.sleep(0.15)

        if _proc.poll() is not None:
            code = _proc.returncode
            err = _last_error or (_output[-1] if _output else f"ngrok exited with code {code}")
            _proc = None
            _clear_pid_file()
            _started_at = None
            status = ngrok_status()
            status["ok"] = False
            status["error"] = err
            blob = f"{err}\n{status.get('last_output') or ''}".lower()
            if "authtoken" in blob or "err_ngrok_107" in blob:
                status["detail"] = (
                    "ngrok rejected the agent authtoken (often ERR_NGROK_107 — revoked/reset). "
                    "Get a fresh token at https://dashboard.ngrok.com/get-started/your-authtoken "
                    "then run: ngrok config add-authtoken YOUR_TOKEN — and click Start again."
                )
            else:
                status["detail"] = (
                    f"ngrok exited immediately ({err}). "
                    "If this mentions authtoken, run `ngrok config add-authtoken <token>` once."
                )
            return status

        status = ngrok_status(reveal_password=True)
        status["detail"] = (
            f"ngrok started → {public_url} (local :{local_port}) with basic auth "
            f"user={username}. Remote MCP: {public_url.rstrip('/')}/mcp "
            f"(HTTP Basic only — {username} + password from this card)."
        )
        status["basic_auth_generated"] = bool(creds.get("generated"))
        return status


def ensure_ngrok_autostart() -> dict[str, Any] | None:
    """If settings.ngrok_autostart, start the tunnel (best-effort)."""
    cfg = config_from_settings()
    if not cfg.get("ngrok_autostart"):
        return None
    try:
        return start_ngrok()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "detail": str(exc)}


def save_ngrok_settings(
    *,
    url: str | None = None,
    local_port: int | None = None,
    autostart: bool | None = None,
    basic_auth_user: str | None = None,
    basic_auth_password: str | None = None,
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if url is not None:
        updates["ngrok_url"] = normalize_public_url(url)
    if local_port is not None:
        updates["ngrok_local_port"] = normalize_local_port(local_port)
    if autostart is not None:
        updates["ngrok_autostart"] = normalize_autostart(autostart)
    if basic_auth_user is not None:
        updates["ngrok_basic_auth_user"] = str(basic_auth_user).strip() or DEFAULT_BASIC_AUTH_USER
    if basic_auth_password is not None and str(basic_auth_password).strip() and str(basic_auth_password).strip() != "********":
        updates["ngrok_basic_auth_password"] = str(basic_auth_password).strip()
    if updates:
        save_settings(updates)
    return config_from_settings()


def ngrok_status(*, reveal_password: bool = False) -> dict[str, Any]:
    cfg = config_from_settings()
    settings = load_settings()
    exe = find_ngrok()
    running = _owned_running()
    pid = _owned_pid() if running else None
    recent = list(_output)[-12:]
    error = _last_error or None
    username = cfg["ngrok_basic_auth_user"]
    password = str(settings.get("ngrok_basic_auth_password") or "")
    token_set = bool(ngrok_authtoken())
    # Detect existing agent config without reading the secret value.
    ngrok_config = Path.home() / ".config" / "ngrok" / "ngrok.yml"
    if not ngrok_config.is_file():
        ngrok_config = Path.home() / ".ngrok2" / "ngrok.yml"
    config_present = ngrok_config.is_file()
    authtoken_configured = token_set or config_present
    if not exe:
        detail = "ngrok binary not found."
        ok = False
    elif not authtoken_configured:
        detail = (
            "ngrok binary found, but authtoken is missing. "
            "Set NGROK_AUTHTOKEN in .env or run: ngrok config add-authtoken YOUR_TOKEN"
        )
        ok = False
    elif running:
        detail = (
            f"Tunnel up: {cfg['public_url']} -> 127.0.0.1:{cfg['local_port']} "
            f"(basic auth user={username})"
        )
        ok = True
        error = None
    elif error:
        detail = error
        ok = False
    else:
        detail = "ngrok is stopped."
        ok = False
    out: dict[str, Any] = {
        "ok": ok,
        "running": running,
        "pid": pid,
        "ngrok_found": bool(exe),
        "ngrok_path": exe,
        "authtoken_configured": authtoken_configured,
        "authtoken_env_set": token_set,
        "public_url": cfg["public_url"],
        "mcp_url": cfg["mcp_url"],
        "local_port": cfg["local_port"],
        "ngrok_url": cfg["ngrok_url"],
        "ngrok_local_port": cfg["local_port"],
        "ngrok_autostart": cfg["ngrok_autostart"],
        "basic_auth_user": username,
        "ngrok_basic_auth_user": username,
        "basic_auth_password_set": bool(password),
        "ngrok_basic_auth_password_set": bool(password),
        "basic_auth_password": "********" if password else "",
        "command": cfg["command"],
        "started_at": _started_at,
        "error": error,
        "last_output": "\n".join(recent),
        "detail": detail,
        "log_path": str(_log_path()),
        "traffic_policy_path": str(_policy_path()),
    }
    if reveal_password and password:
        out["basic_auth_password"] = password
        out["basic_auth_password_revealed"] = True
    elif _last_revealed_password and reveal_password:
        out["basic_auth_password"] = _last_revealed_password
        out["basic_auth_password_revealed"] = True
    return out
