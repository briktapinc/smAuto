"""YouTube OAuth (system browser + loopback) and Data API uploads."""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import threading
import time
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

from studio.aspect import ALL_ASPECTS, normalize_aspect
from studio.paths import YOUTUBE_ACCOUNTS_PATH, YOUTUBE_TOKEN_PATH, ensure_dirs
from studio.projects import (
    collect_renders,
    cover_path,
    final_video_path,
    frames_dir,
    hashtags_to_text,
    input_prefix,
    last_video_path,
    load_meta,
    normalize_youtube_hashtags,
    normalize_youtube_keywords,
    project_dir,
    save_meta,
    shorts_input_prefix,
)
from studio.settings import (
    YOUTUBE_PRIVACY,
    load_settings,
    normalize_bool,
    normalize_youtube_privacy,
    save_settings,
)
from studio.thumbs import THUMB_NAME

_log = logging.getLogger("studio.youtube")

SCOPES = (
    # force-ssl covers upload + videos.update (title rename). Reconnect after scope changes.
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)

_lock = threading.Lock()
_pending: dict[str, Any] | None = None
_PENDING_TTL = 15 * 60


_pending_creds: dict[str, Any] | None = None
_pending_channels: list[dict[str, Any]] = []


def _cred_payload_from_creds(creds: Any) -> dict[str, Any]:
    cid, secret = client_id_secret()
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": getattr(creds, "token_uri", None) or "https://oauth2.googleapis.com/token",
        "client_id": cid or getattr(creds, "client_id", None),
        "client_secret": secret or getattr(creds, "client_secret", None),
        "scopes": list(creds.scopes or SCOPES),
        "expiry": creds.expiry.isoformat() if getattr(creds, "expiry", None) else None,
    }


def _public_account(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry.get("id") or entry.get("channel_id") or "",
        "channel_id": entry.get("channel_id") or entry.get("id") or "",
        "title": entry.get("channel_title") or entry.get("title") or "",
        "channel_title": entry.get("channel_title") or entry.get("title") or "",
        "custom_url": entry.get("custom_url") or "",
        "thumbnail": entry.get("thumbnail") or "",
        "is_default": bool(entry.get("is_default")),
        "connected_at": entry.get("connected_at") or "",
    }


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "default_id": "", "accounts": []}


def _migrate_legacy_token_locked() -> dict[str, Any]:
    """One-time: single youtube_token.json + settings channel → accounts store."""
    store = _empty_store()
    if not YOUTUBE_TOKEN_PATH.is_file():
        return store
    try:
        creds = json.loads(YOUTUBE_TOKEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return store
    if not isinstance(creds, dict) or not (creds.get("refresh_token") or creds.get("token")):
        return store
    settings = load_settings()
    channel_id = (settings.get("youtube_channel_id") or "").strip()
    channel_title = (settings.get("youtube_channel_title") or "").strip()
    account_id = channel_id or "legacy"
    entry = {
        "id": account_id,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "custom_url": "",
        "thumbnail": "",
        "is_default": True,
        "credentials": creds,
        "connected_at": _now(),
    }
    store["accounts"] = [entry]
    store["default_id"] = account_id
    YOUTUBE_ACCOUNTS_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")
    return store


def _load_accounts_store() -> dict[str, Any]:
    ensure_dirs()
    if YOUTUBE_ACCOUNTS_PATH.is_file():
        try:
            data = json.loads(YOUTUBE_ACCOUNTS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return data
    return _migrate_legacy_token_locked()


def _sync_default_settings(store: dict[str, Any]) -> None:
    default_id = (store.get("default_id") or "").strip()
    match = None
    for item in store.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        cid = (item.get("channel_id") or item.get("id") or "").strip()
        if default_id and cid == default_id:
            match = item
            break
        if item.get("is_default") and match is None:
            match = item
    if match is None and (store.get("accounts") or []):
        match = next((a for a in store["accounts"] if isinstance(a, dict)), None)
    if match is None:
        save_settings({"youtube_channel_id": "", "youtube_channel_title": ""})
        return
    save_settings(
        {
            "youtube_channel_id": (match.get("channel_id") or match.get("id") or "").strip(),
            "youtube_channel_title": (match.get("channel_title") or "").strip(),
        }
    )


def _mirror_default_token(store: dict[str, Any]) -> None:
    """Keep legacy youtube_token.json as a copy of the default account credentials."""
    default_id = (store.get("default_id") or "").strip()
    entry = None
    for item in store.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        cid = (item.get("channel_id") or item.get("id") or "").strip()
        if default_id and cid == default_id:
            entry = item
            break
        if item.get("is_default") and entry is None:
            entry = item
    if entry is None and (store.get("accounts") or []):
        entry = next((a for a in store["accounts"] if isinstance(a, dict)), None)
    creds = (entry or {}).get("credentials") if entry else None
    if isinstance(creds, dict) and (creds.get("refresh_token") or creds.get("token")):
        YOUTUBE_TOKEN_PATH.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    elif YOUTUBE_TOKEN_PATH.is_file():
        try:
            YOUTUBE_TOKEN_PATH.unlink()
        except OSError:
            pass


def _save_accounts_store(store: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    accounts: list[dict[str, Any]] = []
    default_id = (store.get("default_id") or "").strip()
    raw_accounts = [a for a in (store.get("accounts") or []) if isinstance(a, dict)]
    if not default_id and raw_accounts:
        default_id = (raw_accounts[0].get("channel_id") or raw_accounts[0].get("id") or "").strip()
    if default_id and not any(
        (a.get("channel_id") or a.get("id") or "").strip() == default_id for a in raw_accounts
    ):
        default_id = (raw_accounts[0].get("channel_id") or raw_accounts[0].get("id") or "").strip() if raw_accounts else ""
    for item in raw_accounts:
        cid = (item.get("channel_id") or item.get("id") or "").strip()
        entry = dict(item)
        entry["id"] = cid or entry.get("id") or secrets.token_hex(8)
        entry["channel_id"] = cid
        entry["is_default"] = bool(cid and cid == default_id)
        accounts.append(entry)
    if accounts and not any(a.get("is_default") for a in accounts):
        accounts[0]["is_default"] = True
        default_id = (accounts[0].get("channel_id") or accounts[0].get("id") or "").strip()
    out = {"version": 1, "default_id": default_id, "accounts": accounts}
    YOUTUBE_ACCOUNTS_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    _mirror_default_token(out)
    try:
        _sync_default_settings(out)
    except Exception:
        pass
    return out


def connected_accounts() -> list[dict[str, Any]]:
    store = _load_accounts_store()
    return [_public_account(a) for a in (store.get("accounts") or []) if isinstance(a, dict)]


def _find_account_entry(channel_id: str = "", *, prefer_default: bool = True) -> dict[str, Any] | None:
    store = _load_accounts_store()
    accounts = [a for a in (store.get("accounts") or []) if isinstance(a, dict)]
    if not accounts:
        return None
    wanted = (channel_id or "").strip()
    if wanted:
        for item in accounts:
            cid = (item.get("channel_id") or item.get("id") or "").strip()
            if cid == wanted:
                return item
        return None
    if not prefer_default:
        return None
    default_id = (store.get("default_id") or "").strip()
    if default_id:
        for item in accounts:
            cid = (item.get("channel_id") or item.get("id") or "").strip()
            if cid == default_id:
                return item
    for item in accounts:
        if item.get("is_default"):
            return item
    return accounts[0]


def _upsert_account(
    *,
    channel_id: str,
    channel_title: str = "",
    credentials: dict[str, Any],
    custom_url: str = "",
    thumbnail: str = "",
    make_default: bool | None = None,
) -> dict[str, Any]:
    store = _load_accounts_store()
    accounts = [a for a in (store.get("accounts") or []) if isinstance(a, dict)]
    cid = (channel_id or "").strip()
    if not cid:
        raise RuntimeError("channel_id is required to save a YouTube account.")
    if not isinstance(credentials, dict) or not (credentials.get("refresh_token") or credentials.get("token")):
        raise RuntimeError("Missing YouTube credentials for this channel.")
    existing = None
    for item in accounts:
        if (item.get("channel_id") or item.get("id") or "").strip() == cid:
            existing = item
            break
    entry = {
        "id": cid,
        "channel_id": cid,
        "channel_title": (channel_title or "").strip() or ((existing or {}).get("channel_title") or ""),
        "custom_url": custom_url or ((existing or {}).get("custom_url") or ""),
        "thumbnail": thumbnail or ((existing or {}).get("thumbnail") or ""),
        "is_default": False,
        "credentials": credentials,
        "connected_at": _now(),
    }
    if existing is None:
        accounts.append(entry)
    else:
        accounts = [
            entry if (a.get("channel_id") or a.get("id") or "").strip() == cid else a
            for a in accounts
        ]
    become_default = make_default
    if become_default is None:
        become_default = not any(
            (a.get("channel_id") or a.get("id") or "").strip() != cid and a.get("is_default")
            for a in accounts
        ) or len(accounts) == 1
    default_id = cid if become_default else (store.get("default_id") or "").strip()
    if become_default:
        default_id = cid
    elif not default_id:
        default_id = cid
    return _save_accounts_store({"version": 1, "default_id": default_id, "accounts": accounts})



_CALLBACK_OK_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>YouTube connected</title>
<style>
body{font-family:"Segoe UI",Trebuchet MS,sans-serif;background:#f6efe3;color:#1c1712;
display:grid;place-items:center;min-height:100vh;margin:0}
.card{background:#fffaf2;border:1.5px solid #1c1712;border-radius:18px;padding:28px 32px;
max-width:28rem;box-shadow:6px 8px 0 #ead9c2}
h1{margin:0 0 8px;font-size:1.4rem}p{margin:0;color:#6e6256;line-height:1.45}
</style></head><body><div class="card">
<h1>YouTube connected</h1>
<p>You can close this tab and return to Stickman Automation. Pick a channel in Settings if you have more than one.</p>
</div></body></html>
"""

_CALLBACK_ERR_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>YouTube connect failed</title>
<style>
body{font-family:"Segoe UI",Trebuchet MS,sans-serif;background:#f6efe3;color:#1c1712;
display:grid;place-items:center;min-height:100vh;margin:0}
.card{background:#fffaf2;border:1.5px solid #1c1712;border-radius:18px;padding:28px 32px;
max-width:32rem;box-shadow:6px 8px 0 #ead9c2}
h1{margin:0 0 8px;font-size:1.4rem;color:#d45a4a}p{margin:0;color:#6e6256;line-height:1.45}
code{background:#efe4d4;padding:.1em .3em;border-radius:4px}
</style></head><body><div class="card">
<h1>Could not connect YouTube</h1>
<p>__MSG__</p>
<p style="margin-top:12px">Close this tab, then click Connect YouTube in Studio Settings and try again.</p>
</div></body></html>
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_google():
    try:
        from google.auth.transport.requests import Request  # noqa: F401
        from google.oauth2.credentials import Credentials  # noqa: F401
        from google_auth_oauthlib.flow import Flow  # noqa: F401
        from googleapiclient.discovery import build  # noqa: F401
        from googleapiclient.http import MediaFileUpload  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "YouTube libraries are missing. Install google-api-python-client and "
            "google-auth-oauthlib (pip install -r requirements.txt)."
        ) from exc


def studio_base_url() -> str:
    """Public base URL for OAuth redirects and absolute links (env/settings or local host:port)."""
    from studio.settings import resolve_public_base_url

    return resolve_public_base_url()


def oauth_redirect_uri() -> str:
    return f"{studio_base_url()}/api/youtube/oauth/callback"


def _is_loopback_http_uri(uri: str) -> bool:
    """True for http://127.0.0.1|localhost|::1 (installed-app / Studio loopback)."""
    raw = (uri or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if (parsed.scheme or "").lower() != "http":
        return False
    host = (parsed.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


@contextmanager
def _oauth_insecure_transport_if_loopback(*uris: str) -> Iterator[None]:
    """Allow oauthlib token exchange over local HTTP only; leave https (e.g. ngrok) alone."""
    if not any(_is_loopback_http_uri(u) for u in uris):
        yield
        return
    key = "OAUTHLIB_INSECURE_TRANSPORT"
    previous = os.environ.get(key)
    os.environ[key] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def connect_page_url() -> str:
    return f"{studio_base_url()}/api/youtube/connect"


def client_id_secret() -> tuple[str, str]:
    settings = load_settings()
    cid = (settings.get("youtube_client_id") or "").strip()
    secret = (settings.get("youtube_client_secret") or "").strip()
    return cid, secret


def has_client() -> bool:
    cid, secret = client_id_secret()
    return bool(cid and secret)


def is_connected() -> bool:
    store = _load_accounts_store()
    for item in store.get("accounts") or []:
        if not isinstance(item, dict):
            continue
        creds = item.get("credentials") or {}
        if isinstance(creds, dict) and (creds.get("refresh_token") or creds.get("token")):
            return True
    # Legacy single-token file (pre-migration callers / race before first load).
    if YOUTUBE_TOKEN_PATH.is_file():
        try:
            data = json.loads(YOUTUBE_TOKEN_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        return bool(data.get("refresh_token") or data.get("token"))
    return False


def _client_config(redirect_uri: str) -> dict[str, Any]:
    cid, secret = client_id_secret()
    if not cid or not secret:
        raise RuntimeError(
            "Set YouTube client ID and secret in Settings (Google Cloud OAuth Desktop "
            "or Web client) or env YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET."
        )
    body = {
        "client_id": cid,
        "client_secret": secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [redirect_uri, "http://127.0.0.1", "http://localhost"],
    }
    return {"installed": body}


def _save_credentials(creds: Any, *, channel_id: str = "") -> None:
    """Persist credentials onto the matching account (or default / legacy mirror)."""
    payload = _cred_payload_from_creds(creds)
    store = _load_accounts_store()
    accounts = [a for a in (store.get("accounts") or []) if isinstance(a, dict)]
    wanted = (channel_id or "").strip()
    target = None
    if wanted:
        for item in accounts:
            if (item.get("channel_id") or item.get("id") or "").strip() == wanted:
                target = item
                break
    if target is None:
        target = _find_account_entry(wanted, prefer_default=True)
    if target is not None:
        cid = (target.get("channel_id") or target.get("id") or "").strip()
        _upsert_account(
            channel_id=cid or wanted or "legacy",
            channel_title=target.get("channel_title") or "",
            credentials=payload,
            custom_url=target.get("custom_url") or "",
            thumbnail=target.get("thumbnail") or "",
            make_default=bool(target.get("is_default")),
        )
        return
    # No accounts yet — keep legacy file so migration/connect can pick it up.
    ensure_dirs()
    YOUTUBE_TOKEN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _credentials_from_info(info: dict[str, Any], *, channel_id: str = "") -> Any:
    _require_google()
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    data = dict(info)
    cid, secret = client_id_secret()
    if cid:
        data["client_id"] = cid
    if secret:
        data["client_secret"] = secret
    creds = Credentials.from_authorized_user_info(data, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_credentials(creds, channel_id=channel_id)
    return creds


def _load_credentials(channel_id: str = "") -> Any:
    entry = _find_account_entry(channel_id, prefer_default=True)
    if entry and isinstance(entry.get("credentials"), dict):
        cid = (entry.get("channel_id") or entry.get("id") or "").strip()
        return _credentials_from_info(entry["credentials"], channel_id=cid)
    if YOUTUBE_TOKEN_PATH.is_file():
        try:
            info = json.loads(YOUTUBE_TOKEN_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if isinstance(info, dict) and (info.get("refresh_token") or info.get("token")):
            return _credentials_from_info(info, channel_id=channel_id)
    return None


def _youtube_service(creds: Any | None = None, *, channel_id: str = ""):
    _require_google()
    from googleapiclient.discovery import build

    creds = creds or _load_credentials(channel_id)
    if creds is None:
        raise RuntimeError(
            "YouTube is not connected. Open the Studio Connect URL in your system browser "
            f"({connect_page_url()}) — MCP cannot show a popup."
        )
    if not creds.valid:
        raise RuntimeError("YouTube token is invalid. Click Connect YouTube in Settings and sign in again.")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _format_http_error(exc: Exception) -> str:
    content = getattr(exc, "content", None)
    text = ""
    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="replace")
    elif content:
        text = str(content)
    reason = ""
    message = str(exc)
    try:
        data = json.loads(text) if text else {}
        err = data.get("error") or {}
        message = err.get("message") or message
        errors = err.get("errors") or []
        if errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
    except Exception:
        pass
    blob = f"{reason} {message} {text}".lower()
    if "quota" in blob or reason in {"quotaExceeded", "dailyLimitExceeded"}:
        return (
            "YouTube API quota exceeded. Uploads are blocked until the quota resets "
            "(usually midnight Pacific) or you request more quota in Google Cloud."
        )
    if "youtubesignuprequired" in blob or "youtubeSignupRequired" in reason:
        return "This Google account has no YouTube channel. Create a channel on YouTube, then reconnect."
    if "forbidden" in blob or "accessnotconfigured" in blob:
        return (
            "YouTube Data API is not enabled or this OAuth client is not allowed. "
            "Enable YouTube Data API v3 in Google Cloud and use a Desktop (or Web) OAuth client."
        )
    return message or str(exc)


def _error_html(message: str) -> str:
    safe = (
        (message or "Unknown error")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return _CALLBACK_ERR_HTML.replace("__MSG__", safe)


def start_connect(*, open_browser: bool = True) -> dict[str, Any]:
    """Build a Google OAuth URL and optionally open the system/default browser."""
    _require_google()
    from google_auth_oauthlib.flow import Flow

    redirect_uri = oauth_redirect_uri()
    flow = Flow.from_client_config(_client_config(redirect_uri), scopes=list(SCOPES), redirect_uri=redirect_uri)
    flow.code_verifier = secrets.token_urlsafe(64)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
    )
    with _lock:
        global _pending
        _pending = {
            "flow": flow,
            "state": state,
            "redirect_uri": redirect_uri,
            "created_at": time.time(),
        }
    opened = False
    if open_browser:
        try:
            opened = bool(webbrowser.open(auth_url, new=2, autoraise=True))
        except Exception:
            opened = False
    return {
        "ok": True,
        "connected": False,
        "auth_url": auth_url,
        "studio_connect_url": connect_page_url(),
        "redirect_uri": redirect_uri,
        "browser_opened": opened,
        "message": (
            "Open this URL in your system browser (Chrome, Edge, …) — not an in-app popup. "
            "Sign in with Google, pick the YouTube channel if asked, then Studio receives "
            f"the callback on {redirect_uri}. If the browser did not open, copy auth_url "
            "or open studio_connect_url."
        ),
    }


def _pending_flow():
    global _pending
    with _lock:
        pending = _pending
    if not pending:
        return None
    if time.time() - float(pending.get("created_at") or 0) > _PENDING_TTL:
        with _lock:
            if _pending is pending:
                _pending = None
        return None
    return pending


def _channels_for_creds(creds: Any) -> list[dict[str, Any]]:
    youtube = _youtube_service(creds)
    resp = youtube.channels().list(part="id,snippet", mine=True, maxResults=50).execute()
    channels = []
    for item in resp.get("items") or []:
        snippet = item.get("snippet") or {}
        thumbs = snippet.get("thumbnails") or {}
        thumb = (thumbs.get("default") or thumbs.get("medium") or {}).get("url") or ""
        channels.append(
            {
                "id": item.get("id") or "",
                "title": snippet.get("title") or "",
                "custom_url": snippet.get("customUrl") or "",
                "thumbnail": thumb,
            }
        )
    return channels


def finish_oauth(*, code: str = "", state: str = "", authorization_response: str = "") -> dict[str, Any]:
    pending = _pending_flow()
    if not pending:
        raise RuntimeError(
            "No YouTube sign-in is in progress (or it expired). Click Connect YouTube "
            "in Settings or open the Studio connect URL in your browser again."
        )
    flow = pending["flow"]
    expected = pending.get("state") or ""
    if state and expected and state != expected:
        raise RuntimeError("OAuth state mismatch. Click Connect YouTube and try again.")
    redirect_uri = (
        (pending.get("redirect_uri") or "").strip()
        or (getattr(flow, "redirect_uri", None) or "")
        or oauth_redirect_uri()
    )
    try:
        # Local Studio uses http://127.0.0.1 loopback; oauthlib requires this flag for HTTP.
        # HTTPS redirect URIs (e.g. ngrok) do not enable insecure transport.
        with _oauth_insecure_transport_if_loopback(redirect_uri, authorization_response):
            if authorization_response:
                flow.fetch_token(authorization_response=authorization_response)
            else:
                if not (code or "").strip():
                    raise RuntimeError("Missing OAuth code. Paste the redirect URL or the code from Google.")
                flow.fetch_token(code=code.strip())
    except Exception as exc:
        raise RuntimeError(f"Could not exchange the YouTube code: {exc}") from exc
    creds = flow.credentials
    if not creds or not creds.refresh_token:
        # Still store; user may need to reconnect with prompt=consent.
        pass
    payload = _cred_payload_from_creds(creds)
    with _lock:
        global _pending, _pending_creds, _pending_channels
        _pending = None
        _pending_creds = payload
        _pending_channels = []
    try:
        items = _channels_for_creds(creds)
    except Exception as exc:
        # Keep pending creds so the user can retry channel pick / reconnect.
        with _lock:
            _pending_channels = []
        raise RuntimeError(f"Signed in, but could not list YouTube channels: {exc}") from exc
    with _lock:
        _pending_channels = list(items)
    existing = connected_accounts()
    make_default = len(existing) == 0
    if not items:
        with _lock:
            _pending_creds = None
            _pending_channels = []
        raise RuntimeError(
            "This Google account has no YouTube channel. Create a channel on YouTube, then reconnect."
        )
    connected_ids = {(a.get("channel_id") or a.get("id") or "") for a in existing}
    fresh = [c for c in items if (c.get("id") or "") and c.get("id") not in connected_ids]
    # Auto-bind when Google returns a single channel, or exactly one new channel on this account.
    auto = None
    if len(items) == 1:
        auto = items[0]
    elif len(fresh) == 1:
        auto = fresh[0]
    if auto and auto.get("id"):
        _upsert_account(
            channel_id=auto["id"],
            channel_title=auto.get("title") or "",
            credentials=payload,
            custom_url=auto.get("custom_url") or "",
            thumbnail=auto.get("thumbnail") or "",
            make_default=True if make_default else False,
        )
        with _lock:
            _pending_creds = None
            _pending_channels = []
    # else: leave _pending_creds so set_channel can finish adding from pending_channels
    return status()


def finish_oauth_from_request(url: str, code: str = "", state: str = "", error: str = "") -> tuple[dict[str, Any] | None, str, bool]:
    """Handle the loopback callback. Returns (payload, html, ok)."""
    if error:
        msg = f"Google returned: {error}"
        return None, _error_html(msg), False
    try:
        payload = finish_oauth(code=code, state=state, authorization_response=url if "code=" in url else "")
        return payload, _CALLBACK_OK_HTML, True
    except Exception as exc:
        return None, _error_html(str(exc)), False


def finish_oauth_paste(code_or_url: str) -> dict[str, Any]:
    raw = (code_or_url or "").strip()
    if not raw:
        raise RuntimeError("Paste the Google redirect URL or the code parameter.")
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        qs = parse_qs(parsed.query)
        code = (qs.get("code") or [""])[0]
        state = (qs.get("state") or [""])[0]
        err = (qs.get("error") or [""])[0]
        if err:
            raise RuntimeError(f"Google returned: {err}")
        return finish_oauth(code=code, state=state, authorization_response=raw)
    return finish_oauth(code=raw)


def disconnect(channel_id: str = "") -> dict[str, Any]:
    """Remove one connected channel, or all when channel_id is empty."""
    wanted = (channel_id or "").strip()
    store = _load_accounts_store()
    accounts = [a for a in (store.get("accounts") or []) if isinstance(a, dict)]
    if wanted:
        accounts = [
            a for a in accounts
            if (a.get("channel_id") or a.get("id") or "").strip() != wanted
        ]
        default_id = (store.get("default_id") or "").strip()
        if default_id == wanted:
            default_id = (accounts[0].get("channel_id") or accounts[0].get("id") or "").strip() if accounts else ""
        _save_accounts_store({"version": 1, "default_id": default_id, "accounts": accounts})
    else:
        _save_accounts_store(_empty_store())
        if YOUTUBE_TOKEN_PATH.is_file():
            try:
                YOUTUBE_TOKEN_PATH.unlink()
            except OSError:
                pass
        if YOUTUBE_ACCOUNTS_PATH.is_file():
            try:
                # rewritten empty by _save_accounts_store already
                pass
            except OSError:
                pass
    with _lock:
        global _pending, _pending_creds, _pending_channels
        if not wanted:
            _pending = None
            _pending_creds = None
            _pending_channels = []
    return status()


def set_channel(channel_id: str, title: str = "") -> dict[str, Any]:
    """Set the default upload channel, or finish adding a pending OAuth channel."""
    global _pending_creds, _pending_channels
    cid = (channel_id or "").strip()
    label = (title or "").strip()
    with _lock:
        pending_creds = _pending_creds
        pending_channels = list(_pending_channels or [])
    if pending_creds and cid:
        match = next((c for c in pending_channels if c.get("id") == cid), None)
        if match is None and pending_channels:
            raise RuntimeError(
                "That channel is not on the Google account you just signed in with. "
                "Pick one from the list, or Connect again."
            )
        if not label and match:
            label = match.get("title") or ""
        existing = connected_accounts()
        _upsert_account(
            channel_id=cid,
            channel_title=label,
            credentials=pending_creds,
            custom_url=(match or {}).get("custom_url") or "",
            thumbnail=(match or {}).get("thumbnail") or "",
            make_default=len(existing) == 0,
        )
        with _lock:
            _pending_creds = None
            _pending_channels = []
        return status()
    if not cid:
        raise RuntimeError("Select a YouTube channel id.")
    store = _load_accounts_store()
    accounts = [a for a in (store.get("accounts") or []) if isinstance(a, dict)]
    match_entry = None
    for item in accounts:
        if (item.get("channel_id") or item.get("id") or "").strip() == cid:
            match_entry = item
            break
    if match_entry is None:
        raise RuntimeError(
            "That YouTube channel is not connected. Click Add YouTube channel / Connect "
            "and sign in with the Google account for that channel."
        )
    if label:
        match_entry = dict(match_entry)
        match_entry["channel_title"] = label
        accounts = [
            match_entry if (a.get("channel_id") or a.get("id") or "").strip() == cid else a
            for a in accounts
        ]
    _save_accounts_store({"version": 1, "default_id": cid, "accounts": accounts})
    return status()


def list_channels(*, force: bool = False) -> dict[str, Any]:
    """Return connected channels (multi-account store). force kept for API compat."""
    del force  # connected store does not need a live Google round-trip
    accounts = connected_accounts()
    with _lock:
        pending_channels = list(_pending_channels or [])
        has_pending = _pending_creds is not None
    channels = [
        {
            "id": a.get("channel_id") or a.get("id") or "",
            "title": a.get("title") or a.get("channel_title") or "",
            "custom_url": a.get("custom_url") or "",
            "thumbnail": a.get("thumbnail") or "",
            "is_default": bool(a.get("is_default")),
        }
        for a in accounts
    ]
    return {
        "ok": True,
        "connected": bool(accounts) or has_pending,
        "channels": channels,
        "accounts": accounts,
        "pending_channel_pick": bool(has_pending and pending_channels),
        "pending_channels": pending_channels if has_pending else [],
    }


def status() -> dict[str, Any]:
    settings = load_settings()
    # Ensure legacy token migrates before we report connection state.
    store = _load_accounts_store()
    accounts = connected_accounts()
    connected = bool(accounts)
    pending = _pending_flow() is not None
    with _lock:
        pending_pick = _pending_creds is not None and bool(_pending_channels)
        pending_channels = list(_pending_channels or []) if pending_pick else []
    error = ""
    default = _find_account_entry("", prefer_default=True)
    channel_id = ((default or {}).get("channel_id") or (default or {}).get("id") or "").strip()
    channel_title = ((default or {}).get("channel_title") or "").strip()
    if not channel_id:
        channel_id = (settings.get("youtube_channel_id") or "").strip()
        channel_title = channel_title or (settings.get("youtube_channel_title") or "").strip()
    channels = [
        {
            "id": a.get("channel_id") or a.get("id") or "",
            "title": a.get("title") or a.get("channel_title") or "",
            "custom_url": a.get("custom_url") or "",
            "thumbnail": a.get("thumbnail") or "",
            "is_default": bool(a.get("is_default")),
        }
        for a in accounts
    ]
    if pending_pick:
        # Offer channels from the in-progress Google sign-in so the user can finish add.
        for item in pending_channels:
            if not any(c.get("id") == item.get("id") for c in channels):
                channels.append(
                    {
                        "id": item.get("id") or "",
                        "title": item.get("title") or "",
                        "custom_url": item.get("custom_url") or "",
                        "thumbnail": item.get("thumbnail") or "",
                        "is_default": False,
                        "pending": True,
                    }
                )
    msg = "YouTube connected."
    if not connected and pending_pick:
        msg = "Signed in — pick which channel to add, then it becomes available for uploads."
    elif not connected:
        msg = (
            "Open studio_connect_url in your system browser to sign in. "
            "Do not use an in-app popup."
        )
    elif pending_pick:
        msg = "Add another channel: pick it from the list to finish connecting."
    return {
        "ok": True,
        "connected": connected,
        "has_client": has_client(),
        "pending": pending,
        "pending_channel_pick": pending_pick,
        "channel_id": channel_id,
        "channel_title": channel_title,
        "default_channel_id": channel_id,
        "channels": channels,
        "accounts": accounts,
        "auto_upload": bool(settings.get("youtube_auto_upload")),
        "delete_file_after_upload": normalize_bool(
            settings.get("youtube_delete_file_after_upload"), True
        ),
        "privacy": normalize_youtube_privacy(settings.get("youtube_privacy")),
        "privacy_options": list(YOUTUBE_PRIVACY),
        "studio_connect_url": connect_page_url(),
        "redirect_uri": oauth_redirect_uri(),
        "error": error,
        "token_path": str(YOUTUBE_TOKEN_PATH),
        "accounts_path": str(YOUTUBE_ACCOUNTS_PATH),
        "message": msg,
        "account_count": len(accounts),
    }


def _file_ok(path: Path, min_bytes: int = 1000) -> bool:
    return path.is_file() and path.stat().st_size >= min_bytes


def delete_file_after_upload_enabled() -> bool:
    """Global setting: remove local mp4 + render assets after a successful YouTube upload. Default True."""
    return normalize_bool(load_settings().get("youtube_delete_file_after_upload"), True)


def _maybe_cleanup_after_upload(project_id: str, uploaded_video: Path | None = None) -> dict[str, Any]:
    """Delete local mp4s, frames, schedules, and temp YouTube thumb when setting is on.

    Keeps meta/scripts/covers/wav so the library can still show the YouTube listing.
    """
    if not delete_file_after_upload_enabled():
        return {"deleted": False, "skipped": True, "reason": "delete_after_upload_off"}
    deleted_files: list[str] = []
    deleted_dirs: list[str] = []
    errors: list[str] = []
    prefix = Path(input_prefix(project_id))
    shorts = Path(shorts_input_prefix(project_id))
    file_targets: list[Path] = [
        last_video_path(project_id),
        project_dir(project_id) / "_yt_thumb_upload.jpg",
        prefix.with_name(prefix.name + "_schedule.csv"),
        shorts.with_name(shorts.name + "_schedule.csv"),
    ]
    for asp in ALL_ASPECTS:
        file_targets.append(final_video_path(project_id, asp))
    if uploaded_video is not None:
        file_targets.append(uploaded_video)
    seen_files: set[Path] = set()
    for path in file_targets:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen_files:
            continue
        seen_files.add(key)
        try:
            if path.is_file():
                path.unlink()
                deleted_files.append(path.name)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            _log.warning("Failed to delete local file after YouTube upload (%s): %s", path, exc)
    dir_targets: list[Path] = [
        Path(str(prefix) + "_frames"),
        Path(str(shorts) + "_frames"),
    ]
    for asp in ALL_ASPECTS:
        dir_targets.append(frames_dir(project_id, asp))
    seen_dirs: set[Path] = set()
    for folder in dir_targets:
        try:
            key = folder.resolve()
        except OSError:
            key = folder
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        if not folder.is_dir():
            continue
        try:
            shutil.rmtree(folder, ignore_errors=False)
            deleted_dirs.append(folder.name)
        except OSError as exc:
            errors.append(f"{folder.name}/: {exc}")
            _log.warning("Failed to delete frames after YouTube upload (%s): %s", folder, exc)
    return {
        "deleted": bool(deleted_files or deleted_dirs),
        "skipped": False,
        "files": deleted_files,
        "dirs": deleted_dirs,
        "error": "; ".join(errors) if errors else "",
        "clear_render_meta": True,
    }


def _video_path(project_id: str, aspect: str | None = None) -> Path:
    """Prefer the requested aspect mp4, else last render / script_final.mp4, else any ready aspect."""
    if aspect:
        specific = final_video_path(project_id, normalize_aspect(aspect))
        if _file_ok(specific):
            return specific
    try:
        meta = load_meta(project_id)
    except FileNotFoundError:
        meta = {}
    last = (meta.get("last_render_aspect") or "").strip()
    if last:
        specific = final_video_path(project_id, normalize_aspect(last))
        if _file_ok(specific):
            return specific
    alias = last_video_path(project_id)
    if _file_ok(alias):
        return alias
    renders = collect_renders(project_id)
    for asp in ALL_ASPECTS:
        if renders.get(asp, {}).get("ready"):
            return final_video_path(project_id, asp)
    return alias


def _thumb_candidates(project_id: str, aspect: str | None = None) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for path in (
        cover_path(project_id, normalize_aspect(aspect)) if aspect else None,
        cover_path(project_id),
        project_dir(project_id) / THUMB_NAME,
    ):
        if path is None:
            continue
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        if path.is_file() and path.stat().st_size > 200:
            out.append(path)
    return out


def _resolve_privacy(project_id: str, override: str | None) -> str:
    if override:
        return normalize_youtube_privacy(override)
    meta = load_meta(project_id)
    stored = (meta.get("youtube_privacy") or "").strip()
    if stored:
        return normalize_youtube_privacy(stored)
    return normalize_youtube_privacy(load_settings().get("youtube_privacy"))


def _compose_description(meta: dict[str, Any], override: str | None = None) -> str:
    if override is not None and str(override).strip():
        return str(override).strip()
    desc = (meta.get("youtube_description") or "").strip()
    if not desc:
        desc = (meta.get("summary") or meta.get("topic") or meta.get("title") or "").strip()
    hashtags = normalize_youtube_hashtags(meta.get("youtube_hashtags"))
    if hashtags:
        tag_line = hashtags_to_text(hashtags)
        lower = desc.casefold()
        missing = [h for h in hashtags if h.casefold() not in lower]
        if missing:
            missing_line = " ".join(missing)
            desc = f"{desc.rstrip()}\n\n{missing_line}" if desc else missing_line
        elif tag_line.casefold() not in lower and not desc:
            desc = tag_line
    if len(desc) > 4900:
        desc = desc[:4900].rstrip() + "…"
    return desc


def _resolve_tags(meta: dict[str, Any], override: Any = None) -> list[str]:
    if override is not None and override != "":
        tags = normalize_youtube_keywords(override)
        if tags:
            return tags
    tags = normalize_youtube_keywords(meta.get("youtube_keywords"))
    if tags:
        return tags
    # Soft fallback: hashtag words without # so uploads still get some tags.
    return normalize_youtube_keywords(
        [h.lstrip("#") for h in normalize_youtube_hashtags(meta.get("youtube_hashtags"))]
    )


def job_auto_upload(project_id: str) -> bool:
    meta = load_meta(project_id)
    if "youtube_auto_upload" in meta and meta["youtube_auto_upload"] is not None:
        return bool(meta["youtube_auto_upload"])
    return bool(load_settings().get("youtube_auto_upload"))


def _picked_channel(
    project_id: str,
    *,
    channel_id: str | None = None,
    require_channel: bool = False,
) -> dict[str, str]:
    """Resolve which connected channel to upload to (explicit → project → default)."""
    accounts = connected_accounts()
    if not accounts:
        raise RuntimeError(
            "No YouTube channel connected. Open Connect YouTube in Settings (system browser), "
            f"or {connect_page_url()} — MCP cannot show a popup."
        )
    meta = load_meta(project_id)
    wanted = (
        (channel_id or "").strip()
        or (meta.get("youtube_channel_id") or "").strip()
        or (load_settings().get("youtube_channel_id") or "").strip()
    )
    if require_channel and len(accounts) > 1 and not (channel_id or "").strip() and not (meta.get("youtube_channel_id") or "").strip():
        listing = ", ".join(
            f"{a.get('title') or a.get('channel_title') or a.get('channel_id')} [{a.get('channel_id') or a.get('id')}]"
            + (" (default)" if a.get("is_default") else "")
            for a in accounts
        )
        raise RuntimeError(
            "Multiple YouTube channels are connected. Ask the user which channel/account to "
            f"publish to, then pass channel_id. Connected: {listing}"
        )
    if wanted:
        match = next(
            (
                a
                for a in accounts
                if (a.get("channel_id") or a.get("id") or "") == wanted
            ),
            None,
        )
        if not match:
            raise RuntimeError(
                "Selected YouTube channel is not connected. Add it in Settings "
                "(Add YouTube channel), or pick another channel_id from list_youtube_channels."
            )
        return {
            "id": match.get("channel_id") or match.get("id") or "",
            "title": match.get("title") or match.get("channel_title") or "",
        }
    if len(accounts) == 1:
        only = accounts[0]
        cid = only.get("channel_id") or only.get("id") or ""
        title = only.get("title") or only.get("channel_title") or ""
        store = _load_accounts_store()
        _save_accounts_store(
            {
                "version": 1,
                "default_id": cid,
                "accounts": store.get("accounts") or [],
            }
        )
        return {"id": cid, "title": title}
    default = next((a for a in accounts if a.get("is_default")), accounts[0])
    return {
        "id": default.get("channel_id") or default.get("id") or "",
        "title": default.get("title") or default.get("channel_title") or "",
    }


def _prepare_youtube_thumbnail(src: Path, project_id: str) -> Path:
    """Ensure a JPEG under YouTube's 2MiB cap. Logs final byte size."""
    import logging

    from studio.image_normalize import YT_THUMB_MAX_BYTES, write_jpeg_under

    log = logging.getLogger("studio.youtube")
    dest = project_dir(project_id) / "_yt_thumb_upload.jpg"
    meta = write_jpeg_under(src, dest, max_bytes=YT_THUMB_MAX_BYTES)
    log.info(
        "YouTube thumbnail prepared project=%s src=%s bytes=%s quality=%s size=%sx%s",
        project_id,
        src.name,
        meta.get("bytes"),
        meta.get("quality"),
        meta.get("width"),
        meta.get("height"),
    )
    if int(meta.get("bytes") or 0) > YT_THUMB_MAX_BYTES:
        raise RuntimeError(
            f"Thumbnail still larger than {YT_THUMB_MAX_BYTES} bytes after compress "
            f"({meta.get('bytes')} bytes)."
        )
    return dest


def _set_youtube_thumbnail(youtube, video_id: str, thumb_path: Path, *, progress=None) -> dict:
    """Upload custom thumbnail; retry once after re-compress. Raises on failure."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    import logging

    log = logging.getLogger("studio.youtube")
    last_err = ""
    for attempt in (1, 2):
        try:
            size = thumb_path.stat().st_size if thumb_path.is_file() else 0
            log.info(
                "Uploading YouTube thumbnail video_id=%s attempt=%s bytes=%s path=%s",
                video_id,
                attempt,
                size,
                thumb_path,
            )
            if progress:
                progress(f"Setting YouTube thumbnail… ({size} bytes, try {attempt})")
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(str(thumb_path), mimetype="image/jpeg", resumable=False),
            ).execute()
            return {"ok": True, "bytes": size, "attempt": attempt}
        except Exception as exc:
            if isinstance(exc, HttpError):
                last_err = _format_http_error(exc)
            else:
                last_err = str(exc)
            log.warning("YouTube thumbnail upload failed attempt=%s: %s", attempt, last_err)
            if attempt == 1:
                # Re-compress harder and retry once.
                try:
                    from studio.image_normalize import YT_THUMB_MAX_BYTES, write_jpeg_under

                    tighter = thumb_path.with_name("_yt_thumb_retry.jpg")
                    write_jpeg_under(thumb_path, tighter, max_bytes=max(500_000, YT_THUMB_MAX_BYTES // 2))
                    thumb_path = tighter
                except Exception as prep_exc:
                    last_err = f"{last_err}; recompress failed: {prep_exc}"
                    break
    raise RuntimeError(
        f"YouTube thumbnail upload failed after retry: {last_err}. "
        "Video uploaded, but custom thumbnail was not set."
    )


def upload_project_video(
    project_id: str,
    privacy_status: str | None = None,
    title: str | None = None,
    description: str | None = None,
    tags: Any = None,
    aspect: str | None = None,
    channel_id: str | None = None,
    *,
    require_channel: bool = False,
    progress=None,
) -> dict[str, Any]:
    """Upload a finished mp4 (preferred aspect, else last render / script_final.mp4). Raises on failure.

    channel_id selects which connected YouTube account/channel receives the upload.
    When omitted, uses the job override, then the workspace default channel.
    require_channel=True (MCP) refuses ambiguous multi-channel uploads without an explicit id.
    """
    _require_google()
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    if not is_connected():
        raise RuntimeError(
            "YouTube is not connected. Open the Studio connect URL in your system browser "
            f"({connect_page_url()}) — MCP cannot show a popup."
        )
    wanted_aspect = normalize_aspect(aspect) if aspect else None
    video = _video_path(project_id, wanted_aspect)
    if not _file_ok(video):
        raise RuntimeError(
            "No finished video (script_final.mp4 or aspect mp4). Render first."
        )
    privacy = _resolve_privacy(project_id, privacy_status)
    meta = load_meta(project_id)
    title_text = (title or meta.get("title") or meta.get("topic") or project_id).strip()[:100]
    desc_text = _compose_description(meta, description)
    tag_list = _resolve_tags(meta, tags)
    channel = _picked_channel(
        project_id,
        channel_id=channel_id,
        require_channel=require_channel,
    )
    if progress:
        ch_label = channel.get("title") or channel.get("id") or "YouTube"
        progress(f"Uploading to {ch_label} as {privacy}…")
    try:
        youtube = _youtube_service(channel_id=channel.get("id") or "")
        snippet: dict[str, Any] = {
            "title": title_text,
            "description": desc_text,
            "categoryId": "27",
        }
        if tag_list:
            snippet["tags"] = tag_list
        body = {
            "snippet": snippet,
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=1024 * 1024)
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            status_obj, response = request.next_chunk()
            if status_obj and progress:
                pct = int(status_obj.progress() * 100)
                ch_label = channel.get("title") or channel.get("id") or "YouTube"
                progress(f"Uploading to {ch_label} as {privacy}… {pct}%")
    except Exception as exc:
        from googleapiclient.errors import HttpError as _HttpError

        message = _format_http_error(exc) if isinstance(exc, _HttpError) else str(exc)
        try:
            failed = load_meta(project_id)
            failed["youtube_error"] = message
            save_meta(project_id, failed)
        except Exception:
            pass
        raise RuntimeError(message) from exc
    video_id = (response or {}).get("id") or ""
    url = f"https://youtu.be/{video_id}" if video_id else ""
    thumb_error = ""
    thumb_meta: dict[str, Any] = {}
    thumbs = _thumb_candidates(project_id, wanted_aspect)
    if video_id and thumbs:
        if progress:
            progress("Setting YouTube thumbnail…")
        try:
            prepared = _prepare_youtube_thumbnail(thumbs[0], project_id)
            thumb_meta = _set_youtube_thumbnail(youtube, video_id, prepared, progress=progress)
        except Exception as exc:
            # Surface clearly — never silently skip; do not change privacy / stop uploader.
            thumb_error = str(exc)
            try:
                failed = load_meta(project_id)
                failed["youtube_thumbnail_error"] = thumb_error
                save_meta(project_id, failed)
            except Exception:
                pass
    elif video_id and not thumbs:
        thumb_error = "No cover/thumbnail file found to upload as YouTube custom thumbnail."
    result = {
        "ok": True,
        "project_id": project_id,
        "video_id": video_id,
        "url": url,
        "privacy": privacy,
        "title": title_text,
        "description": desc_text,
        "tags": tag_list,
        "file": video.name,
        "aspect": wanted_aspect or meta.get("last_render_aspect") or "",
        "channel_id": channel.get("id") or "",
        "channel_title": channel.get("title") or "",
        "thumbnail_error": thumb_error,
        "thumbnail": thumb_meta or None,
        "uploaded_at": _now(),
    }
    stored = load_meta(project_id)
    stored["youtube"] = result
    stored["youtube_error"] = None
    stored["youtube_pending"] = False
    # Only free disk after a real YouTube id exists.
    if video_id:
        cleanup = _maybe_cleanup_after_upload(project_id, uploaded_video=video)
        result["local_file_deleted"] = bool(cleanup.get("deleted"))
        if cleanup.get("deleted"):
            result["local_files_deleted"] = cleanup.get("files") or []
            result["local_dirs_deleted"] = cleanup.get("dirs") or []
            if cleanup.get("clear_render_meta"):
                stored["renders"] = {}
                stored["last_render_aspect"] = None
        elif cleanup.get("skipped"):
            result["local_file_deleted"] = False
            result["local_file_delete_skipped"] = cleanup.get("reason") or "skipped"
        if cleanup.get("error"):
            result["local_file_delete_error"] = cleanup.get("error")
        stored["youtube"] = result
    save_meta(project_id, stored)
    return result


def _stored_youtube_video_id(meta: dict[str, Any]) -> str:
    blob = meta.get("youtube")
    if isinstance(blob, dict):
        for key in ("video_id", "id"):
            vid = str(blob.get(key) or "").strip()
            if vid:
                return vid
        url = str(blob.get("url") or "").strip()
        if "youtu.be/" in url:
            return url.rstrip("/").split("youtu.be/")[-1].split("?")[0].strip()
        if "v=" in url:
            return parse_qs(urlparse(url).query).get("v", [""])[0].strip()
    for key in ("youtube_video_id", "youtube_id"):
        vid = str(meta.get(key) or "").strip()
        if vid:
            return vid
    return ""


def update_uploaded_video_title(project_id: str, title: str) -> dict[str, Any]:
    """Update the YouTube listing title for a job that already uploaded. Skips if no video_id."""
    title_text = (title or "").strip()[:100]
    if not title_text:
        raise RuntimeError("title is required.")
    meta = load_meta(project_id)
    video_id = _stored_youtube_video_id(meta)
    if not video_id:
        return {
            "ok": False,
            "skipped": True,
            "reason": "No YouTube video_id on this job yet.",
            "project_id": project_id,
        }
    if not is_connected():
        raise RuntimeError(
            "YouTube is not connected. Open the Studio Connect URL in your system browser "
            f"({connect_page_url()}) — MCP cannot show a popup."
        )
    try:
        youtube = _youtube_service()
        listed = youtube.videos().list(part="snippet", id=video_id).execute()
        items = listed.get("items") or []
        if not items:
            raise RuntimeError(f"YouTube video not found for id={video_id}.")
        snippet = dict(items[0].get("snippet") or {})
        old_title = (snippet.get("title") or "").strip()
        snippet["title"] = title_text
        # videos.update requires categoryId when rewriting snippet.
        if not snippet.get("categoryId"):
            snippet["categoryId"] = "27"
        youtube.videos().update(
            part="snippet",
            body={"id": video_id, "snippet": snippet},
        ).execute()
    except Exception as exc:
        from googleapiclient.errors import HttpError as _HttpError

        message = _format_http_error(exc) if isinstance(exc, _HttpError) else str(exc)
        low = message.lower()
        if "insufficient" in low or "permission" in low or "scope" in low:
            message = (
                f"{message} Reconnect YouTube in Settings so the token includes "
                "youtube.force-ssl (needed to rename uploaded videos)."
            )
        raise RuntimeError(message) from exc

    stored = load_meta(project_id)
    blob = stored.get("youtube")
    if isinstance(blob, dict):
        blob = dict(blob)
        blob["title"] = title_text
        stored["youtube"] = blob
    else:
        stored["youtube_video_id"] = video_id
    save_meta(project_id, stored)
    return {
        "ok": True,
        "skipped": False,
        "project_id": project_id,
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "title": title_text,
        "old_title": old_title,
        "updated_at": _now(),
    }


def maybe_auto_upload(project_id: str, progress=None) -> dict[str, Any]:
    """After a successful render. Never raises; caller should not fail the mp4."""
    if not job_auto_upload(project_id):
        return {"skipped": True, "reason": "auto_upload_off"}
    if not is_connected():
        try:
            meta = load_meta(project_id)
            meta["youtube_pending"] = True
            meta["youtube_error"] = None
            save_meta(project_id, meta)
        except Exception:
            pass
        privacy = _resolve_privacy(project_id, None)
        return {
            "skipped": False,
            "ok": False,
            "pending": True,
            "reason": "not_connected",
            "privacy": privacy,
            "detail": (
                f"Video is ready. YouTube upload pending as {privacy} — "
                "connect YouTube in Settings, then upload."
            ),
        }
    try:
        result = upload_project_video(project_id, progress=progress)
        result["skipped"] = False
        result["detail"] = (
            f"Video is ready. Uploaded to YouTube as {result.get('privacy')} "
            f"({result.get('url') or result.get('video_id')})."
        )
        return result
    except Exception as exc:
        message = str(exc)
        try:
            meta = load_meta(project_id)
            meta["youtube_error"] = message
            save_meta(project_id, meta)
        except Exception:
            pass
        return {
            "skipped": False,
            "ok": False,
            "error": message,
            "detail": f"Video is ready. YouTube upload failed: {message}",
        }
