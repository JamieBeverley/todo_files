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
from .sync import assign_ids, build_plan, execute_plan, flatten, mark_synced


@click.group()
def cli() -> None:
    """Sync local .todo files to Jira and other ticketing backends."""
    todo_log.setup(todo_config.get_log_level())


# ------------------------------------------------------------------
# push
# ------------------------------------------------------------------


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "--dry-run", is_flag=True, help="Show what would change without doing anything."
)
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
            click.echo(
                "\n(--dry-run: new IDs were assigned in memory but not written to file)"
            )
        return

    # Per-ticket 3-way prompt for every detected deletion.
    for db_t in list(plan.to_delete):
        remote = f" ({db_t.remote_key})" if db_t.remote_key else ""
        click.echo(f"\n  Ticket removed from file: '{db_t.title}'{remote}")
        choice = click.prompt(
            "  What should happen?",
            type=click.Choice(["delete", "untrack", "abort"]),
            default="untrack",
        )
        if choice == "abort":
            click.echo("Aborted.")
            return
        elif choice == "untrack":
            plan.to_delete.remove(db_t)
            plan.to_untrack.append(db_t)
        # "delete" → leave in plan.to_delete as-is

    # Overall confirmation for creates/updates (respects ask mode).
    ask = todo_config.get_ask_mode()
    has_remote_changes = bool(plan.to_create or plan.to_update)
    if ask == "always" and has_remote_changes:
        if not click.confirm("\nProceed?", default=False):
            click.echo("Aborted.")
            return

    # Jira sync (if configured)
    jira_cfg = todo_config.get_jira_config()
    if jira_cfg:
        mapper = JiraMapper(
            base_url=jira_cfg["base_url"],
            username=jira_cfg["username"],
            api_token=jira_cfg["api_token"],
        )
        synced_ids = _push_to_jira(plan, parsed, mapper)
    else:
        click.echo(
            "\n(Jira not configured — syncing to local DB only. Run `todofiles config set jira.*` to enable.)"
        )
        # No remote — local DB is the only target, so everything is clean after write.
        synced_ids = {t.id for t in plan.to_create if t.id} | {t.id for t in plan.to_update if t.id}

    execute_plan(plan, parsed, session)
    mark_synced(synced_ids, session)

    keys_written_back = any(t.remote_key for t in plan.to_create)
    if changed or keys_written_back:
        todo_writer.write(parsed)
        click.echo("\nWrote IDs back to file.")

    click.echo("Done.")


def _push_to_jira(plan, parsed, mapper: JiraMapper) -> set[str]:
    """Call the Jira API for each planned change. Updates ticket.remote_key in place.
    Returns the set of local ticket IDs that were successfully synced."""
    synced: set[str] = set()

    for ticket in plan.to_create:
        try:
            key = mapper.create(ticket, parsed.config)
            ticket.remote_key = key
            # Materialize config-level labels onto the ticket so they get written back
            # to the file and included in the stored hash.
            if not ticket.labels and parsed.config.labels:
                ticket.labels = list(parsed.config.labels)
            synced.add(ticket.id)
            click.echo(f"  Created {key}: {ticket.title}")
        except Exception as e:
            click.echo(f"  ERROR creating '{ticket.title}': {e}", err=True)

    for ticket in plan.to_update:
        try:
            mapper.update(ticket, parsed.config)
            synced.add(ticket.id)
            click.echo(f"  Updated {ticket.remote_key}: {ticket.title}")
        except Exception as e:
            click.echo(f"  ERROR updating '{ticket.title}': {e}", err=True)

    for db_t in plan.to_delete:
        if not db_t.remote_key:
            continue
        try:
            mapper.delete(db_t.remote_key)
            synced.add(db_t.id)
            click.echo(f"  Deleted {db_t.remote_key}: {db_t.title}")
        except Exception as e:
            click.echo(f"  ERROR deleting '{db_t.remote_key}': {e}", err=True)

    return synced


# ------------------------------------------------------------------
# pull
# ------------------------------------------------------------------


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option(
    "--dry-run", is_flag=True, help="Show what would change without doing anything."
)
def pull(file: str, dry_run: bool) -> None:
    """Fetch the latest state from Jira and update FILE."""
    try:
        parsed = todo_parser.parse(file)
    except Exception as e:
        click.echo(f"Parse error: {e}", err=True)
        sys.exit(1)

    assign_ids(parsed)

    # Restore remote_key from DB for tickets whose write-back previously failed,
    # so _pull_from_jira can fetch and update them too.
    init_db()
    session = get_session()
    _restore_remote_keys(parsed, session)

    jira_cfg = todo_config.get_jira_config()
    if not jira_cfg:
        click.echo(
            "Jira not configured — run `todofiles config set jira.*` to enable.",
            err=True,
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

    plan = build_plan(parsed, session)
    execute_plan(plan, parsed, session)

    # execute_plan marks everything local_dirty; tickets we just fetched are
    # actually clean (in sync with Jira), so fix their status.
    pulled_keys = {remote_key for remote_key, _, _ in changes}
    _mark_pulled_clean(parsed.path, pulled_keys, session)

    click.echo("\nDone.")


# ------------------------------------------------------------------
# import
# ------------------------------------------------------------------


@cli.command("import")
@click.argument("file", type=click.Path(exists=True))
@click.argument("ticket_key")
def import_ticket(file: str, ticket_key: str) -> None:
    """Fetch TICKET_KEY from Jira and append it to FILE."""
    jira_cfg = todo_config.get_jira_config()
    if not jira_cfg:
        click.echo("Jira not configured — run `todofiles config set jira.*` to enable.", err=True)
        sys.exit(1)

    try:
        parsed = todo_parser.parse(file)
    except Exception as e:
        click.echo(f"Parse error: {e}", err=True)
        sys.exit(1)

    # Guard against importing a ticket that's already tracked.
    existing_keys = {t.remote_key for t, _ in flatten(parsed) if t.remote_key}
    if ticket_key in existing_keys:
        click.echo(f"{ticket_key} is already tracked in this file.", err=True)
        sys.exit(1)

    mapper = JiraMapper(
        base_url=jira_cfg["base_url"],
        username=jira_cfg["username"],
        api_token=jira_cfg["api_token"],
    )

    try:
        ticket = mapper.fetch(ticket_key)
    except Exception as e:
        click.echo(f"Failed to fetch {ticket_key}: {e}", err=True)
        sys.exit(1)

    # Map Jira status back to a local code using the file's status_map.
    inverse_status_map = {v.lower(): k for k, v in parsed.config.status_map.items()}
    jira_status = ticket.extra_fields.pop("_jira_status", "")
    ticket.status = inverse_status_map.get(jira_status.lower(), "")

    parsed.items.append(ticket)
    assign_ids(parsed)

    click.echo(f"  Importing {ticket_key}: {ticket.title}  [{ticket.status}]")

    todo_writer.write(parsed)

    init_db()
    session = get_session()
    plan = build_plan(parsed, session)
    execute_plan(plan, parsed, session)
    mark_synced({ticket.id} if ticket.id else set(), session)

    click.echo("Done.")


def _restore_remote_keys(parsed, session) -> None:
    """Copy remote_key from DB to any in-memory ticket that lacks one."""
    db_file = session.query(DBFile).filter_by(path=parsed.path).first()
    if not db_file:
        return
    db_by_id = {t.id: t for t in db_file.tickets}
    for ticket, _ in flatten(parsed):
        if ticket.id and not ticket.remote_key:
            db_t = db_by_id.get(ticket.id)
            if db_t and db_t.remote_key:
                ticket.remote_key = db_t.remote_key


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


@config.command("whoami")
def config_whoami() -> None:
    """Show your Jira account ID (useful for setting assignee in .todo headers)."""
    jira_cfg = todo_config.get_jira_config()
    if not jira_cfg:
        click.echo("Jira not configured — run `todofiles config set jira.*` to enable.", err=True)
        sys.exit(1)

    import requests
    from requests.auth import HTTPBasicAuth

    url = jira_cfg["base_url"].rstrip("/") + "/rest/api/3/myself"
    resp = requests.get(
        url,
        auth=HTTPBasicAuth(jira_cfg["username"], jira_cfg["api_token"]),
        headers={"Accept": "application/json"},
    )
    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        click.echo(f"Jira request failed: {e}", err=True)
        sys.exit(1)

    data = resp.json()
    click.echo(f"displayName: {data.get('displayName')}")
    click.echo(f"emailAddress: {data.get('emailAddress')}")
    click.echo(f"accountId:    {data.get('accountId')}")


@config.command("status-map")
@click.option("--board", default=None, help="Project key to scope statuses (e.g. MYPROJ). Defaults to global.")
def config_status_map(board: str | None) -> None:
    """Print a status_map template based on your Jira project's statuses."""
    jira_cfg = todo_config.get_jira_config()
    if not jira_cfg:
        click.echo("Jira not configured — run `todofiles config set jira.*` to enable.", err=True)
        sys.exit(1)

    import requests
    from requests.auth import HTTPBasicAuth

    base = jira_cfg["base_url"].rstrip("/")
    auth = HTTPBasicAuth(jira_cfg["username"], jira_cfg["api_token"])
    headers = {"Accept": "application/json"}

    project_key = board or jira_cfg.get("board")
    if project_key:
        url = f"{base}/rest/api/3/project/{project_key}/statuses"
        resp = requests.get(url, auth=auth, headers=headers)
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            click.echo(f"Jira request failed: {e}", err=True)
            sys.exit(1)
        # Response is a list of issue types, each with a list of statuses.
        # Deduplicate by name, preserving first-seen order.
        seen: set[str] = set()
        status_names: list[str] = []
        for issue_type in resp.json():
            for s in issue_type.get("statuses", []):
                name = s["name"]
                if name not in seen:
                    seen.add(name)
                    status_names.append(name)
    else:
        url = f"{base}/rest/api/3/status"
        resp = requests.get(url, auth=auth, headers=headers)
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            click.echo(f"Jira request failed: {e}", err=True)
            sys.exit(1)
        status_names = [s["name"] for s in resp.json()]

    # Suggest local codes for well-known Jira status names.
    _SUGGESTIONS: dict[str, str] = {
        "to do": '""',
        "open": '""',
        "backlog": '""',
        "in progress": "in_prog",
        "in review": "review",
        "done": "x",
        "closed": "x",
        "resolved": "x",
        "won't do": "skip",
        "cancelled": "skip",
    }

    click.echo("status_map:")
    for name in status_names:
        suggested = _SUGGESTIONS.get(name.lower(), f'"{name.lower().replace(" ", "_")}"')
        click.echo(f"    {suggested}: {name}")


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

    if plan.to_delete or plan.to_untrack:
        total = len(plan.to_delete) + len(plan.to_untrack)
        click.echo(f"\n  {click.style('DELETE', fg='red')} ({total})")
        for t in plan.to_delete:
            remote = f"  ({t.remote_key})" if t.remote_key else ""
            click.echo(f"    - {t.title}{remote}")
        for t in plan.to_untrack:
            click.echo(f"    - {t.title}  (untrack only)")

    if plan.clean:
        click.echo(
            f"\n  {click.style('CLEAN', fg='bright_black')} ({len(plan.clean)}) — no changes"
        )
