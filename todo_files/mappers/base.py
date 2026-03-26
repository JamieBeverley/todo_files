"""Abstract base class for ticketing-service mappers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import FileConfig, Ticket


class BaseMapper(ABC):
    """Translates between the internal AST and a remote ticketing service."""

    @abstractmethod
    def create(self, ticket: Ticket, config: FileConfig) -> str:
        """Create a ticket in the remote service. Returns the remote key (e.g. 'PROJ-42')."""

    @abstractmethod
    def update(self, ticket: Ticket, config: FileConfig) -> None:
        """Update an existing ticket in the remote service."""

    @abstractmethod
    def delete(self, remote_key: str) -> None:
        """Delete a ticket in the remote service by its remote key."""

    @abstractmethod
    def fetch(self, remote_key: str) -> Ticket:
        """Fetch a single ticket from the remote service and return it as a Ticket."""
