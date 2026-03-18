"""CLI entry point for todofiles."""
from __future__ import annotations

import sys

import click

from . import parser as todo_parser
from . import writer as todo_writer
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
    """Parse FILE and push changes to the local DB (and eventually Jira)."""
    try:
        parsed = todo_parser.parse(file)
    except Exception as e:
        click.echo(f"Parse error: {e}", err=True)
        sys.exit(1)

    changed = assign_ids(parsed)

    session = get_session()
    init_db()  # no-op if tables exist; Alembic handles prod migrations

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
        answer = click.confirm(
            f"You removed ticket '{db_t.title}'{remote} — delete it in Jira?",
            default=False,
        )
        if answer:
            confirmed_deletes.append(db_t)
        else:
            click.echo(f"  Skipping deletion of '{db_t.title}'.")

    plan.to_delete = confirmed_deletes

    execute_plan(plan, parsed, session)

    if changed:
        todo_writer.write(parsed)
        click.echo("\nWrote IDs back to file.")

    click.echo("Done.")


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
@click.argument("assignment")  # key=value
def config_set(assignment: str) -> None:
    """Set a config value (e.g. todofiles config set autopush=true)."""
    if "=" not in assignment:
        click.echo("Expected key=value format.", err=True)
        sys.exit(1)
    key, _, value = assignment.partition("=")
    click.echo(f"Config: {key!r} = {value!r}  (config persistence not yet implemented)")


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
