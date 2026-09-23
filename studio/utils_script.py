from __future__ import annotations

import re
import sys
from pathlib import Path

from studio.art_style import wrap_scene_prompt
from studio.paths import CODE_DIR, REPO_ROOT
from studio.script_rules import ALLOWED_EMOTIONS

if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from utils import getTopic, lineBillboardStem, removeTags  # type: ignore  # noqa: E402


# Labeled image-prompt lines MCP sometimes pastes into the script body.
_LABELED_IMAGE_LINE_RE = re.compile(
    r"^\s*(?:IMAGE|ILLUSTRATION|PROMPT|SCENE|ART\s*DIRECTION|FLUX|COVER)\s*:\s*.+$",
    re.I,
)

_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
_ART_PAREN_RE = re.compile(r"\(([^)]{3,})\)")

# Cue words that mark art-direction / illustration prompts (not spoken topics).
_ART_CUE_RE = re.compile(
    r"\b("
    r"motion\s+lines|film\s+set|soft\s+light|zoom\s+out|art\s+direction|"
    r"illustration|billboard|hand[- ]drawn|flat\s+colo(?:u)?rs|no\s+watermarks|"
    r"16:9|9:16|1920\s*[x×]\s*1080|1080\s*[x×]\s*1920|flux|comfyui|"
    r"full[- ]bleed|letterbox|subscribe\s+button|sound\s+waves|"
    r"volume\s+knob|warning\s+bell|tickle\s+meter|deleted\s+scenes|"
    r"director'?s\s+cap|glowing|skeptical\s+face"
    r")\b",
    re.I,
)


def is_illustration_cue(inner: str) -> bool:
    """True when bracket/paren text is a scene/art prompt, not a short spoken [topic]."""
    text = (inner or "").strip()
    if not text:
        return True
    words = text.split()
    if _ART_CUE_RE.search(text):
        return True
    if _LABELED_IMAGE_LINE_RE.match(text):
        return True
    # Legitimate spoken topics are short noun phrases: [tarantula], [black hole], [ice cube].
    if len(words) <= 3 and "," not in text:
        return False
    if "," in text:
        return True
    if len(words) >= 4:
        return True
    # "a brain scan", "an eraser wiping…" — indefinite article + scene noun phrase.
    if re.match(r"^(a|an|the)\s+\w+", text, re.I) and len(words) >= 3:
        return True
    return False


def extract_illustration_cues(tagged_line: str) -> list[str]:
    """Return illustration/scene cue strings embedded in a tagged line (brackets first)."""
    cues: list[str] = []
    for match in _BRACKET_RE.finditer(tagged_line or ""):
        inner = match.group(1).strip()
        if inner and is_illustration_cue(inner):
            cues.append(inner)
    for match in _ART_PAREN_RE.finditer(tagged_line or ""):
        inner = match.group(1).strip()
        if inner and is_illustration_cue(inner):
            cues.append(inner)
    return cues


def _strip_non_spoken_cues(text: str) -> str:
    """Remove image-prompt lines and illustration brackets/parens; keep short [topic] words."""

    def _bracket_sub(match: re.Match[str]) -> str:
        inner = match.group(1)
        if is_illustration_cue(inner):
            return ""
        return inner

    def _paren_sub(match: re.Match[str]) -> str:
        inner = match.group(1)
        if is_illustration_cue(inner):
            return ""
        return match.group(0)

    kept: list[str] = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if _LABELED_IMAGE_LINE_RE.match(line):
            continue
        kept.append(line)
    out = "\n".join(kept)
    out = _BRACKET_RE.sub(_bracket_sub, out)
    out = _ART_PAREN_RE.sub(_paren_sub, out)
    return out


def raw_from_tagged(tagged: str) -> str:
    """Derive TTS/Gentle spoken text: drop emotion tags and any illustration/art cues.

    Short [topic] noun phrases stay as spoken words (brackets removed).
    Long / descriptive / art-direction brackets and IMAGE:/PROMPT: lines are dropped.
    """
    cleaned = _strip_non_spoken_cues(tagged.replace("\r\n", "\n"))
    return removeTags(cleaned)


def parse_tagged_script(
    tagged: str,
    title: str = "",
    summary: str = "",
    scene_hints: list[str] | None = None,
    aspect: str = "16:9",
    layout: str = "cover",
    image_provider: str = "flux",
    art_style: str | None = None,
) -> list[dict]:
    tagged = tagged.replace("\r\n", "\n")
    raw_lines = tagged.split("\n")
    emotion = "explain"
    lines = []
    hint_i = 0
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            if lines:
                lines[-1]["section_break_after"] = True
            continue
        match = re.match(r"^<([a-zA-Z]+)>", stripped)
        if match:
            tag = match.group(1).lower()
            if tag in ALLOWED_EMOTIONS:
                emotion = tag
        topic = getTopic(stripped)
        # Short stable names (b001.png …) — topic text as a filename blows past Windows MAX_PATH.
        filename = lineBillboardStem(len(lines) + 1)
        spoken = raw_from_tagged(stripped).strip()
        illus_cues = extract_illustration_cues(stripped)
        scene = ""
        if scene_hints and hint_i < len(scene_hints):
            scene = scene_hints[hint_i]
            hint_i += 1
        elif illus_cues:
            # Prefer embedded scene prompts for pictures; never leave them in spoken/raw.
            scene = illus_cues[0]
        elif topic and topic.strip() and topic.strip() != spoken:
            scene = topic.strip()
        prompt = wrap_scene_prompt(
            scene or spoken,
            title,
            summary,
            spoken,
            aspect=aspect,
            layout=layout,
            provider=image_provider,
            art_style=art_style,
        )
        lines.append(
            {
                "tagged": stripped,
                "raw": spoken,
                "emotion": emotion,
                "topic": topic,
                "filename": filename,
                "scene": scene or spoken,
                "image_prompt": prompt,
                "section_break_after": False,
            }
        )
    return lines


_SUBSCRIBE_RE = re.compile(r"\bsubscribe\b", re.I)
_GREETING_RE = re.compile(
    r"^(hey(\s+(guys|everybody|everyone|there))?|hi(\s+(guys|everybody|everyone|there))?|"
    r"hello(\s+(guys|everybody|everyone|there|friends))?|welcome\s+back|"
    r"welcome\s+to\s+(my|this)\s+(channel|show|video)|what'?s\s+up|yo\b|"
    r"in\s+this\s+video|thanks\s+for\s+(watching|clicking)|"
    r"today\s+(we('re| are)|i('m| am))\s+(going\s+to|gonna)\s+(talk|learn|look)|"
    r"today\s+i\s+want\s+to\s+talk)",
    re.I,
)


DEFAULT_SHORTS_CTA = "If you want to know more, visit our channel in the link below."


def extract_hook_lines(tagged: str, max_lines: int = 3) -> list[str]:
    """First contiguous spoken lines (max 3), stopping at the first blank section break."""
    lines: list[str] = []
    for raw_line in (tagged or "").replace("\r\n", "\n").split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            if lines:
                break
            continue
        lines.append(stripped)
        if len(lines) >= max_lines:
            break
    return lines


def extract_hook_tagged(tagged: str, max_lines: int = 3) -> str:
    return "\n".join(extract_hook_lines(tagged, max_lines=max_lines))


def shorts_cta_spoken() -> str:
    """Editable 9:16-only spoken CTA from script.shorts_cta (never added to 16:9)."""
    try:
        from studio.prompts import get_prompt

        text = (get_prompt("script.shorts_cta") or "").strip()
    except Exception:
        text = ""
    if not text:
        text = DEFAULT_SHORTS_CTA
    # Allow a tagged override; otherwise strip any accidental tags for spoken form.
    if text.lstrip().startswith("<"):
        return raw_from_tagged(text).strip() or DEFAULT_SHORTS_CTA
    return text


def shorts_cta_tagged() -> str:
    """Tagged line for the 9:16 short CTA (appended after the hook)."""
    try:
        from studio.prompts import get_prompt

        text = (get_prompt("script.shorts_cta") or "").strip()
    except Exception:
        text = ""
    if not text:
        text = DEFAULT_SHORTS_CTA
    if text.lstrip().startswith("<"):
        return text.lstrip()
    return f"<happy> {text}"


def build_shorts_script(tagged: str) -> tuple[str, str]:
    """Hook lines + 9:16-only CTA. Returns (tagged, raw) for script_9x16.* files."""
    hook = extract_hook_tagged(tagged)
    if not hook.strip():
        raise RuntimeError("Script has no hook lines to build a 9:16 short.")
    tagged_out = (hook.rstrip() + "\n" + shorts_cta_tagged()).strip() + "\n"
    raw_out = raw_from_tagged(tagged_out).strip() + "\n"
    return tagged_out, raw_out


def script_structure_warnings(tagged: str) -> list[str]:
    """Warn if a script is missing a topic hook or subscribe outro. Never a hard error for saved/old scripts."""
    spoken = raw_from_tagged(tagged)
    words = spoken.split()
    if len(words) < 8:
        return []
    issues = []
    head = " ".join(words[:24])
    if _GREETING_RE.search(head):
        issues.append(
            "Missing topic hook: open with 1-3 spoken lines about THIS topic, not a generic greeting. "
            "That hook is also the 9:16 short."
        )
    tail = " ".join(words[-40:])
    if not _SUBSCRIBE_RE.search(tail):
        issues.append(
            "Missing subscribe outro: end with a short closer that actually says subscribe "
            "(16:9 full explainer only)."
        )
    return issues


def validate_tagged_script(tagged: str) -> list[str]:
    errors = []
    if "<" in tagged:
        tags = re.findall(r"<([^>]+)>", tagged)
        for tag in tags:
            if tag.lower() not in ALLOWED_EMOTIONS:
                errors.append(f"Unknown emotion tag <{tag}>. Allowed: {', '.join(ALLOWED_EMOTIONS)}")
        if tagged.count("<") != tagged.count(">"):
            errors.append("Unbalanced <emotion> tags.")
    if tagged.count("[") != tagged.count("]"):
        errors.append("Unbalanced [topic] brackets.")
    spoken = raw_from_tagged(tagged)
    if len(spoken.split()) < 8:
        errors.append("Script is too short to build a video.")
    return errors
