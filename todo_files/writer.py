"""
Writer for .todo files.

Reconstructs a .todo file from a ParsedFile, preserving free-form comment
blocks exactly and serialising Ticket objects back to the plain-text syntax.
"""
from __future__ import annotations

import json

from .models import FileConfig, ParsedFile, Ticket


def write(parsed: ParsedFile) -> None:
    """Overwrite parsed.path with the current state of the ParsedFile."""
    text = serialise(parsed)
    with open(parsed.path, "w", encoding="utf-8") as f:
        f.write(text)


def serialise(parsed: ParsedFile) -> str:
    parts: list[str] = []

    if _config_has_content(parsed.config):
        parts.append(_serialise_config(parsed.config))

    for item in parsed.items:
        if isinstance(item, str):
            parts.append(item)
        else:
            parts.append(_serialise_ticket(item, indent=0))

    return "\n".join(parts) + "\n"


# ------------------------------------------------------------------
# Config / frontmatter
# ------------------------------------------------------------------

def _config_has_content(cfg: FileConfig) -> bool:
    return bool(cfg.board or cfg.labels or cfg.status_map or cfg.item_type != "task" or cfg.extra)


def _serialise_config(cfg: FileConfig) -> str:
    lines = ["---"]
    if cfg.board:
        lines.append(f"board: {_yaml_str(cfg.board)}")
    if cfg.item_type and cfg.item_type != "task":
        lines.append(f"item_type: {_yaml_str(cfg.item_type)}")
    if cfg.labels:
        lines.append(f"labels: {_yaml_list(cfg.labels)}")
    if cfg.status_map:
        lines.append("status_map:")
        for code, jira_status in cfg.status_map.items():
            lines.append(f"    {code}: {_yaml_str(jira_status)}")
    for k, v in cfg.extra.items():
        lines.append(f"{k}: {json.dumps(v)}")
    lines.append("---")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Tickets
# ------------------------------------------------------------------

def _serialise_ticket(ticket: Ticket, indent: int) -> str:
    pad = " " * indent
    lines = [f"{pad}[{ticket.status}] {ticket.title}"]

    field_pad = " " * (indent + 4)

    # Write managed fields in a consistent order, then extras
    if ticket.id:
        lines.append(f"{field_pad}id: {ticket.id}")
    if ticket.remote_key:
        lines.append(f"{field_pad}jira: {ticket.remote_key}")
    if ticket.item_type:
        lines.append(f"{field_pad}item_type: {_yaml_str(ticket.item_type)}")
    if ticket.labels:
        lines.append(f"{field_pad}labels: {_yaml_list(ticket.labels)}")
    if ticket.description:
        lines.append(f"{field_pad}description: |")
        for desc_line in ticket.description.splitlines():
            lines.append(f"{field_pad}    {desc_line}")
    for k, v in ticket.extra_fields.items():
        lines.append(f"{field_pad}{k}: {json.dumps(v)}")
    if ticket.subtasks:
        lines.append(f"{field_pad}subtasks:")
        for sub in ticket.subtasks:
            lines.append(_serialise_ticket(sub, indent=indent + 8))

    return "\n".join(lines)


# ------------------------------------------------------------------
# YAML-ish value helpers
# ------------------------------------------------------------------

def _yaml_str(value: str) -> str:
    """Quote only if the value contains special characters."""
    if any(c in value for c in ('"', "'", ":", "#", "{", "}", "[", "]", ",")):
        return json.dumps(value)
    return value


def _yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"
