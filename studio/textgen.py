"""Billed OpenAI vs local LM Studio vs native ChatGPT/Claude MCP text generation."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from studio.prompts import get_prompt
from studio.script_rules import with_playbook_runtime_notes
from studio.settings import (
    LMSTUDIO_DEFAULT_BASE_URL,
    current_text_provider,
    is_api_text_provider,
    is_native_text_provider,
    load_settings,
    normalize_lmstudio_base_url,
    text_provider_label,
)

LMSTUDIO_DUMMY_KEY = "lm-studio"
LMSTUDIO_MAX_TOKENS = 16384
LMSTUDIO_TIMEOUT_SEC = 900.0
LMSTUDIO_RAW_DUMP_NAME = "script_lmstudio_raw.txt"
_LMSTUDIO_UNREACHABLE = (
    "LM Studio isn't running. Start the LM Studio local server."
)
_LMSTUDIO_JSON_SYSTEM = (
    "Output ONLY a single JSON object. No markdown fences, no commentary before or after. "
    "Inside strings, escape newlines as \\n and double quotes as \\\". "
    "No trailing commas, no comments, no single-quoted keys."
)
_JSON_RETRY_USER = (
    "Your previous reply was not valid JSON. Reply with ONLY one JSON object. "
    "No markdown fences, no commentary. Escape newlines inside strings as \\n "
    "and double quotes as \\\". No trailing commas."
)
_JSON_CONTINUE_USER = (
    "Your JSON was cut off mid-string or mid-object. Continue EXACTLY from the last "
    "character of your previous message. Output only the missing suffix that completes "
    "the JSON object. Do not repeat earlier text. No markdown fences, no commentary."
)
_TRUNCATION_MARKERS = (
    "unterminated string",
    "unterminated",
    "unexpected eof",
    "end of document",
    "expecting ',' delimiter",
    "expecting ':'",
    "expecting value",
    "expecting property name",
)
_FENCE_RE = re.compile(
    r"```(?:json|javascript|js)?\s*\r?\n?(.*?)```",
    re.IGNORECASE | re.DOTALL,
)


def native_script_handoff(project_id: str = "", extra: str = "") -> dict[str, Any]:
    """Playbook + illustration notes for ChatGPT/Claude Desktop. Does not call OpenAI."""
    provider = current_text_provider()
    label = text_provider_label(provider)
    playbook = with_playbook_runtime_notes(get_prompt("mcp.chatgpt_playbook"))
    illustration_notes = get_prompt("images.chatgpt_instructions")
    payload: dict[str, Any] = {
        "ok": True,
        "provider": provider,
        "script_provider": provider,
        "use_mcp": True,
        "playbook_hint": playbook,
        "illustration_notes": illustration_notes,
        "script_rules": get_prompt("script.rules"),
        "message": (
            f"Use {label} Desktop MCP: write the tagged script (topic hook + subscribe outro, "
            "emotion tags) then save_script. Do not call generate_script_via_api — that spends "
            "OpenAI tokens."
        ),
    }
    extra = (extra or "").strip()
    if extra:
        payload["extra"] = extra
    pid = (project_id or "").strip()
    if not pid:
        return payload
    payload["project_id"] = pid
    try:
        from studio.projects import load_meta

        meta = load_meta(pid)
        payload["topic"] = meta.get("topic")
        payload["title"] = meta.get("title")
        payload["duration_seconds"] = meta.get("duration_seconds")
        payload["aspect"] = meta.get("aspect")
        payload["video_layout"] = meta.get("video_layout")
        payload["image_provider"] = meta.get("image_provider")
    except Exception:
        pass
    try:
        from studio.illustrations import illustration_jobs

        jobs = illustration_jobs(pid)
        payload["illustration_jobs"] = jobs.get("jobs") or []
        payload["image_provider"] = jobs.get("image_provider") or payload.get("image_provider")
    except Exception:
        pass
    return payload


def native_topics_handoff(seed: str = "", count: int = 8, duration_min: float = 2) -> dict[str, Any]:
    """Topic-generation playbook for ChatGPT/Claude Desktop. Does not call OpenAI."""
    provider = current_text_provider()
    label = text_provider_label(provider)
    playbook = with_playbook_runtime_notes(get_prompt("mcp.chatgpt_playbook"))
    prompt = get_prompt(
        "topics.generate",
        seed=(seed or "").strip() or "(none — invent a varied mix)",
        count=count,
        duration_min=duration_min,
    )
    return {
        "ok": True,
        "provider": provider,
        "script_provider": provider,
        "use_mcp": True,
        "playbook_hint": playbook,
        "topics_prompt": prompt,
        "seed": (seed or "").strip(),
        "count": count,
        "duration_min": duration_min,
        "created": [],
        "message": (
            f"Use {label} Desktop MCP: invent {count} original explainer topics "
            "(title + one-sentence angle), then create_topic and/or schedule_topic. "
            "Do not call generate_topics — that spends OpenAI tokens."
        ),
    }


def billed_text_enabled() -> bool:
    return is_api_text_provider()


def script_progress_detail() -> str:
    return f"Writing tagged and raw scripts with {text_provider_label()}..."


def _strip_code_fences(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return text
    match = _FENCE_RE.search(text)
    if match:
        inner = match.group(1).strip()
        if inner.startswith("{") or inner.startswith("["):
            return inner
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|javascript|js)?\s*", "", text, count=1, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


def _extract_json_blob(text: str) -> str:
    start_obj = text.find("{")
    start_arr = text.find("[")
    if start_obj < 0 and start_arr < 0:
        return text
    if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
        start, end = start_arr, text.rfind("]")
    else:
        start, end = start_obj, text.rfind("}")
    if end > start:
        return text[start : end + 1]
    return text[start:]


def _escape_raw_controls_in_strings(text: str) -> str:
    """Escape raw newlines/tabs and unescaped inner quotes inside JSON strings."""
    out: list[str] = []
    in_str = False
    escape = False
    n = len(text)

    def _is_string_closer(idx: int) -> bool:
        j = idx + 1
        while j < n and text[j] in " \t":
            j += 1
        if j >= n:
            return True
        if text[j] in ":}]\n\r":
            return True
        if text[j] == ",":
            k = j + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k >= n:
                return True
            # JSON continues with another value/key; prose after a comma is an inner quote.
            return text[k] in "\"{[}]" or text[k] in "tfn-0123456789"
        while j < n and text[j] in " \t\r\n":
            j += 1
        if j >= n:
            return True
        return text[j] in ",:}\]\""

    i = 0
    while i < n:
        ch = text[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            i += 1
            continue
        if escape:
            out.append(ch)
            escape = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escape = True
            i += 1
            continue
        if ch == '"':
            if _is_string_closer(i):
                out.append(ch)
                in_str = False
            else:
                out.append('\\"')
            i += 1
            continue
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _remove_trailing_commas(text: str) -> str:
    out: list[str] = []
    in_str = False
    escape = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _insert_missing_commas(text: str) -> str:
    """Insert commas between adjacent JSON values (e.g. `}{` or `".."\\n  ".."`)."""
    out: list[str] = []
    in_str = False
    escape = False
    last_was_value = False
    i = 0
    n = len(text)

    def _peek_nonspace(idx: int) -> int:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        return idx

    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
                nxt = _peek_nonspace(i + 1)
                last_was_value = not (nxt < n and text[nxt] == ":")
            i += 1
            continue
        if ch in " \t\r\n":
            out.append(ch)
            i += 1
            continue
        if last_was_value and ch not in ",:}]":
            out.append(",")
        last_was_value = False
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch in "{[":
            out.append(ch)
            i += 1
            continue
        if ch in "}]":
            last_was_value = True
            out.append(ch)
            i += 1
            continue
        if ch in ",:":
            out.append(ch)
            i += 1
            continue
        if ch == "t" and text[i : i + 4] == "true":
            out.append("true")
            i += 4
            last_was_value = True
            continue
        if ch == "f" and text[i : i + 5] == "false":
            out.append("false")
            i += 5
            last_was_value = True
            continue
        if ch == "n" and text[i : i + 4] == "null":
            out.append("null")
            i += 4
            last_was_value = True
            continue
        if ch == "-" or ch.isdigit():
            j = i + 1
            while j < n and text[j] in "0123456789.eE+-":
                j += 1
            out.append(text[i:j])
            i = j
            last_was_value = True
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _repair_json(text: str) -> str:
    return _insert_missing_commas(_remove_trailing_commas(_escape_raw_controls_in_strings(text)))


def parse_json_object(content: str) -> Any:
    stripped = _strip_code_fences(content)
    blobs = []
    for candidate in (stripped, _extract_json_blob(stripped)):
        if candidate and candidate not in blobs:
            blobs.append(candidate)
    last_err: json.JSONDecodeError | None = None
    tried: set[str] = set()
    for blob in blobs:
        for variant in (blob, _repair_json(blob)):
            if variant in tried:
                continue
            tried.add(variant)
            for strict in (True, False):
                try:
                    return json.loads(variant, strict=strict)
                except json.JSONDecodeError as exc:
                    last_err = exc
    if last_err:
        raise last_err
    raise json.JSONDecodeError("Expecting value", content or "", 0)


def in_string_at_eof(text: str) -> bool:
    """True when a JSON scan ends inside an open string (truncated / unterminated)."""
    in_str = False
    escape = False
    for ch in text or "":
        if not in_str:
            if ch == '"':
                in_str = True
            continue
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = False
    return in_str


def is_truncated_json_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if isinstance(exc, json.JSONDecodeError) and any(n in msg for n in _TRUNCATION_MARKERS):
        return True
    return any(n in msg for n in ("unterminated string", "unexpected eof", "unterminated"))


def looks_truncated_json(text: str, finish_reason: str = "", exc: BaseException | None = None) -> bool:
    reason = (finish_reason or "").lower()
    if reason in ("length", "max_tokens", "max_completion_tokens"):
        return True
    if in_string_at_eof(text):
        return True
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.count("{") > stripped.count("}"):
        return True
    if stripped.startswith("[") and stripped.count("[") > stripped.count("]"):
        return True
    if exc is not None and is_truncated_json_error(exc) and (
        stripped.startswith("{") or stripped.startswith("[")
    ):
        return True
    return False


def join_json_continuation(prefix: str, suffix: str) -> str:
    """Glue a truncated JSON prefix to a continuation suffix (or keep a full rewrite)."""
    suf = _strip_code_fences(suffix or "").strip()
    if suf.startswith("{") or suf.startswith("["):
        return suf
    return (prefix or "") + (suffix or "")


def save_raw_model_text(
    path: str | Path | None,
    text: str,
    *,
    label: str = "",
    append: bool = True,
) -> None:
    """Write raw model output for debug. No-op when path is empty."""
    if not path:
        return
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        header = f"===== {label or 'model'} {stamp} =====\n"
        blob = header + (text or "") + "\n\n"
        if append and target.is_file():
            target.write_text(target.read_text(encoding="utf-8") + blob, encoding="utf-8")
        else:
            target.write_text(blob, encoding="utf-8")
    except OSError:
        pass


def _is_json_parse_failure(exc: BaseException) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "jsondecode" in name:
        return True
    return "invalid json" in msg or "unterminated string" in msg or "unexpected eof" in msg


def _invalid_json_error(exc: BaseException) -> RuntimeError:
    msg = str(exc).strip() or type(exc).__name__
    if msg.lower().startswith("model returned invalid json"):
        return RuntimeError(msg if msg.endswith(".") else msg + ".")
    return RuntimeError(f"Model returned invalid JSON: {exc}.")


def _is_unreachable(exc: BaseException) -> bool:
    if _is_json_parse_failure(exc):
        return False
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    blob = f"{name} {msg}"
    needles = (
        "connection refused",
        "connecterror",
        "connect error",
        "connection error",
        "apiconnectionerror",
        "connecttimeout",
        "failed to establish",
        "name or service not known",
        "nodename nor servname",
        "actively refused",
        "10061",
        "winerror 10061",
        "network is unreachable",
        "no connection could be made",
        "connection aborted",
    )
    return any(n in blob for n in needles)


def _wrap_lmstudio_error(exc: BaseException) -> RuntimeError:
    if _is_json_parse_failure(exc):
        return _invalid_json_error(exc)
    msg = str(exc).strip() or type(exc).__name__
    lower = msg.lower()
    if _is_unreachable(exc):
        return RuntimeError(_LMSTUDIO_UNREACHABLE)
    if "no models loaded" in lower or "model_not_found" in lower or "does not exist" in lower:
        return RuntimeError(
            "No model is loaded in LM Studio. Load a model, or set lmstudio_model in Settings."
        )
    return RuntimeError(msg)


def _first_lmstudio_model(client: Any) -> str:
    try:
        listing = client.models.list()
    except Exception as exc:
        raise _wrap_lmstudio_error(exc) from exc
    rows = getattr(listing, "data", None) or []
    ids: list[str] = []
    for row in rows:
        ident = getattr(row, "id", None)
        if not ident and isinstance(row, dict):
            ident = row.get("id")
        ident = str(ident or "").strip()
        if ident:
            ids.append(ident)
    if not ids:
        raise RuntimeError(
            "No model is loaded in LM Studio. Load a model, or set lmstudio_model in Settings."
        )
    return ids[0]


def chat_completion_client() -> tuple[Any, str, str]:
    """OpenAI-compatible client + model + provider for openai / lmstudio. Never MCP-native."""
    from openai import OpenAI

    settings = load_settings()
    provider = current_text_provider()
    if is_native_text_provider(provider):
        raise RuntimeError(
            f"text_provider is {provider}. Write the script in {text_provider_label(provider)} "
            "Desktop MCP (save_script). Do not call the OpenAI API."
        )
    if provider == "lmstudio":
        base = normalize_lmstudio_base_url(settings.get("lmstudio_base_url")) or LMSTUDIO_DEFAULT_BASE_URL
        client = OpenAI(base_url=base, api_key=LMSTUDIO_DUMMY_KEY, timeout=LMSTUDIO_TIMEOUT_SEC)
        model = str(settings.get("lmstudio_model") or "").strip()
        if not model:
            model = _first_lmstudio_model(client)
        return client, model, provider
    key = settings.get("openai_api_key") or ""
    if not key:
        raise RuntimeError("OpenAI API key is not set. Add it in Settings.")
    model = settings.get("openai_model") or "gpt-4o"
    return OpenAI(api_key=key), model, provider


def _with_lmstudio_json_instructions(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = [dict(item) for item in messages]
    if rows and rows[0].get("role") == "system":
        body = str(rows[0].get("content") or "").rstrip()
        if _LMSTUDIO_JSON_SYSTEM not in body:
            rows[0]["content"] = body + "\n\n" + _LMSTUDIO_JSON_SYSTEM
        return rows
    rows.insert(0, {"role": "system", "content": _LMSTUDIO_JSON_SYSTEM})
    return rows


def _chat_create(
    client: Any,
    kwargs: dict[str, Any],
    provider: str,
    *,
    json_object: bool = True,
) -> Any:
    if json_object:
        try:
            return client.chat.completions.create(
                **kwargs, response_format={"type": "json_object"}
            )
        except Exception as fmt_exc:
            if provider != "lmstudio":
                raise
            if _is_unreachable(fmt_exc):
                raise _wrap_lmstudio_error(fmt_exc) from fmt_exc
    return client.chat.completions.create(**kwargs)


def _response_payload(response: Any) -> tuple[str, str]:
    if not getattr(response, "choices", None):
        return "{}", ""
    choice = response.choices[0]
    message = getattr(choice, "message", None)
    text = (getattr(message, "content", None) if message is not None else None) or "{}"
    reason = str(getattr(choice, "finish_reason", None) or "")
    return text, reason


def chat_json(
    *,
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int | None = None,
    raw_dump_path: str | Path | None = None,
) -> tuple[Any, str, str]:
    """Chat completion expecting a JSON object. Uses OpenAI cloud or local LM Studio."""
    client, model, provider = chat_completion_client()
    if provider == "lmstudio":
        messages = _with_lmstudio_json_instructions(messages)
        if max_tokens is None:
            max_tokens = LMSTUDIO_MAX_TOKENS

    def _complete(
        msgs: list[dict[str, str]],
        temp: float,
        *,
        json_object: bool = True,
    ) -> tuple[str, str]:
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temp,
            "messages": msgs,
        }
        if max_tokens:
            kwargs["max_tokens"] = int(max_tokens)
        try:
            response = _chat_create(client, kwargs, provider, json_object=json_object)
        except Exception as exc:
            if (
                provider == "lmstudio"
                and "max_tokens" in kwargs
                and any(n in str(exc).lower() for n in ("max_tokens", "max_completion", "context length"))
            ):
                kwargs.pop("max_tokens", None)
                try:
                    response = _chat_create(client, kwargs, provider, json_object=json_object)
                except Exception as exc2:
                    raise _wrap_lmstudio_error(exc2) from exc2
            elif provider == "lmstudio":
                raise _wrap_lmstudio_error(exc) from exc
            else:
                raise
        return _response_payload(response)

    dumps: list[tuple[str, str]] = []

    def _parse_or_note(text: str, label: str) -> Any:
        try:
            return parse_json_object(text)
        except json.JSONDecodeError:
            dumps.append((label, text))
            raise

    def _flush_dumps() -> None:
        for label, text in dumps:
            save_raw_model_text(raw_dump_path, text, label=label)

    content, finish_reason = _complete(messages, temperature)
    try:
        return _parse_or_note(content, f"attempt1 finish_reason={finish_reason or 'unknown'}"), model, provider
    except json.JSONDecodeError as first_err:
        truncated = looks_truncated_json(content, finish_reason, first_err)
        retry_temp = min(float(temperature), 0.3)
        if truncated:
            continue_messages = list(messages) + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": _JSON_CONTINUE_USER},
            ]
            try:
                suffix, cont_reason = _complete(
                    continue_messages, retry_temp, json_object=False
                )
                combined = join_json_continuation(content, suffix)
                return (
                    _parse_or_note(
                        combined,
                        f"continuation finish_reason={cont_reason or 'unknown'}",
                    ),
                    model,
                    provider,
                )
            except json.JSONDecodeError:
                pass
            except Exception as exc:
                if _is_json_parse_failure(exc):
                    pass
                else:
                    _flush_dumps()
                    raise
        retry_messages = list(messages) + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": _JSON_RETRY_USER},
        ]
        try:
            content, retry_reason = _complete(retry_messages, retry_temp)
            return (
                _parse_or_note(content, f"rewrite finish_reason={retry_reason or 'unknown'}"),
                model,
                provider,
            )
        except json.JSONDecodeError as exc:
            _flush_dumps()
            raise _invalid_json_error(exc) from exc
        except Exception:
            _flush_dumps()
            raise

