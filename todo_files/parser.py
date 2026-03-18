"""
Parser for .todo files.

Produces a ParsedFile containing FileConfig and an ordered list of Ticket
objects and free-form string blocks, preserving everything in the file so the
writer can round-trip it unchanged.
"""
from __future__ import annotations

import re
from typing import Union

import yaml

from .models import FileConfig, ParsedFile, Ticket

# Matches: [status] title text
# Status may be empty, "x", "in_prog", etc.
_TICKET_RE = re.compile(r"^\[([^\]]*)\]\s+(.+)$")

# Matches a simple key: value field line (after stripping indentation)
_FIELD_RE = re.compile(r"^([\w][\w_-]*):\s*(.*)$")


def parse(path: str) -> ParsedFile:
    with open(path, encoding="utf-8") as f:
        content = f.read()
    p = _Parser(content.splitlines())
    config = p.parse_frontmatter()
    items = p.parse_body(base_indent=0)
    return ParsedFile(path=path, config=config, items=items)


class _Parser:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.pos = 0

    # ------------------------------------------------------------------
    # Cursor helpers
    # ------------------------------------------------------------------

    def at_end(self) -> bool:
        return self.pos >= len(self.lines)

    def peek(self) -> str | None:
        return self.lines[self.pos] if not self.at_end() else None

    def consume(self) -> str:
        line = self.lines[self.pos]
        self.pos += 1
        return line

    @staticmethod
    def _indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    # ------------------------------------------------------------------
    # Frontmatter
    # ------------------------------------------------------------------

    def parse_frontmatter(self) -> FileConfig:
        # Skip leading blank lines
        while not self.at_end() and self.peek().strip() == "":
            self.consume()

        if self.at_end() or self.peek().strip() != "---":
            return FileConfig()

        self.consume()  # opening ---
        yaml_lines: list[str] = []
        while not self.at_end() and self.peek().strip() != "---":
            yaml_lines.append(self.consume())
        if not self.at_end():
            self.consume()  # closing ---

        data: dict = yaml.safe_load("\n".join(yaml_lines)) or {}
        known = {"board", "item_type", "labels", "status_map"}
        return FileConfig(
            board=data.get("board"),
            item_type=data.get("item_type", "task"),
            labels=data.get("labels", []),
            status_map=data.get("status_map", {}),
            extra={k: v for k, v in data.items() if k not in known},
        )

    # ------------------------------------------------------------------
    # Body
    # ------------------------------------------------------------------

    def parse_body(self, base_indent: int) -> list[Union[Ticket, str]]:
        """
        Parse a sequence of tickets and free-form text at `base_indent`.
        Stops when a line dedents below `base_indent`.
        """
        items: list[Union[Ticket, str]] = []
        free_form: list[str] = []

        while not self.at_end():
            line = self.peek()
            assert line is not None
            stripped = line.strip()

            if stripped == "":
                free_form.append(self.consume())
                continue

            indent = self._indent(line)

            if indent < base_indent:
                break  # caller's responsibility

            if indent == base_indent and _TICKET_RE.match(stripped):
                if free_form:
                    items.append("\n".join(free_form))
                    free_form = []
                items.append(self._parse_ticket(base_indent))
            else:
                free_form.append(self.consume())

        if free_form:
            items.append("\n".join(free_form))

        return items

    # ------------------------------------------------------------------
    # Ticket
    # ------------------------------------------------------------------

    def _parse_ticket(self, base_indent: int) -> Ticket:
        line = self.consume()
        m = _TICKET_RE.match(line.strip())
        assert m, f"Expected ticket line, got: {line!r}"
        status, title = m.group(1), m.group(2).strip()
        ticket = Ticket(title=title, status=status)

        field_indent = base_indent + 4
        self._parse_fields(ticket, field_indent)
        return ticket

    def _parse_fields(self, ticket: Ticket, field_indent: int) -> None:
        """Consume indented field lines belonging to `ticket`."""
        while not self.at_end():
            line = self.peek()
            assert line is not None

            if line.strip() == "":
                break  # blank line ends the ticket block

            indent = self._indent(line)
            if indent < field_indent:
                break  # dedented — ticket block is done

            if indent > field_indent:
                # Unexpectedly deep — treat as part of previous multiline; skip
                self.consume()
                continue

            m = _FIELD_RE.match(line.strip())
            if not m:
                break  # not a field line — stop

            self.consume()
            key, raw_value = m.group(1), m.group(2).strip()

            if key == "subtasks" and raw_value == "":
                ticket.subtasks = self._parse_subtasks(field_indent + 4)
            elif raw_value == "|":
                value = self._parse_multiline(field_indent + 4)
                self._assign_field(ticket, key, value)
            else:
                value = yaml.safe_load(raw_value) if raw_value else ""
                self._assign_field(ticket, key, value)

    def _parse_subtasks(self, subtask_indent: int) -> list[Ticket]:
        subtasks: list[Ticket] = []
        while not self.at_end():
            line = self.peek()
            assert line is not None
            if line.strip() == "":
                break
            indent = self._indent(line)
            if indent < subtask_indent:
                break
            if indent == subtask_indent and _TICKET_RE.match(line.strip()):
                subtasks.append(self._parse_ticket(subtask_indent))
            else:
                break
        return subtasks

    def _parse_multiline(self, content_indent: int) -> str:
        """Consume a YAML block-scalar body (lines indented >= content_indent)."""
        lines: list[str] = []
        while not self.at_end():
            line = self.peek()
            assert line is not None
            if line.strip() == "":
                lines.append("")
                self.consume()
                continue
            if self._indent(line) < content_indent:
                break
            lines.append(line[content_indent:])  # strip exactly content_indent spaces
            self.consume()
        # Strip trailing blank lines (YAML clip chomping)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Field assignment
    # ------------------------------------------------------------------

    @staticmethod
    def _assign_field(ticket: Ticket, key: str, value) -> None:
        match key:
            case "id":
                ticket.id = str(value)
            case "jira":
                ticket.remote_key = str(value)
            case "item_type":
                ticket.item_type = str(value)
            case "description":
                ticket.description = str(value)
            case "labels":
                ticket.labels = list(value) if isinstance(value, list) else [str(value)]
            case _:
                ticket.extra_fields[key] = value
