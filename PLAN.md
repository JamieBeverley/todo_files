
# Motivation
As a developer, I keep track of my tasks in local .md files and any time I need to copy those things to Jira/Clickup/whichever tool we're using its a pain.
As a result, I often just don't so PMs and others lack visibility on what I'm working on and current progress.

# Objective

Create a tool that can read my local .md files and create/update/delete tickets in our ticketing system when I save changes to those files.

# Scope

Lets start just with Jira (but keep in mind future extensibility to other ticketing services).

# Design

## Syntax

Plain-text-first syntax optimized for developer readability. Not intended to be fully markdown-compliant — developers work in the raw file, PMs use the Jira view.

```
---
# File-level defaults applied to all tickets unless overridden
labels: ["my_team", "my_project"]
board: "BackendTeamBoard"
item_type: "task"
# Map local status codes to Jira statuses
status_map:
    x: "Done"
    in_prog: "In Progress"
    review: "In Review"
---

[ ] add json output format to products API

[in_prog] refactor frontend to use v2 API
    id: abc123
    jira: PROJ-42

[x] fix css alignment issue on homepage
    id: def456
    jira: PROJ-38

# Per-ticket overrides via indented fields:
[in_prog] add slack integration
    id: ghi789
    jira: PROJ-45
    item_type: "story"
    description: |
        Add integration with our slack bot using Oauth workflows
        etc...etc...etc..
    labels: ["custom_label2"]
    subtasks:
        [x] create oauth callback endpoint
            id: jkl012
            jira: PROJ-46
        [ ] register new Slack App
            id: mno345


Other notes that I add to this doc (without starting with [...] or being part of a ticket block
are allowed and should just remain in the doc but not be pushed anywhere

Kind of like code comments.

```

**Notes on syntax:**
- Status codes inside `[ ]` are user-defined and mapped to backend statuses via `status_map` in frontmatter.
- `id` is a stable local identifier, auto-written by the tool after first push. If absent, the backend attempts a fuzzy title match; on success or creation the `id` is written back automatically.
- `jira:` (or equivalent per backend) stores the remote ticket key, also auto-written after first push.
- Explicit `id` always takes precedence over fuzzy matching.
- Any line that doesn't start a ticket block (i.e. doesn't begin with `[...] `) and isn't an indented field of a ticket is treated as a free-form comment. The parser preserves these lines and round-trips them unchanged — they are never synced to any backend.

## Architecture

The system is a layered pipeline:

```
.todo file → Parser → Internal AST → SQLite (via Alembic) → Service Mapper → Jira API
```

Each layer is independently testable and the service mapper is the only Jira-specific component.

### Layers

1. **Parser** — reads `.todo` files, produces typed ticket objects (internal AST). Validates syntax and errors early.
2. **Internal AST / data model** — language-agnostic representation of a ticket and its fields. The single source of truth that all other layers speak.
3. **Local storage (SQLite + Alembic)** — persists the AST and tracks sync state (last-pushed snapshot, remote ticket keys). Acts as a buffer so push/pull can be decoupled from parsing.
4. **Service mapper** — translates the internal AST to/from Jira API types. The only layer that needs to change when adding a new backend.
5. **CLI / config** — user-facing interface, described below.

## Ticket Identity & Idempotency

- Each ticket has a stable **local ID** (short UUID or slug) stored as an indented `id:` field.
- If no ID is present, the backend normalizes the title and checks for a fuzzy match in SQLite. If confident, it links to the existing record; if ambiguous, it creates a new ticket.
- After any create or link, the tool **writes the `id:` and remote key back into the `.todo` file** as indented fields so future syncs are unambiguous.
- Explicit IDs always take precedence over fuzzy matching.

## Conflict Resolution

A `pull` command fetches the current state from Jira and updates the local `.todo` file and SQLite to match. This is the primary conflict resolution path — remote changes are not silently overwritten.

Sync flow:
- `push`: local → Jira (local wins)
- `pull`: Jira → local (remote wins)
- Conflicts (both sides changed since last sync) are surfaced as warnings; user chooses push or pull explicitly.

## CLI

```
todofiles push <file>          # parse → SQLite → push to Jira
todofiles pull <file>          # fetch from Jira → update SQLite + file
todofiles diff <file>          # compare local file vs. live Jira state (requires API call)
todofiles push --dry-run <file> # show what would change vs. last SQLite snapshot (no API call)
todofiles config set <key=val>  # set config values
```

**`diff` vs `--dry-run`:**
- `--dry-run` is fast — compares local AST against the last SQLite snapshot, no API call needed.
- `diff` is thorough — fetches live Jira state and shows any drift in either direction.

Both output a structured list of would-be creates, updates, and deletes.

## Daemon / Auto-push

- `todofiles config set autopush=true` enables a file watcher daemon.
- Uses `watchdog` to watch `.todo` files for changes.
- Debounced: a burst of saves triggers only one sync.
- Runs `push` automatically on save.

## Storage

- **SQLite** for local persistence (lightweight, no server, good single-row lookup performance).
- **Alembic** for schema migrations — versioned from day one.

### DB Schema

**`files`** — tracks which `.todo` files are registered/watched
| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `path` | TEXT UNIQUE | absolute path to the `.todo` file |
| `last_parsed_at` | DATETIME | updated on every successful parse |

**`tickets`** — one row per ticket, regardless of which file it lives in
| column | type | notes |
|---|---|---|
| `id` | TEXT PK | stable local ID (short UUID), mirrors the `id:` field in the file |
| `file_id` | INTEGER FK → files | which file this ticket belongs to |
| `title` | TEXT | |
| `status` | TEXT | local status code (e.g. `in_prog`) |
| `fields_json` | TEXT | JSON blob of all other fields (labels, description, item_type, etc.) |
| `remote_key` | TEXT NULLABLE | e.g. `PROJ-42`; null until first push |
| `last_synced_at` | DATETIME NULLABLE | null if never pushed |
| `last_synced_hash` | TEXT NULLABLE | hash of `fields_json` at last sync, used by `--dry-run` to detect local changes without an API call |
| `sync_status` | TEXT | `clean` / `local_dirty` / `remote_dirty` / `conflict` / `pending_deletion` |

**`subtasks`** — subtask relationships (parent/child are both rows in `tickets`)
| column | type | notes |
|---|---|---|
| `parent_id` | TEXT FK → tickets | |
| `child_id` | TEXT FK → tickets | |
| `position` | INTEGER | preserves ordering |

**Notes:**
- Line numbers are intentionally not stored — they go stale immediately on any edit. The parser locates a ticket by scanning for its `id:` field.
- Deletions are detected by diffing the set of IDs returned by the parser against the IDs in the DB for that file. A ticket present in the DB but absent from the parsed file is flagged as `pending_deletion`. The CLI then prompts: `"You removed ticket '<title>' (PROJ-42) — delete it in Jira? [y/N]"` before taking any action.
- `--dry-run` uses `last_synced_hash` to detect changes locally with no API call. `diff` fetches live Jira state and updates `sync_status` accordingly.

## Config

User-level config follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/):

- Config file: `$XDG_CONFIG_HOME/todofiles/config.yaml` (default: `~/.config/todofiles/config.yaml`)
- Format: YAML

```yaml
jira:
  base_url: "https://mycompany.atlassian.net"
  username: "me@example.com"
  api_token: "your_api_token_here"
```

Set via CLI:
```
todofiles config set jira.base_url=https://mycompany.atlassian.net
todofiles config set jira.username=me@example.com
todofiles config set jira.api_token=secret
```

Config is user-level (not per-repo). Sensitive values (api_token) are stored in the config file with user-only permissions (chmod 600).

## Jira Integration

Uses the Jira REST API v3 with HTTP Basic auth (`username:api_token`).

**Field mapping (internal → Jira):**
| Internal field | Jira field |
|---|---|
| `ticket.title` | `summary` |
| `ticket.description` | `description` (Atlassian Document Format) |
| `ticket.labels` | `labels` |
| `ticket.item_type` (or `config.item_type`) | `issuetype.name` |
| `config.board` | `project.key` |
| `config.status_map[ticket.status]` | target status for transition |

**Status updates** require a Jira transition (not a direct field update). The mapper:
1. Fetches available transitions for the issue
2. Matches by name using `config.status_map`
3. POSTs the transition

**Description format:** Jira API v3 uses Atlassian Document Format (ADF). Plain text descriptions are converted to a minimal ADF document (paragraphs split on double newlines).

**Push flow (with Jira configured):**
1. Parse file → assign IDs → write IDs back
2. Build sync plan (DB diff)
3. Show plan, confirm deletions
4. For each CREATE: `POST /rest/api/3/issue` → get key → write `jira: KEY` back to file + DB
5. For each UPDATE: `PUT /rest/api/3/issue/{key}` + transition if status changed
6. For each confirmed DELETE: `DELETE /rest/api/3/issue/{key}`
7. Update `sync_status` → `clean` and `last_synced_at` in DB

# Open Questions

- Should `pull` rewrite the entire file or do a smart merge that preserves ordering and comments? *(decided: full rewrite for now)*
- What's the right behavior when a Jira ticket is deleted remotely — error, warn, or mark as deleted locally?
