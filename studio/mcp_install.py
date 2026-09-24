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
    """Loopback HTTP base for local clients (stdio installers, LAN)."""
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


def studio_public_base() -> str:
    """Public HTTPS/HTTP base for remote MCP (PUBLIC_BASE_URL, then ngrok, then local)."""
    try:
        from studio.settings import resolve_public_base_url

        return resolve_public_base_url().rstrip("/")
    except Exception:
        return studio_http_base()


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
    from studio.settings import is_production, normalize_public_tunnel, public_settings

    entry = stdio_server_entry()
    local_base = studio_http_base().rstrip("/")
    public_base = studio_public_base().rstrip("/")
    http_url = f"{local_base}{MCP_HTTP_PATH}"
    pub = public_settings()
    pin_set = bool(pub.get("mcp_pin_set") or mcp_pin_configured())
    ngrok = None
    try:
        from studio.ngrok_tunnel import ngrok_status

        ngrok = ngrok_status(reveal_password=True)
    except Exception:
        ngrok = None
    tailscale = None
    try:
        from studio.tailscale_tunnel import tailscale_status

        tailscale = tailscale_status()
    except Exception:
        tailscale = None
    tunnel = normalize_public_tunnel(pub.get("public_tunnel"))
    ngrok_public = (ngrok or {}).get("public_url") if (ngrok or {}).get("running") else None
    ngrok_mcp = (ngrok or {}).get("mcp_url") if ngrok_public else None
    ts_public = (tailscale or {}).get("public_url") if (tailscale or {}).get("running") else None
    ts_mcp = (tailscale or {}).get("mcp_url") if ts_public else None
    # Selected tunnel wins. With no selection, a live ngrok URL still overrides the public base.
    if tunnel == "tailscale" and ts_mcp:
        public_mcp = str(ts_mcp).rstrip("/")
        public_url = str(ts_public).rstrip("/")
    elif tunnel == "ngrok" and ngrok_mcp:
        public_mcp = str(ngrok_mcp).rstrip("/")
        public_url = str(ngrok_public).rstrip("/")
    elif tunnel == "ngrok" and str(pub.get("ngrok_url") or "").startswith("http"):
        public_url = str(pub.get("ngrok_url") or "").rstrip("/")
        public_mcp = f"{public_url}{MCP_HTTP_PATH}"
    elif tunnel == "":
        if ngrok_mcp:
            public_mcp = str(ngrok_mcp).rstrip("/")
            public_url = str(ngrok_public).rstrip("/")
        elif public_base:
            public_mcp = f"{public_base}{MCP_HTTP_PATH}"
            public_url = public_base
        else:
            public_mcp = None
            public_url = None
    elif public_base and public_base != local_base:
        public_mcp = f"{public_base}{MCP_HTTP_PATH}"
        public_url = public_base
    else:
        public_mcp = None
        public_url = None
    # Prefer showing the public URL in the primary HTTP field when it differs from loopback
    # (production VPS / configured PUBLIC_BASE_URL / selected tunnel). Keep local_url for LAN/stdio clients.
    primary_http = public_mcp if (public_mcp and public_mcp != http_url) else http_url
    pin_query = "?mcp_pin=YOUR_MCP_PIN"
    prod = is_production()
    if tunnel == "tailscale":
        shown = public_mcp or f"{local_base}{MCP_HTTP_PATH}"
        chatgpt_hint = (
            f"Tailscale Funnel is the public tunnel. Remote MCP URL: {shown}. "
            "Authenticate with the MCP PIN (?mcp_pin= / X-MCP-Pin) or a Studio JWT. "
            "Funnel does not use ngrok Basic auth. "
            f"Example PIN URL: {shown}{pin_query}. "
            f"Loopback-only: {http_url}. "
            "After Studio updates: fully quit ChatGPT, reopen, /mcp, new thread."
        )
    elif tunnel == "ngrok":
        shown = public_mcp or http_url
        chatgpt_hint = (
            f"Ngrok is the public tunnel. Remote MCP URL: {shown}. "
            "Authenticate with HTTP Basic only — username/password from Settings → Ngrok "
            "(do not also send Authorization: Bearer; many clients drop Basic when both are set). "
            "Optional alternatives: append ?mcp_pin=YOUR_MCP_PIN, header X-MCP-Pin, or a Studio JWT. "
            f"Example PIN URL: {shown.rstrip('/')}{pin_query}. "
            "After Studio updates: fully quit ChatGPT, reopen, /mcp, new thread."
        )
    elif prod or (public_base and public_base != local_base):
        chatgpt_hint = (
            f"Remote HTTP MCP: use {public_mcp} "
            "(PUBLIC_BASE_URL / production domain). "
            "Authenticate with MCP PIN (?mcp_pin= / X-MCP-Pin), Studio JWT, or ngrok HTTP Basic "
            "if you also run a reserved-domain tunnel. "
            f"Example PIN URL: {public_mcp}{pin_query}. "
            f"Loopback-only: {http_url}. "
            "After Studio updates: fully quit ChatGPT, reopen, /mcp, new thread."
        )
    else:
        chatgpt_hint = (
            "Remote HTTP MCP (ChatGPT URL connectors, Muse, etc.): use the Public MCP URL while ngrok is running. "
            "Authenticate with HTTP Basic only — username/password from Settings → Ngrok "
            "(do not also send Authorization: Bearer; many clients drop Basic when both are set). "
            "Optional alternatives: append ?mcp_pin=YOUR_MCP_PIN, header X-MCP-Pin, or a Studio JWT. "
            f"Example PIN URL: {(public_mcp or http_url).rstrip('/')}{pin_query}. "
            "Local-only: "
            f"{http_url} with the same Basic credentials, PIN, or JWT. "
            "After Studio updates: fully quit ChatGPT, reopen, /mcp, new thread."
        )
    return {
        "ok": True,
        "mcp_build": MCP_BUILD,
        "mcp_tools": list(MCP_TOOL_NAMES),
        "tool_count": len(MCP_TOOL_NAMES),
        "http_mounted": bool(http_mounted),
        "http_path": MCP_HTTP_PATH,
        "http_url": primary_http,
        "local_http_url": http_url,
        "public_base_url": public_base,
        "public_url": public_url,
        "public_mcp_url": public_mcp,
        "mcp_pin_set": pin_set,
        "auth_required": True,
        "public_tunnel": tunnel,
        "tailscale": tailscale,
        "ngrok": ngrok,
        "stdio": entry,
        "stdio_line": f'{entry["command"]} -m studio.mcp_server',
        "claude_desktop": _server_status(claude_desktop_config_path()),
        "claude_code": _server_status(claude_code_config_path()),
        "codex_path": str(codex_config_path()),
        "codex_snippet": codex_toml_snippet(),
        "chatgpt_hint": chatgpt_hint,
        "claude_hint": (
            "Claude Desktop / Claude Code use local stdio (python -m studio.mcp_server) — "
            "not HTTP JWT/PIN/Basic. Click Install Claude config to write mcpServers.lazykh, then quit and reopen Claude."
        ),
        "auth_hint": (
            "HTTP /mcp auth (any one): (1) MCP PIN via ?mcp_pin= / X-MCP-Pin / Bearer <pin> "
            "(use this for Tailscale Funnel). "
            "(2) Studio login JWT. (3) ngrok HTTP Basic from Settings → Ngrok when Ngrok is the selected tunnel — "
            "send Basic only, not Basic+Bearer. Edge ngrok checks Basic then strips Authorization; "
            "Studio trusts the tunnel hop. Docs/OpenAPI (/docs, /redoc, /openapi.json) require Studio login."
        ),
    }
