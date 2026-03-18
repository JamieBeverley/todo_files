"""Jira service mapper (stub — not yet implemented)."""
from __future__ import annotations

from ..models import FileConfig, Ticket
from .base import BaseMapper


class JiraMapper(BaseMapper):
    def __init__(self, base_url: str, email: str, api_token: str):
        self.base_url = base_url
        self.email = email
        self.api_token = api_token

    def create(self, ticket: Ticket, config: FileConfig) -> str:
        raise NotImplementedError("Jira integration not yet implemented")

    def update(self, ticket: Ticket, config: FileConfig) -> None:
        raise NotImplementedError("Jira integration not yet implemented")

    def delete(self, remote_key: str) -> None:
        raise NotImplementedError("Jira integration not yet implemented")

    def fetch(self, remote_key: str) -> Ticket:
        raise NotImplementedError("Jira integration not yet implemented")
