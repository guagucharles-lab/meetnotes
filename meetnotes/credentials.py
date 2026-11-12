"""The Anthropic API key: environment variable first, then the OS credential
store (Windows Credential Manager, macOS Keychain, Linux Secret Service), so
the desktop app can save it without a plain-text file."""
from __future__ import annotations

import os

SERVICE, USER = "meetnotes", "anthropic_api_key"


def get_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        import keyring
        return keyring.get_password(SERVICE, USER)
    except Exception:
        return None


def set_api_key(key: str) -> None:
    import keyring
    if key:
        keyring.set_password(SERVICE, USER, key.strip())
    else:
        try:
            keyring.delete_password(SERVICE, USER)
        except Exception:
            pass


def has_api_key() -> bool:
    return bool(get_api_key() or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
