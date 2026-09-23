from __future__ import annotations

from studio.settings import BILLBOARD_SIZE, normalize_video_layout

ASPECT_16_9 = "16:9"
ASPECT_9_16 = "9:16"
ASPECT_BOTH = "both"
DEFAULT_ASPECT = ASPECT_16_9
ALL_ASPECTS = (ASPECT_16_9, ASPECT_9_16)
JOB_ASPECTS = (ASPECT_16_9, ASPECT_9_16, ASPECT_BOTH)
_BOTH_ALIASES = {
    "both",
    "all",
    "16:9+9:16",
    "16:9,9:16",
    "16:9and9:16",
    "landscape+portrait",
}

CANVAS = {
    ASPECT_16_9: (1920, 1080),
    ASPECT_9_16: (1080, 1920),
}

_ASPECT_SLUGS = {
    ASPECT_16_9: "16x9",
    ASPECT_9_16: "9x16",
}

# ChatGPT Desktop image UI preset names (landscape / portrait). Never square.
CHATGPT_PRESET_16_9 = "16:9"
CHATGPT_PRESET_9_16 = "9:16"


def normalize_job_aspect(value: str | None) -> str:
    """Stored job/settings aspect: 16:9, 9:16, or both."""
    raw = (value or DEFAULT_ASPECT).strip().lower().replace(" ", "")
    compact = raw.replace("x", ":")
    if raw in _BOTH_ALIASES or compact in _BOTH_ALIASES:
        return ASPECT_BOTH
    if compact in {"9:16", "portrait", "vertical", "shorts", "tiktok", "reel"}:
        return ASPECT_9_16
    return ASPECT_16_9


def aspects_to_render(value: str | None) -> tuple[str, ...]:
    """Canvas ratios Render should write. both → (16:9, 9:16) in that order."""
    job = normalize_job_aspect(value)
    if job == ASPECT_BOTH:
        return ALL_ASPECTS
    return (job,)


def normalize_aspect(value: str | None) -> str:
    """Canvas aspect only (16:9 or 9:16). both uses 16:9 as the primary canvas."""
    job = normalize_job_aspect(value)
    if job == ASPECT_BOTH:
        return ASPECT_16_9
    return job


def aspect_slug(aspect: str | None) -> str:
    """Filesystem token: 16:9 → 16x9, 9:16 → 9x16."""
    return _ASPECT_SLUGS[normalize_aspect(aspect)]


def cover_filename(aspect: str | None) -> str:
    return f"script_cover_{aspect_slug(aspect)}.png"


def final_video_filename(aspect: str | None) -> str:
    return f"script_final_{aspect_slug(aspect)}.mp4"


def frames_dirname(aspect: str | None) -> str:
    return f"script_frames_{aspect_slug(aspect)}"


# 9:16 hook-only short uses its own script/audio/align stem (not the full explainer).
SHORTS_STEM = "script_9x16"


def needs_shorts_assets(aspect: str | None) -> bool:
    """True when the job will render a 9:16 hook short (9:16 or both)."""
    return ASPECT_9_16 in aspects_to_render(aspect)


def needs_explainer_assets(aspect: str | None) -> bool:
    """True when the job will render the full 16:9 explainer (16:9 or both)."""
    return ASPECT_16_9 in aspects_to_render(aspect)


def aspect_from_cover_filename(filename: str | None) -> str | None:
    """Aspect encoded in script_cover_16x9.png / script_cover_9x16.png. None = unspecified."""
    stem = (filename or "").replace("\\", "/").split("/")[-1].lower()
    stem = stem.removesuffix(".png").replace("-", "_")
    if "9x16" in stem or stem.endswith("9_16"):
        return ASPECT_9_16
    if "16x9" in stem or stem.endswith("16_9"):
        return ASPECT_16_9
    return None


def canvas_size(aspect: str | None) -> tuple[int, int]:
    return CANVAS[normalize_aspect(aspect)]


def is_portrait(aspect: str | None) -> bool:
    return normalize_aspect(aspect) == ASPECT_9_16


def pixel_size(aspect: str | None) -> str:
    width, height = canvas_size(aspect)
    return f"{width}x{height}"


def illustration_size(aspect: str | None = None, layout: str | None = None) -> tuple[int, int]:
    if normalize_video_layout(layout) == "billboard":
        return BILLBOARD_SIZE
    return canvas_size(aspect)


def generate_aspect(aspect: str | None, layout: str | None = None) -> str:
    """Aspect the image itself should be (billboard line art is always 16:9)."""
    if normalize_video_layout(layout) == "billboard":
        return ASPECT_16_9
    return normalize_aspect(aspect)


def chatgpt_preset(aspect: str | None, layout: str | None = None) -> str:
    """ChatGPT native image-tool preset to pick (16:9 or 9:16 — never square)."""
    return generate_aspect(aspect, layout)


def job_image_fields(aspect: str | None, layout: str | None = None, provider: str | None = None) -> dict:
    """width/height/aspect/image_size for list_illustration_jobs so models do not default to square."""
    video_aspect = normalize_aspect(aspect)
    gen_aspect = generate_aspect(aspect, layout)
    width, height = illustration_size(aspect, layout)
    size = f"{width}x{height}"
    return {
        "width": width,
        "height": height,
        "aspect": gen_aspect,
        "image_size": size,
        "pixel_size": size,
        "video_aspect": video_aspect,
        "chatgpt_preset": gen_aspect,
        "generate_at": generate_at_line(aspect, layout, provider=provider),
    }


def generate_at_line(aspect: str | None, layout: str | None = None, provider: str | None = None) -> str:
    """One-line size instruction. Flux/ComfyUI get pixels only; ChatGPT native gets image-UI preset names."""
    from studio.settings import uses_short_image_prompt

    pixel_mode = uses_short_image_prompt(provider or "chatgpt")
    if normalize_video_layout(layout) == "billboard":
        width, height = BILLBOARD_SIZE
        if pixel_mode:
            return (
                f"Generate at exactly {width}x{height}. 16:9 landscape, fill the frame, "
                f"no letterbox. Even if the video is 9:16, this line image stays 16:9 "
                f"{width}x{height}. Never square, never 1:1, never 4:5."
            )
        return (
            f"In ChatGPT's image UI pick the 16:9 landscape preset and generate at exactly "
            f"{width}x{height}. Fill the frame, no letterbox. Even if the video is 9:16, "
            f"this line image stays 16:9 {width}x{height}. Never square, never 1:1, never 4:5."
        )
    aspect = normalize_aspect(aspect)
    width, height = canvas_size(aspect)
    if pixel_mode:
        return f"Generate at exactly {width}x{height}. Never square, never 1:1, never 4:5."
    if aspect == ASPECT_9_16:
        return (
            f"In ChatGPT's image UI pick the 9:16 portrait preset and generate at exactly "
            f"{width}x{height}. Never square, never 1:1, never 4:5."
        )
    return (
        f"In ChatGPT's image UI pick the 16:9 landscape preset and generate at exactly "
        f"{width}x{height}. Never square, never 1:1, never 4:5."
    )


def canvas_phrase(aspect: str | None, layout: str | None = None) -> str:
    """e.g. 'exactly 16:9 (1920x1080 landscape)' or full-bleed 16:9 billboard line art."""
    if normalize_video_layout(layout) == "billboard":
        width, height = BILLBOARD_SIZE
        return f"16:9 landscape {width}x{height}, fill the frame, no letterbox"
    aspect = normalize_aspect(aspect)
    orientation = "portrait" if aspect == ASPECT_9_16 else "landscape"
    return f"exactly {aspect} ({pixel_size(aspect)} {orientation})"
