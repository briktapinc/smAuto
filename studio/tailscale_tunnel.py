"""Tailscale Funnel status for Stickman Automation Studio (public MCP).

Funnel proxies this machine's Studio port to https://<node>.<tailnet>.ts.net.
Auth stays the MCP PIN — Funnel does not add HTTP Basic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CACHE_TTL_SEC = 8.0
_cache_at: float = 0.0
_cache: dict[str, Any] | None = None

COMMON_WIN_PATHS = (
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Tailscale" / "tailscale.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Tailscale" / "tailscale.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Tailscale" / "tailscale.exe",
)


def _popen_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {}
    if os.name == "nt" and CREATE_NO_WINDOW:
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


def tailscale_bin() -> str | None:
    env = (os.environ.get("TAILSCALE_PATH") or "").strip()
    if env and Path(env).is_file():
        return env
    found = shutil.which("tailscale")
    if found:
        return found
    for path in COMMON_WIN_PATHS:
        if path.is_file():
            return str(path)
    return None


def invalidate_tailscale_cache() -> None:
    global _cache_at, _cache
    _cache_at = 0.0
    _cache = None


def _run(args: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    bin_path = tailscale_bin()
    if not bin_path:
        raise FileNotFoundError("tailscale executable not found")
    return subprocess.run(
        [bin_path, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        **_popen_kwargs(),
    )


def _public_from_funnel_json(payload: dict[str, Any]) -> tuple[str, str]:
    """Return (https origin, proxy target) for the first Funnel-enabled web host."""
    web = payload.get("Web") if isinstance(payload.get("Web"), dict) else {}
    allow = payload.get("AllowFunnel") if isinstance(payload.get("AllowFunnel"), dict) else {}
    for key, spec in web.items():
        if allow and not allow.get(key):
            continue
        host = str(key).split(":")[0].strip()
        if not host:
            continue
        proxy = ""
        handlers = spec.get("Handlers") if isinstance(spec, dict) else None
        if isinstance(handlers, dict):
            for handler in handlers.values():
                if isinstance(handler, dict) and handler.get("Proxy"):
                    proxy = str(handler.get("Proxy") or "")
                    break
        return f"https://{host}", proxy
    return "", ""


def tailscale_status(*, fresh: bool = False) -> dict[str, Any]:
    """Funnel status. Cached briefly so health/settings polls do not spawn tailscale."""
    global _cache_at, _cache
    now = time.monotonic()
    if not fresh and _cache is not None and (now - _cache_at) < _CACHE_TTL_SEC:
        return dict(_cache)

    bin_path = tailscale_bin()
    out: dict[str, Any] = {
        "ok": bool(bin_path),
        "tailscale_found": bool(bin_path),
        "running": False,
        "public_url": "",
        "mcp_url": "",
        "local_target": "",
        "detail": "Tailscale is not installed." if not bin_path else "Funnel is off.",
        "error": "",
    }
    if not bin_path:
        _cache = dict(out)
        _cache_at = now
        return out
    try:
        proc = _run(["funnel", "status", "--json"], timeout=12)
    except (OSError, subprocess.TimeoutExpired) as exc:
        out["ok"] = False
        out["error"] = str(exc)
        out["detail"] = "Could not read Tailscale Funnel status."
        _cache = dict(out)
        _cache_at = now
        return out
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0 or not raw:
        err = (proc.stderr or proc.stdout or "").strip()
        out["ok"] = False
        out["error"] = err[:500]
        out["detail"] = err[:240] or "Tailscale Funnel status failed. Sign in to Tailscale, then try again."
        _cache = dict(out)
        _cache_at = now
        return out
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        out["ok"] = False
        out["detail"] = "Tailscale returned a status Studio could not read."
        _cache = dict(out)
        _cache_at = now
        return out
    if not isinstance(payload, dict):
        _cache = dict(out)
        _cache_at = now
        return out
    public, proxy = _public_from_funnel_json(payload)
    if public:
        out["running"] = True
        out["public_url"] = public
        out["mcp_url"] = f"{public}/mcp"
        out["local_target"] = proxy
        out["detail"] = f"Funnel on: {public}/mcp"
    else:
        out["detail"] = "Tailscale is installed. Funnel is off."
    _cache = dict(out)
    _cache_at = now
    return out


def start_tailscale_funnel(port: int) -> dict[str, Any]:
    """Expose Studio's listen port on the node's Funnel HTTPS URL."""
    invalidate_tailscale_cache()
    if not tailscale_bin():
        return tailscale_status(fresh=True)
    try:
        port_i = int(port)
    except (TypeError, ValueError):
        port_i = 7878
    if port_i < 1 or port_i > 65535:
        port_i = 7878
    try:
        proc = _run(["funnel", "--bg", "--yes", str(port_i)], timeout=40)
    except (OSError, subprocess.TimeoutExpired) as exc:
        status = tailscale_status(fresh=True)
        status["ok"] = False
        status["error"] = str(exc)
        status["detail"] = "Tailscale Funnel did not start."
        return status
    status = tailscale_status(fresh=True)
    if proc.returncode != 0 and not status.get("running"):
        err = (proc.stderr or proc.stdout or "").strip()
        status["ok"] = False
        status["error"] = err[:500]
        status["detail"] = err[:240] or "Tailscale Funnel did not start."
    elif status.get("running"):
        status["detail"] = f"Funnel on: {status.get('mcp_url')}"
    return status


def stop_tailscale_funnel() -> dict[str, Any]:
    """Turn Funnel off. Tailscale itself stays signed in."""
    invalidate_tailscale_cache()
    if not tailscale_bin():
        return tailscale_status(fresh=True)
    try:
        proc = _run(["funnel", "--yes", "reset"], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        status = tailscale_status(fresh=True)
        status["ok"] = False
        status["error"] = str(exc)
        status["detail"] = "Could not stop Tailscale Funnel."
        return status
    status = tailscale_status(fresh=True)
    if proc.returncode != 0 and status.get("running"):
        err = (proc.stderr or proc.stdout or "").strip()
        status["ok"] = False
        status["error"] = err[:500]
        status["detail"] = err[:240] or "Funnel is still on."
    elif not status.get("running"):
        status["detail"] = "Tailscale Funnel stopped."
    return status
