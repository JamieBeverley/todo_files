from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Ticket:
    title: str
    status: str  # local code: "", "x", "in_prog", etc.
    id: str | None = None
    remote_key: str | None = None  # e.g. "PROJ-42"
    item_type: str | None = None
    description: str | None = None
    labels: list[str] = field(default_factory=list)
    subtasks: list[Ticket] = field(default_factory=list)
    extra_fields: dict = field(default_factory=dict)


@dataclass
class FileConfig:
    board: str | None = None
    item_type: str = "task"
    labels: list[str] = field(default_factory=list)
    status_map: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass
class ParsedFile:
    path: str
    config: FileConfig
    # Ordered mix of Ticket objects and str (free-form comment blocks).
    # Preserving this order allows the writer to round-trip the file faithfully.
    items: list[Ticket | str]
