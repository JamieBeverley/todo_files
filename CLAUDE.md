# todofiles

A CLI tool that syncs local `.todo` files to Jira (and eventually other ticketing backends).

See `PLAN.md` for full design doc: syntax spec, architecture, DB schema, and CLI design.

## Architecture

Layered pipeline — each layer is independently testable:

```
.todo file → Parser → Internal AST → SQLite (via Alembic) → Service Mapper → Jira API
```

- **Parser**: reads `.todo` files, knows nothing about storage or backends
- **Internal AST**: shared data model, the only thing all layers import
- **Storage**: SQLite + Alembic; `sync_status` and `last_synced_hash` live here
- **Service mapper**: the only Jira-specific code; new backends add a new mapper
- **CLI**: user-facing commands (`push`, `pull`, `diff`, `config`)

**Layer boundary rule**: lower layers must never import from higher layers. The parser must not import from the service mapper; the AST must not import from storage; etc.

## Stack

- Python
- SQLite for local storage
- Alembic for migrations (versioned from day one)
- `watchdog` for file watching (daemon/autopush mode)

## Conventions

- New backends: add a service mapper only — parser, AST, and storage are backend-agnostic
- Migrations: always add an Alembic migration for schema changes, never modify the DB directly
- The parser must round-trip free-form comment lines unchanged — never strip or reorder them
