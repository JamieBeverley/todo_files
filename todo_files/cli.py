"""CLI entry point for todofiles."""
from __future__ import annotations

import sys

import click

from . import config as todo_config
from . import parser as todo_parser
from . import writer as todo_writer
from .mappers.jira import JiraMapper
from .storage.database import get_session, init_db
from .sync import assign_ids, build_plan, execute_plan


@click.group()
def cli() -> None:
    """Sync local .todo files to Jira and other ticketing backends."""


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
# pull  (stub)
# ------------------------------------------------------------------

@cli.command()
@click.argument("file", type=click.Path(exists=True))
def pull(file: str) -> None:
    """Fetch the latest state from Jira and update FILE. (Not yet implemented.)"""
    click.echo("pull is not yet implemented.", err=True)
    sys.exit(1)


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
