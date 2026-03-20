"""
User-level config following the XDG Base Directory Specification.

Config file: $XDG_CONFIG_HOME/todofiles/config.yaml
             (default: ~/.config/todofiles/config.yaml)
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import yaml


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "todofiles" / "config.yaml"


def load() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save(data: dict) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    # Restrict to owner read/write only — config contains API tokens
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def set_value(key_path: str, value: str) -> None:
    """
    Set a nested key using dot notation (e.g. "jira.api_token").
    Value is always stored as a string.
    """
    data = load()
    keys = key_path.split(".")
    node: Any = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    save(data)


def get_log_level() -> str:
    """Return the configured log level (default: 'warning')."""
    return load().get("log_level", "warning")


_VALID_ASK_MODES = {"always", "never", "delete_only"}


def get_ask_mode() -> str:
    """Return the confirmation mode: 'always', 'never', or 'delete_only' (default)."""
    mode = load().get("ask", "always")
    if mode not in _VALID_ASK_MODES:
        raise ValueError(f"Invalid ask mode {mode!r}. Must be one of: {', '.join(sorted(_VALID_ASK_MODES))}")
    return mode


def get_jira_config() -> dict | None:
    """Return the [jira] section if all required fields are present, else None."""
    data = load()
    jira = data.get("jira", {})
    required = ("base_url", "username", "api_token")
    if all(jira.get(k) for k in required):
        return jira
    return None
