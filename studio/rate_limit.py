"""In-process sliding-window rate limits keyed by client IP."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

# Defaults (overridable via settings.json)
DEFAULT_LOGIN_LIMIT = 8
DEFAULT_LOGIN_WINDOW_SEC = 15 * 60
DEFAULT_SIGNUP_LIMIT = 5
DEFAULT_SIGNUP_WINDOW_SEC = 15 * 60
DEFAULT_API_LIMIT = 180
DEFAULT_API_WINDOW_SEC = 60
DEFAULT_MCP_LIMIT = 600
DEFAULT_MCP_WINDOW_SEC = 60
DEFAULT_STATIC_LIMIT = 600
DEFAULT_STATIC_WINDOW_SEC = 60

_lock = threading.Lock()
# key -> deque of request timestamps (monotonic)
_windows: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    """Best-effort client IP.

    Behind a single trusted reverse proxy (ngrok), use the *rightmost*
    X-Forwarded-For hop — that is the address the proxy observed. Clients can
    spoof left-side values; we do not trust them when a proxy header is present.
    Falls back to X-Real-IP, then ASGI client host.
    """
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _cfg() -> dict[str, Any]:
    try:
        from studio.settings import load_settings

        data = load_settings()
    except Exception:
        data = {}
    return {
        "login_limit": _pos_int(data.get("rate_limit_login"), DEFAULT_LOGIN_LIMIT),
        "login_window": _pos_int(data.get("rate_limit_login_window_sec"), DEFAULT_LOGIN_WINDOW_SEC),
        "signup_limit": _pos_int(data.get("rate_limit_signup"), DEFAULT_SIGNUP_LIMIT),
        "signup_window": _pos_int(data.get("rate_limit_signup_window_sec"), DEFAULT_SIGNUP_WINDOW_SEC),
        "api_limit": _pos_int(data.get("rate_limit_api"), DEFAULT_API_LIMIT),
        "api_window": _pos_int(data.get("rate_limit_api_window_sec"), DEFAULT_API_WINDOW_SEC),
        "mcp_limit": _pos_int(data.get("rate_limit_mcp"), DEFAULT_MCP_LIMIT),
        "mcp_window": _pos_int(data.get("rate_limit_mcp_window_sec"), DEFAULT_MCP_WINDOW_SEC),
        "static_limit": _pos_int(data.get("rate_limit_static"), DEFAULT_STATIC_LIMIT),
        "static_window": _pos_int(data.get("rate_limit_static_window_sec"), DEFAULT_STATIC_WINDOW_SEC),
        "enabled": _truthy(data.get("rate_limit_enabled"), True),
    }


def _pos_int(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _bucket_key(kind: str, ip: str) -> str:
    return f"{kind}:{ip}"


def _prune(q: deque[float], now: float, window: float) -> None:
    cutoff = now - window
    while q and q[0] < cutoff:
        q.popleft()


def check(kind: str, ip: str, *, limit: int, window_sec: int, record: bool = True) -> tuple[bool, dict[str, str]]:
    """Return (allowed, rate-limit headers). Records the hit when allowed and record=True."""
    now = time.monotonic()
    key = _bucket_key(kind, ip or "unknown")
    with _lock:
        q = _windows[key]
        _prune(q, now, float(window_sec))
        remaining = max(0, limit - len(q))
        reset_in = int(max(1, (q[0] + window_sec) - now)) if q else window_sec
        headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(max(0, remaining - (1 if record and remaining else 0))),
            "X-RateLimit-Reset": str(reset_in),
            "Retry-After": str(reset_in),
        }
        if len(q) >= limit:
            headers["X-RateLimit-Remaining"] = "0"
            return False, headers
        if record:
            q.append(now)
            headers["X-RateLimit-Remaining"] = str(max(0, limit - len(q)))
        return True, headers


def record_hit(kind: str, ip: str, *, window_sec: int | None = None) -> None:
    """Count a failed auth attempt against the login/signup bucket."""
    cfg = _cfg()
    if not cfg["enabled"]:
        return
    if window_sec is None:
        if kind == "login":
            window_sec = cfg["login_window"]
        elif kind == "signup":
            window_sec = cfg["signup_window"]
        else:
            window_sec = cfg["api_window"]
    now = time.monotonic()
    key = _bucket_key(kind, ip or "unknown")
    with _lock:
        q = _windows[key]
        _prune(q, now, float(window_sec))
        q.append(now)


def clear_login_limits(ip: str | None = None) -> None:
    """Clear login/signup buckets (all IPs, or one IP)."""
    with _lock:
        if not ip:
            for key in list(_windows.keys()):
                if key.startswith("login:") or key.startswith("signup:"):
                    _windows.pop(key, None)
            return
        _windows.pop(_bucket_key("login", ip), None)
        _windows.pop(_bucket_key("signup", ip), None)


def classify_path(path: str, method: str) -> str | None:
    """Return rate-limit bucket kind, or None to skip."""
    if path.startswith("/static/") or path == "/static":
        return "static"
    if path in ("/", "/favicon.ico"):
        return "static"
    # High-frequency local polls — do not burn the API budget.
    if path in ("/api/health", "/api/auth/me", "/api/gentle"):
        return None
    if method.upper() == "GET" and path.startswith("/api/projects/") and path.endswith("/job"):
        return None
    # Login/signup are checked without recording here; failures record in the handlers.
    if path == "/api/auth/login" and method.upper() == "POST":
        return "login"
    if path == "/api/auth/signup" and method.upper() == "POST":
        return "signup"
    if path in ("/api/auth/forgot-password", "/api/auth/reset-password") and method.upper() == "POST":
        return "login"
    if path.startswith("/api/"):
        return "api"
    if path == "/mcp" or path.startswith("/mcp/"):
        return "mcp"
    return None


def enforce(request: Request) -> JSONResponse | None:
    """If over limit, return a 429 JSONResponse; else None (and record the hit for non-auth)."""
    cfg = _cfg()
    if not cfg["enabled"]:
        return None
    path = request.url.path
    kind = classify_path(path, request.method)
    if kind is None:
        return None
    if kind == "login":
        limit, window = cfg["login_limit"], cfg["login_window"]
        detail = (
            f"Too many login attempts. Limit is {limit} per {window // 60} minutes "
            "from this IP. Try again later."
        )
        # Peek only — successful logins must not burn the quota.
        record = False
    elif kind == "signup":
        limit, window = cfg["signup_limit"], cfg["signup_window"]
        detail = (
            f"Too many signups from this IP. Limit is {limit} per {window // 60} minutes. "
            "Try again later."
        )
        record = True
    elif kind == "static":
        limit, window = cfg["static_limit"], cfg["static_window"]
        detail = f"Too many requests. Limit is {limit} per {window}s from this IP."
        record = True
    elif kind == "mcp":
        limit, window = cfg["mcp_limit"], cfg["mcp_window"]
        detail = f"Too many MCP requests. Limit is {limit} per {window}s from this IP."
        record = True
    else:
        limit, window = cfg["api_limit"], cfg["api_window"]
        detail = f"Too many API requests. Limit is {limit} per {window}s from this IP."
        record = True

    ip = client_ip(request)
    ok, headers = check(kind, ip, limit=limit, window_sec=window, record=record)
    if ok:
        try:
            request.state.rate_limit_headers = headers
        except Exception:
            pass
        return None
    return JSONResponse(
        {"detail": detail, "rate_limited": True, "retry_after": int(headers.get("Retry-After") or 1)},
        status_code=429,
        headers=headers,
    )


def note_login_failure(request: Request) -> None:
    """Record a failed login/forgot/reset attempt for this client IP."""
    record_hit("login", client_ip(request))


def reset_for_tests() -> None:
    with _lock:
        _windows.clear()
