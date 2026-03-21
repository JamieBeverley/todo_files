# todofiles

A CLI tool that syncs local `.todo` files to Jira (and eventually other ticketing backends). Write your tasks in a plain-text file, run `todofiles push`, and your tickets are created or updated in Jira automatically.

## Why

Copying tasks from a local notes file into Jira is tedious leaving PMs and other teammates without visibility. `todofiles` allows you to write todos in plaintext and sync to/from Jira.

## File format

`.todo` files use a plain-text syntax designed to be readable and writable without any tooling.

```markdown
---
# File-level defaults applied to all tickets
labels: ["my_team", "my_project"]
board: "BackendTeamBoard"
item_type: "task"
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

[in_prog] add slack integration
    id: ghi789
    jira: PROJ-45
    item_type: "story"
    description: |
        Add integration with our Slack bot using OAuth workflows.
    labels: ["custom_label"]
    subtasks:
        [x] create oauth callback endpoint
            id: jkl012
            jira: PROJ-46
        [ ] register new Slack App
            id: mno345

Lines like this that don't start with [...] are free-form comments.
They are preserved in the file but never synced to any backend.
```

**Key rules:**
- Status codes inside `[ ]` are user-defined and mapped to Jira statuses via `status_map` in the frontmatter.
- `id` is a stable local identifier, auto-assigned by the tool on first push and written back into the file so future syncs are unambiguous.
- `jira:` stores the remote ticket key (e.g. `PROJ-42`), also written back automatically after creation.
- Any line that does not start a ticket block and is not an indented field is treated as a free-form comment — preserved verbatim, never synced.

## Architecture

The system is a layered pipeline from todofile to the ticketin backend, with a local AST and sqlite db inbetween:

```
.todo file → Parser → Internal AST → SQLite (via Alembic) → Service Mapper → Jira API
```

### Parser (`todo_files/parser.py`)

Reads a `.todo` file and produces a `ParsedFile`. Responsibilities:

- Parses the YAML frontmatter block into a `FileConfig` object
- Parses the body into an ordered list of `Ticket` objects and free-form string blocks
- Preserves everything else in the file as is
- Handles nested subtasks (parsed recursively at a deeper indent level)
- Handles multiline field values using YAML block-scalar (`|`) syntax

### Internal AST (`todo_files/models.py`)

The shared data model imported by every other layer:

| Class | Purpose |
|---|---|
| `Ticket` | One ticket: title, status, id, remote_key, labels, description, subtasks, extra fields |
| `FileConfig` | File-level defaults: board, item_type, labels, status_map, assignee, sprint |
| `ParsedFile` | The full file: path + config + ordered list of `Ticket | str` items |

### Storage (`todo_files/storage/`)

SQLite + Alembic. Tracks every known ticket and its sync state so that `push` can diff local state against the last-pushed snapshot without making an API call.

### Sync engine (`todo_files/sync.py`)

Bridges the parser and the storage/Jira layers. Responsibilities:

- **`assign_ids`** - walks the parsed ticket tree and assigns 8-char hex UUIDs to any ticket that lacks one; returns `True` if the file needs to be written back
- **`ticket_hash`** - stable SHA-256 of a ticket's content fields (excludes `id`/`remote_key`), used to detect local changes
- **`build_plan`** - diffs a `ParsedFile` against the DB and returns a `SyncPlan` (lists of tickets to create, update, delete, untrack, or leave clean); does not modify anything
- **`execute_plan`** - applies the plan to SQLite; Jira calls are the CLI's responsibility
- **`mark_synced`** - sets `sync_status=clean` for tickets that were successfully pushed

### Service mapper (`todo_files/mappers/`)

The only Jira-specific code. `JiraMapper` implements `BaseMapper` and translates between the internal AST and Jira REST API v3.

Status updates require a Jira transition (not a direct field update): the mapper fetches available transitions, matches by name, and POSTs the transition.

Adding a new backend (e.g. Linear, ClickUp) requires a new mapper class - parser, AST, and storage should remain unchanged.

## CLI (`todo_files/cli.py`)

```
todofiles push <file>             # parse → SQLite → push to Jira
todofiles push --dry-run <file>   # show what would change (no API call)
todofiles pull <file>             # fetch from Jira → update SQLite + file
todofiles pull --dry-run <file>   # show what would change without writing
todofiles import <file> <key>     # fetch an existing Jira ticket and append it to file
todofiles config set <key=val>    # set a config value
todofiles config show             # print current config (api_token redacted)
todofiles config whoami           # print your Jira account info
todofiles config status-map       # print a status_map template from your Jira project
```

#### `push` flow
1. Parse file → assign missing IDs → write IDs back if any were assigned
2. Build sync plan (DB diff, no API call)
3. Print plan; prompt for confirmation on each deletion (`delete` / `untrack` / `abort`)
4. For each CREATE: `POST /rest/api/3/issue` → write `jira: KEY` back to file and DB
5. For each UPDATE: `PUT /rest/api/3/issue/{key}` + transition if status changed
6. For each DELETE: `DELETE /rest/api/3/issue/{key}`
7. Update `sync_status → clean` in DB

### `pull` flow
1. Parse file → ensure all tickets have IDs
2. For each ticket with a `remote_key`, fetch from Jira and update the in-memory AST (remote wins)
3. Write the updated file; update DB; mark pulled tickets clean

## Configuration

Config follows the [XDG Base Directory Specification](https://specifications.freedesktop.org/basedir-spec/latest/):

- File: `$XDG_CONFIG_HOME/todofiles/config.yaml` (default: `~/.config/todofiles/config.yaml`)
- Sensitive values (`api_token`) are stored with user-only permissions (`chmod 600`)

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

