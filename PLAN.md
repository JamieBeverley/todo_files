
# Motivation
As a developer, I keep track of my tasks in local .md files and any time I need to copy those things to Jira/Clickup/whichever tool we're using its a pain.
As a result, I often just don't so PMs and others lack visibility on what I'm working on and current progress.

# Objective

Create a tool that can read my local .md files and create/update/delete tickets in our ticketing system when I save changes to those files.

# Scope

Lets start just with Jira (but keep in mind future extensibility to other ticketing services).

# Design

- We'll need to create a syntax that makes sense. Here's a rough idea:
```markdown
# project_x.todo
---
# A yaml section containing file-level defaults/configs
# e.g. for Jira, all tickets maybe have default labels, a default board, etc...
labels: ["my_team","my_project"]
board: "BackendTeamBoard"
item_type: "task"
---

# A list of ticket in a sort-of checkbox like syntax
[ ] - add json output format to products API

# Tickets may have different statuses - represent those with text inside the [ ]'s, for example:
[in_prog] - refactor frontend to use v2 API

# finished tickets maybe just have an `[x]`? Or let users define some mapping between these short-codes and corresponding
# statuses in the ticketing backend
[x] - fix css alignment issue on homepage

# Allow adding sub-fields with indented bullets and key identifiers:
[in_prog] - add slack integration
    item_type: "story"
    # Ideally appear as subtasks?
    subtasks:
        [x] - create oauth callback endpoint
        [ ] - register new Slack App
    description: |
        Add integration with our slack bot using Oauth workflows
        etc...etc...etc..
    labels: ["custom_label2"] 
```
- We'll need a parser that can parse this file, produce datatypes for each ticket
- I think we'll want to persist that datatype to some local storage (maybe duckdb or sqlite?)
- Some way of syncing that local persistent storage to the cloud service (like Jira):
  - probably a mapping from the library's internal data type to the cloud service's API types
- Some UI/CLI for actually running that sync:
  - at minimum could just be `todofiles push my_project.todo` which will:
    - parse the given todo file (error out if syntax errors) 
    - save the internal representation (e.g. to duckdb)
    - push the internal representation to Jira
  - more advanced API might:
    - run a daemon that automatically parses to internal representation and writes to duckdb
    - maybe if `todofiles config set autopush=true`, it auto-pushes to jira on save


# Questions
- Is this a good system design? Is it good to separate: 'parser', internal data structure/AST, internal storage, mapping to external ticket service, UI/CLI/config?
- How would you improve it?




