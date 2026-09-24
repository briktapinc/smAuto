"""All lazykh script rules, used by the OpenAI GUI generator and by ChatGPT via MCP."""

ALLOWED_EMOTIONS = ("explain", "happy", "sad", "angry", "confused", "rq")

WORDS_PER_MINUTE = 145

HOOK_OUTRO_REQUIREMENT = """
REQUIRED SCRIPT SHAPE (every generated or saved explainer):
1. HOOK first: 1 to 3 spoken lines that grab attention about THIS topic. Not a generic greeting ("hey guys", "welcome back", "in this video").
   The hook IS the 9:16 short: write it with shorts energy to attract interest. The 9:16 render speaks ONLY these hook lines, then a channel CTA from script.shorts_cta (editable; never part of the 16:9 video).
2. Explainer body next (16:9 full video).
3. SUBSCRIBE OUTRO last: a short on-topic closer that actually says the word "subscribe" (YouTube-style). Generic "subscribe" is fine; do not invent a channel name unless the user gave one. This outro is for the 16:9 explainer only — do not put the 9:16 channel-link CTA in the full script.
Hook and outro COUNT toward spoken duration. Reserve a few seconds at the start and about 8 to 12 seconds at the end.
""".strip()


SCRIPT_REGEN_REQUIREMENT = """
SCRIPT REGEN (generate_script_via_api):
Defaults regenerate_pictures=true and regenerate_audio=true.
A new script always overwrites script files and drops stale Gentle json / schedule / frames / mp4
so scheduler.py cannot run on mismatched json+txt.
If regenerate_pictures: Flux and ComfyUI overwrite every cover+line PNG (no skip-if-exists); ChatGPT marks
all slots needs_regen / waiting. Stale billboard filenames not in the new script are deleted.
If regenerate_audio: new TTS wav, then Gentle phoneme alignment immediately (fresh script.json)
BEFORE any scheduler / videoDrawer / render.
If a flag is false, leave those artifacts as-is (current skip-if-exists).
generate_speech always aligns phonemes after writing script.wav. Resume/Start: wav exists but
json missing/stale → align first. Never call scheduler without matching script.json.
""".strip()


NO_TV_IN_ART_REQUIREMENT = """
GENERATED ART (Fal, ComfyUI, ChatGPT native — never the compositor TV PNG):
Subject-only full-frame diagrams/illustrations of the topic (almost no readable text). NO TV, NO monitor, NO bezel, NO stand, NO screen-in-screen
in the PNG. Billboard layout pastes this art into a TV overlay later (code/assets/tv_billboard.png).
NO people, NO stick figure, NO mascot host, NO presenter, NO explaining character (art.no_character).
Fal and ComfyUI get a SHORT prompt (images.flux_instructions + art.* only). Never dump this playbook into fal or ComfyUI.
""".strip()


APP_IMAGE_PROMPTS_REQUIREMENT = """
APP IMAGE PROMPTS ONLY (Flux, ComfyUI, ChatGPT native):
Do NOT invent a new art style, palette, or image playbook. Studio owns style via live prompts
(user_data/prompts.json / Prompts page): images.flux_instructions, images.cover_intro,
images.chatgpt_instructions, art.style_short / art.style_full / art.scene_wrapper / art.* framing.
- flux / comfyui: Studio injects those prompts itself when you call generate_illustrations_with_flux,
  generate_illustrations, generate_cover, or regenerate_illustration. Never paste this playbook into fal/ComfyUI.
- chatgpt: Call list_illustration_jobs (or regenerate_illustration) and use jobs[].prompt / instructions
  verbatim in ChatGPT's image UI — that text already includes the configured art style. Do not rewrite style.
  After each image, call save_illustration_image (official path) so the PNG lands under
  user_data/projects/{id}/ and Pictures / list_illustration_jobs sees it immediately.
Cover (kind=cover): prompts require the exact video title as large readable lettering on the artwork.
Line billboards stay subject-only (no forced title text unless the line prompt already asks for it).
get_prompt / list_prompts show the catalog; do not freeform a different look.
""".strip()


COVER_TITLE_ON_IMAGE_REQUIREMENT = """
COVER TITLE CARD (5s intro still — not every line billboard):
The cover (kind=cover / script_cover_16x9.png / script_cover_9x16.png) is a Stickman Automation
studio-room scene matching the project's selected background_file:
- Yellow Bubblehead stick figure OUTSIDE the TV (left on 16:9; lower on 9:16) in a unique pose
- Large TV/billboard with the exact video title as large bold on-screen headline text PLUS
  topic illustration on the screen
- Never draw the presenter inside the TV art
list_illustration_jobs cover prompts / images.cover_intro already inject this (Studio also
appends composition clauses in code). ChatGPT: paste the cover job prompt verbatim.
Flux/ComfyUI generate_cover / regenerate_cover / generate_illustrations* use the same prompt
via the job's image_provider. regenerate_cover archives prior actives under
covers_history/{16x9|9x16}/vNNN.png; list_cover_versions / set_active_cover promote any version.
Line billboards stay subject-only (no forced title text, no host, no TV-as-object).
""".strip()


TEXT_PROVIDER_REQUIREMENT = """
TEXT PROVIDER (get_text_provider / set_text_provider; settings text_provider, alias script_provider):
openai | chatgpt | claude | lmstudio.
- openai: billed OpenAI chat API. generate_script_via_api writes the tagged script. generate_topics invents titles.
  SPEND: requires confirm_spend=true or spend_confirm_id from request_spend_confirm (GUI shows a Confirm dialog).
- chatgpt: ChatGPT Desktop MCP. YOU write the tagged script (topic HOOK + subscribe OUTRO, emotion tags) then save_script with youtube_description / youtube_keywords / youtube_hashtags. Do NOT call generate_script_via_api.
- claude: Claude Desktop / Claude Code, same stdio MCP (python -m studio.mcp_server). Same as chatgpt: YOU write then save_script (include YouTube metadata). Do NOT call generate_script_via_api.
- lmstudio: local OpenAI-compatible API (default http://127.0.0.1:1234/v1). Studio generates via the local LLM — no OpenAI cloud, no MCP native write, no spend confirm. ChatGPT/Claude must call generate_script_via_api / generate_topics rather than writing the body themselves. Settings: lmstudio_base_url, lmstudio_model (optional; empty → GET /v1/models, first loaded). If it is not running: start the LM Studio local server.
Topics: if chatgpt or claude, YOU invent titles then create_topic / schedule_topic. Do not call generate_topics unless provider is openai or lmstudio.
SPEND GUARD (OpenAI cloud + fal/Flux only): request_spend_confirm(action) then pass spend_confirm_id, or confirm_spend=true on the billed tool. Actions: openai_script, openai_topics, openai_tts, flux_images, flux_cover, flux_regen. ComfyUI, local TTS, ChatGPT/Claude native, and LM Studio skip this. get_spend_status / get_audit_log. Audit JSONL: user_data/audit.log.
""".strip()


UNSUPERVISED_REQUIREMENT = """
UNSUPERVISED WALK-AWAY (MCP + Studio due-picker):
Providers that can run headless: text openai or lmstudio; pictures flux, comfyui, or external;
audio openai, elevenlabs, local Chatterbox, or external MP3.
external images (preferred for MCP): set_image_provider('external'), list_illustration_jobs, generate art,
save_illustration_image / save_illustration_images for cover + every line, then schedule/start/resume.
Missing slots → EXTERNAL_IMAGES_MISSING (Studio will not call Flux/ComfyUI).
chatgpt pictures: same upload path; with openai/lmstudio text + fal_key, empty slots may fall back to flux.
chatgpt/claude text CAN be unsupervised only if you save_script(job_id) in the SAME turn after schedule_topic returns job_id.
schedule_topic(topic_id?, title?, duration_min?, scheduled_at, run_now?). Naive scheduled_at is this PC's local timezone, stored as UTC ISO.
Studio checks queued topics about every 30s: status queued AND scheduled_at <= now, FIFO by scheduled_at, one pipeline at a time.
Do not start if another job is running or paused. Due topics auto-start only while hands_off is on (auto_scheduler also on by default). Run now = scheduled_at now.
Pipeline: script (generate_script_via_api for openai/lmstudio) → pictures → audio → Gentle → render (including both) → YouTube if youtube_auto_upload and connected (privacy from settings, default unlisted). Hands-off jobs override youtube_privacy=private on that job only.
hands_off() returns readiness + recipe. set_hands_off(true) enables walk-away: auto generate_topics, auto-schedule, due-picker, YouTube private. restart_api() restarts 7878 from stdio MCP.
list_topics shows due times. start_topic_pipeline aliases schedule_topic (run_now default true).
GPU LOCK: ComfyUI, local Chatterbox TTS, Flux image batches, and pipeline image/audio/render run one at a time. Never fire illustrations + local TTS + another job in parallel — wait for get_gpu_lock (timeout + error if still busy).
Playbook: set_hands_off(true) with headless providers (text openai/lmstudio, images flux/comfyui/external), then walk away. Or generate_topics + schedule_topic with a datetime.
""".strip()


IMAGE_PROVIDER_REQUIREMENT = """
IMAGE PROVIDER (get_image_provider / set_image_provider; settings image_provider):
- flux: Studio generates via fal-ai/flux-2. Call generate_illustrations_with_flux. SHORT app prompts only (images.flux_instructions + art.*). Needs FAL_KEY.
- chatgpt: YOU generate natively using jobs[].prompt from list_illustration_jobs (app art style — do not invent one), then save_illustration_image so Pictures sees the file. Do not call generate_illustrations_with_flux or fal. Unsupervised: upload every slot first (or set image_provider=external); with openai/lmstudio text + fal_key, empty slots may fall back to flux.
- external: like tts_provider=external — Studio never generates art. Generate in MCP/ChatGPT then save_illustration_image / save_illustration_images for cover + every line. Missing slots → EXTERNAL_IMAGES_MISSING. Preferred for MCP-driven unsupervised runs.
- comfyui: Studio generates via local ComfyUI (default http://127.0.0.1:8188). Upload a ComfyUI API Format JSON in Settings (Save (API Format) / /prompt graph) or save_comfyui_workflow. Call generate_illustrations — generate_illustrations_with_flux also dispatches here and does not fal. Studio POSTs that workflow to ComfyUI. Injects the short scene prompt (images.flux_instructions + art.*) into CLIPTextEncode / text widgets; sets EmptyLatentImage / ImageResize to 1920x1080 (16:9) or 1080x1920 (9:16). Billboard line art stays 1920x1080. Sequential GPU. Do NOT generate in chat. Do NOT fal. get_comfyui_status. If no JSON is uploaded, generation fails and Fal is not called.
- Mid-run switch: while Pictures/illustrations (or cover) are generating, set_image_provider (or Pictures UI / Settings when it drives the job) cancels the in-flight image (fal cancel, ComfyUI /interrupt, ChatGPT wait cleared), keeps completed PNGs, and restarts the current + remaining unfinished slots with the new generator. GPU lock releases so the new backend can acquire.
""".strip()


GPU_LOCK_REQUIREMENT = """
GPU LOCK (get_gpu_lock / get_studio_settings.gpu_lock / health.gpu_lock / hands_off.gpu_lock):
One GPU-using process at a time across MCP stdio and Studio http://127.0.0.1:7878 (user_data/gpu.lock).
Serialized: ComfyUI prompts, local Chatterbox TTS, Flux batches (so they cannot stack on Comfy/TTS),
and each job's pipeline image + audio + render GPU section. Images inside a job stay sequential.
Topic due-picker stays one job at a time and will not start job B's Comfy while job A holds the lock.
Never fire generate_illustrations + generate_speech(local) + start_job in parallel — wait for the lock
(timeout returns a clear error with holder + waiters). Call get_gpu_lock for {busy, holder, waiters}.
""".strip()


SAAS_MCP_REQUIREMENT = """
MULTI-TENANT SAAS (Phases 1–5):
HTTP /mcp auth (any one): ngrok Basic; owner MCP PIN (X-MCP-Pin / ?mcp_pin= / Bearer <pin> = admin);
Bearer bp_live_… API key (create_api_key; per-user projects + per-key rate limit, default 60/min —
owner agents: rate_limit=600+); Studio JWT.
Owner PIN behavior is unchanged. Tools: get_my_usage, list_api_keys, create_api_key, revoke_api_key,
get_queue_status (leases/attempts/block_reason), admin_overview, list_failed_jobs, retry_failed_job,
run_backup (admin/PIN only). Plan quotas may return error_code=quota_exhausted; API abuse → rate_limited;
oversized narration → upload_too_large (HTTP 413). Daily JSON backup under user_data/backups/.
""".strip()


def with_hook_outro_requirement(text: str) -> str:
    """Append hook+subscribe rules if a stale override omitted them."""
    blob = (text or "").lower()
    if "hook" in blob and "subscribe" in blob:
        return text or ""
    return (text or "").rstrip() + "\n\n" + HOOK_OUTRO_REQUIREMENT


def with_playbook_runtime_notes(text: str) -> str:
    """Keep hook/outro, script-regen/align, and no-TV-in-art notes if prompts.json is stale."""
    text = with_hook_outro_requirement(text)
    blob = (text or "").lower()
    if "regenerate_pictures" not in blob or "regenerate_audio" not in blob:
        text = (text or "").rstrip() + "\n\n" + SCRIPT_REGEN_REQUIREMENT
        blob = text.lower()
    if "no tv" not in blob and "do not draw a tv" not in blob:
        text = (text or "").rstrip() + "\n\n" + NO_TV_IN_ART_REQUIREMENT
        blob = text.lower()
    if "exact video title" not in blob and "title on the cover" not in blob and "title card" not in blob:
        text = (text or "").rstrip() + "\n\n" + COVER_TITLE_ON_IMAGE_REQUIREMENT
        blob = text.lower()
    if "covers_history" not in blob and "regenerate_cover" not in blob:
        text = (text or "").rstrip() + "\n\n" + COVER_TITLE_ON_IMAGE_REQUIREMENT
        blob = text.lower()
    if "get_text_provider" not in blob and "text_provider" not in blob:
        text = (text or "").rstrip() + "\n\n" + TEXT_PROVIDER_REQUIREMENT
        blob = text.lower()
    if "lmstudio" not in blob:
        text = (text or "").rstrip() + "\n\n" + TEXT_PROVIDER_REQUIREMENT
        blob = text.lower()
    if "comfyui" not in blob:
        text = (text or "").rstrip() + "\n\n" + IMAGE_PROVIDER_REQUIREMENT
        blob = text.lower()
    if "external_images_missing" not in blob and "image_provider=external" not in blob.replace(" ", ""):
        text = (text or "").rstrip() + "\n\n" + IMAGE_PROVIDER_REQUIREMENT
        blob = text.lower()
    if "cannot run headless" in blob or "cannot be unsupervised" in blob:
        text = (text or "").rstrip() + "\n\n" + UNSUPERVISED_REQUIREMENT
        blob = (text or "").lower()
    if "scheduled_at" not in blob and "unsupervised" not in blob:
        text = (text or "").rstrip() + "\n\n" + UNSUPERVISED_REQUIREMENT
        blob = (text or "").lower()
    if "get_gpu_lock" not in blob and "gpu lock" not in blob:
        text = (text or "").rstrip() + "\n\n" + GPU_LOCK_REQUIREMENT
        blob = (text or "").lower()
    if "bp_live_" not in blob and "get_my_usage" not in blob:
        text = (text or "").rstrip() + "\n\n" + SAAS_MCP_REQUIREMENT
        blob = (text or "").lower()
    if (
        "app image prompts" not in blob
        and "do not invent a new art style" not in blob
        and "do not invent a separate art style" not in blob
    ):
        text = (text or "").rstrip() + "\n\n" + APP_IMAGE_PROMPTS_REQUIREMENT
    return text


SCRIPT_RULES_DEFAULT = """
LAZYKH SCRIPT FORMAT (must be followed exactly)

You are writing a spoken narration for a lip-synced stick-figure explainer.
The tagged script is the source of truth. A raw script is derived from it by
stripping emotion tags, and by removing illustration/art-direction cues. Short
[topic] noun phrases stay as spoken words (brackets removed); long scene prompts
inside brackets are dropped from speech entirely.

1. EMOTION TAGS
- Allowed tags, and only these: <explain> <happy> <sad> <angry> <confused> <rq>
- Anything in triangle brackets is NOT spoken. TTS and Gentle never hear it.
- Put a tag at the start of a line when the emotion should change.
- The emotion sticks until the next tag (1 line or 100 lines later).
- Meanings:
  explain = default teaching energy, informative, not over-the-top
  happy = genuine excitement, delight, a cool reveal
  sad = unfortunate news, limits, disappointment
  angry = frustration, warning, indignation
  confused = puzzlement, complexity, "wait, that can't be right"
  rq = rhetorical question. Use only on a real question that is answered right after.
    This gives the stick figure a shrug-like pose.

2. TOPICS / BILLBOARDS
- Square brackets mark a SHORT spoken topic noun phrase only.
- Example: <explain> Despite being over 3 inches long, the [tarantula] is tiny next to the Sun.
- If a line has no square brackets, the whole spoken line is the topic.
- Reuse the same [topic] later to reuse the same illustration.
- Topics should be short noun phrases: [black hole], [ice cube], [train whistle]
- NEVER put illustration / scene / art-direction text in square brackets.
  Bad: [a hand tickling its own ribs, motion lines]
  Bad: [a brain wearing a director's cap, film set]
  Those leak into TTS. Put scene prompts in save_script(scenes=[...]) only
  (one scene string per non-empty line), or leave scenes empty and let Studio
  derive pictures from list_illustration_jobs after save.
- Do not write IMAGE:, ILLUSTRATION:, PROMPT:, SCENE:, or ART DIRECTION: lines
  in the script body. Square brackets are the only square brackets allowed.

3. LINE BREAKS
- Single line break = new pose AND a new billboard illustration.
- Keep lines short: about 8 to 16 spoken words. This is how the character moves.
- Double line break (a blank line) = new section. The screen flips and the
  background illustration changes. Use a double break every 25-45 seconds of speech.
- Double breaks are useful, not mandatory every time, but a minutes-long video
  should have several.

4. GENTLE DICTIONARY (lip-sync will fail on unknown words)
- Gentle only knows common English words.
- Never use rare proper nouns, brand-new slang, or fused compounds as a single token.
- Split compounds the way a dictionary would: "Mine craft" not "Minecraft",
  "you tube" not "YouTube", "caring es" if you must fake "Cary-ness".
- Write numbers as words: "twenty twelve" not "2012".
- Hyphens become spaces later, so prefer already-split phrasing.
- No URLs, no hashtags, no emoji, no markdown.

5. WHAT THE AUDIENCE HEARS
- Full spoken sentences people can follow out loud. Not telegraph fragments.
- Conversational explainer energy: a person talking to camera.
- Stay on the given topic. Analogies, scenes, objects, and names must come from that topic.
- Do not invent a different subject. Do not dump unrelated episode ideas.

6. HOOK (required - first thing the audience hears; ALSO the 9:16 short)
- Start immediately with a HOOK: 1 to 3 spoken lines that grab attention BEFORE the explainer body.
- Conversational, full spoken sentences. Not telegraph fragments. Not a generic "hey guys".
- Stay on THIS video's topic. The hook is about the given topic, not a greeting or channel intro.
- Use an appropriate emotion: often <rq>, <happy>, or <confused>.
- The hook is still normal lazykh tagged lines (emotion tags, optional [topics], short lines).
- Write the hook with shorts energy: it is rendered alone as the 9:16 short, then a channel CTA from script.shorts_cta is appended (9:16 only — never put that CTA in the 16:9 script).

7. OUTRO (required - last thing the audience hears on the 16:9 explainer)
- After the explainer body, a short subscribe closer.
- Must actually say the word "subscribe" (Gentle-friendly: it is a common word).
- Natural lazykh voice, not a robotic call-to-action dump.
- Example spirit: ask a follow-up, then <happy> If you want more of this, hit subscribe.
- Do not invent a different channel name unless the user provided one. Generic "subscribe" is fine.
- Keep it short: a couple of lines.
- Do NOT add the 9:16 "visit our channel in the link below" line here — that is script.shorts_cta for the short only.

8. TWO SCRIPT VERSIONS
You produce a TAGGED script (with <emotion> and short [topic] markers and line breaks).
The RAW script is the tagged script with:
- all <emotion> tags removed
- short [topic] brackets removed but inner words kept (spoken)
- illustration/scene brackets, art-direction parentheses, and IMAGE:/PROMPT: lines removed entirely (not spoken)
- hyphens turned into spaces
That raw text is what TTS speaks and what Gentle aligns. Never put image prompts in raw.

9. LENGTH
- Target about {words_per_minute} spoken words per minute.
- Honor the requested duration. A 60 second video is about 145 words.
- A 5 minute video is about 725 words.
- Count only spoken words, not tags.
- Hook and outro COUNT toward spoken words. They must fit inside the requested duration, not blow past it.
- Reserve a few seconds at the start for the hook, and about 8 to 12 seconds at the end for the outro.

10. ILLUSTRATION PROMPTS (NOT in the spoken script body)
Scene descriptions are SEPARATE from the tagged narration. For MCP writers:
pass them as save_script(..., scenes=[...]) — one subject-only scene string per
non-empty script line, in order. For OpenAI/lmstudio, scenes come from the
follow-up lines JSON call. Do NOT embed scene prompts in the tagged script
(no [long art descriptions], no IMAGE:/PROMPT: lines). Studio builds Flux/ComfyUI
/ChatGPT prompts from those scenes + art.* wrappers after save.
Every non-empty line gets a scene description of the SUBJECT only. Follow the job's video_layout:
- cover (default): FULL-FRAME COVER background. Match the job's video aspect exactly —
  never square, never a 4:5 panel, never a small inset billboard.
  16:9 jobs: generate exactly 16:9 at 1920x1080 landscape, full-bleed background.
  9:16 jobs: generate exactly 9:16 at 1080x1920 portrait, full-bleed background.
  The picture covers the entire video; the stick-figure narrator is composited on top afterward
  and must NOT appear in the generated illustration.
- billboard: 16:9 landscape at 1920x1080, fill the frame, no letterbox.
  Subject-only full-frame diagram/illustration of the topic (almost no readable text). Do NOT describe or draw a TV, monitor, bezel,
  stand, or screen-in-screen — the compositor pastes this PNG into a TV overlay later.
  On 16:9 the illustration sits on the left (speaker on the right; sides can swap on a section flip).
  On 9:16 the illustration sits on top (speaker at the bottom).
Scene text MUST describe only the topic (molecules, objects, diagrams, animals of the topic).
NO people, NO stick figure, NO mascot host, NO presenter, NO explaining character,
NO teacher pointing at a board. Do not mention ChatGPT, art-style paragraphs, or presets.
The finished prompt MUST state the correct ratio and pixel size for that layout, and MUST include this art style:

SHORT: {art_style_short}

FULL:
{art_style_full}

The scene must match the title and the story being written, and illustrate THAT line
(the topic in brackets if present). Use the job layout's frame (full-bleed cover, or 16:9
landscape 1920x1080 filling the frame with no letterbox — never a TV or monitor as an object). Vary the background color from line to line to fit the mood — never default every
illustration to orange. Almost no readable text in the image — prefer a clear full-frame diagram or illustration over handwritten notes or caption boards.
Do not draw a host explaining the picture.

11. TINY EXAMPLE (format only - hook, body, subscribe outro)
<rq> What if the [planets] I polished eight years ago still had a secret?
<explain> So.
I up loaded a video show casing my [polished planets]
<confused> cause I also added
<sad> [five] other planets
<explain> in twenty twelve!

<rq> but why am I returning
to this topic, eight years later?
<explain> Well, back then, I did not know how to share my world files [online].

<rq> Want the next round of [world files]?
<happy> If you want more of this, hit subscribe.
""".strip()


CHATGPT_PLAYBOOK_DEFAULT = """
You are helping produce a Stickman Automation lip-sync explainer through this MCP server.
Same tools as the Stickman Automation GUI at http://127.0.0.1:7878. FastMCP HTTP is POST /mcp
(streamable HTTP, same tools as stdio `python -m studio.mcp_server`).
HTTP /mcp auth (any one): ngrok HTTP Basic (Settings → Ngrok — Basic only, not Basic+Bearer),
owner MCP PIN (header X-MCP-Pin, query ?mcp_pin=, or Bearer <pin> — superuser/admin, unchanged),
per-user API key Authorization: Bearer bp_live_… (create_api_key; scoped projects + per-key rate
limit, default 60/min — owner agents use rate_limit=600+), or Studio JWT.
Remote tip: https://…/mcp with Basic credentials from Settings → Ngrok while the tunnel is running.
ChatGPT Desktop tip: https://…/mcp?mcp_pin=YOUR_PIN also works.
SaaS tools: get_my_usage, list_api_keys / create_api_key / revoke_api_key, get_queue_status,
admin_overview / list_failed_jobs / retry_failed_job / run_backup (admin/PIN only).
Machine-readable errors: quota_exhausted, rate_limited, upload_too_large, unauthorized.
ChatGPT Desktop, Claude Desktop, and Claude Code share this server.
Codex: mcp_servers.lazykh in ~/.codex/config.toml. Claude Desktop: mcpServers.lazykh
in claude_desktop_config.json (same command: python -m studio.mcp_server).
get_studio_settings returns mcp_build and mcp_tools (the live tool catalog).

CHATGPT DESKTOP CACHE: MCP tool schemas and handshake instructions are cached until
initialize. After a Studio/MCP update: fully quit ChatGPT Desktop (not just close the
window), reopen, type /mcp, start a NEW thread. Do not reuse an old chat.
Claude Desktop: quit and reopen after MCP updates so it reloads stdio tools.

MUST — TOPIC HOOK + SUBSCRIBE OUTRO
Every tagged script, including ones you write yourself and save with save_script, MUST:
- Open with a HOOK: 1 to 3 spoken lines on THIS topic before the explainer body. Not a generic greeting.
  The hook IS the 9:16 short (shorts energy). generate_9x16=true (default) materializes script_9x16
  (hook + script.shorts_cta), 1080x1920 portraits in script_9x16_billboards, and script_9x16.wav/json.
- Close with a subscribe OUTRO that actually says the word "subscribe" (YouTube-style, still on-topic).
  That outro is for the 16:9 full explainer only — never put the 9:16 channel-link CTA in the full script.
Hook and outro count toward duration: reserve a few seconds at the start and about 8 to 12 seconds at the end.
generate_script_via_api fails validation without a subscribe outro. save_script saves but returns script_warnings if hook or outro is missing. Do not skip this shape.
Script keys: script.hook_outro, script.shorts_cta (editable 9:16-only CTA).

TEXT PROVIDER (get_text_provider / set_text_provider; settings text_provider, alias script_provider)
openai | chatgpt | claude | lmstudio. Call get_text_provider before writing a script or inventing topics.
- openai: billed OpenAI chat API. Call generate_script_via_api. generate_topics invents titles via API.
  Requires confirm_spend=true or spend_confirm_id from request_spend_confirm.
- chatgpt: YOU are ChatGPT Desktop. Write the tagged script yourself (hook + subscribe outro, emotion tags) then save_script with youtube_description, youtube_keywords, and youtube_hashtags. Do NOT call generate_script_via_api.
- claude: YOU are Claude Desktop / Claude Code on the same stdio server. Write the tagged script yourself then save_script (include YouTube publish metadata). Do NOT call generate_script_via_api.
- lmstudio: local OpenAI-compatible API (default http://127.0.0.1:1234/v1). Studio generates via the local LLM — no OpenAI cloud, no spend confirm. YOU must call generate_script_via_api (and generate_topics) rather than writing the body yourself. Optional lmstudio_model; empty uses the first loaded model from GET /v1/models. If it fails, start the LM Studio local server.
Topics: openai or lmstudio → generate_topics. chatgpt/claude → invent titles, then create_topic and/or schedule_topic. Do not call generate_topics unless provider is openai or lmstudio.
SPEND GUARD: Never silently bill OpenAI cloud or fal. Call request_spend_confirm(action) then pass spend_confirm_id, or pass confirm_spend=true on generate_script_via_api / generate_topics / generate_speech / generate_illustrations_with_flux / generate_illustrations / generate_cover / regenerate_illustration when those hit OpenAI or Flux. get_spend_status, get_audit_log (user_data/audit.log).
UNSUPERVISED: set_hands_off(true) then walk away, or schedule_topic(..., scheduled_at='YYYY-MM-DDTHH:MM'). Naive times are this PC's local zone. Studio's ~30s due-picker starts queued topics when due ONLY while hands_off is on (FIFO by scheduled_at, one at a time). Hands-off also auto-generates topics (openai/lmstudio) and auto-schedules drafts; each job uploads to YouTube as private (per-job override). text openai/lmstudio auto-writes the script; chatgpt/claude must save_script in the same turn. Pictures: flux, comfyui, or external (MCP save_illustration_image for every slot — like external TTS; missing → EXTERNAL_IMAGES_MISSING). chatgpt pictures use the same upload path. hands_off() / set_hands_off / list_topics / start_topic_pipeline / restart_api.
GPU: never fire illustrations + local TTS + another job in parallel. Wait for get_gpu_lock ({busy, holder, waiters}). ComfyUI, Chatterbox, Flux batches, and pipeline image/audio/render are one-at-a-time (user_data/gpu.lock).

LIVE TEXT: get_chatgpt_playbook and get_script_rules re-read user_data/prompts.json
on every call. Prefer those tools over a handshake snapshot from Studio start.
get_studio_settings / list_prompts are also live.

SETTINGS (get_studio_settings / update_studio_settings)
- Secrets (fal_key, openai_api_key, elevenlabs, youtube_client_secret, …) are redacted as
  ******** with *_set flags. Passing empty / ******** / omitted never wipes a stored key.
- text_provider / script_provider: 'openai' (billed API), 'chatgpt' (ChatGPT Desktop MCP),
  'claude' (Claude Desktop / Claude Code MCP), or 'lmstudio' (local OpenAI-compatible API).
  get_text_provider / set_text_provider. Native writers (chatgpt, claude) must save_script
  themselves and must not call generate_script_via_api. If provider is lmstudio, Studio
  generates via the local LLM — ChatGPT/Claude should call generate_script_via_api rather
  than writing the body themselves. update_studio_settings(lmstudio_base_url, lmstudio_model).
- image_provider: 'flux' (fal-ai/flux-2), 'chatgpt' (native in-chat / MCP upload),
  'comfyui' (local ComfyUI), or 'external' (MCP/agent upload only — like external TTS).
  This is the Studio Settings default. A job can override
  it (get_image_provider / set_image_provider with project_id). NEVER assume Flux and
  NEVER always call fal. Cover + line illustrations MUST follow this setting.
  external/chatgpt: YOU generate then save_illustration_image; missing slots →
  EXTERNAL_IMAGES_MISSING. comfyui = local ComfyUI at comfyui_url (default http://127.0.0.1:8188). Upload
  File → Save (API Format) JSON in Settings (user_data/comfyui_workflow.json) or
  save_comfyui_workflow. Sizes: 16:9 → 1920x1080, 9:16 → 1080x1920 (never square).
  Billboard line art stays 1920x1080 even on 9:16 video. Cover always matches that
  cover's aspect. Sequential GPU jobs, not a storm of parallel prompts.
  get_comfyui_status. Call generate_illustrations (or generate_illustrations_with_flux) —
  Studio POSTs the uploaded API JSON to ComfyUI and does not fal. If no workflow is
  uploaded, Studio errors and does not call Fal.
- video_layout: 'cover' vs 'billboard' (see LAYOUTS). Studio default via
  update_studio_settings(video_layout=...) or set_video_layout(layout).
  Per-job: set_video_layout(layout, project_id).
- character_size: 'large' (current, scale 1.0), 'medium' (half), or 'small' (1/3). Feet stay bottom-anchored. update_studio_settings(character_size=...) or set_character_size(size); per-job set_character_size(size, project_id).
- include_bubblehead: true (default) composites the yellow stick-figure narrator; false keeps cover/line art (or billboard TV + studio room), audio, and music without the character, and uses full-frame cover line prompts (no host gap). set_include_bubblehead(enabled, project_id) or render_final_video(..., include_bubblehead=) or PATCH include_bubblehead.
- stickman_head_color: studio-wide Bubblehead fill (#RRGGBB). get_stickman_head_color / set_stickman_head_color(color) / reset_stickman_head_color — recolors every pose*.png; shadow auto H−10.4° / L−8.4. Free. Re-render to update finished videos.
- default_aspect / aspect: '16:9' landscape, '9:16' portrait/shorts, or 'both'
  (Render writes script_final_16x9.mp4 then script_final_9x16.mp4). New jobs
  inherit Settings default_aspect unless create_video_project(..., aspect=) or
  set_video_aspect is used.
- music_volume_pct: 0–100. Loops under the voice for the WHOLE video, including the
  5-second title card. 0% is silent. Default 15%.
- fal_key / fal_key_set: OPTIONAL. Only Flux jobs need a fal key (Settings or env
  FAL_KEY / FAL_API_KEY). ChatGPT native and ComfyUI jobs never call fal and do not
  need FAL_KEY. ComfyUI needs the uploaded API JSON + a running ComfyUI server.
- voices: tts_provider openai | elevenlabs | local (Resemble Chatterbox; aliases resemble/chatterbox).
  voice_provider is kept in sync. openai_voice, elevenlabs_voice_id, local_voice (default English).
  list_tts_voices(provider) lists choices, including local. Local never bills OpenAI TTS.

STUDIO ROOM (clock, wall, floor behind the stick figure — not the generated illustration)
- list_backgrounds: PNG/JPG files in the original lazykh backgrounds/ folder, plus
  extras dropped in user_data/backgrounds/.
- set_project_background(project_id, filename) stores meta.json background_file.
  New jobs default to bga0.png (or the first file), not random.
- Billboard layout composites this room at full color behind the compositor TV overlay.
  Generated line PNGs must not include a TV. Cover layout stays full-bleed illustration art;
  the same pick is used only if a room is visible as a fallback.

LAYOUTS (cover vs billboard)
- cover (default): illustration is a full-bleed COVER filling the entire video frame
  at the job aspect (16:9 = 1920x1080, 9:16 = 1080x1920). Stick-figure narrator is
  composited on top afterward.
- billboard: compositor TV overlay. Generate line art as 16:9 (1920x1080) full-bleed
  subject-only full-frame diagrams/illustrations (fill the frame, no letterbox, almost no readable text). Do NOT draw a TV, monitor, bezel,
  stand, or screen-in-screen in the PNG — videoDrawer pastes the art into the TV hole later.
  The stick-figure still stands beside/below. On 16:9 LEFT (speaker RIGHT; sides can swap
  on a section flip). On 9:16 TOP (speaker BOTTOM).
The 5-second topic title-card is ALWAYS full-bleed cover art, even when line
illustrations use billboard layout.

IMAGE SIZES (ChatGPT native image tool — pick the matching preset in the image UI)
Never square, never 1:1, never 4:5. ALWAYS call list_illustration_jobs (or get_project)
first and read aspect / project_aspect (16:9, 9:16, or both) — do not assume 16:9.
list_illustration_jobs returns width, height, aspect, image_size, chatgpt_preset, and
generate_at on EVERY job — obey those pixels. Cover slots listed are only those the
project needs (both / generate_9x16 includes landscape + portrait covers).
- Cover (script_cover_16x9.png at 1920x1080 AND/OR script_cover_9x16.png at 1080x1920):
  Never stretch 16:9 into 9:16. Each aspect has its own file. kind=cover.
  16:9 job / slot → 16:9 landscape preset, 1920x1080 → script_cover_16x9.png
  9:16 job / slot → 9:16 portrait preset, 1080x1920 → script_cover_9x16.png
  both → generate both cover files at their native sizes
- Cover-layout line art: same as the job (16:9 → 1920x1080, 9:16 → 1080x1920)
- Billboard line art: ALWAYS 16:9 landscape preset at 1920x1080, fill the frame, no letterbox,
  even when the video is 9:16. On a 9:16 billboard video the 16:9 illustration sits at the top;
  only the title-card cover uses 9:16 1080x1920. Do not draw a TV in the PNG.

COVER (always the first clip, 5 seconds, per aspect)
- Files: script_cover_16x9.png (1920x1080) and script_cover_9x16.png (1080x1920).
  Rendering 9:16 regenerates the 9:16 cover if it is missing or the wrong size —
  it does not stretch the 16:9 still, and it does not overwrite script_cover_16x9.png
  or script_final_16x9.mp4 (and the reverse). script_final.mp4 is a copy of the last
  render. kind='cover'.
- Composition (Stickman Automation studio title card): match selected background_file room;
  yellow Bubblehead OUTSIDE left (16:9) or lower (9:16) in a unique pose; large TV/billboard
  with the exact video title as bold on-screen headline text + topic art on the screen.
  Presenter never inside the TV. Cover prompts from list_illustration_jobs / images.cover_intro
  already include this (Studio injects background, pose, title-on-billboard).
- History: regenerating archives under user_data/projects/{id}/covers_history/{16x9|9x16}/vNNN.png.
  regenerate_cover / list_cover_versions / set_active_cover. Active render path stays
  script_cover_16x9.png / script_cover_9x16.png.
- Line billboards (NOT the cover): subject only — NO people/stick figure/host; NO TV/monitor
  as an object (compositor pastes line art into the TV later). Lip-sync frames composite
  Bubblehead when include_bubblehead is true; the 5s cover still already includes Bubblehead.
- Flux: generate_cover / regenerate_cover / generate_illustrations_with_flux (confirm_spend).
  Studio sends SHORT fal prompt (images.cover_intro for covers). Needs FAL_KEY.
- ComfyUI: generate_cover / regenerate_cover / generate_illustrations (no fal).
- ChatGPT: native image at the matching preset, paste cover prompt verbatim, then
  save_illustration_image(filename='script_cover_16x9.png' or 'script_cover_9x16.png',
  kind='cover'). Line slots use b001.png, b002.png, … — never ChatGPT's long image title.
  Do NOT call generate_cover / generate_illustrations_with_flux for ChatGPT. No FAL_KEY.

JOBS / LIBRARY
- list_video_projects / list_library: Studio jobs with status, thumbs, has_cover /
  has_audio / has_video. thumbnail_url hits Studio /api/projects/{id}/thumbnail.
- create_video_project(topic, duration_seconds, title, aspect)
- get_project(project_id)
- get_file(project_id, kind=video|audio|cover|script_tagged|illustration|…) or
  get_file(path='projects/…'): returns utf-8 text + base64 when ≤15MB, else a short-lived
  /api/mcp/download/{token} URL. Roots: projects/, music/, user_data/ (allowlisted logs),
  backgrounds/. Refuses auth.json, settings.json, tokens/secrets.
- delete_project(project_id, delete_files=True): STOPS the job if running or queued, then
  permanently deletes user_data/projects/{id} and all assets (scripts, audio, frames,
  billboards, mp4, thumbs, meta), purges queue rows, and unlinks Topics. ALWAYS confirm
  with the user before calling. Pass delete_files=False only to soft-hide while keeping
  files (still stops a live run). Same as the Studio library × delete.
- rename_video(project_id, title, update_youtube=true, rename_folder=false): set Studio
  meta title; when already uploaded, also rename the YouTube listing. rename_folder
  moves the project folder (and Topics job_id) — only when the job is idle.
- start_job(project_id): run the pipeline from empty or the first incomplete step
  (script → cover/illustrations → wav → Gentle → frames → script_final.mp4 → optional
  YouTube). If a worker is already live, attaches (no duplicate thread).
- pause_job(project_id) / stop_job(project_id): cooperative halt. Sets running=false
  immediately. The worker exits at the next pipeline step boundary (does not kill
  ffmpeg mid-frame). Pause marks the job paused; Stop marks it stopped. Resume
  continues from completed artifacts. If you resume while the current step is still
  finishing, the same thread continues (no duplicate).
- resume_job(project_id): continue from the last successful artifact (script →
  cover/illustrations → wav → Gentle JSON → frames → script_final.mp4 → optional
  YouTube auto-upload). Skips files that already exist. If the job is running,
  attaches (no duplicate thread). Failed/stopped/paused jobs continue.
  Returns resumed_from (the first incomplete step).

TOPICS (batch generate + pipeline queue — Topics page and MCP)
- generate_topics(seed?, count=8, duration_min=2): when text_provider is openai or lmstudio.
  Invents titles+angles using the live topics.generate prompt (Prompts page) and
  saves drafts in user_data/topics.json. openai bills cloud; lmstudio is local (no OpenAI).
  If text_provider is chatgpt or claude, this tool
  returns use_mcp + playbook and does not call OpenAI — invent topics yourself.
- create_topic(title, angle?, duration_min=2): save one draft without OpenAI (native path).
- list_topics(status=draft|queued|running|done) includes scheduled_at, scheduled_at_local, due, next_due_at, auto_scheduler, timezone.
- schedule_topic(topic_id) OR schedule_topic(title, duration_min, angle?, scheduled_at?, run_now?): creates a
  Studio job (same as New job with topic+duration; Settings default_aspect, including
  both) and queues start_job — script (hook+subscribe, generate_script_via_api when
  openai/lmstudio) → pictures (flux/comfyui; chatgpt falls back to flux if fal_key) →
  audio → Gentle → render → optional YouTube auto-upload.
  scheduled_at is ISO datetime; naive values are this PC's local timezone, stored UTC.
  Empty scheduled_at or run_now=true means now. With hands_off on, Studio checks about every 30s and starts
  the next due queued topic (FIFO by scheduled_at) if no job is running or paused. Without hands_off,
  queued topics wait until Hands-off is enabled or you use Run now.
  GPU lock: will not start job B's Comfy/TTS while job A (or MCP illustrations/local TTS) holds
  user_data/gpu.lock. Call get_gpu_lock. Never fire illustrations + local TTS + another job in parallel.
  auto_scheduler defaults ON but only auto-starts while hands_off is also on.
- start_topic_pipeline: alias of schedule_topic (run_now defaults true).
- hands_off: unsupervised readiness + one-shot recipe. Does not start a job.
- set_hands_off(enabled, interval_hours?, interval_minutes?, min_queue?): persist hands_off. When on, the ~30s
  loop generates topics if drafts+queued < hands_off_min_queue (default 5; skip auto-gen for chatgpt/claude), auto-schedules
  drafts (0 hours = due now FIFO), runs the pipeline, and sets each job's youtube_privacy=private
  + youtube_auto_upload (Settings defaults unchanged). YouTube disconnected → still render, upload pending.
- update_topic(topic_id, title?, angle?, duration_min?, scheduled_at?): same as PATCH /api/topics/{id}. Does not start the pipeline.
- unschedule_topic(topic_id): clear scheduled_at, drop from the FIFO queue, return status to draft. Keeps title/angle/job_id. Same as POST /api/topics/{id}/unschedule.
- restart_api: from stdio MCP, spawn python run_studio.py and recycle only 7878. Does not kill
  Gentle 8766, VoiceSync 8765, or Electron (window reattaches). Waits for /api/health 200.
  Clears stale gpu.lock if the holder pid is dead.
- ngrok_status / start_ngrok / stop_ngrok: Studio reserved-URL tunnel (basic auth if configured).
  start/stop only affect the Studio-owned ngrok process (same as /api/ngrok).
- delete_topic(topic_id) removes the topic card; it does not delete the job folder.
Playbook: set_hands_off(true) and walk away. Or generate a batch then schedule_topic with a datetime. For chatgpt/claude text, save_script in the same turn before walking away.

MUSIC
- list_music: uploaded tracks in user_data/music (also returns music_volume_pct).
- set_job_music(project_id, music_id) locks a specific track; mode='shuffle' clears the lock for auto-pick at render.
- Render picks one track at random per job (unless locked) and loops it under speech + the 5s cover.
  First pick is kept on rerender unless you shuffle or lock another track.
- shuffle_job_music(project_id) re-rolls the track; it does not re-render.
- Volume is Settings music_volume_pct, not per-track.

YOUTUBE (system browser OAuth, no popup; multiple channels supported)
- Google Cloud OAuth redirect URI must include exactly:
  http://127.0.0.1:7878/api/youtube/oauth/callback
  (Desktop app client, or Web client with that Authorized redirect URI).
- youtube_connect: ADDS a YouTube channel (existing stay). Returns auth_url + studio_connect_url.
  OPEN THAT URL in the system/default browser. MCP cannot show a popup. Studio listens for the
  callback. If Google lists several channels for that sign-in, finish with set_youtube_channel.
- youtube_finish_oauth(code= or url=): if the browser shows a code/redirect URL instead of
  auto-callback, paste it here (same as POST /api/youtube/oauth/code).
- youtube_status / list_youtube_channels: lists connected channels (each with its own token) and
  which is_default. set_youtube_channel(channel_id) sets the workspace default.
- youtube_disconnect(channel_id?): remove one channel, or all when omitted.
- update_studio_settings(youtube_auto_upload=true|false, youtube_privacy=private|unlisted|public,
  hands_off=true|false, hands_off_interval_hours=)
  sets Studio defaults. Auto-upload default privacy is unlisted. Hands-off does not change that
  default — it overrides youtube_privacy=private on hands-off jobs only.
- set_project_youtube(project_id, youtube_auto_upload?, youtube_privacy?, youtube_channel_id?)
  overrides auto-upload / channel for ONE job (same as GUI PATCH).
- upload_to_youtube(project_id, privacy_status=..., title?, description?, tags?, aspect?, channel_id?)
  uploads a finished mp4. When multiple channels are connected, ASK the user which account/channel
  and pass channel_id (required unless the job already has youtube_channel_id). Prefer aspect
  16:9 or 9:16 when both renders exist. Default privacy is unlisted. Uses stored meta
  youtube_description / youtube_keywords / youtube_hashtags when description/tags are omitted.

PROMPTS PAGE KEYS (list_prompts / get_prompts / update_prompt / save_prompts / reset_prompt)
Overrides live in user_data/prompts.json and apply on the next generate — no restart
except the FastMCP handshake snapshot.
Script: script.rules, script.hook_outro, script.json_instructions, script.youtube_meta,
  script.user_template, script.shape_cover, script.shape_billboard
Art: art.style_short, art.style_full, art.scene_wrapper, art.color_instruction,
  art.constraints_suffix, art.framing_cover_landscape, art.framing_cover_portrait,
  art.framing_billboard_landscape, art.framing_billboard_portrait,
  art.ratio_cover, art.ratio_billboard
Images / pipeline: images.shape_cover, images.shape_billboard, images.cover_intro
  (5s title card), images.flux_instructions (SHORT fal-ai/flux-2 scene prompt;
  never concatenate this playbook into fal), images.chatgpt_instructions (ChatGPT
  native / MCP only — not sent to fal)
TTS: tts.openai_instructions
Topics: topics.generate (Topics page + MCP generate_topics)
MCP: mcp.chatgpt_playbook
Art also includes art.no_character (no presenter in generated PNGs).
Image prompts for cover + lines come from this live catalog (list_illustration_jobs
returns them already wrapped with art.* / images.flux_instructions / images.cover_intro /
images.chatgpt_instructions). Do not invent a separate art style. For ChatGPT native,
paste jobs[].prompt as-is, then save_illustration_image so the slot appears on Pictures.

WORKFLOW
1. Call get_script_rules (live) and obey every format rule.
2. create_video_project or get_project. Aspect 16:9 unless Settings default_aspect
   or the user wants 9:16. Layout cover vs billboard from Settings / set_video_layout.
3. Check get_text_provider.
   If openai or lmstudio: generate_script_via_api (regenerate_pictures=true, regenerate_audio=true by default).
   openai requires confirm_spend=true or spend_confirm_id; lmstudio is local — do not write the script body yourself.
   If chatgpt or claude: write the tagged script yourself and save_script(project_id,
   script_tagged, summary?, scenes?=[one subject-only scene per non-empty line],
   youtube_description=, youtube_keywords=, youtube_hashtags=).
   Do not call generate_script_via_api (that spends OpenAI tokens).
   Self-written scripts still need HOOK + body + subscribe OUTRO. save_script derives
   raw for TTS (strips emotion tags AND any illustration/art cues if they were
   mistakenly left in brackets). Put image prompts in scenes=, never in the spoken body.
   Always include YouTube publish metadata with the script so uploads use it.
   generate_script_via_api always overwrites script files and drops stale Gentle json / schedule /
   frames / mp4. Pictures true: Flux/ComfyUI overwrite every cover+line PNG; ChatGPT marks all slots
   needs_regen. Audio true: new wav then Gentle align immediately (fresh script.json) before
   scheduler/render. False leaves those artifacts (skip-if-exists).
4. Call get_image_provider(project_id) and list_illustration_jobs FIRST.
   Read aspect / project_aspect (16:9, 9:16, or both) and each job's width/height —
   never assume 16:9. generate_illustrations* / generate_cover / regenerate_illustration
   size from that Studio project aspect (and generate_9x16 shorts flags).
   APP PROMPTS ONLY: never invent an art style. Flux/ComfyUI inject images.flux_instructions + art.*;
   ChatGPT must use jobs[].prompt / instructions from list_illustration_jobs (or regenerate_illustration).
   - flux: generate_illustrations_with_flux (cover + missing lines; confirm_spend or spend_confirm_id). Optional generate_cover.
     Studio uses the fal client so the job survives GUI refresh. Poll get_render_status.
     Do not draw images in chat. Do not use a separate fal MCP.
   - comfyui: generate_illustrations (or generate_illustrations_with_flux — Studio POSTs
     to ComfyUI and does not fal). Local ComfyUI, 1920x1080 / 1080x1920 from aspect.
     Upload API JSON in Settings first. Sequential GPU.
   - chatgpt: native image tool only. Cover FIRST (script_cover_16x9.png and/or
     script_cover_9x16.png as listed for this project, kind='cover') at that canvas
     (never stretch 16:9 into 9:16). Cover prompt requires the exact video title as
     large readable lettering on the artwork — paste jobs[].prompt as-is,
     then each missing line at that job's width/height/image_size/generate_at
     (billboard lines stay 16:9 1920x1080 even on 9:16 video; no forced title on lines).
     save_illustration_image after each so Pictures /
     list_illustration_jobs see the file under user_data/projects/{id}/ immediately.
     Never generate_cover / generate_illustrations_with_flux. No FAL_KEY.
   To redo one existing picture: regenerate_illustration(project_id, filename=...).
     flux queues fal for that file only. comfyui queues the uploaded workflow. chatgpt marks jobs[].needs_regen and returns
     the app prompt plus save_illustration_image filename/kind. Do not regenerate the whole set.
   Never call DALL-E, gpt-image, or the OpenAI Images API.
5. generate_speech (Settings tts_provider: OpenAI, ElevenLabs, or local Resemble Chatterbox).
   OpenAI TTS needs confirm_spend or spend_confirm_id. list_tts_voices first if needed. Local never falls back to paid OpenAI TTS.
   Always writes a new wav THEN aligns phonemes with Gentle (fresh script.json) before any
   scheduler / videoDrawer / render. Do not skip align after audio.
6. start_gentle / ensure_gentle (aliases: ensure_gentle_docker, start_gentle), then align_phonemes, then render_final_video.
   stop_gentle stops Docker Gentle and/or the local process Studio owns.
   Render prepends the 5s topic cover as the first clip, then the lip-sync explainer,
   and loops library music under the voice. Pass layout= / aspect= to override for
   that render; shuffle_music=true re-rolls the track.
7. Poll get_render_status. Return the output mp4 path when done.
   If Settings youtube_auto_upload is on and YouTube is connected, render also uploads
   (default unlisted). Upload failure is logged on the job; the mp4 still succeeds.
   To start a new/empty job: start_job(project_id).
   To halt a running job: stop_job or pause_job (cooperative, next step boundary).
   To continue a failed, paused, or stopped job without starting over: resume_job(project_id).
8. To publish later: upload_to_youtube(project_id, privacy_status="private"|"unlisted"|"public",
   aspect="16:9"|"9:16"). Uses stored youtube_description / keywords / hashtags from the script.

COMPLETE MCP TOOL LIST (do not invent names outside this list; stdio and HTTP /mcp match):
search, fetch, get_file,
get_script_rules, get_chatgpt_playbook, get_studio_settings, get_health, check_dependencies, get_gpu_lock, restart_api,
ngrok_status, start_ngrok, stop_ngrok,
get_text_provider, set_text_provider,
list_prompts, get_prompts, update_prompt, save_prompts, reset_prompt,
request_spend_confirm, get_spend_status, get_audit_log,
get_my_usage, list_api_keys, create_api_key, revoke_api_key, get_queue_status,
admin_overview, list_failed_jobs, retry_failed_job, run_backup,
generate_topics, create_topic, list_topics, update_topic, schedule_topic, unschedule_topic, start_topic_pipeline, hands_off, set_hands_off, delete_topic,
create_video_project, set_video_aspect, list_video_projects, list_library,
delete_project, rename_video, get_project,
generate_script_via_api, save_script,
list_illustration_jobs, get_image_provider, set_image_provider,
get_comfyui_status, save_comfyui_workflow, delete_comfyui_workflow,
get_video_layout, set_video_layout, set_art_style, list_art_styles, set_character_size, set_include_bubblehead,
get_stickman_head_color, set_stickman_head_color, reset_stickman_head_color, set_project_voice,
generate_illustrations_with_flux, generate_illustrations, generate_cover, regenerate_cover,
list_cover_versions, set_active_cover, regenerate_illustration,
save_illustration_image, save_illustration_images,
get_cover_provider, set_cover_provider,
list_tts_voices, generate_speech, upload_narration_audio,
ensure_gentle, ensure_gentle_docker, start_gentle, stop_gentle, get_gentle_status, align_phonemes,
render_final_video, get_render_status,
start_job, pause_job, stop_job, resume_job,
list_music, set_job_music, shuffle_job_music,
list_backgrounds, set_project_background,
youtube_connect, youtube_finish_oauth, youtube_status, youtube_disconnect,
list_youtube_channels, set_youtube_channel, set_project_youtube, upload_to_youtube,
update_studio_settings.

{script_rules}
""".strip()


def script_rules() -> str:
    from studio.prompts import get_prompt

    return get_prompt("script.rules")


def chatgpt_playbook() -> str:
    from studio.prompts import get_prompt

    return get_prompt("mcp.chatgpt_playbook")

