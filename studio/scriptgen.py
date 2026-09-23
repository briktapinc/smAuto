from __future__ import annotations

from typing import Any

from studio.art_style import wrap_scene_prompt
from studio.aspect import DEFAULT_ASPECT, canvas_phrase, generate_at_line, normalize_aspect
from studio.projects import project_dir, save_lines, write_scripts
from studio.prompts import get_prompt
from studio.script_rules import ALLOWED_EMOTIONS, WORDS_PER_MINUTE
from studio.settings import current_text_provider, normalize_video_layout
from studio.textgen import (
    LMSTUDIO_RAW_DUMP_NAME,
    chat_json,
    save_raw_model_text,
)
from studio.utils_script import (
    parse_tagged_script,
    raw_from_tagged,
    script_structure_warnings,
    validate_tagged_script,
)

SCRIPT_ONLY_JSON_INSTRUCTIONS = """
Return ONLY a single JSON object (no markdown fences, no commentary) with keys:
- title: short video title
- summary: 2-4 sentences describing the whole story, used as illustration context
- youtube_description: YouTube video description for this topic/script (2-5 short
  paragraphs, plain text). Include a one-line hook, what the video covers, and a soft
  subscribe CTA. Do NOT put spoken script lines here. You MAY end with hashtags.
- youtube_keywords: array of 8-15 short YouTube search tags (strings, no #). Relevant
  to THIS topic only. No duplicates.
- youtube_hashtags: array of 3-8 hashtags WITH leading # (e.g. "#AI", "#Science").
- script_tagged: the full tagged script as one JSON string. Escape line breaks as \\n
  (never raw line breaks inside the string). Escape any double quotes as \\".
- script_raw: the spoken script without <emotion> tags, without illustration/scene
  prompts, and with short [topic] markers unwrapped to plain words (same escaping).
  Studio may re-derive this from script_tagged; never put IMAGE:/PROMPT: or long
  art-direction brackets in either field.

Do NOT include a lines array. Scene illustrations will be requested in a second call.
script_tagged MUST be hook, then explainer body, then subscribe outro.
No trailing commas, no comments, no single-quoted keys.
""".strip()

LINES_JSON_INSTRUCTIONS = """
Return ONLY a single JSON object (no markdown fences) with key lines: an array of
objects, one per NON-EMPTY script line, in order. Each object:
- tagged: that line including any <emotion> and [topic] markers, copied exactly
- scene: a concrete FULL-FRAME illustration or diagram of THIS line's subject only
  (objects, molecules, cutaways, processes, animals of the topic). Prefer one clear
  visual that fills the frame over scattered tiny doodles or handwritten notes.
  Almost no readable text or labels in the scene description — teach with the picture.
  Never a person, stick figure, mascot host, presenter, teacher, or "character explaining".
  Do not mention art-style, ChatGPT, aspect presets, or a narrator.
  Follow the video_layout for this job:
  cover = a full-bleed COVER background at the video's exact aspect
  (16:9 = 1920x1080 landscape, or 9:16 = 1080x1920 portrait — never square or 4:5).
  billboard = a 16:9 landscape 1920x1080 subject-only illustration, fill the frame,
  no letterbox. Do not mention a TV, monitor, bezel, stand, or screen-in-screen
  (that overlay is composited later).
  Do not repeat the art-style paragraph here; only the scene.

No trailing commas, no comments. Do not repeat script_tagged.
""".strip()


def _script_dump_path(project_id: str):
    pid = (project_id or "").strip()
    if not pid:
        return None
    try:
        return project_dir(pid) / LMSTUDIO_RAW_DUMP_NAME
    except Exception:
        return None


def _rules_system(json_instructions: str) -> str:
    return (
        get_prompt("script.rules")
        + "\n\n"
        + get_prompt("script.hook_outro")
        + "\n\n"
        + get_prompt("script.youtube_meta")
        + "\n\n"
        + json_instructions
    )


def _lmstudio_two_step(
    user: str,
    *,
    dump_path,
    aspect: str,
    layout: str,
    shape: str,
) -> tuple[dict[str, Any], str, str]:
    data, model, provider = chat_json(
        temperature=0.7,
        raw_dump_path=dump_path,
        messages=[
            {"role": "system", "content": _rules_system(SCRIPT_ONLY_JSON_INSTRUCTIONS)},
            {"role": "user", "content": user},
        ],
    )
    if not isinstance(data, dict):
        raise RuntimeError("Generated script was not a JSON object.")
    tagged = (data.get("script_tagged") or "").replace("\r\n", "\n").strip()
    if not tagged:
        raise RuntimeError("Generated script was missing script_tagged.")
    lines_user = (
        f"Video aspect: {aspect}. Video layout: {layout}. {shape}\n\n"
        "Tagged script:\n"
        f"{tagged}\n\n"
        'Return JSON {"lines": [...]} with one object per non-empty line, in order.'
    )
    try:
        lines_data, _model, _provider = chat_json(
            temperature=0.4,
            raw_dump_path=dump_path,
            messages=[
                {"role": "system", "content": LINES_JSON_INSTRUCTIONS},
                {"role": "user", "content": lines_user},
            ],
        )
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "invalid json" not in msg:
            raise
        save_raw_model_text(dump_path, str(exc), label="lines_step_failed")
        return data, model, provider
    if isinstance(lines_data, dict) and lines_data.get("lines"):
        data = dict(data)
        data["lines"] = lines_data["lines"]
    elif isinstance(lines_data, list):
        data = dict(data)
        data["lines"] = lines_data
    return data, model, provider


def _assemble_generated(
    data: dict[str, Any],
    *,
    topic: str,
    target_words: int,
    duration_seconds: int,
    aspect: str,
    layout: str,
    image_provider: str,
    model: str,
    art_style: str | None = None,
) -> dict[str, Any]:
    tagged = (data.get("script_tagged") or "").replace("\r\n", "\n").strip()
    if not tagged and data.get("lines"):
        chunks = []
        for line in data["lines"]:
            chunks.append(line.get("tagged") or "")
            if line.get("section_break_after"):
                chunks.append("")
        tagged = "\n".join(chunks).strip()
    errors = validate_tagged_script(tagged)
    errors.extend(script_structure_warnings(tagged))
    if errors:
        raise RuntimeError("Generated script failed validation: " + "; ".join(errors))

    title = (data.get("title") or topic).strip()
    summary = (data.get("summary") or topic).strip()
    from studio.projects import normalize_youtube_hashtags, normalize_youtube_keywords

    youtube_description = (data.get("youtube_description") or "").strip()
    youtube_keywords = normalize_youtube_keywords(data.get("youtube_keywords"))
    youtube_hashtags = normalize_youtube_hashtags(data.get("youtube_hashtags"))
    scene_hints = [str(item.get("scene") or "") for item in data.get("lines") or [] if item.get("tagged")]
    lines = parse_tagged_script(
        tagged,
        title=title,
        summary=summary,
        scene_hints=scene_hints,
        aspect=aspect,
        layout=layout,
        image_provider=image_provider,
        art_style=art_style,
    )
    for line, item in zip(lines, data.get("lines") or []):
        if item.get("scene"):
            line["scene"] = item["scene"]
            line["image_prompt"] = wrap_scene_prompt(
                item["scene"],
                title,
                summary,
                line["raw"],
                aspect=aspect,
                layout=layout,
                provider=image_provider,
                art_style=art_style,
            )
    # Always derive spoken text from tagged so illustration cues cannot leak into TTS.
    raw = raw_from_tagged(tagged)
    return {
        "title": title,
        "summary": summary,
        "youtube_description": youtube_description,
        "youtube_keywords": youtube_keywords,
        "youtube_hashtags": youtube_hashtags,
        "script_tagged": tagged + "\n",
        "script_raw": raw.rstrip() + "\n",
        "lines": lines,
        "target_words": target_words,
        "duration_seconds": duration_seconds,
        "emotions": list(ALLOWED_EMOTIONS),
        "model": model,
    }


def generate_script(
    topic: str,
    duration_seconds: int,
    extra: str = "",
    aspect: str = DEFAULT_ASPECT,
    layout: str = "cover",
    image_provider: str = "flux",
    project_id: str = "",
    provider: str | None = None,
) -> dict[str, Any]:
    duration_seconds = max(15, int(duration_seconds))
    aspect = normalize_aspect(aspect)
    layout = normalize_video_layout(layout)
    target_words = max(40, int(round(duration_seconds / 60 * WORDS_PER_MINUTE)))
    phrase = canvas_phrase(aspect, layout)
    size_line = generate_at_line(aspect, layout)
    shape_key = "script.shape_billboard" if layout == "billboard" else "script.shape_cover"
    shape = get_prompt(shape_key, phrase=phrase, size_line=size_line)
    extra_block = f"Extra direction from the user:\n{extra.strip()}\n" if extra.strip() else ""
    user = get_prompt(
        "script.user_template",
        topic=topic.strip(),
        duration_seconds=duration_seconds,
        target_words=target_words,
        aspect=aspect,
        layout=layout,
        shape=shape,
        extra_block=extra_block,
    )
    dump_path = _script_dump_path(project_id)
    if dump_path is not None:
        try:
            dump_path.unlink(missing_ok=True)
        except OSError:
            pass

    from studio.settings import normalize_text_provider

    if provider:
        try:
            active = normalize_text_provider(provider)
        except RuntimeError:
            active = current_text_provider()
    else:
        active = current_text_provider()
    if active == "lmstudio":
        data, model, _provider = _lmstudio_two_step(
            user,
            dump_path=dump_path,
            aspect=aspect,
            layout=layout,
            shape=shape,
        )
    else:
        data, model, _provider = chat_json(
            temperature=0.7,
            raw_dump_path=dump_path,
            messages=[
                {"role": "system", "content": _rules_system(get_prompt("script.json_instructions"))},
                {"role": "user", "content": user},
            ],
        )
    if not isinstance(data, dict):
        raise RuntimeError("Generated script was not a JSON object.")
    art_style = None
    if project_id:
        try:
            from studio.projects import project_art_style

            art_style = project_art_style(project_id)
        except Exception:
            art_style = None
    return _assemble_generated(
        data,
        topic=topic,
        target_words=target_words,
        duration_seconds=duration_seconds,
        aspect=aspect,
        layout=layout,
        image_provider=image_provider,
        model=model,
        art_style=art_style,
    )


def apply_generated_script(project_id: str, generated: dict[str, Any]) -> dict[str, Any]:
    from studio.projects import apply_youtube_publish_meta, load_meta, save_meta

    meta = load_meta(project_id)
    if generated.get("title"):
        meta["title"] = generated["title"]
    if generated.get("summary"):
        meta["summary"] = generated["summary"]
    save_meta(project_id, meta)
    apply_youtube_publish_meta(
        project_id,
        description=generated.get("youtube_description"),
        keywords=generated.get("youtube_keywords"),
        hashtags=generated.get("youtube_hashtags"),
        replace=True,
    )
    save_lines(project_id, generated["lines"], summary=generated.get("summary"))
    return write_scripts(project_id, generated["script_tagged"], generated.get("script_raw"))
