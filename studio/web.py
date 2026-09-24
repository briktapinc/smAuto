from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from studio import auth as studio_auth

from studio.aspect import DEFAULT_ASPECT, normalize_aspect, normalize_job_aspect
from studio.backgrounds import catalog_payload as backgrounds_catalog, resolve_background
from studio.gentle import ensure_gentle, gentle_status, stop_gentle
from studio.illustrations import (
    cover_history_payload,
    illustration_jobs,
    list_cover_versions,
    refresh_covers,
    regenerate_cover,
    resolve_cover_history_path,
    save_upload,
    set_active_cover,
)
from studio.music import (
    delete_track,
    get_track,
    library_payload,
    save_upload as save_music_upload,
    clear_project_music,
    set_project_music,
    shuffle_project_music,
)
from studio.paths import STUDIO_DIR, asset_warnings, ensure_dirs, ensure_fallback_background
from studio.pipeline import (
    attach_job,
    job_status,
    list_library_items,
    pause_project,
    resume_project,
    start_align_job,
    start_audio_job,
    start_illustrations_job,
    start_project,
    start_regenerate_illustration_job,
    start_render_thread,
    start_script_job,
    stop_project,
)
from studio.projects import (
    apply_youtube_publish_meta,
    cover_path,
    create_project,
    delete_project,
    rename_video,
    final_video_path,
    input_prefix,
    is_listed_project,
    last_video_path,
    load_meta,
    project_payload,
    project_video_layout,
    project_image_provider,
    project_include_bubblehead,
    save_lines,
    set_aspect,
    set_art_style,
    set_character_size,
    set_include_bubblehead,
    set_generate_9x16,
    set_image_provider,
    set_project_background,
    set_project_voice,
    set_project_youtube,
    set_video_layout,
    write_scripts,
)
from studio.prompts import catalog_payload, get_prompt, reset_prompt, reset_prompt_user, save_prompt, save_prompts, set_prompt_user
from studio.thumbs import ensure_thumbnail
from studio.settings import is_placeholder_secret, load_settings, public_settings, save_settings, scrub_secret_updates, settings_for_user
from studio.topics import (
    SCHEDULER_INTERVAL_SEC,
    create_topic,
    delete_topic,
    generate_topics,
    list_topics,
    schedule_topic,
    scheduler_tick,
    unschedule_topic,
    update_topic,
)
from studio.tts import list_voices
from studio.utils_script import parse_tagged_script, raw_from_tagged, script_structure_warnings, validate_tagged_script
from studio import youtube as yt

STATIC_DIR = STUDIO_DIR / "static"
TEMPLATES_DIR = STUDIO_DIR / "templates"


class NewProject(BaseModel):
    topic: str
    duration_seconds: int = Field(ge=15, le=1800)
    title: str = ""
    aspect: str = DEFAULT_ASPECT


class RenameVideoBody(BaseModel):
    title: str
    topic: str | None = None
    update_youtube: bool = True
    rename_folder: bool = False


class ProjectPatch(BaseModel):
    aspect: str | None = None
    title: str | None = None
    image_provider: str | None = None
    video_layout: str | None = None
    character_size: str | None = None
    art_style: str | None = None
    include_bubblehead: bool | None = None
    tts_provider: str | None = None
    voice_provider: str | None = None
    voice_id: str | None = None
    background_file: str | None = None
    music_id: str | None = None
    music_mode: str | None = None
    youtube_auto_upload: bool | None = None
    youtube_privacy: str | None = None
    youtube_channel_id: str | None = None
    youtube_description: str | None = None
    youtube_keywords: str | list[str] | None = None
    youtube_hashtags: str | list[str] | None = None
    generate_9x16: bool | None = None


class SetMusicBody(BaseModel):
    music_id: str | None = None
    track_id: str | None = None
    mode: str | None = None


class ScriptUpdate(BaseModel):
    script_tagged: str
    summary: str = ""
    scenes: list[str] = []
    youtube_description: str | None = None
    youtube_keywords: str | list[str] | None = None
    youtube_hashtags: str | list[str] | None = None


class GenerateScriptBody(BaseModel):
    extra: str = ""
    regenerate_pictures: bool = True
    regenerate_audio: bool = True
    generate_9x16: bool = False
    provider: str = ""
    voice_id: str = ""
    confirm_spend: bool = False
    spend_confirm_id: str = ""


class AudioBody(BaseModel):
    provider: str = ""
    voice_id: str = ""
    confirm_spend: bool = False
    spend_confirm_id: str = ""


class RenderBody(BaseModel):
    use_billboards: bool = True
    keep_frames: bool = False
    aspect: str | None = None
    layout: str | None = None
    character_size: str | None = None
    include_bubblehead: bool | None = None
    shuffle_music: bool = False


class RegenerateIllustrationBody(BaseModel):
    filename: str | None = None
    index: int | None = None
    kind: str | None = None
    confirm_spend: bool = False
    spend_confirm_id: str = ""


class RegenerateCoverBody(BaseModel):
    aspect: str | None = None
    confirm_spend: bool = False
    spend_confirm_id: str = ""


class RefreshCoversBody(BaseModel):
    covers: list[dict] = []
    render: bool = True
    wait: bool = False
    layout: str | None = None
    character_size: str | None = None
    include_bubblehead: bool | None = None


class CoverProviderBody(BaseModel):
    cover_provider: str = ""


class SetActiveCoverBody(BaseModel):
    aspect: str = "16:9"
    version_id: str = ""


class SettingsBody(BaseModel):
    openai_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    fal_key: str | None = None
    openai_model: str | None = None
    openai_tts_model: str | None = None
    elevenlabs_model: str | None = None
    gentle_url: str | None = None
    tts_provider: str | None = None
    voice_provider: str | None = None
    openai_voice: str | None = None
    elevenlabs_voice_id: str | None = None
    local_voice: str | None = None
    image_provider: str | None = None
    cover_provider: str | None = None
    script_draft_provider: str | None = None
    text_provider: str | None = None
    script_provider: str | None = None
    lmstudio_base_url: str | None = None
    lmstudio_model: str | None = None
    comfyui_url: str | None = None
    video_layout: str | None = None
    character_size: str | None = None
    stickman_head_color: str | None = None
    music_volume_pct: int | None = None
    default_aspect: str | None = None
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_channel_id: str | None = None
    youtube_auto_upload: bool | None = None
    youtube_delete_file_after_upload: bool | None = None
    youtube_privacy: str | None = None
    auto_scheduler: bool | None = None
    hands_off: bool | None = None
    hands_off_interval_hours: float | None = None
    hands_off_min_queue: int | None = None
    max_concurrent_jobs: int | None = None
    per_user_concurrency: int | None = None
    admin_concurrency: int | None = None
    owner_priority: bool | None = None
    port: int | None = None
    public_tunnel: str | None = None
    ngrok_url: str | None = None
    ngrok_local_port: int | None = None
    ngrok_autostart: bool | None = None
    ngrok_basic_auth_user: str | None = None
    ngrok_basic_auth_password: str | None = None
    mcp_pin: str | None = None
    rate_limit_enabled: bool | None = None
    rate_limit_login: int | None = None
    rate_limit_login_window_sec: int | None = None
    rate_limit_api: int | None = None
    rate_limit_api_window_sec: int | None = None
    rate_limit_static: int | None = None
    rate_limit_static_window_sec: int | None = None
    require_spend_confirm: bool | None = None
    max_openai_calls_per_day: int | None = None
    max_flux_images_per_day: int | None = None
    stripe_secret_key: str | None = None
    stripe_publishable_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_product_id: str | None = None
    stripe_price_id: str | None = None
    stripe_price_amount_cents: int | None = None
    stripe_price_currency: str | None = None
    stripe_price_interval: str | None = None
    public_base_url: str | None = None
    membership_required: bool | None = None
    registration_mode: str | None = None
    registration_invite_code: str | None = None
    email_enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool | None = None
    smtp_use_ssl: bool | None = None
    email_from: str | None = None
    email_from_name: str | None = None
    email_reply_to: str | None = None
    email_templates: dict[str, Any] | None = None


class SpendConfirmBody(BaseModel):
    action: str = ""
    detail: str = ""
    units: int = 1
    project_id: str = ""
    confirm_spend: bool = False
    spend_confirm_id: str = ""


class FluxConfirmBody(BaseModel):
    confirm_spend: bool = False
    spend_confirm_id: str = ""


class GenerateTopicsBody(BaseModel):
    seed: str = ""
    count: int = 8
    duration_min: float = 5
    confirm_spend: bool = False
    spend_confirm_id: str = ""


class CreateTopicBody(BaseModel):
    title: str
    angle: str = ""
    duration_min: float = 5


class TopicPatch(BaseModel):
    title: str | None = None
    angle: str | None = None
    duration_min: float | None = None
    scheduled_at: str | None = None


class ScheduleTopicBody(BaseModel):
    topic_id: str = ""
    title: str = ""
    duration_min: float = 0
    angle: str = ""
    run: str = "queue"
    scheduled_at: str = ""
    run_now: bool = False
    confirm_spend: bool = False
    spend_confirm_id: str = ""


class NgrokBody(BaseModel):
    url: str | None = None
    local_port: int | None = None
    autostart: bool | None = None
    basic_auth_user: str | None = None
    basic_auth_password: str | None = None
    rotate_password: bool | None = None


class PromptsBody(BaseModel):
    prompts: dict[str, str] | None = None
    key: str | None = None
    text: str | None = None
    value: str | None = None


class YoutubeCodeBody(BaseModel):
    code: str = ""
    url: str = ""


class YoutubeChannelBody(BaseModel):
    channel_id: str = ""
    title: str = ""


class YoutubeDisconnectBody(BaseModel):
    channel_id: str = ""


class YoutubeUploadBody(BaseModel):
    privacy_status: str = ""
    title: str = ""
    description: str = ""
    tags: str | list[str] | None = None
    keywords: str | list[str] | None = None
    aspect: str = ""
    channel_id: str = ""


class PromptResetBody(BaseModel):
    key: str | None = None


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


class SignupBody(BaseModel):
    username: str = ""
    password: str = ""
    email: str = ""
    invite_code: str = ""


class ApiKeyCreateBody(BaseModel):
    name: str = "default"
    rate_limit: int = 60


class ChangePasswordBody(BaseModel):
    current_password: str = ""
    new_password: str = ""
    confirm_password: str = ""


class ForgotPasswordBody(BaseModel):
    email: str = ""
    username: str = ""


class ResetPasswordBody(BaseModel):
    token: str = ""
    new_password: str = ""
    confirm_password: str = ""


class EmailTestBody(BaseModel):
    to: str = ""


class AdminMemberBody(BaseModel):
    username: str = ""
    password: str = ""
    email: str = ""
    role: str = "member"
    subscription_status: str | None = None
    disabled: bool | None = None


class AdminMemberPatch(BaseModel):
    email: str | None = None
    role: str | None = None
    password: str | None = None
    subscription_status: str | None = None
    disabled: bool | None = None


class RefundBody(BaseModel):
    payment_intent: str = ""
    charge_id: str = ""
    amount_cents: int | None = None
    reason: str = "requested_by_customer"


class RateLimitASGIMiddleware:
    """Pure ASGI rate limit so streamable /mcp is not buffered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from studio.rate_limit import enforce

        request = Request(scope, receive=receive)
        blocked = enforce(request)
        if blocked is not None:
            await blocked(scope, receive, send)
            return

        rate_headers = getattr(getattr(request, "state", None), "rate_limit_headers", None) or {}

        async def send_with_headers(message):
            if message.get("type") == "http.response.start" and rate_headers:
                headers = list(message.get("headers") or [])
                existing = {k.decode().lower() for k, _ in headers}
                for key, value in rate_headers.items():
                    if key.lower() not in existing:
                        headers.append((key.lower().encode(), str(value).encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers if rate_headers else send)


class AuditASGIMiddleware:
    """Persist significant /api actions to user_data/audit.log (JSONL). Pure ASGI so uploads are not buffered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        method = (scope.get("method") or "GET").upper()
        noisy_get = method == "GET" and (
            path in (
                "/api/jobs",
                "/api/projects",
                "/api/topics",
                "/api/settings",
                "/api/auth/me",
                "/api/gentle",
                "/api/ngrok",
                "/api/mcp",
                "/api/audit",
                "/api/health",
                "/api/gpu-lock",
                "/api/voices",
                "/api/music",
                "/api/backgrounds",
                "/api/prompts",
                "/api/script-rules",
                "/api/spend",
            )
            or path.startswith("/api/projects/")
            or path.startswith("/api/backgrounds/")
            or path.startswith("/api/music/")
        )
        status_box = {"code": 0}

        async def send_wrap(message):
            if message.get("type") == "http.response.start":
                status_box["code"] = int(message.get("status") or 0)
            await send(message)

        await self.app(scope, receive, send_wrap)
        code = status_box["code"]
        if noisy_get and code and code < 400:
            return
        try:
            from studio.audit import write_entry
            from studio.rate_limit import client_ip

            request = Request(scope, receive=receive)
            username = ""
            auth_method = ""
            try:
                user = studio_auth.try_jwt_user(request)
                if user:
                    username = user
                    auth_method = "jwt"
            except Exception:
                pass
            write_entry(
                action=f"{method} {path}",
                source="api",
                ip=client_ip(request),
                username=username,
                auth_method=auth_method,
                success=bool(code and code < 400),
                status=code or 0,
                error="" if (code and code < 400) else f"HTTP {code or '?'}",
            )
        except Exception:
            pass


class AuthASGIMiddleware:
    """JWT for /api + docs/OpenAPI. Pure ASGI so multipart uploads are not buffered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        # Electron/.exe desktop: single-user local app — skip JWT, membership, tenant gates.
        if studio_auth.is_desktop_mode():
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if studio_auth.is_mcp_path(path):
            await self.app(scope, receive, send)
            return
        if studio_auth.path_requires_auth(path):
            request = Request(scope, receive=receive)
            tokens = studio_auth.extract_tokens(request)
            if not tokens:
                body = JSONResponse({"detail": "Not authenticated"}, status_code=401)
                await body(scope, receive, send)
                return
            decoded_ok = False
            last_detail: str | object = "Not authenticated"
            last_status = 401
            for token in tokens:
                try:
                    studio_auth.decode_token(token)
                    decoded_ok = True
                    break
                except HTTPException as exc:
                    last_detail = exc.detail
                    last_status = exc.status_code
            if not decoded_ok:
                body = JSONResponse({"detail": last_detail}, status_code=last_status)
                await body(scope, receive, send)
                return
            if studio_auth.path_requires_membership(path, scope.get("method") or "GET"):
                try:
                    from studio.members import user_has_access

                    user = studio_auth.require_session(request)
                    if not user_has_access(user):
                        body = JSONResponse(
                            studio_auth.membership_denied_payload(),
                            status_code=402,
                        )
                        await body(scope, receive, send)
                        return
                except HTTPException as exc:
                    body = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                    await body(scope, receive, send)
                    return
            # Multi-tenant: members may only touch their own /api/projects/{id}/… and /api/jobs/{id}/…
            for prefix, kind in (("/api/projects/", "project"), ("/api/jobs/", "job")):
                if not path.startswith(prefix):
                    continue
                parts = [p for p in path.split("/") if p]
                # api, projects|jobs, {id}, …
                if len(parts) >= 3 and parts[0] == "api" and parts[1] in ("projects", "jobs"):
                    project_id = parts[2]
                    if project_id and project_id not in (".", ".."):
                        try:
                            from studio.tenant import require_project_access

                            require_project_access(request, project_id)
                        except HTTPException as exc:
                            detail = exc.detail
                            if not isinstance(detail, (str, dict, list)):
                                detail = str(detail)
                            body = JSONResponse({"detail": detail}, status_code=exc.status_code)
                            await body(scope, receive, send)
                            return
                break
            if path.startswith("/api/topics/"):
                parts = [p for p in path.split("/") if p]
                # api, topics, {id} or generate/schedule — only gate uuid-like topic ids
                if len(parts) >= 3 and parts[0] == "api" and parts[1] == "topics":
                    topic_id = parts[2]
                    if topic_id and topic_id not in ("generate", "schedule", ".", ".."):
                        try:
                            from studio.tenant import require_topic_access

                            require_topic_access(request, topic_id)
                        except HTTPException as exc:
                            detail = exc.detail
                            if not isinstance(detail, (str, dict, list)):
                                detail = str(detail)
                            body = JSONResponse({"detail": detail}, status_code=exc.status_code)
                            await body(scope, receive, send)
                            return
        await self.app(scope, receive, send)


class McpAuthASGIMiddleware:
    """Pure ASGI gate for /mcp so streamable HTTP is not buffered."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not studio_auth.is_mcp_path(path):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        try:
            studio_auth.authorize_mcp_http(request)
        except HTTPException as exc:
            headers = {"WWW-Authenticate": 'Bearer realm="Stickman Automation MCP"'}
            # Prefer challenge headers from authorize_mcp_http when present.
            if getattr(exc, "headers", None):
                headers.update(exc.headers)
            body = JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=headers,
            )
            await body(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _cors_origins() -> list[str]:
    """Tight CORS for production; localhost retained for desktop/dev (port from settings/env)."""
    import os

    raw = (os.environ.get("BUBBLEPOD_CORS_ORIGINS") or os.environ.get("LAZYKH_CORS_ORIGINS") or "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    origins = [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]
    try:
        from studio.settings import load_settings, local_base_url, resolve_public_base_url

        settings = load_settings()
        local = local_base_url(settings)
        origins.extend([local, local.replace("127.0.0.1", "localhost")])
        public = resolve_public_base_url(settings)
        if public:
            from urllib.parse import urlparse

            parsed = urlparse(public)
            if parsed.scheme and parsed.netloc:
                # Origin header is scheme+host only (no /app path).
                origins.append(f"{parsed.scheme}://{parsed.netloc}")
            origins.append(public)
    except Exception:
        from studio.settings import DEFAULT_LISTEN_PORT

        origins.extend(
            [
                f"http://127.0.0.1:{DEFAULT_LISTEN_PORT}",
                f"http://localhost:{DEFAULT_LISTEN_PORT}",
            ]
        )
    # Dedupe preserving order
    seen = set()
    out = []
    for o in origins:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


def _studio_prefix() -> str:
    from studio.settings import studio_url_prefix

    return studio_url_prefix()


def _pref_path(path: str) -> str:
    """Prefix a root-absolute path when Studio is under a subpath (e.g. /app)."""
    prefix = _studio_prefix()
    if not prefix or not path.startswith("/"):
        return path
    if path == prefix or path.startswith(prefix + "/"):
        return path
    return prefix + path


def _render_html(page: Path, *, cache_control: str = "no-store") -> HTMLResponse:
    """Serve an HTML template, injecting window.__STUDIO_BASE__ and rewriting asset hrefs."""
    import json

    html = page.read_text(encoding="utf-8")
    prefix = _studio_prefix()
    if prefix:
        inject = f"<script>window.__STUDIO_BASE__={json.dumps(prefix)};</script>\n"
        if "<head>" in html.lower():
            # Preserve original <head> casing
            idx = html.lower().index("<head>")
            html = html[: idx + 6] + "\n  " + inject + html[idx + 6 :]
        else:
            html = inject + html
        for old, new in (
            ('href="/static/', f'href="{prefix}/static/'),
            ('src="/static/', f'src="{prefix}/static/'),
            ('href="/pricing', f'href="{prefix}/pricing'),
            ('href="/admin', f'href="{prefix}/admin'),
            ('href="/"', f'href="{prefix}/"'),
            ('href="/#', f'href="{prefix}/#'),
        ):
            html = html.replace(old, new)
    headers = {"Cache-Control": cache_control} if cache_control else None
    return HTMLResponse(html, headers=headers)


def _session_ok(request: Request) -> bool:
    """True when the request carries a valid Studio session (cookie or Bearer)."""
    if studio_auth.is_desktop_mode():
        return True
    try:
        studio_auth.require_session(request)
        return True
    except HTTPException:
        return False


def _redir(path: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url=_pref_path(path), status_code=status_code)


def _err(exc: Exception) -> HTTPException:
    from studio.gpu_lock import GpuLockTimeout
    from studio.spend_guard import SpendBlocked, spend_error_http

    if isinstance(exc, GpuLockTimeout):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SpendBlocked):
        status, payload = spend_error_http(exc)
        return HTTPException(status_code=status, detail=payload)
    return HTTPException(status_code=400, detail=str(exc))


def _reject_desktop_saas(feature: str = "This feature") -> None:
    """Raise 404 when auth / multi-tenant SaaS is disabled in the .exe app."""
    if studio_auth.is_desktop_mode():
        raise HTTPException(
            status_code=404,
            detail=f"{feature} is not available in the desktop app.",
        )


def _gate_spend(
    action: str,
    *,
    confirm_spend: bool = False,
    spend_confirm_id: str = "",
    units: int = 1,
    detail: str = "",
    project_id: str = "",
    provider: str = "",
    tts_provider: str = "",
) -> None:
    from studio.spend_guard import require_spend

    ctx: dict = {}
    if project_id:
        ctx["project_id"] = project_id
    if provider:
        ctx["image_provider"] = provider
    if tts_provider:
        ctx["tts_provider"] = tts_provider
    require_spend(
        action,
        confirm_spend=confirm_spend,
        spend_confirm_id=spend_confirm_id or None,
        units=units,
        source="api",
        detail=detail or action,
        context=ctx,
    )


def create_app() -> FastAPI:
    ensure_dirs()
    ensure_fallback_background()
    try:
        studio_auth.ensure_auth_config()
    except Exception:
        pass
    try:
        from studio.members import ensure_members_store

        ensure_members_store()
    except Exception:
        pass
    try:
        from studio.tenant import migrate_orphans_to_admin

        migrate_orphans_to_admin()
    except Exception:
        pass
    try:
        studio_auth.ensure_mcp_pin()
    except Exception:
        pass
    try:
        from studio.mcp_install import ensure_claude_mcp_config

        ensure_claude_mcp_config()
    except Exception:
        pass

    mcp_app = None
    mcp_build = None
    try:
        from studio.mcp_server import MCP_BUILD, build_mcp

        mcp_build = MCP_BUILD
        mcp = build_mcp()
        mcp_app = mcp.http_app(path="/")
    except Exception:
        mcp_app = None
        mcp_build = None

    def _gentle_boot() -> None:
        try:
            ensure_gentle(required=False)
        except Exception:
            pass

    def _ngrok_boot() -> None:
        try:
            from studio.ngrok_tunnel import ensure_ngrok_autostart

            ensure_ngrok_autostart()
        except Exception:
            pass

    def _topic_scheduler_loop() -> None:
        time.sleep(2)
        while True:
            try:
                scheduler_tick()
            except Exception:
                pass
            time.sleep(SCHEDULER_INTERVAL_SEC)

    @asynccontextmanager
    async def studio_lifespan(app):
        inner_cm = None
        if mcp_app is not None:
            inner = getattr(mcp_app, "lifespan", None)
            if inner is not None:
                inner_cm = inner(app)
                await inner_cm.__aenter__()
        threading.Thread(target=_gentle_boot, daemon=True, name="gentle-boot").start()
        threading.Thread(target=_ngrok_boot, daemon=True, name="ngrok-boot").start()
        threading.Thread(target=_topic_scheduler_loop, daemon=True, name="topic-scheduler").start()
        try:
            from studio.restart import clear_stale_gpu_lock

            clear_stale_gpu_lock()
        except Exception:
            pass
        try:
            from studio.job_queue import ensure_queue_boot

            threading.Thread(target=ensure_queue_boot, daemon=True, name="job-queue-boot").start()
        except Exception:
            pass
        try:
            def _warm_models() -> None:
                from studio.deps_health import model_cache_status, warm_local_tts_models
                from studio.settings import load_settings, normalize_tts_provider

                settings = load_settings()
                if normalize_tts_provider(settings.get("tts_provider")) != "local":
                    return
                cache = model_cache_status()
                if cache.get("chatterbox_cached"):
                    return
                warm_local_tts_models(timeout_sec=900)

            threading.Thread(target=_warm_models, daemon=True, name="tts-model-warm").start()
        except Exception:
            pass
        try:
            yield
        finally:
            try:
                from studio.gentle import stop_owned_local_gentle
                from studio.restart import keep_gentle_on_shutdown

                if not keep_gentle_on_shutdown():
                    stop_owned_local_gentle()
            except Exception:
                pass
            try:
                from studio.ngrok_tunnel import stop_owned_ngrok
                from studio.restart import keep_gentle_on_shutdown

                # Keep tunnel across Studio self-restart (same rule as Gentle).
                if not keep_gentle_on_shutdown():
                    stop_owned_ngrok()
            except Exception:
                pass
            if inner_cm is not None:
                await inner_cm.__aexit__(None, None, None)

    app = FastAPI(
        title="Stickman Automation",
        lifespan=studio_lifespan,
        # Do not set root_path here: Starlette StaticFiles mounts break when
        # root_path is set and nginx strips /app. Public URLs use PUBLIC_BASE_URL
        # + window.__STUDIO_BASE__ instead.
        # Docs/OpenAPI served only to authenticated sessions (AuthMiddleware).
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    # Last add_middleware = outermost on the request.
    # CORS → RateLimit (ASGI) → McpAuth (ASGI) → Audit (ASGI) → Auth (ASGI) → app
    app.add_middleware(AuthASGIMiddleware)
    app.add_middleware(AuditASGIMiddleware)
    app.add_middleware(McpAuthASGIMiddleware)
    app.add_middleware(RateLimitASGIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        # SaaS: never ship the workspace shell to logged-out browsers.
        if not studio_auth.is_desktop_mode() and not _session_ok(request):
            login = TEMPLATES_DIR / "login.html"
            if login.is_file():
                return _render_html(login)
            raise HTTPException(status_code=401, detail="Sign in required")
        index = TEMPLATES_DIR / "index.html"
        return _render_html(index)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_home():
        if studio_auth.is_desktop_mode():
            return _redir("/")
        page = TEMPLATES_DIR / "admin.html"
        if not page.is_file():
            raise HTTPException(404, "Admin UI missing")
        return _render_html(page)

    @app.get("/pricing", response_class=HTMLResponse)
    def pricing_home():
        if studio_auth.is_desktop_mode():
            return _redir("/")
        page = TEMPLATES_DIR / "pricing.html"
        if not page.is_file():
            raise HTTPException(404, "Pricing page missing")
        return _render_html(page)

    @app.get("/billing/success", response_class=HTMLResponse)
    def billing_success():
        if studio_auth.is_desktop_mode():
            return _redir("/")
        return _redir("/pricing?checkout=success")

    @app.get("/billing/cancel", response_class=HTMLResponse)
    def billing_cancel():
        if studio_auth.is_desktop_mode():
            return _redir("/")
        return _redir("/pricing?checkout=canceled")

    @app.post("/api/auth/login")
    def auth_login(body: LoginBody, response: Response, request: Request):
        _reject_desktop_saas("Sign-in")
        from studio.members import authenticate_user, ensure_members_store, public_session
        from studio.rate_limit import clear_login_limits, client_ip, note_login_failure

        ensure_members_store()
        username = (body.username or "").strip()
        password = body.password or ""
        user = authenticate_user(username, password) if username and password else None
        if not user:
            note_login_failure(request)
            raise HTTPException(status_code=401, detail="Invalid username or password")
        clear_login_limits(client_ip(request))
        token, expires_in = studio_auth.issue_token(
            user["username"],
            user_id=user["id"],
            role=user.get("role") or "member",
            token_version=int(user.get("token_version") or 0),
        )
        studio_auth.set_auth_cookie(response, token, expires_in)
        return {
            "token": token,
            "expires_in": expires_in,
            **public_session(user),
        }

    @app.post("/api/auth/signup")
    def auth_signup(body: SignupBody, response: Response):
        _reject_desktop_saas("Sign-up")
        from studio.members import create_user, ensure_members_store, public_session
        from studio.settings import load_settings, normalize_registration_mode

        settings = load_settings()
        mode = normalize_registration_mode(settings.get("registration_mode"), "invite_only")
        if mode == "disabled":
            raise HTTPException(
                status_code=403,
                detail="Registration is disabled. Ask an admin for access.",
            )
        if mode == "invite_only":
            expected = str(settings.get("registration_invite_code") or "").strip()
            provided = (body.invite_code or "").strip()
            if not expected:
                raise HTTPException(
                    status_code=403,
                    detail="Registration is invite-only. Ask an admin to set an invite code.",
                )
            if provided != expected:
                raise HTTPException(status_code=403, detail="Invalid invite code")

        ensure_members_store()
        try:
            user = create_user(
                username=body.username,
                password=body.password,
                email=body.email,
                role="member",
                subscription_status="none",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Re-load full record for token fields.
        from studio.members import get_user_by_id

        full = get_user_by_id(user["id"]) or user
        token, expires_in = studio_auth.issue_token(
            full["username"],
            user_id=full["id"],
            role=full.get("role") or "member",
            token_version=int(full.get("token_version") or 0),
        )
        studio_auth.set_auth_cookie(response, token, expires_in)
        try:
            from studio.email import notify_signup

            notify_signup(full)
        except Exception:
            pass
        return {
            "token": token,
            "expires_in": expires_in,
            "created": True,
            **public_session(full),
            "next": "Subscribe via /api/billing/checkout to unlock Studio (all members equal).",
        }

    @app.get("/api/auth/api-keys")
    def list_api_keys(request: Request):
        user = studio_auth.require_session(request)
        from studio.api_keys import list_keys_for_user

        return {"keys": list_keys_for_user(str(user.get("id") or ""))}

    @app.post("/api/auth/api-keys")
    def create_api_key(request: Request, body: ApiKeyCreateBody):
        user = studio_auth.require_session(request)
        from studio.api_keys import create_api_key as _create

        return _create(
            str(user.get("id") or ""),
            name=body.name or "default",
            rate_limit=int(body.rate_limit or 60),
        )

    @app.delete("/api/auth/api-keys/{key_id}")
    def delete_api_key(request: Request, key_id: str):
        user = studio_auth.require_session(request)
        from studio.api_keys import revoke_api_key

        try:
            return revoke_api_key(str(user.get("id") or ""), key_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/api/auth/forgot-password")
    def auth_forgot_password(body: ForgotPasswordBody):
        """Always return ok (no account enumeration). Sends reset email when possible."""
        _reject_desktop_saas("Password reset")
        from studio.email import _base_url, notify_password_reset
        from studio.members import get_user_by_email, get_user_by_username
        from studio.password_reset import DEFAULT_TTL_MINUTES, create_reset_token

        email = (body.email or "").strip().lower()
        username = (body.username or "").strip()
        user = get_user_by_email(email) if email else None
        if user is None and username:
            user = get_user_by_username(username)
        if user and (user.get("email") or "").strip():
            token, _exp = create_reset_token(user["id"], ttl_minutes=DEFAULT_TTL_MINUTES)
            reset_url = f"{_base_url()}/#reset={token}"
            notify_password_reset(user, reset_url, expires_minutes=DEFAULT_TTL_MINUTES)
        return {
            "ok": True,
            "message": "If that account has an email on file, a reset link was sent.",
        }

    @app.post("/api/auth/reset-password")
    def auth_reset_password(body: ResetPasswordBody, response: Response):
        _reject_desktop_saas("Password reset")
        from studio.email import notify_password_changed
        from studio.members import get_user_by_id, public_session, update_user
        from studio.password_reset import consume_reset_token

        new_pw = (body.new_password or "").strip()
        confirm = (body.confirm_password or "").strip()
        if len(new_pw) < 6:
            raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
        if new_pw != confirm:
            raise HTTPException(status_code=400, detail="New password and confirmation do not match")
        uid = consume_reset_token(body.token or "")
        if not uid:
            raise HTTPException(status_code=400, detail="Invalid or expired reset link")
        user = get_user_by_id(uid)
        if not user or user.get("disabled"):
            raise HTTPException(status_code=400, detail="Invalid or expired reset link")
        update_user(uid, password=new_pw, must_change_password=False)
        fresh = get_user_by_id(uid) or user
        token, expires_in = studio_auth.issue_token(
            fresh["username"],
            user_id=fresh["id"],
            role=fresh.get("role") or "member",
            token_version=int(fresh.get("token_version") or 0),
        )
        studio_auth.set_auth_cookie(response, token, expires_in)
        try:
            notify_password_changed(fresh)
        except Exception:
            pass
        return {
            "ok": True,
            "token": token,
            "expires_in": expires_in,
            **public_session(fresh),
        }

    @app.post("/api/auth/logout")
    def auth_logout(response: Response):
        studio_auth.clear_auth_cookie(response)
        return {"ok": True, "desktop_mode": studio_auth.is_desktop_mode()}

    @app.get("/api/auth/me")
    def auth_me(request: Request, response: Response):
        from studio.members import public_session

        user = studio_auth.require_session(request)
        # Re-assert HttpOnly cookie from the first candidate that matches this user
        # (cookie or Bearer), so a stale localStorage token cannot block HTML /.
        if not studio_auth.is_desktop_mode():
            for token in studio_auth.extract_tokens(request):
                try:
                    matched = studio_auth._user_from_access_token(token)
                except HTTPException:
                    continue
                if matched.get("id") == user.get("id"):
                    studio_auth.set_auth_cookie(response, token, studio_auth.TOKEN_TTL_SEC)
                    break
        out = public_session(user)
        out["desktop_mode"] = studio_auth.is_desktop_mode()
        out["auth_required"] = not studio_auth.is_desktop_mode()
        return out

    @app.get("/api/me/usage")
    def me_usage(request: Request):
        """Current-period usage vs plan quotas for the signed-in user."""
        user = studio_auth.require_session(request)
        from studio.usage import usage_snapshot

        return {"ok": True, **usage_snapshot(user)}

    @app.post("/api/auth/change-password")
    def auth_change_password(body: ChangePasswordBody, request: Request, response: Response):
        _reject_desktop_saas("Account password")
        user = studio_auth.require_session(request)
        new_pw = (body.new_password or "").strip()
        confirm = (body.confirm_password or "").strip()
        if new_pw != confirm:
            raise HTTPException(status_code=400, detail="New password and confirmation do not match")
        result = studio_auth.change_password(
            body.current_password or "",
            new_pw,
            username=user.get("username"),
        )
        from studio.members import get_user_by_id

        fresh = get_user_by_id(user.get("id") or "") or user
        token, expires_in = studio_auth.issue_token(
            result["username"],
            user_id=fresh.get("id") or "",
            role=fresh.get("role") or "member",
            token_version=int(fresh.get("token_version") or 0),
        )
        studio_auth.set_auth_cookie(response, token, expires_in)
        try:
            from studio.email import notify_password_changed

            notify_password_changed(fresh)
        except Exception:
            pass
        return {**result, "token": token, "expires_in": expires_in, "must_change_password": False}

    @app.get("/api/billing/config")
    def billing_config():
        if studio_auth.is_desktop_mode():
            return {
                "enabled": False,
                "desktop_mode": True,
                "stripe_configured": False,
            }
        from studio.stripe_billing import public_billing_config

        return public_billing_config()

    @app.get("/api/billing/status")
    def billing_status(request: Request):
        from studio.members import public_session
        from studio.stripe_billing import public_billing_config, stripe_configured

        user = studio_auth.require_session(request)
        if studio_auth.is_desktop_mode():
            return {
                **public_session(user),
                "stripe_configured": False,
                "catalog": {},
                "pricing_url": "",
                "desktop_mode": True,
                "auth_required": False,
            }
        return {
            **public_session(user),
            "stripe_configured": stripe_configured(),
            "catalog": public_billing_config(),
            "pricing_url": "/pricing",
        }

    @app.post("/api/billing/checkout")
    def billing_checkout(request: Request):
        _reject_desktop_saas("Membership checkout")
        from studio.stripe_billing import create_checkout_session

        user = studio_auth.require_session(request)
        if (user.get("role") or "") == "admin":
            return {"ok": True, "skipped": True, "reason": "Admins already have full access."}
        try:
            return create_checkout_session(user)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/billing/portal")
    def billing_portal(request: Request):
        _reject_desktop_saas("Billing portal")
        from studio.stripe_billing import create_portal_session

        user = studio_auth.require_session(request)
        try:
            return create_portal_session(user)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/billing/ensure-catalog")
    def billing_ensure_catalog(request: Request):
        _reject_desktop_saas("Billing catalog")
        studio_auth.require_admin(request)
        from studio.stripe_billing import ensure_membership_catalog

        try:
            return ensure_membership_catalog()
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/stripe/webhook")
    async def stripe_webhook(request: Request):
        _reject_desktop_saas("Stripe webhooks")
        from studio.stripe_billing import handle_webhook

        payload = await request.body()
        sig = request.headers.get("stripe-signature") or ""
        try:
            return handle_webhook(payload, sig)
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/admin/overview")
    def admin_overview(request: Request):
        _reject_desktop_saas("Admin console")
        studio_auth.require_admin(request)
        from studio.admin_ops import admin_overview_payload

        return admin_overview_payload()

    @app.get("/api/admin/jobs/failed")
    def admin_failed_jobs(request: Request, limit: int = Query(50, ge=1, le=200)):
        _reject_desktop_saas("Admin jobs")
        studio_auth.require_admin(request)
        from studio.admin_ops import list_failed_jobs

        rows = list_failed_jobs(limit=limit)
        return {"ok": True, "count": len(rows), "jobs": rows}

    @app.post("/api/admin/jobs/{job_id}/retry")
    def admin_retry_job(job_id: str, request: Request):
        _reject_desktop_saas("Admin jobs")
        admin = studio_auth.require_admin(request)
        from studio.admin_ops import retry_failed_job

        try:
            return retry_failed_job(job_id, admin_username=str(admin.get("username") or ""))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/admin/backup")
    def admin_backup_now(request: Request):
        _reject_desktop_saas("Admin backup")
        admin = studio_auth.require_admin(request)
        from studio.audit import write_entry
        from studio.backup import run_backup

        result = run_backup()
        try:
            write_entry(
                source="admin",
                action="backup",
                username=str(admin.get("username") or ""),
                args={"stamp": result.get("stamp")},
                success=True,
            )
        except Exception:
            pass
        return result

    @app.get("/api/admin/members")
    def admin_list_members(request: Request):
        _reject_desktop_saas("Member management")
        studio_auth.require_admin(request)
        from studio.members import list_users

        users = list_users(include_disabled=True)
        return {"users": users, "count": len(users), "membership_equal": True}

    @app.post("/api/admin/members")
    def admin_create_member(body: AdminMemberBody, request: Request):
        _reject_desktop_saas("Member management")
        studio_auth.require_admin(request)
        from studio.members import create_user

        try:
            user = create_user(
                username=body.username,
                password=body.password,
                email=body.email,
                role=body.role or "member",
                subscription_status=body.subscription_status or ("active" if body.role == "admin" else "none"),
            )
            return {"ok": True, "user": user}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/admin/members/{user_id}")
    def admin_patch_member(user_id: str, body: AdminMemberPatch, request: Request):
        _reject_desktop_saas("Member management")
        studio_auth.require_admin(request)
        from studio.members import update_user

        fields = body.model_dump(exclude_unset=True)
        try:
            user = update_user(user_id, **fields)
            return {"ok": True, "user": user}
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/admin/members/{user_id}")
    def admin_delete_member(user_id: str, request: Request):
        _reject_desktop_saas("Member management")
        studio_auth.require_admin(request)
        from studio.members import delete_user

        try:
            return delete_user(user_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/admin/billing/signups")
    def admin_signups(request: Request, limit: int = Query(100, ge=1, le=500)):
        _reject_desktop_saas("Admin billing")
        studio_auth.require_admin(request)
        from studio.stripe_billing import list_ledger

        return list_ledger("signups", limit=limit)

    @app.get("/api/admin/billing/payments")
    def admin_payments(request: Request, limit: int = Query(100, ge=1, le=500)):
        _reject_desktop_saas("Admin billing")
        studio_auth.require_admin(request)
        from studio.stripe_billing import list_ledger

        return list_ledger("payments", limit=limit)

    @app.get("/api/admin/billing/refunds")
    def admin_refunds(request: Request, limit: int = Query(100, ge=1, le=500)):
        _reject_desktop_saas("Admin billing")
        studio_auth.require_admin(request)
        from studio.stripe_billing import list_ledger

        return list_ledger("refunds", limit=limit)

    @app.get("/api/admin/billing/subscriptions")
    def admin_subscriptions(request: Request, limit: int = Query(100, ge=1, le=500)):
        _reject_desktop_saas("Admin billing")
        studio_auth.require_admin(request)
        from studio.stripe_billing import list_ledger

        return list_ledger("subscription_events", limit=limit)

    @app.post("/api/admin/billing/refunds")
    def admin_create_refund(body: RefundBody, request: Request):
        _reject_desktop_saas("Admin billing")
        studio_auth.require_admin(request)
        from studio.stripe_billing import create_refund

        try:
            return create_refund(
                payment_intent=body.payment_intent,
                charge_id=body.charge_id,
                amount_cents=body.amount_cents,
                reason=body.reason or "requested_by_customer",
            )
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/health")
    def health(full: bool = Query(False)):
        """Lightweight liveness for Electron + chrome polls.

        Avoid public_settings()/ngrok/asset scans on the hot path — those made the
        app feel stuck under frequent health checks. Pass ?full=1 for richer status.
        """
        from studio.gpu_lock import gpu_lock_public
        from studio.settings import (
            DEFAULT_LISTEN_PORT,
            app_env,
            load_settings,
            local_base_url,
            loopback_display_host,
            normalize_auto_scheduler,
            normalize_hands_off,
            normalize_listen_port,
            resolve_public_base_url,
        )

        settings = load_settings()
        host = loopback_display_host(settings.get("host"))
        port = normalize_listen_port(settings.get("port"), DEFAULT_LISTEN_PORT)
        local = local_base_url(settings)
        public = resolve_public_base_url(settings)
        prefix = _studio_prefix()
        mcp_path = f"{prefix}/mcp" if mcp_app is not None else None
        # public already includes /app when mounted under a subpath
        mcp_url = f"{public.rstrip('/')}/mcp" if mcp_app is not None and public else mcp_path
        out = {
            "ok": True,
            "host": host,
            "port": port,
            "url": f"{local}/",
            "local_url": f"{local}/",
            "public_base_url": public,
            "url_prefix": prefix,
            "app_env": app_env(),
            "gentle": gentle_status(),
            "mcp": mcp_path,
            "mcp_url": mcp_url,
            "public_mcp_url": mcp_url,
            "mcp_build": mcp_build,
            "auto_scheduler": normalize_auto_scheduler(settings.get("auto_scheduler", True)),
            "hands_off": normalize_hands_off(settings.get("hands_off")),
            "scheduler_interval_sec": SCHEDULER_INTERVAL_SEC,
            "gpu_lock": gpu_lock_public(),
            "desktop_mode": studio_auth.is_desktop_mode(),
            "auth_required": not studio_auth.is_desktop_mode(),
        }
        try:
            from studio.job_queue import public_status

            out["job_queue"] = public_status()
        except Exception:
            out["job_queue"] = None
        if full:
            ngrok = None
            try:
                from studio.ngrok_tunnel import ngrok_status

                ngrok = ngrok_status()
            except Exception:
                ngrok = None
            out["ngrok"] = ngrok
            out["warnings"] = asset_warnings()
            try:
                from studio.deps_health import check_dependencies

                deps = check_dependencies(settings=settings)
                out["ok"] = bool(deps.get("ok", True))
                out["tts"] = deps.get("tts")
                out["image_provider"] = deps.get("image_provider")
                out["model_cache"] = deps.get("model_cache")
                out["disk"] = deps.get("disk")
                out["gpu"] = deps.get("gpu")
                out["dependencies"] = deps
                out["can_start_jobs"] = deps.get("can_start_jobs")
                out["blockers"] = deps.get("blockers") or []
            except Exception as exc:
                out["dependencies_error"] = str(exc)
        return out

    @app.get("/api/gpu-lock")
    def get_gpu_lock():
        from studio.gpu_lock import snapshot

        return snapshot()

    @app.get("/api/queue")
    def get_job_queue(request: Request, history: bool = Query(False)):
        """Multi-user pipeline queue: user's jobs, or global view for admins."""
        from studio.job_queue import list_queue

        user = studio_auth.require_session(request)
        is_admin = (user.get("role") or "") == "admin"
        return list_queue(
            owner_id=None if is_admin else user.get("id"),
            is_admin=is_admin,
            include_history=history,
        )

    @app.post("/api/queue/{project_id}/cancel")
    def cancel_queued_job(project_id: str, request: Request):
        from studio.job_queue import cancel
        from studio.tenant import require_project_access

        user, _meta = require_project_access(request, project_id)
        try:
            return cancel(
                project_id,
                owner_id=str(user.get("id") or "") or None,
                is_admin=(user.get("role") or "") == "admin",
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/admin/queue/pump")
    def admin_pump_queue(request: Request):
        studio_auth.require_admin(request)
        from studio.job_queue import pump

        return pump()

    @app.post("/api/admin/email/test")
    def admin_email_test(body: EmailTestBody, request: Request):
        studio_auth.require_admin(request)
        from studio.email import send_test_email
        from studio.members import get_user_by_id

        to = (body.to or "").strip()
        if not to:
            user = studio_auth.require_session(request)
            to = (user.get("email") or "").strip()
        if not to:
            raise HTTPException(status_code=400, detail="Provide a recipient email address")
        try:
            return send_test_email(to)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/admin/restart")
    def post_admin_restart(request: Request):
        """Spawn a new `python run_studio.py`, then exit this uvicorn. Does not kill Gentle or VoiceSync."""
        studio_auth.require_admin(request)
        from studio.restart import begin_self_restart, restart_studio

        try:
            return begin_self_restart()
        except Exception as exc:
            try:
                return restart_studio(wait=False)
            except Exception:
                raise _err(exc)

    @app.get("/api/settings")
    def get_settings(request: Request):
        user = studio_auth.require_session(request)
        return settings_for_user(user)

    @app.get("/api/audit")
    def get_audit(request: Request, limit: int = Query(50, ge=1, le=200)):
        studio_auth.require_admin(request)
        from studio.audit import audit_public

        return audit_public(limit)

    @app.get("/api/spend")
    def get_spend(request: Request):
        studio_auth.require_admin(request)
        from studio.spend_guard import public_spend_status

        return public_spend_status()

    @app.get("/api/costs")
    def get_costs(request: Request, limit: int = Query(40, ge=1, le=200)):
        """Per-video Flux cost estimates + short daily spend series for the Costs screen."""
        from studio.costs import cost_report
        from studio.tenant import filter_owned

        try:
            user = studio_auth.require_session(request)
            report = cost_report(limit_projects=limit)
            if (user.get("role") or "") != "admin":
                projects = filter_owned(report.get("projects") or [], user)
                chart = filter_owned(report.get("chart") or [], user)
                report = {**report, "projects": projects, "chart": chart}
            return report
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/spend/confirm")
    def post_spend_confirm(body: SpendConfirmBody, request: Request):
        studio_auth.require_session(request)
        from studio.spend_guard import request_spend_confirm

        try:
            ctx = {}
            if body.project_id:
                ctx["project_id"] = body.project_id
            return request_spend_confirm(
                body.action or "pipeline_billed",
                detail=body.detail,
                units=body.units or 1,
                context=ctx,
            )
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/mcp")
    def get_mcp_settings(request: Request):
        studio_auth.require_admin(request)
        from studio.mcp_install import mcp_settings_payload

        return mcp_settings_payload(http_mounted=mcp_app is not None)

    @app.get("/api/mcp/download/{token}")
    def mcp_download_file(token: str, request: Request):
        """Short-lived artifact download minted by MCP get_file. Token is the credential (no JWT)."""
        import mimetypes

        from studio.audit import write_entry
        from studio.mcp_files import resolve_download_token
        from studio.rate_limit import client_ip

        ip = ""
        try:
            ip = client_ip(request)
        except Exception:
            pass
        try:
            path = resolve_download_token(token)
        except FileNotFoundError as exc:
            try:
                write_entry(
                    action="mcp_download",
                    source="api",
                    ip=ip,
                    success=False,
                    error=str(exc),
                    args={"token": "***"},
                    status=404,
                )
            except Exception:
                pass
            raise HTTPException(404, str(exc))
        except RuntimeError as exc:
            try:
                write_entry(
                    action="mcp_download",
                    source="api",
                    ip=ip,
                    success=False,
                    error=str(exc),
                    args={"token": "***"},
                    status=403,
                )
            except Exception:
                pass
            raise HTTPException(403, str(exc))
        try:
            write_entry(
                action="mcp_download",
                source="api",
                ip=ip,
                success=True,
                args={"name": path.name, "size": path.stat().st_size},
                status=200,
            )
        except Exception:
            pass
        media, _ = mimetypes.guess_type(str(path))
        return FileResponse(
            path,
            media_type=media or "application/octet-stream",
            filename=path.name,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/mcp/install-claude")
    def post_mcp_install_claude(request: Request):
        studio_auth.require_admin(request)
        from studio.mcp_install import ensure_claude_mcp_config, mcp_settings_payload

        try:
            result = ensure_claude_mcp_config()
            payload = mcp_settings_payload(http_mounted=mcp_app is not None)
            payload["install"] = result
            return payload
        except Exception as exc:
            raise _err(exc)

    @app.put("/api/settings")
    def put_settings(body: SettingsBody, request: Request):
        studio_auth.require_admin(request)
        raw = body.model_dump()
        # Booleans / zeros must survive scrub; secrets ignore empty/********.
        updates = scrub_secret_updates({k: v for k, v in raw.items() if v is not None})
        # Drop empty non-secret strings that would otherwise clear optional fields poorly
        for key in list(updates.keys()):
            if key in (
                "openai_api_key",
                "elevenlabs_api_key",
                "fal_key",
                "youtube_client_secret",
                "ngrok_basic_auth_password",
                "mcp_pin",
                "smtp_password",
                "stripe_secret_key",
                "stripe_webhook_secret",
            ):
                if is_placeholder_secret(updates.get(key)):
                    updates.pop(key, None)
        if "music_volume_pct" in updates or body.music_volume_pct == 0:
            updates["music_volume_pct"] = body.music_volume_pct
        if body.youtube_auto_upload is not None:
            updates["youtube_auto_upload"] = body.youtube_auto_upload
        if body.youtube_delete_file_after_upload is not None:
            updates["youtube_delete_file_after_upload"] = body.youtube_delete_file_after_upload
        if body.auto_scheduler is not None:
            updates["auto_scheduler"] = body.auto_scheduler
        if body.hands_off is not None:
            updates["hands_off"] = body.hands_off
        if body.hands_off_interval_hours is not None:
            updates["hands_off_interval_hours"] = body.hands_off_interval_hours
        if body.hands_off_min_queue is not None:
            updates["hands_off_min_queue"] = body.hands_off_min_queue
        if body.max_concurrent_jobs is not None:
            updates["max_concurrent_jobs"] = body.max_concurrent_jobs
        if body.per_user_concurrency is not None:
            updates["per_user_concurrency"] = body.per_user_concurrency
        if body.admin_concurrency is not None:
            updates["admin_concurrency"] = body.admin_concurrency
        if body.owner_priority is not None:
            updates["owner_priority"] = body.owner_priority
        if body.public_tunnel is not None:
            updates["public_tunnel"] = body.public_tunnel
        if body.ngrok_url is not None:
            updates["ngrok_url"] = body.ngrok_url
        if body.ngrok_local_port is not None:
            updates["ngrok_local_port"] = body.ngrok_local_port
        if body.ngrok_autostart is not None:
            updates["ngrok_autostart"] = body.ngrok_autostart
        if body.ngrok_basic_auth_user is not None and str(body.ngrok_basic_auth_user).strip():
            updates["ngrok_basic_auth_user"] = body.ngrok_basic_auth_user
        if body.ngrok_basic_auth_password is not None and not is_placeholder_secret(body.ngrok_basic_auth_password):
            updates["ngrok_basic_auth_password"] = body.ngrok_basic_auth_password
        if body.mcp_pin is not None and not is_placeholder_secret(body.mcp_pin):
            updates["mcp_pin"] = body.mcp_pin
        if body.lmstudio_model is not None:
            updates["lmstudio_model"] = body.lmstudio_model
        if body.comfyui_url is not None:
            updates["comfyui_url"] = body.comfyui_url
        if body.rate_limit_enabled is not None:
            updates["rate_limit_enabled"] = body.rate_limit_enabled
        if body.rate_limit_login is not None:
            updates["rate_limit_login"] = body.rate_limit_login
        if body.rate_limit_login_window_sec is not None:
            updates["rate_limit_login_window_sec"] = body.rate_limit_login_window_sec
        if body.rate_limit_api is not None:
            updates["rate_limit_api"] = body.rate_limit_api
        if body.rate_limit_api_window_sec is not None:
            updates["rate_limit_api_window_sec"] = body.rate_limit_api_window_sec
        if body.rate_limit_static is not None:
            updates["rate_limit_static"] = body.rate_limit_static
        if body.rate_limit_static_window_sec is not None:
            updates["rate_limit_static_window_sec"] = body.rate_limit_static_window_sec
        if body.require_spend_confirm is not None:
            updates["require_spend_confirm"] = body.require_spend_confirm
        if body.max_openai_calls_per_day is not None:
            updates["max_openai_calls_per_day"] = body.max_openai_calls_per_day
        if body.max_flux_images_per_day is not None:
            updates["max_flux_images_per_day"] = body.max_flux_images_per_day
        if body.stickman_head_color is not None and str(body.stickman_head_color).strip():
            updates["stickman_head_color"] = body.stickman_head_color
        if body.membership_required is not None:
            updates["membership_required"] = body.membership_required
        if body.registration_mode is not None:
            from studio.settings import normalize_registration_mode

            updates["registration_mode"] = normalize_registration_mode(body.registration_mode)
        if body.registration_invite_code is not None:
            updates["registration_invite_code"] = str(body.registration_invite_code or "").strip()
        if body.email_enabled is not None:
            updates["email_enabled"] = body.email_enabled
        if body.smtp_host is not None:
            updates["smtp_host"] = body.smtp_host
        if body.smtp_port is not None:
            updates["smtp_port"] = body.smtp_port
        if body.smtp_user is not None:
            updates["smtp_user"] = body.smtp_user
        if body.smtp_password is not None and not is_placeholder_secret(body.smtp_password):
            updates["smtp_password"] = body.smtp_password
        if body.smtp_use_tls is not None:
            updates["smtp_use_tls"] = body.smtp_use_tls
        if body.smtp_use_ssl is not None:
            updates["smtp_use_ssl"] = body.smtp_use_ssl
        if body.email_from is not None:
            updates["email_from"] = body.email_from
        if body.email_from_name is not None:
            updates["email_from_name"] = body.email_from_name
        if body.email_reply_to is not None:
            updates["email_reply_to"] = body.email_reply_to
        if body.email_templates is not None:
            updates["email_templates"] = body.email_templates
        if updates:
            prev_image = None
            if "image_provider" in updates:
                prev_image = (load_settings().get("image_provider") or "").strip()
            try:
                save_settings(updates)
            except RuntimeError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if "image_provider" in updates:
                from studio.settings import normalize_image_provider

                new_image = normalize_image_provider(updates.get("image_provider"))
                old_image = normalize_image_provider(prev_image)
                if new_image != old_image:
                    # Settings drive jobs that are mid-pictures: cancel+resume with the new provider.
                    try:
                        from studio.pipeline import running_project_ids
                        from studio.projects import set_image_provider as write_job_image_provider

                        for pid in running_project_ids():
                            st = job_status(pid)
                            step = (st.get("step") or "")
                            if step in ("illustrations", "cover") or st.get("busy"):
                                write_job_image_provider(pid, new_image)
                    except Exception:
                        pass
        return settings_for_user(studio_auth.require_session(request))

    class StickmanHeadBody(BaseModel):
        color: str = ""
        from_color: str | None = None

    @app.get("/api/poses/head-color")
    def get_pose_head_color(request: Request):
        studio_auth.require_session(request)
        from studio.pose_colors import head_color_status

        return head_color_status()

    @app.get("/api/poses/preview")
    def get_pose_preview(request: Request):
        studio_auth.require_session(request)
        from studio.pose_colors import list_pose_files

        files = list_pose_files()
        path = next((p for p in files if p.name.lower() == "pose0001.png"), None)
        if path is None and files:
            path = files[0]
        if path is None or not path.is_file():
            raise HTTPException(404, "No pose preview found.")
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/poses/head-color")
    def post_pose_head_color(body: StickmanHeadBody, request: Request):
        studio_auth.require_admin(request)
        from studio.pose_colors import apply_head_color, head_color_status

        try:
            result = apply_head_color(body.color, from_color=body.from_color, save_setting=True)
            result["stickman_head"] = head_color_status()
            return result
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/poses/head-color/reset")
    def post_pose_head_color_reset(request: Request):
        studio_auth.require_admin(request)
        from studio.pose_colors import head_color_status, restore_stock_poses

        try:
            result = restore_stock_poses()
            result["stickman_head"] = head_color_status()
            return result
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/settings/comfyui-workflow")
    def get_comfyui_workflow(request: Request):
        studio_auth.require_admin(request)
        from studio.comfyui import list_workflows

        return list_workflows()

    @app.post("/api/settings/comfyui-workflow")
    async def upload_comfyui_workflow(request: Request, file: UploadFile = File(...)):
        studio_auth.require_admin(request)
        from studio.comfyui import save_workflow

        try:
            raw = await file.read()
            text = raw.decode("utf-8")
            return save_workflow(text, filename=file.filename or "comfyui_workflow.json")
        except Exception as exc:
            raise _err(exc)

    @app.delete("/api/settings/comfyui-workflow")
    def delete_comfyui_workflow(request: Request):
        studio_auth.require_admin(request)
        from studio.comfyui import delete_workflow

        return delete_workflow()

    @app.get("/api/comfyui")
    def get_comfyui(request: Request):
        studio_auth.require_admin(request)
        from studio.comfyui import comfyui_status

        return comfyui_status()

    @app.post("/api/comfyui/test")
    def test_comfyui(request: Request):
        studio_auth.require_admin(request)
        from studio.comfyui import comfyui_status

        return comfyui_status()

    @app.get("/api/youtube")
    def get_youtube():
        try:
            return yt.status()
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/youtube/connect")
    def get_youtube_connect():
        try:
            payload = yt.start_connect(open_browser=False)
            return RedirectResponse(payload["auth_url"], status_code=302)
        except Exception as exc:
            return HTMLResponse(yt._error_html(str(exc)), status_code=400)

    @app.post("/api/youtube/connect")
    def post_youtube_connect():
        try:
            return yt.start_connect(open_browser=True)
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/youtube/oauth/callback")
    def youtube_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        _payload, html, ok = yt.finish_oauth_from_request(str(request.url), code=code, state=state, error=error)
        return HTMLResponse(html, status_code=200 if ok else 400)

    @app.post("/api/youtube/oauth/code")
    def youtube_oauth_code(body: YoutubeCodeBody):
        try:
            return yt.finish_oauth_paste(body.url or body.code)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/youtube/disconnect")
    def youtube_disconnect(body: YoutubeDisconnectBody = YoutubeDisconnectBody()):
        return yt.disconnect(channel_id=body.channel_id or "")

    @app.put("/api/youtube/channel")
    def youtube_channel(body: YoutubeChannelBody):
        try:
            return yt.set_channel(body.channel_id, title=body.title)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/youtube")
    def youtube_upload(project_id: str, body: YoutubeUploadBody):
        try:
            return yt.upload_project_video(
                project_id,
                privacy_status=body.privacy_status or None,
                title=body.title or None,
                description=body.description or None,
                tags=body.tags if body.tags is not None else body.keywords,
                aspect=body.aspect or None,
                channel_id=body.channel_id or None,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/music")
    def get_music():
        return library_payload()

    @app.get("/api/backgrounds")
    def get_backgrounds():
        return backgrounds_catalog()

    @app.get("/api/backgrounds/{filename}")
    def get_background_file(filename: str):
        item = resolve_background(filename)
        if not item:
            raise HTTPException(404, "Background not found.")
        path = Path(item["path"])
        if not path.is_file():
            raise HTTPException(404, "Background file missing.")
        suffix = path.suffix.lower()
        media = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(suffix, "application/octet-stream")
        return FileResponse(path, media_type=media, filename=path.name)

    @app.post("/api/music")
    async def upload_music(file: UploadFile = File(...)):
        try:
            data = await file.read()
            track = save_music_upload(file.filename or "track.mp3", data)
            payload = library_payload()
            payload["track"] = track
            return payload
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/music/{track_id}")
    def get_music_file(track_id: str):
        track = get_track(track_id)
        if not track:
            raise HTTPException(404, "Music track not found.")
        path = Path(track["path"])
        if not path.is_file():
            raise HTTPException(404, "Music file missing.")
        suffix = path.suffix.lower()
        media = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".ogg": "audio/ogg",
            ".flac": "audio/flac",
        }.get(suffix, "application/octet-stream")
        return FileResponse(path, media_type=media, filename=track.get("name") or path.name)

    @app.delete("/api/music/{track_id}")
    def remove_music(track_id: str):
        try:
            delete_track(track_id)
            return library_payload()
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/prompts")
    def get_prompts(request: Request):
        user = studio_auth.require_session(request)
        is_admin = (user.get("role") or "") == "admin"
        token = set_prompt_user(user.get("id"))
        try:
            return catalog_payload(user.get("id"), is_admin=is_admin)
        finally:
            reset_prompt_user(token)

    @app.put("/api/prompts")
    def put_prompts(body: PromptsBody, request: Request):
        user = studio_auth.require_session(request)
        uid = user.get("id")
        is_admin = (user.get("role") or "") == "admin"
        token = set_prompt_user(uid)
        try:
            if body.prompts:
                return save_prompts(body.prompts, user_id=uid, is_admin=is_admin)
            key = (body.key or "").strip()
            text = body.text if body.text is not None else body.value
            if key and text is not None:
                return save_prompt(key, text, user_id=uid, is_admin=is_admin)
            raise RuntimeError("Provide prompts={key: text} or key + text.")
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise _err(exc)
        finally:
            reset_prompt_user(token)

    @app.post("/api/prompts/reset")
    def post_prompts_reset(request: Request, body: PromptResetBody = PromptResetBody()):
        user = studio_auth.require_session(request)
        uid = user.get("id")
        is_admin = (user.get("role") or "") == "admin"
        token = set_prompt_user(uid)
        try:
            return reset_prompt(body.key, user_id=uid, is_admin=is_admin)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise _err(exc)
        finally:
            reset_prompt_user(token)

    @app.get("/api/topics")
    def get_topics(request: Request, status: str | None = Query(None)):
        try:
            user = studio_auth.require_session(request)
            return list_topics(
                status,
                kick=False,
                owner_id=user.get("id"),
                is_admin=(user.get("role") or "") == "admin",
            )
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/topics/generate")
    def post_topics_generate(body: GenerateTopicsBody, request: Request):
        try:
            user = studio_auth.require_session(request)
            _gate_spend(
                "openai_topics",
                confirm_spend=body.confirm_spend,
                spend_confirm_id=body.spend_confirm_id,
                units=1,
                detail=f"Generate {body.count} topics via OpenAI",
            )
            return generate_topics(
                seed=body.seed,
                count=body.count,
                duration_min=body.duration_min,
                owner_id=user.get("id"),
            )
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/topics")
    def post_topic(body: CreateTopicBody, request: Request):
        try:
            user = studio_auth.require_session(request)
            return create_topic(
                title=body.title,
                angle=body.angle,
                duration_min=body.duration_min,
                owner_id=user.get("id"),
            )
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/topics/schedule")
    def post_topics_schedule(body: ScheduleTopicBody):
        try:
            return schedule_topic(
                topic_id=body.topic_id,
                title=body.title,
                duration_min=body.duration_min or None,
                angle=body.angle,
                run=body.run,
                scheduled_at=body.scheduled_at,
                run_now=body.run_now,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.patch("/api/topics/{topic_id}")
    def patch_topic(topic_id: str, body: TopicPatch):
        try:
            return update_topic(
                topic_id,
                title=body.title,
                angle=body.angle,
                duration_min=body.duration_min,
                scheduled_at=body.scheduled_at,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.delete("/api/topics/{topic_id}")
    def remove_topic(topic_id: str):
        try:
            return delete_topic(topic_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/topics/{topic_id}/schedule")
    def post_topic_schedule(topic_id: str, body: ScheduleTopicBody = ScheduleTopicBody()):
        try:
            return schedule_topic(
                topic_id=topic_id,
                title=body.title,
                duration_min=body.duration_min or None,
                angle=body.angle,
                run=body.run,
                scheduled_at=body.scheduled_at,
                run_now=body.run_now,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/topics/{topic_id}/unschedule")
    def post_topic_unschedule(topic_id: str):
        try:
            return unschedule_topic(topic_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/script-rules")
    def rules():
        return {
            "rules": get_prompt("script.rules"),
            "hook_outro": get_prompt("script.hook_outro"),
            "art_style_short": get_prompt("art.style_short"),
            "art_style_full": get_prompt("art.style_full"),
        }

    @app.get("/api/art-styles")
    def get_art_styles():
        from studio.art_style import list_art_styles

        return {"styles": list_art_styles(), "default": "classic"}

    @app.get("/api/voices")
    def voices(provider: str = ""):
        try:
            return list_voices(provider or None)
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/projects")
    def projects(request: Request):
        user = studio_auth.require_session(request)
        return list_library_items(
            owner_id=user.get("id"),
            is_admin=(user.get("role") or "") == "admin",
        )

    @app.get("/api/jobs")
    def jobs(request: Request):
        user = studio_auth.require_session(request)
        return list_library_items(
            owner_id=user.get("id"),
            is_admin=(user.get("role") or "") == "admin",
        )

    @app.post("/api/projects")
    def new_project(body: NewProject, request: Request):
        user = studio_auth.require_session(request)
        return attach_job(
            create_project(
                body.topic,
                body.duration_seconds,
                body.title or None,
                aspect=normalize_job_aspect(body.aspect),
                owner_id=user.get("id"),
            )
        )

    @app.patch("/api/projects/{project_id}")
    def patch_project(project_id: str, body: ProjectPatch):
        try:
            provider_switch = None
            if body.aspect:
                set_aspect(project_id, body.aspect)
            if body.image_provider:
                patched = set_image_provider(project_id, body.image_provider)
                provider_switch = patched.get("provider_switch")
            if body.video_layout:
                set_video_layout(project_id, body.video_layout)
            if body.character_size:
                set_character_size(project_id, body.character_size)
            if body.art_style:
                set_art_style(project_id, body.art_style)
            if body.include_bubblehead is not None:
                set_include_bubblehead(project_id, bool(body.include_bubblehead))
            if body.tts_provider or body.voice_provider or body.voice_id:
                set_project_voice(
                    project_id,
                    provider=body.tts_provider or body.voice_provider,
                    voice_id=body.voice_id,
                )
            if body.background_file:
                set_project_background(project_id, body.background_file)
            if body.music_mode == "shuffle" and not body.music_id:
                clear_project_music(project_id)
            elif body.music_id:
                set_project_music(project_id, body.music_id)
            if (
                body.youtube_auto_upload is not None
                or (body.youtube_privacy is not None and body.youtube_privacy != "")
                or body.youtube_channel_id is not None
            ):
                set_project_youtube(
                    project_id,
                    auto_upload=body.youtube_auto_upload,
                    privacy=body.youtube_privacy,
                    channel_id=body.youtube_channel_id,
                )
            if (
                body.youtube_description is not None
                or body.youtube_keywords is not None
                or body.youtube_hashtags is not None
            ):
                apply_youtube_publish_meta(
                    project_id,
                    description=body.youtube_description,
                    keywords=body.youtube_keywords,
                    hashtags=body.youtube_hashtags,
                )
            if body.generate_9x16 is not None:
                set_generate_9x16(project_id, bool(body.generate_9x16))
            if body.title:
                meta = load_meta(project_id)
                meta["title"] = body.title
                from studio.projects import save_meta
                save_meta(project_id, meta)
            out = attach_job(project_payload(project_id))
            if provider_switch:
                out["provider_switch"] = provider_switch
            return out
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        try:
            if not is_listed_project(project_id):
                raise FileNotFoundError(f"Unknown project: {project_id}")
            return attach_job(project_payload(project_id))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

    @app.delete("/api/projects/{project_id}")
    async def remove_project(
        project_id: str,
        request: Request,
        delete_files: bool = Query(True),
    ):
        flag = bool(delete_files)
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            try:
                data = await request.json()
                if isinstance(data, dict) and "delete_files" in data:
                    flag = bool(data["delete_files"])
            except Exception:
                pass
        try:
            return delete_project(project_id, delete_files=flag)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/rename")
    def rename_project_video(project_id: str, body: RenameVideoBody):
        try:
            return rename_video(
                project_id,
                body.title,
                topic=body.topic,
                rename_folder=bool(body.rename_folder),
                update_youtube=bool(body.update_youtube),
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/generate-script")
    def gen_script(project_id: str, body: GenerateScriptBody):
        try:
            _gate_spend(
                "openai_script",
                confirm_spend=body.confirm_spend,
                spend_confirm_id=body.spend_confirm_id,
                units=1,
                detail="Generate script via OpenAI",
                project_id=project_id,
            )
            return start_script_job(
                project_id,
                extra=body.extra,
                regenerate_pictures=body.regenerate_pictures,
                regenerate_audio=body.regenerate_audio,
                generate_9x16=body.generate_9x16,
                provider=body.provider or None,
                voice_id=body.voice_id or None,
            )
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/audio")
    def make_audio(project_id: str, body: AudioBody):
        try:
            from studio.settings import load_settings, normalize_tts_provider

            tts = normalize_tts_provider(body.provider or load_settings().get("tts_provider") or "openai")
            _gate_spend(
                "openai_tts",
                confirm_spend=body.confirm_spend,
                spend_confirm_id=body.spend_confirm_id,
                units=1,
                detail="OpenAI TTS narration",
                project_id=project_id,
                tts_provider=tts,
            )
            return start_audio_job(project_id, provider=body.provider or None, voice_id=body.voice_id or None)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/align")
    def align(project_id: str):
        try:
            return start_align_job(project_id)
        except Exception as exc:
            raise _err(exc)

    @app.put("/api/projects/{project_id}/script")
    def put_script(project_id: str, body: ScriptUpdate):
        try:
            errors = validate_tagged_script(body.script_tagged)
            if errors:
                raise RuntimeError("; ".join(errors))
            meta = load_meta(project_id)
            title = meta.get("title") or meta.get("topic") or project_id
            summary = body.summary or meta.get("summary") or meta.get("topic") or ""
            lines = parse_tagged_script(
                body.script_tagged,
                title=title,
                summary=summary,
                scene_hints=body.scenes,
                aspect=meta.get("aspect") or DEFAULT_ASPECT,
                layout=project_video_layout(project_id),
                image_provider=project_image_provider(project_id),
                include_bubblehead=project_include_bubblehead(meta),
            )
            save_lines(project_id, lines, summary=summary)
            if (
                body.youtube_description is not None
                or body.youtube_keywords is not None
                or body.youtube_hashtags is not None
            ):
                apply_youtube_publish_meta(
                    project_id,
                    description=body.youtube_description,
                    keywords=body.youtube_keywords,
                    hashtags=body.youtube_hashtags,
                )
            payload = attach_job(write_scripts(project_id, body.script_tagged, raw_from_tagged(body.script_tagged)))
            warnings = script_structure_warnings(body.script_tagged)
            if warnings:
                payload["script_warnings"] = warnings
                payload["message"] = "Saved with warnings: " + "; ".join(warnings)
            return payload
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/projects/{project_id}/illustrations")
    def get_illustrations(project_id: str, lite: bool = Query(False)):
        data = illustration_jobs(project_id)
        if not lite:
            return data
        return {
            "project_id": data.get("project_id"),
            "missing": data.get("missing"),
            "image_provider": data.get("image_provider"),
            "jobs": [
                {
                    "filename": job.get("filename"),
                    "role": job.get("role"),
                    "index": job.get("index"),
                    "ready": job.get("ready", job.get("has_image")),
                    "has_image": job.get("has_image"),
                    "url": job.get("url"),
                    "mtime": job.get("mtime") or 0,
                    "topic": job.get("topic"),
                    "line": job.get("line"),
                    "needs_regen": job.get("needs_regen"),
                    "waiting_for": job.get("waiting_for"),
                }
                for job in data.get("jobs") or []
            ],
            "pending_regen": data.get("pending_regen") or [],
        }

    @app.post("/api/projects/{project_id}/illustrations/regenerate")
    def regenerate_one_illustration(project_id: str, body: RegenerateIllustrationBody):
        try:
            _gate_spend(
                "flux_regen",
                confirm_spend=body.confirm_spend,
                spend_confirm_id=body.spend_confirm_id,
                units=1,
                detail=f"Regenerate illustration {body.filename or body.index}",
                project_id=project_id,
            )
            return start_regenerate_illustration_job(
                project_id,
                filename=body.filename,
                index=body.index,
                kind=body.kind,
            )
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/covers/regenerate")
    def regenerate_cover_route(project_id: str, body: RegenerateCoverBody | None = None):
        try:
            from studio.projects import cover_provider_is_agent, project_cover_provider

            payload = body or RegenerateCoverBody()
            backend = project_cover_provider(project_id)
            if not cover_provider_is_agent(project_id) and backend == "flux":
                _gate_spend(
                    "flux_regen",
                    confirm_spend=payload.confirm_spend,
                    spend_confirm_id=payload.spend_confirm_id,
                    units=1,
                    detail=f"Regenerate cover aspect={payload.aspect or 'project'}",
                    project_id=project_id,
                )
            return regenerate_cover(project_id, aspect=payload.aspect)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/covers/refresh")
    def refresh_covers_route(project_id: str, body: RefreshCoversBody):
        try:
            return refresh_covers(
                project_id,
                covers=body.covers or [],
                render=body.render,
                wait=body.wait,
                layout=body.layout or "",
                character_size=body.character_size or "",
                include_bubblehead=body.include_bubblehead,
            )
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/cover-provider")
    def set_project_cover_provider(project_id: str, body: CoverProviderBody):
        try:
            from studio.projects import set_cover_provider

            return set_cover_provider(project_id, body.cover_provider)
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/projects/{project_id}/covers/history")
    def get_cover_history(project_id: str, aspect: str | None = None):
        try:
            return cover_history_payload(project_id, aspect=aspect)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/covers/active")
    def set_cover_active(project_id: str, body: SetActiveCoverBody):
        try:
            if not (body.version_id or "").strip():
                raise RuntimeError("version_id is required (e.g. v001).")
            return set_active_cover(project_id, body.aspect or DEFAULT_ASPECT, body.version_id)
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/projects/{project_id}/covers/history/{aspect_slug}/{filename}")
    def cover_history_file(project_id: str, aspect_slug: str, filename: str):
        try:
            slug = (aspect_slug or "").strip().lower().replace("-", "x")
            aspect = "9:16" if "9x16" in slug or slug == "9:16" else "16:9"
            path = resolve_cover_history_path(project_id, aspect, filename)
            return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/illustrations/flux")
    @app.post("/api/projects/{project_id}/illustrations/generate")
    def gen_flux_illustrations(project_id: str, body: FluxConfirmBody | None = None):
        try:
            payload = body or FluxConfirmBody()
            _gate_spend(
                "flux_images",
                confirm_spend=payload.confirm_spend,
                spend_confirm_id=payload.spend_confirm_id,
                units=1,
                detail="Generate backgrounds with Flux (fal)",
                project_id=project_id,
            )
            return start_illustrations_job(project_id)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/illustrations")
    async def upload_illustration(
        project_id: str,
        file: UploadFile = File(...),
        filename: str = Form(""),
        kind: str = Form("billboard"),
        aspect: str = Form(""),
    ):
        try:
            data = await file.read()
            name = filename or file.filename or "illustration.png"
            return save_upload(
                project_id,
                name,
                data,
                kind=kind,
                aspect=aspect or None,
            )
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/illustrations/bulk")
    async def upload_illustrations_bulk(project_id: str, request: Request):
        """JSON bulk upload: {images:[{filename, image|image_url, kind?}]} — same as MCP save_illustration_images."""
        from studio.illustrations import save_illustration_images as bulk_save

        try:
            body = await request.json()
            images = body.get("images") if isinstance(body, dict) else body
            return bulk_save(project_id, images=images)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/narration")
    async def upload_narration(
        project_id: str,
        file: UploadFile = File(...),
        shorts: bool = Form(False),
    ):
        """Binary MP3 upload for tts_provider=external (same as MCP upload_narration_audio)."""
        from studio.tts_external import upload_narration_audio

        try:
            data = await file.read()
            return upload_narration_audio(
                project_id,
                data=data,
                format="mp3",
                shorts=bool(shorts),
            )
        except Exception as exc:
            msg = str(exc)
            if "too large" in msg.lower() or "upload_too_large" in msg.lower():
                raise HTTPException(
                    status_code=413,
                    detail={"detail": msg, "error_code": "upload_too_large"},
                ) from exc
            raise _err(exc)

    @app.get("/api/gentle")
    def get_gentle():
        return gentle_status()

    @app.post("/api/gentle/start")
    def start_gentle(request: Request):
        studio_auth.require_admin(request)
        try:
            return ensure_gentle()
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/gentle/stop")
    def stop_gentle_route(request: Request):
        studio_auth.require_admin(request)
        try:
            return stop_gentle()
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/tailscale")
    def get_tailscale(request: Request):
        studio_auth.require_admin(request)
        from studio.tailscale_tunnel import tailscale_status

        return tailscale_status(fresh=True)

    @app.post("/api/tailscale/start")
    def start_tailscale_route(request: Request):
        studio_auth.require_admin(request)
        from studio.settings import load_settings, save_settings
        from studio.tailscale_tunnel import start_tailscale_funnel

        try:
            settings = load_settings()
            port = int(settings.get("port") or 7878)
            save_settings({"public_tunnel": "tailscale"})
            return start_tailscale_funnel(port)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/tailscale/stop")
    def stop_tailscale_route(request: Request):
        studio_auth.require_admin(request)
        from studio.tailscale_tunnel import stop_tailscale_funnel

        try:
            return stop_tailscale_funnel()
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/ngrok")
    def get_ngrok(request: Request):
        studio_auth.require_admin(request)
        from studio.ngrok_tunnel import ngrok_status

        # Authenticated admin UI needs the tunnel basic-auth password to configure clients.
        return ngrok_status(reveal_password=True)

    @app.post("/api/ngrok/start")
    def start_ngrok_route(request: Request, body: NgrokBody | None = None):
        studio_auth.require_admin(request)
        from studio.ngrok_tunnel import (
            ensure_basic_auth_credentials,
            save_ngrok_settings,
            start_ngrok,
        )
        from studio.settings import save_settings

        try:
            save_settings({"public_tunnel": "ngrok"})
            payload = body or NgrokBody()
            if (
                payload.url is not None
                or payload.local_port is not None
                or payload.autostart is not None
                or payload.basic_auth_user is not None
                or payload.basic_auth_password is not None
            ):
                save_ngrok_settings(
                    url=payload.url,
                    local_port=payload.local_port,
                    autostart=payload.autostart,
                    basic_auth_user=payload.basic_auth_user,
                    basic_auth_password=payload.basic_auth_password,
                )
            if payload.rotate_password:
                ensure_basic_auth_credentials(rotate_password=True)
            return start_ngrok()
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/ngrok/stop")
    def stop_ngrok_route(request: Request):
        studio_auth.require_admin(request)
        from studio.ngrok_tunnel import stop_owned_ngrok

        try:
            return stop_owned_ngrok()
        except Exception as exc:
            raise _err(exc)

    @app.put("/api/ngrok")
    def put_ngrok(body: NgrokBody, request: Request):
        studio_auth.require_admin(request)
        from studio.ngrok_tunnel import (
            ensure_basic_auth_credentials,
            ngrok_status,
            save_ngrok_settings,
        )

        try:
            save_ngrok_settings(
                url=body.url,
                local_port=body.local_port,
                autostart=body.autostart,
                basic_auth_user=body.basic_auth_user,
                basic_auth_password=body.basic_auth_password,
            )
            if body.rotate_password:
                ensure_basic_auth_credentials(rotate_password=True)
            return ngrok_status(reveal_password=True)
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/render")
    def render(project_id: str, body: RenderBody):
        try:
            return start_render_thread(
                project_id,
                use_billboards=body.use_billboards,
                keep_frames=body.keep_frames,
                aspect=body.aspect,
                layout=body.layout,
                character_size=body.character_size,
                include_bubblehead=body.include_bubblehead,
                shuffle_music=body.shuffle_music,
            )
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/resume")
    def resume(project_id: str, request: Request):
        try:
            from studio.tenant import require_project_access
            from studio.job_queue import request_run

            user, _meta = require_project_access(request, project_id)
            return request_run(
                project_id,
                kind="resume",
                owner_id=str(user.get("id") or "") or None,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/start")
    def start(project_id: str, request: Request):
        try:
            from studio.tenant import require_project_access
            from studio.job_queue import request_run

            user, _meta = require_project_access(request, project_id)
            return request_run(
                project_id,
                kind="start",
                owner_id=str(user.get("id") or "") or None,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/pause")
    def pause(project_id: str):
        try:
            if not is_listed_project(project_id):
                raise FileNotFoundError(f"Unknown project: {project_id}")
            return pause_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/stop")
    def stop(project_id: str):
        try:
            if not is_listed_project(project_id):
                raise FileNotFoundError(f"Unknown project: {project_id}")
            return stop_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/jobs/{project_id}/resume")
    def resume_job(project_id: str):
        return resume(project_id)

    @app.post("/api/jobs/{project_id}/start")
    def start_job(project_id: str):
        return start(project_id)

    @app.post("/api/jobs/{project_id}/pause")
    def pause_job(project_id: str):
        return pause(project_id)

    @app.post("/api/jobs/{project_id}/stop")
    def stop_job(project_id: str):
        return stop(project_id)

    @app.post("/api/projects/{project_id}/shuffle-music")
    def shuffle_music(project_id: str):
        try:
            return attach_job(shuffle_project_music(project_id))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.post("/api/projects/{project_id}/set-music")
    def set_music(project_id: str, body: SetMusicBody):
        try:
            mode = str(body.mode or "").strip().lower()
            track_id = str(body.music_id or body.track_id or "").strip()
            if mode == "shuffle" and not track_id:
                return attach_job(clear_project_music(project_id))
            if not track_id:
                raise ValueError("Pass music_id, or mode=shuffle.")
            return attach_job(set_project_music(project_id, track_id))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        except Exception as exc:
            raise _err(exc)

    @app.get("/api/projects/{project_id}/job")
    def job(project_id: str):
        try:
            return job_status(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/projects/{project_id}/video")
    def video(project_id: str, aspect: str | None = None):
        payload = project_payload(project_id)
        if aspect:
            path = final_video_path(project_id, normalize_aspect(aspect))
        else:
            path = Path(payload["paths"]["video"])
            if not path.is_file():
                path = last_video_path(project_id)
        if not path.is_file():
            raise HTTPException(404, "Video not rendered yet.")
        return FileResponse(path, media_type="video/mp4", filename=path.name)

    @app.get("/api/projects/{project_id}/thumbnail")
    def thumbnail(project_id: str):
        try:
            path = ensure_thumbnail(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        if not path.is_file():
            raise HTTPException(404, "No thumbnail.")
        suffix = path.suffix.lower()
        media = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
        return FileResponse(path, media_type=media)

    @app.get("/api/projects/{project_id}/cover")
    def cover_image(project_id: str, aspect: str | None = None, filename: str | None = None):
        try:
            if filename:
                name = Path(filename).name
                if not name.lower().startswith("script_cover") or not name.lower().endswith(".png"):
                    raise HTTPException(400, "Not a cover filename.")
                path = Path(project_payload(project_id)["paths"]["folder"]) / name
            elif aspect:
                path = cover_path(project_id, normalize_aspect(aspect))
            else:
                path = cover_path(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))
        if not path.is_file():
            raise HTTPException(404, "Cover image not generated yet.")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})

    @app.get("/api/projects/{project_id}/billboards/{filename}")
    def billboard(project_id: str, filename: str, aspect: str | None = None):
        from studio.projects import billboards_dir

        folder = billboards_dir(project_id, aspect)
        path = folder / Path(filename).name
        if not path.is_file() and aspect:
            # Fallback to the other folder if aspect was wrong.
            alt = billboards_dir(project_id, "16:9" if normalize_aspect(aspect) == "9:16" else "9:16")
            path = alt / Path(filename).name
        if not path.is_file():
            raise HTTPException(404, "Illustration not found.")
        return FileResponse(path, headers={"Cache-Control": "no-cache"})

    @app.get("/api/projects/{project_id}/audio-file")
    def audio_file(project_id: str):
        payload = project_payload(project_id)
        path = Path(payload["paths"]["audio"])
        if not path.is_file():
            raise HTTPException(404, "Audio not generated yet.")
        return FileResponse(path, media_type="audio/wav", filename=path.name)

    return app


def run() -> None:
    import json
    import os

    import uvicorn

    from studio.ffmpeg_bin import ensure_ffmpeg_on_path
    from studio.paths import USER_DATA, ensure_dirs
    from studio.restart import clear_stale_gpu_lock, wait_for_port_free

    try:
        ensure_ffmpeg_on_path()
    except Exception:
        pass

    settings = load_settings()
    host = (os.environ.get("BUBBLEPOD_HOST") or os.environ.get("LAZYKH_HOST") or os.environ.get("HOST") or "").strip() or (
        settings.get("host") or "127.0.0.1"
    )
    try:
        port = int(
            os.environ.get("BUBBLEPOD_PORT")
            or os.environ.get("LAZYKH_PORT")
            or os.environ.get("PORT")
            or 0
        ) or int(settings.get("port") or 7878)
    except (TypeError, ValueError):
        port = int(settings.get("port") or 7878)
    try:
        ensure_dirs()
        (USER_DATA / "listen_port.json").write_text(
            json.dumps(
                {
                    "host": host if host not in ("0.0.0.0", "::", "[::]") else "127.0.0.1",
                    "port": port,
                    "url": f"http://{host if host not in ('0.0.0.0', '::', '[::]') else '127.0.0.1'}:{port}/",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    if os.environ.get("BUBBLEPOD_WAIT_FOR_PORT"):
        wait_for_port_free(host, port, timeout=45)
    try:
        clear_stale_gpu_lock()
    except Exception:
        pass
    uvicorn.run(
        "studio.web:create_app",
        factory=True,
        host=host,
        port=port,
        reload=False,
        # Keep MCP streamable-HTTP sessions alive through nginx (proxy_read 600s).
        timeout_keep_alive=75,
        timeout_graceful_shutdown=30,
        limit_concurrency=100,
        h11_max_incomplete_event_size=64 * 1024 * 1024,
    )
