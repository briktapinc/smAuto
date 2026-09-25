"""JWT login for Stickman Automation Studio (HTTP UI + /api) and MCP HTTP auth.

Credentials live in user_data/auth.json (bcrypt hash + JWT secret).
First boot defaults: username admin / password bubblepod
  — override with BUBBLEPOD_USER / BUBBLEPOD_PASSWORD (or LAZYKH_*).
To change later: Settings → Account (change password), or from the server:

    cd /root/stickmanautomation && .venv/bin/python -m studio.auth set-password admin 'NewPasswordHere'

That bumps token_version (invalidates old JWTs) without recreating jwt_secret.
Env reset (new auth.json only): BUBBLEPOD_RESET_AUTH=1 with BUBBLEPOD_USER /
BUBBLEPOD_PASSWORD and restart — also run set-password for the members store.

HTTP /mcp requires a Studio JWT (per-user) for identity, OR the shared MCP
connection PIN (header X-MCP-Pin / ?mcp_pin=…) which authenticates as the
primary admin (ChatGPT Desktop URL connectors). An optional PIN may also be
sent together with a JWT; a wrong PIN is rejected. Stdio MCP
(`python -m studio.mcp_server`) stays local and is not JWT-gated.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import HTTPException, Request, Response

from studio.paths import USER_DATA, ensure_dirs

AUTH_PATH = USER_DATA / "auth.json"
COOKIE_NAME = "bubblepod_token"
TOKEN_TTL_SEC = 60 * 60 * 24 * 14  # 14 days
DEFAULT_USER = "admin"
DEFAULT_PASSWORD = "bubblepod"
ALGORITHM = "HS256"


def is_desktop_mode() -> bool:
    """True when launched from the Electron/.exe app (single-user, no SaaS auth)."""
    raw = (
        os.environ.get("BUBBLEPOD_DESKTOP")
        or os.environ.get("LAZYKH_DESKTOP")
        or ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def desktop_local_user() -> dict[str, Any]:
    """Synthetic local admin for desktop mode (no JWT / multi-tenant session)."""
    from studio.members import ensure_members_store, get_user_by_username, list_users

    ensure_members_store()
    for user in list_users(include_disabled=False):
        if (user.get("role") or "") == "admin" and user.get("username"):
            return user
    user = get_user_by_username(DEFAULT_USER)
    if user and not user.get("disabled"):
        return user
    raise HTTPException(status_code=500, detail="Desktop local admin missing")

MCP_PIN_HEADERS = (
    "X-MCP-Pin",
    "X-BubblePod-MCP-Pin",
    "X-MCP-PIN",
    "X-Bubblepod-Mcp-Pin",
)

# Public HTTP paths (no JWT). Static is open; /mcp is gated separately.
# /api/mcp/download/{token} is token-gated (see is_mcp_download_path), not JWT.
PUBLIC_API_PATHS = frozenset({
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/signup",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/billing/config",
    "/api/stripe/webhook",
    "/api/youtube/oauth/callback",
})

# Authenticated users may hit these even without an active membership.
MEMBERSHIP_EXEMPT_PREFIXES = (
    "/api/auth/",
    "/api/billing/",
    "/api/stripe/",
)

MEMBERSHIP_EXEMPT_PATHS = frozenset({
    "/api/health",
})

# Job control that does not create/advance generation — always allowed when authenticated.
_MEMBERSHIP_ALLOWED_PROJECT_ACTIONS = frozenset({"pause", "stop"})
_MEMBERSHIP_ALLOWED_TOPIC_ACTIONS = frozenset({"unschedule"})

# MCP tools that create/edit jobs, generate topics, or advance video production.
# Read-only tools, delete_project/delete_topic, unschedule_topic, pause/stop stay open.
MEMBERSHIP_GATED_MCP_TOOLS = frozenset({
    "generate_topics",
    "create_topic",
    "update_topic",
    "schedule_topic",
    "start_topic_pipeline",
    "set_hands_off",
    "create_video_project",
    "set_video_aspect",
    "rename_video",
    "generate_script_via_api",
    "save_script",
    "set_image_provider",
    "set_video_layout",
    "set_character_size",
    "set_include_bubblehead",
    "set_project_voice",
    "generate_illustrations_with_flux",
    "generate_illustrations",
    "generate_cover",
    "regenerate_cover",
    "refresh_covers",
    "set_cover_provider",
    "set_active_cover",
    "regenerate_illustration",
    "convert_illustration_to_clip",
    "convert_all_illustrations_to_clips",
    "set_fal_video_model",
    "save_illustration_image",
    "save_illustration_images",
    "generate_speech",
    "upload_narration_audio",
    "align_phonemes",
    "render_final_video",
    "start_job",
    "resume_job",
    "set_project_background",
    "shuffle_job_music",
    "set_job_music",
    "set_project_youtube",
    "upload_to_youtube",
    "create_api_key",
})

DOCS_PATHS = frozenset({
    "/docs",
    "/redoc",
    "/openapi.json",
})

MEMBERSHIP_HTTP_DETAIL = {
    "detail": "Membership required",
    "code": "membership_required",
    "billing_url": "/pricing",
}


def _env(name: str, *alts: str) -> str:
    for key in (name, *alts):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            return raw
    return ""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


# Back-compat aliases used elsewhere
_hash_password = hash_password
_verify_password = verify_password


def _read_auth_file() -> dict[str, Any] | None:
    if not AUTH_PATH.is_file():
        return None
    try:
        import json

        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if not data.get("username") or not data.get("password_hash") or not data.get("jwt_secret"):
            return None
        return data
    except Exception:
        return None


def _write_auth_file(data: dict[str, Any]) -> None:
    import json

    ensure_dirs()
    AUTH_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def ensure_auth_config() -> dict[str, Any]:
    """Load or create auth.json. Env reset recreates credentials."""
    reset = _env("BUBBLEPOD_RESET_AUTH", "LAZYKH_RESET_AUTH").lower() in ("1", "true", "yes")
    existing = None if reset else _read_auth_file()
    if existing is not None:
        return existing

    username = _env("BUBBLEPOD_USER", "LAZYKH_USER") or DEFAULT_USER
    password = _env("BUBBLEPOD_PASSWORD", "LAZYKH_PASSWORD") or DEFAULT_PASSWORD
    secret = secrets.token_urlsafe(48)
    data = {
        "username": username,
        "password_hash": hash_password(password),
        "jwt_secret": secret,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_auth_file(data)
    return data


def get_auth_config() -> dict[str, Any]:
    return ensure_auth_config()


def issue_token(username: str, *, user_id: str = "", role: str = "", token_version: int | None = None) -> tuple[str, int]:
    cfg = get_auth_config()
    expires_in = TOKEN_TTL_SEC
    exp = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    payload: dict[str, Any] = {"sub": username, "exp": exp}
    if user_id:
        payload["uid"] = user_id
    if role:
        payload["role"] = role
    tv = 0 if token_version is None else int(token_version)
    payload["tv"] = tv
    token = jwt.encode(
        payload,
        cfg["jwt_secret"],
        algorithm=ALGORITHM,
    )
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token, expires_in


def decode_token(token: str) -> dict[str, Any]:
    cfg = get_auth_config()
    try:
        payload = jwt.decode(token, cfg["jwt_secret"], algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc
    if not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid token")
    return payload


def _cookie_secure() -> bool:
    """Set Secure on cookies when serving over HTTPS (Hostinger / public_base_url)."""
    flag = (os.environ.get("BUBBLEPOD_COOKIE_SECURE") or os.environ.get("LAZYKH_COOKIE_SECURE") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    try:
        from studio.settings import resolve_public_base_url

        base = resolve_public_base_url().strip().lower()
        if base.startswith("https://"):
            return True
    except Exception:
        pass
    return False


def _cookie_path() -> str:
    """Scope auth cookie to Studio subpath when mounted under /app (etc.)."""
    try:
        from studio.settings import studio_url_prefix

        prefix = studio_url_prefix()
        return prefix or "/"
    except Exception:
        return "/"


def set_auth_cookie(response: Response, token: str, max_age: int) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        path=_cookie_path(),
        secure=_cookie_secure(),
    )


def authenticate(username: str, password: str) -> bool:
    """Back-compat boolean check. Prefer authenticate_user() for sessions."""
    try:
        from studio.members import authenticate_user

        return authenticate_user(username, password) is not None
    except Exception:
        cfg = get_auth_config()
        if username != cfg["username"]:
            return False
        return verify_password(password, cfg["password_hash"])


def change_password(current_password: str, new_password: str, *, username: str | None = None) -> dict[str, Any]:
    """Verify current password and store a new bcrypt hash."""
    try:
        from studio.members import get_user_by_username, set_password_for_username, ensure_members_store

        ensure_members_store()
        name = (username or get_auth_config().get("username") or "").strip()
        user = get_user_by_username(name)
        if user:
            set_password_for_username(name, current_password or "", new_password or "")
            return {"ok": True, "username": name}
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except FileNotFoundError:
        pass
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cfg = get_auth_config()
    if not verify_password(current_password or "", cfg["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    new_pw = (new_password or "").strip()
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    if new_pw == (current_password or ""):
        raise HTTPException(status_code=400, detail="New password must differ from the current password")
    cfg["password_hash"] = hash_password(new_pw)
    cfg["password_changed_at"] = datetime.now(timezone.utc).isoformat()
    _write_auth_file(cfg)
    return {"ok": True, "username": cfg["username"]}


def extract_tokens(request: Request) -> list[str]:
    """Candidate JWTs: HttpOnly cookie first, then Authorization Bearer.

    Browser fetches send both after login. Trying cookie first avoids a reload
    loop when localStorage Bearer lags behind a password-change cookie refresh.
    Bearer-only clients (MCP, curl) still work when no cookie is set. Callers
    that need resilience try these in order (see require_session).
    """
    seen: set[str] = set()
    out: list[str] = []
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and cookie.strip():
        tok = cookie.strip()
        seen.add(tok)
        out.append(tok)
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token and token not in seen:
            out.append(token)
    return out


def extract_token(request: Request) -> str | None:
    """Return the preferred session JWT (cookie first, then Bearer)."""
    tokens = extract_tokens(request)
    return tokens[0] if tokens else None


def _user_from_access_token(token: str) -> dict[str, Any]:
    """Resolve members-store user for a JWT; raise HTTPException on failure."""
    from studio.members import get_user_by_id, get_user_by_username

    payload = decode_token(token)
    uid = str(payload.get("uid") or "").strip()
    user = get_user_by_id(uid) if uid else None
    if user is None:
        user = get_user_by_username(str(payload.get("sub") or ""))
    if user is None or user.get("disabled"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    expected_tv = int(user.get("token_version") or 0)
    got_tv = int(payload.get("tv") or 0)
    if got_tv != expected_tv:
        raise HTTPException(status_code=401, detail="Session expired — sign in again")
    return user


def require_user(request: Request) -> str:
    if is_desktop_mode():
        return str(desktop_local_user().get("username") or DEFAULT_USER)
    user = require_session(request)
    return str(user.get("username") or "")


def require_session(request: Request) -> dict[str, Any]:
    """Return the members-store user for this JWT or bp_live_ API key."""
    if is_desktop_mode():
        return desktop_local_user()
    from studio.members import ensure_members_store

    ensure_members_store()

    # Per-user API keys (Authorization: Bearer bp_live_...)
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
        from studio.api_keys import looks_like_api_key, resolve_api_key

        if looks_like_api_key(raw):
            user = resolve_api_key(raw)
            if user is None:
                raise HTTPException(status_code=401, detail="Invalid API key")
            return user

    tokens = extract_tokens(request)
    if not tokens:
        raise HTTPException(status_code=401, detail="Not authenticated")
    last_exc: HTTPException | None = None
    for token in tokens:
        from studio.api_keys import looks_like_api_key

        if looks_like_api_key(token):
            continue
        try:
            return _user_from_access_token(token)
        except HTTPException as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    raise HTTPException(status_code=401, detail="Not authenticated")


def require_admin(request: Request) -> dict[str, Any]:
    if is_desktop_mode():
        return desktop_local_user()
    user = require_session(request)
    if (user.get("role") or "") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def membership_required_enabled() -> bool:
    if is_desktop_mode():
        return False
    try:
        from studio.settings import load_settings, normalize_bool
        from studio.stripe_billing import stripe_configured

        if not normalize_bool(load_settings().get("membership_required"), True):
            return False
        # Enforce only once Stripe keys are present so existing local installs keep working.
        return stripe_configured()
    except Exception:
        return False


def membership_denied_payload() -> dict[str, Any]:
    return dict(MEMBERSHIP_HTTP_DETAIL)


def mcp_tool_requires_membership(tool_name: str) -> bool:
    return (tool_name or "").strip() in MEMBERSHIP_GATED_MCP_TOOLS


def path_requires_membership(path: str, method: str = "GET") -> bool:
    """True when this HTTP call needs an active membership (or admin).

    Unsubscribed members may still read library/jobs/topics and DELETE jobs/topics.
    Create/edit/update of jobs, topic generation/scheduling, and any pipeline
    advance (start/resume/render/script/audio/pictures/…) require membership.
    Settings/prompts/billing/auth stay open. Admins always pass via user_has_access.
    """
    if not membership_required_enabled():
        return False
    if is_public_path(path) or is_docs_path(path) or is_mcp_path(path):
        return False
    if path in MEMBERSHIP_EXEMPT_PATHS:
        return False
    if any(path.startswith(prefix) for prefix in MEMBERSHIP_EXEMPT_PREFIXES):
        return False
    if path.startswith("/api/admin"):
        return False  # admin role checked in handlers
    if path in ("/admin",) or path.startswith("/admin/") or path.startswith("/billing/") or path.startswith("/pricing"):
        return False
    if path == "/" or path.startswith("/static/"):
        return False
    if not path.startswith("/api/"):
        return False

    m = (method or "GET").upper()
    if m in ("GET", "HEAD", "OPTIONS"):
        return False

    # Deletes of jobs/topics (and other library cleanup) stay allowed.
    if m == "DELETE":
        return False

    if path == "/api/projects" and m == "POST":
        return True
    if path.startswith("/api/projects/"):
        parts = [p for p in path[len("/api/projects/") :].split("/") if p]
        if not parts:
            return False
        if len(parts) == 1:
            # PATCH/PUT job fields (script is PUT …/script under len>=2)
            return m in ("PATCH", "PUT", "POST")
        action = parts[1]
        if action in _MEMBERSHIP_ALLOWED_PROJECT_ACTIONS and m == "POST":
            return False
        return True

    if path.startswith("/api/jobs/"):
        parts = [p for p in path[len("/api/jobs/") :].split("/") if p]
        if len(parts) >= 2 and parts[1] in _MEMBERSHIP_ALLOWED_PROJECT_ACTIONS and m == "POST":
            return False
        if len(parts) >= 2 and parts[1] in ("start", "resume") and m == "POST":
            return True
        return m in ("POST", "PUT", "PATCH")

    if path in ("/api/topics", "/api/topics/generate", "/api/topics/schedule") and m == "POST":
        return True
    if path.startswith("/api/topics/"):
        parts = [p for p in path[len("/api/topics/") :].split("/") if p]
        if not parts:
            return False
        if len(parts) == 1 and m == "PATCH":
            return True
        if len(parts) >= 2 and m == "POST":
            if parts[1] in _MEMBERSHIP_ALLOWED_TOPIC_ACTIONS:
                return False
            if parts[1] == "schedule":
                return True
        return False

    # Settings, prompts, music library, YouTube connect, poses, etc. stay open.
    return False


def require_user_membership(user: dict[str, Any] | None) -> None:
    """Raise HTTP 402 when membership is required and the user has no access."""
    if not membership_required_enabled():
        return
    from studio.members import user_has_access

    if user_has_access(user):
        return
    raise HTTPException(status_code=402, detail=membership_denied_payload())


def bound_mcp_user_has_access(request: Request | None) -> bool | None:
    """Whether the MCP caller (JWT or PIN→admin) has membership."""
    if not membership_required_enabled():
        return True
    if request is None:
        return False
    username = try_jwt_user(request)
    if not username:
        try:
            username = getattr(request.state, "mcp_username", None)
        except Exception:
            username = None
    if not username:
        return False
    from studio.members import get_user_by_username, user_has_access

    user = get_user_by_username(str(username))
    return bool(user_has_access(user))


def enforce_mcp_tool_membership(tool_name: str, request: Request | None = None) -> None:
    """Block gated MCP tools when the caller lacks membership."""
    if not mcp_tool_requires_membership(tool_name):
        return
    access = bound_mcp_user_has_access(request)
    if access:
        return
    raise PermissionError(
        "Membership required to create/edit jobs, generate topics, or run video production. "
        "Connect with a Studio JWT, Bearer bp_live_… API key, or the admin MCP PIN (?mcp_pin=…). "
        "Delete/read-only tools remain available."
    )


def clear_auth_cookie(response: Response) -> None:
    """Expire the auth cookie with attributes matching set_auth_cookie.

    Chromium ignores delete Set-Cookie unless Path/Secure/SameSite match the
    original cookie. Login sets Secure on HTTPS; delete_cookie defaults to
    secure=False, which left sessions alive after Logout under /app.
    Also clear path=/ in case an older cookie predates ROOT_PATH=/app.
    """
    secure = _cookie_secure()
    paths = {_cookie_path(), "/"}
    for path in paths:
        response.delete_cookie(
            key=COOKIE_NAME,
            path=path,
            secure=secure,
            httponly=True,
            samesite="lax",
        )


def looks_like_jwt(token: str) -> bool:
    parts = (token or "").split(".")
    return len(parts) == 3 and all(parts) and (token.startswith("eyJ") or token.startswith("ey"))


def mcp_pin_configured() -> bool:
    from studio.settings import load_settings

    return bool((load_settings().get("mcp_pin_hash") or "").strip())


def verify_mcp_pin(pin: str) -> bool:
    from studio.settings import load_settings

    raw = (pin or "").strip()
    if not raw:
        return False
    stored = (load_settings().get("mcp_pin_hash") or "").strip()
    if not stored:
        return False
    return verify_password(raw, stored)


def extract_mcp_pin(request: Request) -> str | None:
    """PIN from headers, initialize meta, mcp-pin auth scheme, or query (ChatGPT URL)."""
    for header in MCP_PIN_HEADERS:
        value = (request.headers.get(header) or "").strip()
        if value:
            return value
    try:
        meta = getattr(request.state, "mcp_initialize_meta", None)
        if isinstance(meta, dict):
            for key in ("mcp_pin", "pin", "mcpPin", "authorization"):
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    except Exception:
        pass
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("mcp-pin "):
        token = auth[8:].strip()
        if token:
            return token
    # ChatGPT Desktop HTTP connectors only support a URL — query PIN is required there.
    # Prefer headers when both are present (already returned above).
    try:
        qp = request.query_params.get("mcp_pin") or request.query_params.get("pin")
        if qp and str(qp).strip():
            return str(qp).strip()
    except Exception:
        pass
    return None


def try_jwt_user(request: Request) -> str | None:
    if is_desktop_mode():
        try:
            return str(desktop_local_user().get("username") or DEFAULT_USER)
        except Exception:
            return DEFAULT_USER
    for token in extract_tokens(request):
        if not looks_like_jwt(token):
            continue
        try:
            user = _user_from_access_token(token)
            return str(user.get("username") or "")
        except HTTPException:
            continue
    return None


def _bind_mcp_username(request: Request, username: str, *, method: str) -> None:
    try:
        request.state.mcp_username = username
        request.state.mcp_auth_method = method
    except Exception:
        pass


def _primary_admin_username() -> str | None:
    from studio.members import list_users

    for user in list_users(include_disabled=False):
        if (user.get("role") or "") == "admin" and user.get("username"):
            return str(user["username"])
    return None


def verify_ngrok_tunnel_gate(request: Request) -> bool:
    """True when ngrok stamped X-BubblePod-Tunnel-Auth after successful basic-auth.

    ngrok removes the Authorization header before proxying upstream, so Studio
    never sees Basic credentials on tunnel requests — only this gate header.
    """
    gate = (request.headers.get("X-BubblePod-Tunnel-Auth") or "").strip()
    if not gate:
        return False
    from studio.settings import load_settings

    expected = str(load_settings().get("ngrok_tunnel_gate") or "").strip()
    if not expected:
        return False
    return secrets.compare_digest(gate, expected)


def verify_ngrok_basic_auth(request: Request) -> bool:
    """True when Authorization: Basic matches Settings → Ngrok tunnel credentials."""
    import base64
    import binascii

    auth = request.headers.get("Authorization") or ""
    if not auth.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(auth[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    if ":" not in raw:
        return False
    user, _, password = raw.partition(":")
    from studio.settings import load_settings

    settings = load_settings()
    expected_user = (settings.get("ngrok_basic_auth_user") or "bubblepod").strip() or "bubblepod"
    expected_pass = str(settings.get("ngrok_basic_auth_password") or "")
    if not expected_pass:
        return False
    return secrets.compare_digest(user, expected_user) and secrets.compare_digest(
        password, expected_pass
    )


def authorize_mcp_http(request: Request) -> str:
    """Authorize FastMCP HTTP /mcp.

    Prefer Studio JWT (per-member). ChatGPT Desktop often only sends ngrok HTTP Basic
    and/or ?mcp_pin= — either maps to the primary admin when valid.

    Through ngrok, basic-auth is verified at the edge and Authorization is stripped;
    Studio then accepts X-BubblePod-Tunnel-Auth stamped by the traffic policy.
    """
    user = try_jwt_user(request)
    if user:
        if mcp_pin_configured():
            pin = extract_mcp_pin(request)
            if pin and not verify_mcp_pin(pin):
                raise HTTPException(status_code=401, detail="Invalid MCP PIN")
        _bind_mcp_username(request, user, method="jwt")
        return user

    # Per-user API key (Bearer bp_live_...) — scoped to that user, not owner PIN.
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
        from studio.api_keys import looks_like_api_key, resolve_api_key

        if looks_like_api_key(raw):
            keyed = resolve_api_key(raw)
            if keyed is None:
                raise HTTPException(status_code=401, detail="Invalid API key")
            _bind_mcp_username(request, keyed.get("username") or "", method="api_key")
            return str(keyed.get("username") or "")

    pin = extract_mcp_pin(request)
    if pin:
        if verify_mcp_pin(pin):
            admin = _primary_admin_username()
            if not admin:
                raise HTTPException(
                    status_code=503,
                    detail="MCP PIN is valid but no admin account exists to bind.",
                )
            _bind_mcp_username(request, admin, method="pin")
            return admin
        raise HTTPException(status_code=401, detail="Invalid MCP PIN")

    # Direct-to-Studio: Authorization Basic matches Settings -> Ngrok credentials.
    # Via ngrok: edge already checked Basic and stamped X-BubblePod-Tunnel-Auth.
    basic_ok = verify_ngrok_basic_auth(request)
    tunnel_ok = verify_ngrok_tunnel_gate(request)
    if basic_ok or tunnel_ok:
        admin = _primary_admin_username()
        if not admin:
            raise HTTPException(
                status_code=503,
                detail="Basic/tunnel auth matched but no admin account exists to bind.",
            )
        _bind_mcp_username(request, admin, method="basic" if basic_ok else "tunnel_gate")
        return admin

    www = 'Bearer realm="Stickman Automation MCP"'
    try:
        from studio.settings import load_settings

        if str(load_settings().get("ngrok_basic_auth_password") or "").strip():
            www = 'Basic realm="Stickman Automation Studio", Bearer realm="Stickman Automation MCP"'
    except Exception:
        pass

    if mcp_pin_configured():
        raise HTTPException(
            status_code=401,
            detail=(
                "MCP requires a Studio JWT, MCP PIN (?mcp_pin=... / X-MCP-Pin), "
                "Bearer bp_live_… API key, or ngrok HTTP basic-auth (Settings -> Ngrok). "
                "Through the tunnel, Basic is checked by ngrok then stripped; "
                "restart ngrok after updating Studio so the tunnel gate header is stamped."
            ),
            headers={"WWW-Authenticate": www},
        )
    raise HTTPException(
        status_code=401,
        detail=(
            "MCP requires a Studio login token (Authorization: Bearer <jwt>). "
            "Set an MCP PIN or ngrok basic-auth for ChatGPT URL connectors."
        ),
        headers={"WWW-Authenticate": www},
    )


def generate_mcp_pin() -> str:
    """Strong URL-safe PIN for ChatGPT / remote MCP connectors."""
    return secrets.token_urlsafe(12)


def ensure_mcp_pin() -> dict[str, Any]:
    """Ensure settings.mcp_pin_hash exists. Returns plaintext only when newly generated."""
    from studio.settings import load_settings, save_settings

    if (load_settings().get("mcp_pin_hash") or "").strip():
        return {"mcp_pin_set": True, "generated": False, "pin": None}
    pin = generate_mcp_pin()
    save_settings({"mcp_pin_hash": hash_password(pin)})
    return {"mcp_pin_set": True, "generated": True, "pin": pin}


def set_mcp_pin(pin: str) -> dict[str, Any]:
    from studio.settings import is_placeholder_secret, save_settings

    raw = (pin or "").strip()
    if is_placeholder_secret(raw):
        raise ValueError("MCP PIN cannot be empty or a redaction placeholder.")
    if len(raw) < 6:
        raise ValueError("MCP PIN must be at least 6 characters.")
    save_settings({"mcp_pin_hash": hash_password(raw)})
    return {"mcp_pin_set": True}


def is_mcp_download_path(path: str) -> bool:
    """Short-lived MCP artifact download URLs minted by get_file (token = auth)."""
    return path.startswith("/api/mcp/download/")


def is_public_path(path: str) -> bool:
    if path == "/" or path in PUBLIC_API_PATHS:
        return True
    if path in ("/admin", "/pricing", "/billing/success", "/billing/cancel"):
        return True
    if path.startswith("/billing/") or path.startswith("/pricing"):
        return True
    if is_mcp_download_path(path):
        return True
    if path.startswith("/static/") or path == "/static":
        return True
    return False


def is_docs_path(path: str) -> bool:
    if path in DOCS_PATHS:
        return True
    if path.startswith("/docs/") or path.startswith("/redoc/"):
        return True
    return False


def is_mcp_path(path: str) -> bool:
    return path == "/mcp" or path.startswith("/mcp/")


def path_requires_auth(path: str) -> bool:
    """Standard JWT gate for /api/* (except public) and docs/OpenAPI."""
    if is_desktop_mode():
        return False
    if is_public_path(path):
        return False
    if is_docs_path(path):
        return True
    return path.startswith("/api/")


def _cli_set_password(username: str, new_password: str) -> None:
    """Admin recovery: set password and bump token_version (no current-password check)."""
    from studio.members import ensure_members_store, get_user_by_username, update_user

    ensure_members_store()
    name = (username or "").strip()
    user = get_user_by_username(name)
    if not user:
        raise SystemExit(f"Unknown user: {name}")
    pw = (new_password or "").strip()
    if len(pw) < 6:
        raise SystemExit("Password must be at least 6 characters")
    update_user(user["id"], password=pw, must_change_password=False)
    # Keep legacy auth.json hash in sync for the primary admin username.
    cfg = get_auth_config()
    if (cfg.get("username") or "").strip().lower() == name.lower():
        cfg["password_hash"] = hash_password(pw)
        cfg["password_changed_at"] = datetime.now(timezone.utc).isoformat()
        _write_auth_file(cfg)
    fresh = get_user_by_username(name) or user
    print(
        f"Password updated for {fresh.get('username')} "
        f"(token_version={fresh.get('token_version')}). Old sessions are invalid."
    )


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if len(argv) >= 3 and argv[0] in ("set-password", "set_password"):
        _cli_set_password(argv[1], argv[2])
    else:
        print(
            "Usage:\n"
            "  python -m studio.auth set-password <username> <new-password>\n"
            "\n"
            "Sets the members-store password, bumps token_version (invalidates JWTs),\n"
            "and syncs user_data/auth.json when username matches the legacy admin."
        )
        raise SystemExit(2)
