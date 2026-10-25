"""Remembered settings. The API key goes to the OS credential store when available."""

from __future__ import annotations

import json
import logging

from ..paths import data_dir

log = logging.getLogger(__name__)
SETTINGS_FILE = data_dir() / "settings.json"
KEYRING_SERVICE = "MontageDirector"


def load() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(values: dict) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps(values, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save settings: %s", exc)


def get_api_key() -> str:
    try:
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, "anthropic")
        if key:
            return key
    except Exception:
        pass
    return load().get("api_key_fallback", "")


def set_api_key(key: str) -> str:
    """Store the key; returns where it was stored (for the settings dialog)."""
    key = key.strip()
    values = load()
    try:
        import keyring

        if key:
            keyring.set_password(KEYRING_SERVICE, "anthropic", key)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, "anthropic")
            except Exception:
                pass
        values.pop("api_key_fallback", None)
        save(values)
        return "your system's password manager"
    except Exception:
        values["api_key_fallback"] = key
        save(values)
        return "the app's settings file"
