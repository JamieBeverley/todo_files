"""CLI entry point for todofiles."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import click

from . import config as todo_config
from . import log as todo_log
from . import parser as todo_parser
from . import writer as todo_writer
from .mappers.jira import JiraMapper
from .models import Ticket
from .storage.database import get_session, init_db
from .storage.models import File as DBFile
from .sync import assign_ids, build_plan, execute_plan


@click.group()
def cli() -> None:
    """Sync local .todo files to Jira and other ticketing backends."""
    todo_log.setup(todo_config.get_log_level())


# ------------------------------------------------------------------
# push
# ------------------------------------------------------------------

@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Show what would change without doing anything.")
def push(file: str, dry_run: bool) -> None:
    """Parse FILE and push changes to the local DB and Jira (if configured)."""
    try:
        parsed = todo_parser.parse(file)
    except Exception as e:
        click.echo(f"Parse error: {e}", err=True)
        sys.exit(1)

    changed = assign_ids(parsed)

    init_db()
    session = get_session()
    plan = build_plan(parsed, session)

    if not plan.has_changes and not changed:
        click.echo("Nothing to do — everything is up to date.")
        return

    _print_plan(plan)

    if dry_run:
        if changed:
            click.echo("\n(--dry-run: new IDs were assigned in memory but not written to file)")
        return

    # Confirm deletions before executing
    confirmed_deletes = []
    for db_t in plan.to_delete:
        remote = f" ({db_t.remote_key})" if db_t.remote_key else ""
        if click.confirm(f"\nYou removed ticket '{db_t.title}'{remote} — delete it in Jira?", default=False):
            confirmed_deletes.append(db_t)
        else:
            click.echo(f"  Skipping deletion of '{db_t.title}'.")
    plan.to_delete = confirmed_deletes

    # Jira sync (if configured)
    jira_cfg = todo_config.get_jira_config()
    if jira_cfg:
        mapper = JiraMapper(
            base_url=jira_cfg["base_url"],
            username=jira_cfg["username"],
            api_token=jira_cfg["api_token"],
        )
        _push_to_jira(plan, parsed, mapper)
    else:
        click.echo("\n(Jira not configured — syncing to local DB only. Run `todofiles config set jira.*` to enable.)")

    execute_plan(plan, parsed, session)

    if changed:
        todo_writer.write(parsed)
        click.echo("\nWrote IDs back to file.")

    click.echo("Done.")


def _push_to_jira(plan, parsed, mapper: JiraMapper) -> None:
    """Call the Jira API for each planned change. Updates ticket.remote_key in place."""
    for ticket in plan.to_create:
        try:
            key = mapper.create(ticket, parsed.config)
            ticket.remote_key = key
            click.echo(f"  Created {key}: {ticket.title}")
        except Exception as e:
            click.echo(f"  ERROR creating '{ticket.title}': {e}", err=True)

    for ticket in plan.to_update:
        try:
            mapper.update(ticket, parsed.config)
            click.echo(f"  Updated {ticket.remote_key}: {ticket.title}")
        except Exception as e:
            click.echo(f"  ERROR updating '{ticket.title}': {e}", err=True)

    for db_t in plan.to_delete:
        if not db_t.remote_key:
            continue
        try:
            mapper.delete(db_t.remote_key)
            click.echo(f"  Deleted {db_t.remote_key}: {db_t.title}")
        except Exception as e:
            click.echo(f"  ERROR deleting '{db_t.remote_key}': {e}", err=True)


# ------------------------------------------------------------------
# pull
# ------------------------------------------------------------------

@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Show what would change without doing anything.")
def pull(file: str, dry_run: bool) -> None:
    """Fetch the latest state from Jira and update FILE."""
    try:
        parsed = todo_parser.parse(file)
    except Exception as e:
        click.echo(f"Parse error: {e}", err=True)
        sys.exit(1)

    assign_ids(parsed)

    jira_cfg = todo_config.get_jira_config()
    if not jira_cfg:
        click.echo(
            "Jira not configured — run `todofiles config set jira.*` to enable.", err=True
        )
        sys.exit(1)

    mapper = JiraMapper(
        base_url=jira_cfg["base_url"],
        username=jira_cfg["username"],
        api_token=jira_cfg["api_token"],
    )

    # Invert status_map so we can translate Jira status names back to local codes.
    # e.g. {"in_prog": "In Progress"} → {"in progress": "in_prog"}
    inverse_status_map = {v.lower(): k for k, v in parsed.config.status_map.items()}

    changes = _pull_from_jira(parsed, mapper, inverse_status_map)

    if not changes:
        click.echo("Nothing to update — all linked tickets are up to date.")
        return

    _print_pull_changes(changes)

    if dry_run:
        click.echo("\n(--dry-run: no changes written)")
        return

    todo_writer.write(parsed)

    init_db()
    session = get_session()
    plan = build_plan(parsed, session)
    execute_plan(plan, parsed, session)

    # execute_plan marks everything local_dirty; tickets we just fetched are
    # actually clean (in sync with Jira), so fix their status.
    pulled_keys = {remote_key for remote_key, _, _ in changes}
    _mark_pulled_clean(parsed.path, pulled_keys, session)

    click.echo("\nDone.")


def _pull_from_jira(parsed, mapper: JiraMapper, inverse_status_map: dict) -> list:
    """
    Fetch each linked ticket from Jira and update parsed in place (remote wins).
    Returns list of (remote_key, new_title, change_descriptions) for changed tickets.
    """
    changes = []

    def _walk(tickets: list[Ticket]) -> None:
        for ticket in tickets:
            if ticket.remote_key:
                try:
                    remote = mapper.fetch(ticket.remote_key)
                except Exception as e:
                    click.echo(f"  ERROR fetching {ticket.remote_key}: {e}", err=True)
                    _walk(ticket.subtasks)
                    continue

                jira_status = remote.extra_fields.get("_jira_status", "")
                new_status = inverse_status_map.get(jira_status.lower(), ticket.status)

                diffs = []
                if remote.title != ticket.title:
                    diffs.append(f"title: {ticket.title!r} → {remote.title!r}")
                if remote.description != ticket.description:
                    diffs.append("description updated")
                if sorted(remote.labels) != sorted(ticket.labels):
                    diffs.append(f"labels: {ticket.labels} → {remote.labels}")
                if new_status != ticket.status:
                    diffs.append(f"status: [{ticket.status}] → [{new_status}]")

                if diffs:
                    changes.append((ticket.remote_key, remote.title, diffs))
                    ticket.title = remote.title
                    ticket.description = remote.description
                    ticket.labels = remote.labels
                    ticket.status = new_status

            _walk(ticket.subtasks)

    for item in parsed.items:
        if isinstance(item, Ticket):
            _walk([item])

    return changes


def _mark_pulled_clean(file_path: str, pulled_keys: set[str], session) -> None:
    """Set sync_status=clean and last_synced_at for tickets we just pulled."""
    now = datetime.now(timezone.utc)
    db_file = session.query(DBFile).filter_by(path=file_path).first()
    if not db_file:
        return
    for db_t in db_file.tickets:
        if db_t.remote_key in pulled_keys:
            db_t.sync_status = "clean"
            db_t.last_synced_at = now
    session.commit()


def _print_pull_changes(changes: list) -> None:
    for remote_key, title, diffs in changes:
        click.echo(f"\n  {click.style(remote_key, fg='cyan')}: {title}")
        for d in diffs:
            click.echo(f"    ~ {d}")


# ------------------------------------------------------------------
# diff  (stub)
# ------------------------------------------------------------------

@cli.command()
@click.argument("file", type=click.Path(exists=True))
def diff(file: str) -> None:
    """Compare FILE against live Jira state. (Not yet implemented.)"""
    click.echo("diff is not yet implemented.", err=True)
    sys.exit(1)


# ------------------------------------------------------------------
# config
# ------------------------------------------------------------------

@cli.group()
def config() -> None:
    """Manage todofiles configuration."""


@config.command("set")
@click.argument("assignment")  # e.g. jira.api_token=secret
def config_set(assignment: str) -> None:
    """Set a config value, e.g.: todofiles config set jira.base_url=https://..."""
    if "=" not in assignment:
        click.echo("Expected key=value format.", err=True)
        sys.exit(1)
    key, _, value = assignment.partition("=")
    todo_config.set_value(key, value)
    click.echo(f"Set {key!r}.")


@config.command("show")
def config_show() -> None:
    """Show current configuration (api_token is redacted)."""
    data = todo_config.load()
    if not data:
        click.echo("No config found.")
        return
    # Redact sensitive values
    if "jira" in data and "api_token" in data["jira"]:
        data["jira"]["api_token"] = "***"
    import yaml
    click.echo(yaml.dump(data, default_flow_style=False).strip())


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------

def _print_plan(plan) -> None:
    if plan.to_create:
        click.echo(f"\n  {click.style('CREATE', fg='green')} ({len(plan.to_create)})")
        for t in plan.to_create:
            click.echo(f"    + [{t.status}] {t.title}")

    if plan.to_update:
        click.echo(f"\n  {click.style('UPDATE', fg='yellow')} ({len(plan.to_update)})")
        for t in plan.to_update:
            remote = f"  {t.remote_key}" if t.remote_key else ""
            click.echo(f"    ~ [{t.status}] {t.title}{remote}")

    if plan.to_delete:
        click.echo(f"\n  {click.style('DELETE', fg='red')} ({len(plan.to_delete)})")
        for t in plan.to_delete:
            remote = f"  ({t.remote_key})" if t.remote_key else ""
            click.echo(f"    - {t.title}{remote}")

    if plan.clean:
        click.echo(f"\n  {click.style('CLEAN', fg='bright_black')} ({len(plan.clean)}) — no changes")
