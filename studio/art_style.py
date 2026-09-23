"""Illustration art styles for line/cover prompts. Classic (default) = current Stickman Automation look."""

from __future__ import annotations

import re
from typing import Any

ART_STYLE_SHORT = (
    "Hand-drawn 2D cartoon full-frame diagrams and illustrations of the topic, "
    "organic black outlines, flat colors, bold clear subjects filling the frame, "
    "almost no readable text or labels."
)

ART_STYLE_FULL = """The style is hand-drawn 2D cartoon illustration — educational explainer art, not sparse whiteboard doodling.
- Loose black outlines: Slightly uneven strokes, informal and hand-sketched.
- Full-frame subject illustrations: Fill most of the frame with one clear diagram, cutaway, process, object, animal, or scene that teaches the idea at a glance. Prefer a complete illustration over scattered tiny doodles and comic stickers.
- Subject-only: Draw the thing being explained. Never a presenter, teacher, mascot, stick-figure host, or anyone pointing at a board.
- Flat colors: Solid fills with very little shading or dimensional detail.
- Varied flat palette: Each scene picks its own background color to fit the mood and subject — sky blue, mint, cream, coral, lavender, sunshine yellow, charcoal, forest green, or whatever the line needs. Do not default every frame to orange.
- Visual first, text last: Almost no readable lettering. Do not cover the image with handwritten notes, comic captions, bullet lists, or paragraphs. At most one tiny optional label if essential; prefer pure diagram/illustration.
- Motion / comic devices sparingly: A few motion lines or symbols are fine when they clarify the idea — not a clutter of stickers across empty space.
Overall, it feels friendly and educational like a clean textbook diagram drawn as a cartoon — not a page of doodles to read.
Short style description: “Hand-drawn 2D cartoon full-frame diagrams and illustrations, organic black outlines, flat colors, bold subjects filling the frame, almost no readable text.”"""

DEFAULT_ART_STYLE = "classic"

# Catalog: id → label + prompt text. Classic resolves to live Prompts overrides when present.
ART_STYLES: dict[str, dict[str, str]] = {
    "classic": {
        "label": "Classic (hand-drawn 2D)",
        "short": ART_STYLE_SHORT,
        "full": ART_STYLE_FULL,
    },
    "pixar_3d": {
        "label": "3D Pixar / Disney",
        "short": (
            "Stylized 3D Pixar/Disney feature-animation look: soft rounded forms, subsurface skin-like "
            "shading on organic shapes, cinematic key light + gentle rim, saturated friendly colors, "
            "full-frame subject as a clear educational diagram/prop, almost no readable text."
        ),
        "full": (
            "Render as stylized 3D CGI in the spirit of modern Pixar/Disney features — appealing "
            "rounded volumes, soft occlusion, subtle material sheen, warm cinematic lighting. "
            "Subject-only educational illustration filling the frame (object, cutaway, process, animal, scene). "
            "Never a presenter, teacher, mascot host, or stick figure. Almost no readable lettering."
        ),
    },
    "cinematic": {
        "label": "Cinematic",
        "short": (
            "Cinematic illustrated key-art: dramatic lighting, shallow depth cues, rich contrast, "
            "film-still composition, atmospheric color grade, full-frame topic subject as a clear "
            "diagram/scene, stylized (not raw photo), almost no readable text."
        ),
        "full": (
            "Cinematic illustrated look — dramatic key light, volumetric atmosphere, strong silhouette, "
            "widescreen storytelling composition. Keep it stylized illustration (not photoreal photography). "
            "Subject-only educational frame; never a host/presenter. Almost no readable text."
        ),
    },
    "claymation": {
        "label": "Claymation",
        "short": (
            "Stop-motion claymation / Plasticine look: fingerprint texture in clay, soft sculpted forms, "
            "matte clay materials, studio tabletop lighting, full-frame subject as a handmade educational "
            "model/diorama, almost no readable text."
        ),
        "full": (
            "Claymation / stop-motion Plasticine aesthetic — visible clay surface texture, handmade edges, "
            "soft tabletop lighting, playful sculpted props. Subject-only educational model filling the frame. "
            "Never a presenter or stick-figure host. Almost no readable lettering."
        ),
    },
    "watercolor": {
        "label": "Watercolor",
        "short": (
            "Watercolor illustration: translucent washes, soft bleeds, paper grain, gentle outlines, "
            "full-frame educational subject, calm natural palette, almost no readable text."
        ),
        "full": (
            "Traditional watercolor on paper — translucent layered washes, soft edges, visible paper tooth, "
            "light ink accents. Subject-only educational illustration filling the frame. Never a host. "
            "Almost no readable text."
        ),
    },
    "anime": {
        "label": "Anime",
        "short": (
            "Clean anime/manga illustration: crisp linework, cel shading, expressive shapes, "
            "full-frame educational subject diagram/scene, vibrant but controlled colors, almost no readable text."
        ),
        "full": (
            "Anime/manga visual language — clean contours, cel fills, simple screentone-like shading. "
            "Subject-only educational illustration filling the frame (not a character-host talking head). "
            "Almost no readable lettering or speech bubbles."
        ),
    },
    "comic_book": {
        "label": "Comic book",
        "short": (
            "Bold comic-book illustration: thick ink outlines, halftone dots, dynamic graphic shapes, "
            "high-contrast flat colors, full-frame educational subject, almost no readable captions or speech balloons."
        ),
        "full": (
            "American comic / graphic-novel ink style — bold blacks, halftone texture, punchy color flats. "
            "Subject-only educational panel filling the frame. Prefer pure illustration over caption boxes. "
            "Never a stick-figure host."
        ),
    },
    "paper_craft": {
        "label": "Paper craft",
        "short": (
            "Layered paper-craft / cut-paper diorama: stacked colored paper silhouettes, soft drop shadows "
            "between layers, craft-table lighting, full-frame educational subject, almost no readable text."
        ),
        "full": (
            "Papercraft / cut-paper collage look — distinct paper layers, slight thickness shadows, matte "
            "colored stock. Subject-only educational diorama filling the frame. Never a presenter. "
            "Almost no readable lettering."
        ),
    },
    "voxel": {
        "label": "Voxel / blocky 3D",
        "short": (
            "Charming voxel / blocky 3D mini-world: cubic forms, soft AO, toy-like scale, "
            "full-frame educational subject built from voxels, almost no readable text."
        ),
        "full": (
            "Voxel art / Minecraft-adjacent blocky 3D — readable cubic silhouette, gentle lighting, "
            "toy-like materials. Subject-only educational build filling the frame. Never a host avatar. "
            "Almost no readable text."
        ),
    },
}

_STYLE_DUMP_RE = re.compile(
    r"(?:,\s*)?(?:portrait\s+4:5|hand-drawn\s+2d\s+cartoon|whiteboard[-\s]explainer|"
    r"expressive\s+white-faced\s+stick|loose\s+bold\s+uneven\s+black\s+outlines|"
    r"stylized\s+3d\s+pixar|claymation|watercolor\s+illustration|anime/manga|"
    r"comic-book\s+illustration|paper-craft|voxel\s+/).*$",
    re.I,
)

# Old scriptgen baked compositor layout into scene text ("16:9 TV screen, 1920x1080").
_TV_FRAME_RE = re.compile(
    r"(?:,\s*)?(?:a\s+)?(?:fully\s+opaque\s+)?(?:16:9\s+)?(?:\(\s*\d+x\d+\s*\)\s*)?"
    r"(?:opaque\s+)?(?:TV|television|monitor)(?:-|\s+)?(?:screen|bezel|stand)?"
    r"(?:[^.]*?)(?:,\s*\d+x\d+)?\.?\s*$",
    re.I,
)
_TRAILING_SIZE_RE = re.compile(r",\s*\d{3,4}x\d{3,4}\.?\s*$", re.I)


def normalize_art_style(value: str | None) -> str:
    raw = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": DEFAULT_ART_STYLE,
        "default": DEFAULT_ART_STYLE,
        "bubble_pod": DEFAULT_ART_STYLE,
        "bubblepod": DEFAULT_ART_STYLE,
        "hand_drawn": DEFAULT_ART_STYLE,
        "handdrawn": DEFAULT_ART_STYLE,
        "2d": DEFAULT_ART_STYLE,
        "pixar": "pixar_3d",
        "disney": "pixar_3d",
        "3d_pixar": "pixar_3d",
        "3d_disney": "pixar_3d",
        "clay": "claymation",
        "stop_motion": "claymation",
        "manga": "anime",
        "comic": "comic_book",
        "comics": "comic_book",
        "papercraft": "paper_craft",
        "paper": "paper_craft",
        "blocks": "voxel",
        "minecraft": "voxel",
    }
    raw = aliases.get(raw, raw)
    if raw not in ART_STYLES:
        raise RuntimeError(
            f"Unknown art style: {value!r}. Use one of: {', '.join(ART_STYLES)}."
        )
    return raw


def list_art_styles() -> list[dict[str, Any]]:
    return [
        {
            "id": style_id,
            "label": entry["label"],
            "short": entry["short"],
            "default": style_id == DEFAULT_ART_STYLE,
        }
        for style_id, entry in ART_STYLES.items()
    ]


def art_style_texts(style_id: str | None = None, *, user_id: str | None = None) -> dict[str, str]:
    """Resolve short/full prompt text for a style id.

    Classic uses live Prompts overrides (art.style_short / art.style_full) when set.
    Other presets use the catalog copy.
    """
    style_id = normalize_art_style(style_id or DEFAULT_ART_STYLE)
    entry = ART_STYLES[style_id]
    if style_id == DEFAULT_ART_STYLE:
        from studio.prompts import get_prompt_raw

        short = (get_prompt_raw("art.style_short", user_id=user_id) or "").strip() or entry["short"]
        full = (get_prompt_raw("art.style_full", user_id=user_id) or "").strip() or entry["full"]
        return {"id": style_id, "label": entry["label"], "short": short, "full": full}
    return {
        "id": style_id,
        "label": entry["label"],
        "short": entry["short"],
        "full": entry["full"],
    }


def scene_subject(scene: str) -> str:
    """Keep the topic subject; drop art-style dumps and TV-as-object framing."""
    text = (scene or "").strip()
    if not text:
        return ""
    match = _STYLE_DUMP_RE.search(text)
    if match:
        text = text[: match.start()].strip(" ,.;")
    text = _TV_FRAME_RE.sub("", text).strip(" ,.;")
    text = _TRAILING_SIZE_RE.sub("", text).strip(" ,.;")
    return text


def _framing_bundle(aspect: str, layout: str, provider: str = "flux") -> dict:
    from studio.aspect import (
        canvas_phrase,
        canvas_size,
        generate_aspect,
        generate_at_line,
        illustration_size,
        is_portrait,
        normalize_aspect,
    )
    from studio.prompts import get_prompt
    from studio.settings import normalize_image_provider, normalize_video_layout

    aspect = normalize_aspect(aspect)
    layout = normalize_video_layout(layout)
    provider = normalize_image_provider(provider)
    video_w, video_h = canvas_size(aspect)
    gen_w, gen_h = illustration_size(aspect, layout)
    gen_aspect = generate_aspect(aspect, layout)
    size_line = generate_at_line(aspect, layout, provider=provider)
    phrase = canvas_phrase(aspect, layout)
    frame_kwargs = {"size_line": size_line, "phrase": phrase, "gen_w": gen_w, "gen_h": gen_h}
    if layout == "billboard":
        framing_key = (
            "art.framing_billboard_portrait" if is_portrait(aspect) else "art.framing_billboard_landscape"
        )
        ratio_line = get_prompt("art.ratio_billboard", **frame_kwargs)
    elif is_portrait(aspect):
        framing_key = "art.framing_cover_portrait"
        ratio_line = get_prompt("art.ratio_cover", **frame_kwargs)
    else:
        framing_key = "art.framing_cover_landscape"
        ratio_line = get_prompt("art.ratio_cover", **frame_kwargs)
    return {
        "aspect": aspect,
        "layout": layout,
        "video_w": video_w,
        "video_h": video_h,
        "width": gen_w,
        "height": gen_h,
        "gen_aspect": gen_aspect,
        "size_line": size_line,
        "phrase": phrase,
        "ratio_line": ratio_line,
        "framing": get_prompt(framing_key, **frame_kwargs),
    }


def wrap_scene_prompt(
    scene: str,
    title: str,
    summary: str,
    line_text: str,
    aspect: str = "16:9",
    layout: str = "cover",
    provider: str = "flux",
    art_style: str | None = None,
    user_id: str | None = None,
) -> str:
    """Build the image-model prompt for one line.

    Flux and ComfyUI use images.flux_instructions + art.* only — a short scene prompt.
    ChatGPT native uses art.scene_wrapper (may mention the image-UI preset).
    Neither path concatenates MCP playbook, script.rules, or TTS text.
    """
    from studio.prompts import get_prompt
    from studio.settings import normalize_image_provider, uses_short_image_prompt

    scene = scene_subject(scene or "")
    provider = normalize_image_provider(provider)
    style = art_style_texts(art_style, user_id=user_id)
    ctx = _framing_bundle(aspect, layout, provider=provider)
    style_kwargs = {
        "art_style_short": style["short"],
        "art_style_full": style["full"],
        "art_style": style["id"],
        "art_style_label": style["label"],
    }
    if uses_short_image_prompt(provider):
        w, h, lay = ctx["width"], ctx["height"], ctx["layout"]
        if lay == "billboard":
            ctx["framing"] = (
                "Full-frame topic diagram/illustration only. Almost no readable text."
            )
        elif h > w:
            ctx["framing"] = (
                f"{w}x{h} full-bleed. Fill the upper area with a clear diagram/illustration; "
                "keep the lower third empty for the host; almost no readable text."
            )
        else:
            ctx["framing"] = (
                f"{w}x{h} full-bleed. Fill the left/center with a clear diagram/illustration; "
                "keep the right third empty for the host; almost no readable text."
            )
        return get_prompt(
            "images.flux_instructions",
            scene=scene,
            title=title or "",
            summary=summary or "",
            line_text=line_text or "",
            user_id=user_id,
            **style_kwargs,
            **ctx,
        )
    return get_prompt(
        "art.scene_wrapper",
        title=title,
        summary=summary,
        line_text=line_text,
        scene=scene,
        user_id=user_id,
        **style_kwargs,
        **ctx,
    )
