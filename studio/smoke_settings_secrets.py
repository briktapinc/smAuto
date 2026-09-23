"""Smoke: fal_key redaction + PUT ******** must not wipe secrets; MCP PIN hashed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Run from repo root with PYTHONPATH=.
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from studio.paths import SETTINGS_PATH, ensure_dirs  # noqa: E402
from studio.settings import (  # noqa: E402
    is_placeholder_secret,
    load_settings,
    public_settings,
    save_settings,
)


def main() -> int:
    ensure_dirs()
    backup = None
    if SETTINGS_PATH.is_file():
        backup = SETTINGS_PATH.read_text(encoding="utf-8")

    marker = "fal-smoke-test-key-DO-NOT-COMMIT-9f3a"
    try:
        save_settings({"fal_key": marker})
        raw = load_settings()
        assert raw.get("fal_key") == marker, "fal_key not stored"

        pub = public_settings()
        assert pub.get("fal_key") == "********", f"public fal_key not redacted: {pub.get('fal_key')!r}"
        assert pub.get("fal_key_set") is True
        assert marker not in json.dumps(pub), "raw fal_key leaked in public_settings"

        # Simulate Settings UI / MCP update with placeholder
        save_settings({"fal_key": "********"})
        assert load_settings().get("fal_key") == marker, "******** wiped fal_key"

        save_settings({"fal_key": ""})
        assert load_settings().get("fal_key") == marker, "empty string wiped fal_key"

        save_settings({"fal_key": None})  # scrubbed before write in API; direct call
        # None is placeholder — scrub_secret_updates in save_settings path via SECRET_KEYS
        from studio.settings import scrub_secret_updates

        scrubbed = scrub_secret_updates({"fal_key": None, "openai_model": "gpt-4o"})
        assert "fal_key" not in scrubbed
        assert scrubbed.get("openai_model") == "gpt-4o"
        assert is_placeholder_secret("********")
        assert is_placeholder_secret("")
        assert not is_placeholder_secret(marker)

        # MCP PIN hashing
        from studio.auth import ensure_mcp_pin, hash_password, verify_mcp_pin

        pin_info = ensure_mcp_pin()
        assert pin_info["mcp_pin_set"]
        settings = load_settings()
        assert settings.get("mcp_pin_hash"), "mcp_pin_hash missing"
        assert "mcp_pin" not in settings or not settings.get("mcp_pin")
        pub2 = public_settings()
        assert pub2.get("mcp_pin_set") is True
        assert "mcp_pin_hash" not in pub2
        assert pub2.get("mcp_pin") == ""
        if pin_info.get("generated") and pin_info.get("pin"):
            assert verify_mcp_pin(pin_info["pin"])

        print("OK: fal_key redaction + no-wipe; MCP PIN hashed/public-safe")
        if pin_info.get("generated"):
            print(f"GENERATED_MCP_PIN={pin_info['pin']}")
        return 0
    finally:
        if backup is not None:
            # Restore prior settings but keep security fields we may have created.
            try:
                current = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except Exception:
                current = {}
            try:
                prior = json.loads(backup)
            except Exception:
                prior = {}
            for key in (
                "mcp_pin_hash",
                "ngrok_basic_auth_user",
                "ngrok_basic_auth_password",
            ):
                if current.get(key) and not prior.get(key):
                    prior[key] = current[key]
            # Always restore fal_key from backup (smoke marker must not linger)
            SETTINGS_PATH.write_text(json.dumps(prior, indent=2) + "\n", encoding="utf-8")
        else:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
