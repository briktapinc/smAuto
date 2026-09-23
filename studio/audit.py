"""JSONL audit log for MCP tool calls and significant HTTP API actions."""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any

from studio.paths import USER_DATA, ensure_dirs

AUDIT_PATH = USER_DATA / "audit.log"
_lock = threading.Lock()
_MAX_ARG_CHARS = 400
_MAX_READ_BYTES = 512_000

_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|mcp_pin|pin|jwt|"
    r"bearer|cookie|fal_key|openai_api_key|elevenlabs|client_secret|spend_confirm)",
    re.I,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact_value(key: str, value: Any) -> Any:
    if _SECRET_KEY_RE.search(str(key or "")):
        if value in (None, "", False, True, 0):
            return value
        return "***"
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(key, v) for v in value[:40]]
    if isinstance(value, str) and len(value) > _MAX_ARG_CHARS:
        return value[:_MAX_ARG_CHARS] + "…"
    return value


def summarize_args(args: Any) -> dict[str, Any] | list | str | None:
    if args is None:
        return None
    if isinstance(args, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(args.items()):
            if i >= 24:
                out["…"] = f"+{len(args) - 24} more"
                break
            out[str(k)] = redact_value(str(k), v)
        return out
    if isinstance(args, (list, tuple)):
        return [redact_value("", v) for v in list(args)[:24]]
    if isinstance(args, str):
        return args[:_MAX_ARG_CHARS] + ("…" if len(args) > _MAX_ARG_CHARS else "")
    try:
        return redact_value("", args)
    except Exception:
        return str(args)[:_MAX_ARG_CHARS]


def write_entry(
    *,
    action: str,
    source: str = "api",
    ip: str = "",
    username: str = "",
    auth_method: str = "",
    success: bool = True,
    error: str = "",
    args: Any = None,
    status: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": _now_iso(),
        "action": action,
        "source": source,
        "ip": ip or "",
        "username": username or "",
        "auth_method": auth_method or "",
        "success": bool(success),
    }
    if error:
        entry["error"] = str(error)[:500]
    if status is not None:
        entry["status"] = int(status)
    summary = summarize_args(args)
    if summary is not None:
        entry["args"] = summary
    if extra:
        entry["extra"] = summarize_args(extra)
    line = json.dumps(entry, ensure_ascii=False, default=str)
    ensure_dirs()
    with _lock:
        with AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return entry


def read_entries(limit: int = 100) -> list[dict[str, Any]]:
    """Return the last N audit entries (newest last)."""
    n = max(1, min(500, int(limit or 100)))
    if not AUDIT_PATH.is_file():
        return []
    try:
        raw = AUDIT_PATH.read_bytes()
        if len(raw) > _MAX_READ_BYTES:
            raw = raw[-_MAX_READ_BYTES:]
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out: list[dict[str, Any]] = []
    for ln in lines[-n:]:
        try:
            row = json.loads(ln)
            if isinstance(row, dict):
                out.append(row)
        except json.JSONDecodeError:
            continue
    return out


def audit_public(limit: int = 50) -> dict[str, Any]:
    entries = read_entries(limit)
    return {
        "path": str(AUDIT_PATH),
        "count": len(entries),
        "entries": entries,
    }
