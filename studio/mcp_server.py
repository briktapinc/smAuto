from __future__ import annotations

from mcp.types import ToolAnnotations

from studio.aspect import DEFAULT_ASPECT, aspects_to_render, normalize_aspect, normalize_job_aspect
from studio.backgrounds import catalog_payload as backgrounds_catalog
from studio.gentle import align_audio, ensure_gentle as boot_gentle, gentle_status, stop_gentle as halt_gentle
from studio.illustrations import (
    cover_history_payload,
    generate_cover_image,
    illustration_jobs,
    list_cover_versions as list_cover_versions_impl,
    refresh_covers as refresh_covers_impl,
    regenerate_cover as regenerate_cover_impl,
    resolve_project_image_aspect,
    save_illustration,
    set_active_cover as set_active_cover_impl,
)
from studio.music import clear_project_music, library_payload, set_project_music, shuffle_project_music
from studio.pipeline import (
    generate_audio_then_align,
    job_status,
    list_library_items,
    pause_project,
    render_video,
    resume_project,
    start_illustrations_job,
    start_project,
    start_regenerate_illustration_job,
    start_render_thread,
    start_script_downstream_job,
    stop_project,
)
from studio.projects import (
    apply_youtube_publish_meta,
    create_project,
    list_projects,
    load_meta,
    project_art_style,
    project_image_provider,
    project_payload,
    project_video_layout,
    save_lines,
    set_aspect,
    set_art_style as write_project_art_style,
    set_character_size as write_project_character_size,
    set_include_bubblehead as write_project_include_bubblehead,
    set_image_provider as write_project_image_provider,
    set_project_background as write_project_background,
    set_project_voice as write_project_voice,
    set_project_youtube as write_project_youtube,
    set_video_layout as write_project_video_layout,
    write_scripts,
    delete_project as remove_studio_project,
    rename_video as rename_studio_video,
)
from studio.prompts import (
    PROMPT_KEYS,
    catalog_payload,
    get_prompt,
    reset_prompt as restore_prompts,
    save_prompt,
    save_prompts as write_prompts,
)
from studio.script_rules import with_hook_outro_requirement, with_playbook_runtime_notes
from studio.scriptgen import apply_generated_script, generate_script
from studio.settings import (
    FLUX_MODEL,
    is_placeholder_secret,
    load_settings,
    normalize_character_size,
    normalize_text_provider,
    normalize_tts_provider,
    normalize_video_layout,
    public_settings,
    save_settings,
    scrub_secret_updates,
)
from studio.topics import (
    create_topic as add_topic,
    delete_topic as remove_topic,
    generate_topics as invent_topics,
    hands_off_status,
    list_topics as topics_catalog,
    schedule_topic as enqueue_topic,
    start_topic_pipeline as launch_topic_pipeline,
    unschedule_topic as clear_topic_schedule,
    update_topic as patch_topic,
)
from studio.textgen import native_script_handoff
from studio.tts import list_voices
from studio.utils_script import parse_tagged_script, raw_from_tagged, script_structure_warnings, validate_tagged_script
from studio import youtube as yt

MCP_BUILD = "2026-09-18-agent-automation"

# Keep in sync with every @mcp.tool in build_mcp (stdio and FastMCP HTTP /mcp).
MCP_TOOL_NAMES = (
    "search",
    "fetch",
    "get_file",
    "get_script_rules",
    "get_chatgpt_playbook",
    "get_studio_settings",
    "get_health",
    "get_gpu_lock",
    "restart_api",
    "ngrok_status",
    "start_ngrok",
    "stop_ngrok",
    "get_text_provider",
    "set_text_provider",
    "list_prompts",
    "get_prompts",
    "update_prompt",
    "save_prompts",
    "reset_prompt",
    "request_spend_confirm",
    "get_spend_status",
    "get_audit_log",
    "generate_topics",
    "create_topic",
    "list_topics",
    "update_topic",
    "schedule_topic",
    "unschedule_topic",
    "start_topic_pipeline",
    "hands_off",
    "set_hands_off",
    "delete_topic",
    "create_video_project",
    "set_video_aspect",
    "list_video_projects",
    "list_library",
    "delete_project",
    "rename_video",
    "get_project",
    "generate_script_via_api",
    "save_script",
    "list_illustration_jobs",
    "get_image_provider",
    "set_image_provider",
    "get_comfyui_status",
    "save_comfyui_workflow",
    "delete_comfyui_workflow",
    "get_video_layout",
    "set_video_layout",
    "set_art_style",
    "list_art_styles",
    "set_character_size",
    "set_include_bubblehead",
    "get_stickman_head_color",
    "set_stickman_head_color",
    "reset_stickman_head_color",
    "set_project_voice",
    "generate_illustrations_with_flux",
    "generate_illustrations",
    "generate_cover",
    "regenerate_cover",
    "refresh_covers",
    "list_cover_versions",
    "set_active_cover",
    "regenerate_illustration",
    "save_illustration_image",
    "get_cover_provider",
    "set_cover_provider",
    "list_tts_voices",
    "generate_speech",
    "ensure_gentle",
    "ensure_gentle_docker",
    "start_gentle",
    "stop_gentle",
    "get_gentle_status",
    "align_phonemes",
    "render_final_video",
    "get_render_status",
    "start_job",
    "resume_job",
    "stop_job",
    "pause_job",
    "list_music",
    "list_backgrounds",
    "set_project_background",
    "shuffle_job_music",
    "set_job_music",
    "youtube_status",
    "list_youtube_channels",
    "youtube_connect",
    "youtube_finish_oauth",
    "youtube_disconnect",
    "set_youtube_channel",
    "set_project_youtube",
    "upload_to_youtube",
    "update_studio_settings",
)

try:
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware
except ImportError:  # pragma: no cover
    FastMCP = None
    Middleware = None  # type: ignore


def handshake_instructions() -> str:
    """Live playbook for FastMCP initialize. Re-reads prompts.json."""
    playbook = with_playbook_runtime_notes(get_prompt("mcp.chatgpt_playbook"))
    tools = ", ".join(MCP_TOOL_NAMES)
    return (
        f"Stickman Automation MCP build {MCP_BUILD}. "
        "stdio: python -m studio.mcp_server (Codex mcp_servers.lazykh; Claude Desktop mcpServers.lazykh). "
        "HTTP: FastMCP streamable POST /mcp on Studio — same tools. "
        "HTTP /mcp AUTH (any one): (1) ngrok HTTP Basic — Settings → Ngrok username/password; "
        "send Basic only (not Basic+Bearer together). Preferred for remote agents over the tunnel. "
        "(2) MCP PIN via header X-MCP-Pin, query ?mcp_pin=, or Authorization: Bearer <pin>. "
        "(3) Studio JWT (Bearer/cookie) for per-member access. "
        "ChatGPT tip: public https://…/mcp with Basic, or …/mcp?mcp_pin=YOUR_PIN. "
        "ChatGPT Desktop caches schemas: full-quit the app, reopen, /mcp, NEW thread. "
        "Claude Desktop / Claude Code use this same stdio server (local; not HTTP-PIN-gated). "
        "EVERY tagged script MUST open with a topic HOOK (1-3 spoken lines on THIS topic, "
        "not a greeting — that hook is also the 9:16 short) and close with a subscribe OUTRO "
        "that actually says subscribe (16:9 only). generate_9x16=true by default builds "
        "script_9x16 (hook + script.shorts_cta), 1080x1920 short art, and script_9x16 wav/align. "
        "Call get_text_provider. If text_provider is chatgpt or claude: YOU write the tagged "
        "script (hook + subscribe outro, emotion tags) then save_script. Do NOT call "
        "generate_script_via_api (that bills OpenAI). If openai or lmstudio, call "
        "generate_script_via_api (openai bills cloud; lmstudio uses the local OpenAI-compatible "
        "server — no OpenAI cloud, do not write the body yourself). "
        "Topics: openai and lmstudio use generate_topics; chatgpt/claude invent titles then create_topic / "
        "schedule_topic (do not call generate_topics — that bills OpenAI). "
        "generate_script_via_api defaults regenerate_pictures=true and regenerate_audio=true: "
        "new script drops stale Gentle json/frames/mp4; pictures overwrites every slot; "
        "audio writes a new wav then aligns phonemes immediately. generate_speech also aligns "
        "after TTS (start_gentle / ensure_gentle Docker or local, then align_phonemes; stop_gentle to halt). Never run scheduler "
        "without matching script.json. "
        "UNSUPERVISED WALK-AWAY: set_hands_off(true) (or update_studio_settings(hands_off=true)) "
        "then walk away. Studio's ~30s loop generates topics when drafts+queued < hands_off_min_queue (default 5; openai/lmstudio only), "
        "auto-schedules drafts (interval hours, 0 = due now FIFO), runs the full pipeline, and uploads "
        "each hands-off job to YouTube as private (per-job override; Settings defaults unchanged). "
        "If YouTube is not connected, still generate+schedule+render and mark upload pending. "
        "Headless providers: text openai|lmstudio, images flux|comfyui (chatgpt pictures cannot run headless). "
        "restart_api() from stdio MCP restarts http://127.0.0.1:7878 (python run_studio.py) without killing "
        "Gentle 8766, VoiceSync 8765, or the Electron window. "
        "Otherwise: set text_provider openai|lmstudio, image_provider flux|comfyui "
        "(chatgpt pictures cannot run headless — schedule_topic falls back to flux if fal_key exists, else errors), "
        "tts openai|elevenlabs|local. youtube_connect once if uploading. generate_topics then "
        "schedule_topic(topic_id, scheduled_at='YYYY-MM-DDTHH:MM') — naive times are this PC's local zone, "
        "stored as UTC ISO. With hands_off on, Studio's ~30s due-picker starts queued topics with scheduled_at <= now, "
        "FIFO by scheduled_at, one pipeline at a time (script via API → pictures → audio → Gentle → "
        "render including both → optional YouTube). Without hands_off, due topics stay queued until Hands-off or Run now. "
        "GPU lock: ComfyUI, local Chatterbox TTS, Flux batches, "
        "and pipeline image/audio/render run one at a time — never fire illustrations + local TTS + "
        "another job in parallel; wait or call get_gpu_lock. run_now=true starts immediately. Pause holds the queue. "
        "auto_scheduler defaults ON but only auto-starts while hands_off is also on. If text_provider is chatgpt/claude, write save_script(job_id) in the "
        "SAME turn after schedule_topic returns job_id, then walk away. "
        "list_topics shows due times. hands_off() returns readiness + recipe. start_topic_pipeline is "
        "schedule_topic with run_now default true. "
        "Call get_chatgpt_playbook and get_script_rules for LIVE production text "
        "(they re-read user_data/prompts.json). Call get_studio_settings for "
        "text_provider/script_provider (openai|chatgpt|claude|lmstudio), "
        "image_provider, video_layout, character_size, default_aspect, music_volume_pct, "
        "tts_provider (openai|elevenlabs|local; local is Resemble Chatterbox, alias resemble), "
        "voices, fal_key_set, "
        "youtube_auto_upload/privacy/connected, gpu_lock {busy, holder, waiters}, mcp_build, and mcp_tools. "
        "SPEND GUARD: OpenAI cloud (generate_script_via_api, generate_topics, generate_speech) and "
        "Flux/fal (generate_illustrations_with_flux / generate_illustrations / generate_cover / "
        "regenerate_illustration) require confirm_spend=true OR spend_confirm_id from "
        "request_spend_confirm first. LM Studio, ChatGPT/Claude native, ComfyUI, and local TTS "
        "skip this. get_spend_status / get_audit_log for counters and the JSONL audit trail. "
        "Jobs: list_library / list_video_projects, create_video_project, get_project, "
        "get_file(project_id, kind=video|audio|cover|script_tagged|illustration|…) or "
        "get_file(path='projects/…') returns text/base64 (max 15MB) or a short-lived "
        "/api/mcp/download/{token} URL for larger binaries — secrets (auth.json, settings.json, "
        "tokens) are refused. "
        "delete_project(delete_files=False), rename_video(project_id, title, update_youtube=true), "
        "start_job, pause_job, stop_job, resume_job. "
        "Topics: generate_topics when text_provider is openai or lmstudio; otherwise invent topics "
        "yourself then create_topic / schedule_topic. list_topics(status) includes scheduled_at, "
        "scheduled_at_local, due, next_due_at. "
        "schedule_topic(topic_id, scheduled_at=, run_now=) or schedule_topic(title, duration_min, angle, "
        "scheduled_at) creates a job and queues the full unsupervised pipeline. run_now=true (or run='now') "
        "sets scheduled_at=now. If a pipeline worker is already live or paused, scheduled topics wait. "
        "delete_topic(topic_id). unschedule_topic(topic_id) clears scheduled_at and returns queued to draft. "
        "update_topic(topic_id, title?, angle?, duration_min?, scheduled_at?). "
        "hands_off() / set_hands_off(enabled, interval_hours?, min_queue?) / start_topic_pipeline(). "
        "get_health() matches GET /api/health (mcp_build, gpu_lock, hands_off, gentle). "
        "restart_api() restarts the 7878 Studio API from stdio (independent of the old uvicorn). "
        "APP IMAGE PROMPTS ONLY: never invent an art style. Flux/ComfyUI inject live "
        "images.flux_instructions + art.* (lines) and images.cover_intro (covers); "
        "ChatGPT must paste list_illustration_jobs "
        "jobs[].prompt / instructions verbatim, then save_illustration_image so Pictures "
        "sees files under user_data/projects/{id}/. "
        "COVER TITLE CARD: kind=cover / script_cover_*.png is a studio-room still — yellow "
        "Bubblehead OUTSIDE left (or lower on 9:16), large TV/billboard with the exact video "
        "title as bold on-screen headline text + topic art on the screen. Match selected "
        "background_file room. regenerate_cover / list_cover_versions / set_active_cover "
        "keep prior covers under covers_history/{16x9|9x16}/vNNN.png. "
        "SHORT BILLBOARD NAMES: line art files are b001.png, b002.png, … (1-based script "
        "order) — always use list_illustration_jobs jobs[].filename / save_path; never save "
        "ChatGPT's long image title as the disk name (Windows MAX_PATH). "
        "Cover + illustrations MUST follow Settings image_provider (and per-job override) AND "
        "this project's aspect from Studio (list_illustration_jobs.aspect / project_aspect — "
        "16:9, 9:16, or both; never assume 16:9): "
        "flux uses fal (SHORT app prompts); comfyui uses local ComfyUI (uploaded API JSON, "
        "1920x1080 for 16:9 / 1080x1920 for 9:16, sequential GPU); chatgpt uses native "
        "in-chat images and "
        "save_illustration_image (filename='script_cover_16x9.png' or "
        "'script_cover_9x16.png', kind='cover' for the 5s topic card with title lettering). "
        "ChatGPT image UI: pick 16:9 landscape (1920x1080) or 9:16 portrait (1080x1920) — never square. "
        "Each aspect has its own cover and mp4 (script_cover_16x9.png / script_cover_9x16.png, "
        "script_final_16x9.mp4 / script_final_9x16.mp4). Rendering 9:16 does not delete 16:9. "
        "Do not stretch a 16:9 cover into 9:16 — regenerate that cover at 1080x1920. "
        "Cover always matches the aspect being rendered. Billboard line art is always 16:9 1920x1080 "
        "even on a 9:16 video (only the cover is 9:16 1080x1920 on portrait billboard jobs). "
        "list_illustration_jobs.jobs[].width/height/aspect/image_size are the exact pixels. "
        "To redo one picture, call regenerate_illustration(project_id, filename=...) — Flux "
        "queues fal for that file; ComfyUI queues the local workflow; ChatGPT marks needs_regen and returns prompt + "
        "save_illustration_image args. regenerate_cover(project_id, aspect=) is the cover-only "
        "path (same image_provider; archives prior cover). "
        "LINE PNGs: NO TV/monitor/bezel/stand/screen-in-screen and NO explainer character — "
        "the compositor TV overlay and Bubblehead are applied later for lip-sync frames. "
        "Covers ARE the full studio scene (Bubblehead + TV) because the title-card still has "
        "no character overlay. "
        "Studio room (clock/wall/floor) is list_backgrounds / set_project_background "
        "(stored as background_file) and is injected into cover prompts. "
        "Render: 5s cover + billboard TV-frame composite + music loop. "
        "set_video_layout cover|billboard. set_art_style classic|pixar_3d|cinematic|claymation|… (per-job; classic = current default). "
        "set_character_size large|medium|small (feet stay bottom-anchored). "
        "set_include_bubblehead true|false (per-job; false omits stick-figure narrator on lip-sync "
        "frames, keeps cover/line art/audio). "
        "get_stickman_head_color / set_stickman_head_color(#RRGGBB) / reset_stickman_head_color — "
        "recolors every pose*.png head fill + auto shadow (H−10.4°, L−8.4); studio-wide, free. "
        "list_music / shuffle_job_music / music_volume_pct. "
        "Prompts: list_prompts / get_prompts / update_prompt / save_prompts / reset_prompt "
        "(live user_data/prompts.json, including topics.generate). "
        "FAL_KEY is optional and only required for flux. ComfyUI needs an uploaded API JSON "
        "plus comfyui_url (default http://127.0.0.1:8188). get_studio_settings shows "
        "comfyui_workflow_loaded. delete_comfyui_workflow removes the uploaded JSON. "
        "If image_provider is comfyui, call generate_illustrations "
        "or generate_illustrations_with_flux — Studio POSTs to ComfyUI and does not fal. "
        "YouTube: youtube_connect returns a URL to open in the SYSTEM browser (no popup). "
        "youtube_status, list_youtube_channels, set_youtube_channel, youtube_disconnect, "
        "youtube_finish_oauth, set_project_youtube, "
        "upload_to_youtube(project_id, privacy_status=private|unlisted|public, aspect?, "
        "title?, description?, tags?) — uses stored youtube_description / youtube_keywords / "
        "youtube_hashtags from meta when description/tags omitted. "
        "rename_video(project_id, title, update_youtube=true, rename_folder=false) updates "
        "Studio meta title and, when uploaded, the live YouTube title. "
        "save_script also accepts youtube_description, youtube_keywords, youtube_hashtags. "
        "start_job(project_id) runs the pipeline from empty or the first incomplete step. "
        "stop_job / pause_job halt cooperatively at the next pipeline step boundary "
        "(script, images, audio, align, render, youtube) and set running=false. "
        "resume_job continues from the last successful artifact and never starts a duplicate "
        f"thread (attaches if the worker is still winding down). Tools: {tools}.\n\n"
        + playbook
    )


def studio_settings_payload() -> dict:
    data = public_settings()
    data["mcp_build"] = MCP_BUILD
    data["mcp_tools"] = list(MCP_TOOL_NAMES)
    data["prompt_keys"] = sorted(PROMPT_KEYS)
    from studio.gpu_lock import gpu_lock_public

    data["gpu_lock"] = gpu_lock_public()
    data["stdio"] = "python -m studio.mcp_server"
    data["http_mcp"] = "/mcp"
    return data


def studio_health_payload() -> dict:
    """Same shape as GET /api/health (no HTTP round-trip)."""
    from studio.gpu_lock import gpu_lock_public
    from studio.paths import asset_warnings
    from studio.topics import SCHEDULER_INTERVAL_SEC

    pub = public_settings()
    return {
        "ok": True,
        "gentle": gentle_status(),
        "mcp": "/mcp",
        "mcp_build": MCP_BUILD,
        "auto_scheduler": pub.get("auto_scheduler", True),
        "hands_off": pub.get("hands_off", False),
        "scheduler_interval_sec": SCHEDULER_INTERVAL_SEC,
        "gpu_lock": gpu_lock_public(),
        "warnings": asset_warnings(),
    }


class _LivePlaybookMiddleware(Middleware if Middleware is not None else object):  # type: ignore[misc]
    """Refresh handshake instructions from prompts.json on every MCP initialize."""

    def __init__(self, holder: dict):
        self._holder = holder

    async def on_initialize(self, context, call_next):  # pragma: no cover - exercised by MCP clients
        text = handshake_instructions()
        server = self._holder.get("mcp")
        if server is not None:
            server.instructions = text
        result = await call_next(context)
        if result is None:
            return result
        if isinstance(result, dict):
            result["instructions"] = text
            return result
        try:
            result.instructions = text
        except Exception:
            pass
        return result


class _AuditToolMiddleware(Middleware if Middleware is not None else object):  # type: ignore[misc]
    """Log every MCP tools/call to user_data/audit.log; enforce membership on gated tools."""

    async def on_call_tool(self, context, call_next):  # pragma: no cover
        from studio.audit import write_entry
        from studio.auth import enforce_mcp_tool_membership

        name = ""
        args = None
        try:
            msg = getattr(context, "message", None)
            params = getattr(msg, "params", None)
            name = str(getattr(params, "name", None) or getattr(msg, "name", None) or "unknown")
            args = getattr(params, "arguments", None)
        except Exception:
            name = "unknown"
        err = ""
        ok = True
        try:
            request = None
            try:
                from fastmcp.server.dependencies import get_http_request

                request = get_http_request()
            except Exception:
                request = None
            enforce_mcp_tool_membership(name, request)
            result = await call_next(context)
            return result
        except Exception as exc:
            ok = False
            err = str(exc)
            raise
        finally:
            try:
                write_entry(
                    action=name or "unknown",
                    source="mcp",
                    ip="",
                    username="",
                    auth_method="mcp",
                    success=ok,
                    error=err,
                    args=args if isinstance(args, dict) else {"_": args},
                )
            except Exception:
                pass


def _mcp_spend(
    action: str,
    *,
    confirm_spend: bool = False,
    spend_confirm_id: str = "",
    units: int = 1,
    detail: str = "",
    project_id: str = "",
    tts_provider: str = "",
) -> None:
    from studio.spend_guard import require_spend

    ctx: dict = {}
    if project_id:
        ctx["project_id"] = project_id
    if tts_provider:
        ctx["tts_provider"] = tts_provider
    require_spend(
        action,
        confirm_spend=confirm_spend,
        spend_confirm_id=spend_confirm_id or None,
        units=units,
        source="mcp",
        detail=detail or action,
        context=ctx,
    )


def build_mcp() -> "FastMCP":
    if FastMCP is None:
        raise RuntimeError("Install fastmcp to expose the ChatGPT MCP server.")

    holder: dict = {"mcp": None}
    middleware = []
    if Middleware is not None:
        middleware = [_LivePlaybookMiddleware(holder), _AuditToolMiddleware()]
    mcp = FastMCP(
        "Stickman Automation",
        instructions=handshake_instructions(),
        middleware=middleware,
    )
    holder["mcp"] = mcp

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def search(query: str) -> dict:
        """Search Stickman Automation projects, scripts, and illustration jobs."""
        q = (query or "").strip().lower()
        ids = []
        try:
            from studio.topics import list_topics as topics_catalog

            for topic in topics_catalog(kick=False).get("topics") or []:
                blob = f"{topic.get('id')} {topic.get('title')} {topic.get('angle')} topic".lower()
                if not q or q in blob:
                    ids.append(f"topic/{topic['id']}")
        except Exception:
            pass
        for item in list_projects():
            blob = f"{item.get('id')} {item.get('title')} {item.get('topic')}".lower()
            if not q or q in blob:
                ids.append(item["id"])
            for job in illustration_jobs(item["id"]).get("jobs", []):
                jid = f"{item['id']}/image/{job['filename']}"
                hay = f"{job.get('topic')} {job.get('line')} {job.get('prompt')}".lower()
                if not q or q in hay:
                    ids.append(jid)
        return {"ids": ids[:50]}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def fetch(id: str) -> dict:
        """Fetch a project or illustration prompt by id from search()."""
        if id.startswith("topic/"):
            from studio.topics import list_topics as topics_catalog

            tid = id.split("/", 1)[1]
            for topic in topics_catalog(kick=False).get("topics") or []:
                if topic.get("id") == tid:
                    return {
                        "id": id,
                        "title": topic.get("title") or tid,
                        "content": f"{topic.get('title')}\n{topic.get('angle') or ''}".strip(),
                        "metadata": topic,
                    }
            raise RuntimeError(f"Unknown topic: {tid}")
        if "/image/" in id:
            project_id, _, filename = id.partition("/image/")
            for job in illustration_jobs(project_id)["jobs"]:
                if job["filename"] == filename or job["filename"] == filename + ".png":
                    return {
                        "id": id,
                        "title": job["filename"],
                        "content": (
                            f"{job.get('generate_at') or ''}\n"
                            f"aspect={job.get('aspect')} image_size={job.get('image_size')} "
                            f"width={job.get('width')} height={job.get('height')} "
                            f"preset={job.get('chatgpt_preset')}\n\n"
                            f"{job['prompt']}"
                        ).strip(),
                        "metadata": job,
                    }
            raise RuntimeError(f"Unknown illustration id: {id}")
        payload = project_payload(id)
        return {
            "id": id,
            "title": payload.get("title") or id,
            "content": payload.get("script_tagged") or payload.get("topic") or "",
            "metadata": payload,
        }

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_file(
        project_id: str = "",
        kind: str = "",
        path: str = "",
        filename: str = "",
        index: int = -999,
        aspect: str = "",
        music_id: str = "",
        mode: str = "auto",
    ) -> dict:
        """Retrieve a Studio artifact for ChatGPT/Claude MCP.

        Prefer project_id + kind, or a relative path under allowed roots only
        (projects/, music/, user_data/, backgrounds/). Never absolute OS paths.

        kind examples: video, video_16x9, video_9x16, audio, audio_9x16, cover,
        cover_16x9, cover_9x16, script_tagged, script_raw, script_9x16, alignment,
        illustration (pass filename= or index= from list_illustration_jobs;
        index -1=current cover, -2=9:16 cover), lines, meta, thumbnail, music
        (filename or music_id), background (filename), log (audit.log).

        Delivery (mode=auto|base64|url):
        - auto: inline base64 (+ utf-8 text when applicable) if size <= 15MB;
          otherwise a short-lived download_url at /api/mcp/download/{token}
          (token is the credential; no JWT/PIN on that URL; ~10 min TTL).
        - base64: require <= 15MB or error.
        - url: always return download_url.

        Refuses secrets (auth.json, settings.json, youtube_token.json, *.pem, *secret*, …).
        HTTP /mcp already requires MCP PIN/JWT; stdio is local. Audit-logged."""
        from studio.mcp_files import get_file_payload

        idx = None if int(index) == -999 else int(index)
        return get_file_payload(
            project_id=project_id or "",
            kind=kind or "",
            path=path or "",
            filename=filename or "",
            index=idx,
            aspect=aspect or "",
            music_id=music_id or "",
            mode=mode or "auto",
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_script_rules() -> str:
        """Live lazykh script format: emotion tags, topic hook, subscribe outro, art style (re-reads user_data/prompts.json)."""
        return with_hook_outro_requirement(
            get_prompt("script.rules")
            + "\n\n"
            + get_prompt("script.hook_outro")
            + "\n\nART STYLE SHORT:\n"
            + get_prompt("art.style_short")
            + "\n\nART STYLE FULL:\n"
            + get_prompt("art.style_full")
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_chatgpt_playbook() -> str:
        """Live production playbook (re-reads user_data/prompts.json). Requires topic HOOK + subscribe OUTRO. Covers image_provider, 5s cover, ChatGPT 16:9/9:16 image-UI presets, no TV/explainer in generated art, music, layouts, delete_files, prompt keys, generate_script_via_api regen defaults, align-after-audio, YouTube, and the full mcp_tools catalog."""
        return with_playbook_runtime_notes(get_prompt("mcp.chatgpt_playbook"))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_studio_settings() -> dict:
        """Live Studio settings: text_provider/script_provider (openai billed API, chatgpt MCP, claude MCP, lmstudio local), lmstudio_base_url, lmstudio_model, image_provider (flux|chatgpt|comfyui), comfyui_url, comfyui_workflow_loaded, video_layout, character_size (large/medium/small), default_aspect, music_volume_pct, tts_provider (openai|elevenlabs|local/resemble Chatterbox), local_tts status, fal_key_set (optional; flux only), voices, prompt keys, YouTube auto_upload/privacy/connected, auto_scheduler (default on), hands_off / hands_off_interval_hours / hands_off_min_queue, gpu_lock {busy, holder, waiters}, mcp_build, mcp_tools. Prefer set_hands_off / restart_api for walk-away and recycling 7878."""
        return studio_settings_payload()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_health() -> dict:
        """Same payload as GET /api/health: ok, gentle, mcp=/mcp, mcp_build, auto_scheduler, hands_off, scheduler_interval_sec, gpu_lock, warnings. Use this to confirm the live API and MCP share the same mcp_build."""
        return studio_health_payload()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_gpu_lock() -> dict:
        """Studio GPU mutex: {busy, holder, waiters}. ComfyUI, local Chatterbox TTS, Flux batches, and pipeline image/audio/render run one at a time (MCP stdio + http://127.0.0.1:7878 share user_data/gpu.lock). Also honors VoiceSync %LOCALAPPDATA%/VoiceSync/gpu_job.lock when present. Wait for this before illustrations + local TTS + another job."""
        from studio.gpu_lock import snapshot

        return snapshot()

    @mcp.tool
    def restart_api() -> dict:
        """Restart Studio uvicorn on http://127.0.0.1:7878 from stdio MCP (does not need the old API to be healthy). Spawns `python run_studio.py` then exits/kills only the 7878 listener. Does not kill Gentle 8766, VoiceSync 8765, or the Electron window (it reattaches). Clears stale gpu.lock if the holder pid is dead. Waits until /api/health returns 200."""
        from studio.restart import restart_studio

        return restart_studio(wait=True)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def ngrok_status() -> dict:
        """Status of Studio's reserved-URL ngrok tunnel (same as GET /api/ngrok). Includes public_url / mcp_url, local port, whether the Studio-owned process is running, and basic-auth user/password when configured. Does not start or stop the tunnel."""
        from studio.ngrok_tunnel import ngrok_status as tunnel_status

        return tunnel_status(reveal_password=True)

    @mcp.tool
    def start_ngrok() -> dict:
        """Enable (start) the Studio-owned ngrok tunnel to the reserved public URL → local Studio port (same as POST /api/ngrok/start). Uses configured basic auth when set. Only manages the process Studio started — does not touch unrelated ngrok instances."""
        from studio.ngrok_tunnel import start_ngrok as enable_ngrok

        return enable_ngrok()

    @mcp.tool
    def stop_ngrok() -> dict:
        """Disable (stop) only the Studio-owned ngrok tunnel process (same as POST /api/ngrok/stop). Does not kill unrelated ngrok instances. Reserved URL and basic-auth settings are kept for the next start_ngrok."""
        from studio.ngrok_tunnel import stop_owned_ngrok

        return stop_owned_ngrok()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_text_provider() -> dict:
        """Return who writes scripts and topic batches: 'openai' (billed chat API), 'chatgpt' (ChatGPT Desktop MCP + save_script), 'claude' (Claude Desktop / Claude Code MCP + save_script), or 'lmstudio' (local OpenAI-compatible API). Alias: script_provider."""
        settings = studio_settings_payload()
        provider = settings.get("text_provider") or "openai"
        return {
            "text_provider": provider,
            "script_provider": provider,
            "options": ["openai", "chatgpt", "claude", "lmstudio"],
            "native": provider in ("chatgpt", "claude"),
            "billed": provider == "openai",
            "local": provider == "lmstudio",
            "lmstudio_base_url": settings.get("lmstudio_base_url"),
            "lmstudio_model": settings.get("lmstudio_model"),
            "note": settings.get("text_provider_note"),
        }

    @mcp.tool
    def set_text_provider(provider: str) -> dict:
        """Set script/topic writer to openai (billed API), chatgpt, claude (Desktop MCP), or lmstudio (local). Persists as text_provider in settings.json (script_provider is the same value)."""
        save_settings({"text_provider": normalize_text_provider(provider)})
        return get_text_provider()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_prompts() -> dict:
        """List every editable LLM/image/TTS prompt (same catalog as GET /api/prompts)."""
        return catalog_payload()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_prompts() -> dict:
        """Live prompt catalog with overrides. Same as list_prompts / GET /api/prompts."""
        return catalog_payload()

    @mcp.tool
    def update_prompt(key: str, text: str) -> dict:
        """Save one prompt override by key (e.g. script.rules). Live on the next generate; no restart."""
        return save_prompt(key, text)

    @mcp.tool
    def save_prompts(prompts: dict) -> dict:
        """Save many prompt overrides as {key: text}. Live on the next generate."""
        if not isinstance(prompts, dict):
            raise RuntimeError("prompts must be an object of key -> text.")
        return write_prompts({str(k): str(v) for k, v in prompts.items()})

    @mcp.tool
    def reset_prompt(key: str = "") -> dict:
        """Restore one prompt (or all, if key is empty) to the code default."""
        return restore_prompts(key or None)

    @mcp.tool
    def request_spend_confirm(action: str, detail: str = "", units: int = 1, project_id: str = "") -> dict:
        """Issue a one-time spend_confirm_id for a billed OpenAI-cloud or fal/Flux action. Pass that id (or confirm_spend=true) on the next billed tool within ~5 minutes. Actions: openai_script, openai_topics, openai_tts, flux_images, flux_cover, flux_regen, pipeline_billed. LM Studio / ChatGPT-native / ComfyUI / local TTS do not need this."""
        from studio.spend_guard import request_spend_confirm as issue

        ctx = {"project_id": project_id} if project_id else {}
        return issue(action or "pipeline_billed", detail=detail, units=units or 1, context=ctx)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_spend_status() -> dict:
        """Daily OpenAI/Flux counters, require_spend_confirm flag, and optional daily caps (0 = unlimited)."""
        from studio.spend_guard import public_spend_status

        return public_spend_status()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_audit_log(limit: int = 50) -> dict:
        """Read-only tail of user_data/audit.log (JSONL): MCP tool calls and significant API actions. Secrets redacted."""
        from studio.audit import audit_public

        return audit_public(max(1, min(200, int(limit or 50))))

    @mcp.tool
    def generate_topics(
        seed: str = "",
        count: int = 8,
        duration_min: float = 2,
        confirm_spend: bool = False,
        spend_confirm_id: str = "",
    ) -> dict:
        """When text_provider is openai or lmstudio: invent explainer topics via that API (prompt key topics.generate) and save drafts. openai bills cloud — requires confirm_spend=true or spend_confirm_id from request_spend_confirm. lmstudio is local (no OpenAI cloud, no spend confirm). When chatgpt or claude: returns use_mcp + playbook and does NOT call OpenAI — invent titles yourself, then create_topic / schedule_topic."""
        _mcp_spend(
            "openai_topics",
            confirm_spend=confirm_spend,
            spend_confirm_id=spend_confirm_id,
            detail=f"Generate {count} topics via OpenAI",
        )
        return invent_topics(seed=seed, count=count, duration_min=duration_min)

    @mcp.tool
    def create_topic(title: str, angle: str = "", duration_min: float = 2) -> dict:
        """Save one draft topic (title + optional angle) without calling OpenAI. ChatGPT/Claude should invent topics then call this (and schedule_topic to produce)."""
        return add_topic(title=title, angle=angle, duration_min=duration_min)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_topics(status: str = "") -> dict:
        """List generated topics (title, angle, duration, status draft|queued|running|done, job_id, scheduled_at UTC ISO, scheduled_at_local, due). Also next_due_at, due_count, auto_scheduler, hands_off, timezone. Filter with status. Same cards as the Topics page."""
        return topics_catalog(status or None, kick=False)

    @mcp.tool
    def update_topic(
        topic_id: str,
        title: str = "",
        angle: str = "",
        duration_min: float = -1,
        scheduled_at: str = "",
    ) -> dict:
        """Edit a saved topic card (same as PATCH /api/topics/{id}). Empty strings leave that field unchanged. Pass scheduled_at as ISO datetime (naive = this PC's local zone). Omitting scheduled_at leaves the due time; this does not start the pipeline (use schedule_topic for that)."""
        kwargs: dict = {}
        if title != "":
            kwargs["title"] = title
        if angle != "":
            kwargs["angle"] = angle
        if duration_min is not None and float(duration_min) >= 0:
            kwargs["duration_min"] = duration_min
        if scheduled_at != "":
            kwargs["scheduled_at"] = scheduled_at
        return patch_topic(topic_id, **kwargs)

    @mcp.tool
    def schedule_topic(
        topic_id: str = "",
        title: str = "",
        duration_min: float = 0,
        angle: str = "",
        run: str = "queue",
        scheduled_at: str = "",
        run_now: bool = False,
    ) -> dict:
        """Create a Studio job from a saved topic_id, or from title + duration_min + optional angle, then queue the full unsupervised pipeline (script with hook+subscribe → pictures → audio → Gentle → render at Settings default_aspect including both → optional YouTube). scheduled_at is ISO datetime; naive values are this PC's local timezone and are stored as UTC. Empty scheduled_at means now. run_now=true (or run='now') starts as soon as the queue is free. run='queue' waits until scheduled_at. With hands_off on, Studio's ~30s due-picker starts queued topics with scheduled_at <= now, FIFO by scheduled_at, one at a time. Without hands_off, due topics wait. Pause holds the queue. chatgpt pictures cannot run headless (falls back to flux if fal_key exists). chatgpt/claude text: save_script on the returned job_id in this same turn before walking away."""
        return enqueue_topic(
            topic_id=topic_id,
            title=title,
            duration_min=duration_min or None,
            angle=angle,
            run=run,
            scheduled_at=scheduled_at,
            run_now=run_now,
        )

    @mcp.tool
    def unschedule_topic(topic_id: str) -> dict:
        """Cancel a queued/scheduled topic without deleting it. Clears scheduled_at, removes it from the FIFO queue, and returns status to draft. Keeps title, angle, and job_id. Same as POST /api/topics/{id}/unschedule. Fails if the topic is running or done."""
        return clear_topic_schedule(topic_id)

    @mcp.tool
    def start_topic_pipeline(
        topic_id: str = "",
        title: str = "",
        duration_min: float = 0,
        angle: str = "",
        scheduled_at: str = "",
        run_now: bool = True,
    ) -> dict:
        """Alias of schedule_topic. Defaults to run_now=true so the due picker starts this topic as soon as the queue is free. Pass scheduled_at and run_now=false to queue for a later local datetime."""
        return launch_topic_pipeline(
            topic_id=topic_id,
            title=title,
            duration_min=duration_min or None,
            angle=angle,
            scheduled_at=scheduled_at,
            run_now=run_now,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def hands_off() -> dict:
        """Unsupervised readiness + one-shot recipe. Checks hands_off, text_provider (openai/lmstudio walk-away; chatgpt/claude skip auto-gen), image_provider (flux/comfyui; chatgpt cannot run headless), tts, auto_scheduler, YouTube connect/auto-upload, and next due topic. Playbook: set_hands_off(true) then walk away. Does not start a job."""
        return hands_off_status()

    @mcp.tool
    def set_hands_off(
        enabled: bool = True,
        interval_hours: float = -1,
        interval_minutes: float = -1,
        min_queue: int = -1,
    ) -> dict:
        """Enable or disable hands-off mode (persists hands_off in settings.json). When on, Studio's ~30s loop generates topics if drafts+queued < hands_off_min_queue (default 5; openai/lmstudio only), auto-schedules drafts, runs the full pipeline, and uploads each job as YouTube private (per-job override). interval_hours 0 = due now FIFO; 2 = spread every 2 hours. interval_minutes is an alternative (60 → 1 hour). min_queue is the target drafts+queued pool (same as Settings hands_off_min_queue). Does not change the user's default youtube_privacy. If YouTube is not connected, still generate+schedule+render and mark upload pending."""
        updates: dict = {"hands_off": enabled}
        if interval_hours is not None and float(interval_hours) >= 0:
            updates["hands_off_interval_hours"] = interval_hours
        elif interval_minutes is not None and float(interval_minutes) >= 0:
            updates["hands_off_interval_hours"] = float(interval_minutes) / 60.0
        if min_queue is not None and int(min_queue) >= 1:
            updates["hands_off_min_queue"] = int(min_queue)
        save_settings(updates)
        status = hands_off_status()
        status["settings"] = studio_settings_payload()
        return status

    @mcp.tool
    def delete_topic(topic_id: str) -> dict:
        """Remove a topic from user_data/topics.json and the FIFO queue. Does not delete the Studio job if one was already created."""
        return remove_topic(topic_id)

    @mcp.tool
    def create_video_project(topic: str, duration_seconds: int, title: str = "", aspect: str = "") -> dict:
        """Create a project from a topic and target length. Aspect omits to Settings default_aspect (usually 16:9); pass 9:16 for vertical or both to render 16:9 and 9:16 as separate files."""
        return create_project(topic, duration_seconds, title or None, aspect=aspect or None)

    @mcp.tool
    def set_video_aspect(project_id: str, aspect: str) -> dict:
        """Set output aspect: '16:9' (landscape), '9:16' (portrait / shorts), or 'both' (render 16:9 then 9:16 as separate mp4s)."""
        return set_aspect(project_id, aspect)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_video_projects() -> list:
        """List Studio library jobs (thumbs, cover/audio/video flags, running status). Same cards as the GUI library."""
        return list_library_items()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_library() -> dict:
        """Studio job library with thumbnail paths/URLs. Same jobs as list_video_projects."""
        items = list_library_items()
        settings = studio_settings_payload()
        return {
            "jobs": items,
            "count": len(items),
            "studio_url": f"http://{settings.get('host') or '127.0.0.1'}:{int(settings.get('port') or 7878)}/",
            "image_provider": settings.get("image_provider"),
            "text_provider": settings.get("text_provider"),
            "script_provider": settings.get("script_provider") or settings.get("text_provider"),
            "video_layout": settings.get("video_layout"),
            "character_size": settings.get("character_size"),
            "tts_provider": settings.get("tts_provider"),
            "voice_provider": settings.get("voice_provider"),
            "default_aspect": settings.get("default_aspect"),
            "music_volume_pct": settings.get("music_volume_pct"),
        }

    @mcp.tool
    def delete_project(project_id: str, delete_files: bool = False) -> dict:
        """Remove a job from the Studio listing. delete_files defaults to False: hide the job but keep user_data/projects/{id}. True permanently deletes that folder (scripts, audio, frames, billboards, mp4, thumbs, meta)."""
        return remove_studio_project(project_id, delete_files=delete_files)

    @mcp.tool
    def rename_video(
        project_id: str,
        title: str,
        topic: str = "",
        update_youtube: bool = True,
        rename_folder: bool = False,
    ) -> dict:
        """Rename a Studio video job. Sets meta title (YouTube max 100 chars). update_youtube=true also patches the live YouTube listing when meta has a video_id (requires youtube.force-ssl — reconnect if rename fails on permissions). rename_folder=true moves user_data/projects/{id} to a slug of the new title and retargets Topics job_id — refuse while the job is running. topic= optionally updates meta.topic too."""
        return rename_studio_video(
            project_id,
            title,
            topic=topic or None,
            rename_folder=bool(rename_folder),
            update_youtube=bool(update_youtube),
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_project(project_id: str) -> dict:
        """Load a project, both script versions, illustration status, and output paths."""
        return project_payload(project_id)

    @mcp.tool
    def generate_script_via_api(
        project_id: str,
        extra: str = "",
        regenerate_pictures: bool = True,
        regenerate_audio: bool = True,
        generate_9x16: bool = False,
        provider: str = "",
        voice_id: str = "",
        confirm_spend: bool = False,
        spend_confirm_id: str = "",
    ) -> dict:
        """Generate a tagged+raw script via API when text_provider is openai (billed cloud — requires confirm_spend=true or spend_confirm_id) or lmstudio (local, no spend confirm). If chatgpt or claude, returns {provider, playbook_hint, use_mcp:true} — write yourself (hook + subscribe outro) then save_script. Required shape: topic hook (also the 9:16 short), explainer body, subscribe outro (16:9). generate_9x16=true (default) writes script_9x16 hook+CTA, 1080x1920 short art, and script_9x16.wav/align. regenerate_pictures/audio default true."""
        from studio.settings import is_native_text_provider

        if is_native_text_provider():
            return native_script_handoff(project_id, extra)
        _mcp_spend(
            "openai_script",
            confirm_spend=confirm_spend,
            spend_confirm_id=spend_confirm_id,
            detail="Generate script via OpenAI",
            project_id=project_id,
        )
        meta = load_meta(project_id)
        generated = generate_script(
            meta["topic"],
            meta["duration_seconds"],
            extra,
            aspect=meta.get("aspect") or DEFAULT_ASPECT,
            layout=project_video_layout(project_id),
            image_provider=project_image_provider(project_id),
            project_id=project_id,
        )
        payload = apply_generated_script(project_id, generated)
        follow = start_script_downstream_job(
            project_id,
            regenerate_pictures=regenerate_pictures,
            regenerate_audio=regenerate_audio,
            generate_9x16=generate_9x16,
            provider=provider or None,
            voice_id=voice_id or None,
        )
        payload["regenerate_pictures"] = regenerate_pictures
        payload["regenerate_audio"] = regenerate_audio
        payload["generate_9x16"] = generate_9x16
        if follow:
            payload["job"] = follow
        return payload

    @mcp.tool
    def save_script(
        project_id: str,
        script_tagged: str,
        summary: str = "",
        scenes: list[str] | None = None,
        youtube_description: str = "",
        youtube_keywords: str | list[str] = "",
        youtube_hashtags: str | list[str] = "",
    ) -> dict:
        """Save a ChatGPT- or Claude-written tagged script (hook + body + subscribe outro). Raw/TTS text is derived automatically: emotion tags and any illustration/art-direction cues are stripped (short [topic] nouns stay spoken). Pass optional scenes=[...] — one subject-only illustration prompt per non-empty line, in order — do NOT embed IMAGE:/PROMPT:/long [scene descriptions] in script_tagged. Also pass youtube_description, youtube_keywords (comma-separated or list), and youtube_hashtags (with #) so upload_to_youtube can publish with that metadata."""
        errors = validate_tagged_script(script_tagged)
        if errors:
            raise RuntimeError("Script failed validation: " + "; ".join(errors))
        meta = load_meta(project_id)
        title = meta.get("title") or meta.get("topic") or project_id
        summary = summary or meta.get("summary") or meta.get("topic") or ""
        lines = parse_tagged_script(
            script_tagged,
            title=title,
            summary=summary,
            scene_hints=scenes or [],
            aspect=meta.get("aspect") or DEFAULT_ASPECT,
            layout=project_video_layout(project_id),
            image_provider=project_image_provider(project_id),
        )
        save_lines(project_id, lines, summary=summary)
        if youtube_description or youtube_keywords or youtube_hashtags:
            apply_youtube_publish_meta(
                project_id,
                description=youtube_description if youtube_description != "" else None,
                keywords=youtube_keywords if youtube_keywords != "" else None,
                hashtags=youtube_hashtags if youtube_hashtags != "" else None,
            )
        payload = write_scripts(project_id, script_tagged, raw_from_tagged(script_tagged))
        warnings = script_structure_warnings(script_tagged)
        if warnings:
            payload["script_warnings"] = warnings
            payload["message"] = "Saved with warnings: " + "; ".join(warnings)
        return payload

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_illustration_jobs(project_id: str) -> dict:
        """Read this project's aspect from Studio FIRST (payload.aspect / project_aspect: 16:9, 9:16, or both — never assume 16:9). Returns cover + line slots sized for that project: 16:9→1920x1080, 9:16→1080x1920, both→both cover files plus matching line art; generate_9x16 adds 9:16 shorts slots. Prompts come from live Studio art.* / images.* — do not invent a style. Cover job prompts require the exact video title as large readable lettering on the artwork (title on cover/title-card only — not every line). Each job includes prompt, width, height, aspect, image_size, chatgpt_preset, generate_at, needs_regen, save_path, filename, required. FILENAMES ARE SHORT on purpose (Windows MAX_PATH): covers script_cover_16x9.png / script_cover_9x16.png; lines b001.png, b002.png, … — always use the exact jobs[].filename and jobs[].save_path; never save ChatGPT's long image title as the disk name. For chatgpt: paste jobs[].prompt verbatim, then save_illustration_image with that short filename. regenerate_illustration(project_id, filename or index) regenerates one slot. index -1 is the current-aspect cover, -2 is 9:16 cover, or pass filename. If image_provider is flux, call generate_illustrations_with_flux for missing files. If comfyui, call generate_illustrations (or generate_illustrations_with_flux — Studio POSTs ComfyUI, no fal). Never use the OpenAI Images API."""
        return illustration_jobs(project_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_image_provider(project_id: str = "") -> dict:
        """Return the image provider: 'flux' (fal-ai/flux-2), 'chatgpt' (native in-chat), or 'comfyui' (local API JSON). With project_id, returns that job's override; otherwise the studio default."""
        settings = public_settings()
        out = {
            "default": settings.get("image_provider") or "flux",
            "flux_model": FLUX_MODEL,
            "options": ["flux", "chatgpt", "comfyui"],
            "fal_key_set": bool(settings.get("fal_key_set")),
            "comfyui_url": settings.get("comfyui_url"),
            "comfyui_workflow_loaded": bool(settings.get("comfyui_workflow_loaded")),
        }
        if project_id:
            out["project_id"] = project_id
            out["image_provider"] = project_image_provider(project_id)
        else:
            out["image_provider"] = out["default"]
        return out

    @mcp.tool
    def set_image_provider(provider: str, project_id: str = "") -> dict:
        """Set image provider to 'flux' (fal-ai/flux-2), 'chatgpt' (native), or 'comfyui' (local). If project_id is set, store the override in that job's meta.json; otherwise update the studio default. While a Pictures/illustrations (or cover) job is generating, changing the provider cancels the in-flight image, keeps completed PNGs, and restarts the current + remaining unfinished slots with the new generator (GPU lock released so the new backend can run)."""
        if project_id:
            return write_project_image_provider(project_id, provider)
        from studio.settings import normalize_image_provider

        prev = normalize_image_provider(load_settings().get("image_provider"))
        new = normalize_image_provider(provider)
        save_settings({"image_provider": provider})
        if new != prev:
            try:
                from studio.pipeline import running_project_ids
                from studio.projects import set_image_provider as write_job_image_provider

                for pid in running_project_ids():
                    st = job_status(pid)
                    step = st.get("step") or ""
                    if step in ("illustrations", "cover") or st.get("busy"):
                        write_job_image_provider(pid, new)
            except Exception:
                pass
        return studio_settings_payload()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_comfyui_status() -> dict:
        """ComfyUI local image backend: url (default http://127.0.0.1:8188), whether user_data/comfyui_workflow.json is loaded, and whether the server is reachable. Upload API JSON in Settings (File → Save (API Format)) or save_comfyui_workflow."""
        from studio.comfyui import comfyui_status

        return comfyui_status()

    @mcp.tool
    def save_comfyui_workflow(workflow_json: str, filename: str = "workflow.json") -> dict:
        """Save a ComfyUI API-format workflow JSON string (File → Save (API Format) in ComfyUI). Stored at user_data/comfyui_workflow.json. Rejects the regular nodes/links UI export. GUI upload in Settings also works."""
        from studio.comfyui import save_workflow

        return save_workflow(workflow_json, filename=filename or "workflow.json")

    @mcp.tool
    def delete_comfyui_workflow() -> dict:
        """Remove the uploaded ComfyUI API workflow JSON (same as DELETE /api/settings/comfyui-workflow / Settings Remove workflow). ComfyUI jobs fail until you upload again. Does not change image_provider."""
        from studio.comfyui import delete_workflow

        return delete_workflow()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_video_layout(project_id: str = "") -> dict:
        """Return video layout: 'cover' (full-bleed) or 'billboard' (16:9 line art composited into a TV overlay later). With project_id, that job's override; otherwise the studio default."""
        settings = public_settings()
        out = {
            "default": settings.get("video_layout") or "cover",
            "options": ["cover", "billboard"],
        }
        if project_id:
            out["project_id"] = project_id
            out["video_layout"] = project_video_layout(project_id)
        else:
            out["video_layout"] = out["default"]
        return out

    @mcp.tool
    def set_video_layout(layout: str, project_id: str = "") -> dict:
        """Set video layout to 'cover' (full-bleed) or 'billboard' (16:9 full-bleed subject doodles; TV overlay is composited later). If project_id is set, store the override in that job's meta.json without changing the studio default; otherwise update Settings."""
        if project_id:
            return write_project_video_layout(project_id, layout)
        save_settings({"video_layout": layout})
        return studio_settings_payload()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_art_styles() -> dict:
        """List per-job illustration art styles (classic hand-drawn default, Pixar/Disney 3D, cinematic, claymation, watercolor, anime, comic book, paper craft, voxel). Used by Flux/ComfyUI/ChatGPT prompts for covers and line art."""
        from studio.art_style import DEFAULT_ART_STYLE, list_art_styles as catalog

        return {"ok": True, "default": DEFAULT_ART_STYLE, "styles": catalog()}

    @mcp.tool
    def set_art_style(style: str, project_id: str = "") -> dict:
        """Set this job's illustration art style (classic, pixar_3d, cinematic, claymation, watercolor, anime, comic_book, paper_craft, voxel). Classic is the original Stickman Automation hand-drawn look. Pass project_id. Regenerates line image_prompts; regenerate covers/line PNGs to apply visually."""
        if not project_id:
            raise ValueError("project_id is required (art_style is per-job).")
        return write_project_art_style(project_id, style)

    @mcp.tool
    def set_character_size(size: str, project_id: str = "") -> dict:
        """Set bubble-head character size to 'large' (current, scale 1.0), 'medium' (half), or 'small' (1/3). Feet stay bottom-anchored; the figure shrinks upward. If project_id is set, store the override in that job's meta.json; otherwise update the studio default."""
        if project_id:
            return write_project_character_size(project_id, size)
        save_settings({"character_size": size})
        return studio_settings_payload()

    @mcp.tool
    def set_include_bubblehead(enabled: bool = True, project_id: str = "") -> dict:
        """Include or omit the yellow stick-figure narrator (poses/mouth/bubble head) on final frames. Default true. Per-job meta include_bubblehead — pass project_id. Does not remove audio, cover intro, or cover/billboard art. Also accepted on render_final_video(include_bubblehead=) and PATCH /api/projects/{id}."""
        if not project_id:
            raise ValueError("project_id is required (include_bubblehead is per-job).")
        return write_project_include_bubblehead(project_id, bool(enabled))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_stickman_head_color() -> dict:
        """Current Bubblehead / stickman head fill color (#RRGGBB), auto-derived shadow, pose file count, and HSL shadow formula. Studio-wide (all jobs share the pose PNGs). Same as GET /api/poses/head-color / Settings → Stickman head."""
        from studio.pose_colors import head_color_status

        out = head_color_status()
        out["ok"] = True
        return out

    @mcp.tool
    def set_stickman_head_color(color: str, from_color: str = "") -> dict:
        """Recolor Bubblehead head fill on every pose under user_data/poses (NOT repo poses/ or poses_stock). videoDrawer reads that same folder via BUBBLEPOD_POSES_DIR. color=#RRGGBB. Shadow auto H−10.4°/L−8.4. Clears cached frames/mp4s — call render_final_video. Title-card covers with a baked-in stickman still need regenerate_cover. Free."""
        from studio.pose_colors import apply_head_color, head_color_status

        result = apply_head_color(
            color,
            from_color=(from_color or "").strip() or None,
            save_setting=True,
        )
        result["stickman_head"] = head_color_status()
        return result

    @mcp.tool
    def reset_stickman_head_color() -> dict:
        """Restore stock yellow Bubblehead poses (#FAE02E / #F8AF05) from user_data/poses_stock into poses/, reset stickman_head_color, and clear cached frames/mp4s. Re-render afterward."""
        from studio.pose_colors import head_color_status, restore_stock_poses

        result = restore_stock_poses()
        result["stickman_head"] = head_color_status()
        return result

    @mcp.tool
    def set_project_voice(project_id: str, provider: str = "", voice_id: str = "") -> dict:
        """Per-job TTS override (same as PATCH /api/projects/{id} tts_provider / voice_id). provider openai|elevenlabs|local (or empty to leave). voice_id is the OpenAI/ElevenLabs/local voice name. Empty strings leave that field unchanged."""
        return write_project_voice(
            project_id,
            provider=provider or None,
            voice_id=voice_id or None,
        )

    @mcp.tool
    def generate_illustrations_with_flux(
        project_id: str,
        confirm_spend: bool = False,
        spend_confirm_id: str = "",
    ) -> dict:
        """Queue Studio image generation using THIS project's aspect from meta (16:9 / 9:16 / both — call list_illustration_jobs or read aspect first; never assume 16:9). Generates the matching 5s title-card(s) and missing line illustrations at 1920x1080 and/or 1080x1920. Flux uses fal-ai/flux-2 (needs FAL_KEY) and requires confirm_spend=true or spend_confirm_id. ComfyUI POSTs the uploaded API JSON to comfyui_url and does not fal (no spend confirm). Refuses chatgpt jobs (native images + save_illustration_image). Same queue as generate_illustrations when provider is comfyui. Poll get_render_status. Waits for the GPU lock (one Comfy/TTS/Flux/pipeline GPU section at a time)."""
        aspect_info = resolve_project_image_aspect(project_id)
        _mcp_spend(
            "flux_images",
            confirm_spend=confirm_spend,
            spend_confirm_id=spend_confirm_id,
            detail=f"Generate backgrounds with Flux (fal) aspect={aspect_info.get('aspect')}",
            project_id=project_id,
        )
        out = start_illustrations_job(project_id)
        if isinstance(out, dict):
            out["project_aspect"] = aspect_info.get("aspect")
            out["aspect"] = aspect_info.get("aspect")
            out["aspects_to_render"] = aspect_info.get("aspects_to_render")
            out["generate_9x16"] = aspect_info.get("generate_9x16")
            out["canvas"] = aspect_info.get("canvas")
            out["aspect_rule"] = aspect_info.get("rule")
        return out

    @mcp.tool
    def generate_illustrations(
        project_id: str,
        confirm_spend: bool = False,
        spend_confirm_id: str = "",
    ) -> dict:
        """Queue cover + missing line illustrations for THIS project's aspect (16:9→1920x1080, 9:16→1080x1920, both→both; read list_illustration_jobs.aspect first). Uses image_provider: flux (fal — needs confirm_spend or spend_confirm_id), comfyui (local, free), not chatgpt (native + save_illustration_image). Poll get_render_status. Waits for the GPU lock."""
        aspect_info = resolve_project_image_aspect(project_id)
        _mcp_spend(
            "flux_images",
            confirm_spend=confirm_spend,
            spend_confirm_id=spend_confirm_id,
            detail=f"Generate illustrations aspect={aspect_info.get('aspect')}",
            project_id=project_id,
        )
        out = start_illustrations_job(project_id)
        if isinstance(out, dict):
            out["project_aspect"] = aspect_info.get("aspect")
            out["aspect"] = aspect_info.get("aspect")
            out["aspects_to_render"] = aspect_info.get("aspects_to_render")
            out["generate_9x16"] = aspect_info.get("generate_9x16")
            out["canvas"] = aspect_info.get("canvas")
            out["aspect_rule"] = aspect_info.get("rule")
        return out

    @mcp.tool
    def generate_cover(
        project_id: str,
        force: bool = False,
        aspect: str = "",
        confirm_spend: bool = False,
        spend_confirm_id: str = "",
    ) -> dict:
        """Ensure the 5-second topic title-card using the project's Studio aspect unless aspect= overrides. Pass aspect 16:9, 9:16, or both. Omit to use the job aspect from meta (both generates script_cover_16x9.png and script_cover_9x16.png). Do not assume 16:9 — check list_illustration_jobs.aspect / get_project first. Does not overwrite the other aspect's cover. Cover prompt (images.cover_intro) requires the exact video title as large readable lettering on the artwork. Uses cover_provider (override) or image_provider: flux spends fal (confirm_spend); comfyui is free; manual/chatgpt mark needs_regen and return the cover prompt (no fal spend). Prefer refresh_covers to upload agent art + re-render in one call. Never stretch 16:9 into 9:16. Waits for the GPU lock when auto-generating."""
        from studio.gpu_lock import holding, wait_for_gpu
        from studio.projects import cover_provider_is_agent, project_cover_provider

        aspect_info = resolve_project_image_aspect(project_id)
        cover_backend = project_cover_provider(project_id)
        if not cover_provider_is_agent(project_id) and cover_backend == "flux":
            _mcp_spend(
                "flux_cover",
                confirm_spend=confirm_spend,
                spend_confirm_id=spend_confirm_id,
                detail=f"Generate cover aspect={aspect or aspect_info.get('aspect')}",
                project_id=project_id,
            )
        wanted = normalize_job_aspect(
            aspect or aspect_info.get("aspect") or load_meta(project_id).get("aspect") or DEFAULT_ASPECT
        )
        if cover_provider_is_agent(project_id) or cover_backend == "chatgpt":
            results = [
                generate_cover_image(project_id, force=force, aspect=canvas)
                for canvas in aspects_to_render(wanted)
            ]
        else:
            wait_for_gpu(name=f"cover:{project_id}", kind="cover", project_id=project_id)
            with holding(f"cover:{project_id}", kind="cover", project_id=project_id):
                results = [
                    generate_cover_image(project_id, force=force, aspect=canvas)
                    for canvas in aspects_to_render(wanted)
                ]
        if len(results) == 1:
            out = results[0]
            if isinstance(out, dict):
                out["project_aspect"] = aspect_info.get("aspect")
                out["aspect_rule"] = aspect_info.get("rule")
                out["cover_provider"] = cover_backend
            return out
        return {
            "ok": True,
            "aspect": "both" if wanted == "both" else wanted,
            "project_aspect": aspect_info.get("aspect"),
            "aspects_to_render": list(aspects_to_render(wanted)),
            "covers": results,
            "cover_provider": cover_backend,
            "aspect_rule": aspect_info.get("rule"),
        }

    @mcp.tool
    def regenerate_cover(
        project_id: str,
        aspect: str = "",
        confirm_spend: bool = False,
        spend_confirm_id: str = "",
    ) -> dict:
        """Regenerate the 5s title-card cover using THIS job's cover_provider (or image_provider). Archives the prior active cover under covers_history/{16x9|9x16}/vNNN.png, bumps Bubblehead pose, and writes a new active script_cover_*.png. Pass aspect 16:9, 9:16, or both. Flux needs confirm_spend; ComfyUI is free; manual/chatgpt mark needs_regen and return the studio cover prompt (no fal). Prefer refresh_covers to upload new art + re-render in one call."""
        from studio.projects import cover_provider_is_agent, project_cover_provider

        aspect_info = resolve_project_image_aspect(project_id)
        cover_backend = project_cover_provider(project_id)
        if not cover_provider_is_agent(project_id) and cover_backend == "flux":
            _mcp_spend(
                "flux_regen",
                confirm_spend=confirm_spend,
                spend_confirm_id=spend_confirm_id,
                detail=f"Regenerate cover aspect={aspect or aspect_info.get('aspect')}",
                project_id=project_id,
            )
        out = regenerate_cover_impl(project_id, aspect=aspect or None)
        if isinstance(out, dict):
            out.setdefault("project_aspect", aspect_info.get("aspect"))
            out.setdefault("aspect_rule", aspect_info.get("rule"))
            out["cover_provider"] = cover_backend
        return out

    @mcp.tool
    def refresh_covers(
        project_id: str,
        covers: list | None = None,
        render: bool = True,
        wait: bool = False,
        layout: str = "",
        character_size: str = "",
    ) -> dict:
        """One-shot cover refresh: normalize each image (Lanczos to 1920x1080 / 1080x1920 JPEG ≤1MB; orientation guard only), archive current actives under covers_history, write script_cover_16x9.png / script_cover_9x16.png, then queue render_final_video so auto-upload updates the existing YouTube video. covers=[{aspect:'16:9'|'9:16', image_b64|image_url}, ...]. Wrong-size same-orientation inputs are normalized, never rejected. Queues behind the GPU lock (does not preempt running renders). Poll get_render_status."""
        return refresh_covers_impl(
            project_id,
            covers=covers or [],
            render=render,
            wait=wait,
            layout=layout or "",
            character_size=character_size or "",
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_cover_provider(project_id: str = "") -> dict:
        """Return effective cover_provider (manual|chatgpt|comfyui|flux) and override. Empty project_id → global Settings."""
        from studio.projects import project_cover_provider, project_cover_provider_override, project_image_provider
        from studio.settings import load_settings, normalize_cover_provider

        if project_id:
            return {
                "ok": True,
                "project_id": project_id,
                "cover_provider": project_cover_provider(project_id),
                "cover_provider_override": project_cover_provider_override(project_id),
                "image_provider": project_image_provider(project_id),
            }
        s = load_settings()
        return {
            "ok": True,
            "cover_provider": normalize_cover_provider(s.get("cover_provider")),
            "image_provider": s.get("image_provider"),
        }

    @mcp.tool
    def set_cover_provider(project_id: str = "", provider: str = "") -> dict:
        """Set cover_provider override: '' (inherit image_provider), manual, chatgpt, comfyui, or flux. With project_id sets per-job meta; without project_id updates global Settings. When manual/chatgpt, generate_cover/regenerate_cover never spend fal on covers."""
        from studio.projects import set_cover_provider as write_cover_provider
        from studio.settings import normalize_cover_provider, public_settings, save_settings

        if project_id:
            return write_cover_provider(project_id, provider)
        save_settings({"cover_provider": normalize_cover_provider(provider)})
        return {"ok": True, **public_settings()}

    @mcp.tool
    def list_cover_versions(project_id: str, aspect: str = "") -> dict:
        """List archived cover generations per aspect under covers_history/{16x9|9x16}/vNNN.png. Omit aspect (or pass both) for every canvas this job needs. Each entry has version_id, url, active flag. Use set_active_cover(version_id) to promote one for rebuild/render."""
        return cover_history_payload(project_id, aspect=aspect or None)

    @mcp.tool
    def set_active_cover(project_id: str, version_id: str, aspect: str = "16:9") -> dict:
        """Promote a covers_history version (e.g. v001) to the canonical script_cover_16x9.png or script_cover_9x16.png used by render. Pass aspect 16:9 or 9:16. Does not delete other history versions."""
        return set_active_cover_impl(project_id, aspect=aspect or "16:9", version_id=version_id)

    @mcp.tool
    def regenerate_illustration(
        project_id: str,
        filename: str = "",
        index: int | None = None,
        kind: str = "",
        confirm_spend: bool = False,
        spend_confirm_id: str = "",
    ) -> dict:
        """Regenerate ONE illustration using this job's image_provider (covers use cover_provider). Pass filename (script_cover_16x9.png, script_cover_9x16.png, or a line PNG) or index (-1=current-aspect cover, -2=9:16 cover, else line index) and optional kind cover|line. Cover slots use images.cover_intro with the exact video title as large readable lettering on the artwork; line slots stay subject-only. Flux queues fal (confirm_spend) unless cover_provider is manual/chatgpt. ComfyUI is free. ChatGPT/manual covers mark needs_regen and return prompt + save_illustration_image args. Prefer refresh_covers for agent cover uploads + re-render."""
        from studio.illustrations import is_cover_filename
        from studio.projects import cover_provider_is_agent, project_cover_provider

        aspect_info = resolve_project_image_aspect(project_id)
        is_cover = (
            (kind or "").strip().lower() == "cover"
            or is_cover_filename(filename or "")
            or (isinstance(index, int) and index < 0)
        )
        needs_fal = True
        if is_cover and (cover_provider_is_agent(project_id) or project_cover_provider(project_id) != "flux"):
            needs_fal = project_cover_provider(project_id) == "flux"
        elif project_image_provider(project_id) != "flux":
            needs_fal = False
        if needs_fal:
            _mcp_spend(
                "flux_regen",
                confirm_spend=confirm_spend,
                spend_confirm_id=spend_confirm_id,
                detail=f"Regenerate illustration {filename or index} aspect={aspect_info.get('aspect')}",
                project_id=project_id,
            )
        out = start_regenerate_illustration_job(
            project_id,
            filename=filename or None,
            index=index,
            kind=kind or None,
        )
        if isinstance(out, dict):
            out.setdefault("project_aspect", aspect_info.get("aspect"))
            out.setdefault("aspect_rule", aspect_info.get("rule"))
            out.setdefault("canvas", aspect_info.get("canvas"))
        return out

    @mcp.tool
    def save_illustration_image(
        project_id: str,
        filename: str = "",
        line_index: int | None = None,
        image: str = "",
        image_url: str = "",
        kind: str = "billboard",
        prompt_used: str = "",
    ) -> dict:
        """Save a ChatGPT-native illustration into the correct project slot (cover / line / 9:16 shorts) so Pictures and list_illustration_jobs see it immediately — no manual copy.

        First read list_illustration_jobs for this project's aspect (16:9 / 9:16 / both) — do not assume 16:9. Generate using jobs[].prompt VERBATIM (live Studio art style). Prefer the job's width/height; wrong-size same-orientation images are auto-resized (Lanczos) and JPEG-compressed ≤1MB server-side — never rejected for size alone. Opposite orientation (portrait into landscape slot or reverse) is still rejected. Cover: filename='script_cover_16x9.png' or 'script_cover_9x16.png', kind='cover'. Lines: filename='b001.png' / … from jobs[].filename. For multi-cover refresh + re-render prefer refresh_covers. NEVER use ChatGPT's long generated image title as the disk filename."""
        return save_illustration(
            project_id,
            filename=filename or None,
            line_index=line_index,
            image=image,
            image_url=image_url or None,
            kind=kind,
            prompt_used=prompt_used or None,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_tts_voices(provider: str = "") -> dict:
        """List voices for openai, elevenlabs, or local (Resemble Chatterbox / Piper fallback). Empty provider uses Settings tts_provider."""
        return list_voices(provider or None)

    @mcp.tool
    def generate_speech(
        project_id: str,
        provider: str = "",
        voice_id: str = "",
        confirm_spend: bool = False,
        spend_confirm_id: str = "",
    ) -> dict:
        """Create narration WAV using Settings tts_provider (openai, elevenlabs, or local Resemble Chatterbox) unless provider is set, then immediately align phonemes with Gentle (fresh script.json) before scheduler/render. OpenAI TTS requires confirm_spend=true or spend_confirm_id. Local never falls back to paid OpenAI TTS and needs no spend confirm. Local Chatterbox waits for the GPU lock (exclusive with ComfyUI)."""
        from studio.settings import load_settings, normalize_tts_provider

        settings = load_settings()
        tts = normalize_tts_provider(provider or settings.get("tts_provider") or "openai")
        _mcp_spend(
            "openai_tts",
            confirm_spend=confirm_spend,
            spend_confirm_id=spend_confirm_id,
            detail="OpenAI TTS narration",
            project_id=project_id,
            tts_provider=tts,
        )
        if tts == "local":
            from studio.gpu_lock import wait_for_gpu

            wait_for_gpu(name=f"tts-local:{project_id}", kind="tts-local", project_id=project_id)
        return generate_audio_then_align(
            project_id,
            provider=provider or None,
            voice_id=voice_id or None,
            progress=False,
        )

    @mcp.tool
    def ensure_gentle() -> dict:
        """Start the phoneme aligner. Uses Docker Gentle when that container is healthy; otherwise starts a local Gentle-compatible server (no Docker)."""
        return boot_gentle()

    @mcp.tool
    def ensure_gentle_docker() -> dict:
        """Alias for ensure_gentle (Docker if available, else the local fallback)."""
        return boot_gentle()

    @mcp.tool
    def start_gentle() -> dict:
        """Alias for ensure_gentle — start Docker Gentle or the local fallback."""
        return boot_gentle()

    @mcp.tool
    def stop_gentle() -> dict:
        """Stop Docker Gentle (lazykh-gentle) and/or the local aligner process Studio owns. Does not kill a remote unmanaged Gentle URL."""
        return halt_gentle()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_gentle_status() -> dict:
        """Check whether the Gentle HTTP API is reachable and whether the backend is Docker or local."""
        return gentle_status()

    @mcp.tool
    def align_phonemes(project_id: str) -> dict:
        """Run Gentle (Docker or local fallback) to timestamp every phoneme for lip-sync."""
        return align_audio(project_id)

    @mcp.tool
    def render_final_video(
        project_id: str,
        use_billboards: bool = True,
        wait: bool = False,
        aspect: str = "",
        layout: str = "",
        character_size: str = "",
        include_bubblehead: bool | None = None,
        shuffle_music: bool = False,
    ) -> dict:
        """Align if needed, prepend the 5s topic title-card, draw lip-sync frames, mux that aspect's mp4, and loop library music. Pass aspect 16:9, 9:16, or both. both writes script_final_16x9.mp4 then script_final_9x16.mp4 without deleting the other. script_final.mp4 is a copy of the last render. Missing/wrong-size covers regenerate per aspect (never stretch). If one aspect fails, the other file is kept. include_bubblehead=false skips compositing the stick-figure narrator (keeps art/audio/cover)."""
        ratio = normalize_job_aspect(aspect) if aspect else None
        video_layout = normalize_video_layout(layout) if layout else None
        size = normalize_character_size(character_size) if character_size else None
        if wait:
            from studio.gpu_lock import holding, wait_for_gpu

            wait_for_gpu(name=f"render:{project_id}", kind="render", project_id=project_id)
            with holding(f"render:{project_id}", kind="render", project_id=project_id):
                return render_video(
                    project_id,
                    use_billboards=use_billboards,
                    generate_missing_audio=False,
                    aspect=ratio,
                    layout=video_layout,
                    character_size=size,
                    include_bubblehead=include_bubblehead,
                    shuffle_music=shuffle_music,
                )
        return start_render_thread(
            project_id,
            use_billboards=use_billboards,
            aspect=ratio,
            layout=video_layout,
            character_size=size,
            include_bubblehead=include_bubblehead,
            shuffle_music=shuffle_music,
        )

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_render_status(project_id: str) -> dict:
        """Poll frame-draw / ffmpeg progress for a project."""
        return job_status(project_id)

    @mcp.tool
    def start_job(project_id: str) -> dict:
        """Start a Studio job pipeline from empty or the first incomplete step (script → cover/illustrations → audio → align → render → optional YouTube). If a worker is already live, attaches without starting a duplicate thread. Returns resumed_from for the first incomplete step. Waits for the GPU lock before launching image/TTS/render work."""
        return start_project(project_id)

    @mcp.tool
    def resume_job(project_id: str) -> dict:
        """Resume a Studio job from the last successful pipeline step. Skips existing script, cover/illustrations, and wav. Gentle json is reused only if it matches the current script_g.txt and is not older than the wav; otherwise aligns first. Never runs scheduler on stale json. Rendering the current aspect does not remove the other aspect's mp4. If auto-upload is on and the mp4 exists, continues to YouTube. If the job is already running or still winding down after Pause/Stop, attaches without starting a duplicate thread. Failed/stopped/paused jobs clear the halt and continue. Returns resumed_from. Waits for the GPU lock before launching image/TTS/render work."""
        return resume_project(project_id)

    @mcp.tool
    def stop_job(project_id: str) -> dict:
        """Stop a running Studio job. Sets running=false immediately. The worker exits at the next pipeline step boundary (does not kill ffmpeg mid-frame). Resume after it has stopped continues from completed artifacts and will not start a second worker while this one is still alive."""
        return stop_project(project_id)

    @mcp.tool
    def pause_job(project_id: str) -> dict:
        """Pause a running Studio job (same cooperative halt as stop_job, marked paused). Sets running=false. The worker exits at the next step boundary. resume_job continues from completed artifacts; if the current step is still finishing, resume attaches to that same thread instead of starting a duplicate."""
        return pause_project(project_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_music() -> dict:
        """List uploaded background-music tracks in user_data/music, plus Settings music_volume_pct. Render picks one at random per job and loops it under speech and the 5s cover."""
        return library_payload()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_backgrounds() -> dict:
        """List studio-room images (clock, wall, floor behind the stick figure) from backgrounds/ and user_data/backgrounds/. Pick one per job with set_project_background; stored as meta.json background_file."""
        return backgrounds_catalog()

    @mcp.tool
    def set_project_background(project_id: str, filename: str) -> dict:
        """Set this job's studio room image by filename from list_backgrounds (e.g. bga0.png). Billboard uses it full-color behind the TV; cover still full-bleed doodles. Saved on meta.json as background_file."""
        return write_project_background(project_id, filename)

    @mcp.tool
    def shuffle_job_music(project_id: str) -> dict:
        """Pick a different library track for this job (shuffle mode). The next render uses it; this does not re-render."""
        return shuffle_project_music(project_id)

    @mcp.tool
    def set_job_music(project_id: str, music_id: str = "", mode: str = "") -> dict:
        """Lock this job to a specific library track (music_id from list_music), or pass mode='shuffle' with empty music_id to clear the lock so the next render auto-picks. Does not re-render."""
        if (mode or "").strip().lower() == "shuffle" and not (music_id or "").strip():
            return clear_project_music(project_id)
        tid = (music_id or "").strip()
        if not tid:
            raise ValueError("Pass music_id from list_music, or mode='shuffle'.")
        return set_project_music(project_id, tid)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def youtube_status() -> dict:
        """YouTube connection status: connected, selected channel, auto-upload default, privacy default, and the browser connect URL. No popup."""
        return yt.status()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def list_youtube_channels() -> dict:
        """List YouTube channels on the connected Google account. Connect first via youtube_connect (open the URL in a system browser)."""
        return yt.list_channels()

    @mcp.tool
    def youtube_connect() -> dict:
        """Start YouTube OAuth. Open the returned auth_url or studio_connect_url in your SYSTEM/default browser — MCP cannot show a popup or Electron window. Studio receives the 127.0.0.1 loopback callback. After sign-in, call list_youtube_channels then set_youtube_channel(channel_id). If the browser shows a code or redirect URL instead of auto-callback, call youtube_finish_oauth."""
        return yt.start_connect(open_browser=True)

    @mcp.tool
    def youtube_finish_oauth(code: str = "", url: str = "") -> dict:
        """Finish YouTube OAuth by pasting the redirect URL or code from the browser (same as POST /api/youtube/oauth/code). Use when the 127.0.0.1 callback did not auto-complete."""
        blob = (url or code or "").strip()
        if not blob:
            raise RuntimeError("Pass url= (full redirect) or code= from the Google OAuth page.")
        return yt.finish_oauth_paste(blob)

    @mcp.tool
    def youtube_disconnect() -> dict:
        """Disconnect YouTube: delete the stored Google token. Does not change client id/secret."""
        return yt.disconnect()

    @mcp.tool
    def set_youtube_channel(channel_id: str, title: str = "") -> dict:
        """Select which YouTube channel uploads go to after youtube_connect. channel_id from list_youtube_channels. Same as PUT /api/youtube/channel."""
        return yt.set_channel(channel_id, title=title)

    @mcp.tool
    def set_project_youtube(
        project_id: str,
        youtube_auto_upload: str = "",
        youtube_privacy: str = "",
        youtube_channel_id: str = "",
    ) -> dict:
        """Per-job YouTube override (same as PATCH /api/projects/{id}). youtube_auto_upload true|false, youtube_privacy private|unlisted|public, optional youtube_channel_id. Empty strings leave that field unchanged."""
        return write_project_youtube(
            project_id,
            auto_upload=youtube_auto_upload if youtube_auto_upload != "" else None,
            privacy=youtube_privacy or None,
            channel_id=youtube_channel_id if youtube_channel_id != "" else None,
        )

    @mcp.tool
    def upload_to_youtube(
        project_id: str,
        privacy_status: str = "unlisted",
        title: str = "",
        description: str = "",
        tags: str | list[str] = "",
        aspect: str = "",
    ) -> dict:
        """Upload this job's finished mp4 to the selected YouTube channel. Prefer aspect 16:9 or 9:16 when both exist; otherwise last render / script_final.mp4. privacy_status must be private, unlisted, or public (default unlisted). Title defaults to the job topic/title. Description defaults to meta youtube_description (then summary/topic); tags default to meta youtube_keywords (API tags). Hashtags from meta youtube_hashtags are appended to the description when missing. Pass description/tags to override. Does not re-render. Requires youtube_connect first."""
        return yt.upload_project_video(
            project_id,
            privacy_status=privacy_status or None,
            title=title or None,
            description=description or None,
            tags=tags if tags != "" else None,
            aspect=aspect or None,
        )

    @mcp.tool
    def update_studio_settings(
        openai_api_key: str = "",
        elevenlabs_api_key: str = "",
        fal_key: str = "",
        tts_provider: str = "",
        voice_provider: str = "",
        openai_voice: str = "",
        elevenlabs_voice_id: str = "",
        local_voice: str = "",
        gentle_url: str = "",
        image_provider: str = "",
        text_provider: str = "",
        script_provider: str = "",
        video_layout: str = "",
        character_size: str = "",
        music_volume_pct: int = -1,
        default_aspect: str = "",
        aspect: str = "",
        openai_model: str = "",
        openai_tts_model: str = "",
        elevenlabs_model: str = "",
        lmstudio_base_url: str = "",
        lmstudio_model: str = "",
        comfyui_url: str = "",
        youtube_client_id: str = "",
        youtube_client_secret: str = "",
        youtube_channel_id: str = "",
        youtube_auto_upload: str = "",
        youtube_privacy: str = "",
        auto_scheduler: str = "",
        hands_off: str = "",
        hands_off_interval_hours: float = -1,
        hands_off_min_queue: int = -1,
        require_spend_confirm: str = "",
        max_openai_calls_per_day: int = -1,
        max_flux_images_per_day: int = -1,
        rate_limit_enabled: str = "",
        rate_limit_api: int = -1,
        rate_limit_login: int = -1,
    ) -> dict:
        """Store API keys, text_provider/script_provider (openai billed API, chatgpt MCP, claude MCP, or lmstudio local), lmstudio_base_url / lmstudio_model, default image provider (flux, chatgpt, or comfyui), comfyui_url (default http://127.0.0.1:8188), default video layout (cover or billboard), character_size (large, medium, or small), default_aspect (16:9, 9:16, or both), background music loudness 0–100, tts_provider (openai, elevenlabs, or local/resemble Chatterbox), voice settings, YouTube auto-upload (youtube_auto_upload true/false, youtube_privacy private|unlisted|public, youtube_channel_id), auto_scheduler (true/false, default on — only auto-starts due topics while hands_off is also on), hands_off (true/false — generate topics, auto-schedule, run pipeline, YouTube private per job), hands_off_interval_hours (0 = due now FIFO), require_spend_confirm, daily OpenAI/Flux caps (0=unlimited), and rate_limit_api / rate_limit_login. fal_key is optional and only needed for Flux. Empty string / ******** / omitted secrets are ignored and never wipe stored keys. ComfyUI workflows are uploaded in Settings or save_comfyui_workflow. YouTube OAuth is youtube_connect (open the URL in a browser; no popup). Prefer set_youtube_channel after listing channels. Prefer set_hands_off for walk-away. Billed OpenAI/Flux tools still need confirm_spend or spend_confirm_id unless require_spend_confirm is false."""
        candidate = {
            "openai_api_key": openai_api_key,
            "elevenlabs_api_key": elevenlabs_api_key,
            "fal_key": fal_key,
            "youtube_client_secret": youtube_client_secret,
        }
        updates = scrub_secret_updates({k: v for k, v in candidate.items() if v})
        # Extra guard: never persist redaction placeholders even if scrub misses
        for key in list(updates.keys()):
            if is_placeholder_secret(updates.get(key)):
                updates.pop(key, None)
        chosen_tts = tts_provider or voice_provider
        if chosen_tts:
            updates["tts_provider"] = normalize_tts_provider(chosen_tts)
        if openai_voice:
            updates["openai_voice"] = openai_voice
        if elevenlabs_voice_id:
            updates["elevenlabs_voice_id"] = elevenlabs_voice_id
        if local_voice:
            updates["local_voice"] = local_voice
        if gentle_url:
            updates["gentle_url"] = gentle_url
        if image_provider:
            updates["image_provider"] = image_provider
        chosen_text = text_provider or script_provider
        if chosen_text:
            updates["text_provider"] = normalize_text_provider(chosen_text)
        if video_layout:
            updates["video_layout"] = video_layout
        if character_size:
            updates["character_size"] = character_size
        aspect_value = default_aspect or aspect
        if aspect_value:
            updates["default_aspect"] = aspect_value
        if openai_model:
            updates["openai_model"] = openai_model
        if openai_tts_model:
            updates["openai_tts_model"] = openai_tts_model
        if elevenlabs_model:
            updates["elevenlabs_model"] = elevenlabs_model
        if lmstudio_base_url:
            updates["lmstudio_base_url"] = lmstudio_base_url
        if lmstudio_model:
            updates["lmstudio_model"] = lmstudio_model
        if comfyui_url:
            updates["comfyui_url"] = comfyui_url
        if music_volume_pct >= 0:
            updates["music_volume_pct"] = music_volume_pct
        if youtube_client_id:
            updates["youtube_client_id"] = youtube_client_id
        if youtube_channel_id:
            updates["youtube_channel_id"] = youtube_channel_id
        if youtube_auto_upload:
            updates["youtube_auto_upload"] = youtube_auto_upload
        if youtube_privacy:
            updates["youtube_privacy"] = youtube_privacy
        if auto_scheduler != "":
            updates["auto_scheduler"] = auto_scheduler
        if hands_off != "":
            updates["hands_off"] = hands_off
        if hands_off_interval_hours is not None and float(hands_off_interval_hours) >= 0:
            updates["hands_off_interval_hours"] = hands_off_interval_hours
        if hands_off_min_queue is not None and int(hands_off_min_queue) >= 0:
            updates["hands_off_min_queue"] = hands_off_min_queue
        if require_spend_confirm != "":
            updates["require_spend_confirm"] = require_spend_confirm
        if max_openai_calls_per_day is not None and int(max_openai_calls_per_day) >= 0:
            updates["max_openai_calls_per_day"] = max_openai_calls_per_day
        if max_flux_images_per_day is not None and int(max_flux_images_per_day) >= 0:
            updates["max_flux_images_per_day"] = max_flux_images_per_day
        if rate_limit_enabled != "":
            updates["rate_limit_enabled"] = rate_limit_enabled
        if rate_limit_api is not None and int(rate_limit_api) > 0:
            updates["rate_limit_api"] = rate_limit_api
        if rate_limit_login is not None and int(rate_limit_login) > 0:
            updates["rate_limit_login"] = rate_limit_login
        if updates:
            save_settings(updates)
        return studio_settings_payload()

    return mcp


if __name__ == "__main__":
    try:
        from studio.mcp_install import ensure_claude_mcp_config

        ensure_claude_mcp_config()
    except Exception:
        pass
    build_mcp().run(transport="stdio")
