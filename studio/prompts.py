"""Editable prompt catalog. Defaults live here / in domain modules; overrides in user_data/prompts.json."""

from __future__ import annotations

import contextvars
import json
import re
import threading
from pathlib import Path
from typing import Any

from studio.art_style import ART_STYLE_FULL, ART_STYLE_SHORT
from studio.paths import PROMPTS_PATH, PROMPTS_USERS_DIR, ensure_dirs
from studio.script_rules import CHATGPT_PLAYBOOK_DEFAULT, HOOK_OUTRO_REQUIREMENT, SCRIPT_RULES_DEFAULT, WORDS_PER_MINUTE
from studio.utils_script import DEFAULT_SHORTS_CTA

_lock = threading.Lock()
_cache: dict[str, Any] = {}
_prompt_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bubblepod_prompt_user_id", default=None
)

_PLACEHOLDER_TO_KEY = {
    "art_style_short": "art.style_short",
    "art_style_full": "art.style_full",
    "script_rules": "script.rules",
    "color_instruction": "art.color_instruction",
    "constraints_suffix": "art.constraints_suffix",
    "no_character": "art.no_character",
}

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


JSON_INSTRUCTIONS_DEFAULT = """
Return ONLY a single JSON object (no markdown fences, no commentary) with keys:
- title: short video title
- summary: 2-4 sentences describing the whole story, used as illustration context
- youtube_description: YouTube video description for this topic/script (2-5 short
  paragraphs, plain text). Include a one-line hook, what the video covers, and a soft
  subscribe CTA. Do NOT put spoken script lines here. You MAY end with hashtags.
- youtube_keywords: array of 8-15 short YouTube search tags (strings, no #). Relevant
  to THIS topic only. No duplicates. Prefer specific phrases over generic words.
- youtube_hashtags: array of 3-8 hashtags WITH leading # (e.g. "#AI", "#Science").
  Match the topic; safe for the end of the description.
- script_tagged: the full tagged script as one JSON string. Escape line breaks as \\n
  (never raw line breaks inside the string). Escape any double quotes as \\".
- lines: array of objects, one per NON-EMPTY script line, in order, each with:
    tagged: that line including any <emotion> and [topic] markers
    scene: a concrete FULL-FRAME illustration or diagram of THIS line's subject only
           (objects, molecules, cutaways, processes, animals of the topic). Prefer one
           clear visual that fills the frame; almost no readable text or labels.
           Never a person, stick figure, mascot host, presenter, teacher, or
           "character explaining". Do not mention art-style, ChatGPT, aspect presets,
           or a narrator.
           Follow the video_layout for this job:
           cover = a full-bleed COVER background at the video's exact aspect
           (16:9 = 1920x1080 landscape, or 9:16 = 1080x1920 portrait — never square or 4:5).
           billboard = a 16:9 landscape 1920x1080 subject-only illustration, fill the frame,
           no letterbox. Do not mention a TV, monitor, bezel, stand, or screen-in-screen
           (that overlay is composited later).
           Do not repeat the art-style paragraph here; only the scene.

script_tagged MUST be hook, then explainer body, then subscribe outro.
No trailing commas, no comments, no single-quoted keys. script_tagged must match the
tagged fields joined by \\n, using a blank line (\\n\\n) where a section/background
change should happen.
""".strip()

YOUTUBE_META_INSTRUCTIONS_DEFAULT = """
Also produce YouTube publish metadata consistent with the topic and script:
- youtube_description: full description (hook + what viewers learn + soft subscribe CTA).
- youtube_keywords: 8-15 tags without # for the YouTube Data API tags field.
- youtube_hashtags: 3-8 hashtags with # for the visible end of the description.
Keep them on-topic, family-friendly, and free of clickbait spam.
""".strip()

SCRIPT_USER_TEMPLATE_DEFAULT = """
Topic: {topic}
Target duration: {duration_seconds} seconds (about {target_words} spoken words at {words_per_minute} wpm).
Video aspect: {aspect}. Video layout: {layout}. {shape}
Write a complete explainer on THIS topic only.
Required shape: HOOK, then explainer body, then subscribe OUTRO.
- HOOK first: 1 to 3 spoken lines on THIS topic. Reserve a few seconds at the start. Not a generic greeting.
- BODY next: the explainer.
- OUTRO last: a natural closer that actually says subscribe. Reserve about 8 to 12 seconds at the end.
Hook and outro count toward the {target_words} spoken words. Stay inside the target length.
Do not invent a channel name unless the user provided one.
{extra_block}
""".strip()

SCRIPT_SHAPE_COVER_DEFAULT = (
    "The finished video is {phrase}. Each illustration MUST be generated at {phrase}, "
    "full-bleed COVER background covering the whole picture. {size_line}. Not square, not 4:5."
)

SCRIPT_SHAPE_BILLBOARD_DEFAULT = (
    "The finished video uses the BILLBOARD layout: each line illustration is {phrase}. "
    "{size_line}. Full-bleed 16:9 subject-only diagram/illustration filling the frame, "
    "no letterbox, almost no readable text. "
    "Do not draw a TV, monitor, bezel, stand, or screen-in-screen (composited later). "
    "Not a full-bleed cover of the whole video frame. "
    "On 16:9 the illustration sits left (speaker right); on 9:16 it sits top (speaker bottom)."
)

SCENE_WRAPPER_DEFAULT = """
{art_style_short}

Finished video aspect: {aspect} ({video_w}x{video_h})
Generate this illustration at: {width}x{height} — ChatGPT image UI preset {gen_aspect} (never square, never 1:1, never 4:5)
Video layout: {layout}
{ratio_line}

Scene to draw: {scene}

{no_character}

{color_instruction}

{framing}{constraints_suffix}
""".strip()

COLOR_INSTRUCTION_DEFAULT = (
    "Pick a distinct flat background color for this line from the scene's mood "
    "(sky, mint, cream, coral, lavender, yellow, charcoal, forest, night blue, etc.). "
    "Do not default to orange. Consecutive lines should not share the same fill."
)

CONSTRAINTS_SUFFIX_DEFAULT = (
    "no watermarks; no UI chrome; no photorealism; no 3D render; no heavy shading. "
    "Flat cartoon colors. Almost no readable text or handwritten labels — prefer a full-frame diagram/illustration."
)

FRAMING_COVER_LANDSCAPE_DEFAULT = (
    "Constraints: {size_line}. The image MUST be {phrase}, full-bleed, covering the entire "
    "video frame as the COVER background (not square, not 4:5, not a framed billboard). "
    "Fill most of the left and center with one clear diagram or illustration of the topic; "
    "keep the right third as empty negative space (do not draw a person or host there). "
    "Almost no readable lettering."
)

FRAMING_COVER_PORTRAIT_DEFAULT = (
    "Constraints: {size_line}. The image MUST be {phrase}, full-bleed, covering the entire "
    "video frame as the COVER background (not square, not 4:5, not a small inset or framed billboard). "
    "Fill most of the upper area with one clear diagram or illustration of the topic; "
    "keep the lower third as empty negative space (do not draw a person or host there). "
    "Almost no readable lettering."
)

# Used when include_bubblehead is false — no host gap; fill the whole frame.
FRAMING_COVER_LANDSCAPE_FULL_DEFAULT = (
    "Constraints: {size_line}. The image MUST be {phrase}, full-bleed, covering the entire "
    "video frame as the COVER background (not square, not 4:5, not a framed billboard). "
    "Fill the entire frame with one clear diagram or illustration of the topic. "
    "Almost no readable lettering."
)

FRAMING_COVER_PORTRAIT_FULL_DEFAULT = (
    "Constraints: {size_line}. The image MUST be {phrase}, full-bleed, covering the entire "
    "video frame as the COVER background (not square, not 4:5, not a small inset or framed billboard). "
    "Fill the entire frame with one clear diagram or illustration of the topic. "
    "Almost no readable lettering."
)

FRAMING_BILLBOARD_LANDSCAPE_DEFAULT = (
    "Constraints: {size_line}. The image MUST be {phrase} — 16:9 at {gen_w}x{gen_h}, "
    "fill the frame, no letterbox. Full-frame subject-only diagram/illustration of the topic. "
    "NO TV, NO monitor, NO bezel, NO stand, NO screen-in-screen. "
    "Do not draw a picture frame or device around the art. "
    "Almost no readable text — teach with the picture, not captions."
)

FRAMING_BILLBOARD_PORTRAIT_DEFAULT = (
    "Constraints: {size_line}. The image MUST be {phrase} — 16:9 at {gen_w}x{gen_h}, "
    "fill the frame, no letterbox. Full-frame subject-only diagram/illustration of the topic. "
    "NO TV, NO monitor, NO bezel, NO stand, NO screen-in-screen. "
    "Do not draw a picture frame or device around the art. "
    "Almost no readable text — teach with the picture, not captions."
)

RATIO_COVER_DEFAULT = (
    "{size_line}. Demand this exact ratio in the image generator; do not default to square or 4:5."
)

RATIO_BILLBOARD_DEFAULT = (
    "{size_line}. Demand this 16:9 landscape ratio; fill the frame, no letterbox. "
    "Do not generate a 4:5 panel, a TV/monitor as an object, or a screen-in-screen."
)

NO_CHARACTER_DEFAULT = (
    "NO people, NO stick figure, NO mascot host, NO presenter, NO explaining character. "
    "Only the subject being explained (molecules, objects, diagrams of the topic). "
    "The yellow stick-figure host is composited later — do not draw it."
)

IMAGES_SHAPE_COVER_DEFAULT = (
    "This project's video is {phrase}. "
    "{size_line} for EVERY line AND the cover — full-bleed COVER background filling the whole frame. "
    "ChatGPT image UI: 16:9 landscape preset = 1920x1080, 9:16 portrait preset = 1080x1920. Never square. "
)

IMAGES_SHAPE_BILLBOARD_DEFAULT = (
    "This project's video is {aspect} ({video_w}x{video_h}) with BILLBOARD layout. "
    "Cover (script_cover_16x9.png 1920x1080 and script_cover_9x16.png 1080x1920) is full-bleed. "
    "Do not stretch 16:9 into 9:16. "
    "Line art is ALWAYS the 16:9 landscape preset at 1920x1080: full-frame subject diagrams/"
    "illustrations, fill the frame, no letterbox, almost no readable text. "
    "Do NOT draw a TV, monitor, bezel, stand, or screen-in-screen — "
    "the compositor pastes this PNG into the TV overlay later. On a 9:16 video the line art is still "
    "16:9 (top of frame); only the cover is 9:16. "
    "{size_line} for EVERY line. "
)

FLUX_INSTRUCTIONS_DEFAULT = """
{art_style_short}

Subject: {scene}

{phrase}. {framing}
{no_character}
Full-frame diagram or illustration of the subject — not sparse doodles or caption boards.
Almost no readable text. Vary the flat background color; do not default to orange. {constraints_suffix}
""".strip()

CHATGPT_IMAGE_INSTRUCTIONS_DEFAULT = (
    "This job's image provider is ChatGPT native. "
    "Use ChatGPT's built-in image tool only. In the image UI pick the named preset "
    "that matches each job's width/height/image_size: 16:9 landscape = 1920x1080, "
    "9:16 portrait = 1080x1920. Never the square, 1:1, or 4:5 preset. "
    "Paste each job's prompt from list_illustration_jobs VERBATIM — Studio already "
    "injected the configured art style (art.*). Do not invent a new art style. "
    "Cover FIRST (script_cover_16x9.png 1920x1080 and/or script_cover_9x16.png 1080x1920): "
    "studio-room title card — Bubblehead stick figure OUTSIDE on one side "
    "(head fill from Settings stickman_head_color — use the hex in jobs[].prompt), "
    "large TV/billboard with the exact video title as bold on-screen headline text plus "
    "topic art on the screen. Match the project's selected studio background room. "
    "Never stretch 16:9 into 9:16. Paste jobs[].prompt verbatim for cover slots. "
    "Then each missing LINE at that job's generate_at / width / height / image_size: "
    "line art stays subject-only (NO stick figure, NO TV/monitor as an object); "
    "do not put the video title on every line billboard. "
    "Cover-layout lines match the video aspect. Billboard line art is ALWAYS the 16:9 "
    "landscape preset at 1920x1080, fill the frame, no letterbox, even when the video is 9:16 — "
    "only the cover uses 9:16 1080x1920 on portrait billboard jobs. "
    "Do not call DALL-E, gpt-image, the OpenAI Images API, fal, or any other image API. "
    "{shape}"
    "{extra}"
    "Vary background colors from line to line; do not default to orange. "
    "After each image is generated, call save_illustration_image with the image "
    "(kind='cover' for the title card) so Pictures sees it under this project."
)

IMAGES_COVER_INTRO_DEFAULT = """
{art_style_short}

Stickman Automation 5-second title-card still for: {title}
Clean flat 2D cartoon, educational explainer aesthetic.

{background_room}

{cover_layout}

{bubblehead_look} standing OUTSIDE the TV — {bubblehead_pose}. Unique pose; do not reuse a stiff default every time. Head fill {head_color}, shading {head_dark} (Settings stickman_head_color — not stock yellow unless that is the current setting).

Large modern flat-screen TV / billboard with thick black bezel on a stand. ON THE SCREEN:
- Large bold sans-serif headline text of the exact video title "{title}" (stacked/readable like a title card; full title on-frame, not cut off, not tiny, not a watermark).
- Topic illustration / diagram related to "{title}" beside or under that headline.
Do NOT draw Bubblehead or any presenter inside the TV screen art — screen content is title text + topic art only.

Full frame {phrase} at {width}x{height}. Flat even lighting with a slight glow from the monitor. No watermarks, no UI chrome, no photorealism, no 3D.
""".strip()

TTS_OPENAI_INSTRUCTIONS_DEFAULT = (
    "Speak as a friendly educational explainer. Full sentences, clear consonants "
    "for lip-sync, natural pauses at punctuation. Do not rush."
)

TOPICS_GENERATE_DEFAULT = """
Invent exactly {count} original YouTube explainer video topics for a lip-sync stick-figure channel.
Target length for each video is about {duration_min} minutes.
Niche / seed from the user (may be none): {seed}

Return a JSON object:
{{"topics": [{{"title": "short specific title", "angle": "one-line unique take"}}]}}

Rules:
- Titles are concrete and curiosity-driven, not generic ("cool science facts").
- Each angle is one spoken-quality sentence: the unique hook, not a restatement of the title.
- Distinct subjects. Do not produce near-duplicates or numbered variations of one idea.
- No hashtags, no quotation marks wrapping the title, no "Episode N".
- Stay family-friendly and factual-curious. Analogies must come from that topic.
""".strip()

# Platform-wide prompts: only admins edit; never stored in per-member overrides.
ADMIN_ONLY_PROMPT_KEYS = frozenset({
    "script.rules",
    "script.hook_outro",
})

# (key, category, label, description, default)
CATALOG: list[dict[str, str]] = [
    {
        "key": "script.rules",
        "category": "Script",
        "label": "Script format rules",
        "description": "System prompt for OpenAI script generation and MCP get_script_rules. Includes required topic HOOK and subscribe OUTRO. Placeholders: {words_per_minute}, {art_style_short}, {art_style_full}.",
        "default": SCRIPT_RULES_DEFAULT,
    },
    {
        "key": "script.json_instructions",
        "category": "Script",
        "label": "Script JSON instructions",
        "description": "Appended to the script system prompt so OpenAI returns title, summary, youtube_description / youtube_keywords / youtube_hashtags, tagged script, and per-line scenes as JSON. script_tagged must be hook, then body, then subscribe outro.",
        "default": JSON_INSTRUCTIONS_DEFAULT,
    },
    {
        "key": "script.youtube_meta",
        "category": "Script",
        "label": "YouTube publish metadata",
        "description": "Reminder injected with script generation so the model also returns youtube_description, youtube_keywords, and youtube_hashtags for upload.",
        "default": YOUTUBE_META_INSTRUCTIONS_DEFAULT,
    },
    {
        "key": "script.user_template",
        "category": "Script",
        "label": "Script user prompt",
        "description": "User message for generate_script. Placeholders: {topic}, {duration_seconds}, {target_words}, {words_per_minute}, {aspect}, {layout}, {shape}, {extra_block}. Required shape is hook + body + subscribe outro.",
        "default": SCRIPT_USER_TEMPLATE_DEFAULT,
    },
    {
        "key": "script.hook_outro",
        "category": "Script",
        "label": "Topic hook + subscribe outro",
        "description": "Always appended to OpenAI scriptgen and MCP get_script_rules / playbook. Required shape: topic hook first (also the 9:16 short), subscribe outro last (16:9 only).",
        "default": HOOK_OUTRO_REQUIREMENT,
    },
    {
        "key": "script.shorts_cta",
        "category": "Script",
        "label": "9:16 short CTA",
        "description": "Spoken line appended only on the 9:16 hook short after the hook (never added to the 16:9 full explainer). Plain text or a tagged line.",
        "default": DEFAULT_SHORTS_CTA,
    },
    {
        "key": "script.shape_cover",
        "category": "Script",
        "label": "Cover layout shape (script)",
        "description": "Inserted as {shape} in the script user prompt for cover jobs. Placeholders: {phrase}, {size_line}.",
        "default": SCRIPT_SHAPE_COVER_DEFAULT,
    },
    {
        "key": "script.shape_billboard",
        "category": "Script",
        "label": "Billboard layout shape (script)",
        "description": "Inserted as {shape} in the script user prompt for billboard jobs. Placeholders: {phrase}, {size_line}.",
        "default": SCRIPT_SHAPE_BILLBOARD_DEFAULT,
    },
    {
        "key": "art.style_short",
        "category": "Images / art",
        "label": "Art style (short)",
        "description": "One-line art style. Used in Flux fal prompts ({art_style_short}), cover, script.rules, and MCP get_script_rules. Subject-only full-frame diagrams — not a host.",
        "default": ART_STYLE_SHORT,
    },
    {
        "key": "art.style_full",
        "category": "Images / art",
        "label": "Art style (full)",
        "description": "Full illustration style paragraph for script.rules / MCP. Not concatenated into fal. Subject-only full-frame cartoon diagrams, minimal readable text, never a presenter.",
        "default": ART_STYLE_FULL,
    },
    {
        "key": "art.scene_wrapper",
        "category": "Images / art",
        "label": "ChatGPT native scene wrapper",
        "description": "Wraps each line's scene for ChatGPT native image gen only (not sent to fal). Placeholders: {art_style_short}, {aspect}, {width}, {height}, {video_w}, {video_h}, {gen_aspect}, {layout}, {ratio_line}, {scene}, {no_character}, {color_instruction}, {framing}, {constraints_suffix}. Flux uses images.flux_instructions instead.",
        "default": SCENE_WRAPPER_DEFAULT,
    },
    {
        "key": "art.color_instruction",
        "category": "Images / art",
        "label": "Background color instruction",
        "description": "Inserted as {color_instruction} in the scene wrapper. Tells the image model not to default every frame to orange.",
        "default": COLOR_INSTRUCTION_DEFAULT,
    },
    {
        "key": "art.constraints_suffix",
        "category": "Images / art",
        "label": "Image constraints suffix",
        "description": "Appended after {framing} in Flux and ChatGPT scene prompts (no watermarks, no photorealism, flat cartoon colors, almost no readable text).",
        "default": CONSTRAINTS_SUFFIX_DEFAULT,
    },
    {
        "key": "art.no_character",
        "category": "Images / art",
        "label": "No explainer character",
        "description": "Negative inserted as {no_character} in Flux prompts, cover, and ChatGPT scene wrapper. The yellow stick-figure is composited later and must not appear in the PNG.",
        "default": NO_CHARACTER_DEFAULT,
    },
    {
        "key": "art.framing_cover_landscape",
        "category": "Images / art",
        "label": "Cover framing (16:9)",
        "description": "Cover-layout framing for landscape jobs when Bubblehead is on. Placeholders: {size_line}, {phrase}. Leave empty space on the right for the host.",
        "default": FRAMING_COVER_LANDSCAPE_DEFAULT,
    },
    {
        "key": "art.framing_cover_portrait",
        "category": "Images / art",
        "label": "Cover framing (9:16)",
        "description": "Cover-layout framing for portrait jobs when Bubblehead is on. Placeholders: {size_line}, {phrase}. Leave empty space at the bottom for the host.",
        "default": FRAMING_COVER_PORTRAIT_DEFAULT,
    },
    {
        "key": "art.framing_cover_landscape_full",
        "category": "Images / art",
        "label": "Cover framing full-frame (16:9)",
        "description": "Cover-layout framing when Include Bubblehead is off. Placeholders: {size_line}, {phrase}. Fill the entire frame.",
        "default": FRAMING_COVER_LANDSCAPE_FULL_DEFAULT,
    },
    {
        "key": "art.framing_cover_portrait_full",
        "category": "Images / art",
        "label": "Cover framing full-frame (9:16)",
        "description": "Cover-layout framing when Include Bubblehead is off. Placeholders: {size_line}, {phrase}. Fill the entire frame.",
        "default": FRAMING_COVER_PORTRAIT_FULL_DEFAULT,
    },
    {
        "key": "art.framing_billboard_landscape",
        "category": "Images / art",
        "label": "Billboard framing (16:9)",
        "description": "Billboard-layout framing for landscape jobs. Placeholders: {size_line}, {phrase}, {gen_w}, {gen_h}. 16:9 full-bleed topic art, no TV/monitor, no host.",
        "default": FRAMING_BILLBOARD_LANDSCAPE_DEFAULT,
    },
    {
        "key": "art.framing_billboard_portrait",
        "category": "Images / art",
        "label": "Billboard framing (9:16)",
        "description": "Billboard-layout framing for portrait jobs. Placeholders: {size_line}, {phrase}, {gen_w}, {gen_h}. 16:9 full-bleed topic art, no TV/monitor, no host.",
        "default": FRAMING_BILLBOARD_PORTRAIT_DEFAULT,
    },
    {
        "key": "art.ratio_cover",
        "category": "Images / art",
        "label": "Cover ratio line",
        "description": "Ratio demand line for cover jobs. Placeholder: {size_line}.",
        "default": RATIO_COVER_DEFAULT,
    },
    {
        "key": "art.ratio_billboard",
        "category": "Images / art",
        "label": "Billboard ratio line",
        "description": "Ratio demand line for billboard jobs (16:9 landscape, fill the frame, no TV as an object). Placeholder: {size_line}.",
        "default": RATIO_BILLBOARD_DEFAULT,
    },
    {
        "key": "images.shape_cover",
        "category": "Pipeline",
        "label": "Cover shape (illustration jobs)",
        "description": "Shape blurb on list_illustration_jobs for cover layout. Placeholders: {phrase}, {size_line}.",
        "default": IMAGES_SHAPE_COVER_DEFAULT,
    },
    {
        "key": "images.shape_billboard",
        "category": "Pipeline",
        "label": "Billboard shape (illustration jobs)",
        "description": "Shape blurb on list_illustration_jobs for billboard layout (16:9 full-bleed line art; TV overlay is composited later). Placeholders: {aspect}, {video_w}, {video_h}, {size_line}.",
        "default": IMAGES_SHAPE_BILLBOARD_DEFAULT,
    },
    {
        "key": "images.cover_intro",
        "category": "Images / art",
        "label": "Title-card cover (5s intro)",
        "description": "Short prompt for script_cover_16x9.png / script_cover_9x16.png (Flux, ComfyUI, ChatGPT). Studio-room title card: Bubblehead outside left (or lower on 9:16), TV/billboard with large topic title text + topic art on screen. Placeholders: {art_style_short}, {title}, {phrase}, {width}, {height}, {background_room}, {bubblehead_pose}, {bubblehead_look}, {cover_layout}, {head_color}, {head_dark}. Studio injects live stickman_head_color, selected background_file, a unique pose, and title-on-billboard if a stale override omits them.",
        "default": IMAGES_COVER_INTRO_DEFAULT,
    },
    {
        "key": "images.flux_instructions",
        "category": "Images / art",
        "label": "Flux (fal) scene prompt",
        "description": "The SHORT prompt actually sent to fal-ai/flux-2. Art style + this line's subject + framing + no-character. Placeholders: {art_style_short}, {scene}, {phrase}, {width}, {height}, {framing}, {no_character}, {constraints_suffix}. Never ChatGPT playbook, MCP handshake, script.rules, or TTS text.",
        "default": FLUX_INSTRUCTIONS_DEFAULT,
    },
    {
        "key": "images.chatgpt_instructions",
        "category": "Pipeline",
        "label": "ChatGPT native image instructions",
        "description": "MCP/ChatGPT operator text for native in-chat images only. Not sent to fal. Names the 16:9 / 9:16 image-UI presets. Placeholders: {shape}, {extra}.",
        "default": CHATGPT_IMAGE_INSTRUCTIONS_DEFAULT,
    },
    {
        "key": "tts.openai_instructions",
        "category": "Pipeline",
        "label": "OpenAI TTS instructions",
        "description": "Passed as instructions= to gpt-4o / mini-tts speech.create when generating narration audio.",
        "default": TTS_OPENAI_INSTRUCTIONS_DEFAULT,
    },
    {
        "key": "topics.generate",
        "category": "Topics",
        "label": "Topic generator",
        "description": "Prompt for the Topics page and MCP generate_topics when text_provider is openai or lmstudio. ChatGPT/Claude invent topics themselves then create_topic. Placeholders: {count}, {duration_min}, {seed}. Returns JSON {topics: [{title, angle}]}.",
        "default": TOPICS_GENERATE_DEFAULT,
    },
    {
        "key": "mcp.chatgpt_playbook",
        "category": "MCP",
        "label": "ChatGPT MCP playbook",
        "description": "MCP server instructions for ChatGPT Desktop and Claude Desktop / Claude Code (get_chatgpt_playbook). Requires topic HOOK + subscribe OUTRO. Obeys text_provider (openai billed API, lmstudio local API, or native save_script). Placeholder: {script_rules} (live script.rules). get_chatgpt_playbook / get_script_rules re-read prompts.json; FastMCP handshake also refreshes on initialize.",
        "default": CHATGPT_PLAYBOOK_DEFAULT,
    },
]

PROMPT_KEYS = {item["key"] for item in CATALOG}
_BY_KEY = {item["key"]: item for item in CATALOG}


def _spec(key: str) -> dict[str, str]:
    spec = _BY_KEY.get(key)
    if not spec:
        raise RuntimeError(f"Unknown prompt key: {key}")
    return spec


def default_text(key: str) -> str:
    return _spec(key)["default"]


def set_prompt_user(user_id: str | None):
    """Bind prompt overrides to a member id for this async/task context."""
    return _prompt_user_id.set((user_id or "").strip() or None)


def reset_prompt_user(token) -> None:
    _prompt_user_id.reset(token)


def current_prompt_user() -> str | None:
    return _prompt_user_id.get()


def _safe_user_id(user_id: str | None = None, *, fallback_context: bool = True) -> str | None:
    if user_id is None and fallback_context:
        raw = _prompt_user_id.get() or ""
    else:
        raw = user_id or ""
    raw = str(raw).strip()
    if not raw:
        return None
    if Path(raw).name != raw or ".." in raw:
        return None
    return raw


def _overrides_path(user_id: str | None = None, *, fallback_context: bool = True) -> Path:
    uid = _safe_user_id(user_id, fallback_context=fallback_context)
    if uid:
        return PROMPTS_USERS_DIR / f"{uid}.json"
    return PROMPTS_PATH


def _parse_overrides_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("overrides"), dict):
        data = data["overrides"]
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(k) in PROMPT_KEYS and isinstance(v, str)}


def _cached_file_overrides(path: Path, cache_key: str) -> dict[str, str]:
    try:
        mtime = path.stat().st_mtime if path.is_file() else None
    except OSError:
        mtime = None
    with _lock:
        cached = _cache.get(cache_key)
        if cached and cached.get("mtime") == mtime and "data" in cached:
            return dict(cached["data"])
        data = _parse_overrides_file(path)
        _cache[cache_key] = {"mtime": mtime, "data": dict(data)}
        return dict(data)


def _load_overrides(user_id: str | None = None) -> dict[str, str]:
    """User overrides win; global prompts.json is the shared fallback for unset keys / MCP.

    Admin-only keys (script.rules, …) always come from the global file — members cannot override.
    """
    ensure_dirs()
    uid = _safe_user_id(user_id)
    global_over = _cached_file_overrides(PROMPTS_PATH, "__global__")
    if not uid:
        return global_over
    user_over = _cached_file_overrides(PROMPTS_USERS_DIR / f"{uid}.json", uid)
    merged = dict(global_over)
    for key, value in user_over.items():
        if key in ADMIN_ONLY_PROMPT_KEYS:
            continue
        merged[key] = value
    return merged


def is_admin_only_prompt(key: str) -> bool:
    return str(key or "").strip() in ADMIN_ONLY_PROMPT_KEYS


def _write_overrides(overrides: dict[str, str], user_id: str | None = None) -> None:
    ensure_dirs()
    uid = _safe_user_id(user_id, fallback_context=False) if user_id is not None else _safe_user_id(None)
    # Prefer explicit user_id from callers; else context.
    if user_id is not None:
        uid = _safe_user_id(user_id, fallback_context=False)
    else:
        uid = _safe_user_id(None, fallback_context=True)
    path = PROMPTS_USERS_DIR / f"{uid}.json" if uid else PROMPTS_PATH
    if uid:
        PROMPTS_USERS_DIR.mkdir(parents=True, exist_ok=True)
    cleaned = {}
    for key, value in overrides.items():
        if key not in PROMPT_KEYS or not isinstance(value, str):
            continue
        # Never persist platform rules into a member's override file.
        if uid and key in ADMIN_ONLY_PROMPT_KEYS:
            continue
        if value != default_text(key):
            cleaned[key] = value
    path.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    cache_key = uid or "__global__"
    with _lock:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        _cache[cache_key] = {"mtime": mtime, "data": dict(cleaned)}


def get_prompt_raw(key: str, user_id: str | None = None) -> str:
    """Override or default template, without placeholder substitution."""
    overrides = _load_overrides(user_id)
    if key in overrides:
        return overrides[key]
    return default_text(key)


def get_prompt(key: str, **kwargs: Any) -> str:
    """Live prompt text. Missing keys fall back to code defaults. Re-read from disk each change."""
    user_id = kwargs.pop("user_id", None)
    raw = get_prompt_raw(key, user_id=user_id)
    names = set(_PLACEHOLDER_RE.findall(raw))
    ctx: dict[str, Any] = dict(kwargs)
    if "words_per_minute" in names and "words_per_minute" not in ctx:
        ctx["words_per_minute"] = WORDS_PER_MINUTE
    for name in names:
        if name in ctx:
            continue
        nested = _PLACEHOLDER_TO_KEY.get(name)
        if nested and nested != key:
            ctx[name] = get_prompt(nested, user_id=user_id)
    try:
        return raw.format_map(_SafeDict(ctx))
    except (ValueError, IndexError):
        return raw


def list_prompt_entries(user_id: str | None = None, *, is_admin: bool = False) -> list[dict[str, Any]]:
    overrides = _load_overrides(user_id)
    uid = _safe_user_id(user_id)
    user_only = _parse_overrides_file(_overrides_path(uid)) if uid else {}
    global_only = _parse_overrides_file(PROMPTS_PATH)
    entries = []
    for spec in CATALOG:
        key = spec["key"]
        admin_only = key in ADMIN_ONLY_PROMPT_KEYS
        if admin_only:
            value = global_only[key] if key in global_only else spec["default"]
            overridden = key in global_only
            scope = "global"
        else:
            value = overrides[key] if key in overrides else spec["default"]
            overridden = key in user_only
            scope = "user" if uid else "global"
        entries.append(
            {
                "key": key,
                "label": spec["label"],
                "description": spec["description"],
                "category": spec["category"],
                "value": value,
                "default": spec["default"],
                "is_overridden": overridden,
                "scope": scope,
                "admin_only": admin_only,
                "editable": (not admin_only) or bool(is_admin),
            }
        )
    return entries


def catalog_payload(user_id: str | None = None, *, is_admin: bool = False) -> dict[str, Any]:
    uid = _safe_user_id(user_id)
    path = _overrides_path(uid)
    return {
        "prompts": list_prompt_entries(uid, is_admin=is_admin),
        "path": str(path),
        "user_id": uid,
        "scope": "user" if uid else "global",
        "is_admin": bool(is_admin),
        "live": True,
        "note": (
            "Most prompt edits apply only to your account. "
            "script.rules and script.hook_outro are platform-wide (admins only). "
            "Flux (fal) and ComfyUI use images.flux_instructions + art.* only. "
            "Every script must open with a topic hook and end with a subscribe outro."
        ),
    }


def save_prompts(
    updates: dict[str, str],
    user_id: str | None = None,
    *,
    is_admin: bool = False,
) -> dict[str, Any]:
    uid = _safe_user_id(user_id)
    admin_updates = {k: v for k, v in updates.items() if k in ADMIN_ONLY_PROMPT_KEYS}
    member_updates = {k: v for k, v in updates.items() if k not in ADMIN_ONLY_PROMPT_KEYS}
    if admin_updates and not is_admin:
        blocked = ", ".join(sorted(admin_updates))
        raise PermissionError(f"Admin only: cannot edit {blocked}")
    if admin_updates:
        current = _parse_overrides_file(PROMPTS_PATH)
        for key, text in admin_updates.items():
            _spec(key)
            if not isinstance(text, str):
                raise RuntimeError(f"Prompt {key} must be a string.")
            if text == default_text(key):
                current.pop(key, None)
            else:
                current[key] = text
        _write_overrides(current, user_id=None)
    if member_updates:
        if uid:
            current = _parse_overrides_file(_overrides_path(uid))
        else:
            current = _parse_overrides_file(PROMPTS_PATH)
        for key, text in member_updates.items():
            _spec(key)
            if not isinstance(text, str):
                raise RuntimeError(f"Prompt {key} must be a string.")
            if text == default_text(key):
                current.pop(key, None)
            else:
                current[key] = text
        _write_overrides(current, user_id=uid)
    return catalog_payload(uid, is_admin=is_admin)


def save_prompt(key: str, text: str, user_id: str | None = None, *, is_admin: bool = False) -> dict[str, Any]:
    return save_prompts({key: text}, user_id=user_id, is_admin=is_admin)


def reset_prompt(
    key: str | None = None,
    user_id: str | None = None,
    *,
    is_admin: bool = False,
) -> dict[str, Any]:
    uid = _safe_user_id(user_id)
    if not key:
        _write_overrides({}, user_id=uid)
        return catalog_payload(uid, is_admin=is_admin)
    _spec(key)
    if key in ADMIN_ONLY_PROMPT_KEYS:
        if not is_admin:
            raise PermissionError(f"Admin only: cannot reset {key}")
        current = _parse_overrides_file(PROMPTS_PATH)
        current.pop(key, None)
        _write_overrides(current, user_id=None)
        return catalog_payload(uid, is_admin=is_admin)
    if uid:
        current = _parse_overrides_file(_overrides_path(uid))
    else:
        current = _parse_overrides_file(PROMPTS_PATH)
    current.pop(key, None)
    _write_overrides(current, user_id=uid)
    return catalog_payload(uid, is_admin=is_admin)
