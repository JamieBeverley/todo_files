"""
Sync engine: compares a ParsedFile against the local SQLite DB and produces a
SyncPlan describing what would be created, updated, or deleted.

Executing the plan (writing to DB or Jira) is the CLI's responsibility.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .models import ParsedFile, Ticket
from .storage.models import DBTicket, File, SubtaskLink


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def assign_ids(parsed: ParsedFile) -> bool:
    """
    Assign short UUIDs to any ticket that lacks an `id`. Mutates in place.
    Returns True if any IDs were assigned (caller should write the file back).
    """
    changed = False

    def _walk(tickets: list[Ticket]) -> None:
        nonlocal changed
        for t in tickets:
            if t.id is None:
                t.id = uuid.uuid4().hex[:8]
                changed = True
            _walk(t.subtasks)

    for item in parsed.items:
        if isinstance(item, Ticket):
            _walk([item])

    return changed


def ticket_hash(ticket: Ticket) -> str:
    """Stable hash of a ticket's content fields (excludes id/remote_key)."""
    payload = {
        "title": ticket.title,
        "status": ticket.status,
        "item_type": ticket.item_type,
        "description": ticket.description,
        "labels": sorted(ticket.labels),
        "extra_fields": ticket.extra_fields,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()[:16]


def flatten(parsed: ParsedFile) -> list[tuple[Ticket, str | None]]:
    """
    Return (ticket, parent_id) for every ticket in the file tree.
    parent_id is None for top-level tickets.
    """
    result: list[tuple[Ticket, str | None]] = []

    def _walk(tickets: list[Ticket], parent_id: str | None) -> None:
        for t in tickets:
            result.append((t, parent_id))
            _walk(t.subtasks, t.id)

    for item in parsed.items:
        if isinstance(item, Ticket):
            _walk([item], None)

    return result


# ------------------------------------------------------------------
# Sync plan
# ------------------------------------------------------------------

@dataclass
class SyncPlan:
    to_create: list[Ticket] = field(default_factory=list)
    to_update: list[Ticket] = field(default_factory=list)
    to_delete: list[DBTicket] = field(default_factory=list)   # pending confirmation
    clean: list[Ticket] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.to_create or self.to_update or self.to_delete)


def build_plan(parsed: ParsedFile, session) -> SyncPlan:
    """
    Diff the ParsedFile against the DB for this file path and return a SyncPlan.
    Does NOT modify the DB.
    """
    plan = SyncPlan()

    db_file = session.query(File).filter_by(path=parsed.path).first()
    db_tickets: dict[str, DBTicket] = {}
    if db_file:
        db_tickets = {t.id: t for t in db_file.tickets}

    local_ids: set[str] = set()
    for ticket, _ in flatten(parsed):
        assert ticket.id, "All tickets must have IDs before building a plan"
        local_ids.add(ticket.id)

        if ticket.id not in db_tickets:
            plan.to_create.append(ticket)
        else:
            db_t = db_tickets[ticket.id]
            # Restore remote_key from DB in case a previous write-back to the file failed.
            ticket.remote_key = ticket.remote_key or db_t.remote_key
            if db_t.last_synced_hash != ticket_hash(ticket):
                plan.to_update.append(ticket)
            elif db_t.sync_status == "local_dirty":
                # Hash matches but a previous push never made it to the remote.
                # Re-attempt: create if we never got a remote key, update if we did.
                if db_t.remote_key:
                    plan.to_update.append(ticket)
                else:
                    plan.to_create.append(ticket)
            else:
                plan.clean.append(ticket)

    # Tickets in DB but absent from the file → pending deletion
    for tid, db_t in db_tickets.items():
        if tid not in local_ids:
            plan.to_delete.append(db_t)

    return plan


# ------------------------------------------------------------------
# Plan execution (DB only — Jira push is separate)
# ------------------------------------------------------------------

def execute_plan(plan: SyncPlan, parsed: ParsedFile, session) -> None:
    """
    Apply the plan to the SQLite DB. Deletions must already be confirmed by
    the caller — every ticket in plan.to_delete will be removed.
    """
    now = datetime.now(timezone.utc)

    # Ensure the File row exists
    db_file = session.query(File).filter_by(path=parsed.path).first()
    if not db_file:
        db_file = File(path=parsed.path)
        session.add(db_file)
        session.flush()

    db_file.last_parsed_at = now

    flat = {t.id: (t, parent_id) for t, parent_id in flatten(parsed)}

    for ticket in plan.to_create:
        existing = session.get(DBTicket, ticket.id)
        if existing:
            # Recovery: row was written on a previous failed push; just update it.
            existing.title = ticket.title
            existing.status = ticket.status
            existing.fields_json = _fields_json(ticket)
            existing.remote_key = ticket.remote_key
            existing.last_synced_hash = ticket_hash(ticket)
            existing.sync_status = "local_dirty"
        else:
            db_t = DBTicket(
                id=ticket.id,
                file_id=db_file.id,
                title=ticket.title,
                status=ticket.status,
                fields_json=_fields_json(ticket),
                remote_key=ticket.remote_key,
                last_synced_hash=ticket_hash(ticket),
                sync_status="local_dirty",
            )
            session.add(db_t)

    for ticket in plan.to_update:
        db_t = session.get(DBTicket, ticket.id)
        db_t.title = ticket.title
        db_t.status = ticket.status
        db_t.fields_json = _fields_json(ticket)
        db_t.remote_key = ticket.remote_key
        db_t.last_synced_hash = ticket_hash(ticket)
        db_t.sync_status = "local_dirty"

    for db_t in plan.to_delete:
        session.delete(db_t)

    # Rebuild subtask links for the whole file
    all_ids = list(flat.keys())
    existing_links = (
        session.query(SubtaskLink)
        .filter(SubtaskLink.child_id.in_(all_ids))
        .all()
    )
    for link in existing_links:
        session.delete(link)
    session.flush()

    for pos, (ticket_id, (ticket, parent_id)) in enumerate(flat.items()):
        if parent_id:
            session.add(SubtaskLink(parent_id=parent_id, child_id=ticket_id, position=pos))

    session.commit()


def _fields_json(ticket: Ticket) -> str:
    return json.dumps({
        "item_type": ticket.item_type,
        "description": ticket.description,
        "labels": ticket.labels,
        "extra_fields": ticket.extra_fields,
    })
