"""Point Claude Desktop (and Claude Code, if present) at the Stickman Automation stdio MCP server."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from studio.paths import REPO_ROOT

SERVER_KEY_PREFERRED = "lazykh"
SERVER_KEY_ALIAS = "bubble-pod"
MCP_HTTP_PATH = "/mcp"


def _python_exe() -> str:
    return sys.executable or "python"


def studio_http_base() -> str:
    try:
        from studio.settings import load_settings

        data = load_settings()
        host = (data.get("host") or "127.0.0.1").strip() or "127.0.0.1"
        if host in ("0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        port = int(data.get("port") or 7878)
        return f"http://{host}:{port}"
    except Exception:
        return "http://127.0.0.1:7878"


def stdio_server_entry(*, claude_code: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "command": _python_exe(),
        "args": ["-m", "studio.mcp_server"],
        "cwd": str(REPO_ROOT),
        "env": {"PYTHONPATH": str(REPO_ROOT)},
    }
    if claude_code:
        entry["type"] = "stdio"
    return entry


def claude_desktop_config_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Claude" / "claude_desktop_config.json"


def claude_code_config_path() -> Path:
    return Path.home() / ".claude.json"


def codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _server_status(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    key = None
    if SERVER_KEY_PREFERRED in servers:
        key = SERVER_KEY_PREFERRED
    elif SERVER_KEY_ALIAS in servers:
        key = SERVER_KEY_ALIAS
    entry = servers.get(key) if key else None
    return {
        "path": str(path),
        "exists": path.is_file(),
        "configured": bool(key and isinstance(entry, dict)),
        "key": key,
        "server": entry if isinstance(entry, dict) else None,
    }


def _merge_server(path: Path, *, claude_code: bool, create: bool) -> dict[str, Any] | None:
    if not path.is_file() and not create:
        return None
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        data["mcpServers"] = servers
    entry = stdio_server_entry(claude_code=claude_code)
    if SERVER_KEY_PREFERRED in servers:
        key = SERVER_KEY_PREFERRED
    elif SERVER_KEY_ALIAS in servers:
        key = SERVER_KEY_ALIAS
    else:
        key = SERVER_KEY_PREFERRED
    servers[key] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "key": key, "server": entry}


def ensure_claude_mcp_config() -> dict[str, Any]:
    """Write/merge mcpServers.lazykh (or bubble-pod if that key already exists). Codex config is untouched."""
    desktop = _merge_server(claude_desktop_config_path(), claude_code=False, create=True)
    code = _merge_server(claude_code_config_path(), claude_code=True, create=False)
    return {
        "claude_desktop": desktop,
        "claude_code": code,
        "codex": "unchanged (~/.codex/config.toml mcp_servers.lazykh)",
        "ok": True,
    }


def codex_toml_snippet() -> str:
    entry = stdio_server_entry()
    cmd = entry["command"].replace("\\", "/")
    cwd = entry["cwd"].replace("\\", "/")
    return (
        "[mcp_servers.lazykh]\n"
        f'command = "{cmd}"\n'
        'args = ["-m", "studio.mcp_server"]\n'
        f'cwd = "{cwd}"\n'
        f'[mcp_servers.lazykh.env]\nPYTHONPATH = "{cwd}"\n'
    )


def mcp_settings_payload(*, http_mounted: bool = True) -> dict[str, Any]:
    """Status + copy/paste snippets for the Settings MCP card."""
    from studio.auth import mcp_pin_configured
    from studio.mcp_server import MCP_BUILD, MCP_TOOL_NAMES
    from studio.settings import public_settings

    entry = stdio_server_entry()
    http_url = f"{studio_http_base()}{MCP_HTTP_PATH}"
    pub = public_settings()
    pin_set = bool(pub.get("mcp_pin_set") or mcp_pin_configured())
    ngrok = None
    try:
        from studio.ngrok_tunnel import ngrok_status

        ngrok = ngrok_status(reveal_password=True)
    except Exception:
        ngrok = None
    public_url = (ngrok or {}).get("public_url") if (ngrok or {}).get("running") else None
    public_mcp = (ngrok or {}).get("mcp_url") if public_url else None
    pin_query = "?mcp_pin=YOUR_MCP_PIN"
    return {
        "ok": True,
        "mcp_build": MCP_BUILD,
        "mcp_tools": list(MCP_TOOL_NAMES),
        "tool_count": len(MCP_TOOL_NAMES),
        "http_mounted": bool(http_mounted),
        "http_path": MCP_HTTP_PATH,
        "http_url": http_url,
        "public_url": public_url,
        "public_mcp_url": public_mcp,
        "mcp_pin_set": pin_set,
        "auth_required": True,
        "ngrok": ngrok,
        "stdio": entry,
        "stdio_line": f'{entry["command"]} -m studio.mcp_server',
        "claude_desktop": _server_status(claude_desktop_config_path()),
        "claude_code": _server_status(claude_code_config_path()),
        "codex_path": str(codex_config_path()),
        "codex_snippet": codex_toml_snippet(),
        "chatgpt_hint": (
            "Remote HTTP MCP (ChatGPT URL connectors, Muse, etc.): use the Public MCP URL while ngrok is running. "
            "Authenticate with HTTP Basic only — username/password from Settings → Ngrok "
            f"(do not also send Authorization: Bearer; many clients drop Basic when both are set). "
            "Optional alternatives: append ?mcp_pin=YOUR_MCP_PIN, header X-MCP-Pin, or a Studio JWT. "
            f"Example PIN URL: {(public_mcp or http_url).rstrip('/')}{pin_query}. "
            "Local-only: "
            f"{http_url} with the same Basic credentials, PIN, or JWT. "
            "After Studio updates: fully quit ChatGPT, reopen, /mcp, new thread."
        ),
        "claude_hint": (
            "Claude Desktop / Claude Code use local stdio (python -m studio.mcp_server) — "
            "not HTTP JWT/PIN/Basic. Click Install Claude config to write mcpServers.lazykh, then quit and reopen Claude."
        ),
        "auth_hint": (
            "HTTP /mcp auth (any one): (1) ngrok HTTP Basic from Settings → Ngrok — preferred for remote agents; "
            "send Basic only, not Basic+Bearer. (2) MCP PIN via ?mcp_pin= / X-MCP-Pin / Bearer <pin>. "
            "(3) Studio login JWT. Edge ngrok checks Basic then strips Authorization; Studio trusts the tunnel hop. "
            "Docs/OpenAPI (/docs, /redoc, /openapi.json) require Studio login."
        ),
    }
